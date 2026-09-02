#!/usr/bin/env python3
"""
USB-serial to web bridge for the AQ-logger — a second, display-only dashboard.

Runs on a host computer, reads the tagged JSON lines main.py streams over
USB serial, and serves a dashboard at http://127.0.0.1:9876 with four
sections: configuration (read-only), the live reading table, the last 200
samples, and 30-minute plots of every variable.

Nothing here can change the device. Logging, location and the clock are set
from the WiFi dashboard at http://192.168.4.1 — this one only watches.

    pip install pyserial
    python3 aq_bridge.py                  # auto-detect the board
    python3 aq_bridge.py --port /dev/ttyACM0
    python3 aq_bridge.py --list           # show candidate ports and exit

The dashboard defaults to port 9876 rather than 8000, which is heavily
contested (nginx, Django, python -m http.server). If it is taken anyway the
bridge steps to the next free port and tells you which one it used.

DTR is asserted on open. On an ESP32-S3's native USB CDC that is how the
host says "I am listening": with DTR low, MicroPython treats stdout as
disconnected and truncates it, so records arrive shredded or not at all.
RTS is left low, which is what an ordinary terminal does and does not reset
the board. --no-dtr exists for the rare adapter that auto-resets on DTR.

The port is opened exclusively, so a second bridge (or a REPL) fails to
start rather than silently stealing bytes from this one — two readers on one
tty corrupt the stream for both.
"""

import argparse
import errno
import json
import re
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is required:  pip install pyserial")

TAG = "AQ1"
KEYS = ("pm1p0", "pm2p5", "pm4p0", "pm10p0", "voc", "nox")
NAMES = ("PM1.0", "PM2.5", "PM4.0", "PM10", "VOC", "NOx")
UNITS = ("µg/m³", "µg/m³", "µg/m³", "µg/m³",
         "index", "index")

PLOT_SECONDS = 30 * 60      # widest the plot x-axis ever gets
MIN_PLOT_SECONDS = 5 * 60   # narrowest, so a fresh start is not a single dot
TABLE_ROWS = 200            # history table shows the last 200 samples
# 30 min at ~1 Hz, with headroom so a faster sensor cannot truncate the window.
HISTORY = PLOT_SECONDS * 2

# Espressif USB VIDs plus the usual USB-UART bridges, best guess first.
_USB_VIDS = (0x303A, 0x10C4, 0x1A86, 0x0403, 0x2341)


class State:
    """Everything the web layer serves, guarded by one lock.

    Samples carry a monotonic sequence number so a browser can ask for just
    what it has not seen yet; sending the whole 30-minute window every
    second would be ~100x the traffic for no benefit.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.samples = deque(maxlen=HISTORY)
        self.status = {}
        self.seq = 0
        self.connected = False
        self.port = None
        self.error = ""
        self.last_rx = 0.0
        self.lines = 0
        self.bad_lines = 0

    def add_sample(self, rec):
        with self.lock:
            self.seq += 1
            self.samples.append({
                "seq": self.seq,
                "ts": rec.get("ts", ""),
                "host": time.time(),
                "v": rec.get("v", {}),
                "mx": rec.get("mx", {}),
                "mn": rec.get("mn", {}),
            })
            self.last_rx = time.time()

    def set_status(self, rec):
        with self.lock:
            rec.pop("t", None)
            self.status = rec
            self.last_rx = time.time()

    def snapshot(self, since):
        """Samples newer than `since`, plus enough context to render.

        `full` tells the browser to throw away what it had: either it asked
        for something older than the window still holds, or the bridge
        restarted and sequence numbers went backwards.
        """
        with self.lock:
            oldest = self.samples[0]["seq"] if self.samples else 0
            full = since <= 0 or since < oldest - 1 or since > self.seq
            if full:
                # A full resync only needs what the page actually renders:
                # the 30-minute plot window, widened if necessary so the
                # 200-row table is still populated after a quiet spell.
                cut = time.time() - PLOT_SECONDS
                new = [s for s in self.samples if s["host"] >= cut]
                if len(new) < TABLE_ROWS:
                    new = list(self.samples)[-TABLE_ROWS:]
            else:
                new = [s for s in self.samples if s["seq"] > since]
            latest = self.samples[-1] if self.samples else None
            stale = (time.time() - self.last_rx) if self.last_rx else None
            return {
                "full": full,
                "seq": self.seq,
                "samples": new,
                "latest": latest,
                "status": self.status,
                "connected": self.connected,
                "port": self.port,
                "error": self.error,
                "stale": round(stale, 1) if stale is not None else None,
                "lines": self.lines,
                "bad_lines": self.bad_lines,
                "count": len(self.samples),
                "plot_seconds": PLOT_SECONDS,
                "min_plot_seconds": MIN_PLOT_SECONDS,
                "table_rows": TABLE_ROWS,
            }


STATE = State()


# ---------------------------------------------------------------------------
# Serial reader
# ---------------------------------------------------------------------------

def pick_port(explicit=None):
    if explicit:
        return explicit
    ports = list(list_ports.comports())
    # Real USB devices only: /dev/ttyS* are motherboard UARTs that always
    # exist on Linux and would otherwise win the race.
    usb = [p for p in ports if p.vid is not None]
    for vid in _USB_VIDS:
        for p in usb:
            if p.vid == vid:
                return p.device
    return usb[0].device if usb else None


def describe_ports():
    rows = [p for p in list_ports.comports() if p.vid is not None]
    if not rows:
        return "  (no USB serial devices found)"
    return "\n".join(
        "  %-20s %04X:%04X  %s" % (p.device, p.vid, p.pid, p.description)
        for p in rows)


def serial_reader(port_arg, baud, stop, use_dtr=True):
    """Read tagged lines forever, reconnecting when the board goes away."""
    line_re = re.compile(r"^%s\s+(\{.*\})\s*$" % TAG)
    junk_since = None
    while not stop.is_set():
        port = pick_port(port_arg)
        if port is None:
            with STATE.lock:
                STATE.connected = False
                STATE.error = "no USB serial device found"
            stop.wait(2.0)
            continue

        ser = None
        try:
            ser = serial.Serial()
            ser.port = port
            ser.baudrate = baud
            ser.timeout = 1.0
            ser.dsrdtr = False
            ser.rtscts = False
            # Refuse to share the port. Linux happily lets two processes open
            # the same tty and then splits the bytes between them, which
            # corrupts every line for both readers.
            ser.exclusive = True
            ser.open()
            try:
                # DTR high = "a host is reading". MicroPython's USB CDC drops
                # stdout when this is low, which shreds records mid-line.
                # RTS stays low: that combination does not reset the board.
                ser.dtr = use_dtr
                ser.rts = False
            except (OSError, IOError):
                pass        # some drivers refuse; harmless

            # Whatever the device sent before DTR came up is a partial line;
            # drop it so the first record the bridge sees is a whole one.
            time.sleep(0.2)
            try:
                ser.reset_input_buffer()
            except (OSError, IOError):
                pass

            with STATE.lock:
                STATE.connected = True
                STATE.port = port
                STATE.error = ""
            print("Connected to %s at %d baud" % (port, baud))

            while not stop.is_set():
                raw = ser.readline()
                if not raw:
                    continue            # just a read timeout, keep waiting
                with STATE.lock:
                    STATE.lines += 1
                try:
                    line = raw.decode("utf-8", "replace").strip()
                except Exception:
                    continue
                m = line_re.match(line)
                if not m:
                    # Boot banners and print() diagnostics land here.
                    if line:
                        print("  dev: %s" % line[:160])
                    # A steady stream that never parses is the signature of
                    # truncated output, so say so instead of sitting mute.
                    if junk_since is None:
                        junk_since = time.time()
                    elif time.time() - junk_since > 15:
                        junk_since = time.time()
                        print("Note: reading data but no complete '%s {...}' "
                              "records for 15 s." % TAG)
                        if not use_dtr:
                            print("      --no-dtr is set; on an ESP32-S3 that "
                                  "truncates the stream. Try without it.")
                        else:
                            print("      Check nothing else is reading the port.")
                    continue
                try:
                    rec = json.loads(m.group(1))
                except ValueError:
                    with STATE.lock:
                        STATE.bad_lines += 1
                    continue
                junk_since = None
                kind = rec.get("t")
                if kind == "data":
                    STATE.add_sample(rec)
                elif kind == "status":
                    STATE.set_status(rec)

        except (serial.SerialException, OSError) as e:
            msg = str(e)
            if "Permission denied" in msg:
                msg += "  (add yourself to the 'dialout' group and re-login)"
            elif "Device or resource busy" in msg or "Errno 16" in msg:
                msg += ("  (another program holds the port — close the REPL, "
                        "or a second copy of this bridge)")
            with STATE.lock:
                STATE.connected = False
                STATE.error = msg
            print("Serial: %s — retrying in 2 s" % msg)
            stop.wait(2.0)
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
    with STATE.lock:
        STATE.connected = False


# ---------------------------------------------------------------------------
# Web server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass                     # the console is for device output

    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self._send(200, "text/html", HTML)
        elif url.path == "/api/state":
            try:
                since = int(parse_qs(url.query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            self._send(200, "application/json",
                       json.dumps(STATE.snapshot(since)))
        else:
            self._send(404, "text/plain", "Not found")


HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AQ-logger — USB monitor</title>
<style>
*{box-sizing:border-box}
body{font:14px monospace;margin:16px;background:#111827;color:#d1d5db}
h1{color:#60a5fa;margin:0 0 4px;font-size:20px}
h3{color:#9ca3af;margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:1px}
.card{background:#1f2937;border-radius:8px;padding:14px 16px;margin-bottom:12px}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #374151;padding:5px 10px;text-align:right}
th{background:#111827;color:#9ca3af;font-weight:normal;font-size:12px;position:sticky;top:0}
td:first-child,th:first-child{text-align:left}
.vl{color:#34d399}.vm{color:#fbbf24}.va{color:#60a5fa}
.dim{color:#6b7280}.warn{color:#fca5a5}.ok{color:#34d399}
#hd{max-height:460px;overflow:auto;border-radius:4px}
#hd table{font-size:12px}
.cfg{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:8px 18px}
.cfg div{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid #374151;padding:3px 0}
.cfg span:first-child{color:#9ca3af}
.pill{display:inline-block;padding:2px 9px;border-radius:10px;font-size:12px}
.pon{background:#065f46;color:#d1fae5}.poff{background:#7f1d1d;color:#fee2e2}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(660px,1fr));gap:14px}
.pl h4{margin:0 0 2px;font-size:13px;color:#d1d5db;font-weight:normal}
.pl .sub{font-size:11px;color:#6b7280;margin-bottom:4px}
.pl .unit{color:#6b7280;font-size:11px}
canvas{width:100%;height:300px;display:block;background:#111827;border-radius:4px}
.tabs{display:flex;gap:2px;border-bottom:1px solid #374151;margin:-4px -4px 12px}
.tab{background:none;color:#9ca3af;border:none;border-bottom:2px solid transparent;
padding:8px 15px;font:inherit;font-size:13px;cursor:pointer;border-radius:0}
.tab:hover{color:#d1d5db}
.tab.active{color:#60a5fa;border-bottom-color:#60a5fa}
.note{font-size:12px;color:#6b7280;margin-top:8px}
</style>
</head>
<body>
<h1>AQ-logger — USB monitor</h1>
<div id="conn" class="dim" style="font-size:12px;margin-bottom:12px">Connecting…</div>

<div class="card">
<h3>Configuration <span class="dim" style="text-transform:none;letter-spacing:0">(view only)</span></h3>
<div class="cfg" id="cfg"></div>
<div class="note" id="cfgnote"></div>
</div>

<div class="card">
<div class="tabs">
<button class="tab" data-panel="p_live">Live readings</button>
<button class="tab" data-panel="p_hist">Recent samples</button>
<button class="tab" data-panel="p_plot">Plots</button>
</div>

<div class="panel" id="p_live">
<table>
<thead><tr><th>Parameter</th><th class="vl">Latest</th><th class="vm">Max</th><th class="va">1-min Mean</th><th>Units</th></tr></thead>
<tbody id="tb"></tbody>
</table>
<div class="note" id="lts"></div>
</div>

<div class="panel" id="p_hist" hidden>
<h3 id="hdn"></h3>
<div id="hd"></div>
</div>

<div class="panel" id="p_plot" hidden>
<h3 id="pwin">Plots</h3>
<div class="grid" id="pg"></div>
</div>
</div>

<script>
const CFG = __CONFIG__;
const K = CFG.keys, N = CFG.names, U = CFG.units;
const WIN_MAX = CFG.plot_seconds * 1000, WIN_MIN = CFG.min_plot_seconds * 1000;
let samples = [], since = 0, status = {}, lastSeen = null;

// The x-axis grows with the data instead of showing 28 minutes of blank:
// the span of what we hold, clamped between 5 and 30 minutes.
function plotWindow() {
  if (!samples.length) return WIN_MIN;
  const span = Date.now() - samples[0].host * 1000;
  return Math.max(WIN_MIN, Math.min(WIN_MAX, span));
}

const f = v => (v === null || v === undefined) ? "--" : Number(v).toFixed(1);
const pad = v => String(v).padStart(2, "0");
const hhmmss = ms => { const d = new Date(ms);
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()); };
const hhmm = ms => { const d = new Date(ms);
  return pad(d.getHours()) + ":" + pad(d.getMinutes()); };

// --- plot scaffolding -----------------------------------------------------
const canvases = {};
K.forEach((k, i) => {
  const d = document.createElement("div");
  d.className = "pl";
  d.innerHTML = '<h4>' + N[i] + ' <span class="unit">' + U[i] + '</span></h4>'
              + '<div class="sub" id="s_' + k + '">--</div>'
              + '<canvas id="c_' + k + '"></canvas>';
  document.getElementById("pg").appendChild(d);
  canvases[k] = document.getElementById("c_" + k);
});

function drawPlot(k, idx, win) {
  const cv = canvases[k], ctx = cv.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  if (cv.width !== W * dpr || cv.height !== H * dpr) {
    cv.width = W * dpr; cv.height = H * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const padL = 52, padR = 10, padT = 12, padB = 26;
  const now = Date.now(), t0 = now - win;
  const pts = samples.filter(s => s.host * 1000 >= t0 && s.v[k] !== null
                                  && s.v[k] !== undefined);

  // Axes frame first, so an empty plot still reads as a plot.
  ctx.strokeStyle = "#374151"; ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padL, padT); ctx.lineTo(padL, H - padB); ctx.lineTo(W - padR, H - padB);
  ctx.stroke();
  ctx.fillStyle = "#6b7280"; ctx.font = "11px monospace";
  ctx.textAlign = "center";

  // --- time axis: a tick every minute -------------------------------------
  // Ticks sit on wall-clock minute boundaries rather than N minutes before
  // now, so they stay put as time advances instead of sliding every frame.
  const x = t => padL + ((t - t0) / win) * (W - padL - padR);
  const plotW = W - padL - padR;
  const pxPerMin = plotW / (win / 60000);
  // Label only as often as the labels will fit without touching.
  let labelEvery = 30;
  for (const step of [1, 2, 5, 10, 15, 30]) {
    if (step * pxPerMin >= 46) { labelEvery = step; break; }
  }
  for (let t = Math.ceil(t0 / 60000) * 60000; t <= now; t += 60000) {
    const px = x(t);
    const major = ((t / 60000) % labelEvery) === 0;
    if (major) {
      ctx.strokeStyle = "#1f2937";          // faint gridline behind the data
      ctx.beginPath(); ctx.moveTo(px, padT); ctx.lineTo(px, H - padB); ctx.stroke();
    }
    ctx.strokeStyle = "#374151";
    ctx.beginPath();
    ctx.moveTo(px, H - padB); ctx.lineTo(px, H - padB + (major ? 5 : 3));
    ctx.stroke();
    // Skip labels that would run off either end of the axis.
    if (major && px > padL + 16 && px < W - padR - 16)
      ctx.fillText(hhmm(t), px, H - 6);
  }

  const sub = document.getElementById("s_" + k);
  if (pts.length < 1) {
    sub.textContent = "waiting for data";
    return;
  }

  let lo = Infinity, hi = -Infinity;
  for (const p of pts) { const v = p.v[k]; if (v < lo) lo = v; if (v > hi) hi = v; }
  if (hi === lo) { hi = lo + 1; lo = Math.max(0, lo - 1); }   // flat line: give it room
  const span = hi - lo;
  lo -= span * 0.08; hi += span * 0.08;
  if (lo < 0 && pts.every(p => p.v[k] >= 0)) lo = 0;          // never imply negatives
  const rng = hi - lo || 1;

  const y = v => (H - padB) - ((v - lo) / rng) * (H - padT - padB);

  ctx.strokeStyle = "#1f2937";
  for (let g = 1; g < 4; g++) {
    const yy = padT + (H - padT - padB) * g / 4;
    ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(W - padR, yy); ctx.stroke();
  }

  ctx.strokeStyle = ["#34d399","#fbbf24","#60a5fa","#f472b6","#a78bfa","#fb923c"][idx % 6];
  ctx.lineWidth = 2;
  ctx.beginPath();
  let first = true, prevT = null;
  for (const p of pts) {
    const t = p.host * 1000;
    // A gap longer than 10 s is missing data, not a straight line through it.
    if (prevT !== null && t - prevT > 10000) first = true;
    const px = x(t), py = y(p.v[k]);
    first ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
    first = false; prevT = t;
  }
  ctx.stroke();

  ctx.fillStyle = "#6b7280"; ctx.textAlign = "right";
  ctx.fillText(hi.toFixed(1), padL - 6, padT + 9);
  ctx.fillText(((hi + lo) / 2).toFixed(1), padL - 6, padT + (H - padT - padB) / 2 + 4);
  ctx.fillText(lo.toFixed(1), padL - 6, H - padB);

  const cur = pts[pts.length - 1].v[k];
  sub.textContent = "now " + f(cur) + "   ·   " + pts.length + " pts";
}

function drawAll() {
  // A hidden panel has zero width, so drawing into it would produce nothing
  // and waste the work; the tab switch redraws instead.
  if (document.getElementById("p_plot").hidden) return;
  const win = plotWindow();
  K.forEach((k, i) => drawPlot(k, i, win));
  const m = Math.round(win / 60000 * 10) / 10;
  document.getElementById("pwin").textContent =
    "Last " + m + " min" + (win >= WIN_MAX ? "" : " (window grows to 30 min)");
}

// --- tabs -----------------------------------------------------------------
function showTab(id) {
  const panels = document.querySelectorAll(".panel");
  for (const p of panels) p.hidden = (p.id !== id);
  for (const t of document.querySelectorAll(".tab"))
    t.classList.toggle("active", t.dataset.panel === id);
  try { localStorage.setItem("aq_tab", id); } catch (e) {}
  if (id === "p_plot") drawAll();
}
for (const t of document.querySelectorAll(".tab"))
  t.addEventListener("click", () => showTab(t.dataset.panel));
let startTab = "p_live";
try { startTab = localStorage.getItem("aq_tab") || startTab; } catch (e) {}
if (!document.getElementById(startTab)) startTab = "p_live";
showTab(startTab);

// --- rendering ------------------------------------------------------------
function renderConn(d) {
  const c = document.getElementById("conn");
  if (!d.connected) {
    c.className = "warn";
    c.textContent = "Serial disconnected" + (d.error ? " — " + d.error : "")
                  + " — retrying…";
  } else if (d.stale !== null && d.stale > 5) {
    c.className = "warn";
    c.textContent = "Connected to " + d.port + " but no data for "
                  + d.stale.toFixed(0) + " s";
  } else {
    c.className = "ok";
    c.textContent = "Connected to " + d.port + " · " + d.count + " samples buffered"
                  + " · " + d.lines + " lines read";
  }
}

function renderCfg(d) {
  const s = d.status || {};
  const yn = (v, good) => v === undefined ? '<span class="dim">--</span>'
      : '<span class="' + (v === good ? "ok" : "warn") + '">' + (v ? "yes" : "no") + '</span>';
  const up = s.up === undefined ? "--"
      : Math.floor(s.up / 3600) + "h " + Math.floor((s.up % 3600) / 60) + "m";
  const rows = [
    ["Access point", s.ssid ? s.ssid + " @ " + s.ip : "--"],
    ["Device time", s.ts || "--"],
    ["Uptime", up],
    ["Latitude", s.lat === undefined ? "--" : s.lat],
    ["Longitude", s.lon === undefined ? "--" : s.lon],
    ["Logging", s.logging === undefined ? '<span class="dim">--</span>'
        : '<span class="pill ' + (s.logging ? "pon" : "poff") + '">'
          + (s.logging ? "RUNNING" : "IDLE") + "</span>"],
    ["Log file", s.logfile || "--"],
    ["RTC detected", yn(s.rtc_present, true)],
    ["RTC time valid", yn(s.rtc_ok, true)],
    ["Backup battery low", yn(s.batt_low, false)],
    ["Files on device", s.files ? s.files.length : "--"],
  ];
  document.getElementById("cfg").innerHTML = rows.map(
    r => "<div><span>" + r[0] + "</span><span>" + r[1] + "</span></div>").join("");
  document.getElementById("cfgnote").textContent =
    "Read-only. Logging, location and the clock are set on the device's own "
    + "dashboard at http://" + (s.ip || "192.168.4.1") + " over WiFi.";
}

function renderTables(d) {
  const L = d.latest;
  document.getElementById("tb").innerHTML = K.map((k, i) =>
    "<tr><td>" + N[i] + "</td>"
    + '<td class="vl">' + (L ? f(L.v[k]) : "--") + "</td>"
    + '<td class="vm">' + (L ? f(L.mx[k]) : "--") + "</td>"
    + '<td class="va">' + (L ? f(L.mn[k]) : "--") + "</td>"
    + '<td class="dim">' + U[i] + "</td></tr>").join("");
  document.getElementById("lts").textContent =
    L ? "Device timestamp: " + L.ts : "No samples received yet.";

  const rows = samples.slice(-CFG.table_rows).reverse();
  document.getElementById("hdn").textContent =
    "Last " + rows.length + " samples (most recent first, max "
    + CFG.table_rows + ")";
  document.getElementById("hd").innerHTML =
    "<table><thead><tr><th>Time</th>"
    + N.map(n => "<th>" + n + "</th>").join("")
    + "</tr></thead><tbody>"
    + rows.map(s => "<tr><td>" + (s.ts || hhmmss(s.host * 1000)) + "</td>"
        + K.map(k => "<td>" + f(s.v[k]) + "</td>").join("") + "</tr>").join("")
    + "</tbody></table>";
}

async function poll() {
  try {
    const d = await (await fetch("/api/state?since=" + since)).json();
    if (d.full) samples = [];
    if (d.samples.length) samples = samples.concat(d.samples);
    since = d.seq;
    // Keep a little more than the plot window so the table stays populated
    // when the device has been quiet.
    const cut = Date.now() / 1000 - CFG.plot_seconds * 2;
    if (samples.length && samples[0].host < cut)
      samples = samples.filter(s => s.host >= cut);
    status = d.status;
    renderConn(d); renderCfg(d); renderTables(d);
  } catch (e) {
    const c = document.getElementById("conn");
    c.className = "warn";
    c.textContent = "Bridge unreachable — is aq_bridge.py still running?";
  }
}

setInterval(poll, 1000);
setInterval(drawAll, 1000);
window.addEventListener("resize", drawAll);
poll(); drawAll();
</script>
</body>
</html>"""

# The page's column list comes from the constants above, so the two cannot
# drift apart.
HTML = HTML.replace("__CONFIG__", json.dumps({
    "keys": list(KEYS),
    "names": list(NAMES),
    "units": list(UNITS),
    "plot_seconds": PLOT_SECONDS,
    "min_plot_seconds": MIN_PLOT_SECONDS,
    "table_rows": TABLE_ROWS,
}))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

DEFAULT_HTTP_PORT = 9876


def bind_http(host, port, explicit):
    """Bind the dashboard, stepping to a free port if one was not demanded.

    'Address already in use' means another program on this machine is
    serving that port; it says nothing about the serial link, so report it
    as its own problem rather than letting it surface as a traceback.
    """
    candidates = [port] if explicit else range(port, port + 20)
    for p in candidates:
        try:
            httpd = ThreadingHTTPServer((host, p), Handler)
            if p != port:
                print("Port %d was busy; using %d instead." % (port, p))
            return httpd, p
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            last = e
    print("Cannot serve the dashboard on %s:%d — %s."
          % (host, port, last.strerror or last))
    if last.errno == errno.EACCES:
        print("Ports below 1024 need root; pick a higher one with --http-port.")
    else:
        print("Something else on this machine is already using that port.")
        print("Find it with:  ss -ltnp | grep :%d" % port)
        print("Or just pick another:  --http-port %d" % (port + 1))
    return None, None

def main():
    ap = argparse.ArgumentParser(
        description="Serve a display-only AQ-logger dashboard from USB serial.")
    ap.add_argument("--port", help="serial device (default: auto-detect)")
    ap.add_argument("--baud", type=int, default=115200, help="default: 115200")
    ap.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT,
                    help="default: %d; steps to the next free port if busy"
                         % DEFAULT_HTTP_PORT)
    ap.add_argument("--host", default="127.0.0.1",
                    help="HTTP bind address; use 0.0.0.0 to allow other "
                         "machines on your network (default: 127.0.0.1)")
    ap.add_argument("--no-dtr", action="store_true",
                    help="do not assert DTR on open; only for adapters that "
                         "auto-reset on it — an ESP32-S3 needs DTR to send")
    ap.add_argument("--list", action="store_true",
                    help="list candidate serial ports and exit")
    args = ap.parse_args()

    if args.list:
        print("USB serial ports:")
        print(describe_ports())
        return 0

    port = pick_port(args.port)
    if port is None:
        print("No USB serial device found. Candidates:")
        print(describe_ports())
        print("\nPlug the board in, or pass --port explicitly.")
    else:
        print("Using serial port %s" % port)

    # Bind before opening the serial port: no point grabbing the board's
    # port only to exit because the dashboard has nowhere to listen.
    httpd, http_port = bind_http(args.host, args.http_port,
                                 args.http_port != DEFAULT_HTTP_PORT)
    if httpd is None:
        return 1
    httpd.daemon_threads = True

    stop = threading.Event()
    reader = threading.Thread(target=serial_reader,
                              args=(args.port, args.baud, stop, not args.no_dtr),
                              daemon=True)
    reader.start()

    print("Dashboard: http://%s:%d   (Ctrl-C to stop)"
          % ("127.0.0.1" if args.host in ("0.0.0.0", "") else args.host,
             http_port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        stop.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
