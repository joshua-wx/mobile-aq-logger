"""
AQ-logger — MicroPython web application for ESP32-S3.

Creates a WiFi access point "AQ-logger" (open, no password) and serves a
dashboard on http://192.168.4.1 that shows live SEN65 readings, lets the
user start/stop CSV logging, set a GPS location written into the log header,
and download/delete log files.

Time comes from a battery-backed PCF8523 RTC. At boot the ESP32's internal
clock is set from it, timestamps are then taken from the internal clock so
the shared I2C bus is not hit once per sample, and the internal clock is
re-synced from the PCF8523 every 10 minutes to stop it drifting. The RTC
itself is set from the browser via the dashboard.

The same readings are also streamed over USB serial as tagged JSON lines,
one per sample, for aq_bridge.py on a host computer to pick up and serve as
a second, display-only dashboard. See the "Serial stream" section below for
the wire format. Set _SERIAL_STREAM = False to turn it off.

To add a WPA2 password change authmode=0 to authmode=3 and add
password='yourpassword' in _start_ap().

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

import network
import os
import time
from machine import I2C, Pin, RTC

from pcf8523 import PCF8523
from sen65 import SEN65

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AP_SSID    = 'AQ-logger'
_KEYS       = ('pm1p0', 'pm2p5', 'pm4p0', 'pm10p0', 'voc', 'nox')
_CSV_HEADER = 'timestamp,PM1.0_ug_m3,PM2.5_ug_m3,PM4.0_ug_m3,PM10_ug_m3,VOC_index,NOx_index\n'

_RESYNC_S    = 600      # re-read the PCF8523 into the ESP32 clock this often
_DRIFT_WARN  = 2        # seconds of MCU-vs-RTC drift worth printing

_SERIAL_STREAM   = True   # emit tagged JSON lines on USB serial
_SERIAL_TAG      = 'AQ1'  # line prefix the host bridge looks for
_SERIAL_STATUS_S = 5      # seconds between 'status' records

_HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AQ-logger</title>
<style>
*{box-sizing:border-box}
body{font:14px monospace;margin:16px;background:#111827;color:#d1d5db}
h1{color:#60a5fa;margin:0 0 4px}
h3{color:#9ca3af;margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:1px}
.card{background:#1f2937;border-radius:8px;padding:14px 16px;margin-bottom:12px}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #374151;padding:5px 10px;text-align:right}
th{background:#111827;color:#9ca3af;font-weight:normal;font-size:12px}
td:first-child{text-align:left}
.vl{color:#34d399}.vm{color:#fbbf24}.va{color:#60a5fa}
.warn{color:#fca5a5}.ok{color:#34d399}.dim{color:#6b7280}
input{background:#111827;color:#e5e7eb;border:1px solid #4b5563;padding:4px 8px;border-radius:4px;width:130px}
button{padding:6px 14px;border:none;border-radius:4px;cursor:pointer;font:inherit;font-size:13px}
.gs{background:#065f46;color:#d1fae5}.rs{background:#7f1d1d;color:#fee2e2}
.dl{background:#1e3a5f;color:#bfdbfe;padding:3px 8px;font-size:11px}
.rm{background:#450a0a;color:#fca5a5;padding:3px 8px;font-size:11px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:6px}
#ts{color:#6b7280;font-size:12px;margin-bottom:10px}
.fi{margin:3px 0;display:flex;align-items:center;gap:6px;font-size:13px}
</style>
</head>
<body>
<h1>AQ-logger</h1>
<div id="ts">Connecting...</div>
<div class="card">
<h3>Measurements</h3>
<table>
<thead><tr><th>Parameter</th><th class="vl">Latest</th><th class="vm">Max</th><th class="va">1-min Mean</th></tr></thead>
<tbody id="tb"></tbody>
</table>
</div>
<div class="card">
<h3>Clock</h3>
<div class="row">
<span id="rtct" class="dim" style="font-size:13px">--</span>
<span id="rtcd" style="font-size:12px"></span>
</div>
<div class="row" style="margin-top:8px">
<button style="background:#1e3a5f;color:#bfdbfe" onclick="syncRTC()">Sync RTC to browser time</button>
<label style="font-size:12px" title="Sync automatically when this page sees the device clock is unset or more than 10 s out"><input type="checkbox" id="auto" checked style="width:auto"> auto</label>
<span id="rtcs" class="ok" style="font-size:12px"></span>
</div>
</div>
<div class="card">
<h3>Logging</h3>
<div class="row">
<label>Lat <input id="lat" placeholder="-33.87"></label>
<label>Lon <input id="lon" placeholder="151.21"></label>
<button style="background:#064e3b;color:#d1fae5" onclick="saveLoc()">Save Location</button>
<span id="locs" style="color:#34d399;font-size:12px"></span>
</div>
<div class="row" style="margin-top:10px">
<button id="lb" onclick="toggleLog()">Loading...</button>
<span id="ls" style="color:#6b7280;font-size:12px"></span>
</div>
</div>
<div class="card">
<h3>Log Files</h3>
<div id="fl">Loading...</div>
</div>
<script>
const K=["pm1p0","pm2p5","pm4p0","pm10p0","voc","nox"];
const N=["PM1.0","PM2.5","PM4.0","PM10","VOC","NOx"];
let logging=false,locSet=false,autoDone=false;
const f=v=>v==null?"--":Number(v).toFixed(1);
// Device time is local wall-clock, so build a local Date to compare against.
function devDate(s){
if(!s||s.length<19)return null;
const n=(a,b)=>Number(s.slice(a,b));
const dt=new Date(n(0,4),n(5,7)-1,n(8,10),n(11,13),n(14,16),n(17,19));
return isNaN(dt)?null:dt;}
function browserTS(){
const n=new Date(),p=v=>String(v).padStart(2,'0');
return n.getFullYear()+'-'+p(n.getMonth()+1)+'-'+p(n.getDate())+'T'
+p(n.getHours())+':'+p(n.getMinutes())+':'+p(n.getSeconds());}
async function poll(){
try{
const d=await(await fetch("/data")).json();
document.getElementById("ts").textContent="Updated: "+d.ts;
showClock(d);
document.getElementById("tb").innerHTML=K.map((k,i)=>
`<tr><td>${N[i]}</td><td class="vl">${f(d.latest[k])}</td><td class="vm">${f(d.max[k])}</td><td class="va">${f(d.mean[k])}</td></tr>`
).join("");
logging=d.logging;
const lb=document.getElementById("lb");
lb.textContent=logging?"Stop Logging":"Start Logging";
lb.className=logging?"rs":"gs";
document.getElementById("ls").textContent=logging?"Logging: "+d.logfile:"Idle";
if(!locSet&&d.lat!==null){
document.getElementById("lat").value=d.lat;
document.getElementById("lon").value=d.lon;
locSet=true;
}
document.getElementById("fl").innerHTML=d.files.length
?d.files.map(fn=>`<div class="fi"><span>${fn}</span><a href="/dl/${fn}"><button class="dl">Download</button></a><button class="rm" onclick="del('${fn}')">Delete</button></div>`).join("")
:"<span style='color:#6b7280'>No log files.</span>";
}catch(e){document.getElementById("ts").textContent="Error: "+e;}
}
async function toggleLog(){
await fetch("/log/"+(logging?"stop":"start"),{method:"POST"});
poll();
}
function showClock(d){
const t=document.getElementById("rtct"),w=document.getElementById("rtcd");
t.textContent=d.now||"--";
const dev=devDate(d.now);
let msg="",cls="dim";
if(!d.rtc_present){msg="RTC not detected — check wiring";cls="warn";}
else if(!d.rtc_ok){msg="clock not set";cls="warn";}
else if(dev){
const drift=Math.round((dev-new Date())/1000);
msg=(drift===0?"in sync":(drift>0?"+":"")+drift+" s vs browser");
cls=Math.abs(drift)>10?"warn":"ok";
}
if(d.batt_low){msg+=(msg?" — ":"")+"backup battery low";cls="warn";}
w.textContent=msg;w.className=cls;
// Nothing else on this device knows the time, so the browser is the source
// of truth: offer it automatically when the clock is unset or well out.
if(d.rtc_present&&!autoDone&&document.getElementById("auto").checked){
const bad=!d.rtc_ok||(dev&&Math.abs((dev-new Date())/1000)>10);
if(bad){autoDone=true;syncRTC(true);}
}
}
async function syncRTC(auto){
const r=await fetch("/rtc/set",{method:"POST",
headers:{"Content-Type":"application/x-www-form-urlencoded"},
body:"ts="+browserTS()});
const txt=await r.text();
document.getElementById("rtcs").textContent=(auto?"Auto: ":"")+txt;
setTimeout(()=>document.getElementById("rtcs").textContent="",4000);
poll();
}
async function saveLoc(){
const lat=document.getElementById("lat").value.trim();
const lon=document.getElementById("lon").value.trim();
if(!lat||!lon){document.getElementById("locs").textContent="Enter lat and lon first";return;}
const r=await fetch("/location",{method:"POST",
headers:{"Content-Type":"application/x-www-form-urlencoded"},
body:"lat="+encodeURIComponent(lat)+"&lon="+encodeURIComponent(lon)});
document.getElementById("locs").textContent=await r.text();
setTimeout(()=>document.getElementById("locs").textContent="",3000);
}
async function del(fn){
if(!confirm("Delete "+fn+"?"))return;
await fetch("/del/"+fn,{method:"POST"});
poll();
}
setInterval(poll,1000);
poll();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_g = {
    'latest':  {k: None for k in _KEYS},
    'max':     {k: None for k in _KEYS},
    'ring':    [],        # last <=60 readings for 1-min mean
    'logging': False,
    'logfile': None,
    'ts':      '--',
    'lat':     -37.8136,
    'lon':     144.9631,
    'ip':      '0.0.0.0',
    'boot_ms': 0,
    'rtc':     None,      # PCF8523 instance, or None if it did not answer
    'rtc_ok':  False,     # False while the RTC reports a stopped oscillator
    'batt_low': False,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(v):
    return '' if v is None else '{:.1f}'.format(v)

def _timestamp():
    """Local wall-clock time as 'YYYY-MM-DD HH:MM:SS' for CSV rows."""
    y, mo, d, h, mi, s = time.localtime()[:6]
    return '{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}'.format(y, mo, d, h, mi, s)

def _filename():
    y, mo, d, h, mi, s = time.localtime()[:6]
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

def _list_csv():
    return sorted(f for f in os.listdir() if f.endswith('.csv'))

def _urldecode(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == '%' and i + 2 < len(s):
            out.append(chr(int(s[i + 1:i + 3], 16)))
            i += 3
        elif s[i] == '+':
            out.append(' ')
            i += 1
        else:
            out.append(s[i])
            i += 1
    return ''.join(out)

def _parse_form(body):
    params = {}
    for pair in body.decode().split('&'):
        if '=' in pair:
            k, v = pair.split('=', 1)
            params[_urldecode(k)] = _urldecode(v)
    return params

# ---------------------------------------------------------------------------
# Serial stream
#
# One line per record, so a host can read it with readline() and ignore
# anything it does not recognise:
#
#     AQ1 {"t":"data","ts":"2026-09-02 13:26:45","v":{...},"mx":{...},"mn":{...}}
#     AQ1 {"t":"status","lat":-37.8,"logging":true,...}
#
# 'data' goes out once per sensor sample (~1 Hz) and carries the same
# latest/max/1-min-mean triple the web dashboard shows. 'status' goes out
# every _SERIAL_STATUS_S seconds, and immediately whenever the config
# changes, so the host dashboard is not up to 5 s stale after a button press.
#
# Ordinary print() diagnostics share this port; they simply lack the tag.
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
        'rtc_present': _g['rtc'] is not None,
        'rtc_ok':      _g['rtc_ok'],
        'batt_low':    _g['batt_low'],
        'ssid':        _AP_SSID,
        'ip':          _g['ip'],
        'files':       _list_csv(),
        'up':          time.ticks_diff(time.ticks_ms(), _g['boot_ms']) // 1000,
    })


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
    whatever the ESP32 booted with until someone syncs from a browser.
    """
    try:
        rtc = PCF8523(i2c)
    except OSError as e:
        print('WARNING: PCF8523 not responding at 0x68 ({}) - running without it.'.format(e))
        return None

    _g['rtc'] = rtc
    if not _sync_mcu_clock(rtc):
        print('WARNING: PCF8523 oscillator stopped - time is not trustworthy.')
        print('         Open the dashboard and sync the clock to your browser.')
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
    RTC().datetime(dt)
    if rtc is not None:
        rtc.datetime(dt)
        _g['rtc_ok']   = not rtc.lost_power()
        _g['batt_low'] = rtc.battery_low()
    return _g['rtc_ok']


async def _clock_task(rtc):
    """Pull the ESP32 clock back onto the RTC every _RESYNC_S seconds."""
    while True:
        await asyncio.sleep(_RESYNC_S)
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
# WiFi AP
# ---------------------------------------------------------------------------

def _start_ap():
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=_AP_SSID, authmode=0)   # authmode=0 → open
    for _ in range(50):
        if ap.active():
            break
        time.sleep(0.1)
    ip = ap.ifconfig()[0]
    _g['ip'] = ip
    print('AP "{}" started | dashboard: http://{}'.format(_AP_SSID, ip))

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
            line = '{},{},{},{},{},{},{}\n'.format(
                ts,
                _fmt(m.pm1p0), _fmt(m.pm2p5), _fmt(m.pm4p0), _fmt(m.pm10p0),
                _fmt(m.voc_index), _fmt(m.nox_index),
            )
            with open(_g['logfile'], 'a') as fh:
                fh.write(line)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_STATUS = {200: 'OK', 400: 'Bad Request', 404: 'Not Found'}

def _build(status, ctype, body):
    if isinstance(body, str):
        body = body.encode('utf-8')
    hdr = (
        'HTTP/1.0 {} {}\r\n'
        'Content-Type: {}; charset=utf-8\r\n'
        'Content-Length: {}\r\n'
        'Connection: close\r\n\r\n'
    ).format(status, _STATUS.get(status, ''), ctype, len(body))
    return hdr.encode('utf-8') + body

def _ok(text):
    return _build(200, 'text/plain', text)

def _ok_json(obj):
    return _build(200, 'application/json', json.dumps(obj))

def _err(status, msg):
    return _build(status, 'text/plain', msg)

# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

def _route_data():
    return _ok_json({
        'ts':      _g['ts'],
        'latest':  _round1(_g['latest']),
        'max':     _round1(_g['max']),
        'mean':    _compute_mean(_g['ring']),
        'logging': _g['logging'],
        'logfile': _g['logfile'] or '',
        'files':   _list_csv(),
        'lat':     _g['lat'],
        'lon':     _g['lon'],
        'now':         _timestamp(),
        'rtc_present': _g['rtc'] is not None,
        'rtc_ok':      _g['rtc_ok'],
        'batt_low':    _g['batt_low'],
    })

def _route_log_start():
    if not _g['logging']:
        fname = _filename()
        _g['logfile'] = fname
        _g['logging'] = True
        with open(fname, 'w') as fh:
            if _g['lat'] is not None:
                fh.write('# Location: lat={}, lon={}\n'.format(_g['lat'], _g['lon']))
            fh.write('# Start: {}\n'.format(_timestamp()))
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
        print('Logging started:', fname)
        _emit_status()
    return _ok('started')

def _route_log_stop():
    if _g['logging']:
        print('Logging stopped:', _g['logfile'])
        _g['logging'] = False
        _emit_status()
    return _ok('stopped')

def _route_set_rtc(body, rtc):
    params = _parse_form(body)
    try:
        s = params['ts']               # "2026-06-11T14:30:00"
        date, t = s.split('T')
        y, mo, d = date.split('-')
        h, mi, sec = t.split(':')
        dt = (int(y), int(mo), int(d), 0, int(h), int(mi), int(sec), 0)
    except (KeyError, ValueError) as e:
        return _err(400, 'Invalid datetime: ' + str(e))

    try:
        ok = _set_clock(rtc, dt)
    except (OSError, ValueError) as e:
        return _err(400, 'RTC write failed: ' + str(e))

    print('Clock set to:', s)
    _emit_status()
    if rtc is None:
        return _ok('ESP32 clock set (no RTC): ' + s)
    if not ok:
        return _err(400, 'RTC written but oscillator still stopped - check the battery')
    return _ok('Clock set: ' + s)

def _route_set_location(body):
    params = _parse_form(body)
    try:
        lat = float(params['lat'])
        lon = float(params['lon'])
    except (KeyError, ValueError):
        return _err(400, 'Invalid lat/lon')
    _g['lat'] = lat
    _g['lon'] = lon
    _emit_status()
    return _ok('Saved: {:.6f}, {:.6f}'.format(lat, lon))

def _route_delete(fname):
    if '/' in fname or not fname.endswith('.csv'):
        return _err(400, 'Invalid filename')
    if _g['logging'] and _g['logfile'] == fname:
        return _err(400, 'Cannot delete the active log file')
    try:
        os.remove(fname)
        print('Deleted:', fname)
        _emit_status()
        return _ok('deleted')
    except OSError:
        return _err(404, 'Not found')

# ---------------------------------------------------------------------------
# HTTP client handler
# ---------------------------------------------------------------------------

async def _handle_client(reader, writer, rtc):
    try:
        req_line = await reader.readline()
        parts    = req_line.decode().split()
        if len(parts) < 2:
            return
        method, path = parts[0], parts[1]

        content_length = 0
        while True:
            h = await reader.readline()
            if h in (b'\r\n', b''):
                break
            hl = h.lower()
            if hl.startswith(b'content-length:'):
                try:
                    content_length = int(hl.split(b':', 1)[1].strip())
                except ValueError:
                    pass

        body = b''
        if content_length > 0:
            body = await reader.read(content_length)

        # --- route dispatch -------------------------------------------------
        if method == 'GET' and path == '/':
            resp = _build(200, 'text/html', _HTML)

        elif method == 'GET' and path == '/data':
            resp = _route_data()

        elif method == 'POST' and path == '/log/start':
            resp = _route_log_start()

        elif method == 'POST' and path == '/log/stop':
            resp = _route_log_stop()

        elif method == 'POST' and path == '/rtc/set':
            resp = _route_set_rtc(body, rtc)

        elif method == 'POST' and path == '/location':
            resp = _route_set_location(body)

        elif method == 'POST' and path.startswith('/del/'):
            resp = _route_delete(path[5:])

        elif method == 'GET' and path.startswith('/dl/'):
            fname = path[4:]
            if '/' in fname or not fname.endswith('.csv'):
                resp = _err(400, 'Invalid filename')
                writer.write(resp)
                await writer.drain()
                return
            try:
                size = os.stat(fname)[6]
                hdr  = (
                    'HTTP/1.0 200 OK\r\n'
                    'Content-Type: text/csv\r\n'
                    'Content-Length: {}\r\n'
                    'Content-Disposition: attachment; filename="{}"\r\n'
                    'Connection: close\r\n\r\n'
                ).format(size, fname).encode()
                writer.write(hdr)
                await writer.drain()
                with open(fname, 'r') as fh:
                    while True:
                        chunk = fh.read(512)
                        if not chunk:
                            break
                        writer.write(chunk.encode('utf-8'))
                        await writer.drain()
            except OSError:
                writer.write(_err(404, 'Not found'))
                await writer.drain()
            return

        else:
            resp = _err(404, 'Not found')

        writer.write(resp)
        await writer.drain()

    except Exception as e:
        print('Request error:', e)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main():
    _g['boot_ms'] = time.ticks_ms()

    i2c = I2C(0, scl=Pin(6), sda=Pin(5), freq=100_000)
    rtc = _open_rtc(i2c)
    sen = SEN65(i2c)

    print('Clock:', _timestamp())

    _start_ap()
    asyncio.create_task(_sensor_task(sen))
    asyncio.create_task(_clock_task(rtc))
    asyncio.create_task(_status_task())

    async def _cb(r, w):
        await _handle_client(r, w, rtc)

    await asyncio.start_server(_cb, '0.0.0.0', 80)
    print('Web server ready.')

    while True:
        await asyncio.sleep(3600)

asyncio.run(_main())
