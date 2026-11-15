#!/usr/bin/env python3
"""PencilPad: use an iPad + Apple Pencil as a trackpad and command pad for this Mac.

Run this on the Mac you want to control, then open the printed URL in Safari
on the iPad (same Wi-Fi). The terminal app that runs it needs Accessibility
permission (System Settings > Privacy & Security > Accessibility), and Screen
Recording permission for the Screenshot corner.
"""

import asyncio
import datetime
import errno
import hashlib
import ipaddress
import json
import math
import os
import secrets
import shlex
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import Quartz
from AppKit import NSPasteboard
from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

# ---------------------------------------------------------------- settings
PORT = int(os.environ.get("PENCILPAD_PORT", 8765))
POINTER_SPEED = 1.6        # pointer travel per unit of pencil travel
POINTER_ACCEL = 0.045      # extra speed for fast strokes (0 = none)
SCROLL_SPEED = 1.4
HOVER_MOVES = False        # True: a hovering Pencil also moves the pointer

# Dictate corner. mode "hold" = key is held while the corner is pressed
# (push-to-talk); mode "toggle" = each press taps the key once.
# key: any name in KEYCODES below. mods: e.g. ["control", "option"].
DICTATION = {"mode": "hold", "key": "f13", "mods": []}

SCREENSHOT_PASTE = True    # after a region capture, press Ctrl+V (Claude Code image paste)
# ---------------------------------------------------------------------------

KEYCODES = {
    "return": 36, "esc": 53, "space": 49, "tab": 48, "v": 9,
    "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80,
    "fn": 63, "control": 59, "option": 58, "command": 55, "shift": 56,
    "right_option": 61, "right_command": 54, "right_control": 62,
}
MOD_FLAGS = {
    "fn": Quartz.kCGEventFlagMaskSecondaryFn,
    "control": Quartz.kCGEventFlagMaskControl,
    "right_control": Quartz.kCGEventFlagMaskControl,
    "option": Quartz.kCGEventFlagMaskAlternate,
    "right_option": Quartz.kCGEventFlagMaskAlternate,
    "command": Quartz.kCGEventFlagMaskCommand,
    "right_command": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
}
TAP = Quartz.kCGHIDEventTap
TOKEN_FILE = Path(os.environ.get("PENCILPAD_TOKEN_FILE") or Path.home() / ".pencilpad-token")
# This Mac's id, its certificate, and the other Macs it has been paired with.
HOME = Path(os.environ.get("PENCILPAD_HOME") or Path.home() / ".pencilpad")
# The secure (HTTPS) port: the iPad needs it for the microphone and for switching
# between Macs. PENCILPAD_TLS_PORT=off turns it off.
TLS_PORT = os.environ.get("PENCILPAD_TLS_PORT", str(PORT + 1))
TLS_PORT = int(TLS_PORT) if TLS_PORT.isdigit() and int(TLS_PORT) else 0
# A command that takes a WAV file and prints what was said. The menu bar app sets it;
# without it the Dictate corner presses the DICTATION key instead.
TRANSCRIBE = shlex.split(os.environ.get("PENCILPAD_TRANSCRIBE", ""))
# PENCILPAD_DRY_RUN=1: serve the page but post nothing to the Mac. The events it would
# have posted are recorded instead (stdout lines starting "DRY", and GET /<token>/dry).
DRY_RUN = os.environ.get("PENCILPAD_DRY_RUN") == "1"


def post(ev):
    if DRY_RUN:
        dry_record_event(ev)
    else:
        Quartz.CGEventPost(TAP, ev)
BOOT_ID = secrets.token_hex(4)   # changes on every restart; the page reloads when it differs


def load_token():
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(9)
    TOKEN_FILE.write_text(tok)
    TOKEN_FILE.chmod(0o600)
    return tok


class Mac:
    """Posts pointer and keyboard events."""

    def __init__(self):
        self.left_down = False
        self.held_keys = set()
        self.last_click_t = 0.0
        self.last_click_pos = (0.0, 0.0)
        self.click_count = 0
        self.frac = [0.0, 0.0]
        self.last = None                # where we last put the pointer (fractional)
        self.dry_pos = (960.0, 540.0)   # dry run: the pretend pointer position

    # ---- pointer
    def pos(self):
        if DRY_RUN:
            return self.dry_pos
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return loc.x, loc.y

    def bounds(self):
        if DRY_RUN:
            return [DRY_SCREEN]
        err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
        rects = [Quartz.CGDisplayBounds(d) for d in ids[:count]]
        return rects

    def clamp(self, x, y):
        rects = self.bounds()
        for r in rects:
            if (r.origin.x <= x < r.origin.x + r.size.width
                    and r.origin.y <= y < r.origin.y + r.size.height):
                return x, y
        # outside every display: clamp to the display the pointer is on now
        cx, cy = self.pos()
        for r in rects:
            if (r.origin.x <= cx < r.origin.x + r.size.width
                    and r.origin.y <= cy < r.origin.y + r.size.height):
                return (min(max(x, r.origin.x), r.origin.x + r.size.width - 1),
                        min(max(y, r.origin.y), r.origin.y + r.size.height - 1))
        return x, y

    def mouse(self, kind, x, y, button=Quartz.kCGMouseButtonLeft, clicks=1):
        ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), button)
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, clicks)
        post(ev)

    def move(self, dx, dy):
        speed = (dx * dx + dy * dy) ** 0.5
        gain = POINTER_SPEED * (1.0 + POINTER_ACCEL * speed)
        x, y = self.pos()
        # The system may report the pointer rounded to whole points; continue from
        # our own fractional position so slow strokes are not lost, unless
        # something else (a real mouse) has moved the pointer since.
        if self.last and abs(x - self.last[0]) < 1 and abs(y - self.last[1]) < 1:
            x, y = self.last
        nx, ny = self.clamp(x + dx * gain, y + dy * gain)
        self.last = (nx, ny)
        kind = Quartz.kCGEventLeftMouseDragged if self.left_down else Quartz.kCGEventMouseMoved
        ev = Quartz.CGEventCreateMouseEvent(None, kind, (nx, ny), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventDeltaX, round(nx - x))
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventDeltaY, round(ny - y))
        post(ev)

    def click(self):
        x, y = self.pos()
        now = time.monotonic()
        near = abs(x - self.last_click_pos[0]) < 12 and abs(y - self.last_click_pos[1]) < 12
        self.click_count = self.click_count + 1 if (now - self.last_click_t < 0.6 and near) else 1
        self.last_click_t, self.last_click_pos = now, (x, y)
        self.mouse(Quartz.kCGEventLeftMouseDown, x, y, clicks=self.click_count)
        self.mouse(Quartz.kCGEventLeftMouseUp, x, y, clicks=self.click_count)

    def right_click(self):
        x, y = self.pos()
        self.mouse(Quartz.kCGEventRightMouseDown, x, y, Quartz.kCGMouseButtonRight)
        self.mouse(Quartz.kCGEventRightMouseUp, x, y, Quartz.kCGMouseButtonRight)

    def down(self):
        if not self.left_down:
            self.left_down = True
            self.mouse(Quartz.kCGEventLeftMouseDown, *self.pos())

    def up(self):
        if self.left_down:
            self.left_down = False
            self.mouse(Quartz.kCGEventLeftMouseUp, *self.pos())

    def scroll(self, dx, dy):
        self.frac[0] += dx * SCROLL_SPEED
        self.frac[1] += dy * SCROLL_SPEED
        ix, iy = int(self.frac[0]), int(self.frac[1])
        self.frac[0] -= ix
        self.frac[1] -= iy
        if ix or iy:
            ev = Quartz.CGEventCreateScrollWheelEvent(
                None, Quartz.kCGScrollEventUnitPixel, 2, iy, ix)
            post(ev)

    # ---- keyboard
    def key(self, name, is_down, mods=()):
        code = KEYCODES[name]
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, is_down)
        flags = 0
        for m in mods:
            flags |= MOD_FLAGS[m]
        if name in MOD_FLAGS:
            Quartz.CGEventSetType(ev, Quartz.kCGEventFlagsChanged)
            if is_down:
                flags |= MOD_FLAGS[name]
        else:
            # keep the flag macOS gives the key itself (F-keys carry fn, like a real keyboard)
            flags |= Quartz.CGEventGetFlags(ev) & (
                Quartz.kCGEventFlagMaskSecondaryFn | Quartz.kCGEventFlagMaskNumericPad)
        Quartz.CGEventSetFlags(ev, flags)
        post(ev)
        if is_down:
            self.held_keys.add((name, tuple(mods)))
        else:
            self.held_keys.discard((name, tuple(mods)))

    def tap_key(self, name, mods=()):
        self.key(name, True, mods)
        self.key(name, False, mods)

    def type_text(self, text):
        """Types text into the front app, whatever the keyboard layout."""
        if DRY_RUN:
            dry_record({"ev": "text", "s": text})
            return
        units = text.encode("utf-16-le")
        for i in range(0, len(units), 40):           # 20 UTF-16 units per event
            chunk = units[i:i + 40].decode("utf-16-le", "surrogatepass")
            for is_down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(None, 0, is_down)
                Quartz.CGEventSetFlags(ev, 0)
                Quartz.CGEventKeyboardSetUnicodeString(ev, len(chunk.encode("utf-16-le")) // 2, chunk)
                Quartz.CGEventPost(TAP, ev)
            time.sleep(0.004)

    def release_all(self):
        self.up()
        for name, mods in list(self.held_keys):
            self.key(name, False, mods)


mac = Mac()


def computer_name():
    try:
        return subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True,
                              text=True, timeout=3).stdout.strip()
    except Exception:
        return ""


MAC_NAME = computer_name()
shot_running = False


async def screenshot():
    """Interactive region capture to the clipboard, then paste into the front app."""
    global shot_running
    if shot_running:
        return
    shot_running = True
    try:
        if DRY_RUN:
            dry_record({"ev": "screencapture"})
            changed = True
        else:
            pb = NSPasteboard.generalPasteboard()
            before = pb.changeCount()
            proc = await asyncio.create_subprocess_exec("screencapture", "-i", "-c")
            await proc.wait()
            changed = pb.changeCount() != before
        if SCREENSHOT_PASTE and changed:
            await asyncio.sleep(0.25)
            mac.tap_key("v", ["control"])
    finally:
        shot_running = False


def handle(msg):
    t = msg.get("t")
    if t == "m":
        mac.move(num(msg["x"]), num(msg["y"]))
    elif t == "s":
        mac.scroll(num(msg["x"]), num(msg["y"]))
    elif t == "c":
        mac.click()
    elif t == "r":
        mac.right_click()
    elif t == "d":
        mac.down()
    elif t == "u":
        mac.up()
    elif t == "key" and msg.get("k") in ("esc", "return"):
        mac.tap_key(msg["k"])
    elif t == "shot":
        asyncio.ensure_future(screenshot())
    elif t == "dict":
        key, mods = DICTATION["key"], DICTATION.get("mods", [])
        if DICTATION["mode"] == "toggle":
            if msg.get("s"):
                mac.tap_key(key, mods)
        else:
            mac.key(key, bool(msg.get("s")), mods)


async def page(request):
    if request.match_info["token"] != request.app["token"]:
        raise web.HTTPNotFound()
    cfg = {"hover": HOVER_MOVES, "dictMode": DICTATION["mode"], "boot": BOOT_ID, "mac": MAC_NAME,
           "local": local_hostname() or "",
           "self": {k: v for k, v in info(request.app).items() if k != "boot"},
           "peers": list(request.app["peers"].values())}
    html = PAGE.replace("__CFG__", json.dumps(cfg))
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def ws_handler(request):
    if request.match_info["token"] != request.app["token"]:
        raise web.HTTPNotFound()
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    print(f"iPad connected ({request.remote})", flush=True)
    if DRY_RUN:
        dry_record({"ev": "connect"})
    mac.release_all()        # a dropped connection may have left a drag or key held
    request.app["clients"].add(ws)
    ws.secure = request.secure
    await ws.send_json(dict(info(request.app), peers=list(request.app["peers"].values())))
    try:
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                continue
            try:
                handle(json.loads(m.data))
            except Exception as e:  # one bad message must not kill the pad
                print("bad message:", m.data[:120], "->", repr(e))
    finally:
        request.app["clients"].discard(ws)
        if DRY_RUN:
            dry_record({"ev": "disconnect"})
        mac.release_all()
        print("iPad disconnected", flush=True)
    return ws


def lan_ip():
    """This Mac's address on the local network. Asks the hardware ports (Wi-Fi,
    Ethernet) directly, so a VPN's tunnel address is never picked."""
    for i in range(12):
        try:
            out = subprocess.run(["ipconfig", "getifaddr", f"en{i}"], capture_output=True,
                                 text=True, timeout=3).stdout.strip()
        except Exception:
            out = ""
        if out and not out.startswith("169.254."):
            return out
    return "127.0.0.1"


def local_hostname():
    try:
        out = subprocess.run(["scutil", "--get", "LocalHostName"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        return f"{out}.local" if out else None
    except Exception:
        return None


# ---- dry run: what would have been posted, for tests
dry_log = []
DRY_SCREEN = Quartz.CGRectMake(0, 0, 1920, 1080)
EVENT_NAMES = {
    Quartz.kCGEventMouseMoved: "move", Quartz.kCGEventLeftMouseDragged: "drag",
    Quartz.kCGEventLeftMouseDown: "ldown", Quartz.kCGEventLeftMouseUp: "lup",
    Quartz.kCGEventRightMouseDown: "rdown", Quartz.kCGEventRightMouseUp: "rup",
    Quartz.kCGEventScrollWheel: "scroll", Quartz.kCGEventKeyDown: "keydown",
    Quartz.kCGEventKeyUp: "keyup", Quartz.kCGEventFlagsChanged: "flags",
}
KEY_NAMES = {code: name for name, code in KEYCODES.items()}


def dry_record(entry):
    dry_log.append(entry)
    print("DRY", json.dumps(entry), flush=True)


def dry_record_event(ev):
    kind = Quartz.CGEventGetType(ev)
    e = {"ev": EVENT_NAMES.get(kind, kind)}
    field = lambda f: Quartz.CGEventGetIntegerValueField(ev, f)
    if kind == Quartz.kCGEventScrollWheel:
        e.update(dy=field(Quartz.kCGScrollWheelEventPointDeltaAxis1),
                 dx=field(Quartz.kCGScrollWheelEventPointDeltaAxis2))
    elif kind in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp, Quartz.kCGEventFlagsChanged):
        flags = Quartz.CGEventGetFlags(ev)
        code = field(Quartz.kCGKeyboardEventKeycode)
        e.update(key=KEY_NAMES.get(code, code),
                 mods=[m for m in ("fn", "control", "option", "command", "shift")
                       if flags & MOD_FLAGS[m]])
    else:
        loc = Quartz.CGEventGetLocation(ev)
        mac.dry_pos = (loc.x, loc.y)
        e.update(x=round(loc.x, 2), y=round(loc.y, 2),
                 clicks=field(Quartz.kCGMouseEventClickState),
                 dx=field(Quartz.kCGMouseEventDeltaX), dy=field(Quartz.kCGMouseEventDeltaY))
    dry_record(e)


def num(v):
    v = float(v)
    if not math.isfinite(v):
        raise ValueError("not a finite number")
    return v


async def dry_log_handler(request):
    check_token(request)
    return web.json_response(dry_log[int(request.query.get("since", 0)):])


async def dry_drop_handler(request):
    """Close every pad socket from the server side (simulates a dropped connection)."""
    check_token(request)
    for ws in list(request.app["clients"]):
        await ws.close()
    return web.json_response({"dropped": True})


def check_token(request):
    if request.match_info["token"] != request.app["token"]:
        raise web.HTTPNotFound()


def is_local(request):
    return request.remote in ("127.0.0.1", "::1")


def pad_url(app):
    return f"http://{lan_ip()}:{PORT}/{app['token']}"


def secure_url(app):
    return f"http://{lan_ip()}:{PORT}/{app['token']}/secure"


async def connect_page(request):
    """The Mac-side screen: a QR code for the iPad, live status and permissions."""
    check_token(request)
    url = pad_url(request.app)
    try:
        import segno
        qr = segno.make(url, error="m").svg_inline(scale=6, border=1, dark="#000", light="#fff")
    except Exception:
        qr = ""
    html = (CONNECT.replace("__QR__", qr).replace("__URL__", url)
            .replace("__MAC__", MAC_NAME or "this Mac"))
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def state(request):
    check_token(request)
    return web.json_response({
        "clients": len(request.app["clients"]),
        "url": pad_url(request.app) if is_local(request) else "",
        "tls": request.app["tls"], "speech": bool(TRANSCRIBE),
        "secure": any(getattr(c, "secure", False) for c in request.app["clients"]),
        "peers": [p["name"] for p in request.app["peers"].values()],
        "access": bool(getattr(Quartz, "CGPreflightPostEventAccess", lambda: True)()),
        "screen": bool(getattr(Quartz, "CGPreflightScreenCaptureAccess", lambda: True)()),
    })


CORS = {"Access-Control-Allow-Origin": "*"}


async def ping(request):
    """Who this is and where it can be reached. The pad uses it to find its Macs."""
    check_token(request)
    return web.json_response(info(request.app), headers=CORS)


# ---- this Mac, and the other Macs it is paired with
def mac_id():
    f = HOME / "id"
    if not f.exists():
        HOME.mkdir(parents=True, exist_ok=True)
        f.write_text(secrets.token_hex(6))
    return f.read_text().strip()


def tailscale_names():
    """This Mac's Tailscale name and address, when Tailscale is running."""
    exe = shutil.which("tailscale") or next((p for p in (
        "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale") if os.path.exists(p)), None)
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "status", "--json", "--peers=false"], capture_output=True,
                             text=True, timeout=4).stdout
        me = (json.loads(out).get("Self") or {}) if out.strip() else {}
        names = [me.get("DNSName", "").rstrip(".")] + [a for a in me.get("TailscaleIPs") or [] if ":" not in a]
        return [n for n in names if n]
    except Exception:
        return []


def find_hosts():
    """Every name and address the iPad might reach this Mac at, steadiest first."""
    if os.environ.get("PENCILPAD_HOSTS"):          # set by hand, e.g. for an unusual network
        return [h.strip().lower() for h in os.environ["PENCILPAD_HOSTS"].split(",") if h.strip()]
    hosts = [local_hostname(), *tailscale_names(), lan_ip()]
    seen = []
    for h in hosts:
        h = (h or "").lower()
        if h and h != "127.0.0.1" and h not in seen:
            seen.append(h)
    return seen


def info(app):
    return {"id": app["id"], "name": MAC_NAME, "hosts": app["hosts"], "port": PORT,
            "tls": app["tls"], "boot": BOOT_ID, "speech": bool(TRANSCRIBE)}


def clean_peer(d):
    """A peer record from the network: keep only well-formed fields."""
    peer = {"id": str(d["id"])[:40], "name": str(d.get("name") or "Mac")[:80],
            "hosts": [str(h)[:255] for h in d.get("hosts") or [] if isinstance(h, str)][:12],
            "port": int(d["port"]), "tls": int(d.get("tls") or 0)}
    if not peer["id"] or not peer["hosts"] or not 0 < peer["port"] < 65536:
        raise ValueError("not a PencilPad Mac")
    return peer


def load_peers():
    try:
        raw = json.loads((HOME / "macs.json").read_text())
        return {p["id"]: p for p in map(clean_peer, raw.values())}
    except Exception:
        return {}


def save_peers(peers):
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "macs.json").write_text(json.dumps(peers, indent=1))


async def macs(request):
    check_token(request)
    return web.json_response({"self": info(request.app), "peers": list(request.app["peers"].values())},
                             headers=CORS)


async def add_peer(request):
    """Another Mac with the same link introduces itself; reply with who we are."""
    check_token(request)
    try:
        peer = clean_peer(await request.json())
    except Exception:
        raise web.HTTPBadRequest()
    app = request.app
    if peer["id"] != app["id"] and app["peers"].get(peer["id"]) != peer:
        app["peers"][peer["id"]] = peer
        save_peers(app["peers"])
    return web.json_response(info(app))


async def keep_current(app):
    """Notices when this Mac's addresses change, and keeps in touch with paired Macs."""
    loop = asyncio.get_running_loop()
    timeout = ClientTimeout(total=3)
    while True:
        await asyncio.sleep(float(os.environ.get("PENCILPAD_REFRESH", 20)))
        try:
            hosts = await loop.run_in_executor(None, find_hosts)
            if hosts and hosts != app["hosts"]:
                app["hosts"] = hosts
                if app["tls_ctx"]:
                    await loop.run_in_executor(None, issue_cert, hosts)
                    app["tls_ctx"].load_cert_chain(HOME / "cert.pem", HOME / "key.pem")
            me = info(app)
            async with ClientSession(timeout=timeout) as session:
                for peer in list(app["peers"].values()):
                    for host in peer["hosts"]:
                        try:
                            url = f"http://{host}:{peer['port']}/{app['token']}/peer"
                            async with session.post(url, json=me) as r:
                                new = clean_peer(await r.json())
                        except Exception:
                            continue
                        if new["id"] == peer["id"] and new != peer:
                            app["peers"][peer["id"]] = new
                            save_peers(app["peers"])
                        break
        except Exception as e:
            print("refresh failed:", repr(e), flush=True)


def pair(link):
    """./run.sh --pair <link from the other Mac>: share its link and introduce the two."""
    u = urlsplit(link.strip())
    token = u.path.strip("/").split("/")[0]
    if u.scheme not in ("http", "https") or not u.netloc or not token:
        sys.exit("That is not a PencilPad link. Use Copy iPad Link on the other Mac.")
    base = f"{u.scheme}://{u.netloc}/{token}"
    ctx = ssl._create_unverified_context()     # the other Mac's own certificate; the link is the secret
    me = {"id": mac_id(), "name": MAC_NAME, "hosts": find_hosts(), "port": PORT, "tls": TLS_PORT}
    try:
        req = urllib.request.Request(base + "/peer", data=json.dumps(me).encode(),
                                     headers={"Content-Type": "application/json"})
        other = clean_peer(json.loads(urllib.request.urlopen(req, timeout=6, context=ctx).read()))
    except Exception as e:
        sys.exit(f"Could not reach PencilPad on the other Mac ({e}).\n"
                 "Check that it is running there and that both Macs are on the same network.")
    if other["id"] == me["id"]:
        sys.exit("That link is this Mac's own.")
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    peers = load_peers()
    peers[other["id"]] = other
    save_peers(peers)
    print(f"Paired with {other['name']}. Restart PencilPad on this Mac to use the shared link.")


# ---- the secure port. The iPad only gives a page the microphone, and lets it talk to
# more than one Mac, over HTTPS. Each Mac makes its own small certificate authority,
# which the iPad is asked to trust once. It can only vouch for names on private networks.
def tls_names(hosts):
    from cryptography import x509
    names = []
    for h in [*hosts, "localhost", "127.0.0.1"]:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            names.append(x509.DNSName(h))
    return names


def make_ca():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    HOME.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"PencilPad ({MAC_NAME or 'Mac'})"[:64]),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PencilPad")])
    now = datetime.datetime.now(datetime.timezone.utc)
    allowed = [x509.DNSName("local"), x509.DNSName("ts.net"), x509.DNSName("localhost")] + [
        x509.IPAddress(ipaddress.ip_network(n)) for n in
        ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8")]
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.NameConstraints(permitted_subtrees=allowed, excluded_subtrees=None),
                           critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    (HOME / "ca.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (HOME / "ca.key").chmod(0o600)
    (HOME / "ca.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def issue_cert(hosts):
    """Writes cert.pem and key.pem for these names, signed by this Mac's authority."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    if not (HOME / "ca.pem").exists() or not (HOME / "ca.key").exists():
        make_ca()
    ca = x509.load_pem_x509_certificate((HOME / "ca.pem").read_bytes())
    ca_key = serialization.load_pem_private_key((HOME / "ca.key").read_bytes(), None)
    names = tls_names(hosts)
    now = datetime.datetime.now(datetime.timezone.utc)
    try:                                       # keep the one we have if it still fits
        old = x509.load_pem_x509_certificate((HOME / "cert.pem").read_bytes())
        san = old.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        if (set(names) <= set(san) and old.issuer == ca.subject and (HOME / "key.pem").exists()
                and old.not_valid_after_utc - now > datetime.timedelta(days=30)):
            return
    except Exception:
        pass
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PencilPad")]))
            .issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=390))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=False, crl_sign=False,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    (HOME / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (HOME / "key.pem").chmod(0o600)
    (HOME / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def tls_context(hosts):
    """The HTTPS context, or None when it is switched off or cannot be set up."""
    if not TLS_PORT:
        return None
    try:
        issue_cert(hosts)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(HOME / "cert.pem", HOME / "key.pem")
        return ctx
    except ImportError:
        print("(No secure port: the 'cryptography' package is missing. Run ./run.sh to install it.)\n")
    except Exception as e:
        print(f"(No secure port: {e!r})\n")
    return None


async def ca_cert(request):
    """This Mac's certificate authority, for the iPad to install and trust."""
    check_token(request)
    try:
        pem = (HOME / "ca.pem").read_bytes()
    except OSError:
        raise web.HTTPNotFound()
    der = ssl.PEM_cert_to_DER_cert(pem.decode())
    return web.Response(body=der, content_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="PencilPad.crt"'})


async def secure_page(request):
    check_token(request)
    html = SECURE.replace("__MACNAME__", MAC_NAME or "your Mac").replace("__CFG__", json.dumps(
        {"tls": request.app["tls"], "mac": MAC_NAME or "Mac"}))
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def service_worker(request):
    check_token(request)
    return web.Response(text=SERVICE_WORKER, content_type="application/javascript",
                        headers={"Cache-Control": "no-store", "Service-Worker-Allowed": "/"})


# ---- Dictate with the iPad's microphone: the pad sends what it recorded, the Mac
# turns it into text and types it.
async def speech(request):
    check_token(request)
    if not TRANSCRIBE:
        return web.json_response({"text": "", "error": "unavailable"}, headers=CORS)
    try:
        rate = int(request.query.get("rate", 0))
    except ValueError:
        rate = 0
    pcm = await request.read()
    if not 8000 <= rate <= 96000 or len(pcm) < 2:
        raise web.HTTPBadRequest()
    pcm = pcm[:len(pcm) & ~1]
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
                + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
                + b"data" + struct.pack("<I", len(pcm)) + pcm)
    text, error = "", ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *TRANSCRIBE, f.name, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), 45)
        except asyncio.TimeoutError:
            proc.kill()
            out, err, error = b"", b"", "timeout"
        text = " ".join(out.decode("utf-8", "replace").split())
        if proc.returncode == 3:
            error = "denied"
        elif proc.returncode and not error:
            error = "failed"
            print("transcribe failed:", err.decode("utf-8", "replace").strip()[:300], flush=True)
    except OSError as e:
        error = "failed"
        print("transcribe could not start:", repr(e), flush=True)
    finally:
        os.unlink(f.name)
    if text:
        mac.type_text(text + " ")
    return web.json_response({"text": text, "error": error}, headers=CORS)


SETTINGS_PANES = {"accessibility": "Privacy_Accessibility", "screen": "Privacy_ScreenCapture"}


async def open_settings(request):
    check_token(request)
    pane = SETTINGS_PANES.get(request.match_info["pane"])
    if not pane or not is_local(request):      # only from this Mac's own browser
        raise web.HTTPNotFound()
    subprocess.Popen(["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"])
    return web.Response(text="ok")


async def exit_with_parent(app):
    """Started by the menu bar app: stop when that app goes away, however it went."""
    parent = os.getppid()

    async def watch():
        while os.getppid() == parent:
            await asyncio.sleep(2)
        mac.release_all()
        os._exit(0)

    app["parent_watch"] = asyncio.ensure_future(watch())


async def serve(app, sock, tls_sock):
    runner = web.AppRunner(app, handle_signals=False)
    await runner.setup()
    await web.SockSite(runner, sock).start()
    if tls_sock:
        await web.SockSite(runner, tls_sock, ssl_context=app["tls_ctx"]).start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    refresh = asyncio.ensure_future(keep_current(app))
    await stop.wait()
    refresh.cancel()
    await runner.cleanup()
    mac.release_all()


def bind(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    return sock


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--pair":
        return pair(sys.argv[2])
    if (DICTATION["mode"] not in ("hold", "toggle") or DICTATION["key"] not in KEYCODES
            or any(m not in MOD_FLAGS for m in DICTATION.get("mods", []))):
        sys.exit(f"The DICTATION setting is not valid: {DICTATION}\n"
                 f"mode: hold or toggle; key: one of {', '.join(KEYCODES)}; "
                 f"mods: any of {', '.join(MOD_FLAGS)}")
    trusted = getattr(Quartz, "CGPreflightPostEventAccess", lambda: True)()
    if not trusted and not DRY_RUN:
        getattr(Quartz, "CGRequestPostEventAccess", lambda: None)()
        print("!! PencilPad does not have Accessibility permission yet.\n"
              "   Turn it on in System Settings > Privacy & Security > Accessibility\n"
              "   (for the PencilPad app, or for the terminal that runs this script),\n"
              "   then start PencilPad again.\n"
              "   (The pad will connect, but the pointer will not move until then.)\n")
    try:
        sock = bind(PORT)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            sys.exit(f"Port {PORT} is already in use. PencilPad is probably already running.\n"
                     f"Stop the other copy, or pick another port: PENCILPAD_PORT=8766 ./run.sh")
        raise
    token = load_token()
    app = web.Application(client_max_size=48 * 1024 * 1024)
    app["token"] = token
    app["clients"] = set()
    app["id"] = mac_id()
    app["hosts"] = find_hosts()
    app["peers"] = {i: p for i, p in load_peers().items() if i != app["id"]}
    app["tls_ctx"] = tls_context(app["hosts"])
    tls_sock = None
    if app["tls_ctx"]:
        try:
            tls_sock = bind(TLS_PORT)
        except OSError:
            print(f"(No secure port: {TLS_PORT} is already in use.)\n")
            app["tls_ctx"] = None
    app["tls"] = TLS_PORT if tls_sock else 0
    app.add_routes([
        web.get("/{token}", page), web.get("/{token}/ws", ws_handler),
        web.get("/{token}/connect", connect_page), web.get("/{token}/state", state),
        web.get("/{token}/ping", ping), web.post("/{token}/settings/{pane}", open_settings),
        web.get("/{token}/macs", macs), web.post("/{token}/peer", add_peer),
        web.get("/{token}/ca.crt", ca_cert), web.get("/{token}/secure", secure_page),
        web.get("/{token}/sw.js", service_worker), web.post("/{token}/speech", speech),
    ])
    if DRY_RUN:
        app.add_routes([web.get("/{token}/dry", dry_log_handler),
                        web.post("/{token}/dry/drop", dry_drop_handler)])
    if os.environ.get("PENCILPAD_EXIT_WITH_PARENT"):
        app.on_startup.append(exit_with_parent)
    setup = f"http://localhost:{PORT}/{token}/connect"
    print("PencilPad is running" + (" (dry run: nothing is sent to this Mac)" if DRY_RUN else "") + ".\n")
    print(f"   Set up your iPad (QR code):  {setup}")
    print(f"   Or open on the iPad:         {pad_url(app)}\n")
    print("Ctrl+C here to stop.\n", flush=True)
    if sys.stdout.isatty() and not DRY_RUN and not os.environ.get("PENCILPAD_NO_OPEN"):
        subprocess.Popen(["open", setup])      # started by hand: show the setup screen
    asyncio.run(serve(app, sock, tls_sock))


CONNECT = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PencilPad</title>
<style>
  :root { color-scheme:light dark;
    --bg:#f2f2f7; --card:#fff; --label:#000; --label2:rgba(60,60,67,.6); --sep:rgba(60,60,67,.14);
    --blue:#007aff; --green:#34c759; --orange:#ff9500; --fill:rgba(118,118,128,.12); }
  @media (prefers-color-scheme: dark) { :root {
    --bg:#000; --card:#1c1c1e; --label:#fff; --label2:rgba(235,235,245,.6); --sep:rgba(84,84,88,.5);
    --blue:#0a84ff; --green:#30d158; --orange:#ff9f0a; --fill:rgba(118,118,128,.24); } }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; background:var(--bg);
    color:var(--label); font:400 17px/1.35 -apple-system, "SF Pro Text", system-ui, sans-serif; -webkit-font-smoothing:antialiased; }
  main { width:min(760px, calc(100vw - 48px)); padding:40px 0; }
  header { text-align:center; margin-bottom:28px; }
  h1 { margin:0 0 6px; font-size:34px; font-weight:700; letter-spacing:-.02em; }
  header p { margin:0; color:var(--label2); }
  .card { background:var(--card); border-radius:22px; margin-bottom:16px; overflow:hidden; }
  .pair { display:flex; gap:32px; align-items:center; padding:28px; }
  .qr { flex:none; padding:10px; background:#fff; border-radius:16px; line-height:0; box-shadow:0 0 0 1px var(--sep); }
  .qr svg { width:196px; height:196px; }
  ol { margin:0; padding:0; list-style:none; counter-reset:n; }
  ol li { position:relative; padding:0 0 16px 40px; counter-increment:n; }
  ol li:last-child { padding-bottom:0; }
  ol li::before { content:counter(n); position:absolute; left:0; top:-1px; width:26px; height:26px; border-radius:50%;
    background:var(--fill); font-size:15px; font-weight:600; display:flex; align-items:center; justify-content:center; }
  ol b { font-weight:600; } ol span { display:block; color:var(--label2); font-size:15px; }
  code { font:500 14px ui-monospace, "SF Mono", Menlo, monospace; user-select:all; -webkit-user-select:all; word-break:break-all; }
  .row { display:flex; align-items:center; gap:12px; min-height:56px; padding:8px 20px; box-sizing:border-box; }
  .row + .row { border-top:1px solid var(--sep); }
  .row .t { flex:1; } .row .t small { display:block; color:var(--label2); font-size:13px; }
  .dot { flex:none; width:11px; height:11px; border-radius:50%; background:var(--orange); }
  .dot.ok { background:var(--green); } .dot.wait { animation:pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity:.3; } }
  button { height:34px; padding:0 16px; border:0; border-radius:17px; background:var(--fill); color:var(--blue);
    font:600 15px -apple-system, system-ui, sans-serif; cursor:pointer; }
  button:active { opacity:.6; } [hidden] { display:none !important; }
  footer { text-align:center; color:var(--label2); font-size:13px; margin-top:20px; }
  @media (max-width:640px) { .pair { flex-direction:column; } }
</style>
</head>
<body>
<main>
  <header><h1>PencilPad</h1><p>Use your iPad and Apple Pencil as a trackpad for __MAC__.</p></header>
  <div class="card pair">
    <div class="qr">__QR__</div>
    <ol>
      <li><b>Point your iPad's camera at the code</b><span>Tap the link that appears. The iPad must be on the same Wi-Fi as this Mac.</span></li>
      <li><b>Follow the short tour on the iPad</b><span>It teaches each gesture in about a minute.</span></li>
      <li><b>Add it to your Home Screen</b><span>In Safari, tap Share, then Add to Home Screen, for a full-screen pad.</span></li>
    </ol>
  </div>
  <div class="card">
    <div class="row"><i class="dot wait" id="dIpad"></i><div class="t"><span id="tIpad">Waiting for your iPad…</span><small>No camera handy? Type this into Safari on the iPad: <code>__URL__</code></small></div></div>
    <div class="row"><i class="dot" id="dAcc"></i><div class="t"><span id="tAcc">Checking Accessibility…</span><small>Needed to move the pointer and press keys.</small></div><button id="bAcc" hidden>Open Settings</button></div>
    <div class="row"><i class="dot" id="dScr"></i><div class="t"><span id="tScr">Checking Screen Recording…</span><small>Only needed for the Screenshot corner.</small></div><button id="bScr" hidden>Open Settings</button></div>
    <div class="row" id="rSec" hidden><i class="dot" id="dSec"></i><div class="t"><span id="tSec">Secure connection</span><small>Lets Dictate use the iPad's microphone and lets the pad find your other Macs. On the iPad, tap the Mac's name at the top of the pad, then Set Up.</small></div></div>
    <div class="row" id="rPeers" hidden><i class="dot ok"></i><div class="t"><span id="tPeers"></span><small>The pad connects to whichever of these it can reach, starting with the one you used last.</small></div></div>
  </div>
  <footer>PencilPad keeps running in the terminal. Anyone on your network with this link can control this Mac, so keep it private.</footer>
</main>
<script>
const base = location.pathname.replace(/\/connect$/, '');
function row(id, ok, yes, no) {
  document.getElementById('d' + id).className = 'dot' + (ok ? ' ok' : id === 'Ipad' ? ' wait' : '');
  document.getElementById('t' + id).textContent = ok ? yes : no;
  const b = document.getElementById('b' + id); if (b) b.hidden = ok;
}
async function poll() {
  try {
    const s = await (await fetch(base + '/state', {cache: 'no-store'})).json();
    row('Ipad', s.clients > 0, 'iPad connected', 'Waiting for your iPad…');
    row('Acc', s.access, 'Accessibility is allowed', 'Accessibility is off: the pointer will not move');
    row('Scr', s.screen, 'Screen Recording is allowed', 'Screen Recording is off');
    document.getElementById('rSec').hidden = !s.tls;
    row('Sec', s.secure, 'iPad connected securely', 'Secure connection not set up on the iPad yet');
    document.getElementById('rPeers').hidden = !s.peers.length;
    document.getElementById('tPeers').textContent = 'Paired with ' + s.peers.join(', ');
  } catch (_) { row('Ipad', false, '', 'PencilPad is not running on this Mac'); }
  setTimeout(poll, 1500);
}
poll();
document.getElementById('bAcc').onclick = () => fetch(base + '/settings/accessibility', {method: 'POST'});
document.getElementById('bScr').onclick = () => fetch(base + '/settings/screen', {method: 'POST'});
</script>
</body>
</html>
"""

SECURE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Set Up PencilPad</title>
<style>
  :root { color-scheme:light dark;
    --bg:#f2f2f7; --card:#fff; --label:#000; --label2:rgba(60,60,67,.6); --sep:rgba(60,60,67,.14);
    --blue:#007aff; --green:#34c759; --fill:rgba(118,118,128,.12); }
  @media (prefers-color-scheme: dark) { :root {
    --bg:#000; --card:#1c1c1e; --label:#fff; --label2:rgba(235,235,245,.6); --sep:rgba(84,84,88,.5);
    --blue:#0a84ff; --green:#30d158; --fill:rgba(118,118,128,.24); } }
  body { margin:0; background:var(--bg); color:var(--label);
    font:400 17px/1.35 -apple-system, "SF Pro Text", system-ui, sans-serif; -webkit-font-smoothing:antialiased; }
  main { width:min(620px, calc(100vw - 40px)); margin:0 auto; padding:44px 0; }
  h1 { margin:0 0 8px; font-size:30px; font-weight:700; letter-spacing:-.02em; text-align:center; }
  header p { margin:0 0 26px; color:var(--label2); text-align:center; }
  .card { background:var(--card); border-radius:22px; padding:24px; margin-bottom:16px; }
  ol { margin:0; padding:0; list-style:none; counter-reset:n; }
  ol li { position:relative; padding:0 0 20px 42px; counter-increment:n; }
  ol li:last-child { padding-bottom:0; }
  ol li::before { content:counter(n); position:absolute; left:0; top:-1px; width:28px; height:28px; border-radius:50%;
    background:var(--fill); font-size:15px; font-weight:600; display:flex; align-items:center; justify-content:center; }
  ol b { font-weight:600; } ol span { display:block; color:var(--label2); font-size:15px; margin-top:2px; }
  a.btn { display:flex; align-items:center; justify-content:center; height:50px; margin-top:12px; border-radius:14px;
    background:var(--blue); color:#fff; font-weight:600; text-decoration:none; }
  a.btn:active { opacity:.7; }
  #wait { display:flex; align-items:center; gap:10px; color:var(--label2); font-size:15px; }
  #wait i { width:10px; height:10px; border-radius:50%; background:var(--blue); animation:pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity:.3; } }
  #done h2 { margin:0 0 6px; font-size:22px; color:var(--green); }
  #done p { margin:0; color:var(--label2); }
  footer { color:var(--label2); font-size:13px; text-align:center; padding:0 12px; }
  [hidden] { display:none !important; }
</style>
</head>
<body>
<main>
  <header><h1>Set Up the Secure Connection</h1>
    <p>It lets Dictate use this iPad's microphone, and lets PencilPad find your Macs by itself.</p></header>
  <div class="card" id="steps">
    <ol>
      <li><b>Download the certificate</b><span>Tap the button, then tap Allow.</span>
        <a class="btn" id="get" href="#">Download Certificate</a></li>
      <li><b>Install it</b><span>Open the Settings app. Tap Profile Downloaded near the top, then Install.</span></li>
      <li><b>Trust it</b><span>In Settings, go to General, About, Certificate Trust Settings and turn on <span id="ca" style="display:inline;color:var(--label)"></span>.</span></li>
      <li><b>Come back to this page</b><span>It moves on by itself once the iPad trusts the certificate.</span></li>
    </ol>
  </div>
  <div class="card" id="wait"><i></i><span>Waiting for the certificate to be trusted…</span></div>
  <div class="card" id="done" hidden><h2>All set</h2>
    <p>Open PencilPad below, then tap Share and Add to Home Screen. If you already have a PencilPad icon, delete the old one first.</p>
    <a class="btn" id="open" href="#">Open PencilPad</a></div>
  <div class="card" id="off" hidden><p style="margin:0;color:var(--label2)">The secure connection is switched off on this Mac. Start PencilPad again with ./run.sh.</p></div>
  <footer>The certificate was made by PencilPad on __MACNAME__, and its private key never leaves that Mac. It can only vouch for addresses on your own networks, not for websites.</footer>
</main>
<script>
const CFG = __CFG__;
const path = location.pathname.replace(/\/secure$/, '');
document.getElementById('ca').textContent = 'PencilPad (' + CFG.mac + ')';
document.getElementById('get').href = path + '/ca.crt';
const pad = 'https://' + location.hostname + ':' + CFG.tls + path;
document.getElementById('open').href = pad;
function done() {
  for (const id of ['steps', 'wait']) document.getElementById(id).hidden = true;
  document.getElementById('done').hidden = false;
}
async function check() {
  try { if ((await fetch(pad + '/ping', {cache: 'no-store'})).ok) return done(); } catch (_) {}
  setTimeout(check, 1500);
}
if (!CFG.tls) { for (const id of ['steps', 'wait']) document.getElementById(id).hidden = true; document.getElementById('off').hidden = false; }
else if (location.protocol === 'https:') done(); else check();
document.addEventListener('visibilitychange', () => { if (!document.hidden && CFG.tls && location.protocol !== 'https:') check(); });
</script>
</body>
</html>
"""

# Keeps a copy of the pad on the iPad, so the Home Screen app opens even when the Mac
# it was added from is asleep or on another network. The pad then finds a Mac itself.
SERVICE_WORKER = r"""
const PAD = self.registration.scope.replace(/\/$/, '');
const open = () => caches.open('pencilpad-1');
self.addEventListener('install', e => { self.skipWaiting(); e.waitUntil(open().then(c => c.add(PAD)).catch(() => {})); });
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET' || e.request.mode !== 'navigate') return;
  if (e.request.url.split(/[?#]/)[0].replace(/\/$/, '') !== PAD) return;
  e.respondWith((async () => {
    const cache = await open();
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 2500);
    try {
      const r = await fetch(PAD, {signal: ctl.signal, cache: 'no-store'});
      if (r.ok) await cache.put(PAD, r.clone());
      return r;
    } catch (_) {
      return (await cache.match(PAD)) || Response.error();
    } finally { clearTimeout(timer); }
  })());
});
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="PencilPad">
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="#f2f2f7" media="(prefers-color-scheme: light)">
<title>PencilPad</title>
<style>
  :root {
    color-scheme: dark;
    --bg:#000; --surface:#1c1c1e; --raised:rgba(118,118,128,.24); --raised-edge:rgba(255,255,255,.10);
    --label:#fff; --label2:rgba(235,235,245,.6); --label3:rgba(235,235,245,.3);
    --blue:#0a84ff; --red:#ff453a; --green:#30d158; --orange:#ff9f0a;
    --armed:rgba(10,132,255,.14); --material:rgba(44,44,46,.72);
    --gap:12px;
    --l:calc(env(safe-area-inset-left) + var(--gap)); --r:calc(env(safe-area-inset-right) + var(--gap));
    --t:calc(env(safe-area-inset-top) + var(--gap)); --b:calc(env(safe-area-inset-bottom) + var(--gap));
  }
  @media (prefers-color-scheme: light) {
    :root { color-scheme: light;
      --bg:#f2f2f7; --surface:#fff; --raised:rgba(118,118,128,.12); --raised-edge:rgba(0,0,0,.06);
      --label:#000; --label2:rgba(60,60,67,.6); --label3:rgba(60,60,67,.3);
      --blue:#007aff; --red:#ff3b30; --green:#34c759; --orange:#ff9500;
      --armed:rgba(0,122,255,.10); --material:rgba(255,255,255,.78); }
  }
  html, body { margin:0; height:100%; background:var(--bg); color:var(--label); overflow:hidden;
    font:400 17px/1.3 -apple-system, "SF Pro Text", system-ui, sans-serif; -webkit-font-smoothing:antialiased;
    -webkit-user-select:none; user-select:none; -webkit-touch-callout:none;
    -webkit-tap-highlight-color:transparent; touch-action:none; overscroll-behavior:none; }

  /* the pad takes every touch on the screen; the surface is only what you see */
  #pad { position:fixed; inset:0; touch-action:none; }
  #surface { position:fixed; left:var(--l); right:var(--r); top:var(--t); bottom:var(--b);
    background:var(--surface); border-radius:36px; pointer-events:none;
    box-shadow:inset 0 0 0 1px var(--raised-edge);
    transition:background-color .18s ease, box-shadow .18s ease, opacity .25s ease; }
  #pad.armed + #surface { background:color-mix(in srgb, var(--surface), var(--blue) 16%);
    box-shadow:inset 0 0 0 2.5px var(--blue); }
  body.offline #surface { opacity:.45; }

  /* corner controls */
  .corner { position:fixed; width:clamp(148px, 23vw, 300px); height:clamp(112px, 20vh, 210px);
    display:flex; flex-direction:column; align-items:center; justify-content:center; gap:10px;
    background:var(--raised); color:var(--label); border-radius:26px; box-sizing:border-box;
    box-shadow:inset 0 0 0 1px var(--raised-edge); touch-action:none;
    font-size:17px; font-weight:600; letter-spacing:-.01em;
    transition:background-color .15s ease, color .15s ease, transform .15s ease, opacity .25s ease; }
  .corner svg { width:34px; height:34px; fill:none; stroke:currentColor; stroke-width:2;
    stroke-linecap:round; stroke-linejoin:round; }
  .corner small { font-size:13px; font-weight:400; color:var(--label2); margin-top:-6px; transition:color .15s ease; }
  .corner.on { background:var(--blue); color:#fff; transform:scale(.97); }
  .corner.on small { color:rgba(255,255,255,.8); }
  #dict.on { background:var(--red); }
  body.offline .corner { opacity:.35; }
  #esc  { top:calc(var(--t) + var(--gap));    left:calc(var(--l) + var(--gap)); }
  #shot { top:calc(var(--t) + var(--gap));    right:calc(var(--r) + var(--gap)); }
  #dict { bottom:calc(var(--b) + var(--gap)); left:calc(var(--l) + var(--gap)); }
  #sub  { bottom:calc(var(--b) + var(--gap)); right:calc(var(--r) + var(--gap)); }

  /* connection status */
  #status { display:flex; align-items:center; gap:8px; height:36px; padding:0 16px 0 13px; border-radius:18px;
    background:var(--material); -webkit-backdrop-filter:blur(24px) saturate(180%); backdrop-filter:blur(24px) saturate(180%);
    box-shadow:inset 0 0 0 1px var(--raised-edge);
    font-size:15px; font-weight:500; color:var(--label); white-space:nowrap; pointer-events:auto; touch-action:none; }
  #dot { width:9px; height:9px; border-radius:50%; background:var(--green); transition:background-color .2s ease; }
  body.offline #dot { background:var(--orange); animation:pulse 1.2s ease-in-out infinite; }
  #pad.armed ~ #status #dot { background:var(--blue); }
  @keyframes pulse { 50% { opacity:.3; } }

  /* gesture guide */
  #hint { position:fixed; left:50%; top:50%; transform:translate(-50%,-50%);
    display:grid; grid-template-columns:auto auto; column-gap:18px; row-gap:9px; align-items:baseline;
    font-size:15px; pointer-events:none; transition:opacity .25s ease; }
  #hint b { font-weight:600; color:var(--label2); text-align:right; }
  #hint span { color:var(--label3); }
  body.busy #hint, body.offline #hint { opacity:0; }

  /* shown while the Mac cannot be reached */
  #lost { position:fixed; left:50%; top:50%; transform:translate(-50%,-50%); width:min(340px, 80vw);
    padding:26px 24px 22px; border-radius:26px; text-align:center;
    background:var(--material); -webkit-backdrop-filter:blur(30px) saturate(180%); backdrop-filter:blur(30px) saturate(180%);
    box-shadow:inset 0 0 0 1px var(--raised-edge), 0 18px 50px rgba(0,0,0,.28);
    opacity:0; visibility:hidden; pointer-events:none; transition:opacity .25s ease, visibility 0s linear .25s; }
  body.offline #lost { opacity:1; visibility:visible; pointer-events:auto; transition:opacity .25s ease .9s, visibility 0s linear .9s; }
  #lost h1 { margin:14px 0 6px; font-size:20px; font-weight:600; letter-spacing:-.01em; }
  #lost p { margin:0 0 18px; font-size:15px; color:var(--label2); }
  #lost button { width:100%; height:50px; border:0; border-radius:14px; background:var(--blue); color:#fff;
    font:600 17px -apple-system, system-ui, sans-serif; }
  #lost button:active { opacity:.7; }
  #spin { width:28px; height:28px; margin:0 auto; border-radius:50%;
    border:3px solid var(--raised); border-top-color:var(--label2); animation:spin .9s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  /* top bar: status + help */
  #top { position:fixed; top:calc(var(--t) + 18px); left:50%; transform:translateX(-50%);
    display:flex; align-items:center; gap:8px; pointer-events:none; }
  #help { position:relative; width:36px; height:36px; border:0; border-radius:50%; padding:0; pointer-events:auto;
    background:var(--material); -webkit-backdrop-filter:blur(24px) saturate(180%); backdrop-filter:blur(24px) saturate(180%);
    box-shadow:inset 0 0 0 1px var(--raised-edge); color:var(--label);
    font:600 17px -apple-system, system-ui, sans-serif; touch-action:none; }
  #help::after { content:""; position:absolute; inset:-6px; }
  #help:active { opacity:.6; }

  /* the list of Macs, opened from the status pill */
  #macs { position:fixed; top:calc(var(--t) + 64px); left:50%; transform:translateX(-50%) scale(.96); width:min(360px, 84vw);
    padding:8px; border-radius:22px; box-sizing:border-box; touch-action:none;
    background:var(--material); -webkit-backdrop-filter:blur(30px) saturate(180%); backdrop-filter:blur(30px) saturate(180%);
    box-shadow:inset 0 0 0 1px var(--raised-edge), 0 18px 50px rgba(0,0,0,.28);
    opacity:0; visibility:hidden; transition:opacity .2s ease, transform .2s ease, visibility 0s linear .2s; }
  body.picking #macs { opacity:1; visibility:visible; transform:translateX(-50%); transition-delay:0s; }
  #macs .mac { display:flex; align-items:center; gap:12px; min-height:52px; padding:6px 14px; border-radius:14px; }
  #macs .mac:active { background:var(--raised); }
  #macs .mac b { flex:1; font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  #macs .mac span { font-size:15px; color:var(--label2); }
  #macs .mac.cur span { color:var(--green); }
  #macs .mac.gone b { color:var(--label2); }
  #macs .more { color:var(--blue); border-top:1px solid var(--raised-edge); border-radius:0 0 14px 14px; }
  #macs p { margin:6px 14px 8px; font-size:13px; color:var(--label2); }

  /* hold progress ring, drawn where the tip rests */
  #ring { position:fixed; width:76px; height:76px; margin:-38px 0 0 -38px; pointer-events:none;
    opacity:0; transform:rotate(-90deg); }
  #ring circle { fill:none; stroke:var(--blue); stroke-width:4; stroke-linecap:round;
    stroke-dasharray:214; stroke-dashoffset:214; }
  #ring.go { opacity:1; transition:opacity .1s linear .14s; }
  #ring.go circle { animation:ringfill var(--hold) linear .14s forwards; }
  #ring.full circle { stroke-dashoffset:0; animation:none; }
  @keyframes ringfill { to { stroke-dashoffset:0; } }

  /* guided tour */
  #tour { position:fixed; left:50%; top:50%; transform:translate(-50%,-50%) scale(.96); width:min(400px, 78vw);
    padding:22px 24px 16px; border-radius:28px; text-align:center; pointer-events:none;
    background:var(--material); -webkit-backdrop-filter:blur(30px) saturate(180%); backdrop-filter:blur(30px) saturate(180%);
    box-shadow:inset 0 0 0 1px var(--raised-edge), 0 18px 50px rgba(0,0,0,.28);
    opacity:0; visibility:hidden; transition:opacity .25s ease, transform .25s ease, visibility 0s linear .25s; }
  body.touring:not(.offline) #tour { opacity:1; visibility:visible; transform:translate(-50%,-50%); transition-delay:0s; }
  body.touring #hint { opacity:0; }
  #tour .step { font-size:13px; font-weight:600; color:var(--label2); text-transform:uppercase; letter-spacing:.04em; }
  #tour h1 { margin:14px 0 6px; font-size:22px; font-weight:700; letter-spacing:-.02em; }
  #tour p { margin:0 auto; max-width:320px; font-size:15px; color:var(--label2); min-height:40px; }
  #tour .note { margin-top:12px; font-size:13px; color:var(--label3); min-height:0; }
  #tour .row { display:flex; gap:10px; margin-top:14px; }
  #tour button { flex:1; height:44px; border:0; border-radius:12px; pointer-events:auto; touch-action:none;
    background:var(--raised); color:var(--blue); font:600 17px -apple-system, system-ui, sans-serif; }
  #tour button.primary { background:var(--blue); color:#fff; }
  #tour button:active { opacity:.6; }
  #dots { display:flex; justify-content:center; gap:7px; margin-top:14px; }
  #dots i { width:7px; height:7px; border-radius:50%; background:var(--label3); transition:background-color .2s ease; }
  #dots i.cur { background:var(--label); } #dots i.ok { background:var(--green); }

  /* the small animated pad inside the tour card */
  #demo { position:relative; width:230px; height:138px; margin:12px auto 0; border-radius:20px; overflow:hidden;
    background:var(--raised); box-shadow:inset 0 0 0 1px var(--raised-edge); }
  #demo .f { position:absolute; left:0; top:0; width:26px; height:26px; margin:-13px 0 0 -13px; border-radius:50%;
    background:var(--blue); opacity:0; }
  #demo .k { position:absolute; width:46px; height:30px; border-radius:8px; background:var(--label3); opacity:0; }
  #demo .k:nth-of-type(1) { left:8px; top:8px; } #demo .k:nth-of-type(2) { right:8px; top:8px; }
  #demo .k:nth-of-type(3) { left:8px; bottom:8px; } #demo .k:nth-of-type(4) { right:8px; bottom:8px; }
  #demo svg { position:absolute; left:50%; top:50%; width:64px; height:64px; margin:-32px 0 0 -32px; opacity:0;
    fill:none; stroke:var(--blue); stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
  #demo .tick { stroke:var(--green); stroke-width:3; }
  #demo[data-s=move] .f1 { animation:d-move 2.6s ease-in-out infinite; }
  #demo[data-s=tap] .f1 { animation:d-tap 1.5s ease-out infinite; }
  #demo[data-s=hold] .f1 { animation:d-hold 2.8s ease-in-out infinite; }
  #demo[data-s=hold], #demo[data-s=drag] { animation:d-tint 2.8s ease-in-out infinite; }
  #demo[data-s=drag] .f1 { animation:d-drag 2.8s ease-in-out infinite; }
  #demo[data-s=scroll] .f1 { animation:d-scroll 2.2s ease-in-out infinite; }
  #demo[data-s=scroll] .f2 { animation:d-scroll2 2.2s ease-in-out infinite; }
  #demo[data-s=corners] .k { animation:d-key 3.2s linear infinite; opacity:.35; }
  #demo[data-s=corners] .k:nth-of-type(2) { animation-delay:.8s; }
  #demo[data-s=corners] .k:nth-of-type(4) { animation-delay:1.6s; }
  #demo[data-s=corners] .k:nth-of-type(3) { animation-delay:2.4s; }
  #demo[data-s=home] .share { opacity:1; }
  #demo.ok > * { animation:none !important; opacity:0 !important; }
  #demo.ok { animation:none !important; }
  #demo.ok .tick { opacity:1 !important; animation:d-pop .35s ease-out !important; }
  @keyframes d-move { 0% { transform:translate(50px,100px); opacity:0; } 12% { opacity:1; }
    50% { transform:translate(120px,40px); } 88% { opacity:1; } 100% { transform:translate(185px,88px); opacity:0; } }
  @keyframes d-tap { 0%,20% { transform:translate(115px,69px) scale(.4); opacity:0; }
    30% { transform:translate(115px,69px) scale(1); opacity:1; } 45%,100% { transform:translate(115px,69px) scale(1.6); opacity:0; } }
  @keyframes d-hold { 0%,8% { transform:translate(115px,69px) scale(.5); opacity:0; } 14%,72% { transform:translate(115px,69px); opacity:1; }
    82%,100% { transform:translate(115px,69px) scale(1.5); opacity:0; } }
  @keyframes d-drag { 0%,8% { transform:translate(60px,69px) scale(.5); opacity:0; } 14%,45% { transform:translate(60px,69px); opacity:1; }
    80% { transform:translate(175px,69px); opacity:1; } 92%,100% { transform:translate(175px,69px) scale(1.5); opacity:0; } }
  @keyframes d-tint { 0%,36% { background-color:var(--raised); } 46%,82% { background-color:color-mix(in srgb, var(--blue) 30%, transparent); }
    94%,100% { background-color:var(--raised); } }
  @keyframes d-scroll { 0% { transform:translate(98px,105px); opacity:0; } 15% { opacity:1; } 85% { opacity:1; } 100% { transform:translate(98px,35px); opacity:0; } }
  @keyframes d-scroll2 { 0% { transform:translate(134px,100px); opacity:0; } 15% { opacity:1; } 85% { opacity:1; } 100% { transform:translate(134px,30px); opacity:0; } }
  @keyframes d-key { 0%,6% { background:var(--blue); opacity:1; } 22%,100% { background:var(--label3); opacity:.35; } }
  @keyframes d-pop { from { transform:scale(.5); opacity:0; } to { transform:scale(1); opacity:1; } }
  @media (prefers-reduced-motion: reduce) {
    * { transition-duration:.01s !important; }
    #spin { animation-duration:2.4s; } body.offline #dot { animation:none; }
  }
</style>
</head>
<body class="offline">
<div id="pad"></div>
<div id="surface"></div>
<div id="hint">
  <b>Drag</b><span>Move the pointer</span>
  <b>Tap</b><span>Click</span>
  <b>Hold, then lift</b><span>Right-click</span>
  <b>Hold, then move</b><span>Drag</span>
  <b>Two fingers</b><span>Scroll, or tap to right-click</span>
</div>
<div id="top"><div id="status"><i id="dot"></i><span id="statusText">Connecting…</span></div><button id="help" aria-label="Show the tour">?</button></div>
<div class="corner" id="esc"><svg viewBox="0 0 34 34"><circle cx="19" cy="19" r="10.5"/><path d="M19 19 6 6M6 14V6h8"/></svg>Escape</div>
<div class="corner" id="shot"><svg viewBox="0 0 34 34"><path d="M5 11V8a3 3 0 0 1 3-3h3M23 5h3a3 3 0 0 1 3 3v3M29 23v3a3 3 0 0 1-3 3h-3M11 29H8a3 3 0 0 1-3-3v-3"/><circle cx="17" cy="17" r="5"/></svg>Screenshot</div>
<div class="corner" id="dict"><svg viewBox="0 0 34 34"><rect x="12.5" y="4" width="9" height="16" rx="4.5"/><path d="M7.5 16.5a9.5 9.5 0 0 0 19 0M17 26v4M12.5 30h9"/></svg>Dictate<small id="dictSub"></small></div>
<div class="corner" id="sub"><svg viewBox="0 0 34 34"><path d="M27 7v9a4 4 0 0 1-4 4H7M13 14l-6 6 6 6"/></svg>Return</div>
<div id="macs"><div id="macList"></div>
  <div class="mac more" id="secure" hidden><b>Set Up the Secure Connection…</b></div>
  <p id="macNote"></p></div>
<div id="lost"><div id="spin"></div><h1 id="lostTitle">Looking for your Mac…</h1>
  <p>Keep PencilPad running on the Mac and this iPad on the same Wi-Fi. It reconnects on its own.</p>
  <button id="reload">Reload</button></div>
<svg id="ring" viewBox="0 0 76 76"><circle cx="38" cy="38" r="34"/></svg>
<div id="tour">
  <div class="step" id="tourStep"></div>
  <div id="demo"><i class="k"></i><i class="k"></i><i class="k"></i><i class="k"></i><i class="f f1"></i><i class="f f2"></i>
    <svg class="share" viewBox="0 0 34 34"><path d="M11 13H9a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V15a2 2 0 0 0-2-2h-2M17 20V4M12 9l5-5 5 5"/></svg>
    <svg class="tick" viewBox="0 0 34 34"><path d="M8 18l6 6 12-14"/></svg></div>
  <h1 id="tourTitle"></h1><p id="tourText"></p><p class="note" id="tourNote"></p>
  <div id="dots"></div>
  <div class="row"><button id="tourSkip">Skip</button><button id="tourEnd">End Tour</button></div>
</div>
<script>
const CFG = __CFG__;
const pad = document.getElementById('pad');
const statusText = document.getElementById('statusText');
let online = false;
let cur = null;           // the Mac this pad is connected to: {id, name, base, speech}
function showStatus(armed) {
  const name = cur ? cur.name : CFG.mac;
  statusText.textContent = armed ? 'Lift to right-click · move to drag'
    : online ? (name ? 'Connected to ' + name : 'Connected') : 'Reconnecting…';
}
function setOnline(on) {
  online = on; document.body.classList.toggle('offline', !on); showStatus(false);
  const name = macs.length > 1 ? 'your Mac' : CFG.mac;
  if (!on && name) document.getElementById('lostTitle').textContent = 'Looking for ' + name + '…';
}
function arm(on) { pad.classList.toggle('armed', on); showStatus(on); }
document.getElementById('reload').addEventListener('click', () => location.reload());
const dictSub = document.getElementById('dictSub');
let dictNote = null;
function dictLabel(note, ms) {
  clearTimeout(dictNote);
  dictSub.textContent = note || (CFG.dictMode === 'toggle' ? 'Tap to start or stop' : 'Hold to talk');
  if (note && ms) dictNote = setTimeout(dictLabel, ms);
}
dictLabel();
// Tolerances. SLOP_*: how far (px, straight line from where it landed) the tip may
// wander and still count as a tap or a hold. HOLD_MS: hold-still time before the pad arms.
const HOLD_MS = 500, TWO_TAP_MS = 600, SLOP_PEN = 18, SLOP_TOUCH = 20, TWO_SLOP = 20;

// ---- the Macs this pad knows, and how to reach each one
function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (_) { return null; } }
const PATH = location.pathname.replace(/\/$/, '');
const SECURE = location.protocol === 'https:';
let macs = [];           // [{id, name, hosts, port, tls}], this page's own Mac first
function learn(list) {
  for (const m of list) {
    if (!m || !m.id || !Array.isArray(m.hosts)) continue;
    const rec = {id: m.id, name: m.name || 'Mac', hosts: m.hosts, port: m.port, tls: m.tls || 0};
    const i = macs.findIndex(x => x.id === m.id);
    if (i < 0) macs.push(rec); else macs[i] = rec;
  }
  store('pencilpad-macs', JSON.stringify(macs));
}
try { learn(JSON.parse(store('pencilpad-macs') || '[]')); } catch (_) {}
learn([CFG.self, ...CFG.peers]);
macs.sort((a, b) => (b.id === CFG.self.id) - (a.id === CFG.self.id));
function preferred() { const id = store('pencilpad-mac'); return macs.some(m => m.id === id) ? id : CFG.self.id; }
// every address worth trying for a Mac, the address this page came from first
function bases(m) {
  const out = m.id === CFG.self.id ? [location.origin] : [];
  for (const h of m.hosts) {
    const b = SECURE ? (m.tls ? 'https://' + h + ':' + m.tls : '') : 'http://' + h + ':' + m.port;
    if (b && !out.includes(b)) out.push(b);
  }
  return out;
}
async function probe(base, id, ms) {
  const ctl = new AbortController(); const timer = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(base + PATH + '/ping', {signal: ctl.signal, cache: 'no-store'});
    const who = r.ok ? await r.json() : null;
    return !!who && who.id === id;
  } catch (_) { return false; } finally { clearTimeout(timer); }
}
// The first reachable address, taking the Macs in the order given. Answers as soon
// as everything ahead of a reachable address has failed.
function find(order, ms) {
  const tries = [];
  for (const m of order) for (const base of bases(m)) tries.push({m, base, ok: null});
  return new Promise(done => {
    const settle = () => {
      for (const t of tries) { if (t.ok) return done(t); if (t.ok === null) return; }
      done(null);
    };
    if (!tries.length) return done(null);
    for (const t of tries) probe(t.base, t.m.id, ms).then(ok => { t.ok = ok; settle(); });
  });
}
function byPreference() { const id = preferred(); return [...macs].sort((a, b) => (b.id === id) - (a.id === id)); }

// ---- connection
let ws = null;
let retry = null;
let finding = false;
async function connect() {
  clearTimeout(retry);
  if (finding || (ws && ws.readyState <= 1)) return;          // already open or opening
  let to = {m: macs[0], base: location.origin};
  if (macs.length > 1 || connect.failed) {                      // more than one place to look
    finding = true;
    to = await find(byPreference(), 1500);
    finding = false;
    if (!to) { retry = setTimeout(connect, 700); return; }
    if (ws && ws.readyState <= 1) return;
  }
  const sock = ws = new WebSocket(to.base.replace(/^http/, 'ws') + PATH + '/ws');
  let opened = false;
  ws.onopen = () => {
    if (sock !== ws) return;
    opened = true; connect.failed = false;
    cur = {id: to.m.id, name: to.m.name, base: to.base, speech: false};
    setOnline(true); if (!tour && !store('pencilpad-tour')) tourStart();
  };
  ws.onclose = () => {
    if (sock !== ws) return;
    if (!opened) connect.failed = true;       // next time, try this Mac's other addresses too
    resetInput(); setOnline(false); retry = setTimeout(connect, 700);
  };
  ws.onerror = () => sock.close();
  ws.onmessage = m => {
    if (sock !== ws) return;
    let hello; try { hello = JSON.parse(m.data); } catch (_) { return; }
    if (!hello || !hello.boot) return;
    if (hello.id) {
      learn([hello, ...(hello.peers || [])]);
      if (cur) { cur.name = hello.name || cur.name; cur.speech = !!hello.speech; showStatus(false); }
      dictLabel();
    }
    // this page's own Mac restarted: load the page again, once, in case it changed
    if ((!hello.id || hello.id === CFG.self.id) && hello.boot !== CFG.boot && store('pencilpad-boot') !== hello.boot) {
      store('pencilpad-boot', hello.boot); location.reload();
    }
  };
}
function reconnect() { const old = ws; ws = null; cur = null; if (old) old.close(); resetInput(); setOnline(false); connect(); }
// back on screen while on the second-choice Mac: go back to the first choice if it is there
async function backToFirstChoice() {
  const first = macs.find(m => m.id === preferred());
  if (!cur || !first || cur.id === first.id || finding) return;
  if (await find([first], 1200) && cur && cur.id !== first.id) reconnect();
}
function send(o) {
  if (tour) { tourSaw(o); if (PRACTICE.has(o.t)) return true; }   // the tour only practises clicks and keys
  if (!ws || ws.readyState !== 1) return false;
  ws.send(JSON.stringify(o)); return true;
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { resetInput(); pickClose(); } else { connect(); backToFirstChoice(); }
});

// ---- the list of Macs
const macList = document.getElementById('macList');
function pickClose() { document.body.classList.remove('picking'); }
function pickDraw(reach) {
  macList.textContent = '';
  for (const m of macs) {
    const here = cur && cur.id === m.id, ok = here || reach[m.id];
    const row = document.createElement('div');
    row.className = 'mac' + (here ? ' cur' : ok === false ? ' gone' : '');
    const b = document.createElement('b'), st = document.createElement('span');
    b.textContent = m.name;
    st.textContent = here ? 'Connected' : ok ? 'Available' : ok === false ? 'Not reachable' : 'Looking…';
    row.append(b, st);
    row.addEventListener('pointerdown', e => {
      e.preventDefault(); e.stopPropagation();
      pickClose();
      if (here || ok === false) return;
      store('pencilpad-mac', m.id); reconnect();
    });
    macList.append(row);
  }
}
function pickOpen() {
  const reach = {};
  document.getElementById('secure').hidden = SECURE || !CFG.self.tls;
  document.getElementById('macNote').textContent = macs.length > 1
    ? 'PencilPad connects to the Mac you chose last, or to another one when that Mac is not reachable.'
    : SECURE ? 'To add another Mac, pair it with this one. See the README.' : '';
  pickDraw(reach);
  document.body.classList.add('picking');
  for (const m of macs) find([m], 1500).then(t => { reach[m.id] = !!t; if (document.body.classList.contains('picking')) pickDraw(reach); });
}

// ---- batched movement, one message per frame
let mvx = 0, mvy = 0, scx = 0, scy = 0, queued = false;
function flush() {
  queued = false;
  if (mvx || mvy) { send({t:'m', x:mvx, y:mvy}); mvx = mvy = 0; }
  if (scx || scy) { send({t:'s', x:scx, y:scy}); scx = scy = 0; }
}
function queue() { if (!queued) { queued = true; requestAnimationFrame(flush); } }

// ---- pad gestures
const pts = new Map();   // pointerId -> {x, y, x0, y0} for pointers in the current gesture
let g = null;            // the current gesture
let holdTimer = null;
let hover = null;

const cornerResets = [];
const ring = document.getElementById('ring');
document.documentElement.style.setProperty('--hold', (HOLD_MS - 140) + 'ms');
function ringOn(x, y) {
  ring.style.left = x + 'px'; ring.style.top = y + 'px';
  ring.classList.remove('go', 'full'); void ring.getBoundingClientRect(); ring.classList.add('go');
}
function ringOff() { ring.classList.remove('go', 'full'); }
// drop every gesture in progress and release anything held on the Mac
function resetInput() {
  cancelGesture();
  mvx = mvy = scx = scy = 0;
  document.body.classList.remove('busy');
  for (const r of cornerResets) r();
}
function penDown() { return g !== null && g.pen; }
function cancelGesture() { endDrag(); pts.clear(); g = null; clearTimeout(holdTimer); ringOff(); arm(false); }
function endDrag() { if (g && g.dragging) { send({t:'u'}); g.dragging = false; } }

pad.addEventListener('pointerdown', e => {
  e.preventDefault();
  hover = null;
  if (document.body.classList.contains('picking')) return pickClose();   // this touch only closes the list
  // palm rejection: while the Pencil is down, ignore fingers
  if (e.pointerType === 'touch' && penDown()) return;
  // the Pencil takes over from fingers; and a primary pointer means nothing else is
  // touching, so anything still in pts is stale (a lost pointerup)
  if ((e.pointerType === 'pen' && g && !g.pen) || (e.isPrimary && pts.size)) cancelGesture();
  try { pad.setPointerCapture(e.pointerId); } catch (_) {}
  pts.set(e.pointerId, {x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY});
  document.body.classList.add('busy');
  if (pts.size === 1) {
    g = {t0: performance.now(), pen: e.pointerType === 'pen', max: 1,
         moving: false, armed: false, dragging: false, spread: 0};
    clearTimeout(holdTimer);
    ringOn(e.clientX, e.clientY);
    holdTimer = setTimeout(() => {
      if (g && g.max === 1 && !g.moving) { g.armed = true; arm(true); ring.classList.add('full'); }
    }, HOLD_MS);
  } else if (g) {
    g.max = Math.max(g.max, pts.size);
    clearTimeout(holdTimer); ringOff();
    if (g.armed && !g.dragging) { g.armed = false; arm(false); }
  }
});

pad.addEventListener('pointermove', e => {
  const p = pts.get(e.pointerId);
  if (!p) {
    // Pencil hovering above the glass
    if (CFG.hover && e.pointerType === 'pen' && !g) {
      if (hover) { mvx += e.clientX - hover.x; mvy += e.clientY - hover.y; queue(); }
      hover = {x: e.clientX, y: e.clientY};
    }
    return;
  }
  e.preventDefault();
  const dx = e.clientX - p.x, dy = e.clientY - p.y;
  p.x = e.clientX; p.y = e.clientY;
  if (!g) return;
  // distance from where this contact landed (not path length, so jitter does not add up)
  const dist = Math.hypot(p.x - p.x0, p.y - p.y0);
  if (pts.size === 1 && g.max === 1) {
    if (!g.moving) {
      if (dist < (g.pen ? SLOP_PEN : SLOP_TOUCH)) return;
      g.moving = true;
      clearTimeout(holdTimer); ringOff();
      if (g.armed) { g.dragging = true; send({t:'d'}); }
    }
    mvx += dx; mvy += dy; queue();
  } else if (g.max >= 2) {
    g.spread = Math.max(g.spread, dist);
    if (pts.size >= 2 && g.spread > TWO_SLOP) { scx += dx / pts.size; scy += dy / pts.size; queue(); }
  }
});

function lift(e) {
  if (!pts.delete(e.pointerId)) return;
  if (pts.size === 0) document.body.classList.remove('busy');
  if (pts.size > 0 || !g) return;
  clearTimeout(holdTimer); ringOff();
  arm(false);
  const dur = performance.now() - g.t0;
  flush();
  if (g.dragging) send({t:'u'});
  else if (e.type === 'pointercancel') {}        // the system took the touch away: not a tap
  else if (g.max === 1 && g.armed && !g.moving) send({t:'r'});
  else if (g.max === 1 && !g.moving) send({t:'c'});
  else if (g.max === 2 && !g.moving && g.spread <= TWO_SLOP && dur < TWO_TAP_MS) send({t:'r'});
  g = null;
}
pad.addEventListener('pointerup', lift);
pad.addEventListener('pointercancel', lift);
pad.addEventListener('pointerleave', () => { hover = null; });

// ---- guided tour: one gesture at a time, each step completes when you do it
const TOUR = [
  {s:'move',    title:'Drag to move the pointer', text:'Slide the Pencil or one finger anywhere. It works like a trackpad.', need:o => o.t === 'm', count:10},
  {s:'tap',     title:'Tap to click', text:'A quick touch clicks where the pointer is. Tap twice to double-click.', need:o => o.t === 'c'},
  {s:'hold',    title:'Hold, then lift to right-click', text:'Rest the tip until the ring fills and the pad turns blue, then lift.', need:o => o.t === 'r'},
  {s:'drag',    title:'Hold, then slide to drag', text:'Rest the tip until the pad turns blue, then slide. Lift to drop.', need:o => o.t === 'u'},
  {s:'scroll',  title:'Two fingers to scroll', text:'Slide two fingers up or down. A two-finger tap also right-clicks.', need:o => o.t === 's', count:4},
  {s:'corners', title:'The corners are keys', text:'Escape, Screenshot, Dictate and Return. You can hit them without looking. Press any corner.', need:o => o.t === 'key' || o.t === 'shot' || o.t === 'dict'},
];
if (!navigator.standalone) TOUR.push({s:'home', title:'Add PencilPad to your Home Screen',
  text:'Tap Share in Safari, then Add to Home Screen. It opens full screen, like an app.', manual:true});
const PRACTICE = new Set(['c', 'r', 'd', 'u', 'key', 'shot', 'dict']);
const tourEl = document.getElementById('tour'), demo = document.getElementById('demo'), dots = document.getElementById('dots');
let tour = null;         // {i, n, done:[]} while the tour is running
function tourShow() {
  const st = TOUR[tour.i];
  tour.n = 0; tour.locked = false;
  demo.classList.remove('ok'); demo.dataset.s = st.s;
  document.getElementById('tourStep').textContent = 'Step ' + (tour.i + 1) + ' of ' + TOUR.length;
  document.getElementById('tourTitle').textContent = st.title;
  document.getElementById('tourText').textContent = st.text;
  document.getElementById('tourNote').textContent = st.manual ? '' : (st.s === 'move' || st.s === 'scroll')
    ? 'Watch the pointer on your Mac.' : 'Practice only: this is not sent to your Mac.';
  document.getElementById('tourSkip').textContent = st.manual ? 'Done' : 'Skip';
  document.getElementById('tourSkip').classList.toggle('primary', !!st.manual);
  dots.innerHTML = TOUR.map((_, j) => '<i class="' + (j === tour.i ? 'cur' : tour.done[j] ? 'ok' : '') + '"></i>').join('');
}
let tourTimer = null;
function tourStart() { clearTimeout(tourTimer); resetInput(); tour = {i:0, n:0, done:[]}; document.body.classList.add('touring'); tourShow(); }
// release practice presses while the tour is still on, so nothing unmatched reaches the Mac
function tourEnd() { clearTimeout(tourTimer); resetInput(); tour = null; document.body.classList.remove('touring'); store('pencilpad-tour', '1'); }
function tourNext() { clearTimeout(tourTimer); if (!tour) return; if (++tour.i >= TOUR.length) tourEnd(); else tourShow(); }
function tourSaw(o) {
  const st = TOUR[tour.i];
  if (tour.locked || st.manual || !st.need(o) || ++tour.n < (st.count || 1)) return;
  tour.locked = true; tour.done[tour.i] = true;
  demo.classList.add('ok');
  document.getElementById('tourTitle').textContent = 'Nice';
  tourTimer = setTimeout(tourNext, 950);
}
function press(id, fn) { document.getElementById(id).addEventListener('pointerdown', e => { e.preventDefault(); e.stopPropagation(); fn(); }); }
press('tourSkip', tourNext);
press('tourEnd', tourEnd);
press('help', () => { pickClose(); tour ? tourEnd() : tourStart(); });
press('status', () => document.body.classList.contains('picking') ? pickClose() : pickOpen());
press('secure', () => { location.href = 'http://' + location.hostname + ':' + CFG.self.port + PATH + '/secure'; });


// ---- corners
function corner(id, onDown, onUp) {
  const el = document.getElementById(id);
  let active = null;
  cornerResets.push(() => { if (active !== null) { active = null; if (onUp) onUp(el); } });
  el.addEventListener('pointerdown', e => {
    e.preventDefault();
    if (e.pointerType === 'touch' && penDown()) return;   // palm rejection
    if (active !== null) return;
    active = e.pointerId;
    try { el.setPointerCapture(e.pointerId); } catch (_) {}
    onDown(el);
  });
  const end = e => {
    if (e.pointerId !== active) return;
    active = null;
    if (onUp) onUp(el);
  };
  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', end);
}
function flash(el) { el.classList.add('on'); setTimeout(() => el.classList.remove('on'), 160); }

corner('esc',  el => { if (send({t:'key', k:'esc'})) flash(el); });
corner('sub',  el => { if (send({t:'key', k:'return'})) flash(el); });
corner('shot', el => { if (send({t:'shot'})) flash(el); });
// Dictate. When the Mac can turn speech into text and this page may use the iPad's
// microphone, the pad records here and the Mac types what was said. Otherwise the
// corner presses the Mac's own dictation key.
const AC = window.AudioContext || window.webkitAudioContext;
let mic = null;          // {stream, ctx} once the microphone is open
let rec = null;          // {chunks, n} while recording
let micIdle = null;
function micReady() {
  return !tour && cur && cur.speech && window.isSecureContext && AC && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
}
function micClose() { if (!mic) return; for (const t of mic.stream.getTracks()) t.stop(); mic.ctx.close(); mic = null; }
async function micStart(el) {
  const mine = rec = {chunks: [], n: 0};
  el.classList.add('on'); clearTimeout(micIdle);
  try {
    if (!mic) {
      const stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1}});
      const ctx = new AC();
      const node = ctx.createScriptProcessor(4096, 1, 1);
      node.onaudioprocess = e => {
        if (!rec || rec.n > ctx.sampleRate * 120) return;      // two minutes at most
        const d = new Float32Array(e.inputBuffer.getChannelData(0)); rec.chunks.push(d); rec.n += d.length;
      };
      ctx.createMediaStreamSource(stream).connect(node); node.connect(ctx.destination);
      mic = {stream, ctx};
    }
    if (mic.ctx.state !== 'running') await mic.ctx.resume();
    if (rec === mine) dictLabel('Listening…');
  } catch (_) {
    if (rec === mine) rec = null;
    el.classList.remove('on'); micClose();
    dictLabel('Allow the microphone, then try again', 3000);
  }
}
async function micStop(el) {
  const mine = rec; rec = null;
  el.classList.remove('on');
  micIdle = setTimeout(micClose, 90000);
  if (!mine || !mic || !cur) return dictLabel();
  if (mine.n < mic.ctx.sampleRate * 0.3) return dictLabel(mine.n ? '' : 'Hold while you speak', 2000);
  const pcm = new Int16Array(mine.n); let i = 0;
  for (const c of mine.chunks) for (let j = 0; j < c.length; j++) pcm[i++] = Math.max(-1, Math.min(1, c[j])) * 32767;
  dictLabel('Typing…');
  try {
    const r = await fetch(cur.base + PATH + '/speech?rate=' + Math.round(mic.ctx.sampleRate), {method: 'POST', body: pcm.buffer});
    const out = await r.json();
    dictLabel(out.text ? '' : out.error === 'denied' ? 'Allow Speech Recognition on the Mac'
      : out.error ? 'Could not transcribe' : 'Did not catch that', 2500);
  } catch (_) { dictLabel('Could not reach the Mac', 2500); }
}
if (CFG.dictMode === 'toggle') {
  let on = false;
  cornerResets.push(() => { if (rec) { rec = null; on = false; dictLabel(); } });
  corner('dict', el => {
    if (micReady() || rec) { on = !on; return on ? micStart(el) : micStop(el); }
    if (!send({t:'dict', s:1})) return; if (tour) return flash(el); on = !on; el.classList.toggle('on', on);
  });
} else {
  let viaMic = false;
  corner('dict', el => { viaMic = !!micReady(); if (viaMic) micStart(el); else if (send({t:'dict', s:1})) el.classList.add('on'); },
                 el => { if (viaMic) micStop(el); else { el.classList.remove('on'); send({t:'dict', s:0}); } });
}

// ---- start. Move to the best address this page can be loaded from: the secure port
// once the iPad trusts this Mac's certificate, and the Mac's .local name, which
// survives IP changes. The Home Screen app stays where it was added.
(async function start() {
  if (location.hash === '#toured') { store('pencilpad-tour', '1'); history.replaceState(null, '', location.pathname); }
  if (SECURE && navigator.serviceWorker) navigator.serviceWorker.register(PATH + '/sw.js', {scope: PATH}).catch(() => {});
  const host = location.hostname, ip = /^[0-9.]+$/.test(host);
  if (navigator.standalone || host === '127.0.0.1' || host === 'localhost') return connect();
  const want = [];
  const name = ip && CFG.local ? CFG.local : null;
  if (!SECURE && CFG.self.tls) { if (name) want.push('https://' + name + ':' + CFG.self.tls); want.push('https://' + host + ':' + CFG.self.tls); }
  if (name) want.push(location.protocol + '//' + name + ':' + location.port);
  if (!want.length) return connect();
  const tries = want.map(base => ({base, ok: null}));
  const to = await new Promise(done => {
    const settle = () => { for (const t of tries) { if (t.ok) return done(t.base); if (t.ok === null) return; } done(null); };
    for (const t of tries) probe(t.base, CFG.self.id, 1500).then(ok => { t.ok = ok; settle(); });
  });
  if (to) location.replace(to + PATH + (store('pencilpad-tour') ? '#toured' : '')); else connect();
})();

// ---- keep Safari from scrolling, zooming or selecting
for (const ev of ['touchstart', 'touchmove', 'gesturestart', 'gesturechange', 'contextmenu', 'dblclick', 'selectstart'])
  document.addEventListener(ev, e => e.preventDefault(), {passive: false});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
