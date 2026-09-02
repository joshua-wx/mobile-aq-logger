"""
AQ-logger — MicroPython air-quality logger for ESP32-S3.

Samples a Sensirion SEN65 once a second, timestamps every row from a
battery-backed PCF8523 RTC, and appends to a CSV file on the board's flash.
A new file is started every hour so an unattended run does not produce one
unbounded file; see _ROTATE_S.

There is no WiFi and no web server here. The device's entire interface is a
line-oriented protocol on the USB serial port: it streams tagged JSON out and
accepts tagged JSON commands in. aq_bridge.py on a host computer speaks that
protocol and serves the one dashboard, which both displays readings and
configures the device — location, clock, start/stop logging, and downloading
or deleting log files.

Logging does not need the host. Configuration is persisted to config.json, so
a device set up at the bench keeps its location and, if autostart is enabled,
begins logging by itself on power-up. Unplugging the host does not interrupt
a run in progress.

Wire format. One record per line, so a reader can use readline() and ignore
anything it does not recognise:

    out   AQ1 {"t":"data","ts":"...","v":{...},"mx":{...},"mn":{...}}  ~1 Hz
    out   AQ1 {"t":"status","lat":-37.8,"logging":true,...}       every 5 s
    out   AQ1 {"t":"ack","id":7,"ok":true,"msg":"started"}      per command
    out   AQ1 {"t":"file","id":8,"seq":0,"d":"<base64>"}     during download
    in    AQC {"id":7,"cmd":"log_start"}

Ordinary print() diagnostics share the port; they simply lack the tag.

Never send 0x03 (Ctrl-C) on this port: MicroPython treats it as an interrupt
and would stop the logger. Commands are plain ASCII JSON lines.

Wiring (I2C bus 0):
    SDA -> GPIO 5
    SCL -> GPIO 6
    SEN65 at 0x6B, PCF8523 at 0x68
"""

try:
    import asyncio
except ImportError:
    import uasyncio as asyncio   # MicroPython < 1.19

try:
    import json
except ImportError:
    import ujson as json

try:
    import select
except ImportError:
    import uselect as select

try:
    import binascii
except ImportError:
    import ubinascii as binascii

import os
import sys
import time
from machine import I2C, Pin, RTC

from pcf8523 import PCF8523
from sen65 import SEN65

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_KEYS       = ('pm1p0', 'pm2p5', 'pm4p0', 'pm10p0', 'voc', 'nox')
# Rows carry seconds elapsed since '# Start:' in the header rather than a
# full timestamp: at 1 Hz the absolute time was ~40% of every row and is
# entirely reconstructible from the start time plus the offset.
_CSV_HEADER = 'elapsed_s,PM1.0_ug_m3,PM2.5_ug_m3,PM4.0_ug_m3,PM10_ug_m3,VOC_index,NOx_index\n'

_ROTATE_S    = 3600     # start a new log file this often; 0 disables rotation
_RESYNC_S    = 600      # re-read the PCF8523 into the ESP32 clock this often
_DRIFT_WARN  = 2        # seconds of MCU-vs-RTC drift worth printing

_SERIAL_STREAM   = True   # emit tagged JSON lines on USB serial
_SERIAL_TAG      = 'AQ1'  # prefix on every line this device emits
_CMD_TAG         = 'AQC'  # prefix the host puts on every command
_SERIAL_STATUS_S = 5      # seconds between unsolicited 'status' records

_CONFIG_FILE = 'config.json'

# File download tuning. 512 raw bytes become a ~700-character line; the pause
# hands the scheduler back to the sensor task between chunks so a download
# cannot starve sampling, and keeps the USB CDC buffer from backing up into a
# blocking write.
_CHUNK        = 512
_CHUNK_PAUSE  = 4         # ms between chunks
_CMD_MAX_LINE = 512       # longest command line accepted, then resynchronise

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_g = {
    'latest':  {k: None for k in _KEYS},
    'max':     {k: None for k in _KEYS},
    'ring':    [],        # last <=60 readings for 1-min mean
    'logging': False,
    'logfile': None,
    'log_t0': None,       # time.time() at the start of the current run
    'ts':      '--',
    'lat':     -37.8136,
    'lon':     144.9631,
    'autostart': False,   # begin logging at boot without a host attached
    'boot_ms': 0,
    'rtc':     None,      # PCF8523 instance, or None if it did not answer
    'rtc_ok':  False,     # False while the RTC reports a stopped oscillator
    'batt_low': False,
    'sending': False,     # a file download is in flight
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(v):
    return '' if v is None else '{:.1f}'.format(v)

def _timestamp(t=None):
    """Local wall-clock time as 'YYYY-MM-DD HH:MM:SS'.

    Takes an optional epoch so a log's header, filename and elapsed_s origin
    can all be derived from one instant; reading the clock three times risks
    the second ticking between them, which would put every reconstructed row
    time out by one.
    """
    y, mo, d, h, mi, s = (time.localtime() if t is None else time.localtime(t))[:6]
    return '{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}'.format(y, mo, d, h, mi, s)

def _filename(t=None):
    y, mo, d, h, mi, s = (time.localtime() if t is None else time.localtime(t))[:6]
    return '{:04d}{:02d}{:02d}_{:02d}{:02d}{:02d}.csv'.format(y, mo, d, h, mi, s)

def _compute_mean(ring):
    if not ring:
        return {k: None for k in _KEYS}
    out = {}
    for k in _KEYS:
        vals = [r[k] for r in ring if r[k] is not None]
        out[k] = round(sum(vals) / len(vals), 1) if vals else None
    return out

def _round1(d):
    return {k: (round(v, 1) if v is not None else None) for k, v in d.items()}

def _list_files():
    """[[name, bytes], ...] for every CSV on flash, oldest name first."""
    out = []
    for f in sorted(os.listdir()):
        if f.endswith('.csv'):
            try:
                out.append([f, os.stat(f)[6]])
            except OSError:
                pass
    return out

def _free_bytes():
    try:
        st = os.statvfs('/')
        return st[0] * st[3]
    except (OSError, AttributeError):
        return None

# ---------------------------------------------------------------------------
# Persisted configuration
#
# With the WiFi dashboard gone the device cannot be reconfigured in the
# field, so what was typed in at the bench has to survive a power cycle.
# ---------------------------------------------------------------------------

def _load_config():
    try:
        with open(_CONFIG_FILE) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return
    for key in ('lat', 'lon'):
        v = cfg.get(key)
        if isinstance(v, (int, float)):
            _g[key] = float(v)
    _g['autostart'] = bool(cfg.get('autostart', False))

def _save_config():
    try:
        with open(_CONFIG_FILE, 'w') as fh:
            json.dump({'lat': _g['lat'], 'lon': _g['lon'],
                       'autostart': _g['autostart']}, fh)
        return True
    except OSError as e:
        print('Config save failed:', e)
        return False

# ---------------------------------------------------------------------------
# Serial output
# ---------------------------------------------------------------------------

def _emit(kind, obj):
    if not _SERIAL_STREAM:
        return
    obj['t'] = kind
    try:
        print(_SERIAL_TAG, json.dumps(obj))
    except Exception as e:      # a blocked port must never stop the logger
        print('Serial emit failed:', e)


def _emit_status():
    _emit('status', {
        'ts':          _timestamp(),
        'lat':         _g['lat'],
        'lon':         _g['lon'],
        'logging':     _g['logging'],
        'logfile':     _g['logfile'] or '',
        'autostart':   _g['autostart'],
        'rtc_present': _g['rtc'] is not None,
        'rtc_ok':      _g['rtc_ok'],
        'batt_low':    _g['batt_low'],
        'files':       _list_files(),
        'free':        _free_bytes(),
        'up':          time.ticks_diff(time.ticks_ms(), _g['boot_ms']) // 1000,
    })


def _ack(cid, ok, msg, data=None):
    rec = {'id': cid, 'ok': ok, 'msg': msg}
    if data is not None:
        rec['data'] = data
    _emit('ack', rec)


async def _status_task():
    while True:
        _emit_status()
        await asyncio.sleep(_SERIAL_STATUS_S)


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------

def _open_rtc(i2c):
    """Attach to the PCF8523 and seed the ESP32 clock from it.

    A missing or faulty RTC must not stop the logger: the sensor still runs
    and the dashboard reports the problem, timestamps just start from
    whatever the ESP32 booted with until the host sets the clock.
    """
    try:
        rtc = PCF8523(i2c)
    except OSError as e:
        print('WARNING: PCF8523 not responding at 0x68 ({}) - running without it.'.format(e))
        return None

    _g['rtc'] = rtc
    if not _sync_mcu_clock(rtc):
        print('WARNING: PCF8523 oscillator stopped - time is not trustworthy.')
        print('         Connect the host bridge and sync the clock.')
    if _g['batt_low']:
        print('WARNING: PCF8523 backup battery is low - replace the cell.')
    return rtc


def _sync_mcu_clock(rtc):
    """Copy RTC -> ESP32 internal clock. Returns True if the time is valid.

    The ESP32 runs its clock off an RC oscillator that drifts by seconds per
    hour, so this is called periodically, not just at boot.
    """
    try:
        _g['rtc_ok']   = not rtc.lost_power()
        _g['batt_low'] = rtc.battery_low()
        if _g['rtc_ok']:
            RTC().datetime(rtc.datetime())
    except OSError as e:
        print('RTC read failed:', e)
        return False
    return _g['rtc_ok']


def _set_clock(rtc, dt):
    """Set both the PCF8523 and the ESP32 clock from an 8-tuple."""
    before = int(time.time())
    RTC().datetime(dt)
    # Setting the clock redefines the time base — often by years, when the
    # RTC was unset. Carry the logging epoch along with it so elapsed_s stays
    # continuous instead of jumping the width of the correction. The periodic
    # RTC resync is deliberately not compensated: those steps are the drift
    # correction, and elapsed_s should follow them.
    if _g['log_t0'] is not None:
        _g['log_t0'] += int(time.time()) - before
    if rtc is not None:
        rtc.datetime(dt)
        _g['rtc_ok']   = not rtc.lost_power()
        _g['batt_low'] = rtc.battery_low()
    return _g['rtc_ok']


async def _clock_task():
    """Pull the ESP32 clock back onto the RTC every _RESYNC_S seconds."""
    while True:
        await asyncio.sleep(_RESYNC_S)
        rtc = _g['rtc']
        if rtc is None:
            continue
        try:
            before = time.time()
            if _sync_mcu_clock(rtc):
                drift = time.time() - before
                if abs(drift) >= _DRIFT_WARN:
                    print('Clock resync: stepped MCU clock by {:+d} s'.format(drift))
        except Exception as e:
            print('Clock resync failed:', e)


# ---------------------------------------------------------------------------
# Radio
# ---------------------------------------------------------------------------

def _radio_off():
    """Shut the WiFi down. Nothing uses it, and this is a battery device.

    The interface state can survive a soft reset, so an AP left running by a
    previous firmware would otherwise keep drawing current forever.
    """
    try:
        import network
        for iface in (network.AP_IF, network.STA_IF):
            w = network.WLAN(iface)
            if w.active():
                w.active(False)
    except Exception as e:
        print('Could not disable WiFi:', e)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _open_log():
    """Create a new CSV, write its header, and make it the active log.

    Returns (filename, message), or (None, reason) if it could not be
    opened. Every file is self-contained: its own location, start time and
    clock provenance, with elapsed_s counting from zero again.
    """
    # One instant for the name, the header and the elapsed_s origin, kept as
    # whole seconds so that start + elapsed_s is exactly the row's wall clock.
    t0 = int(time.time())
    fname = _filename(t0)
    # A stopped RTC hands out the same name on every boot, which would
    # otherwise silently overwrite the previous run's data.
    stem, n = fname[:-4], 1
    while n < 100:
        try:
            os.stat(fname)
        except OSError:
            break                       # the name is free
        fname = '{}-{}.csv'.format(stem, n)
        n += 1
    try:
        with open(fname, 'w') as fh:
            if _g['lat'] is not None:
                fh.write('# Location: lat={}, lon={}\n'.format(_g['lat'], _g['lon']))
            fh.write('# Start: {}  (elapsed_s = 0)\n'.format(_timestamp(t0)))
            # Timestamps are local wall-clock with no timezone, so record
            # where they came from and whether they can be believed.
            if _g['rtc'] is None:
                clock = 'ESP32 internal only, no RTC - times unreliable'
            elif not _g['rtc_ok']:
                clock = 'PCF8523 RTC NOT SET - times unreliable'
            else:
                clock = 'PCF8523 RTC, local time'
            fh.write('# Clock: {}\n'.format(clock))
            fh.write(_CSV_HEADER)
    except OSError as e:
        print('Could not open log file:', e)
        return None, str(e)
    _g['logfile'] = fname
    _g['log_t0']  = t0
    return fname, 'started'


def _start_logging():
    if _g['logging']:
        return _g['logfile'], 'already logging'
    fname, msg = _open_log()
    if fname is None:
        return None, msg
    _g['logging'] = True
    print('Logging started:', fname)
    return fname, msg


def _stop_logging():
    if _g['logging']:
        print('Logging stopped:', _g['logfile'])
        _g['logging'] = False


# ---------------------------------------------------------------------------
# Commands
#
# One JSON object per line, tagged so that terminal noise and half-typed
# input are ignored rather than misread:
#
#     AQC {"id":7,"cmd":"log_start"}
#
# Every command draws exactly one 'ack' carrying the same id, so the host can
# match replies to requests even though status and data records are
# interleaved with them. 'get' acks first and then streams 'file' records.
# ---------------------------------------------------------------------------

def _cmd_set_loc(c):
    try:
        lat = float(c['lat'])
        lon = float(c['lon'])
    except (KeyError, TypeError, ValueError):
        return False, 'invalid lat/lon', None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return False, 'lat/lon out of range', None
    _g['lat'] = lat
    _g['lon'] = lon
    saved = _save_config()
    return True, 'saved {:.6f}, {:.6f}{}'.format(
        lat, lon, '' if saved else ' (not persisted)'), None


def _cmd_set_clock(c):
    try:
        s = c['ts']                    # "2026-06-11T14:30:00"
        date, t = s.split('T')
        y, mo, d = date.split('-')
        h, mi, sec = t.split(':')
        dt = (int(y), int(mo), int(d), 0, int(h), int(mi), int(sec), 0)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        return False, 'invalid datetime: ' + str(e), None

    try:
        ok = _set_clock(_g['rtc'], dt)
    except (OSError, ValueError) as e:
        return False, 'RTC write failed: ' + str(e), None

    print('Clock set to:', s)
    if _g['rtc'] is None:
        return True, 'ESP32 clock set (no RTC): ' + s, None
    if not ok:
        return False, 'RTC written but oscillator still stopped - check the battery', None
    return True, 'clock set: ' + s, None


def _cmd_autostart(c):
    _g['autostart'] = bool(c.get('on'))
    saved = _save_config()
    return (saved,
            'autostart {}{}'.format('on' if _g['autostart'] else 'off',
                                    '' if saved else ' (not persisted)'),
            None)


def _cmd_delete(c):
    name = c.get('name')
    if not isinstance(name, str) or '/' in name or not name.endswith('.csv'):
        return False, 'invalid filename', None
    if _g['logging'] and _g['logfile'] == name:
        return False, 'cannot delete the active log file', None
    try:
        os.remove(name)
    except OSError:
        return False, 'not found', None
    print('Deleted:', name)
    return True, 'deleted ' + name, None


async def _send_file(cid, c):
    """Stream one CSV back as base64 'file' records, then an eof record.

    The host verifies what arrived against the byte count and checksum in
    the eof record, so a chunk lost to a serial hiccup is detected rather
    than silently producing a short file. 'off' lets it resume instead of
    starting the whole transfer again.
    """
    name = c.get('name')
    if not isinstance(name, str) or '/' in name or not name.endswith('.csv'):
        _ack(cid, False, 'invalid filename')
        return
    try:
        off = int(c.get('off', 0))
    except (TypeError, ValueError):
        off = 0
    try:
        size = os.stat(name)[6]
    except OSError:
        _ack(cid, False, 'not found')
        return
    if _g['sending']:
        _ack(cid, False, 'another download is in progress')
        return

    _g['sending'] = True
    _ack(cid, True, 'sending', {'name': name, 'size': size, 'off': off})
    total = 0
    csum  = 0
    seq   = 0
    try:
        with open(name, 'rb') as fh:
            if off:
                fh.seek(off)
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                csum   = (csum + sum(chunk)) & 0xFFFFFFFF
                total += len(chunk)
                _emit('file', {
                    'id':  cid,
                    'seq': seq,
                    'd':   binascii.b2a_base64(chunk).decode().strip(),
                })
                seq += 1
                await asyncio.sleep_ms(_CHUNK_PAUSE)
    except OSError as e:
        _emit('file', {'id': cid, 'eof': True, 'err': str(e),
                       'n': total, 'sum': csum})
        return
    finally:
        _g['sending'] = False
    _emit('file', {'id': cid, 'eof': True, 'n': total, 'sum': csum})


async def _handle_command(line):
    """Dispatch one already-untagged JSON command line."""
    try:
        c = json.loads(line)
    except ValueError:
        _emit('ack', {'id': None, 'ok': False, 'msg': 'malformed JSON'})
        return
    if not isinstance(c, dict):
        _emit('ack', {'id': None, 'ok': False, 'msg': 'not an object'})
        return

    cid  = c.get('id')
    name = c.get('cmd')

    if name == 'get':
        await _send_file(cid, c)
        return

    if name == 'ping':
        ok, msg, data = True, 'pong', None
    elif name == 'status':
        ok, msg, data = True, 'status', None
    elif name == 'log_start':
        fname, msg = _start_logging()
        ok, data = fname is not None, {'logfile': fname} if fname else None
    elif name == 'log_stop':
        _stop_logging()
        ok, msg, data = True, 'stopped', None
    elif name == 'set_loc':
        ok, msg, data = _cmd_set_loc(c)
    elif name == 'set_clock':
        ok, msg, data = _cmd_set_clock(c)
    elif name == 'autostart':
        ok, msg, data = _cmd_autostart(c)
    elif name == 'list':
        ok, msg, data = True, 'files', {'files': _list_files()}
    elif name == 'delete':
        ok, msg, data = _cmd_delete(c)
    else:
        _ack(cid, False, 'unknown command: {}'.format(name))
        return

    _ack(cid, ok, msg, data)
    # Every command either changes configuration or is a request to see it,
    # so refresh the host immediately rather than up to 5 s later.
    _emit_status()


async def _command_task():
    """Read tagged command lines from USB serial without ever blocking.

    sys.stdin.read() would stall the whole scheduler until a byte arrived,
    so poll first and only read what is already buffered.
    """
    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)
    buf = []
    while True:
        lines = []
        # Cap the work per pass: a host that floods the port must not be able
        # to keep this loop from yielding to the sensor task.
        for _ in range(256):
            if not poller.poll(0):
                break
            ch = sys.stdin.read(1)
            if not ch:
                break
            if ch == '\n':
                lines.append(''.join(buf))
                buf = []
            elif ch != '\r':
                buf.append(ch)
                if len(buf) > _CMD_MAX_LINE:
                    buf = []        # runaway line: drop it and resynchronise
        for line in lines:
            line = line.strip()
            if line.startswith(_CMD_TAG):
                try:
                    await _handle_command(line[len(_CMD_TAG):].strip())
                except Exception as e:
                    print('Command failed:', e)
        await asyncio.sleep_ms(20)


# ---------------------------------------------------------------------------
# Sensor task
# ---------------------------------------------------------------------------

async def _sensor_task(sen):
    sen.device_reset()
    sen.start_measurement()
    print('Sensor: waiting for first reading...')
    while not sen.data_ready():
        await asyncio.sleep_ms(50)

    while True:
        while not sen.data_ready():
            await asyncio.sleep_ms(50)

        m   = sen.read_measured_values()
        ts  = _timestamp()

        row = {
            'pm1p0':  m.pm1p0,
            'pm2p5':  m.pm2p5,
            'pm4p0':  m.pm4p0,
            'pm10p0': m.pm10p0,
            'voc':    m.voc_index,
            'nox':    m.nox_index,
        }

        _g['latest'] = row
        _g['ts']     = ts

        for k in _KEYS:
            v = row[k]
            if v is not None:
                mx = _g['max'][k]
                if mx is None or v > mx:
                    _g['max'][k] = v

        ring = _g['ring']
        ring.append(row)
        if len(ring) > 60:
            ring.pop(0)

        _emit('data', {
            'ts': ts,
            'v':  _round1(row),
            'mx': _round1(_g['max']),
            'mn': _compute_mean(ring),
        })

        if _g['logging'] and _g['logfile']:
            elapsed = int(time.time()) - (_g['log_t0'] or 0)
            # Roll over on the hour mark. Done before the row is written,
            # so the sample that trips it becomes row 0 of the new file
            # rather than being lost or written to both.
            if _ROTATE_S and elapsed >= _ROTATE_S:
                previous = _g['logfile']
                fname, why = _open_log()
                if fname is None:
                    # Stay on the current file; if the filesystem is
                    # genuinely gone, the append below stops logging.
                    print('Log rotation failed, staying on {}: {}'.format(
                        previous, why))
                else:
                    print('Log rotated: {} -> {}'.format(previous, fname))
                    elapsed = int(time.time()) - _g['log_t0']
                    _emit_status()
            line = '{},{},{},{},{},{},{}\n'.format(
                elapsed,
                _fmt(m.pm1p0), _fmt(m.pm2p5), _fmt(m.pm4p0), _fmt(m.pm10p0),
                _fmt(m.voc_index), _fmt(m.nox_index),
            )
            try:
                with open(_g['logfile'], 'a') as fh:
                    fh.write(line)
            except OSError as e:
                # A full or unwritable filesystem stops the log, not the
                # device: readings keep streaming to the host.
                print('Log write failed, logging stopped:', e)
                _stop_logging()
                _emit_status()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main():
    _g['boot_ms'] = time.ticks_ms()

    _radio_off()
    _load_config()

    i2c = I2C(0, scl=Pin(6), sda=Pin(5), freq=100_000)
    _open_rtc(i2c)
    sen = SEN65(i2c)

    print('Clock:', _timestamp())
    print('Location: lat={}, lon={}'.format(_g['lat'], _g['lon']))

    if _g['autostart']:
        _start_logging()

    asyncio.create_task(_sensor_task(sen))
    asyncio.create_task(_clock_task())
    asyncio.create_task(_status_task())
    asyncio.create_task(_command_task())

    print('Ready. Run aq_bridge.py on a host to view and configure.')

    while True:
        await asyncio.sleep(3600)

asyncio.run(_main())
