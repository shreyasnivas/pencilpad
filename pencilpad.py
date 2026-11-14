#!/usr/bin/env python3
"""PencilPad: use an iPad + Apple Pencil as a trackpad and command pad for this Mac.

Run this on the Mac you want to control, then open the printed URL in Safari
on the iPad (same Wi-Fi). The terminal app that runs it needs Accessibility
permission (System Settings > Privacy & Security > Accessibility), and Screen
Recording permission for the Screenshot corner.
"""

import asyncio
import errno
import json
import math
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import Quartz
from AppKit import NSPasteboard
from aiohttp import WSMsgType, web

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
           "local": local_hostname() or ""}
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
    await ws.send_json({"boot": BOOT_ID})
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
        "access": bool(getattr(Quartz, "CGPreflightPostEventAccess", lambda: True)()),
        "screen": bool(getattr(Quartz, "CGPreflightScreenCaptureAccess", lambda: True)()),
    })


async def ping(request):
    check_token(request)
    return web.Response(text="ok", headers={"Access-Control-Allow-Origin": "*"})


SETTINGS_PANES = {"accessibility": "Privacy_Accessibility", "screen": "Privacy_ScreenCapture"}


async def open_settings(request):
    check_token(request)
    pane = SETTINGS_PANES.get(request.match_info["pane"])
    if not pane or not is_local(request):      # only from this Mac's own browser
        raise web.HTTPNotFound()
    subprocess.Popen(["open", f"x-apple.systempreferences:com.apple.preference.security?{pane}"])
    return web.Response(text="ok")


def main():
    if (DICTATION["mode"] not in ("hold", "toggle") or DICTATION["key"] not in KEYCODES
            or any(m not in MOD_FLAGS for m in DICTATION.get("mods", []))):
        sys.exit(f"The DICTATION setting is not valid: {DICTATION}\n"
                 f"mode: hold or toggle; key: one of {', '.join(KEYCODES)}; "
                 f"mods: any of {', '.join(MOD_FLAGS)}")
    trusted = getattr(Quartz, "CGPreflightPostEventAccess", lambda: True)()
    if not trusted and not DRY_RUN:
        getattr(Quartz, "CGRequestPostEventAccess", lambda: None)()
        print("!! This terminal app does not have Accessibility permission yet.\n"
              "   Turn it on in System Settings > Privacy & Security > Accessibility,\n"
              "   then quit and reopen the terminal and run this again.\n"
              "   (The pad will connect, but the pointer will not move until then.)\n")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", PORT))
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            sys.exit(f"Port {PORT} is already in use. PencilPad is probably already running.\n"
                     f"Stop the other copy, or pick another port: PENCILPAD_PORT=8766 ./run.sh")
        raise
    token = load_token()
    app = web.Application()
    app["token"] = token
    app["clients"] = set()
    app.add_routes([
        web.get("/{token}", page), web.get("/{token}/ws", ws_handler),
        web.get("/{token}/connect", connect_page), web.get("/{token}/state", state),
        web.get("/{token}/ping", ping), web.post("/{token}/settings/{pane}", open_settings),
    ])
    if DRY_RUN:
        app.add_routes([web.get("/{token}/dry", dry_log_handler),
                        web.post("/{token}/dry/drop", dry_drop_handler)])
    setup = f"http://localhost:{PORT}/{token}/connect"
    print("PencilPad is running" + (" (dry run: nothing is sent to this Mac)" if DRY_RUN else "") + ".\n")
    print(f"   Set up your iPad (QR code):  {setup}")
    print(f"   Or open on the iPad:         {pad_url(app)}\n")
    print("Ctrl+C here to stop.\n", flush=True)
    if sys.stdout.isatty() and not DRY_RUN and not os.environ.get("PENCILPAD_NO_OPEN"):
        subprocess.Popen(["open", setup])      # started by hand: show the setup screen
    web.run_app(app, sock=sock, print=None)


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
    font-size:15px; font-weight:500; color:var(--label); white-space:nowrap; pointer-events:none; }
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
function showStatus(armed) {
  statusText.textContent = armed ? 'Lift to right-click · move to drag'
    : online ? (CFG.mac ? 'Connected to ' + CFG.mac : 'Connected') : 'Reconnecting…';
}
function setOnline(on) {
  online = on; document.body.classList.toggle('offline', !on); showStatus(false);
  if (!on && CFG.mac) document.getElementById('lostTitle').textContent = 'Looking for ' + CFG.mac + '…';
}
function arm(on) { pad.classList.toggle('armed', on); showStatus(on); }
document.getElementById('reload').addEventListener('click', () => location.reload());
document.getElementById('dictSub').textContent = CFG.dictMode === 'toggle' ? 'Tap to start or stop' : 'Hold to talk';
// Tolerances. SLOP_*: how far (px, straight line from where it landed) the tip may
// wander and still count as a tap or a hold. HOLD_MS: hold-still time before the pad arms.
const HOLD_MS = 500, TWO_TAP_MS = 600, SLOP_PEN = 18, SLOP_TOUCH = 20, TWO_SLOP = 20;

// ---- connection
let ws = null;
let retry = null;
function connect() {
  clearTimeout(retry);
  if (ws && ws.readyState <= 1) return;          // already open or opening
  const sock = ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + location.pathname.replace(/\/$/, '') + '/ws');
  ws.onopen = () => { if (sock !== ws) return; setOnline(true); if (!tour && !store('pencilpad-tour')) tourStart(); };
  ws.onclose = () => { if (sock !== ws) return; resetInput(); setOnline(false); retry = setTimeout(connect, 700); };
  ws.onerror = () => sock.close();
  ws.onmessage = m => { try { if (JSON.parse(m.data).boot !== CFG.boot) location.reload(); } catch (_) {} };
}
function send(o) {
  if (tour) { tourSaw(o); if (PRACTICE.has(o.t)) return true; }   // the tour only practises clicks and keys
  if (!ws || ws.readyState !== 1) return false;
  ws.send(JSON.stringify(o)); return true;
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) resetInput(); else connect();
});

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
function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (_) { return null; } }
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
press('help', () => tour ? tourEnd() : tourStart());


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
if (CFG.dictMode === 'toggle') {
  let on = false;
  corner('dict', el => { if (!send({t:'dict', s:1})) return; if (tour) return flash(el); on = !on; el.classList.toggle('on', on); });
} else {
  corner('dict', el => { if (send({t:'dict', s:1})) el.classList.add('on'); },
                 el => { el.classList.remove('on'); send({t:'dict', s:0}); });
}

// ---- start. If the Mac's .local name works from here, switch to it: it survives IP changes.
(function start() {
  const ip = /^[0-9.]+$/.test(location.hostname);
  if (!ip || !CFG.local || location.hostname === '127.0.0.1') return connect();
  const to = location.protocol + '//' + CFG.local + ':' + location.port + location.pathname;
  const ctl = new AbortController(); setTimeout(() => ctl.abort(), 1500);
  fetch(to.replace(/\/$/, '') + '/ping', {signal: ctl.signal}).then(r => r.ok ? location.replace(to) : connect(), connect);
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
