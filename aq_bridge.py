#!/usr/bin/env python3
"""
USB-serial to web bridge for the AQ-logger — the device's only interface.

Runs on a host computer, reads the tagged JSON lines main.py streams over USB
serial, and serves a dashboard at http://127.0.0.1:9876 that both displays the
readings and configures the device: location, clock, start/stop logging, and
downloading or deleting the CSV log files held on the board's flash.

The device has no WiFi and no web server of its own. Commands travel back up
the same serial line as tagged JSON, each carrying an id the device echoes in
its reply, so a button press can be matched to its answer even though sample
and status records are interleaved with it. Downloads come back as base64
chunks that are reassembled and checked here before the browser sees a file.

    pip install pyserial
    python3 aq_bridge.py                  # auto-detect the board
    python3 aq_bridge.py --port /dev/ttyACM0
    python3 aq_bridge.py --list           # show candidate ports and exit

The dashboard defaults to port 9876 rather than 8000, which is heavily
contested (nginx, Django, python -m http.server). If it is taken anyway the
bridge steps to the next free port and tells you which one it used.

It binds to 127.0.0.1 by default. --host 0.0.0.0 exposes the dashboard to your
network, and the dashboard can now change the device and delete its files, so
only do that on a network you trust: there is no authentication.

DTR is asserted on open. On an ESP32-S3's native USB CDC that is how the host
says "I am listening": with DTR low, MicroPython treats stdout as
disconnected and truncates it, so records arrive shredded or not at all.
RTS is left low, which is what an ordinary terminal does and does not reset
the board. --no-dtr exists for the rare adapter that auto-resets on DTR.

The port is opened exclusively, so a second bridge (or a REPL) fails to
start rather than silently stealing bytes from this one — two readers on one
tty corrupt the stream for both.
"""

import argparse
import base64
import errno
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is required:  pip install pyserial")

TAG = "AQ1"          # every line the device emits
CMD_TAG = "AQC"      # every line the bridge sends
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

# Commands the dashboard may send. Anything else is refused here rather than
# forwarded, so a stray POST cannot reach the device.
ALLOWED_COMMANDS = frozenset((
    "ping", "status", "list", "log_start", "log_stop",
    "set_loc", "set_clock", "autostart", "delete",
))

# --- map tiles ------------------------------------------------------------
# The page asks this bridge for tiles, never OpenStreetMap directly: one place
# to identify ourselves, one disk cache, and the browser makes no third-party
# requests. Only the tiles around a saved location are ever fetched — nine or
# so per location change — and they are reused from disk after that, so a
# laptop taken into the field keeps showing sites it has already displayed.
TILE_URL = "https://tile.openstreetmap.org/%d/%d/%d.png"
TILE_UA = ("aq_bridge.py/1.0 (AQ-logger dashboard; single-user local tool; "
           "https://www.openstreetmap.org/copyright)")
TILE_TIMEOUT = 8.0
TILE_MAX_Z = 19
TILE_CACHE = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
    "aq_bridge", "tiles")
# Two at a time is plenty for a nine-tile view and keeps us well inside the
# OSM tile usage policy.
TILE_SEM = threading.Semaphore(2)
TILE_FETCH = True          # cleared by --no-map
TILE_FAILED = {}           # (z,x,y) -> when, so a dead network is not hammered
TILE_FAIL_TTL = 60.0
TILE_LOCK = threading.Lock()

CMD_TIMEOUT = 5.0        # seconds to wait for an ack
FILE_TIMEOUT = 300.0     # ceiling on a whole download
FILE_IDLE = 10.0         # a download with no chunk for this long has stalled
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.csv$")


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
                "map": TILE_FETCH,
            }


STATE = State()


# ---------------------------------------------------------------------------
# Command channel
#
# The device answers every command with an 'ack' carrying the id it was sent,
# and streams a download as 'file' records under that same id. Both arrive on
# the reader thread interleaved with ordinary telemetry, so a request parks on
# an Event here and the reader hands it its records as they land.
# ---------------------------------------------------------------------------

class CommandError(Exception):
    """A command could not be delivered, or the device refused it."""


class Pending:
    def __init__(self, kind):
        self.kind = kind          # "ack" for a plain command, "file" for a get
        self.event = threading.Event()
        self.ack = None
        self.chunks = {}          # seq -> raw bytes
        self.eof = None
        self.last = time.time()   # for the stalled-transfer check


class Commander:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}
        self.next_id = 1
        self.ser = None
        # One transfer at a time: the device refuses a second anyway, and
        # serialising here gives a better message than its refusal would.
        self.transfer = threading.Lock()

    # --- called by the reader thread ---------------------------------------

    def attach(self, ser):
        with self.lock:
            self.ser = ser

    def detach(self):
        """Fail every waiter rather than leaving the UI hung on a dead port."""
        with self.lock:
            self.ser = None
            waiters = list(self.pending.values())
            self.pending.clear()
        for p in waiters:
            p.ack = {"ok": False, "msg": "serial disconnected"}
            p.event.set()

    def on_ack(self, rec):
        with self.lock:
            p = self.pending.get(rec.get("id"))
            if p is None:
                return          # a reply to a request that already timed out
            p.ack = rec
            p.last = time.time()
            # A 'get' acks first and keeps streaming, so only wake that waiter
            # if the device refused it outright.
            if p.kind == "ack" or not rec.get("ok"):
                p.event.set()

    def on_file(self, rec):
        with self.lock:
            p = self.pending.get(rec.get("id"))
            if p is None:
                return
            p.last = time.time()
            if rec.get("eof"):
                p.eof = rec
                p.event.set()
                return
            try:
                p.chunks[int(rec["seq"])] = base64.b64decode(rec["d"])
            except (KeyError, ValueError, TypeError, base64.binascii.Error):
                p.eof = {"err": "undecodable chunk"}
                p.event.set()

    # --- called by HTTP threads --------------------------------------------

    def _open(self, kind):
        with self.lock:
            cid = self.next_id
            self.next_id += 1
            p = Pending(kind)
            self.pending[cid] = p
        return cid, p

    def _close(self, cid):
        with self.lock:
            self.pending.pop(cid, None)

    def _write(self, payload):
        with self.lock:
            ser = self.ser
        if ser is None:
            raise CommandError("device not connected")
        line = (CMD_TAG + " " + json.dumps(payload) + "\n").encode("utf-8")
        try:
            ser.write(line)
            ser.flush()
        except (serial.SerialException, OSError) as e:
            raise CommandError("serial write failed: %s" % e)

    def send(self, cmd, fields=None, timeout=CMD_TIMEOUT):
        """Send one command and return the device's ack record."""
        cid, p = self._open("ack")
        payload = dict(fields or {})
        payload["cmd"] = cmd
        payload["id"] = cid
        try:
            self._write(payload)
            if not p.event.wait(timeout):
                raise CommandError("no reply from the device in %.0f s" % timeout)
            return p.ack
        finally:
            self._close(cid)

    def fetch(self, name):
        """Pull one CSV off the device and return its bytes.

        Verified against the byte count and checksum the device sends with
        the last record: a chunk lost to a serial hiccup has to surface as a
        failed download, not as a CSV that is quietly missing a few rows.
        """
        if not self.transfer.acquire(blocking=False):
            raise CommandError("another download is already running")
        try:
            cid, p = self._open("file")
            try:
                self._write({"cmd": "get", "id": cid, "name": name, "off": 0})
                deadline = time.time() + FILE_TIMEOUT
                while not p.event.wait(0.25):
                    now = time.time()
                    if now - p.last > FILE_IDLE:
                        raise CommandError("transfer stalled after %d chunks"
                                           % len(p.chunks))
                    if now > deadline:
                        raise CommandError("transfer did not finish in %.0f s"
                                           % FILE_TIMEOUT)
                if p.ack is not None and not p.ack.get("ok"):
                    raise CommandError(p.ack.get("msg", "device refused the request"))
                eof = p.eof or {}
                if eof.get("err"):
                    raise CommandError("device read error: %s" % eof["err"])

                seqs = sorted(p.chunks)
                if seqs != list(range(len(seqs))):
                    raise CommandError("missing chunk in transfer")
                data = b"".join(p.chunks[i] for i in seqs)
                if eof.get("n") is not None and len(data) != eof["n"]:
                    raise CommandError("size mismatch: got %d bytes, device sent %d"
                                       % (len(data), eof["n"]))
                if eof.get("sum") is not None:
                    if (sum(data) & 0xFFFFFFFF) != eof["sum"]:
                        raise CommandError("checksum mismatch — transfer corrupted")
                return data
            finally:
                self._close(cid)
        finally:
            self.transfer.release()


CMD = Commander()


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
            # Commands go out on this same handle, from the HTTP threads.
            CMD.attach(ser)
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
                elif kind == "ack":
                    CMD.on_ack(rec)
                elif kind == "file":
                    CMD.on_file(rec)

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
            CMD.detach()
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
    with STATE.lock:
        STATE.connected = False


# ---------------------------------------------------------------------------
# Map tiles
# ---------------------------------------------------------------------------

def tile_path(z, x, y):
    return os.path.join(TILE_CACHE, str(z), str(x), "%d.png" % y)


def get_tile(z, x, y):
    """Return one PNG from the cache, fetching it once if it is not there.

    None means "no tile available" — offline with nothing cached, or the
    tile genuinely does not exist. The page draws its grid instead.
    """
    path = tile_path(z, x, y)
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        pass
    if not TILE_FETCH:
        return None

    key = (z, x, y)
    with TILE_LOCK:
        failed_at = TILE_FAILED.get(key)
        if failed_at is not None and time.time() - failed_at < TILE_FAIL_TTL:
            return None

    with TILE_SEM:
        # Another thread may have fetched it while this one queued.
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            pass
        req = urllib.request.Request(TILE_URL % (z, x, y),
                                     headers={"User-Agent": TILE_UA})
        try:
            with urllib.request.urlopen(req, timeout=TILE_TIMEOUT) as r:
                data = r.read()
        except (urllib.error.URLError, OSError, ValueError) as e:
            with TILE_LOCK:
                TILE_FAILED[key] = time.time()
            print("Map tile %d/%d/%d unavailable: %s" % (z, x, y, e))
            return None

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)     # never leave a half-written tile in the cache
    except OSError as e:
        print("Could not cache tile %d/%d/%d: %s" % (z, x, y, e))
    with TILE_LOCK:
        TILE_FAILED.pop(key, None)
    return data


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
        elif url.path == "/api/download":
            self._download(parse_qs(url.query).get("name", [""])[0])
        elif url.path.startswith("/api/tile/"):
            self._tile(url.path[len("/api/tile/"):])
        else:
            self._send(404, "text/plain", "Not found")

    def do_POST(self):
        # Drain the body first whatever the path: this is a keep-alive
        # connection, and bytes left unread would be parsed as the next
        # request line.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""

        if urlparse(self.path).path != "/api/cmd":
            self._send(404, "text/plain", "Not found")
            return
        try:
            req = json.loads(raw or b"{}")
            if not isinstance(req, dict):
                raise ValueError("body is not an object")
        except (ValueError, TypeError) as e:
            self._json(400, {"ok": False, "msg": "bad request: %s" % e})
            return

        cmd = req.pop("cmd", None)
        if cmd not in ALLOWED_COMMANDS:
            self._json(400, {"ok": False, "msg": "unknown command: %r" % (cmd,)})
            return
        try:
            ack = CMD.send(cmd, req)
        except CommandError as e:
            # The device is unreachable or mute; that is not the browser's
            # fault, so it gets a 503 and the message to display.
            self._json(503, {"ok": False, "msg": str(e)})
            return
        self._json(200, ack if isinstance(ack, dict) else {"ok": False,
                                                           "msg": "no reply"})

    def _tile(self, spec):
        m = re.match(r"^(\d{1,2})/(\d{1,7})/(\d{1,7})\.png$", spec)
        if not m:
            self._send(404, "text/plain", "Not found")
            return
        z, x, y = (int(g) for g in m.groups())
        if z > TILE_MAX_Z or x >= (1 << z) or y >= (1 << z):
            self._send(404, "text/plain", "No such tile")
            return
        data = get_tile(z, x, y)
        if data is None:
            # 404 rather than a placeholder: the page's onerror handler is
            # what puts up the "map unavailable" note.
            self._send(404, "text/plain", "Tile unavailable")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj))

    def _download(self, name):
        """Fetch a CSV off the device and hand it straight to the browser.

        The whole file is held in memory first: a log is a few hundred kB at
        most, and streaming a partial file would mean the browser saving
        something that failed verification.
        """
        if not NAME_RE.match(name):
            self._send(400, "text/plain", "Invalid filename")
            return
        started = time.time()
        try:
            data = CMD.fetch(name)
        except CommandError as e:
            self._send(503, "text/plain", "Download failed: %s" % e)
            print("Download of %s failed: %s" % (name, e))
            return
        print("Sent %s (%d bytes) in %.1f s"
              % (name, len(data), time.time() - started))
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % name)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AQ-logger</title>
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
.pwarn{background:#78350f;color:#fde68a}
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
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:8px}
.row:first-child{margin-top:0}
label{display:flex;align-items:center;gap:6px;font-size:13px;color:#9ca3af}
input[type=text]{background:#111827;color:#e5e7eb;border:1px solid #4b5563;
padding:5px 8px;border-radius:4px;width:130px;font:inherit;font-size:13px}
input[type=text]:focus{outline:none;border-color:#60a5fa}
button{padding:6px 14px;border:none;border-radius:4px;cursor:pointer;
font:inherit;font-size:13px;background:#374151;color:#e5e7eb}
button:hover:not(:disabled){filter:brightness(1.2)}
button:disabled{opacity:.4;cursor:not-allowed}
.go{background:#065f46;color:#d1fae5}.stop{background:#7f1d1d;color:#fee2e2}
.act{background:#1e3a5f;color:#bfdbfe}
.dl{background:#1e3a5f;color:#bfdbfe;padding:3px 9px;font-size:11px}
.rm{background:#450a0a;color:#fca5a5;padding:3px 9px;font-size:11px}
.fi{display:flex;align-items:center;gap:10px;padding:4px 0;font-size:13px;
border-bottom:1px solid #374151}
.fi .nm{flex:1}
.msg{font-size:12px;color:#9ca3af}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
#map{position:relative;height:240px;margin-top:10px;border-radius:4px;
overflow:hidden;background-color:#0b1220;
background-image:linear-gradient(#1f2937 1px,transparent 1px),
linear-gradient(90deg,#1f2937 1px,transparent 1px);background-size:40px 40px}
#maptiles{position:absolute;inset:0;filter:brightness(.86) saturate(.9)}
#maptiles img{position:absolute;width:256px;height:256px;-webkit-user-drag:none}
#map .mk{position:absolute;left:50%;top:50%;width:18px;height:18px;
margin:-9px 0 0 -9px;border:2px solid #f87171;border-radius:50%;
box-shadow:0 0 0 2px rgba(11,18,32,.7),inset 0 0 0 2px rgba(11,18,32,.5)}
#map .mk::after{content:"";position:absolute;left:50%;top:50%;width:3px;
height:3px;margin:-1.5px 0 0 -1.5px;background:#f87171;border-radius:50%}
#map .zm{position:absolute;left:6px;top:6px;display:flex;gap:4px}
#map .zm button{padding:1px 9px;font-size:14px;line-height:1.4;
background:rgba(11,18,32,.8);color:#d1d5db}
#map .attr{position:absolute;right:5px;bottom:4px;font-size:10px;color:#9ca3af;
background:rgba(11,18,32,.78);padding:1px 6px;border-radius:3px}
#map .attr a{color:#9ca3af}
</style>
</head>
<body>
<h1>AQ-logger</h1>
<div id="conn" class="dim" style="font-size:12px;margin-bottom:12px">Connecting…</div>

<div class="card">
<h3>Device</h3>
<div class="cfg" id="cfg"></div>
</div>

<div class="cols">
<div class="card">
<h3>Logging</h3>
<div class="row">
<button id="lb" onclick="toggleLog()" disabled>Logging…</button>
<span id="ls" class="msg"></span>
</div>
<div class="row"><span id="lm" class="msg"></span></div>
<div class="row">
<label title="With this set the device begins a new log by itself on power-up, so it can be deployed with no computer attached">
<input type="checkbox" id="as" onchange="setAutostart()" disabled>
start logging automatically at power-up</label>
<span id="ass" class="msg"></span>
</div>
</div>

<div class="card">
<h3>Clock</h3>
<div class="row">
<span id="rtct" class="dim">--</span>
<span id="rtcd" class="msg"></span>
</div>
<div class="row">
<button class="act" id="csb" onclick="syncClock(false)" disabled>Sync to this computer</button>
<label title="Sync by itself when the device clock is unset or more than 10 s out">
<input type="checkbox" id="auto" checked> auto</label>
<span id="rtcs" class="msg"></span>
</div>
<div class="note">The device has no other time source. Its RTC keeps the time
on a coin cell between sessions.</div>
</div>
</div>

<div class="card">
<h3>Location <span class="dim" style="text-transform:none;letter-spacing:0">written into every log header</span></h3>
<div class="row">
<label>Lat <input type="text" id="lat" placeholder="-33.87"></label>
<label>Lon <input type="text" id="lon" placeholder="151.21"></label>
<button class="go" id="lsb" onclick="saveLoc()" disabled>Save</button>
<span id="locs" class="msg"></span>
</div>
<div id="map">
<div id="maptiles"></div>
<div class="mk" id="mk" hidden></div>
<div class="zm">
<button onclick="zoomMap(-1)" title="Zoom out">&minus;</button>
<button onclick="zoomMap(1)" title="Zoom in">+</button>
</div>
<div class="attr">&copy; <a href="https://www.openstreetmap.org/copyright"
target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors</div>
</div>
<div class="note" id="mapmsg"></div>
</div>

<div class="card">
<h3>Log files <span class="dim" style="text-transform:none;letter-spacing:0">on the device</span></h3>
<div id="fl" class="dim">--</div>
<div class="note" id="dls"></div>
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

// --- talking to the device ------------------------------------------------
// Every control goes through one endpoint; the bridge forwards it over serial
// and hands back the device's own ack, so the message shown is the device's.
let live = false, locDirty = false, autoSynced = false, fileSig = null;
let mapEnabled = null;

async function cmd(name, fields) {
  try {
    const r = await fetch("/api/cmd", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(Object.assign({cmd: name}, fields || {}))});
    return await r.json();
  } catch (e) {
    return {ok: false, msg: "bridge unreachable"};
  }
}

function flash(id, text, hold) {
  const el = document.getElementById(id);
  el.textContent = text;
  if (el._t) clearTimeout(el._t);
  if (hold !== 0) el._t = setTimeout(() => { el.textContent = ""; }, hold || 5000);
}

async function act(id, name, fields) {
  flash(id, "working…", 0);
  const d = await cmd(name, fields);
  flash(id, (d.ok ? "" : "failed: ") + (d.msg || (d.ok ? "done" : "no reply")));
  poll();
  return d;
}

// --- controls -------------------------------------------------------------
function toggleLog() {
  act("ls", status.logging ? "log_stop" : "log_start");
}

function saveLoc() {
  const lat = document.getElementById("lat").value.trim();
  const lon = document.getElementById("lon").value.trim();
  if (!lat || !lon) { flash("locs", "enter lat and lon first"); return; }
  if (isNaN(Number(lat)) || isNaN(Number(lon))) {
    flash("locs", "lat and lon must be numbers"); return;
  }
  locDirty = false;
  act("locs", "set_loc", {lat: Number(lat), lon: Number(lon)});
}

function setAutostart() {
  act("ass", "autostart", {on: document.getElementById("as").checked});
}

for (const id of ["lat", "lon"])
  document.getElementById(id).addEventListener("input", () => { locDirty = true; });

// --- clock ----------------------------------------------------------------
// Device time is local wall-clock with no zone, so compare it against a local
// Date built the same way rather than parsing it as an instant.
function devDate(s) {
  if (!s || s.length < 19) return null;
  const n = (a, b) => Number(s.slice(a, b));
  const dt = new Date(n(0,4), n(5,7)-1, n(8,10), n(11,13), n(14,16), n(17,19));
  return isNaN(dt) ? null : dt;
}

function browserTS() {
  const n = new Date();
  return n.getFullYear() + "-" + pad(n.getMonth()+1) + "-" + pad(n.getDate())
       + "T" + pad(n.getHours()) + ":" + pad(n.getMinutes())
       + ":" + pad(n.getSeconds());
}

async function syncClock(auto) {
  flash("rtcs", "setting…", 0);
  const d = await cmd("set_clock", {ts: browserTS()});
  flash("rtcs", (auto ? "auto: " : "") + (d.msg || "no reply"));
  poll();
}

function renderClock(s) {
  const t = document.getElementById("rtct"), w = document.getElementById("rtcd");
  t.textContent = s.ts || "--";
  const dev = devDate(s.ts);
  let msg = "", cls = "dim";
  if (s.rtc_present === false) { msg = "RTC not detected — check wiring"; cls = "warn"; }
  else if (s.rtc_ok === false) { msg = "clock not set"; cls = "warn"; }
  else if (dev) {
    const drift = Math.round((dev - new Date()) / 1000);
    msg = drift === 0 ? "in sync" : (drift > 0 ? "+" : "") + drift + " s vs this computer";
    cls = Math.abs(drift) > 10 ? "warn" : "ok";
  }
  if (s.batt_low) { msg += (msg ? " — " : "") + "backup battery low"; cls = "warn"; }
  w.textContent = msg; w.className = cls;

  // Nothing on the device knows the time on its own, so this computer is the
  // source of truth: offer it once when the clock is unset or well out.
  if (live && s.rtc_present && !autoSynced && document.getElementById("auto").checked) {
    const bad = s.rtc_ok === false
             || (dev && Math.abs((dev - new Date()) / 1000) > 10);
    if (bad) { autoSynced = true; syncClock(true); }
  }
}

// --- map ------------------------------------------------------------------
// Slippy-map arithmetic, so a saved lat/lon can be shown without a mapping
// library: Web Mercator world pixels at zoom z, then a grid of 256 px tiles
// positioned so the point lands in the middle of the box.
let mapZoom = 13, mapKey = null, mapMisses = 0;
try { mapZoom = Number(localStorage.getItem("aq_zoom")) || 13; } catch (e) {}

const lon2px = (lon, z) => (lon + 180) / 360 * Math.pow(2, z) * 256;
function lat2px(lat, z) {
  const r = Math.max(-85.05, Math.min(85.05, lat)) * Math.PI / 180;
  return (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2
         * Math.pow(2, z) * 256;
}

// Redrawn only when the location or the zoom actually changes — so on boot,
// and when Save reports a new position back. Polling never touches it.
function drawMap(lat, lon, force) {
  if (lat === undefined || lat === null || lon === undefined || lon === null) return;
  const box = document.getElementById("map");
  const W = box.clientWidth, H = box.clientHeight;
  if (!W || !H) return;                       // card not laid out yet
  const key = [lat, lon, mapZoom, W, H].join(",");
  if (!force && key === mapKey) return;
  mapKey = key;
  mapMisses = 0;

  const n = Math.pow(2, mapZoom);
  const left = lon2px(lon, mapZoom) - W / 2, top = lat2px(lat, mapZoom) - H / 2;
  let html = "";
  for (let ty = Math.floor(top / 256); ty <= Math.floor((top + H) / 256); ty++) {
    if (ty < 0 || ty >= n) continue;          // above the pole or below it
    for (let tx = Math.floor(left / 256); tx <= Math.floor((left + W) / 256); tx++) {
      const wx = ((tx % n) + n) % n;          // wrap across the antimeridian
      html += '<img src="/api/tile/' + mapZoom + "/" + wx + "/" + ty + '.png"'
            + ' style="left:' + Math.round(tx * 256 - left) + "px;top:"
            + Math.round(ty * 256 - top) + 'px" onerror="tileMissing()" alt="">';
    }
  }
  document.getElementById("maptiles").innerHTML = html;
  document.getElementById("mk").hidden = false;
  document.getElementById("mapmsg").textContent =
    Number(lat).toFixed(6) + ", " + Number(lon).toFixed(6)
    + "  ·  zoom " + mapZoom;
}

// One missing tile is a hole in the cache; a boxful means there is no map to
// be had, and the grid behind them is the fallback.
function tileMissing() {
  if (++mapMisses < 2) return;
  document.getElementById("maptiles").innerHTML = "";
  const s = status || {};
  document.getElementById("mapmsg").textContent =
    (s.lat === undefined ? "" : Number(s.lat).toFixed(6) + ", "
      + Number(s.lon).toFixed(6) + "  ·  ")
    + (mapEnabled === false
        ? "map tiles disabled (--no-map); showing the grid only"
        : "map tiles unavailable — no connection, and none cached for here");
}

function zoomMap(d) {
  mapZoom = Math.max(2, Math.min(18, mapZoom + d));
  try { localStorage.setItem("aq_zoom", mapZoom); } catch (e) {}
  const s = status || {};
  drawMap(s.lat, s.lon, true);
}

// A resize changes which tiles cover the box; they are already cached, so
// redrawing costs nothing but is pointless to do on every pixel.
let mapResize = null;
window.addEventListener("resize", () => {
  clearTimeout(mapResize);
  mapResize = setTimeout(() => {
    const s = status || {};
    drawMap(s.lat, s.lon, true);
  }, 300);
});

// --- files ----------------------------------------------------------------
const SAFE_NAME = /^[A-Za-z0-9_.-]+\.csv$/;
const kb = n => n === undefined || n === null ? "--"
  : n < 1024 ? n + " B"
  : n < 1024 * 1024 ? (n / 1024).toFixed(1) + " kB"
  : (n / 1048576).toFixed(2) + " MB";

function renderFiles(s) {
  const files = (s.files || []).map(f => Array.isArray(f) ? f : [f, null])
                               .filter(f => SAFE_NAME.test(f[0]));
  // Rebuilding this every second would fight the buttons, so only redraw when
  // the listing actually changes — sizes included, so a growing log updates.
  const sig = JSON.stringify(files) + "|" + live;
  if (sig === fileSig) return;
  fileSig = sig;
  const el = document.getElementById("fl");
  if (!files.length) {
    el.className = "dim";
    el.textContent = live ? "No log files on the device." : "--";
    return;
  }
  el.className = "";
  const dis = live ? "" : " disabled";
  el.innerHTML = files.map(f =>
    '<div class="fi"><span class="nm">' + f[0] + '</span>'
    + '<span class="dim">' + kb(f[1]) + '</span>'
    + '<button class="dl" onclick="dl(\'' + f[0] + '\')"' + dis + '>Download</button>'
    + '<button class="rm" onclick="del(\'' + f[0] + '\')"' + dis + '>Delete</button>'
    + '</div>').join("");
}

// Fetched rather than linked, so a failed transfer shows its reason here
// instead of the browser saving the error page as a .csv.
async function dl(name) {
  flash("dls", "Downloading " + name + " over serial — this takes a few "
             + "seconds per 100 kB…", 0);
  try {
    const r = await fetch("/api/download?name=" + encodeURIComponent(name));
    if (!r.ok) { flash("dls", "Download failed: " + await r.text(), 15000); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    flash("dls", "Saved " + name + " (" + kb(blob.size) + ")", 15000);
  } catch (e) {
    flash("dls", "Download failed: " + e, 15000);
  }
}

async function del(name) {
  if (!confirm("Delete " + name + " from the device?\nThis cannot be undone.")) return;
  fileSig = null;
  await act("dls", "delete", {name: name});
}

// --- device panel ---------------------------------------------------------
function renderCfg(d) {
  const s = d.status || {};
  live = !!d.connected && s.ts !== undefined;

  const yn = (v, good) => v === undefined ? '<span class="dim">--</span>'
      : '<span class="' + (v === good ? "ok" : "warn") + '">' + (v ? "yes" : "no") + '</span>';
  const up = s.up === undefined ? "--"
      : Math.floor(s.up / 3600) + "h " + Math.floor((s.up % 3600) / 60) + "m";
  const rows = [
    ["Device time", s.ts || "--"],
    ["Uptime", up],
    ["Latitude", s.lat === undefined ? "--" : s.lat],
    ["Longitude", s.lon === undefined ? "--" : s.lon],
    ["Log file", s.logfile || "--"],
    ["RTC detected", yn(s.rtc_present, true)],
    ["RTC time valid", yn(s.rtc_ok, true)],
    ["Backup battery low", yn(s.batt_low, false)],
    ["Files on device", s.files ? s.files.length : "--"],
    // Free space is the one figure here that stops the logger, so it earns a
    // colour: amber while the reserve is close, red once logging is barred.
    ["Flash free", s.free === undefined || s.free === null ? '<span class="dim">--</span>'
        : '<span class="' + (s.free < s.min_free ? "warn"
            : s.free < s.min_free * 2 ? "vm" : "ok") + '">' + kb(s.free) + "</span>"
          + (s.min_free ? ' <span class="dim">of which ' + kb(s.min_free)
             + " is reserved</span>" : "")],
  ];
  document.getElementById("cfg").innerHTML = rows.map(
    r => "<div><span>" + r[0] + "</span><span>" + r[1] + "</span></div>").join("");

  // --- controls follow the device, except where the user is mid-edit ------
  const lb = document.getElementById("lb");
  lb.textContent = s.logging === undefined ? "Logging…"
                 : s.logging ? "Stop logging" : "Start logging";
  lb.className = s.logging ? "stop" : "go";
  lb.disabled = !live;
  // A run that ended by itself must say so: an unexplained IDLE looks the
  // same as one somebody asked for.
  const stopped = !s.logging && s.stop_reason;
  document.getElementById("lm").innerHTML = s.logging === undefined ? ""
    : '<span class="pill ' + (s.logging ? "pon" : stopped ? "pwarn" : "poff") + '">'
      + (s.logging ? "RUNNING" : stopped ? "STOPPED" : "IDLE") + "</span>"
      + (s.logging && s.logfile ? ' <span class="dim">' + s.logfile + "</span>" : "")
      + (stopped ? ' <span class="warn">' + s.stop_reason + "</span>" : "");

  if (!locDirty && s.lat !== undefined) {
    const la = document.getElementById("lat"), lo = document.getElementById("lon");
    if (document.activeElement !== la) la.value = s.lat;
    if (document.activeElement !== lo) lo.value = s.lon;
  }
  mapEnabled = d.map;
  drawMap(s.lat, s.lon);
  document.getElementById("lsb").disabled = !live;
  document.getElementById("csb").disabled = !live;
  const as = document.getElementById("as");
  as.disabled = !live;
  if (document.activeElement !== as && s.autostart !== undefined)
    as.checked = !!s.autostart;

  renderClock(s);
  renderFiles(s);
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
    global TILE_FETCH, TILE_CACHE

    ap = argparse.ArgumentParser(
        description="Serve the AQ-logger dashboard, and configure the device, over USB serial.")
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
    ap.add_argument("--no-map", action="store_true",
                    help="never fetch map tiles; the location card falls back "
                         "to a plain coordinate grid. Cached tiles are still "
                         "shown")
    ap.add_argument("--tile-cache", default=TILE_CACHE,
                    help="where to keep downloaded map tiles (default: %s)"
                         % TILE_CACHE)
    ap.add_argument("--list", action="store_true",
                    help="list candidate serial ports and exit")
    args = ap.parse_args()

    TILE_FETCH = not args.no_map
    TILE_CACHE = os.path.expanduser(args.tile_cache)

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
