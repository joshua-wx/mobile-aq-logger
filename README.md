# AQ Rapid Sampler

A portable air-quality logger built on an ESP32-S3 running MicroPython. A
Sensirion SEN65 samples particulate matter and gas indices once a second; a
battery-backed PCF8523 real-time clock timestamps every row; readings go to a
CSV file on the board's flash.

The device has no WiFi and no web server. Its whole interface is a
line-oriented JSON protocol on the USB serial port, and one dashboard —
[`aq_bridge.py`](aq_bridge.py) on a host computer at `http://127.0.0.1:9876` —
both displays it and drives it:

| | |
|---|---|
| **Display** | Live readings, the last 200 samples, 30-minute plots of every variable |
| **Control** | Start/stop logging, set location, set the clock, download and delete log files |

Logging does not need the host. Location and the autostart flag are persisted
on the device, so a board configured at the bench can be unplugged and run on
battery; a run already in progress continues when the USB cable comes out.

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
| [`main.py`](main.py) | The application. Sampling, logging, clock, and the USB serial protocol. Runs on boot |
| [`sen65.py`](sen65.py) | SEN65 driver, ported from Sensirion's Arduino library |
| [`pcf8523.py`](pcf8523.py) | PCF8523 RTC driver |
| [`aq_bridge.py`](aq_bridge.py) | **Host-side.** Speaks the serial protocol and serves the dashboard |
| [`log_aq.py`](log_aq.py) | Headless alternative to `main.py` — logs to CSV with no host interface at all |
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

Reset the board. It prints its startup banner and begins sampling.

### 2. Start the dashboard

With the board plugged into the host over USB:

```
pip install pyserial
python3 aq_bridge.py                 # auto-detects the board
```

Open `http://127.0.0.1:9876`. Everything below happens there.

### 3. Set the clock

The RTC ships unset, so the first thing to do is give it the time. The Clock
card shows the device time and its drift against the computer you are sitting
at. With **auto** ticked (the default) the page syncs the RTC by itself
whenever the clock is unset or more than 10 s out; the button does the same on
demand.

If the card says **"clock not set"** and stays that way after a sync, or reports
**"backup battery low"**, fit a fresh CR1220. Without a working cell the
oscillator-stop flag sets on every power-up and the time never survives a
reboot.

Other ways to set it, if browser accuracy is not enough:

- **A GPS module.** Gives sub-second time *and* the latitude/longitude that is
  typed in by hand today. Most hardware, biggest payoff.
- **`test_pcf8523.py`** with `SET_TIME = True`, flashed once, for a bench set.

### 4. Log

Type the location into the Logging card and press **Save** — it is written to
`config.json` on the device and survives a power cycle, so it only has to be
done when the site changes. Then press **Start logging**. Files appear in the
Log files card for download or deletion.

To deploy the board away from a computer, tick **start logging automatically
at power-up** before unplugging it. The device then opens a new log on every
boot with no host attached. Leave it unticked and logging is started by hand
from the dashboard — a run that is already going keeps going when the USB
cable comes out.

A run rolls over to a new file every hour, so a long deployment arrives as a
row of hourly files rather than one that is slow to transfer.

---

## Data format

A new file every hour, and one whenever logging starts, named for its own
start time: `YYYYMMDD_HHMMSS.csv`. Rotation keeps an unattended run from
producing one enormous file, and keeps each file individually downloadable
over the serial link. Change `_ROTATE_S` in [`main.py`](main.py) to use a
different interval, or set it to `0` to write one file per run as before.

```
# Location: lat=-37.8136, lon=144.9631
# Start: 2026-09-02 13:26:46  (elapsed_s = 0)
# Clock: PCF8523 RTC, local time
elapsed_s,PM1.0_ug_m3,PM2.5_ug_m3,PM4.0_ug_m3,PM10_ug_m3,VOC_index,NOx_index
0,7.0,8.0,9.0,10.0,106.0,
1,8.0,9.0,10.0,11.0,107.0,
```

- `elapsed_s` is **whole seconds since the `# Start:` time**, one row per
  second. Absolute time is `Start + elapsed_s`; in pandas, for instance:

  ```python
  df = pd.read_csv(path, comment="#")
  t0 = pd.Timestamp("2026-09-02 13:26:46")          # from the header
  df["timestamp"] = t0 + pd.to_timedelta(df.elapsed_s, unit="s")
  ```

  `start + elapsed_s` is exactly the row's wall-clock second: the header time,
  the filename and the offset origin all come from one reading of the clock,
  so hourly files stitch back together without a seam.

- The start time is **local wall-clock with no timezone**.
- Empty fields are readings the sensor did not supply. NOx in particular stays
  blank for the first minutes while its index warms up.
- The `# Clock:` line records whether the start time can be trusted. If the RTC
  is unset or absent it says so explicitly rather than letting you discover it
  later.
- Roughly **64 kB per 35 minutes**, so about 45 hours in 8 MB, split across
  one file per hour of about 110 kB each.
- Every file stands alone: its own location, start time, clock provenance and
  `elapsed_s` origin. Rotation happens *before* the sample that triggers it is
  written, so that sample opens the new file rather than being lost or
  recorded twice — no row falls in a crack between two files.
- A file is never overwritten. If the clock is stopped and hands out a name
  that already exists — the same second on every boot, with a flat RTC — the
  new log becomes `20260902_211719-1.csv`, `-2.csv` and so on rather than
  clobbering what is there.

The column was a full `YYYY-MM-DD HH:MM:SS` timestamp until it became clear it
was ~40% of every row and entirely redundant: at a fixed 1 Hz it is
reconstructible from the start time. That change alone took the same log from
96 kB to 64 kB.

Two clock effects are worth knowing about. The periodic RTC resync can step
the clock by a second or two, and `elapsed_s` follows it — that is the point,
since it is correcting the ESP32's RC drift, so the column tracks real elapsed
time rather than a drifting oscillator. Setting the clock from the dashboard
mid-run does **not** dislocate the column: the logging epoch moves with the
correction, so a run that starts before the clock is set stays continuous.

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
dashboard reports the problem, and the CSV header flags the timestamps as
unreliable.

---

## The dashboard

On the host computer:

```
pip install pyserial
python3 aq_bridge.py                 # auto-detects the board
```

Then open `http://127.0.0.1:9876`. Device status, logging, the clock and the
location are at the top; live readings, recent samples and plots are tabs
below them.

```
--port /dev/ttyACM0    serial device (default: auto-detect)
--http-port 9876       steps to the next free port if busy
--host 0.0.0.0         let other machines on your network view it
--list                 show candidate serial ports and exit
--no-dtr               see the warning below
--no-map               never fetch map tiles
--tile-cache DIR       where tiles are kept (default: ~/.cache/aq_bridge/tiles)
```

Plot x-axes autoscale to the span of buffered data, clamped between 5 and 30
minutes, with a tick every minute.

⚠️ The dashboard can start and stop logging and **delete files on the device**,
and there is no authentication. It binds to `127.0.0.1` for that reason; only
pass `--host 0.0.0.0` on a network you trust.

### The location map

The Location card shows the saved lat/lon on an OpenStreetMap background, so a
typo in a coordinate is visible rather than something you discover in the data
later. It redraws when the device reports a location — on connection, and
after **Save** — and not on the once-a-second poll.

Tiles are fetched **by the bridge, not by your browser**, and cached under
`~/.cache/aq_bridge/tiles`. That has three consequences worth knowing:

- Displaying a location sends those coordinates to `tile.openstreetmap.org`.
  Nine or so tiles are fetched per location or zoom change, never in bulk, with
  an identifying User-Agent as the [tile usage policy][tup] requires.
- Once an area is cached it keeps working with no connection, so viewing a site
  at the bench means it still draws in the field.
- `--no-map` stops all outbound requests. Cached tiles are still shown; where
  there are none the card falls back to a plain coordinate grid and says so.

The same fallback appears whenever tiles cannot be had — the map is never the
reason you cannot read a coordinate, which is also printed under the card.

[tup]: https://operations.osmfoundation.org/policies/tiles/

### Downloading log files

Log files live on the board's flash — that is what lets it run unattended —
so a download is a transfer over the serial link, not a static file the
browser fetches. Press **Download** and the bridge asks the device for the
file, reassembles the base64 chunks it sends back, checks them against the
byte count and checksum the device reports, and only then hands your browser
a `.csv`. A truncated or corrupted transfer fails with a message instead of
silently saving a short file.

It is not instant: expect a few seconds for a typical log, and longer for a
large one. Sampling and logging continue throughout — the device paces itself
between chunks so a download cannot starve the sensor.

### Serial protocol

`main.py` streams tagged JSON lines and accepts tagged JSON commands on the
same port. Ordinary `print()` diagnostics share it and are ignored by the
reader because they lack the tag.

```
out  AQ1 {"t":"data","ts":"2026-09-02 13:26:45","v":{…},"mx":{…},"mn":{…}}  ~1 Hz
out  AQ1 {"t":"status","lat":-37.8,"logging":true,…}      every 5 s + on change
out  AQ1 {"t":"ack","id":7,"ok":true,"msg":"started"}              per command
out  AQ1 {"t":"file","id":8,"seq":0,"d":"<base64>"}          during a download
in   AQC {"id":7,"cmd":"log_start"}
```

`data` carries the latest / max / 1-minute-mean triple the tables show.
`status` carries configuration, which changes rarely. Every command draws
exactly one `ack` echoing its `id`, so replies can be matched to requests
even though sample records are interleaved with them.

| Command | Fields | Does |
|---|---|---|
| `ping` | — | Liveness check |
| `status` | — | Ask for a `status` record now |
| `list` | — | Files and their sizes |
| `log_start` / `log_stop` | — | Begin or end a logging run |
| `set_loc` | `lat`, `lon` | Set the location and persist it |
| `set_clock` | `ts` (`YYYY-MM-DDTHH:MM:SS`) | Set the RTC and the ESP32 clock |
| `autostart` | `on` | Log automatically at power-up |
| `delete` | `name` | Remove a log file |
| `get` | `name`, `off` | Stream a log file back |

Set `_SERIAL_STREAM = False` in `main.py` to silence the outgoing stream.

**Never send `0x03` (Ctrl-C) on this port.** MicroPython treats it as an
interrupt and would stop the logger. For the same reason, do not leave a REPL
(Thonny, `mpremote`) attached while the bridge is running.

### Persisted configuration

Location and the autostart flag are kept in `config.json` on the device, and
`main.py` reloads them at boot. Deleting that file restores the defaults
compiled into `main.py`.

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

**The `# Start:` line reads `2000-01-01`.**
The RTC has never been set, or its backup cell is flat, so the ESP32 is running
from its power-on epoch. See *Set the clock* above. A board deployed with
autostart and a flat cell will still log, and `elapsed_s` is still correct —
only the origin is unknown, and the `# Clock:` line says so.

**Buttons do nothing, or say "no reply from the device in 5 s".**
The device is streaming but not reading commands. Check that nothing else is
holding the port (a REPL will swallow the command bytes), and that `main.py`
is actually running rather than the board sitting at the REPL prompt after a
Ctrl-C.

**A download fails with "transfer stalled" or "checksum mismatch".**
Bytes were lost on the way. Nothing was written to disk — press Download
again. Persistent failures usually mean a second reader on the port.

**`PCF8523 not responding at 0x68`.**
Check wiring. Remember a DS3231 on the bus would answer at the same address.

---

## Known limitations

- Times are local wall-clock with **no timezone recorded**. A log moved between
  machines carries no indication of which offset it was taken in.
- **The device cannot be configured in the field.** With the WiFi dashboard
  gone, setting the clock or the location needs a computer and a USB cable;
  a phone will not do. Configure at the bench, and rely on `config.json` and
  the autostart flag once deployed. Only start/stop is lost in the field, and
  autostart covers the common case.
- The bridge has **no authentication**, so it binds to localhost by default.
- The map is a fixed north-up view with zoom buttons; it cannot be panned, and
  clicking it does not set the location.
- Location is entered by hand, not sensed.
- The SEN65 also reports **temperature and relative humidity**, which the driver
  exposes but nothing currently logs or displays.
- Browser sync sets the clock to about a second; there is no round-trip latency
  compensation.
- Downloads transfer the whole file each time. `get` accepts an `off` offset so
  a resume is possible, but the dashboard does not use it.

---

## Licence

[MIT](LICENSE).

[`sen65.py`](sen65.py) is a port of Sensirion's Arduino I2C library for the
SEN6x family; command IDs, execution delays and scaling factors come from their
published driver. Sensirion's originals are BSD-3-Clause.
