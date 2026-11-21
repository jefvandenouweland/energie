#!/usr/bin/env python3
"""Minimal home energy monitor (standard library only).

Polls a HomeWizard P1 meter (HTTP), an Alfen EV charger (HTTPS API) and an SMA
inverter (Modbus TCP) once per second, keeps the last 24 h in memory, persists it
to one small binary file and serves a single-page graph UI plus a tiny JSON API.

Usage: python3 energy.py [config.json] [bmw-login]
"""
from __future__ import annotations

import gzip
import json
import math
import os
import signal
import socket
import ssl
import struct
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
WINDOW = 24 * 3600          # seconds of history kept
MAX_AGE = 5                 # a reading older than this (s) counts as missing (min.; slower sources get 2 x interval + 2)
STATIC = {"manifest.webmanifest": "application/manifest+json", "icon-192.png": "image/png",
          "icon-512.png": "image/png", "icon-maskable-512.png": "image/png", "apple-touch-icon.png": "image/png"}
HISTORY_STEP = 10           # seconds per bucket in /api/history
REC = struct.Struct("<I8i")  # ts, grid/solar/charger/home (W), water (mL), and P1 phases (W); NONE = missing
REC_V3 = struct.Struct("<I5i")
SAMPLE_FIELDS = ("grid", "solar", "charger", "home", "water", "phase_l1", "phase_l2", "phase_l3")
NONE = -(2 ** 31)

samples: deque = deque(maxlen=WINDOW + 60)  # ts followed by SAMPLE_FIELDS; ints or None
pending: list = []          # samples not yet written to disk
latest: dict = {}           # source -> (value, time.time())
acc: dict = {}              # volume sources: amount reported since the last sample
ACCUMULATE = ("water",)
intervals: dict = {}        # source -> seconds between polls (default 1)
water_info: dict = {"at": None, "interval": 300}  # time of the latest meter report, seconds between reports
peak_info: dict = {}        # monthly peak (capacity tariff) from the P1: w, at (local ISO time), avg, seen
soc_events: list = []       # [(timestamp, percent)] battery charge of the car, newest last
soc_on = False              # BMW CarData configured
soc_path = None
car_position: tuple = None  # (timestamp, latitude, longitude), in memory only
reference_position: tuple = None  # (latitude, longitude), from config
reference_name = "referentiepunt"
position_path = None
dashboard_language = "nl"
dashboard_appliances: dict = {}
lock = threading.Lock()
io_lock = threading.Lock()
charger_poll_lock = threading.Lock()
charger_poll_paused = False
sources: dict = {}          # configured source name -> read() function


_log_lock = threading.Lock()


def log(msg: str) -> None:
    with _log_lock:  # one write per line, so lines from different threads don't get mixed up
        sys.stdout.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
        sys.stdout.flush()


# --------------------------------------------------------------------------- devices
class Modbus:
    """Just enough Modbus TCP to read registers (function 3 or 4)."""

    def __init__(self, host: str, port: int, timeout: float = 3.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = None
        self.tid = 0

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise IOError("connection closed")
            buf += chunk
        return buf

    def read(self, unit: int, func: int, addr: int, count: int) -> bytes:
        try:
            if self.sock is None:
                self.sock = socket.create_connection((self.host, self.port), self.timeout)
            self.tid = (self.tid + 1) & 0xFFFF
            self.sock.sendall(struct.pack(">HHHBBHH", self.tid, 0, 6, unit, func, addr, count))
            tid, _proto, length, _unit = struct.unpack(">HHHB", self._recv(7))
            body = self._recv(length - 1)
            if tid != self.tid:
                raise IOError("modbus transaction id mismatch")
            if body[0] & 0x80:
                raise IOError(f"modbus exception code {body[1]}")
            return body[2:2 + body[1]]
        except Exception:
            self.close()  # next call reconnects
            raise


class P1:
    """HomeWizard P1 local API v1 (GET /api/v1/data); one shared request per second for all readers."""

    def __init__(self, cfg: dict):
        self.cfg, self.conn, self.data, self.at, self.err = cfg, None, None, 0.0, None
        self.mu = threading.Lock()

    def get(self) -> dict:
        with self.mu:
            now = time.monotonic()
            if self.data is not None and now - self.at < 0.8:
                return self.data
            if self.err and now - self.err[0] < 0.8:
                raise self.err[1]
            try:
                if self.conn is None:
                    self.conn = HTTPConnection(self.cfg["host"], self.cfg.get("port", 80), timeout=3)
                self.conn.request("GET", "/api/v1/data")
                resp = self.conn.getresponse()
                body = resp.read()
                if resp.status != 200:
                    raise IOError(f"HTTP {resp.status} (is the Local API enabled in the app?)")
                self.data, self.at, self.err = json.loads(body), time.monotonic(), None
                return self.data
            except Exception as e:
                if self.conn:
                    self.conn.close()
                    self.conn = None
                self.data, self.err = None, (time.monotonic(), e)
                raise


def peak_update(d: dict) -> None:
    """Belgian capacity tariff: highest quarter-hour average of the month, straight from the meter.

    HomeWizard spells the field "montly_power_peak_w". It is part of the same /api/v1/data answer we
    already fetch for the grid power, so this costs no extra request; it is only refreshed once a minute.
    """
    if d.get("montly_power_peak_w") is None or time.time() - peak_info.get("seen", 0) < 60:
        return
    try:
        at = datetime.strptime(f"{int(d['montly_power_peak_timestamp']):012d}", "%y%m%d%H%M%S").isoformat()
    except (KeyError, TypeError, ValueError):
        at = None
    avg = d.get("active_power_average_w")
    with lock:
        peak_info.update(w=float(d["montly_power_peak_w"]), at=at, avg=None if avg is None else float(avg), seen=time.time())


def make_grid(p1: P1):
    """P1 active power in W (+ import, - export), including the three phases."""
    def read() -> dict:
        d = p1.get()
        peak_update(d)
        return {"grid": float(d["active_power_w"]),
                "three_phase": all(d.get(f"active_power_l{i}_w") is not None for i in range(1, 4)),
                **{f"phase_l{i}": float(d[f"active_power_l{i}_w"])
                   for i in range(1, 4) if d.get(f"active_power_l{i}_w") is not None}}
    return read


def make_water(p1: P1):
    """Water used since the previous poll, in litres, from the water meter on the P1.

    The P1 only exposes the cumulative reading (m3) of a connected water meter in
    data["external"], refreshed whenever the meter reports (seconds to minutes). The volume
    is attributed to the moment a new reading arrives, so totals are exact.
    """
    st = {"key": None, "value": 0.0, "changed": time.monotonic()}

    def read() -> float:
        meters = [e for e in p1.get().get("external") or [] if e.get("type") == "water_meter"]
        if not meters:
            raise IOError("no water meter in the P1 'external' data (is it connected to the P1?)")
        value = float(meters[0]["value"])
        key = (meters[0].get("timestamp"), value)
        if key == st["key"]:
            if time.monotonic() - st["changed"] > 7200:
                raise IOError("water meter reading is stale (no new report for 2 h)")
            return 0.0
        used = 0.0 if st["key"] is None else max(0.0, (value - st["value"]) * 1000)
        st.update(key=key, value=value, changed=time.monotonic())
        water_info["at"] = time.time()
        return used

    return read


def make_alfen(cfg: dict):
    """Alfen charger via its local HTTPS API: GET /api/prop?ids=2221_16 (socket 1 total power, W).

    The charger uses a self-signed certificate (not verified) and a session cookie from
    POST /api/login. Without a "password" in the config no login is attempted.
    The charger only has a few session slots, so after every reading we POST /api/logout and
    close the connection (unless "logout": false); that keeps the Alfen app able to log in.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    prop = cfg.get("property", "2221_16")
    st = {"conn": None, "cookie": None, "logged_in": False}

    def request(method: str, path: str, body=None):
        if st["conn"] is None:
            st["conn"] = HTTPSConnection(cfg["host"], cfg.get("port", 443), timeout=5, context=ctx)
        headers = {"Content-Type": "application/json"}
        if st["cookie"]:
            headers["Cookie"] = st["cookie"]
        st["conn"].request(method, path, None if body is None else json.dumps(body), headers)
        resp = st["conn"].getresponse()
        return resp, resp.read()

    def login() -> None:
        body = {"username": cfg.get("username", "admin"), "password": cfg["password"], "displayname": "energy"}
        resp, _ = request("POST", "/api/login", body)
        if resp.status != 200:
            raise IOError(f"login failed (HTTP {resp.status}), check username/password")
        cookies = resp.msg.get_all("Set-Cookie") or []
        st["cookie"] = "; ".join(c.split(";")[0] for c in cookies) or None
        st["logged_in"] = True

    def logout() -> None:
        try:
            request("POST", "/api/logout")
        except Exception:  # noqa: BLE001 - best effort
            pass
        st["logged_in"], st["cookie"] = False, None
        if st["conn"]:
            st["conn"].close()
            st["conn"] = None

    def read() -> float:
        try:
            if "password" in cfg and not st["logged_in"]:
                login()
            resp, data = request("GET", f"/api/prop?ids={prop}")
            if resp.status in (401, 403) and "password" in cfg:  # session expired
                login()
                resp, data = request("GET", f"/api/prop?ids={prop}")
            if resp.status != 200:
                raise IOError(f"HTTP {resp.status}")
            for p in json.loads(data)["properties"]:
                if p["id"] == prop:
                    value = float(p["value"])
                    break
            else:
                raise IOError(f"property {prop} not in response")
            if "password" in cfg and cfg.get("logout", True):
                logout()
            return value
        except Exception:
            if st["conn"]:
                st["conn"].close()
                st["conn"] = None
            raise

    def close() -> None:
        if st["conn"] is not None:
            logout()

    read.close = close
    return read


def make_sma(cfg: dict):
    """SMA inverter: total AC power as S32 over 2 registers (W); NaN (night) -> 0."""
    mb = Modbus(cfg["host"], cfg.get("port", 502))
    unit, reg = cfg.get("unit", 3), cfg.get("register", 30775)

    def read() -> float:
        v = struct.unpack(">i", mb.read(unit, cfg.get("function", 3), reg, 2))[0]
        return 0.0 if v == NONE else float(v)

    return read


def poll_interval(cfg: dict, default: float, name: str) -> float:
    value = cfg.get("interval", default)
    if isinstance(value, bool):
        sys.exit(f'{name} "interval" must be a number of seconds >= 0.1')
    try:
        value = float(value)
    except (TypeError, ValueError):
        sys.exit(f'{name} "interval" must be a number of seconds >= 0.1')
    if not math.isfinite(value) or value < 0.1:
        sys.exit(f'{name} "interval" must be a number of seconds >= 0.1')
    return value


def water_report_interval(cfg) -> int:
    if isinstance(cfg, dict):
        value = cfg.get("report_interval", cfg.get("interval", 300))
    else:
        value = 300
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 1:
        sys.exit('HomeWizard "water.report_interval" must be a number of seconds >= 1')
    return int(value)


def parse_reference_position(cfg: dict) -> tuple:
    bmw_cfg = cfg.get("bmw") or {}
    if "home" not in bmw_cfg and "reference_position" not in cfg:
        return None, "referentiepunt"
    try:
        if "home" in bmw_cfg:
            position = bmw_cfg["home"]
            if not isinstance(position, str) or len(position.split(",")) != 2:
                raise ValueError
            latitude, longitude = map(float, position.split(","))
            name = "Home"
        else:
            legacy = cfg["reference_position"]
            latitude, longitude = float(legacy["latitude"]), float(legacy["longitude"])
            name = str(legacy.get("name") or "Home")
    except (KeyError, TypeError, ValueError):
        sys.exit('"bmw.home" must be "latitude,longitude" (or use the legacy "reference_position" object)')
    if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        sys.exit('"bmw.home" latitude must be between -90 and 90 and longitude between -180 and 180')
    return (latitude, longitude), name


def publish_reading(name: str, value):
    if isinstance(value, dict):
        reported = value.get(name)
        at = time.time()
        with lock:
            latest.update({key: (reading, at) for key, reading in value.items()})
        return reported
    with lock:
        if name in ACCUMULATE:  # volumes add up until the sampler takes them
            acc[name] = acc.get(name, 0.0) + value
            latest[name] = (0.0, time.time())
        else:
            latest[name] = (value, time.time())
    return value


def poller(name: str, read) -> None:
    ok = None
    while True:
        t0 = time.monotonic()
        try:
            if name == "charger":
                with charger_poll_lock:
                    value = None if charger_poll_paused else read()
                    reported = None if value is None else publish_reading(name, value)
            else:
                value = read()
                reported = None if value is None else publish_reading(name, value)
            if value is not None:  # None = nothing to report yet
                if ok is not True:
                    log(f"{name}: ok ({reported:g})")
                    ok = True
        except Exception as e:  # noqa: BLE001 - keep polling whatever happens
            if ok is not False:
                log(f"{name}: ERROR {type(e).__name__}: {e}")
                ok = False
        time.sleep(max(0.05, intervals.get(name, 1.0) - (time.monotonic() - t0)))


def fresh(name: str, now: float):
    r = latest.get(name)
    return r[0] if r and now - r[1] <= max(MAX_AGE, 2 * intervals.get(name, 1.0) + 2) else None


def sampler() -> None:
    last = 0
    while True:
        time.sleep(1.05 - (time.time() % 1))  # just after each wall-clock second
        now = time.time()
        ts = int(now)
        if ts == last:
            continue
        last = ts
        with lock:
            v = {n: fresh(n, now) for n in sources}
            phases = tuple(fresh(f"phase_l{i}", now) for i in range(1, 4))
            used = acc.pop("water", 0.0)  # litres reported since the previous sample
        g, s, c = v.get("grid"), v.get("solar"), v.get("charger")
        if s is not None:
            s = max(0.0, s)  # inverter standby noise
        if c is not None:
            c = max(0.0, c)
        # Home = what the house uses without the charger = grid + solar - charger.
        # Needs grid and solar (an unconfigured source counts as 0). If only the charger is
        # offline, home becomes grid + solar = the total consumption (the page then shows it
        # as "huis + laadpaal, verdeling onbekend"), so the total never disappears.
        # Clamped at 0 because the sources are not sampled at exactly the same instant.
        home = None
        if all(v[k] is not None for k in ("grid", "solar") if k in v):
            home = max(0.0, (g or 0) + (s or 0) - (c or 0))
        w = None if v.get("water") is None else used * 1000  # millilitres
        row = (ts,) + tuple(None if x is None else int(round(x)) for x in (g, s, c, home))
        row += (None if w is None else int(round(w)),)
        row += tuple(None if x is None else int(round(x)) for x in phases)
        with lock:
            samples.append(row)
            pending.append(row)


# --------------------------------------------------------------------------- car battery (BMW CarData)
def position_add(ts: float, latitude: float, longitude: float) -> None:
    global car_position
    with lock:
        if car_position is not None and ts < car_position[0]:
            return
    if position_path:
        with io_lock:
            tmp = position_path.with_suffix(".tmp")
            tmp.write_text(json.dumps([ts, latitude, longitude]) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, position_path)
    with lock:
        if car_position is None or ts >= car_position[0]:
            car_position = (ts, latitude, longitude)


def position_load(path: Path) -> None:
    global car_position, position_path
    position_path = path
    try:
        row = json.loads(path.read_text())
        ts, latitude, longitude = float(row[0]), float(row[1]), float(row[2])
    except FileNotFoundError:
        return
    except (IndexError, KeyError, TypeError, ValueError) as e:
        log(f"ignoring invalid saved car location in {path}: {e}")
        return
    if not all(map(math.isfinite, (ts, latitude, longitude))) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        log(f"ignoring invalid saved car location in {path}: coordinates out of range")
        return
    with lock:
        car_position = (ts, latitude, longitude)
    log(f"loaded last car location from {path}")


def soc_add(ts: float, value: float, key: str = "") -> None:
    ts = int(ts)
    with lock:
        if soc_events and ts <= soc_events[-1][0]:
            return  # older or duplicate report
        soc_events.append((ts, round(value, 1)))
    if soc_path:
        with io_lock, open(soc_path, "a") as f:
            f.write(json.dumps([ts, round(value, 1)]) + "\n")


def soc_load(path: Path) -> None:
    """Read the saved events, keep 24 h plus the one before it (to continue the line), rewrite the file."""
    global soc_path
    soc_path = path
    rows = []
    try:
        for line in path.read_text().splitlines():
            try:
                ts, v = json.loads(line)
                rows.append((int(ts), float(v)))
            except (ValueError, TypeError):
                pass
    except FileNotFoundError:
        return
    rows.sort()
    cutoff = time.time() - WINDOW
    keep = [r for r in rows if r[0] > cutoff]
    older = [r for r in rows if r[0] <= cutoff]
    soc_events.extend(older[-1:] + keep)
    path.write_text("".join(json.dumps(list(r)) + "\n" for r in soc_events))
    log(f"loaded {len(soc_events)} battery readings from {path}")


# --------------------------------------------------------------------------- persistence
def load(path: Path) -> None:
    legacy = path.with_name("samples-v3.bin")
    older_legacy = path.with_name("samples-v2.bin")
    src = path if path.exists() else legacy if legacy.exists() else older_legacy
    old_format = src != path
    try:
        record = REC if not old_format else REC_V3
        n = src.stat().st_size // record.size
    except FileNotFoundError:
        return
    keep = min(n, WINDOW)
    with open(src, "rb") as f:
        f.seek((n - keep) * record.size)
        data = f.read(keep * record.size)
    cutoff = time.time() - WINDOW
    for ts, *vals in record.iter_unpack(data):
        if ts > cutoff:
            vals = [None if x == NONE else x for x in vals]
            if src == older_legacy:
                vals[4] = None
            if old_format:
                vals.extend((None, None, None))
            samples.append((ts,) + tuple(vals))
    log(f"loaded {len(samples)} samples from {src}")
    if old_format:
        compact(path)


def pack(rows) -> bytes:
    return b"".join(REC.pack(ts, *[NONE if x is None else x for x in vals]) for ts, *vals in rows)


def flush(path: Path) -> None:
    with io_lock:
        with lock:
            batch = pending[:]
            pending.clear()
        if batch:
            with open(path, "ab") as f:
                f.write(pack(batch))


def compact(path: Path) -> None:
    """Rewrite the file with only the last 24 h (called hourly)."""
    with io_lock:
        with lock:
            snap = list(samples)
            pending.clear()  # already part of snap
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(pack(snap))
        os.replace(tmp, path)


def persist_loop(path: Path) -> None:
    last_compact = time.time()
    while True:
        time.sleep(60)  # one small append per minute keeps SD-card wear negligible
        flush(path)
        if time.time() - last_compact > 3600:
            compact(path)
            last_compact = time.time()


# --------------------------------------------------------------------------- web
def extras(history: bool = False) -> dict:
    """Monthly peak and car battery for the page."""
    with lock:
        out = {"peak": {k: peak_info.get(k) for k in ("w", "at", "avg")} if peak_info else None,
               "soc_on": soc_on, "soc_last": list(soc_events[-1]) if soc_events else None}
        if car_position and reference_position:
            ts, latitude, longitude = car_position
            lat1, lon1 = map(math.radians, reference_position)
            lat2, lon2 = math.radians(latitude), math.radians(longitude)
            dlat, dlon = lat2 - lat1, lon2 - lon1
            a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
            out["car_distance"] = {"km": 6371.0088 * 2 * math.asin(math.sqrt(min(1.0, a))),
                                   "at": int(ts), "name": reference_name}
        if history and soc_on:
            cutoff = time.time() - WINDOW
            ev = [list(e) for e in soc_events if e[0] > cutoff]
            before = [list(e) for e in soc_events if e[0] <= cutoff][-1:]
            out["soc"] = before + ev
    return out


def charger_polling_state() -> dict:
    with charger_poll_lock:
        return {"available": "charger" in sources, "paused": charger_poll_paused}


def three_phase_available() -> bool:
    now = time.time()
    with lock:
        return "grid" in sources and fresh("three_phase", now) is True


def history_payload() -> dict:
    now = int(time.time())
    cutoff = now - WINDOW
    with lock:
        snap = [s for s in samples if s[0] > cutoff]
    buckets: dict = {}
    for ts, *vals in snap:
        b = buckets.setdefault(ts // HISTORY_STEP, [0, 0, [0] * len(SAMPLE_FIELDS), [0] * len(SAMPLE_FIELDS)])
        b[0] += 1
        b[1] += ts
        for i, x in enumerate(vals):
            if x is not None:
                b[2][i] += x
                b[3][i] += 1
    keys = sorted(buckets)
    out = {"t": [round(buckets[k][1] / buckets[k][0]) for k in keys]}
    for i, name in enumerate(SAMPLE_FIELDS):
        out[name] = [  # powers are averaged, water volumes are summed
            None if not buckets[k][3][i] else buckets[k][2][i] if name == "water"
            else round(buckets[k][2][i] / buckets[k][3][i])
            for k in keys
        ]
    out["until"] = snap[-1][0] if snap else now - 1
    out["water_at"] = int(water_info["at"]) if water_info["at"] else None
    out["water_interval"] = water_info["interval"]
    out["now"] = now
    out["language"] = dashboard_language
    out["three_phase"] = three_phase_available()
    out["appliances"] = dashboard_appliances
    out["charger_polling"] = charger_polling_state()
    out["src"] = source_status()
    out.update(extras(history=True))
    return out


def live_payload(since: int) -> dict:
    now = int(time.time())
    out: list = []
    reload = since < now - 120  # client was away too long (or just started): refetch history
    if not reload:
        with lock:
            for s in reversed(samples):
                if s[0] <= since:
                    break
                out.append(s)
        out.reverse()
    return {"now": now, "reload": reload, "samples": out, "src": source_status(),
            "water_at": int(water_info["at"]) if water_info["at"] else None,
            "water_interval": water_info["interval"], "language": dashboard_language,
            "three_phase": three_phase_available(), "appliances": dashboard_appliances,
            "charger_polling": charger_polling_state(), **extras()}


def source_status() -> dict:
    now = time.time()
    with lock:
        return {n: fresh(n, now) is not None for n in sources}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: one TCP connection for the 1 Hz polling

    def log_message(self, *args) -> None:
        pass

    def reply(self, body: bytes, ctype: str, status: int = 200, cache: str = "no-store") -> None:
        headers = [("Content-Type", ctype), ("Cache-Control", cache)]
        if len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, 5)
            headers.append(("Content-Encoding", "gzip"))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path.startswith("/static/") and url.path[8:] in STATIC:  # app icon + manifest (fixed list)
            f = HERE / "static" / url.path[8:]
            try:
                self.reply(f.read_bytes(), STATIC[url.path[8:]], cache="public, max-age=86400")
            except FileNotFoundError:
                self.reply(b"not found", "text/plain", 404)
            except OSError as e:  # e.g. the service user may not read the file: say so once, don't spam
                if not getattr(Handler, "_warned", False):
                    Handler._warned = True
                    log(f"cannot read {f}: {e.strerror}. Fix with: sudo chmod -R a+rX {HERE / 'static'}")
                self.reply(b"not readable", "text/plain", 500)
        elif url.path in ("/", "/index.html"):
            self.reply((HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/history":
            self.reply(json.dumps(history_payload()).encode(), "application/json")
        elif url.path == "/api/live":
            try:
                since = int(parse_qs(url.query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            self.reply(json.dumps(live_payload(since)).encode(), "application/json")
        else:
            self.reply(b"not found", "text/plain", 404)

    def do_POST(self) -> None:
        global charger_poll_paused
        url = urlparse(self.path)
        if url.path != "/api/charger-polling":
            self.reply(b"not found", "text/plain", 404)
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self.reply(b"expected application/json", "text/plain", 415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.reply(b"invalid JSON request", "text/plain", 400)
            return
        if not isinstance(body, dict) or type(body.get("paused")) is not bool:
            self.reply(b'"paused" must be a boolean', "text/plain", 400)
            return
        with charger_poll_lock:
            if "charger" not in sources:
                self.reply(b"Alfen charger is not configured", "text/plain", 409)
                return
            if body["paused"]:
                close = getattr(sources["charger"], "close", None)
                if close:
                    close()
            charger_poll_paused = body["paused"]
            if charger_poll_paused:
                with lock:
                    latest.pop("charger", None)
            state = {"available": True, "paused": charger_poll_paused}
        self.reply(json.dumps(state).encode(), "application/json")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if not isinstance(exc, (ConnectionError, TimeoutError)):  # a browser closing a page is normal, not an error
            log(f"request from {client_address[0]} failed: {exc!r}")


# --------------------------------------------------------------------------- main
def main() -> None:
    global soc_on, reference_position, reference_name, dashboard_language, dashboard_appliances
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "config.json"
    cfg = json.loads(cfg_path.read_text())
    dashboard_language = cfg.get("language", "nl")
    if dashboard_language not in ("en", "nl"):
        sys.exit('"config "language" must be "en" or "nl"')
    appliance_config = cfg.get("appliances", {})
    if not isinstance(appliance_config, dict):
        sys.exit('"appliances" must be an object with optional "3phase", "l1", "l2", and "l3" names')
    if any(key not in ("3phase", "l1", "l2", "l3") for key in appliance_config):
        sys.exit('"appliances" may only contain "3phase", "l1", "l2", and "l3"')
    if any(not isinstance(value, str) for value in appliance_config.values()):
        sys.exit('each "appliances" entry must be a string')
    dashboard_appliances = {key: value.strip() for key, value in appliance_config.items() if value.strip()}
    reference_position, reference_name = parse_reference_position(cfg)

    homewizard_cfg = cfg.get("homewizard")
    p1 = P1(homewizard_cfg) if homewizard_cfg else None
    if p1:
        intervals["grid"] = poll_interval(homewizard_cfg, 1, "homewizard")
        for i in range(1, 4):
            intervals[f"phase_l{i}"] = intervals["grid"]
        sources["grid"] = make_grid(p1)
    if "sma" in cfg:
        intervals["solar"] = poll_interval(cfg["sma"], 1, "sma")
        sources["solar"] = make_sma(cfg["sma"])
    if "alfen" in cfg:
        intervals["charger"] = poll_interval(cfg["alfen"], 10, "alfen")  # be gentle with the charger
        sources["charger"] = make_alfen(cfg["alfen"])
    water_cfg = homewizard_cfg.get("water") if homewizard_cfg else None
    if water_cfg is None and cfg.get("water"):  # accept the former top-level setting
        water_cfg = cfg["water"]
    if water_cfg is not None and water_cfg is not False:
        if not isinstance(water_cfg, (bool, dict)):
            sys.exit('HomeWizard "water" must be an object or true')
        if not p1:
            sys.exit('"homewizard" is required to read its nested water meter')
        intervals["water"] = poll_interval(homewizard_cfg, 1, "homewizard")
        water_info["interval"] = water_report_interval(water_cfg)
        sources["water"] = make_water(p1)
    if not sources and "bmw" not in cfg:
        sys.exit("config has none of: homewizard, sma, alfen, bmw")

    data_dir = Path(cfg.get("data_dir") or os.environ.get("STATE_DIRECTORY") or HERE / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) > 2 and sys.argv[2] == "bmw-login":  # one-time authorisation, then exit
        import bmw
        if "bmw" not in cfg:
            sys.exit('add a "bmw" section with your CarData client_id to the config first')
        return bmw.login(cfg["bmw"], data_dir)
    path = data_dir / "samples-v4.bin"
    load(path)

    if "bmw" in cfg:
        import bmw
        soc_on = True
        soc_load(data_dir / "soc.jsonl")
        if reference_position:
            position_load(data_dir / "car-position.json")
        stream = bmw.Stream(cfg["bmw"], bmw.Tokens(cfg["bmw"], data_dir / "bmw-tokens.json"),
                            soc_add, log, position_add if reference_position else None)
        threading.Thread(target=stream.run, daemon=True).start()
    for name, read in sources.items():
        threading.Thread(target=poller, args=(name, read), daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=persist_loop, args=(path,), daemon=True).start()

    try:
        server = Server((cfg.get("bind", "0.0.0.0"), cfg.get("port", 8080)), Handler)
    except OSError as e:
        port = cfg.get("port", 8080)
        hint = {13: "no permission for this port: ports below 1024 need the systemd unit with AmbientCapabilities=CAP_NET_BIND_SERVICE "
                    "(copy the new energy-monitor.service and run 'systemctl daemon-reload'), or use \"port\": 8080",
                98: "something else already listens on this port (an older copy of this program still running? "
                    "check with: sudo ss -ltnp | grep :" + str(port) + ")"}.get(e.errno, "")
        sys.exit(f"cannot listen on port {port}: {e.strerror}. {hint}")

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log(f"serving on http://{cfg.get('bind', '0.0.0.0')}:{cfg.get('port', 8080)}  sources: {', '.join(sources)}")
    server.serve_forever()
    flush(path)
    log("stopped")


if __name__ == "__main__":
    main()
