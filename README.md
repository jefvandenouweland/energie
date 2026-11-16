# Energy monitor

Plain Python (standard library only) that polls your devices every second and serves a web page.

The page (in Dutch) shows these graphs and cards (the time range buttons are 15m, 1u, 6u and 24u):

| Graph | Source | Sign |
|---|---|---|
| Energiebalans (very top) | all in one: Huis + Laadpaal stacked, with the Zon line and the Net line (red = usage, green = injection) over it | + / − |
| Net | HomeWizard P1 | + afname (usage), − injectie (injection) |
| Zon | SMA inverter | production, ≥ 0 |
| Auto batterij (optional) | BMW CarData stream | charge in %, as steps; a dashed line means the car hasn't reported for a while |
| Water | water meter on the P1 (optional) | litres used per time slot drawn as a smooth glowing wave; header = total for the selected range |
| Maandpiek (very bottom) | the P1 (Belgian capacity tariff) | highest quarter-hour average of the month, with its date and time, and how far the current quarter is from it |

Files: `energy.py` (collector + web server), `index.html` (the page), `bmw.py` (optional BMW CarData client), `static/` (app icon and manifest), `config.example.json`, `energy-monitor.service`.
Needs only `python3` (3.8+). No pip packages.

## 1. Prepare the devices

- **HomeWizard P1**: in the HomeWizard Energy app → Settings → Meters → your P1 → enable **Local API**. Check: `curl http://<ip>/api/v1/data` returns JSON containing `active_power_w`.
- **Water** (optional): read from the water meter connected to the P1 (set `"water": true`, requires the `homewizard` section). The P1 only reports the cumulative reading (m3) each time the meter reports; yours reports every 5 minutes, which is the default assumption (`"water": {"interval": 300}` if it differs). The litres of a report are spread evenly over the interval before it and drawn as a smooth glowing wave (1 min / 2 min / 10 min / 30 min points for 15m / 1u / 6u / 24u). After the last report the usage is still unknown: that part is shaded ("wacht op meting"), and the header total counts up to the last report ("gemeld t/m"). Totals are exact; the meter counts in 1-litre steps. Omit `water` and the graph is hidden.
- **Alfen charger**: no Modbus needed. The charger's local web API on port 443 is used (self-signed certificate, not verified). Put the charger's web login in `config.json` (`username`, default `admin`, and `password`). If your charger allows reading without login, omit `password`.
  The charger is polled every `interval` seconds (default 10, not 1) and the monitor logs out after every reading, so the Alfen app can still log in. Between polls the last value is held. Set `"interval": 1` for 1-second polling or `"logout": false` to keep a session open (both make the charger busier).
- **SMA inverter**: in the inverter's web interface (Device parameters → External communication → Modbus) enable the **Modbus TCP server**. Default unit id is 3, port 502.

Give all three a fixed IP (DHCP reservation in your router).

## 2. Configure

```
cp config.example.json config.json
```

Edit the IPs. Leave out a section to disable that source (a missing source counts as 0 in the "Home" calculation). Optional keys: `port`, `bind`, `data_dir`; Alfen `username`, `port`, `property`; SMA `port`, `unit`, `register`, `function` (3 or 4).

## 3. Run on a laptop (try it out)

```
python3 energy.py
```

Open `http://localhost:<port>`, or from your phone `http://<laptop-ip>:<port>` (the example config uses port 80; set `"port": 8080` when trying it on a laptop, because ports below 1024 need root there). The console prints `grid: ok` / `grid: ERROR …` whenever a source changes state, which is the quickest way to debug a device.

## 4. Install on a Raspberry Pi (or any systemd Linux)

Raspberry Pi OS Lite (or Debian netinst) with SSH is enough; nothing else to install.

```
sudo mkdir -p /opt/energy-monitor
sudo cp -r energy.py bmw.py index.html static config.json /opt/energy-monitor/
sudo cp energy-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now energy-monitor
journalctl -u energy-monitor -f        # watch it start
```

Then browse to `http://<pi-ip>` (port 80, so no `:8080` to type). The service unit lets its throwaway user listen on port 80 (`AmbientCapabilities=CAP_NET_BIND_SERVICE`).

### A short name instead of an IP address

```
sudo hostnamectl set-hostname energie
sudo apt install avahi-daemon     # only if it isn't installed yet (Raspberry Pi OS has it)
sudo reboot
```

Now `http://energie.local` works on iPhone and on most recent Android versions. Not every Android browser resolves `.local` names: if it doesn't on your phone, keep using the IP address (give the Pi a fixed IP in your router, a DHCP reservation) and use the home-screen icon below, so you never type it anyway.

### As an app on your Android phone

Open the page in Chrome, menu (⋮) → **Add to Home screen**. If Chrome offers **Install app**, choose that: you get a real app with its own window. The page ships a name ("Energie"), an icon and a full-screen manifest for this. Over plain `http` Chrome may only add a shortcut that still opens inside Chrome with the address bar; that is a Chrome limitation for non-`https` sites. The shortcut still gives you the icon and one tap to open.
Update later by copying the files again and `sudo systemctl restart energy-monitor`.
On a system without systemd just run `python3 energy.py config.json` from your init of choice.

## Monthly peak (capacity tariff)

Shown at the bottom when your P1 reports it (Belgian smart meters do): the highest quarter-hour average of this month, when it happened, and a bar for the current quarter's average compared to that peak (orange from 90 %, red when you are setting a new peak). The values come from the same answer the P1 already gives for the grid power, so there is no extra request; they are refreshed once a minute. Nothing to configure; the card stays hidden if the meter doesn't report a peak.

## Car battery with BMW CarData (optional)

Adds a graph of the battery charge in %. It uses BMW's own CarData stream, so no password is stored: you approve the monitor once in your BMW account.

1. In your BMW account's **CarData** page create a client ("Create CarData Client") with both the **CarData API** and **CarData Streaming** subscriptions ticked, and copy the **client ID**.
2. On your car's CarData page open **Data selection** and tick the battery / charging data points (for example the "Electric vehicle" group), then save. BMW only streams what is ticked.
3. Add to `config.json` (the VIN is the 17-character chassis number):
   ```
   "bmw": { "client_id": "your-client-id", "vin": "WBA..." }
   ```
4. Authorise once. Run this while the service is running (it writes the tokens into the service's data folder):
   ```
   sudo STATE_DIRECTORY=/var/lib/energy-monitor python3 /opt/energy-monitor/energy.py /opt/energy-monitor/config.json bmw-login
   ```
   It prints a link and a code: open the link on your phone or laptop and sign in with your BMW account. The service picks the tokens up by itself within about 20 seconds.
5. BMW only sends data when the car reports. Locking or unlocking the car in the MyBMW app triggers a report. Until then the graph says "wacht op melding van de auto".

Good to know:
- The stream carries many data points. The log lists the battery related ones it sees (`bmw: N new data point(s) in the stream: …`). The monitor uses the first of `vehicle.drivetrain.batteryManagement.header`, `vehicle.powertrain.electric.battery.stateOfCharge.displayed`, `vehicle.trip.segment.end.drivetrain.batteryManagement.hvSoc` that your car sends. Which one carries the percentage differs per model: if the graph stays empty or looks wrong, put the right key in `"soc_keys": ["…"]` in the `bmw` section.
- BMW allows **one** stream per account. Don't run this next to another CarData integration (Home Assistant, evcc) on the same client.
- Tokens are renewed automatically and stored in `bmw-tokens.json` (readable by the service only). If the log says `token refresh failed`, run the login command again.
- Because the car only reports now and then, the line is a step graph that holds the last value; it turns dashed when the last report is more than 20 minutes old.

## How it works / good to know

- Each device is polled in its own thread every second; one sample per wall-clock second is stored (watts, integers). A device that is unreachable for more than 5 s shows as a gap. If only the charger is offline, the total consumption (grid + solar) stays visible and is drawn in grey as "huis + laadpaal, verdeling onbekend"; if the grid or solar source is offline, the consumption is a gap. The page lists the offline sources at the top.
- The last 24 h live in memory (~86 400 samples, a few MB). They are appended to `samples-v3.bin` once a minute and trimmed to 24 h hourly, so SD-card wear is negligible and a restart loses at most a minute. Location: `data_dir`, else `$STATE_DIRECTORY` (systemd: `/var/lib/energy-monitor`), else `./data`.
- The page loads the history once (10 s buckets) and then fetches only the new samples every second. Tap/hover a graph to read values at that moment. The buttons 15m (default) / 1u / 6u / 24u switch the time range, because a one-second change is less than a pixel on a 24 h axis.
- "Home" is clamped at 0 because the devices are not sampled at exactly the same instant. Solar and charger are clamped at ≥ 0 (standby noise).
- There is no authentication: keep it on your home network, don't port-forward it.

## If a value looks wrong

- **Alfen**: the power value is API property `2221_16` (socket 1 total active power, W). If the charger has a second socket or a different model, set `"property"` in the `alfen` section. `login failed` means a wrong username/password; the charger only allows a few simultaneous sessions, so avoid logging in elsewhere with the same account while testing.
- **SMA**: the Modbus register numbers (total AC power 30775, S32, unit 3) are the documented defaults but can differ per model/firmware; override `register`, `unit` and `function` and compare with your inverter's Modbus documentation. Most firmware answers function 3; try `"function": 4` on errors.
