# Energy Monitor

A lightweight dashboard for your smart meter, solar inverter, EV charger, and optional BMW. It runs on a Raspberry Pi or any Linux system with Python.

![Energy dashboard](static/dashboard-demo.png)

*The dashboard example above uses fictional demo data.*

## Features

- Displays grid import, grid export, solar production, and EV charging power.
- Calculates home consumption and total energy use for the selected period.
- Stores the latest 24 hours for 15-minute, 1-hour, 6-hour, 12-hour, and 24-hour graphs.
- Supports water consumption, three-phase data, and Belgian monthly peak readings.
- Shows an optional BMW battery level and distance from a configured reference point.
- Can control an Alfen EV charger using solar surplus and grid limits.
- Requires no database or third-party Python packages.

## Supported devices

| Device | Use | Requirement |
|---|---|---|
| HomeWizard P1 | Grid power, phases, water, and monthly peak | Local API enabled |
| SMA inverter | Solar production | Modbus TCP enabled |
| Alfen EV charger | Charging power and optional automatic control | Local web API |
| BMW | Battery level and distance | BMW CarData client |

![System architecture](static/architecture.svg)

## Requirements

- Linux and Python 3.10 or newer.
- Python standard library only.
- Network access to the configured local devices.
- Internet access only for the optional BMW integration.
- A modern browser on a phone, tablet, or computer.

A Raspberry Pi Zero is sufficient for basic use. Allow about 100 MB of available memory. Typical usage is lower.

# Quick start

## 1. Prepare the devices

### HomeWizard P1

1. Open the HomeWizard Energy app.
2. Open **Settings → Meters → P1**.
3. Enable the **Local API**.
4. Test the API:

   ```sh
   curl http://<p1-ip>/api/v1/data
   ```

The response must contain `active_power_w`.

### SMA inverter

In the inverter web interface, enable **Modbus TCP server** under **Device parameters → External communication → Modbus**.

- Port: `502`
- Unit ID: `3`
- Default register: `30775`

### Alfen EV charger

1. Make the charger accessible on port `443`.
2. Record the username and password of the local web interface.
3. Omit `password` if the charger can be read without a login.

Polling logs in for each reading and logs out afterward. Leave `logout` enabled unless you know that keeping a session open is safe for your charger model.

### Network addresses

Give every device a fixed IP address through a DHCP reservation in your router.

## 2. Create the configuration

```sh
cp config.example.json config.json
```

Change the IP addresses, ports, and passwords. Omit a device section to disable that source.

Example with a P1 meter and SMA inverter:

```json
{
  "port": 8080,
  "language": "en",
  "homewizard": {
    "host": "192.168.0.241",
    "interval": 1
  },
  "sma": {
    "host": "192.168.0.131",
    "unit": 3,
    "register": 30775,
    "interval": 1
  }
}
```

Start locally:

```sh
python3 energy.py config.json
```

Open `http://localhost:8080`. From another device, open `http://<laptop-ip>:8080`.

# Configuration

## General options

| Option | Default | Description |
|---|---:|---|
| `language` | `nl` | Dashboard language: `nl` or `en` |
| `port` | `8080` | Web server port |
| `bind` | `0.0.0.0` | Web server bind address |
| `data_dir` | data directory | Directory for history and settings |
| `appliances` | not set | Names for phases and the three-phase graph |

Example appliance names:

```json
"appliances": {
  "3phase": "heat pump, induction hob",
  "l1": "EV charger, dryer",
  "l2": "washing machine",
  "l3": "oven"
}
```

## HomeWizard P1

```json
"homewizard": {
  "host": "192.168.0.241",
  "port": 80,
  "interval": 1,
  "water": {
    "report_interval": 300
  }
}
```

- `interval`: P1 polling interval in seconds.
- `water.report_interval`: expected interval between water-meter reports, not the P1 polling interval.

Add `water` to enable the water graph. Omit it to hide the graph.

## SMA inverter

```json
"sma": {
  "host": "192.168.0.131",
  "port": 502,
  "unit": 3,
  "register": 30775,
  "function": 3,
  "interval": 1
}
```

Use `function: 4` if the inverter does not respond to Modbus function 3. Register values and firmware support differ by model.

## Alfen EV charger

```json
"alfen": {
  "host": "192.168.0.204",
  "port": 443,
  "username": "admin",
  "password": "CHANGE-ME",
  "property": "2221_16",
  "phases": 1,
  "max_current_a": 26,
  "voltage": 230,
  "interval": 10,
  "logout": true
}
```

Defaults:

- `username`: `admin`
- `interval`: `10`
- `property`: `2221_16`
- `phases`: `1`
- `max_current_a`: `16`
- `voltage`: `230`
- `logout`: `true`

Set `phases`, `max_current_a`, and `voltage` to match the charger and electrical installation. Set `property` to the API property for total active charging power if the default differs for your model.

# Dashboard

The dashboard provides 15-minute, 1-hour, 6-hour, 12-hour, and 24-hour ranges. Hover over or tap a graph to inspect a sample. The energy balance also totals energy use and generation for the selected period.

An offline or stale source is marked in the dashboard. A `≈` beside a total indicates incomplete measurement coverage. The home-consumption calculation uses available sources; an unconfigured source is treated as absent, while a configured source that becomes stale or unavailable is reported as offline.

# Automatic EV charging

The EV controller uses P1 grid power, the P1 quarter-hour average, and the monthly peak to calculate a charging target every 10 seconds. The SMA inverter is not required.

## Settings

- **Start at grid export**: grid export required to start a stopped vehicle.
- **Minimum charging rate**: lowest charging rate in 1 A steps.
- **Quarter-hour limit**: calculated automatically from the P1 monthly peak, with a usable 3 kW floor before a peak has been recorded.

With `0 W` export, charging can start without waiting for solar surplus. With a higher threshold, charging starts or resumes only when grid export reaches the configured threshold.

While charging, the controller raises or lowers the current using available solar surplus and the quarter-hour limit. An increase is blocked when P1 or charger readings are missing or stale.

## Safety and operating behavior

Active control requires an Alfen password and a configured HomeWizard P1 source. It is off by default. Its on/off state is saved, so if it was active before a service restart, control resumes after restart once valid meter readings are available.

Use **dry run** to check the calculated target before enabling control. Dry run sends no charger commands.

When control is enabled:

- Alfen Solar Charging is disabled to prevent conflicting control.
- The charger current limit is changed in 1 A steps.
- If readings become invalid, the export threshold is no longer met, or the quarter-hour limit cannot accommodate the minimum rate, the service sets a 0 A limit, installs Alfen's persistent 0 A pause profile, and switches the charger to Green mode. On resume, it disables Green mode, sets the calculated current limit, and then clears its pause profile.
- If the charger reports power while the controller's target is 0 kW, the service reapplies both the 0 A current limit and pause profile (at most once every 30 seconds).
- When control is switched off, the service restores the minimum configured current, sets Alfen Solar Charging to **Green**, and then clears its own pause so the charger waits for solar power.
- After a service restart, active control remains enabled; it does not hand the charger back to Alfen's autonomous mode.

If the Raspberry Pi loses power unexpectedly, it cannot send a recovery command. The charger retains its last state and limit until the service is available again. Configure an independent charger-side or hardware limit if charging must be reliably stopped or constrained during a Pi failure.

Settings are saved in `ev-control.json` in the data directory. Follow control and polling changes with:

```sh
journalctl -u energy-monitor -f
```

The dashboard has no authentication. Use EV control only on a trusted home network.

# BMW battery

## 1. Create a BMW CarData client

In the BMW CarData portal:

1. Create a CarData client.
2. Enable both **CarData API** and **CarData Streaming**.
3. Open **Data selection** and enable the battery/charging data points.

To calculate distance from home, also select:

- `vehicle.cabin.infotainment.navigation.currentLocation.latitude`
- `vehicle.cabin.infotainment.navigation.currentLocation.longitude`

Not every vehicle provides location data.

## 2. Configure the vehicle

```json
"bmw": {
  "client_id": "your-client-id",
  "vin": "YOUR-17-CHARACTER-VIN",
  "home": "51.05,3.72",
  "soc_keys": [
    "vehicle.drivetrain.batteryManagement.header",
    "vehicle.powertrain.electric.battery.stateOfCharge.displayed",
    "vehicle.trip.segment.end.drivetrain.batteryManagement.hvSoc"
  ]
}
```

`soc_keys` is optional. The monitor tries the default keys in the order above.

## 3. Authorize once

Start authorization while the service is running:

```sh
sudo STATE_DIRECTORY=/var/lib/energy-monitor \
  python3 /opt/energy-monitor/energy.py \
  /opt/energy-monitor/config.json bmw-login
```

Open the displayed BMW link and approve the login. Tokens are stored in `bmw-tokens.json` and refreshed automatically.

BMW only sends data when the vehicle reports. Locking or unlocking the car in MyBMW can trigger a report. The graph remains waiting until a new reading arrives.

BMW allows one stream per account. Do not run another CarData integration, such as Home Assistant or evcc, with the same client.

# Install on Raspberry Pi

Raspberry Pi OS Lite or Debian is sufficient. SSH is optional.

## Start the systemd service

```sh
sudo mkdir -p /opt/energy-monitor
sudo cp -r energy.py bmw.py index.html static config.json /opt/energy-monitor/
sudo cp energy-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now energy-monitor
journalctl -u energy-monitor -f
```

Open `http://<pi-ip>`. With port `80`, no port number is needed.

The systemd service:

- runs under a dynamic temporary user;
- stores data in `/var/lib/energy-monitor`;
- receives temporary permission to bind to port 80;
- restarts automatically after failure.

## Use a hostname

```sh
sudo hostnamectl set-hostname energie
sudo apt install avahi-daemon
sudo reboot
```

Open `http://energie.local`. If `.local` does not resolve on your phone, use the Pi's fixed IP address.

## Install as an app

Open the dashboard in Chrome, open the browser menu, and choose **Add to Home screen**. If **Install app** is offered, choose that option.

Over plain HTTP, Chrome may add only a shortcut that opens in the browser with the address bar visible.

## Update the installation

```sh
sudo cp -r energy.py bmw.py index.html static config.json /opt/energy-monitor/
sudo systemctl restart energy-monitor
journalctl -u energy-monitor -f
```

Without systemd:

```sh
python3 energy.py config.json
```

# Data storage

- The latest 24 hours are kept in memory.
- Samples are aggregated into 10-second buckets for stored history.
- History is appended to `samples-v4.bin` once per minute.
- A restart loses at most about one minute of history.
- Existing `samples-v3.bin` history is converted automatically.
- The latest BMW location is stored in `car-position.json`.
- BMW tokens are stored in `bmw-tokens.json`.
- EV settings are stored in `ev-control.json`.

The data directory is selected in this order:

1. `data_dir` in `config.json`
2. `$STATE_DIRECTORY`
3. `./data`

BMW location history is not stored. Coordinates are not sent to the browser; only calculated distance is displayed.

# Troubleshooting

## A source has no data

Follow the service log:

```sh
journalctl -u energy-monitor -f
```

Check:

- whether the fixed IP address is reachable;
- whether the P1 Local API is enabled;
- whether Modbus TCP is enabled on the SMA inverter;
- whether ports `80`, `502`, or `443` are blocked;
- whether the Alfen username and password are correct.

## Alfen reports `login failed`

Check the username and password. A charger has only a limited number of concurrent sessions. Close MyEve or other active sessions before testing.

Alfen polling can be paused from the dashboard. This pauses monitoring only; it does not stop charging.

## SMA reports no values

Check:

- Modbus TCP, unit ID, and port;
- register `30775` and function `3`;
- whether the inverter model uses another register;
- whether a firmware update changed the register layout.

## BMW remains waiting

BMW does not send regular measurements. Lock or unlock the car in MyBMW to trigger a report.

If the graph stays empty:

- check `soc_keys`;
- verify that the required data points are selected;
- ensure the same BMW client is not used by another integration;
- repeat `bmw-login` if token authorization failed.

## Values look incorrect

- P1: positive values mean grid import; negative values mean grid export.
- SMA: negative or standby readings are displayed as 0 W.
- Home: usage is clamped at zero because sources are not sampled at exactly the same instant.
- Water: usage is reported only after a new water-meter reading arrives.

# Security

The dashboard has no user authentication. Keep the monitor on a trusted home network and do not forward its port through the internet-facing router.
