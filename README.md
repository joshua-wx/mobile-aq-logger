# AQ Rapid Sampler

A portable air-quality logger built on an ESP32-S3 running MicroPython. A
Sensirion SEN65 samples particulate matter and gas indices once a second; a
battery-backed PCF8523 real-time clock timestamps every row; readings go to a
CSV file on the board's flash.

Two dashboards read the device, and they serve different situations:

| | Where | Purpose |
|---|---|---|
| **WiFi dashboard** | `http://192.168.4.1` over the device's own access point | Live readings, **and the only place anything can be changed** — start/stop logging, set location, set the clock, download and delete files |
| **USB dashboard** | `http://127.0.0.1:9876` on a host computer | Display only. Live readings, the last 200 samples, and 30-minute plots. Useful on a bench with the board plugged in |

---

## Hardware

ESP32-S3 with two devices on I2C bus 0 at 100 kHz:

```
ESP32-S3            SEN65        PCF8523
--------            -----        -------
GPIO 5  ---- SDA ---- SDA -------- SDA
GPIO 6  ---- SCL ---- SCL -------- SCL
3V3     ---- VCC ---- VCC -------- VCC
GND     ---- GND ---- GND -------- GND
                                   + CR1220 backup cell
```

| Device | Address |
|---|---|
| SEN65 | `0x6B` |
| PCF8523 | `0x68` |

**The PCF8523 and a DS3231 cannot share the bus** — both live at `0x68`. This
project used a DS3231 previously; that support has been removed.

### Two PCF8523 defaults that will bite you

The chip powers up with settings that are wrong for this application, so
[`pcf8523.py`](pcf8523.py) fixes both when you construct the driver:

- **Battery switch-over is disabled** out of reset (`PM = 111`). Until it is
  configured the coin cell does nothing and the clock resets on every power
  cycle. The driver enables standard-mode switch-over with battery-low
  detection, which is correct for a 3.3 V rail and a 3 V cell.
- **CLKOUT runs at 32.768 kHz** out of reset, draining the backup cell for no
  benefit here. The driver turns it off.

⚠️ **Check the crystal on your breakout** and pass `cap_12p5pf=True` (12.5 pF,
e.g. Adafruit's board) or `False` (7 pF). Getting this wrong costs tens of ppm
of timekeeping accuracy.

---

## Files

| File | Role |
|---|---|
| [`main.py`](main.py) | The application. WiFi AP, dashboard, logging, clock, USB serial stream. Runs on boot |
| [`sen65.py`](sen65.py) | SEN65 driver, ported from Sensirion's Arduino library |
| [`pcf8523.py`](pcf8523.py) | PCF8523 RTC driver |
| [`aq_bridge.py`](aq_bridge.py) | **Host-side.** Reads USB serial, serves the display-only dashboard |
| [`log_aq.py`](log_aq.py) | Headless alternative to `main.py` — logs to CSV with no web server |
| [`test_sen65.py`](test_sen65.py) | Bench test: print sensor readings once a second |
| [`test_pcf8523.py`](test_pcf8523.py) | Bench test: print the time; set it with `SET_TIME = True` |
| [`boot.py`](boot.py) | MicroPython boot hook, currently all commented out |

---

## Getting started

### 1. Flash the device

Copy to the board's filesystem with `mpremote`, Thonny, or `ampy`:

```
mpremote cp main.py sen65.py pcf8523.py boot.py :
```

Reset the board. It prints its startup banner, brings up the access point, and
begins sampling.

### 2. Set the clock

The RTC ships unset, so the first thing to do is give it the time. Join the
open WiFi network **`AQ-logger`** and open `http://192.168.4.1`.

The Clock card shows the device time and its drift against your browser. With
**auto** ticked (the default) the page syncs the RTC by itself whenever the
clock is unset or more than 10 s out — so simply connecting a phone fixes it,
to about a second. The button does the same on demand.

If the card says **"clock not set"** and stays that way after a sync, or reports
**"backup battery low"**, fit a fresh CR1220. Without a working cell the
oscillator-stop flag sets on every power-up and the time never survives a
reboot.

Other ways to set it, if browser accuracy is not enough:

- **NTP over station mode.** The ESP32-S3 can run AP and station modes at once,
  so it could join a known network and call `ntptime.settime()`. Not
  implemented — it needs stored credentials and a UTC/local decision.
- **A GPS module.** Gives sub-second time *and* the latitude/longitude that is
  typed in by hand today. Most hardware, biggest payoff.
- **`test_pcf8523.py`** with `SET_TIME = True`, flashed once, for a bench set.

### 3. Log

Set the location on the dashboard if you want it recorded, then press **Start
Logging**. Files appear in the Log Files card for download or deletion.

---

## Data format

One file per logging run, named for its start time: `YYYYMMDD_HHMMSS.csv`.

```
# Location: lat=-37.8136, lon=144.9631
# Start: 2026-09-02 13:26:46
# Clock: PCF8523 RTC, local time
timestamp,PM1.0_ug_m3,PM2.5_ug_m3,PM4.0_ug_m3,PM10_ug_m3,VOC_index,NOx_index
2026-09-02 13:26:46,7.0,8.0,9.0,10.0,106.0,
2026-09-02 13:26:47,8.0,9.0,10.0,11.0,107.0,
```

- Timestamps are **local wall-clock with no timezone**, one row per second.
- Empty fields are readings the sensor did not supply. NOx in particular stays
  blank for the first minutes while its index warms up.
- The `# Clock:` line records whether the times can be trusted. If the RTC is
  unset or absent it says so explicitly rather than letting you discover it
  later.
- Roughly **100 kB per 35 minutes**, so about 30 hours in 8 MB.

### Reading the gas indices

VOC and NOx are Sensirion index values, not concentrations, and **they do not
share a baseline**:

| | Range | Resting value | Meaning |
|---|---|---|---|
| VOC index | 1–500 | **100** | Moves either side of 100 as VOCs rise and fall |
| NOx index | 1–500 | **1** | Sits at 1 and only climbs when NOx is present |

So a NOx column that reads a constant `1.0` is the sensor saying "no NOx above
baseline" — it is not a fault. In a 35-minute indoor test log, NOx was blank
for 9 s, then 0 for 46 s, then 1.0 for the remaining 2076 samples without once
moving, while VOC climbed from 0 and settled at 100.

Both indices read 0 for about the first minute while the algorithm establishes
itself, and are blank for the first few seconds before that.

### Where time comes from

The PCF8523 seeds the ESP32's internal clock at boot; timestamps are then taken
from the internal clock so the shared I2C bus is not hit once per sample; and
the internal clock is re-synced from the RTC every 10 minutes, because the
ESP32 runs its clock off an RC oscillator that drifts by seconds per hour.

A missing or faulty RTC does not stop the logger. Sampling continues, the
dashboards report the problem, and the CSV header flags the timestamps as
unreliable.

---

## USB dashboard

On the host computer:

```
pip install pyserial
python3 aq_bridge.py                 # auto-detects the board
```

Then open `http://127.0.0.1:9876`. Configuration sits at the top (read-only);
live readings, recent samples and plots are tabs below it.

```
--port /dev/ttyACM0    serial device (default: auto-detect)
--http-port 9876       steps to the next free port if busy
--host 0.0.0.0         let other machines on your network view it
--list                 show candidate serial ports and exit
--no-dtr               see the warning below
```

Plot x-axes autoscale to the span of buffered data, clamped between 5 and 30
minutes, with a tick every minute.

The dashboard cannot change anything on the device — that is deliberate, and
the WiFi dashboard remains the only control surface.

### Serial protocol

`main.py` streams tagged JSON lines. Ordinary `print()` diagnostics share the
port and are ignored by the reader because they lack the tag.

```
AQ1 {"t":"data","ts":"2026-09-02 13:26:45","v":{…},"mx":{…},"mn":{…}}   ~1 Hz
AQ1 {"t":"status","lat":-37.8,"logging":true,…}      every 5 s + on change
```

`data` carries the same latest / max / 1-minute-mean triple the WiFi dashboard
shows, so the two never disagree. `status` carries configuration, which changes
rarely. Set `_SERIAL_STREAM = False` in `main.py` to turn the stream off.

---

## Troubleshooting

**Config section fills but the tables and plots stay empty, or data appears
only every few tens of seconds.**
DTR is not asserted. On the ESP32-S3's native USB CDC, DTR is how the host says
"I am listening"; with it low MicroPython treats stdout as disconnected and
truncates it. The signature is lines like `AQ12.2, "pm4p0": 2.8,` — no space
after the tag, starting mid-record. The bridge asserts DTR by default, so this
only happens if you passed `--no-dtr`. Measured on the bench: DTR low gave 0
parseable records in 12 s, DTR high gave 14 with zero errors.

**Garbled lines, or "device reports readiness to read but returned no data".**
Two programs are reading the port. Linux lets several processes open the same
tty and then splits the bytes between them, corrupting every line for all of
them — and an `open()` test cannot detect this, because it succeeds anyway.
Close Thonny, `mpremote`, and any second bridge. The bridge now opens the port
exclusively, so a second copy fails cleanly instead of stealing bytes.

**`OSError: [Errno 98] Address already in use`.**
Something else on the machine holds the HTTP port. Find it with
`ss -ltnp | grep :9876`, or just pass `--http-port`.

**Timestamps read `2000-01-01`.**
The RTC has never been set, or its backup cell is flat, so the ESP32 is running
from its power-on epoch. See *Set the clock* above.

**`PCF8523 not responding at 0x68`.**
Check wiring. Remember a DS3231 on the bus would answer at the same address.

---

## Known limitations

- Times are local wall-clock with **no timezone recorded**. A log moved between
  machines carries no indication of which offset it was taken in.
- The access point is **open** — no password. To add WPA2, change `authmode=0`
  to `authmode=3` and add `password='…'` in `_start_ap()` in `main.py`.
- Location is entered by hand, not sensed.
- The SEN65 also reports **temperature and relative humidity**, which the driver
  exposes but nothing currently logs or displays.
- Browser sync sets the clock to about a second; there is no round-trip latency
  compensation.

---

## Licence

[MIT](LICENSE).

[`sen65.py`](sen65.py) is a port of Sensirion's Arduino I2C library for the
SEN6x family; command IDs, execution delays and scaling factors come from their
published driver. Sensirion's originals are BSD-3-Clause.
