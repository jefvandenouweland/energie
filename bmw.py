"""BMW CarData for the energy monitor (standard library only).

One-time login (authorises this monitor in your BMW account, no password is stored):
    python3 energy.py config.json bmw-login
After that energy.py keeps the tokens fresh and reads the battery state of charge from
BMW's CarData stream (MQTT over TLS). BMW only sends data when the car reports.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

AUTH_BASE = "https://customer.bmwgroup.com/gcdm/oauth"
MQTT_HOST, MQTT_PORT = "customer.streaming-cardata.bmwgroup.com", 9000
SCOPE = "authenticate_user openid cardata:streaming:read cardata:api:read"
KEEPALIVE = 60
# Telematic keys that can carry the battery charge in %, best first. Which ones exist depends on
# the car and on the data you ticked in the BMW CarData portal: override with "soc_keys".
SOC_KEYS = [
    "vehicle.drivetrain.batteryManagement.header",
    "vehicle.powertrain.electric.battery.stateOfCharge.displayed",
    "vehicle.trip.segment.end.drivetrain.batteryManagement.hvSoc",
]


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def tls_context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def http_post(url: str, fields: dict, verify: bool = True, timeout: float = 20):
    req = urllib.request.Request(
        url, urllib.parse.urlencode(fields).encode(),
        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=tls_context(verify)) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def parse_ts(s) -> float | None:
    """ISO timestamp like 2026-03-27T17:55:52.58Z -> epoch seconds."""
    if not isinstance(s, str):
        return None
    s = re.sub(r"\.\d+", "", s).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------- tokens
class Tokens:
    """access/id/refresh tokens in one small file, refreshed shortly before they expire."""

    def __init__(self, cfg: dict, path: Path):
        self.cfg, self.path, self.mu = cfg, Path(path), threading.Lock()
        self.base = cfg.get("auth_base", AUTH_BASE)
        self.verify = cfg.get("tls_verify", True)
        self.d: dict = {}
        self._reload()

    def _reload(self) -> None:
        try:
            self.d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.d = {}

    def store(self, r: dict) -> None:
        d = self.d
        d.update(access_token=r["access_token"], id_token=r.get("id_token", d.get("id_token")),
                 refresh_token=r.get("refresh_token", d.get("refresh_token")),
                 gcid=r.get("gcid", d.get("gcid")), expires_at=time.time() + int(r.get("expires_in", 3600)))
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        if os.geteuid() == 0:  # login run as root: hand the file to whoever owns the data dir (the service user)
            st = self.path.parent.stat()
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, self.path)

    def refresh(self) -> None:
        status, r = http_post(self.base + "/token", {
            "grant_type": "refresh_token", "refresh_token": self.d.get("refresh_token", ""),
            "client_id": self.cfg["client_id"]}, self.verify)
        if status != 200 or "access_token" not in r:
            raise RuntimeError(f"token refresh failed (HTTP {status} {r.get('error_description') or r.get('error') or ''}); "
                               "run 'bmw-login' again if this keeps happening")
        self.store(r)

    def ensure(self, margin: int = 300) -> dict:
        with self.mu:
            if not self.d.get("refresh_token"):
                self._reload()  # login may have been done after the service started
            if not self.d.get("refresh_token"):
                raise RuntimeError("not logged in to BMW yet: run 'python3 energy.py config.json bmw-login'")
            if time.time() > self.d.get("expires_at", 0) - margin:
                self.refresh()
            return dict(self.d)


def login(cfg: dict, data_dir: Path) -> None:
    """OAuth device flow with PKCE: show a code, the user approves it in the BMW portal."""
    tok = Tokens(cfg, data_dir / "bmw-tokens.json")
    verifier = b64u(secrets.token_bytes(32))
    status, r = http_post(tok.base + "/device/code", {
        "client_id": cfg["client_id"], "response_type": "device_code", "scope": SCOPE,
        "code_challenge": b64u(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}, tok.verify)
    if status != 200 or "device_code" not in r:
        sys.exit(f"BMW refused the login request (HTTP {status} {r}). Check client_id and that the CarData client exists.")
    print("\n1. Open this page on any device and sign in with your BMW account:")
    print("   ", r.get("verification_uri_complete") or r.get("verification_uri"))
    print("2. If asked for a code, enter:", r.get("user_code"))
    print("\nWaiting for your approval ...", flush=True)
    interval, deadline = int(r.get("interval", 5)), time.time() + int(r.get("expires_in", 600))
    while time.time() < deadline:
        time.sleep(interval)
        status, t = http_post(tok.base + "/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": cfg["client_id"],
            "device_code": r["device_code"], "code_verifier": verifier}, tok.verify)
        if status == 200 and "access_token" in t:
            tok.store(t)
            print("Logged in. Tokens saved to", tok.path, "- now (re)start the energy-monitor service.")
            return
        err = t.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        sys.exit(f"Login failed: {err or status} {t.get('error_description', '')}")
    sys.exit("Login timed out: run the command again.")


# --------------------------------------------------------------------------- minimal MQTT client
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | 128 if n else b)
        if not n:
            return bytes(out)


def _str(s) -> bytes:
    b = s.encode() if isinstance(s, str) else s
    return struct.pack(">H", len(b)) + b


def _recv(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IOError("connection closed by BMW")
        buf += chunk
    return buf


def read_packet(sock):
    """-> (first byte, body) or None when nothing arrived before the socket timeout."""
    try:
        first = sock.recv(1)
    except (socket.timeout, TimeoutError):
        return None
    if not first:
        raise IOError("connection closed by BMW")
    n, mult = 0, 1
    while True:
        b = _recv(sock, 1)[0]
        n += (b & 127) * mult
        mult *= 128
        if not b & 128:
            break
    return first[0], _recv(sock, n) if n else b""


class Stream:
    def __init__(self, cfg: dict, tokens: Tokens, on_soc, log):
        self.cfg, self.tokens, self.on_soc, self.log = cfg, tokens, on_soc, log
        self.keys = cfg.get("soc_keys") or SOC_KEYS
        self.rank = len(self.keys)       # rank of the key in use (lower = better)
        self.seen: set = set()
        self.ver, self.pw, self.flips = 5, "id_token", 0
        self.state = None

    def note(self, msg: str) -> None:
        if msg != self.state:
            self.state = msg
            self.log("bmw: " + msg)

    # -- one connection, until the tokens need renewing
    def session(self, d: dict) -> None:
        host, port = self.cfg.get("mqtt_host", MQTT_HOST), int(self.cfg.get("mqtt_port", MQTT_PORT))
        ctx = tls_context(self.cfg.get("tls_verify", True))
        sock = ctx.wrap_socket(socket.create_connection((host, port), 15), server_hostname=host)
        try:
            sock.settimeout(15)
            ver = self.ver
            body = (_str("MQTT") + bytes([ver, 0xC2]) + struct.pack(">H", KEEPALIVE) + (b"\x00" if ver == 5 else b"")
                    + _str("energy-" + secrets.token_hex(4)) + _str(d["gcid"]) + _str(d[self.pw]))
            sock.sendall(bytes([0x10]) + _varint(len(body)) + body)
            try:
                pkt = read_packet(sock)
            except (IOError, OSError) as e:  # some brokers simply hang up on a protocol version they don't speak
                self.flips += 1
                if self.flips > 3:
                    raise
                self.ver = 4 if ver == 5 else 5
                self.note(f"connection closed after MQTT v{'5' if ver == 5 else '3.1.1'} connect ({e}); trying the other version")
                return
            if pkt is None or pkt[0] >> 4 != 2 or len(pkt[1]) < 2:
                raise IOError("no valid reply to the MQTT connect")
            code = pkt[1][1]
            if code != 0:
                self.refused(ver, code)
                return
            self.flips = 0
            vin = self.cfg.get("vin")
            topic = f"{d['gcid']}/{vin}" if vin else f"{d['gcid']}/+"
            sub = struct.pack(">H", 1) + (b"\x00" if ver == 5 else b"") + _str(topic) + b"\x01"
            sock.sendall(bytes([0x82]) + _varint(len(sub)) + sub)
            self.note(f"stream connected (MQTT v{'5' if ver == 5 else '3.1.1'}, topic {topic.replace(d['gcid'], '<gcid>')})")
            self.pump(sock, d, ver)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def refused(self, ver: int, code: int) -> None:
        self.flips += 1
        if self.flips > 3:
            raise RuntimeError(f"BMW refused the stream connection (code {code}); check the CarData client subscriptions")
        if code in (1, 0x84):  # protocol version not supported (old-style code 1 or MQTT 5 reason 0x84)
            self.ver = 4 if ver == 5 else 5
        elif code in ((4, 5) if ver == 4 else (0x86, 0x87)):  # bad credentials / not authorised
            self.tokens.refresh()
            self.pw = "access_token" if self.pw == "id_token" else "id_token"
            raise RuntimeError(f"stream login refused (code {code}), retrying with the {self.pw} as password")
        else:
            raise RuntimeError(f"BMW refused the stream connection (code {code})")

    def pump(self, sock, d: dict, ver: int) -> None:
        last_tx = time.monotonic()
        renew_at = d.get("expires_at", time.time() + 3000) - 300
        while time.time() < renew_at:
            sock.settimeout(max(1, min(KEEPALIVE / 2, renew_at - time.time())))
            pkt = read_packet(sock)
            if pkt is None:
                if time.monotonic() - last_tx >= KEEPALIVE / 2:
                    sock.sendall(b"\xc0\x00")
                    last_tx = time.monotonic()
                continue
            kind, body = pkt[0] >> 4, pkt[1]
            if kind == 3:  # PUBLISH
                qos = (pkt[0] >> 1) & 3
                tlen = struct.unpack(">H", body[:2])[0]
                i = 2 + tlen
                if qos:
                    pid = body[i:i + 2]
                    i += 2
                    sock.sendall(b"\x40\x02" + pid)
                    last_tx = time.monotonic()
                if ver == 5:
                    plen, shift = 0, 0
                    while True:
                        b = body[i]
                        i += 1
                        plen |= (b & 127) << shift
                        shift += 7
                        if not b & 128:
                            break
                    i += plen
                self.message(body[i:])
            elif kind == 9 and body and body[-1] >= 0x80:
                raise RuntimeError("BMW refused the subscription (is the CarData stream enabled for this car?)")
            elif kind == 14:
                raise IOError("BMW closed the stream")

    def message(self, payload: bytes) -> None:
        try:
            msg = json.loads(payload)
            data = msg.get("data") or {}
        except (ValueError, AttributeError):
            return
        new = [k for k in data if k not in self.seen]
        if new:
            self.seen.update(new)
            hint = sorted(k for k in new if re.search(r"soc|batter|charg", k, re.I))
            keys = hint or sorted(new)
            shown = ", ".join(keys[:8])
            if len(keys) > 8:
                shown += f", ... (+{len(keys) - 8})"
            detail = f"battery-related keys: {shown}" if hint else f"sample keys (none look battery-related): {shown}"
            self.log(f"bmw: {len(new)} new data point(s) in the stream; {detail}")
        for rank, key in enumerate(self.keys):
            if key not in data or rank > self.rank:
                continue
            e = data[key]
            try:
                v = float(e.get("value") if isinstance(e, dict) else e)
            except (TypeError, ValueError):
                continue
            if not 0 <= v <= 100:
                continue
            ts = parse_ts(e.get("timestamp") if isinstance(e, dict) else None) or parse_ts(msg.get("timestamp")) or time.time()
            self.rank = rank
            self.on_soc(ts, v, key)

    def run(self) -> None:
        backoff = 20
        while True:
            try:
                self.session(self.tokens.ensure())
                backoff = 20
            except Exception as e:  # noqa: BLE001 - keep trying whatever happens
                self.note(f"ERROR {type(e).__name__}: {e}; retrying in {backoff}s")
                time.sleep(backoff)
                backoff = min(300, backoff * 2)
