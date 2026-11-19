#!/usr/bin/env python3
"""PencilPad: use an iPad + Apple Pencil as a trackpad and command pad for this Mac.

Run this on the Mac you want to control, then open the printed URL in Safari
on the iPad (same Wi-Fi). The terminal app that runs it needs Accessibility
permission (System Settings > Privacy & Security > Accessibility), and Screen
Recording permission for the Screenshot key.
"""

import asyncio
import collections
import datetime
import errno
import hashlib
import ipaddress
import json
import re
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
# These are the defaults. The pad's Settings menu changes most of them; what it changes
# is kept on this Mac in ~/.pencilpad/settings.json (see SETTINGS below).
DEFAULT_PORT = 8765        # the first run's port; see choose_port() for when it is another
POINTER_SPEED = 1.6        # pointer travel per unit of pencil travel
POINTER_ACCEL = 0.045      # extra speed for fast strokes (0 = none)
SCROLL_SPEED = 1.4
HOVER_MOVES = False        # True: a hovering Pencil also moves the pointer

# Dictate key, for a dictation app on the Mac. mode "hold" = key is held while
# Dictate is pressed (push-to-talk); mode "toggle" = each press taps the key once.
# key: any name in KEYCODES below. mods: e.g. ["control", "option"].
DICTATION = {"mode": "hold", "key": "f13", "mods": []}

SCREENSHOT_PASTE = True    # after a region capture, press Ctrl+V (Claude Code image paste)
# ---------------------------------------------------------------------------

KEYCODES = {
    "return": 36, "esc": 53, "space": 49, "tab": 48, "delete": 51, "forward_delete": 117,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "page_up": 116, "page_down": 121,
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31, "u": 32,
    "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28, "9": 25, "0": 29,
    "grave": 50, "minus": 27, "equal": 24, "lbracket": 33, "rbracket": 30, "backslash": 42,
    "semicolon": 41, "quote": 39, "comma": 43, "period": 47, "slash": 44,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80,
    "fn": 63, "control": 59, "option": 58, "command": 55, "shift": 56,
    "right_option": 61, "right_command": 54, "right_control": 62,
}
# Keys a real Mac keyboard sends with the fn flag, and the arrows, which also carry the
# numeric-pad flag. Set from this table, never read back from a new event: one made
# with no event source carries whatever modifiers are down on the Mac at that moment.
FN_CODES = {KEYCODES[k] for k in ("left", "right", "down", "up", "home", "end", "page_up", "page_down",
                                  "forward_delete", *(f"f{i}" for i in range(1, 20)))}
NUMPAD_CODES = {KEYCODES[k] for k in ("left", "right", "down", "up")}
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
# PORT: the port this Mac serves the pad on, and TLS_PORT the secure (HTTPS) one, which the
# iPad needs for the microphone and for switching between Macs: one above PORT, or
# PENCILPAD_TLS_PORT (a number, or off). Both are set at start, by use_port().
PORT = TLS_PORT = 0
# A command that takes a WAV file and prints what was said. The menu bar app sets it;
# without it the Dictate key presses the DICTATION key instead.
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


def tls_port_for(port):
    v = os.environ.get("PENCILPAD_TLS_PORT", str(port + 1))
    return int(v) if v.isdigit() and 0 < int(v) < 65536 else 0


def port_free(port):
    try:
        bind(port).close()
        return True
    except OSError:
        return False


def pencilpad_at(port):
    """Whether this Mac's own PencilPad is what answers on that port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/{load_token()}/ping", timeout=1) as r:
            return json.loads(r.read()).get("id") == mac_id()
    except Exception:
        return False


def choose_port():
    """PENCILPAD_PORT, as given; else the port saved in ~/.pencilpad/port; else, on the
    first run, 8765, or the next free pair when 8765 or the one above it is taken by
    another program. That one is saved, and from then on never changes by itself: the
    iPad's link has it in it. Returns (port, a line to print or "")."""
    env = os.environ.get("PENCILPAD_PORT")
    if env:
        if not env.isdigit() or not 0 < int(env) < 65536:
            sys.exit(f"PENCILPAD_PORT must be a port number from 1 to 65535, not {env!r}.")
        return int(env), ""
    unreadable = False
    try:
        text = (HOME / "port").read_text().strip()
        if text.isdigit() and 0 < int(text) < 65536:
            return int(text), ""
        unreadable = True
    except FileNotFoundError:
        pass
    except Exception:
        unreadable = True
    pair_needed = "PENCILPAD_TLS_PORT" not in os.environ      # the secure port is the one above
    port = DEFAULT_PORT
    if pencilpad_at(port):
        return port, ""                       # a copy already running there: say so at bind
    while port < 65534 and not (port_free(port) and (not pair_needed or port_free(port + 1))):
        port += 1
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "port").write_text(f"{port}\n")
    if unreadable:
        return port, f"The saved port in {HOME / 'port'} could not be read, so PencilPad uses port {port} (now saved).\n"
    if port == DEFAULT_PORT:
        return port, ""
    return port, (f"Port {DEFAULT_PORT} is in use by another program, so PencilPad uses port {port}. "
                  f"It is saved in {HOME / 'port'} and stays the same from now on.\n")


def use_port():
    global PORT, TLS_PORT
    PORT, note = choose_port()
    TLS_PORT = tls_port_for(PORT)
    if note:
        print(note, flush=True)


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
        self.owners = {}                # key name -> who holds it down (see key())
        self.epoch = 0                  # counts release_all(): a chord stops when it changes
        self.drag_pad = None            # the pad that pressed the button for a drag
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

    def pointer_flags(self):
        """Exactly the modifiers the pads' keyboards hold (Shift-click, Option-drag), so a
        new event never carries whatever else happens to be down on the Mac."""
        flags = 0
        for m in self.held_mods(pads_only=True):
            flags |= MOD_FLAGS[m]
        return flags

    def mouse(self, kind, x, y, button=Quartz.kCGMouseButtonLeft, clicks=1):
        ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), button)
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, clicks)
        Quartz.CGEventSetFlags(ev, self.pointer_flags())
        post(ev)

    def move(self, dx, dy):
        speed = (dx * dx + dy * dy) ** 0.5
        gain = setting("pointer_speed") * (1.0 + POINTER_ACCEL * speed)
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
        Quartz.CGEventSetFlags(ev, self.pointer_flags())
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
        gain = setting("scroll_speed") * (-1 if setting("scroll_direction") == "reversed" else 1)
        self.frac[0] += dx * gain
        self.frac[1] += dy * gain
        ix, iy = int(self.frac[0]), int(self.frac[1])
        self.frac[0] -= ix
        self.frac[1] -= iy
        if ix or iy:
            ev = Quartz.CGEventCreateScrollWheelEvent(
                None, Quartz.kCGScrollEventUnitPixel, 2, iy, ix)
            Quartz.CGEventSetFlags(ev, self.pointer_flags())
            post(ev)

    # ---- keyboard. Several things press keys here: each pad's keyboard and rail keys,
    # Dictate, chords, and this Mac's own presses (Screenshot's paste). Each owns what it
    # pressed. A key two of them hold goes down once and comes up when the last lets go,
    # and each only ever lets go of what it pressed itself.
    def key(self, name, is_down, mods=(), repeat=False, owner="mac"):
        own = self.owners.get(name)
        if is_down:
            if own:
                if owner in own:          # already down: only a repeat is posted again
                    if repeat and name not in MOD_FLAGS:
                        self.post_key(name, True, mods, repeat=True)
                elif not repeat:
                    own.add(owner)
                return
            if repeat:                    # a repeat only for a key that is still down
                return
            down_mods = self.held_mods() if name in MOD_FLAGS else mods
            self.owners[name] = {owner}
            self.post_key(name, True, down_mods)
        elif own and owner in own:        # never a key-up for a press that is not this owner's
            own.discard(owner)
            if not own:
                del self.owners[name]
                self.post_key(name, False, self.held_mods() if name in MOD_FLAGS else mods)

    def post_key(self, name, is_down, mods=(), repeat=False):
        code = KEYCODES[name]
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, is_down)
        if repeat:      # the pad repeats a held key itself: macOS does not repeat posted keys
            Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGKeyboardEventAutorepeat, 1)
        flags = 0
        for m in mods:
            flags |= MOD_FLAGS[m]
        if name in MOD_FLAGS:
            Quartz.CGEventSetType(ev, Quartz.kCGEventFlagsChanged)
            if is_down:
                flags |= MOD_FLAGS[name]
        else:
            if code in FN_CODES:
                flags |= Quartz.kCGEventFlagMaskSecondaryFn
            if code in NUMPAD_CODES:
                flags |= Quartz.kCGEventFlagMaskNumericPad
        Quartz.CGEventSetFlags(ev, flags)
        post(ev)

    @property
    def held_keys(self):
        return set(self.owners)

    def holding(self, name):
        return name in self.owners

    def held_mods(self, pads_only=False):
        return sorted(n for n, own in self.owners.items()
                      if n in MOD_FLAGS and (not pads_only or any(map(is_pad, own))))

    def release_owner(self, owner):
        """Lets go of what this owner pressed: keys first, then its modifiers."""
        mine = [n for n, own in self.owners.items() if owner in own]
        for name in sorted(n for n in mine if n not in MOD_FLAGS):
            self.key(name, False, self.held_mods(), owner=owner)
        for name in sorted(n for n in mine if n in MOD_FLAGS):
            self.key(name, False, owner=owner)

    def take(self, name):
        """A fixed action's own key, already down for someone else (the pad's keyboard
        holds C, and Copy is pressed): up it goes first, so the action's press is a real
        one. The other owner's hold is over; its later key-up is ignored."""
        if name in self.owners:
            del self.owners[name]
            self.post_key(name, False, self.held_mods() if name in MOD_FLAGS else ())

    def tap_key(self, name, mods=()):
        self.take(name)
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
        """Lets go of everything, whoever pressed it: keys first, then the modifiers one at
        a time, each event carrying only the modifiers still down, so the last one carries
        none. A chord under way stops at its next step."""
        self.up()
        self.epoch += 1
        for name in sorted(n for n in self.owners if n not in MOD_FLAGS):
            del self.owners[name]
            self.post_key(name, False, self.held_mods())
        for name in self.held_mods():
            del self.owners[name]
            self.post_key(name, False, self.held_mods())


def is_pad(owner):
    """A pad's keyboard (its connection), not Dictate, a chord or this Mac itself."""
    return owner == "pad" or not isinstance(owner, (str, tuple))


mac = Mac()
STEP = 0.015        # between the parts of a chord: the Dock's app switcher misses instant ones


async def chord(key, owner, mod="command"):
    """Command+key as a real keyboard sends it: Command down, the key, Command up (with
    Command already held, Command stays as it is). If what it holds is let go meanwhile
    (its pad went away), it stops there: no key, and no Command-up of its own."""
    epoch, was_down = mac.epoch, mac.holding(mod) or mac.holding("right_" + mod)
    mac.key(mod, True, owner=owner)
    for name, down, mods in ((key, True, [mod]), (key, False, [mod]), (mod, False, ())):
        if name == key and down or not was_down:      # no wait around a Command that does not move
            await asyncio.sleep(STEP)
        if mac.epoch != epoch or owner not in mac.owners.get(mod, ()):
            return                                    # let go of meanwhile: its pad went away
        if name == key and down:
            mac.take(key)
        mac.key(name, down, mods, owner=owner)


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
        if setting("screenshot_paste") and changed:
            await asyncio.sleep(0.25)
            while chording or waiting:           # never inside a chord
                await asyncio.sleep(0.005)
            mac.tap_key("v", ["control"])
    finally:
        shot_running = False


mc_running = False


async def mission_control():
    """Shows every open window, or puts them back. Opening the system's own Mission
    Control app toggles it, whatever keyboard shortcut is set, with no permission."""
    global mc_running
    if mc_running:
        return
    mc_running = True
    try:
        if DRY_RUN:
            dry_record({"ev": "missioncontrol"})
            await asyncio.sleep(0.2)
            return
        proc = await asyncio.create_subprocess_exec(
            "open", "-b", "com.apple.exposelauncher",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()
    finally:
        mc_running = False


# ---- Dictate, for a dictation app on the Mac: press its key
def dict_key():
    """The dictation key and the modifiers pressed with it, e.g. ("space", ["option"])."""
    *mods, key = setting("dictation_key").split("+")
    return key, mods


def dict_press(down):
    """Dictate owns what it presses: letting go releases exactly that, whatever else
    (the pad's keyboard) holds the same key or modifier."""
    if not down:
        return mac.release_owner("dict")
    key, mods = dict_key()
    for m in mods:
        mac.key(m, True, owner="dict")
    mac.key(key, True, mods, owner="dict")


dict_tapping = False
dict_pad = None             # the pad that last pressed Dictate


async def dict_tap():
    """Toggle mode: one press of the key. Some apps ignore a zero-length tap of a modifier."""
    global dict_tapping
    if dict_tapping:
        return
    dict_tapping = True
    try:
        dict_press(True)
        await asyncio.sleep(0.04)
        dict_press(False)
    finally:
        dict_tapping = False


def flag(v):
    """A 0/1 or true/false field; anything else is not one."""
    return v if type(v) is bool else bool(v) if type(v) is int and v in (0, 1) else None


# ---- input from the pads, in order. A chord (Copy, Paste, Switch, a zoom step) holds
# the line for its 45 ms: whatever arrives meanwhile waits, then runs in order, so nothing
# lands between its Command-down and Command-up. A chord belongs to the pad that asked:
# when that pad goes away, what it has waiting is dropped. A pad may have one chord of
# each kind waiting; more are dropped. Switch is never queued behind a Switch: a second
# press while one is under way would switch straight back.
CHORDS = {"copy": "c", "paste": "v", "app": "tab"}
waiting = collections.deque()     # (pad, message, app) held back while a chord runs
chording = None                   # (pad, key) of the chord running now


def chord_of(msg):
    t = msg.get("t")
    if t == "zoom":
        d = msg.get("d")
        return {1: "equal", -1: "minus"}.get(d) if type(d) is int else None
    return CHORDS.get(t) if isinstance(t, str) else None


MAX_WAITING = 200


def releases(msg):
    """A key-up, a mouse-up or a Dictate release: never dropped from the queue."""
    t = msg.get("t")
    return t == "u" or t == "k" and flag(msg.get("d")) is False or t == "dict" and flag(msg.get("s")) is False


def wait(pad, msg, app):
    """Queues a message behind the chord. A move or scroll right after another one from
    the same pad is added into it; past MAX_WAITING the oldest move or scroll is dropped
    first, and nothing but a release is taken in when there is none to drop."""
    t = msg.get("t")
    if t in ("m", "s") and waiting and waiting[-1][0] is pad and waiting[-1][1].get("t") == t:
        last = waiting[-1][1]
        try:
            waiting[-1] = (pad, dict(last, x=num(last["x"]) + num(msg["x"]), y=num(last["y"]) + num(msg["y"])), app)
            return
        except (KeyError, TypeError, ValueError):
            pass
    if len(waiting) >= MAX_WAITING:
        old = next((w for w in waiting if w[1].get("t") in ("m", "s")), None)
        if old:
            waiting.remove(old)
        elif not releases(msg):
            return
    waiting.append((pad, msg, app))


def handle(msg, app=None, pad="pad"):
    """One message from a pad (its connection, or "pad")."""
    key = chord_of(msg)
    if chording or waiting:
        if key and (any(p is pad and chord_of(m) == key for p, m, _ in waiting)
                    or key == "tab" and chording == (pad, key)):
            return
        wait(pad, msg, app)
    elif key:
        start_chord(pad, key)
    else:
        act(msg, app, pad)


def start_chord(pad, key):
    global chording
    chording = (pad, key)
    asyncio.ensure_future(run_chord(pad, key))


async def run_chord(pad, key):
    global chording
    try:
        await chord(key, ("chord", pad))
    finally:
        chording = None
        while waiting and not chording:
            p, msg, app = waiting.popleft()
            try:
                if chord_of(msg):
                    start_chord(p, chord_of(msg))
                else:
                    act(msg, app, p)
            except Exception as e:
                print("bad message:", str(msg)[:120], "->", repr(e))


def drop_waiting(pad):
    """The pad went away: what it still had waiting is not done."""
    for item in [w for w in waiting if w[0] is pad]:
        waiting.remove(item)


def act(msg, app, pad):
    global dict_pad
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
        mac.drag_pad = pad                    # if this pad goes, the button comes up
    elif t == "u":
        mac.up()
    elif t == "key" and msg.get("k") in ("esc", "return"):
        mac.tap_key(msg["k"])
    elif t == "shot":
        asyncio.ensure_future(screenshot())
    elif t == "mc":
        asyncio.ensure_future(mission_control())
    elif t == "k":
        # the pad's keyboard: one key down or up, by name, with the modifiers it applies to
        name, mods, down = msg.get("k"), msg.get("m") or [], flag(msg.get("d"))
        if not (isinstance(name, str) and name in KEYCODES and isinstance(mods, list) and down is not None
                and all(isinstance(m, str) and m in MOD_FLAGS for m in mods)):
            return
        repeat = down and flag(msg.get("r")) is True
        if repeat and name in MOD_FLAGS:          # modifiers never repeat
            return
        mac.key(name, down, mods, repeat=repeat, owner=pad)
    elif t == "dict":
        s = flag(msg.get("s"))
        if s is None:
            return
        dict_pad = pad                        # if this pad goes, so does its Dictate
        if setting("dictation_mode") == "toggle":
            if s:
                asyncio.ensure_future(dict_tap())
        else:
            dict_press(s)
    elif t == "set":
        if change_setting(msg.get("k"), msg.get("v")) and app is not None:
            asyncio.ensure_future(push_settings(app))


# ---- settings the pad's Settings menu changes. The constants at the top are the
# defaults; ~/.pencilpad/settings.json keeps only what was changed from the pad, e.g.
#   {"dictation_source": "mac", "dictation_key": "fn", "dictation_mode": "toggle"}
# Each is a range (numbers, clamped), bool, or a list of the values allowed.
DICTATION_KEYS = ["fn", *(f"f{i}" for i in range(13, 20)), "right_option", "right_command",
                  "right_control", "option+space", "control+space"]
DICTATION_DEFAULT = "+".join([*DICTATION.get("mods", []), DICTATION["key"]])
if DICTATION_DEFAULT not in DICTATION_KEYS:      # a key set by hand above stays on offer
    DICTATION_KEYS.append(DICTATION_DEFAULT)
SETTINGS = {
    "pointer_speed": (0.4, 4.0), "scroll_speed": (0.2, 5.0),
    "scroll_direction": ["natural", "reversed"], "hover": bool,
    "dictation_source": ["ipad", "mac"],        # ipad: the iPad's microphone when it can be used
    "dictation_key": DICTATION_KEYS, "dictation_mode": ["hold", "toggle"],
    "screenshot_paste": bool,
}
DEFAULTS = {"pointer_speed": POINTER_SPEED, "scroll_speed": SCROLL_SPEED, "scroll_direction": "natural",
            "hover": HOVER_MOVES, "dictation_source": "ipad", "dictation_key": DICTATION_DEFAULT,
            "dictation_mode": DICTATION["mode"], "screenshot_paste": SCREENSHOT_PASTE}
prefs = {}          # what has been changed from the pad


def clean_setting(name, v):
    """The value, made safe (numbers clamped); None when it is not one this setting takes."""
    kind = SETTINGS.get(name) if isinstance(name, str) else None
    if kind is bool:
        return v if type(v) is bool else None
    if isinstance(kind, tuple):
        if type(v) not in (int, float) or not math.isfinite(v):
            return None
        return round(min(max(float(v), kind[0]), kind[1]), 2)
    if isinstance(kind, list):
        return v if isinstance(v, str) and v in kind else None
    return None


def setting(name):
    return prefs.get(name, DEFAULTS[name])


def current_settings():
    return {k: setting(k) for k in SETTINGS}


def load_settings():
    raw = read_json(HOME / "settings.json", "the settings in pencilpad.py are used")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in ((k, clean_setting(k, v)) for k, v in raw.items()) if v is not None}


def save_settings():
    """Written to a new file that then replaces the old one, so it is never half written."""
    HOME.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=HOME, prefix=".settings-", suffix=".json", delete=False) as f:
        json.dump(prefs, f, indent=1)
    os.replace(f.name, HOME / "settings.json")


def change_setting(name, v):
    v = clean_setting(name, v)
    if v is None or setting(name) == v:
        return False
    if name in ("dictation_key", "dictation_mode"):
        dict_press(False)                 # let go of the old key before it changes
    prefs[name] = v
    try:
        save_settings()
    except OSError as e:
        print("could not save settings:", repr(e), flush=True)
    return True


async def push_settings(app):
    """Every pad sees the change, so two iPads stay in step."""
    for ws in list(app["clients"]):
        try:
            await ws.send_json({"settings": current_settings()})
        except Exception:
            pass


async def page(request):
    if request.match_info["token"] != request.app["token"]:
        raise web.HTTPNotFound()
    cfg = {"hover": setting("hover"), "dictMode": setting("dictation_mode"), "boot": BOOT_ID, "mac": MAC_NAME,
           "settings": current_settings(), "dictKeys": DICTATION_KEYS,
           "local": local_hostname() or "",
           "self": {k: v for k, v in info(request.app).items() if k != "boot"},
           "peers": list(request.app["peers"].values())}
    html = PAGE.replace("__CFG__", json.dumps(cfg))
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


def let_go(app, ws):
    """A pad is gone: it lets go of what it holds (its keys, chords, waiting messages, its
    drag, and a Dictate it started); what other pads hold stays. When the last pad goes,
    everything goes. Done once per connection, whichever comes first: its socket
    closing, or the same pad coming back on a new one."""
    if ws not in app["clients"]:
        return
    app["clients"].discard(ws)
    drop_waiting(ws)
    if not app["clients"]:
        mac.release_all()
        return
    mac.release_owner(ws)
    mac.release_owner(("chord", ws))
    if dict_pad is ws:
        dict_press(False)
    if mac.drag_pad is ws:
        mac.up()


async def ws_handler(request):
    if request.match_info["token"] != request.app["token"]:
        raise web.HTTPNotFound()
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    print(f"iPad connected ({request.remote})", flush=True)
    if DRY_RUN:
        dry_record({"ev": "connect"})
    # Each page names itself (?pad=, once per page load). The same page on a new socket
    # (its Wi-Fi blipped) means its old socket is gone, even if that one has not noticed
    # yet: let go of what it held now, rather than when its heartbeat gives up.
    pad_id = request.query.get("pad", "")
    pad_id = pad_id if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", pad_id) else None
    old = request.app["pads"].get(pad_id) if pad_id else None
    if old is not None:
        let_go(request.app, old)
        asyncio.ensure_future(old.close())   # not awaited: a half-open socket never answers
    if pad_id:
        request.app["pads"][pad_id] = ws
    request.app["clients"].add(ws)
    ws.secure = request.secure
    try:
        await ws.send_json(dict(info(request.app), peers=list(request.app["peers"].values()),
                                settings=current_settings(), dictKeys=DICTATION_KEYS))
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                continue
            try:
                handle(json.loads(m.data), request.app, ws)
            except Exception as e:  # one bad message must not kill the pad
                print("bad message:", m.data[:120], "->", repr(e))
    finally:
        if pad_id and request.app["pads"].get(pad_id) is ws:
            del request.app["pads"][pad_id]
        if DRY_RUN:
            dry_record({"ev": "disconnect"})
        let_go(request.app, ws)
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
    flags = Quartz.CGEventGetFlags(ev)
    mods = [m for m in ("fn", "control", "option", "command", "shift") if flags & MOD_FLAGS[m]]
    if kind == Quartz.kCGEventScrollWheel:
        e.update(dy=field(Quartz.kCGScrollWheelEventPointDeltaAxis1),
                 dx=field(Quartz.kCGScrollWheelEventPointDeltaAxis2), mods=mods)
    elif kind in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp, Quartz.kCGEventFlagsChanged):
        code = field(Quartz.kCGKeyboardEventKeycode)
        e.update(key=KEY_NAMES.get(code, code), mods=mods)
        if field(Quartz.kCGKeyboardEventAutorepeat):
            e["repeat"] = True
    else:
        loc = Quartz.CGEventGetLocation(ev)
        mac.dry_pos = (loc.x, loc.y)
        e.update(x=round(loc.x, 2), y=round(loc.y, 2),
                 clicks=field(Quartz.kCGMouseEventClickState),
                 dx=field(Quartz.kCGMouseEventDeltaX), dy=field(Quartz.kCGMouseEventDeltaY), mods=mods)
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


def same_origin_json(request):
    """A request that changes something must be JSON (a page elsewhere cannot send that
    without asking first) and must not come from a page on another origin."""
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    origin = request.headers.get("Origin")
    if origin and origin != f"{request.scheme}://{request.host}":
        raise web.HTTPForbidden()
    if request.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
        raise web.HTTPForbidden()


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
    # the link typed by hand may wrap: only after "://", the port's ":" and before the token
    u = urlsplit(url)
    breaks = f"{u.scheme}://<wbr>{u.hostname}:<wbr>{u.port}<wbr>{u.path}"
    html = (CONNECT.replace("__QR__", qr).replace("__URLBREAKS__", breaks).replace("__URL__", url)
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


# What this version of the script can do beyond the first pad. A pad that talks to
# several Macs uses it to tell an older one apart, and asks for that one to be updated.
# mc: Windows; app: Switch; k: the keyboard (and Space); clip: Copy and Paste; zoom: pinch;
# settings: the Settings menu's Mac settings.
CAPS = ["mc", "app", "k", "clip", "zoom", "settings"]


def info(app):
    return {"id": app["id"], "name": MAC_NAME, "hosts": app["hosts"], "port": PORT,
            "tls": app["tls"], "boot": BOOT_ID, "speech": bool(TRANSCRIBE), "caps": CAPS}


PEER_NETS = [ipaddress.ip_network(n) for n in
             ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8")]
DNS_NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")


def peer_host(h):
    """Only what this Mac's certificate authority can vouch for: .local and Tailscale
    names, localhost, and private, link-local, CGNAT and loopback addresses."""
    if not isinstance(h, str) or len(h) > 253:
        return False
    h = h.lower()
    try:
        return any(ipaddress.ip_address(h) in n for n in PEER_NETS)
    except ValueError:
        pass
    return bool(DNS_NAME.fullmatch(h)) and (h == "localhost" or h.endswith((".local", ".ts.net")))


def clean_peer(d):
    """A peer record from the network or from macs.json: keep only well-formed fields,
    and only hosts a pad may be sent to. Raises ValueError for anything else."""
    port, tls = d["port"], d.get("tls") or 0
    if type(port) is not int or not 0 < port < 65536 or type(tls) is not int or not 0 <= tls < 65536:
        raise ValueError("not a PencilPad Mac")
    peer = {"id": str(d["id"])[:40], "name": str(d.get("name") or "Mac")[:80],
            "hosts": [h.lower() for h in d.get("hosts") or [] if peer_host(h)][:12],
            "port": port, "tls": tls}
    if not re.fullmatch(r"[A-Za-z0-9_-]+", peer["id"]) or not peer["hosts"]:
        raise ValueError("not a PencilPad Mac")
    return peer


def read_json(path, what):
    """A file of ours, or None. One that is there but cannot be read (garbled, too deeply
    nested) is said once and treated as missing; it never stops PencilPad starting."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except Exception as e:                    # includes RecursionError
        print(f"(Could not read {path} ({type(e).__name__}), so {what}.)", flush=True)
        return None


def load_peers():
    raw = read_json(HOME / "macs.json", "this Mac starts with no paired Macs")
    peers = {}
    for p in (raw.values() if isinstance(raw, dict) else []):
        try:
            p = clean_peer(p)
            peers[p["id"]] = p
        except Exception:
            pass                              # a record no longer accepted is dropped
    return peers


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
    same_origin_json(request)
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


class PairError(Exception):
    """What to tell the person when pairing did not work."""


def pair_with(link, me):
    """Introduces this Mac (me: who it is) to the Mac whose link this is, and takes on that
    Mac's link, so one pad on the iPad reaches both. Returns (the link's token, the other
    Mac). Blocking: run it off the event loop."""
    u = urlsplit(str(link or "").strip())
    token = u.path.strip("/").split("/")[0]
    if u.scheme not in ("http", "https") or not u.netloc or not token:
        raise PairError("That isn't a PencilPad link. On the other Mac, click Copy next to "
                        "This Mac's link and paste that here.")
    base = f"{u.scheme}://{u.netloc}/{token}"
    ctx = ssl._create_unverified_context()     # the other Mac's own certificate; the link is the secret
    try:
        req = urllib.request.Request(base + "/peer", data=json.dumps(me).encode(),
                                     headers={"Content-Type": "application/json"})
        other = clean_peer(json.loads(urllib.request.urlopen(req, timeout=6, context=ctx).read()))
    except Exception:
        raise PairError("Could not reach PencilPad on that Mac. Is it running there, "
                        "and are both Macs on the same network?") from None
    if other["id"] == me["id"]:
        raise PairError("That is this Mac's own link. Paste the link of the other Mac.")
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    peers = load_peers()
    peers[other["id"]] = other
    save_peers(peers)
    return token, other


def pair(link):
    """./run.sh --pair <link from the other Mac>: share its link and introduce the two."""
    me = {"id": mac_id(), "name": MAC_NAME, "hosts": find_hosts(), "port": PORT, "tls": TLS_PORT}
    try:
        token, other = pair_with(link, me)
    except PairError as e:
        sys.exit(str(e))
    print(f"Paired with {other['name']}. Restart PencilPad on this Mac to use the shared link.")


pairing = asyncio.Lock()


async def pair_handler(request):
    """The Pair button on this Mac's setup page. It pairs at once: the new link works
    straight away and the old one stops (the pads still on it are let go)."""
    check_token(request)
    if not is_local(request):                  # only from this Mac's own browser
        raise web.HTTPNotFound()
    same_origin_json(request)
    if pairing.locked():
        return web.json_response({"error": "Pairing is already in progress. Wait for it to finish."}, status=409)
    try:
        link = (await request.json()).get("link")
    except Exception:
        link = None
    app = request.app
    me = {k: v for k, v in info(app).items() if k in ("id", "name", "hosts", "port", "tls")}
    async with pairing:                        # one at a time: the token is swapped below
        try:
            token, other = await asyncio.get_running_loop().run_in_executor(None, pair_with, link, me)
        except PairError as e:
            return web.json_response({"error": str(e)}, status=400)
        app["token"] = token
        app["peers"] = {i: p for i, p in load_peers().items() if i != app["id"]}
    for ws in list(app["clients"]):
        await ws.close()
    return web.json_response({"name": other["name"], "url": f"/{token}/connect"})


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
    same_origin_json(request)
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
    use_port()
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
        if e.errno == errno.EADDRINUSE and pencilpad_at(PORT):
            sys.exit(f"Port {PORT} is already in use. PencilPad is already running on this Mac.\n"
                     f"Stop the other copy, or pick another port: PENCILPAD_PORT=8766 ./run.sh")
        if e.errno == errno.EADDRINUSE:
            sys.exit(f"Port {PORT} is in use by another program. Free it, or set PENCILPAD_PORT to another "
                     f"port (the iPad's link then changes): PENCILPAD_PORT=8766 ./run.sh")
        raise
    token = load_token()
    prefs.update(load_settings())
    app = web.Application(client_max_size=48 * 1024 * 1024)
    app["token"] = token
    app["clients"] = set()
    app["pads"] = {}                # pad id -> its connection (see ws_handler)
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
        web.post("/{token}/pair", pair_handler),
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
    --bg:#f2f2f7; --card:#fff; --label:#000; --label2:rgba(60,60,67,.8); --sep:rgba(60,60,67,.14);
    --blue:#007aff; --green:#34c759; --orange:#ff9500; --red:#ff3b30; --fill:rgba(118,118,128,.12); }
  @media (prefers-color-scheme: dark) { :root {
    --bg:#000; --card:#1c1c1e; --label:#fff; --label2:rgba(235,235,245,.6); --sep:rgba(84,84,88,.5);
    --blue:#0a84ff; --green:#30d158; --orange:#ff9f0a; --red:#ff453a; --fill:rgba(118,118,128,.24); } }
  body { margin:0; min-height:100vh; display:flex; justify-content:center; background:var(--bg);
    color:var(--label); font:400 17px/1.35 -apple-system, "SF Pro Text", system-ui, sans-serif; -webkit-font-smoothing:antialiased; }
  main { width:min(760px, calc(100vw - 48px)); padding:40px 0; }
  header { text-align:center; margin-bottom:28px; }
  h1 { margin:0 0 6px; font-size:34px; font-weight:700; letter-spacing:-.02em; }
  header p { margin:0; color:var(--label2); }
  h2 { margin:34px 4px 4px; font-size:22px; font-weight:700; letter-spacing:-.01em; }
  h2 + p.sub { margin:0 4px 12px; color:var(--label2); font-size:15px; }
  h3 { margin:0 0 6px; font-size:17px; font-weight:600; }
  .card { background:var(--card); border-radius:22px; margin-bottom:16px; overflow:hidden; }
  .pair { display:flex; gap:32px; align-items:center; padding:28px; }
  .qr { flex:none; padding:10px; background:#fff; border-radius:16px; line-height:0; box-shadow:0 0 0 1px var(--sep); }
  .qr svg { width:196px; height:196px; }
  .lead b { display:block; font-size:21px; font-weight:600; letter-spacing:-.01em; margin-bottom:8px; }
  .lead p { margin:0 0 10px; color:var(--label2); font-size:15px; }
  .lead p.all { color:var(--label); font-size:17px; }
  code { font:500 14px ui-monospace, "SF Mono", Menlo, monospace; user-select:all; -webkit-user-select:all; word-break:break-all; }
  .row { display:flex; align-items:center; gap:12px; min-height:56px; padding:8px 20px; box-sizing:border-box; }
  .row + .row { border-top:1px solid var(--sep); }
  .row .t { flex:1; } .row .t small { display:block; color:var(--label2); font-size:13px; }
  .dot { flex:none; width:11px; height:11px; border-radius:50%; background:var(--orange); }
  .dot.ok { background:var(--green); } .dot.wait { animation:pulse 1.2s ease-in-out infinite; }
  .dot.opt { background:var(--fill); box-shadow:inset 0 0 0 1.5px var(--label2); }   /* optional: not a failure */
  .lead code { display:block; margin-top:6px; word-break:normal; overflow-wrap:normal; }
  .box ol { margin:0 0 12px; padding-left:20px; color:var(--label2); font-size:15px; }
  .box ol li { margin:2px 0; }
  #pairLink { font:400 15px -apple-system, system-ui, sans-serif; }
  .box .small { font-size:13px; margin:8px 0 0; }
  @keyframes pulse { 50% { opacity:.3; } }
  button { height:34px; padding:0 16px; border:0; border-radius:17px; background:var(--fill); color:var(--blue);
    font:600 15px -apple-system, system-ui, sans-serif; cursor:pointer; flex:none; }
  button.primary { background:var(--blue); color:#fff; }
  button:active { opacity:.6; } button:disabled { opacity:.4; cursor:default; } [hidden] { display:none !important; }
  .box { padding:20px; }
  .box + .box { border-top:1px solid var(--sep); }
  .box p { margin:0 0 10px; color:var(--label2); font-size:15px; }
  .box ul { margin:0 0 12px; padding-left:20px; color:var(--label2); font-size:15px; }
  .box li { margin:2px 0; }
  .box .how { color:var(--label); }
  .line { display:flex; gap:10px; align-items:center; }
  .line code, .line input { flex:1; min-width:0; }
  .line code { padding:8px 12px; border-radius:10px; background:var(--fill); }
  input { height:34px; padding:0 12px; border:0; border-radius:10px; background:var(--fill); color:var(--label);
    font:400 15px ui-monospace, "SF Mono", Menlo, monospace; outline:none; }
  input:focus { box-shadow:0 0 0 2px var(--blue); }
  #pairSay { margin:10px 0 0; font-size:15px; min-height:20px; }
  #pairSay.bad { color:var(--red); } #pairSay.ok { color:var(--green); }
  footer { text-align:center; color:var(--label2); font-size:13px; margin-top:20px; }
  @media (max-width:640px) { .pair { flex-direction:column; } }
</style>
</head>
<body>
<main>
  <header><h1>PencilPad</h1><p>Use your iPad and Apple Pencil as a trackpad for __MAC__.</p></header>
  <div class="card pair">
    <div class="qr">__QR__</div>
    <div class="lead">
      <b>Point your iPad's camera at the code</b>
      <p class="all" id="leadLine">Tap the link that appears. PencilPad opens in the iPad's browser and works straight away. That is all the setup there is.</p>
      <p>The iPad must be on the same Wi-Fi as this Mac. No camera handy? Type this into Safari on the iPad: <code>__URLBREAKS__</code></p>
    </div>
  </div>
  <div class="card">
    <div class="row"><i class="dot wait" id="dIpad"></i><div class="t"><span id="tIpad">Waiting for your iPad…</span><small>Scan the code above with the iPad.</small></div></div>
    <div class="row"><i class="dot" id="dAcc"></i><div class="t"><span id="tAcc">Checking Accessibility…</span><small>Needed to move the pointer and press keys.</small></div><button id="bAcc" hidden>Open Settings</button></div>
    <div class="row"><i class="dot" id="dScr"></i><div class="t"><span id="tScr">Checking Screen Recording…</span><small>Only the Screenshot key needs it.</small></div><button id="bScr" hidden>Open Settings</button></div>
    <div class="row" id="rSec" hidden><i class="dot" id="dSec"></i><div class="t"><span id="tSec">Installed as an app</span><small>Full screen, the iPad's microphone, several Macs. See below.</small></div></div>
    <div class="row" id="rPeers" hidden><i class="dot ok"></i><div class="t"><span id="tPeers"></span><small>The iPad app connects to whichever of these it can reach, starting with the one you used last.</small></div></div>
  </div>

  <h2>Optional</h2>
  <p class="sub">PencilPad already works in the iPad's browser. These are extras.</p>
  <div class="card">
    <div class="box" id="appBox">
      <h3>Make it an app on the iPad</h3>
      <ul>
        <li>It opens full screen from the Home Screen.</li>
        <li>It opens even when this Mac was asleep.</li>
        <li>Dictate can use the iPad's microphone.</li>
        <li>One app reaches several Macs.</li>
      </ul>
      <p class="how">To start, on the iPad: open PencilPad, tap the gear at the top, then <b>Install as an app</b>.</p>
    </div>
    <div class="box" id="pairBox">
      <h3>Already use the PencilPad app with another Mac?</h3>
      <p>Pair this Mac with that one, and the same app on your iPad reaches both.</p>
      <ol>
        <li>On the other Mac, open its PencilPad setup page and click Copy next to This Mac's link.</li>
        <li>Paste it here, then click Pair.</li>
      </ol>
      <div class="line"><input id="pairLink" placeholder="Paste the other Mac's link" autocomplete="off" spellcheck="false"><button id="pairGo" class="primary">Pair</button></div>
      <p id="pairSay"></p>
      <p class="small">If you added a PencilPad app from this Mac's own link, delete it after pairing and use the other one.</p>
      <p style="margin:16px 0 6px">This Mac's link: paste it on another Mac's setup page to pair the other way.</p>
      <div class="line"><code id="myLink">__URL__</code><button id="copy">Copy</button></div>
    </div>
  </div>
  <footer>PencilPad keeps running in the terminal. Anyone on your network with this link can control this Mac, so keep it private.</footer>
</main>
<script>
const base = location.pathname.replace(/\/connect$/, '');
const LEAD = document.getElementById('leadLine').textContent;
const local = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
// orange is for what must be done (Accessibility); what is optional gets a hollow ring
function row(id, ok, yes, no) {
  document.getElementById('d' + id).className = 'dot' + (ok ? ' ok' : id === 'Ipad' ? ' wait' : id === 'Acc' ? '' : ' opt');
  document.getElementById('t' + id).textContent = ok ? yes : no;
  const b = document.getElementById('b' + id); if (b) b.hidden = ok;
}
async function poll() {
  try {
    const s = await (await fetch(base + '/state', {cache: 'no-store'})).json();
    row('Ipad', s.clients > 0, 'iPad connected', 'Waiting for your iPad…');
    row('Acc', s.access, 'Accessibility is allowed', 'Accessibility is off: the pointer will not move');
    document.getElementById('leadLine').textContent = s.access ? LEAD
      : 'One more step on this Mac: allow Accessibility below, or the pointer won\'t move.';
    row('Scr', s.screen, 'Screen Recording is allowed', 'Screen Recording is off');
    document.getElementById('rSec').hidden = !s.tls;
    document.getElementById('appBox').hidden = !s.tls;
    row('Sec', s.secure, 'The iPad app is connected', 'Not installed as an app');
    document.getElementById('rPeers').hidden = !s.peers.length;
    document.getElementById('tPeers').textContent = 'Paired with ' + s.peers.join(', ');
  } catch (_) { row('Ipad', false, '', 'PencilPad is not running on this Mac'); }
  setTimeout(poll, 1500);
}
poll();
const settings = pane => fetch(base + '/settings/' + pane, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
document.getElementById('bAcc').onclick = () => settings('accessibility');
document.getElementById('bScr').onclick = () => settings('screen');
const say = (text, cls) => { const p = document.getElementById('pairSay'); p.textContent = text; p.className = cls || ''; };
document.getElementById('copy').onclick = async () => {
  const text = document.getElementById('myLink').textContent;
  try { await navigator.clipboard.writeText(text); }
  catch (_) { getSelection().selectAllChildren(document.getElementById('myLink')); document.execCommand('copy'); }
  document.getElementById('copy').textContent = 'Copied';
  setTimeout(() => { document.getElementById('copy').textContent = 'Copy'; }, 1500);
};
if (!local) {        // pairing changes this Mac's link: only from this Mac's own browser
  document.getElementById('pairLink').disabled = document.getElementById('pairGo').disabled = true;
  say('Pair from the setup page on this Mac itself.');
}
async function pairGo() {
  const link = document.getElementById('pairLink').value.trim();
  if (!link) return say('Paste the other Mac\'s link first.', 'bad');
  document.getElementById('pairGo').disabled = true; say('Pairing…');
  try {
    const r = await fetch(base + '/pair', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                           body: JSON.stringify({link})});
    const out = await r.json();
    if (!r.ok) throw new Error(out.error || 'Could not pair.');
    say('Paired with ' + out.name + '. This Mac now uses that link.', 'ok');
    setTimeout(() => location.replace(out.url), 1200);
  } catch (e) { say(e.message === 'Failed to fetch' ? 'PencilPad is not running on this Mac.' : e.message, 'bad');
    document.getElementById('pairGo').disabled = false; }
}
document.getElementById('pairGo').onclick = pairGo;
document.getElementById('pairLink').addEventListener('keydown', e => { if (e.key === 'Enter') pairGo(); });
</script>
</body>
</html>
"""

SECURE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Install PencilPad as an App</title>
<style>
  :root { color-scheme:light dark;
    --bg:#f2f2f7; --card:#fff; --label:#000; --label2:rgba(60,60,67,.8); --sep:rgba(60,60,67,.14);
    --blue:#007aff; --green:#34c759; --orange:#ff9500; --orange-text:#c93400; --fill:rgba(118,118,128,.12); --warm:rgba(255,149,0,.12); }
  @media (prefers-color-scheme: dark) { :root {
    --bg:#000; --card:#1c1c1e; --label:#fff; --label2:rgba(235,235,245,.6); --sep:rgba(84,84,88,.5);
    --blue:#0a84ff; --green:#30d158; --orange:#ff9f0a; --orange-text:#ff9f0a; --fill:rgba(118,118,128,.24); --warm:rgba(255,159,10,.16); } }
  body { margin:0; background:var(--bg); color:var(--label);
    font:400 17px/1.35 -apple-system, "SF Pro Text", system-ui, sans-serif; -webkit-font-smoothing:antialiased; }
  main { width:min(620px, calc(100vw - 40px)); margin:0 auto; padding:44px 0; }
  h1 { margin:0 0 8px; font-size:30px; font-weight:700; letter-spacing:-.02em; text-align:center; text-wrap:balance; }
  #notnow { display:block; margin:-14px 0 22px; text-align:center; font-size:15px; color:var(--blue); text-decoration:none; }
  header p { margin:0 0 26px; color:var(--label2); text-align:center; }
  .card { background:var(--card); border-radius:22px; padding:24px; margin-bottom:16px; }
  ol { margin:0; padding:0; list-style:none; counter-reset:n; }
  ol li { position:relative; padding:0 0 22px 42px; counter-increment:n; }
  ol li:last-child { padding-bottom:0; }
  ol li::before { content:counter(n); position:absolute; left:0; top:-1px; width:28px; height:28px; border-radius:50%;
    background:var(--fill); font-size:15px; font-weight:600; display:flex; align-items:center; justify-content:center; }
  ol b { font-weight:600; } ol span { display:block; color:var(--label2); font-size:15px; margin-top:2px; }
  /* the step everyone misses: the switch is on the iPad, in a place nobody looks */
  ol li.trust { margin:0 -12px 22px; padding:14px 14px 14px 54px; border-radius:16px; background:var(--warm);
    box-shadow:inset 0 0 0 2px var(--orange); }
  ol li.trust::before { left:14px; top:13px; background:var(--orange); color:#fff; }
  ol li.trust .where { color:var(--label); font-weight:600; }
  ol li.trust .path { color:var(--label); }
  a.btn { display:flex; align-items:center; justify-content:center; height:50px; margin-top:12px; border-radius:14px;
    background:var(--blue); color:#fff; font-weight:600; text-decoration:none; }
  a.btn:active { opacity:.7; }
  .wait { display:flex; align-items:center; gap:10px; color:var(--label2); font-size:15px; }
  .wait i { flex:none; width:10px; height:10px; border-radius:50%; background:var(--blue); animation:pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity:.3; } }
  #hint { color:var(--label); font-size:15px; box-shadow:inset 0 0 0 2px var(--orange); }
  #hint b { color:var(--orange-text); }
  .done h2 { margin:0 0 6px; font-size:22px; color:var(--green); }
  .done p { margin:0 0 4px; color:var(--label2); }
  footer { color:var(--label2); font-size:13px; text-align:center; padding:0 12px; }
  [hidden] { display:none !important; }
</style>
</head>
<body>
<main>
  <header><h1>Install PencilPad as an App</h1>
    <p id="intro">Optional, about two minutes. PencilPad already works in Safari. As an app it opens full screen from the Home Screen, opens even after __MACNAME__ was asleep, can use this iPad's microphone for Dictate, and reaches several Macs.</p></header>
  <a id="notnow" href="#" hidden>Not now — back to PencilPad</a>
  <div class="card" id="checking"><div class="wait"><i></i><span>Checking whether this iPad trusts __MACNAME__ already…</span></div></div>
  <div class="card" id="steps" hidden>
    <ol>
      <li><b>Download the certificate</b><span>Tap the button, then tap Allow.</span>
        <a class="btn" id="get" href="#">Download Certificate</a></li>
      <li><b>Install it</b><span>On this iPad, open the Settings app. Tap Profile Downloaded near the top, then Install.</span></li>
      <li class="trust"><b>Trust it</b>
        <span class="where">Still on this iPad, not on the Mac.</span>
        <span class="path">Settings → General → About → scroll to the bottom → Certificate Trust Settings → turn on <b id="ca"></b>.</span>
        <span>Installing the profile is not enough: nothing works until this switch is on.</span></li>
      <li><b>Come back here</b><span>This page notices by itself once the iPad trusts the certificate.</span></li>
    </ol>
  </div>
  <div class="card" id="waiting" hidden><div class="wait"><i></i><span>Waiting for this iPad to trust the certificate…</span></div></div>
  <div class="card" id="hint" hidden><b>Still waiting?</b> Installing the profile is not enough. The switch in step 3 is on this iPad, in the Settings app, under General → About → Certificate Trust Settings.</div>
  <div class="card done" id="done" hidden><h2>All set</h2>
    <p>This iPad trusts __MACNAME__. Last step: open PencilPad, then tap Share → Add to Home Screen. If you already have a PencilPad icon, delete the old one first.</p>
    <a class="btn" id="open" href="#">Open PencilPad</a></div>
  <div class="card done" id="back" hidden><h2>All set</h2>
    <p>This iPad trusts __MACNAME__ now. Go back to PencilPad: tap Done at the top of this page. Your PencilPad app can reach this Mac from now on.</p></div>
  <div class="card" id="off" hidden><p style="margin:0;color:var(--label2)">The secure connection is off on __MACNAME__. On the Mac, start PencilPad again with ./run.sh, then reload this page.</p>
    <a class="btn" id="plain" href="#">Back to PencilPad</a></div>
  <footer id="foot">The certificate was made by PencilPad on __MACNAME__, and its private key never leaves that Mac. It can only vouch for addresses on your own networks, not for websites.</footer>
</main>
<script>
const CFG = __CFG__;
const HINT_MS = 20000;                    // waiting this long after coming back: say where the switch is
const path = location.pathname.replace(/\/secure$/, '');
const fromApp = new URLSearchParams(location.search).get('from') === 'app';   // opened from the PencilPad app
document.getElementById('ca').textContent = 'PencilPad (' + CFG.mac + ')';
document.getElementById('get').href = path + '/ca.crt';
const pad = 'https://' + location.hostname + ':' + CFG.tls + path;
document.getElementById('open').href = pad + '#install';
document.getElementById('notnow').href = document.getElementById('plain').href = path;   // the pad, as it was
// the intro is for deciding; once done, or impossible, it is in the way
const show = (...ids) => {
  for (const id of ['checking', 'steps', 'waiting', 'hint', 'done', 'back', 'off']) document.getElementById(id).hidden = !ids.includes(id);
  const end = ['done', 'back', 'off'].some(id => ids.includes(id));
  document.getElementById('intro').hidden = end;
  document.getElementById('notnow').hidden = !ids.includes('steps');
  document.getElementById('foot').hidden = ids.includes('off');
};
let seen = Date.now(), ended = false, timer = null;
function done() { ended = true; clearTimeout(timer); show(fromApp ? 'back' : 'done'); }
async function trusted() {
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 2500);
  try { return (await fetch(pad + '/ping', {cache: 'no-store', signal: ctl.signal})).ok; } catch (_) { return false; }
  finally { clearTimeout(t); }
}
async function check() {
  if (ended) return;
  if (await trusted()) return done();
  show('steps', 'waiting', ...(Date.now() - seen > HINT_MS ? ['hint'] : []));
  timer = setTimeout(check, 1500);
}
if (!CFG.tls) show('off');
else if (location.protocol === 'https:') done();
else check();
document.addEventListener('visibilitychange', () => {
  if (document.hidden || ended || !CFG.tls) return;
  seen = Date.now(); clearTimeout(timer); check();
});
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
    --seg-on:#636366; --scrim:rgba(0,0,0,.35);
    --gap:12px;
    --l:calc(env(safe-area-inset-left) + var(--gap)); --r:calc(env(safe-area-inset-right) + var(--gap));
    --t:calc(env(safe-area-inset-top) + var(--gap)); --b:calc(env(safe-area-inset-bottom) + var(--gap));
  }
  @media (prefers-color-scheme: light) {
    :root { color-scheme: light;
      --bg:#f2f2f7; --surface:#fff; --raised:rgba(118,118,128,.12); --raised-edge:rgba(0,0,0,.06);
      --label:#000; --label2:rgba(60,60,67,.8); --label3:rgba(60,60,67,.3);
      --blue:#007aff; --red:#ff3b30; --green:#34c759; --orange:#ff9500;
      --armed:rgba(0,122,255,.10); --material:rgba(255,255,255,.78);
      --seg-on:#fff; --scrim:rgba(0,0,0,.18); }
  }
  [hidden] { display:none !important; }
  html, body { margin:0; height:100%; background:var(--bg); color:var(--label); overflow:hidden;
    font:400 17px/1.3 -apple-system, "SF Pro Text", system-ui, sans-serif; -webkit-font-smoothing:antialiased;
    -webkit-user-select:none; user-select:none; -webkit-touch-callout:none;
    -webkit-tap-highlight-color:transparent; touch-action:none; overscroll-behavior:none; }
  /* --rail: the width of the rail of keys. --s: the safe-area inset on its side. --edge: where
     its touch area ends (folded, still 44px from the screen edge). --padl/--padr: the
     trackpad's insets. --cx: the middle of the trackpad, where its cards and the top bar sit.
     --kbh: the keyboard's height while it is up. body.right puts the rail on the right. */
  @property --rail { syntax:'<length>'; inherits:true; initial-value:0px; }
  body { --rail:clamp(116px, 11vw, 148px); --s:var(--l); --edge:max(calc(var(--s) + var(--rail)), 44px);
    --padl:calc(var(--l) + var(--rail) + var(--gap)); --padr:var(--r); --kbh:0px;
    --cx:calc((var(--padl) + 100vw - var(--padr)) / 2); --padw:calc(100vw - var(--padl) - var(--padr));
    transition:--rail .25s ease; }
  body.right { --s:var(--r); --padl:var(--l); --padr:calc(var(--r) + var(--rail) + var(--gap)); }
  body.collapsed { --rail:26px; }

  /* the pad takes every touch beside the rail (above the keyboard while it is up);
     the surface is only what you see */
  #pad { position:fixed; top:0; right:0; bottom:0; left:var(--edge); touch-action:none; }
  body.right #pad { left:0; right:var(--edge); }
  #surface { position:fixed; left:var(--padl); right:var(--padr); top:var(--t); bottom:var(--b);
    background:var(--surface); border-radius:36px; pointer-events:none;
    box-shadow:inset 0 0 0 1px var(--raised-edge);
    transition:background-color .18s ease, box-shadow .18s ease, opacity .25s ease; }
  body.typing #pad { bottom:calc(var(--b) + var(--kbh)); }
  body.typing #surface { bottom:calc(var(--b) + var(--kbh)); border-radius:36px 36px 0 0; }
  #pad.armed + #surface { background:color-mix(in srgb, var(--surface), var(--blue) 16%);
    box-shadow:inset 0 0 0 2.5px var(--blue); }
  body.offline #surface { opacity:.45; }

  /* the rail: big keys down one edge, hit without looking. Its gaps and margins take
     no touches, so a near miss is not a click. The handle at the bottom folds it. */
  #rail { --rg:8px; position:fixed; left:0; top:0; bottom:0; width:var(--edge);
    padding:var(--t) 0 var(--b) var(--s); box-sizing:border-box;
    display:flex; flex-direction:column; align-items:flex-start; gap:var(--rg); touch-action:none; }
  body.right #rail { left:auto; right:0; padding:var(--t) var(--s) var(--b) 0; align-items:flex-end; }
  @media (max-height:900px) { #rail { --rg:6px; } }
  #rail .keys { flex:1 1 0; min-height:0; width:var(--rail); display:flex; flex-direction:column; gap:var(--rg);
    transition:opacity .12s ease, visibility 0s linear 0s; }
  body.collapsed #rail .keys { opacity:0; visibility:hidden; pointer-events:none;
    transition:opacity .12s ease, visibility 0s linear .12s; }
  #rail .key { flex:1 1 0; min-height:0; overflow:hidden; container-type:size;
    display:flex; flex-direction:column; align-items:center; justify-content:center; gap:7px;
    background:var(--raised); color:var(--label); border-radius:22px; box-sizing:border-box;
    box-shadow:inset 0 0 0 1px var(--raised-edge); touch-action:none; text-align:center;
    font-size:16px; font-weight:600; letter-spacing:-.01em;
    transition:background-color .15s ease, color .15s ease, box-shadow .15s ease, transform .15s ease, opacity .25s ease; }
  #rail .key.off { display:none; }
  #rail svg { flex:none; width:30px; height:30px; fill:none; stroke:currentColor; stroke-width:2;
    stroke-linecap:round; stroke-linejoin:round; }
  #rail .key span, #rail .key small { max-width:100%; box-sizing:border-box; padding:0 6px; }
  /* a note (Listening…, Update the Mac) takes the icon's place, two lines at most */
  #rail .key small { font-size:13px; line-height:1.2; font-weight:400; color:var(--label2); margin-top:-3px;
    display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; transition:color .15s ease; }
  #rail .key small:empty { display:none; }
  #rail .key.noted svg { display:none; }
  @container (max-height:88px) {
    #rail .key svg { width:24px; height:24px; }
    #rail .key span { font-size:15px; } #rail .key small.hint { display:none; } }
  /* (a key is the container of its own icon and label, so it cannot set its own gap: the
     icon's margin narrows it to 4px instead) */
  @container (max-height:72px) {
    #rail .key svg { width:20px; height:20px; margin-bottom:-3px; } #rail .key span { font-size:14px; } }
  @container (max-height:60px) {
    #rail .key svg { width:20px; height:20px; } #rail .key span { font-size:13px; } }
  @container (max-height:52px) { #rail .key svg { display:none; } }
  #rail .key.on { background:var(--blue); color:#fff; transform:scale(.97); }
  #rail .key.on small { color:rgba(255,255,255,.8); }
  #rail #dict.on { background:var(--red); }
  #rail .key.lit { background:var(--armed); box-shadow:inset 0 0 0 2px var(--blue); }
  #rail .key.lit svg { color:var(--blue); }
  #rail .key.old > span { opacity:.45; }
  #rail .key.old small { color:var(--label2); }
  #rail .key.gtop { margin-top:8px; }
  body.offline #rail .key { opacity:.35; }
  /* the handle: folds the rail on lift; folded, the whole slim strip brings it back */
  #rail::before { content:""; position:absolute; top:var(--t); bottom:var(--b); left:var(--s); width:var(--rail);
    border-radius:13px; background:rgba(118,118,128,.24); opacity:0; transition:opacity .12s ease; pointer-events:none; }
  body.right #rail::before { left:auto; right:var(--s); }
  body.collapsed #rail::before { opacity:1; transition-delay:.1s; }
  #railToggle { position:relative; flex:none; width:var(--rail); height:44px; display:flex; align-items:center; justify-content:center;
    border-radius:18px; background:var(--raised); color:var(--label2); box-sizing:border-box;
    box-shadow:inset 0 0 0 1px var(--raised-edge); touch-action:none; transition:background-color .15s ease, box-shadow .15s ease; }
  #railToggle svg { width:18px; height:18px; stroke-width:2.2; transition:transform 0s linear .25s; }
  #railToggle.down { background:var(--armed); }
  body.collapsed #railToggle { background:transparent; box-shadow:none; }
  body.collapsed #railToggle svg, body.right #railToggle svg { transform:scaleX(-1); }
  body.right.collapsed #railToggle svg { transform:none; }

  /* the keyboard, in the lower part of the trackpad. Keys fill the width; rows are half
     as tall again as a key is wide, 96 to 112px (so each arrow half is at least 44px),
     anchored to the bottom. Above it the trackpad goes on working. */
  #keyboard { position:fixed; left:var(--padl); right:var(--padr); bottom:var(--b);
    background:var(--surface); border-radius:0 0 36px 36px; container-type:inline-size; touch-action:none;
    box-shadow:inset 1px 0 var(--raised-edge), inset -1px 0 var(--raised-edge), inset 0 -1px var(--raised-edge);
    visibility:hidden; transition:opacity .25s ease; }
  body.typing #keyboard { visibility:visible; }
  body.typing #hint { visibility:hidden; }
  body.offline #keyboard { opacity:.45; }
  #keyboard .rows { --g:8px; --p:14px; --u:calc((100cqw - 2 * var(--p) - 13.5 * var(--g)) / 14.5);
    --h:min(calc((100vh - var(--t) - var(--b) - 120px - 4 * var(--g) - 2 * var(--p)) / 5),
            clamp(96px, calc(var(--u) * 1.5), 112px));
    display:flex; flex-direction:column; gap:var(--g); padding:var(--p); }
  #keyboard .row { display:flex; gap:var(--g); height:var(--h); }
  .cap { --w:1; position:relative; flex:none; width:calc(var(--w) * var(--u) + (var(--w) - 1) * var(--g));
    display:flex; flex-direction:column; align-items:center; justify-content:center; gap:1px;
    background:var(--raised); color:var(--label); border-radius:12px; box-sizing:border-box;
    box-shadow:inset 0 0 0 1px var(--raised-edge); touch-action:none;
    font-size:24px; font-weight:400; transition:background-color .1s ease, color .1s ease, box-shadow .1s ease; }
  .cap.word { font-size:14px; font-weight:500; color:var(--label2); }
  .cap.word i { font-style:normal; font-size:19px; color:var(--label); line-height:1; }
  .cap svg { width:14px; height:14px; fill:currentColor; }
  .cap svg.sh { width:18px; height:18px; fill:none; stroke:currentColor; stroke-width:1.6; stroke-linejoin:round; }
  .cap.armed svg.sh, .cap.locked svg.sh { fill:currentColor; }
  .cap[data-k=up] svg { transform:rotate(90deg); } .cap[data-k=right] svg { transform:rotate(180deg); }
  .cap[data-k=down] svg { transform:rotate(-90deg); }
  @container (max-width:760px) { #keyboard .rows { --g:6px; --p:8px; }
    .cap.word span { display:none; } .cap.word i { font-size:22px; } }
  #keyboard .arrows { display:flex; gap:var(--g); }
  #keyboard .ud { display:flex; flex-direction:column; gap:var(--g); }
  #keyboard .ud .cap { height:calc((var(--h) - var(--g)) / 2); }
  /* pressed; armed for the next key (tapped once: tint, ring, blue glyph); locked (tapped twice) */
  .cap.armed { background:var(--armed); box-shadow:inset 0 0 0 2px var(--blue); color:var(--label); }
  .cap.armed i { color:var(--blue); }
  .cap.on, .cap.locked { background:var(--blue); box-shadow:none; }
  .cap.on, .cap.on i, .cap.locked, .cap.locked i { color:#fff; }
  .cap.on { transition:none; }
  .cap.locked::after { content:""; position:absolute; left:50%; bottom:7px; width:16px; height:3px; margin-left:-8px;
    border-radius:2px; background:currentColor; }

  /* connection status */
  #status { display:flex; align-items:center; gap:8px; height:36px; padding:0 16px 0 13px; border-radius:18px;
    max-width:calc(var(--padw) - 140px); box-sizing:border-box;
    background:var(--material); -webkit-backdrop-filter:blur(24px) saturate(180%); backdrop-filter:blur(24px) saturate(180%);
    box-shadow:inset 0 0 0 1px var(--raised-edge);
    font-size:15px; font-weight:500; color:var(--label); white-space:nowrap; pointer-events:auto; touch-action:none; }
  #statusText { overflow:hidden; text-overflow:ellipsis; min-width:0; }
  #dot { flex:none; width:9px; height:9px; border-radius:50%; background:var(--green); transition:background-color .2s ease; }
  body.offline #dot, body.notice #dot { background:var(--orange); }
  body.offline #dot { animation:pulse 1.2s ease-in-out infinite; }
  #pad.armed ~ #status #dot { background:var(--blue); }
  @keyframes pulse { 50% { opacity:.3; } }

  /* gesture guide */
  #hint { position:fixed; left:var(--cx); top:50%; transform:translate(-50%,-50%);
    display:grid; grid-template-columns:auto auto; column-gap:18px; row-gap:9px; align-items:baseline;
    font-size:15px; pointer-events:none; transition:opacity .25s ease; }
  #hint b { font-weight:600; color:var(--label); text-align:right; }
  #hint span { color:var(--label2); }
  body.busy #hint, body.offline #hint { opacity:0; }

  /* shown while the Mac cannot be reached */
  #lost { position:fixed; left:var(--cx); top:50%; transform:translate(-50%,-50%); width:min(340px, calc(var(--padw) - 32px));
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
  #top { position:fixed; top:calc(var(--t) + 18px); left:var(--cx); transform:translateX(-50%);
    display:flex; align-items:center; gap:8px; pointer-events:none; }
  #help, #gear { position:relative; flex:none; width:36px; height:36px; border:0; border-radius:50%; padding:0; pointer-events:auto;
    background:var(--material); -webkit-backdrop-filter:blur(24px) saturate(180%); backdrop-filter:blur(24px) saturate(180%);
    box-shadow:inset 0 0 0 1px var(--raised-edge); color:var(--label);
    font:600 17px -apple-system, system-ui, sans-serif; touch-action:none;
    display:flex; align-items:center; justify-content:center; }
  #gear svg { width:20px; height:20px; fill:none; stroke:currentColor; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round; }
  #help::after, #gear::after { content:""; position:absolute; inset:-4px; }
  #help:active, #gear:active { opacity:.6; }

  /* the list of Macs, opened from the status pill */
  #macs { position:fixed; top:calc(var(--t) + 64px); left:var(--cx); transform:translateX(-50%) scale(.96); width:min(360px, calc(var(--padw) - 32px));
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
  #macs .fix { min-height:44px; margin-top:-6px; padding-left:28px; }
  #macs .fix b { color:var(--blue); font-size:15px; }
  #macs p { margin:6px 14px 8px; font-size:13px; color:var(--label2); }

  /* the settings sheet, opened from the gear. The scrim under it takes every other touch. */
  #scrim { position:fixed; inset:0; display:none; touch-action:none; }
  body.setting #scrim { display:block; background:var(--scrim); }
  #sheet { position:fixed; top:calc(var(--t) + 16px); left:var(--cx); transform:translateX(-50%) scale(.96);
    width:min(480px, calc(var(--padw) - 32px)); max-height:calc(100vh - var(--t) - var(--b) - 32px);
    display:flex; flex-direction:column; border-radius:22px; box-sizing:border-box;
    background:var(--material); -webkit-backdrop-filter:blur(30px) saturate(180%); backdrop-filter:blur(30px) saturate(180%);
    box-shadow:inset 0 0 0 1px var(--raised-edge), 0 18px 50px rgba(0,0,0,.28);
    opacity:0; visibility:hidden; pointer-events:none; transition:opacity .2s ease, transform .2s ease, visibility 0s linear .2s; }
  body.setting #sheet { opacity:1; visibility:visible; pointer-events:auto; transform:translateX(-50%); transition-delay:0s; }
  #sheet .head { flex:none; display:flex; align-items:center; justify-content:space-between; padding:6px 8px 6px 20px;
    border-bottom:1px solid var(--raised-edge); touch-action:none; }
  #sheet h1 { margin:0; font-size:17px; font-weight:600; }
  #sheet button { border:0; background:none; font:inherit; color:var(--blue); padding:0; touch-action:manipulation; }
  #sheet #done { min-width:64px; height:44px; font-weight:600; }
  #sheetBody { flex:1 1 auto; overflow-y:auto; -webkit-overflow-scrolling:touch; overscroll-behavior:contain;
    touch-action:pan-y; padding:4px 16px 20px; }
  #sheet h2 { margin:18px 4px 8px; font-size:13px; font-weight:600; color:var(--label2); text-transform:uppercase; letter-spacing:.04em; }
  #sheet .group { background:var(--raised); border-radius:14px; overflow:hidden; }
  #sheet .set { display:flex; align-items:center; gap:12px; min-height:52px; padding:6px 14px; box-sizing:border-box;
    font-size:16px; }
  #sheet .set + .set { border-top:1px solid var(--raised-edge); }
  #sheet .set.col { flex-direction:column; align-items:stretch; gap:8px; padding:12px 14px; }
  #sheet .set .name { flex:1; min-width:0; }
  #sheet .line { display:flex; gap:12px; }
  #sheet .set .val { color:var(--label2); font-variant-numeric:tabular-nums; }
  #sheet .set.dis { opacity:.45; pointer-events:none; }
  #sheet .note { margin:8px 4px 0; font-size:13px; color:var(--label2); }
  #sheet .note:empty { display:none; }
  #sheet .say { margin:0; font-size:13px; color:var(--label2); }
  #sheet .banner { display:flex; align-items:flex-start; gap:10px; margin:14px 4px 0; font-size:15px; color:var(--label); }
  #sheet .banner i { flex:none; width:9px; height:9px; margin-top:6px; border-radius:50%; background:var(--orange); }
  .switch { flex:none; position:relative; width:51px; height:31px; border-radius:16px; background:var(--label3);
    transition:background-color .2s ease; }
  .switch::after { content:""; position:absolute; left:2px; top:2px; width:27px; height:27px; border-radius:50%;
    background:#fff; box-shadow:0 2px 6px rgba(0,0,0,.25); transition:transform .2s ease; }
  .set.on .switch { background:var(--green); } .set.on .switch::after { transform:translateX(20px); }
  .slider { position:relative; height:44px; touch-action:none; }
  .slider::before, .slider i { content:""; position:absolute; left:0; right:0; top:20px; height:4px; border-radius:2px; background:var(--label3); }
  .slider i { right:auto; background:var(--blue); }
  .slider b { position:absolute; top:8px; width:28px; height:28px; margin-left:-14px; border-radius:50%; background:#fff;
    box-shadow:0 0 0 .5px rgba(0,0,0,.08), 0 2px 6px rgba(0,0,0,.25); transition:transform .1s ease; }
  .slider.dragging b { transform:scale(1.15); }
  .seg { display:flex; gap:4px; padding:3px; border-radius:11px; background:var(--raised); }
  #sheet .seg button, #sheet .chips button { flex:1; min-height:44px; border-radius:9px; color:var(--label);
    font-size:15px; font-weight:500; padding:0 8px; }
  #sheet .seg button.cur, #sheet .chips button.cur { background:var(--seg-on); box-shadow:0 1px 4px rgba(0,0,0,.18); }
  #sheet .chips { display:flex; flex-direction:column; gap:6px; }
  #sheet .chips div { display:grid; grid-template-columns:repeat(var(--n), minmax(0, 1fr)); gap:6px; }
  #sheet .chips button { background:var(--raised); }
  #sheet .chips button.cur { box-shadow:inset 0 0 0 2px var(--blue), 0 1px 4px rgba(0,0,0,.18); }
  #sheet .act { color:var(--blue); }

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
  #tour { position:fixed; left:var(--cx); top:50%; transform:translate(-50%,-50%) scale(.96); width:min(400px, calc(var(--padw) - 40px));
    padding:22px 24px 16px; border-radius:28px; text-align:center; pointer-events:none;
    background:var(--material); -webkit-backdrop-filter:blur(30px) saturate(180%); backdrop-filter:blur(30px) saturate(180%);
    box-shadow:inset 0 0 0 1px var(--raised-edge), 0 18px 50px rgba(0,0,0,.28);
    opacity:0; visibility:hidden; transition:opacity .25s ease, transform .25s ease, visibility 0s linear .25s; }
  body.touring:not(.offline) #tour { opacity:1; visibility:visible; transform:translate(-50%,-50%); transition-delay:0s; }
  body.touring #hint { opacity:0; }
  #tour .step { font-size:13px; font-weight:600; color:var(--label2); text-transform:uppercase; letter-spacing:.04em; }
  #tour h1 { margin:14px 0 6px; font-size:22px; font-weight:700; letter-spacing:-.02em; }
  #tour p { margin:0 auto; max-width:320px; font-size:15px; color:var(--label2); min-height:40px; }
  #tour .note { margin-top:12px; font-size:13px; color:var(--label2); min-height:0; }
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
  #demo .rail { position:absolute; left:8px; top:8px; bottom:8px; width:34px; display:flex; flex-direction:column; gap:3px; opacity:0; }
  body.right #demo .rail { left:auto; right:8px; }
  #demo .rail i { flex:1; border-radius:3px; background:var(--label3); }
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
  #demo[data-s=rail] .rail { opacity:1; }
  #demo[data-s=rail] .rail i { animation:d-key 4s linear infinite; opacity:.35; }
  #demo[data-s=rail] .rail i:nth-child(2) { animation-delay:.4s; } #demo[data-s=rail] .rail i:nth-child(3) { animation-delay:.8s; }
  #demo[data-s=rail] .rail i:nth-child(4) { animation-delay:1.2s; } #demo[data-s=rail] .rail i:nth-child(5) { animation-delay:1.6s; }
  #demo[data-s=rail] .rail i:nth-child(6) { animation-delay:2s; } #demo[data-s=rail] .rail i:nth-child(7) { animation-delay:2.4s; }
  #demo[data-s=rail] .rail i:nth-child(8) { animation-delay:2.8s; } #demo[data-s=rail] .rail i:nth-child(9) { animation-delay:3.2s; }
  #demo[data-s=rail] .rail i:nth-child(10) { animation-delay:3.6s; }
  #demo[data-s=home] .share, #demo[data-s=done] .tick { opacity:1; }
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
  <b>Pinch</b><span>Zoom</span>
</div>
<div id="top"><div id="status"><i id="dot"></i><span id="statusText">Connecting…</span></div><button id="help" aria-label="Show the tour">?</button><button id="gear" aria-label="Settings"><svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="2.6"/><path d="M8.6 2.2h2.8l.4 2.1 1.5.8 2-.8 1.4 2.4-1.6 1.4v1.8l1.6 1.4-1.4 2.4-2-.8-1.5.8-.4 2.1H8.6l-.4-2.1-1.5-.8-2 .8-1.4-2.4 1.6-1.4V8.1L3.3 6.7l1.4-2.4 2 .8 1.5-.8z"/></svg></button></div>
<div id="keyboard"><div class="rows"></div></div>
<div id="rail"><div class="keys">
  <div class="key" id="esc" data-k="esc"><svg viewBox="0 0 34 34"><path d="M16.3 8.9A10.5 10.5 0 1 1 8.9 16.3"/><path d="M17 17 6 6M6 14V6h8"/></svg><span>Escape</span><small></small></div>
  <div class="key" id="space" data-k="space"><svg viewBox="0 0 34 34"><path d="M5 12v8a3 3 0 0 0 3 3h18a3 3 0 0 0 3-3v-8"/></svg><span>Space</span><small></small></div>
  <div class="key" id="sub" data-k="return"><svg viewBox="0 0 34 34"><path d="M27 7v9a4 4 0 0 1-4 4H7M13 14l-6 6 6 6"/></svg><span>Return</span><small></small></div>
  <div class="key" id="copy"><svg viewBox="0 0 34 34"><rect x="12" y="11" width="16" height="18" rx="3"/><path d="M7 23V9a3 3 0 0 1 3-3h12"/></svg><span>Copy</span><small></small></div>
  <div class="key" id="paste"><svg viewBox="0 0 34 34"><path d="M12 6.5H10a3 3 0 0 0-3 3V27a3 3 0 0 0 3 3h14a3 3 0 0 0 3-3V9.5a3 3 0 0 0-3-3h-2"/><rect x="12" y="4" width="10" height="5" rx="1.5"/></svg><span>Paste</span><small></small></div>
  <div class="key" id="dict"><svg viewBox="0 0 34 34"><rect x="12.5" y="4" width="9" height="16" rx="4.5"/><path d="M7.5 16.5a9.5 9.5 0 0 0 19 0M17 26v4M12.5 30h9"/></svg><span>Dictate</span><small id="dictSub"></small></div>
  <div class="key" id="shot"><svg viewBox="0 0 34 34"><path d="M5 11V8a3 3 0 0 1 3-3h3M23 5h3a3 3 0 0 1 3 3v3M29 23v3a3 3 0 0 1-3 3h-3M11 29H8a3 3 0 0 1-3-3v-3"/><circle cx="17" cy="17" r="5"/></svg><span>Screenshot</span><small></small></div>
  <div class="key" id="mc"><svg viewBox="0 0 34 34"><rect x="4" y="6" width="11.5" height="9.5" rx="2"/><rect x="18.5" y="6" width="11.5" height="9.5" rx="2"/><rect x="4" y="18.5" width="11.5" height="9.5" rx="2"/><rect x="18.5" y="18.5" width="11.5" height="9.5" rx="2"/></svg><span>Windows</span><small></small></div>
  <div class="key" id="app"><svg viewBox="0 0 34 34"><path d="M6 11.5h21M22 6.5l5 5-5 5M28 22.5H7M12 17.5l-5 5 5 5"/></svg><span>Switch</span><small></small></div>
  <div class="key" id="kbd"><svg viewBox="0 0 34 34"><rect x="3" y="8" width="28" height="18" rx="3.5"/><path d="M8 13.5h.01M12.5 13.5h.01M17 13.5h.01M21.5 13.5h.01M26 13.5h.01M8 17.5h.01M12.5 17.5h.01M17 17.5h.01M21.5 17.5h.01M26 17.5h.01M11 21.5h12"/></svg><span>Keyboard</span><small></small></div>
</div>
  <div id="railToggle" role="button" aria-label="Fold the keys away"><svg viewBox="0 0 18 18"><path d="M11 3.5 5.5 9l5.5 5.5"/></svg></div>
</div>
<div id="macs"><div id="macList"></div>
  <div class="mac more" id="secure" hidden><b>Install as an app…</b></div>
  <p id="macNote"></p></div>
<div id="scrim"></div>
<div id="sheet" role="dialog" aria-label="Settings">
  <div class="head"><h1>Settings</h1><button id="done">Done</button></div>
  <div id="sheetBody"></div>
</div>
<div id="lost"><div id="spin"></div><h1 id="lostTitle">Looking for your Mac…</h1>
  <p>Keep PencilPad running on the Mac and this iPad on the same Wi-Fi. It reconnects on its own.</p>
  <button id="reload">Reload</button></div>
<svg id="ring" viewBox="0 0 76 76"><circle cx="38" cy="38" r="34"/></svg>
<div id="tour">
  <div class="step" id="tourStep"></div>
  <div id="demo"><span class="rail"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></span><i class="f f1"></i><i class="f f2"></i>
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
let cur = null;           // the Mac this pad is connected to: {id, name, base, speech, caps, settings}
let notice = '', noticeTimer = null;
function showStatus(armed) {
  const name = cur ? cur.name : CFG.mac;
  statusText.textContent = armed ? 'Lift to right-click · move to drag'
    : online && notice ? notice : online ? (name ? 'Connected to ' + name : 'Connected') : 'Reconnecting…';
  document.body.classList.toggle('notice', online && !!notice && !armed);
}
// A Mac running an older PencilPad does not know the newer keys. Say so rather than do
// nothing. (Before its hello arrives, assume it does.)
function macCan(c) { return !cur || !cur.caps || cur.caps.includes(c); }
function macName() { return cur && cur.name || CFG.mac || 'this Mac'; }
function outdated(what) {
  clearTimeout(noticeTimer);
  notice = what + ' needs PencilPad updated on ' + macName();
  noticeTimer = setTimeout(() => { notice = ''; showStatus(pad.classList.contains('armed')); }, 3500);
  showStatus(false);
}
// the rail keys that need something an older Mac does not have
const NEEDS = {space: 'k', copy: 'clip', paste: 'clip', mc: 'mc', app: 'app', kbd: 'k'};
const KEYNAMES = {esc: 'Escape', space: 'Space', sub: 'Return', copy: 'Copy', paste: 'Paste', dict: 'Dictate',
                  shot: 'Screenshot', mc: 'Windows', app: 'Switch', kbd: 'Keyboard'};
function markOld() {
  for (const [id, c] of Object.entries(NEEDS)) {
    const el = document.getElementById(id), old = !macCan(c);
    el.classList.toggle('old', old); el.classList.toggle('noted', old);
    el.querySelector('small').textContent = old ? 'Update the Mac' : '';
  }
}
// The Mac's own settings (it acts on them): from its hello, or this page's Mac before then.
// An older Mac sends none; then the defaults below apply.
const MAC_DEFAULTS = {pointer_speed: 1.6, scroll_speed: 1.4, scroll_direction: 'natural', hover: false,
                      dictation_source: 'ipad', dictation_key: 'f13', dictation_mode: 'hold', screenshot_paste: true};
function macSet(k) {
  const st = cur ? cur.settings : CFG.settings;
  return st && k in st ? st[k] : MAC_DEFAULTS[k];
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
// Dictate's label: a note while there is one (in place of the icon), else how to use it
function dictLabel(note, ms) {
  clearTimeout(dictNote);
  dictSub.textContent = note || (macSet('dictation_mode') === 'toggle' ? 'Tap to start or stop' : 'Hold to talk');
  dictSub.classList.toggle('hint', !note);
  document.getElementById('dict').classList.toggle('noted', !!note);
  if (note && ms) dictNote = setTimeout(dictLabel, ms);
}
// Tolerances. SLOP_*: how far (px, straight line from where it landed) the tip may
// wander and still count as a tap or a hold. HOLD_MS: hold-still time before the pad arms.
// Two fingers become a pinch once the gap between them has changed by PINCH_LOCK px, and
// by more than they have travelled together; each further PINCH_STEP of change is one zoom.
const HOLD_MS = 500, TWO_TAP_MS = 600, SLOP_PEN = 18, SLOP_TOUCH = 20, TWO_SLOP = 20;
const PINCH_LOCK = 30, PINCH_STEP = 1.3, PINCH_EVERY_MS = 120, PINCH_MAX = 6;

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

// ---- connection. PAD_ID names this page, once per load and the same across reconnects,
// so the Mac can tell this page on a new socket from another pad.
const PAD_ID = Array.from(crypto.getRandomValues(new Uint8Array(9)), b => (b % 36).toString(36)).join('');
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
  const sock = ws = new WebSocket(to.base.replace(/^http/, 'ws') + PATH + '/ws?pad=' + PAD_ID);
  let opened = false;
  ws.onopen = () => {
    if (sock !== ws) return;
    opened = true; connect.failed = false;
    cur = {id: to.m.id, name: to.m.name, base: to.base, speech: false, caps: null,
           settings: to.m.id === CFG.self.id ? CFG.settings : null};
    markOld(); settingsChanged();
    setOnline(true);
    if (!tour && !store('pencilpad-tour')) tourStart();
    else if (!tour && askHome) { askHome = false; tourStart([HOME]); }
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
    if (hello && hello.settings && cur) { cur.settings = hello.settings; settingsChanged(); }   // changed on another pad
    if (!hello || !hello.boot) return;
    if (cur) {
      cur.caps = Array.isArray(hello.caps) ? hello.caps : [];
      cur.settings = hello.settings || null;
      if (Array.isArray(hello.dictKeys)) cur.dictKeys = hello.dictKeys;
      markOld(); settingsChanged();
    }
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
    // Over the secure connection, a paired Mac this iPad does not trust yet cannot be
    // reached at all. Its own install page sets that up (in the app: an in-app browser).
    if (SECURE && !here && ok === false && m.tls) {
      const fix = document.createElement('div');
      fix.className = 'mac fix';
      fix.innerHTML = '<b>Set up its certificate</b>';
      fix.addEventListener('pointerdown', e => {
        e.preventDefault(); e.stopPropagation();
        pickClose(); location.href = installUrl(m) + '?from=app';
      });
      macList.append(fix);
    }
  }
}
function pickOpen() {
  const reach = {};
  document.getElementById('secure').hidden = SECURE || !CFG.self.tls;
  document.querySelector('#secure b').textContent = navigator.standalone ? 'Set up the secure connection…' : 'Install as an app…';
  document.getElementById('macNote').textContent = macs.length > 1
    ? 'PencilPad connects to the Mac you chose last, or to another one when that Mac is not reachable.'
    : SECURE ? 'To add another Mac, use the Pair box on that Mac’s setup page.' : '';
  pickDraw(reach);
  document.body.classList.add('picking');
  for (const m of macs) find([m], 1500).then(t => { reach[m.id] = !!t; if (document.body.classList.contains('picking')) pickDraw(reach); });
}

// ---- batched movement, one message per frame
let mvx = 0, mvy = 0, scx = 0, scy = 0, queued = false;
function flush() {
  queued = false;
  if (mvx || mvy) { send({t:'m', x:mvx, y:mvy}); mvx = mvy = 0; }
  if (scx || scy) { ptrUse(); send({t:'s', x:scx, y:scy}); scx = scy = 0; }
}
function queue() { if (!queued) { queued = true; requestAnimationFrame(flush); } }

// ---- pad gestures
const pts = new Map();   // pointerId -> {x, y, x0, y0} for pointers in the current gesture
let g = null;            // the current gesture
let holdTimer = null;
let hover = null;

const railResets = [];    // release whatever a rail key holds
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
  for (const r of railResets) r();
  kbdRelease();
}
// palm rejection: a Pencil on the pad or on a key
function penDown() { return (g !== null && g.pen) || kbdPen(); }
function cancelGesture() { endDrag(); pts.clear(); g = null; clearTimeout(holdTimer); ringOff(); arm(false); ptrDone(); }
// A click, drag or scroll takes the modifiers that are on on the keyboard (Shift-click,
// Command-click, Option-drag): they go down on the Mac first and stay for the gesture,
// and a one-shot one is used up when it ends.
let ptr = [];
function ptrUse() {
  if (!document.body.classList.contains('typing') || tour) return;
  for (const n of MODS) if (mod[n].state !== 'off' || held(n)) {
    if (!mod[n].mac) macMod(n, true);
    if (!ptr.includes(n)) ptr.push(n);
    if (held(n)) mod[n].used = true;          // held for a click: not a tap when it lifts
  }
}
function ptrDone() {
  if (!ptr.length) return;
  ptr = [];
  for (const n of MODS) if (mod[n].state === 'armed') mod[n].state = 'off';
  syncMods(); drawKeys();
}
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
    g.two = pts.size === 2 ? two() : null;
  }
});
// the two fingers of a two-finger gesture: the gap between them and their midpoint
function two() {
  const [a, b] = pts.values();
  return {d: Math.hypot(a.x - b.x, a.y - b.y), x: (a.x + b.x) / 2, y: (a.y + b.y) / 2};
}
function pinchOn() { return store('pencilpad-pinch') !== 'off'; }
// A two-finger gesture is a scroll or a pinch for the whole of its life. Scroll when the
// fingers travel together past the slop first; pinch when the gap between them changes more.
function lockTwo() {
  const n = two(), d = Math.abs(n.d - g.two.d), moved = Math.hypot(n.x - g.two.x, n.y - g.two.y);
  if (!pinchOn()) { if (g.spread > TWO_SLOP) g.mode = 'scroll'; }
  else if (d > PINCH_LOCK && d > moved) g.mode = 'pinch', g.zoom = {base: g.two.d, gap: n.d, sent: 0, at: 0};
  else if (moved > TWO_SLOP) g.mode = 'scroll';
}
// The steps a pinch is worth come from the whole change in the gap since it began, so a
// fast pinch and a slow one of the same size zoom alike. They go out one per
// PINCH_EVERY_MS, and what is still owed when the fingers lift follows, PINCH_MAX at most.
function pinchStep() { g.zoom.gap = two().d; zoomOwed(g.zoom); }
function zoomOwed(z) {
  clearTimeout(z.timer);
  const want = Math.max(-PINCH_MAX, Math.min(PINCH_MAX, Math.trunc(Math.log(z.gap / z.base) / Math.log(PINCH_STEP))));
  if (want === z.sent) return;
  const wait = z.at + PINCH_EVERY_MS - performance.now();
  if (wait > 0) { z.timer = setTimeout(() => zoomOwed(z), wait); return; }
  const d = Math.sign(want - z.sent);
  z.at = performance.now(); z.sent += d;
  if (!tour && !macCan('zoom')) outdated('Zoom');
  else send({t:'zoom', d});
  if (want !== z.sent) z.timer = setTimeout(() => zoomOwed(z), PINCH_EVERY_MS);
}

pad.addEventListener('pointermove', e => {
  const p = pts.get(e.pointerId);
  if (!p) {
    // Pencil hovering above the glass
    if (macSet('hover') && e.pointerType === 'pen' && !g) {
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
      if (g.armed) { g.dragging = true; ptrUse(); send({t:'d'}); }
    }
    mvx += dx; mvy += dy; queue();
  } else if (g.max >= 2) {
    g.spread = Math.max(g.spread, dist);
    if (!g.mode && pts.size === 2 && g.two) lockTwo();
    else if (!g.mode && g.spread > TWO_SLOP) g.mode = 'scroll';      // three fingers: as they always did
    if (g.mode === 'scroll' && pts.size >= 2) { scx += dx / pts.size; scy += dy / pts.size; queue(); }
    else if (g.mode === 'pinch' && pts.size === 2) pinchStep();
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
  else if (g.max === 1 && g.armed && !g.moving) { ptrUse(); send({t:'r'}); }
  else if (g.max === 1 && !g.moving) { ptrUse(); send({t:'c'}); }
  else if (g.max === 2 && !g.moving && !g.mode && g.spread <= TWO_SLOP && dur < TWO_TAP_MS) { ptrUse(); send({t:'r'}); }
  ptrDone();
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
  {s:'scroll',  title:'Two fingers to scroll', text:'Slide two fingers up or down. A two-finger tap also right-clicks. Pinch to zoom.', need:o => o.t === 's', count:4},
  {s:'rail',    title:'Your keys are on the left', text:'Big keys down the left edge, made to hit without looking. Press any of them. The arrow at the bottom folds them away.',
   need:o => ['key', 'k', 'copy', 'paste', 'shot', 'dict', 'mc', 'app', 'kbd'].includes(o.t)},
];
// The last step, in the browser. On the plain connection: PencilPad already works here,
// and installing it as an app is a choice. On the secure one: add it to the Home Screen.
const HOME = {s:'home', title:'Add PencilPad to your Home Screen', manual:true,
  text:'Tap Share, then Add to Home Screen. It opens full screen, like an app. If you have an older PencilPad icon, delete it first.'};
if (!navigator.standalone) TOUR.push(SECURE ? HOME : Object.assign({s:'done', title:'That’s it', manual:true,
  text:'PencilPad works right here in the browser.'}, CFG.self.tls ? {end:'Install as an app', act:goInstall} : {}));
// the Mac's page that installs PencilPad as an app (on the plain connection: it is how the
// iPad comes to trust the secure one)
function installUrl(m) {
  m = m || CFG.self;
  return 'http://' + (m === CFG.self ? location.hostname : m.hosts[0]) + ':' + m.port + PATH + '/secure';
}
function goInstall() { location.href = installUrl(); }
// back from installing: say once to add the secure pad to the Home Screen
let askHome = SECURE && !navigator.standalone && location.hash === '#install';
if (askHome) history.replaceState(null, '', location.pathname);
const PRACTICE = new Set(['c', 'r', 'd', 'u', 'key', 'shot', 'dict', 'mc', 'app', 'k', 'copy', 'paste', 'zoom']);
const tourEl = document.getElementById('tour'), demo = document.getElementById('demo'), dots = document.getElementById('dots');
let tour = null;         // {steps, i, n, done:[]} while the tour (or a one-step card) is showing
function tourShow() {
  const st = tour.steps[tour.i], many = tour.steps.length > 1;
  tour.n = 0; tour.locked = false;
  demo.classList.remove('ok'); demo.dataset.s = st.s;
  const side = document.body.classList.contains('right') ? 'right' : 'left';
  if (st.s === 'rail') railFold(false);          // the step asks for a key: the keys must be there
  document.getElementById('tourStep').textContent = many ? 'Step ' + (tour.i + 1) + ' of ' + tour.steps.length : '';
  document.getElementById('tourTitle').textContent = st.title.replace('left', side);
  document.getElementById('tourText').textContent = st.text.replace('left', side);
  document.getElementById('tourNote').textContent = st.manual ? '' : (st.s === 'move' || st.s === 'scroll')
    ? 'Watch the pointer on your Mac.' : 'Practice only: this is not sent to your Mac.';
  const skip = document.getElementById('tourSkip'), end = document.getElementById('tourEnd');
  skip.textContent = st.skip || (st.manual ? 'Done' : 'Skip');
  skip.classList.toggle('primary', !!st.manual);
  end.textContent = st.end || 'End Tour';
  end.hidden = !many && !st.act;
  dots.innerHTML = many ? tour.steps.map((_, j) => '<i class="' + (j === tour.i ? 'cur' : tour.done[j] ? 'ok' : '') + '"></i>').join('') : '';
}
let tourTimer = null;
function tourStart(steps) {
  clearTimeout(tourTimer); sheetClose(); resetInput(); kbdShow(false);
  tour = {steps: steps || TOUR, i:0, n:0, done:[]}; document.body.classList.add('touring'); tourShow();
}
// release practice presses while the tour is still on, so nothing unmatched reaches the Mac
function tourEnd() { clearTimeout(tourTimer); resetInput(); tour = null; document.body.classList.remove('touring'); store('pencilpad-tour', '1'); }
function tourNext() { clearTimeout(tourTimer); if (!tour) return; if (++tour.i >= tour.steps.length) tourEnd(); else tourShow(); }
function tourSaw(o) {
  const st = tour.steps[tour.i];
  if (tour.locked || st.manual || !st.need(o) || ++tour.n < (st.count || 1)) return;
  tour.locked = true; tour.done[tour.i] = true;
  demo.classList.add('ok');
  document.getElementById('tourTitle').textContent = 'Nice';
  tourTimer = setTimeout(tourNext, 950);
}
function press(id, fn) { document.getElementById(id).addEventListener('pointerdown', e => { e.preventDefault(); e.stopPropagation(); fn(); }); }
press('tourSkip', tourNext);
press('tourEnd', () => { const st = tour && tour.steps[tour.i]; tourEnd(); if (st && st.act) st.act(); });
press('help', () => { pickClose(); tour ? tourEnd() : tourStart(); });
press('status', () => document.body.classList.contains('picking') ? pickClose() : pickOpen());
press('secure', goInstall);


// ---- the rail. A key acts the moment it is touched, and holds while the touch does.
function railKey(id, onDown, onUp) {
  const el = document.getElementById(id);
  let active = null;
  railResets.push(() => { if (active !== null) { active = null; if (onUp) onUp(el); } });
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
const railEl = document.getElementById('rail'), railToggle = document.getElementById('railToggle');
function folded() { return document.body.classList.contains('collapsed'); }
// fold the rail away to a slim strip, which is then the handle that brings it back
function railFold(fold) {
  if (fold === folded()) return;
  if (fold) {                                        // a folded key cannot be let go of
    for (const r of railResets) r();
    for (const [id, c] of [...caps]) if (railEl.contains(c.el)) keyUp(id, true);
  }
  document.body.classList.toggle('collapsed', fold);
  store('pencilpad-rail', fold ? 'collapsed' : 'open');
}
if (store('pencilpad-rail') === 'collapsed') document.body.classList.add('collapsed');
// The handle acts on lift, by the touch that went down on it and only if it ends there.
// Folded, the whole strip (from the screen's edge) is the handle.
let foldPress = null;
railEl.addEventListener('pointerdown', e => {
  e.preventDefault();
  const on = folded() ? railEl : e.target.closest('#railToggle');
  if (!on || foldPress || (e.pointerType === 'touch' && penDown())) return;
  foldPress = {id: e.pointerId, el: on};
  try { on.setPointerCapture(e.pointerId); } catch (_) {}
  railToggle.classList.add('down');
});
railEl.addEventListener('pointerup', e => {
  if (!foldPress || foldPress.id !== e.pointerId) return;
  const r = foldPress.el.getBoundingClientRect(); foldPress = null;
  railToggle.classList.remove('down');
  if (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) railFold(!folded());
});
railEl.addEventListener('pointercancel', e => {
  if (foldPress && foldPress.id === e.pointerId) { foldPress = null; railToggle.classList.remove('down'); }
});

// Escape, Space and Return are the keyboard's own keys: they repeat while held, take the
// modifiers that are on, and use up a one-shot one (Shift+Return). An older Mac without
// the keyboard gets Escape and Return the old way.
for (const id of ['esc', 'space', 'sub']) {
  const el = document.getElementById(id);
  el.addEventListener('pointerdown', e => {
    e.preventDefault();
    if ((e.pointerType === 'touch' && penDown()) || caps.has(e.pointerId)) return;
    if (!tour && !macCan('k')) {
      if (id === 'space') return outdated('Space');
      if (send({t:'key', k:id === 'esc' ? 'esc' : 'return'})) flash(el);
      return;
    }
    try { el.setPointerCapture(e.pointerId); } catch (_) {}
    keyDown(e.pointerId, e.pointerType, el);
  });
  el.addEventListener('pointerup', e => keyUp(e.pointerId));
  el.addEventListener('pointercancel', e => keyUp(e.pointerId, true));
}
// fixed actions: exactly their own chord, whatever modifiers are on
function action(id, t, what) {
  railKey(id, el => { if (!tour && !macCan(NEEDS[id])) return outdated(what); if (send({t})) flash(el); });
}
action('copy', 'copy', 'Copy');
action('paste', 'paste', 'Paste');
action('mc', 'mc', 'Windows');
action('app', 'app', 'Switch');
railKey('shot', el => { if (send({t:'shot'})) flash(el); });
railKey('kbd',  el => {
  if (tour) { tourSaw({t:'kbd'}); return flash(el); }    // the tour needs the trackpad
  if (!macCan('k')) return outdated('The keyboard');
  kbdShow(!document.body.classList.contains('typing'));
});

// Dictate. When the Mac can turn speech into text and this page may use the iPad's
// microphone, the pad records here and the Mac types what was said. Otherwise the
// key presses the Mac's own dictation key.
const AC = window.AudioContext || window.webkitAudioContext;
let mic = null;          // {stream, ctx} once the microphone is open
let rec = null;          // {chunks, n} while recording
let micIdle = null;
function micReady() {
  return !tour && cur && cur.speech && macSet('dictation_source') !== 'mac'
    && window.isSecureContext && AC && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
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
    dictLabel('Allow the microphone', 3000);
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
    dictLabel(out.text ? '' : out.error === 'denied' ? 'Allow Speech on the Mac'
      : out.error ? 'Could not transcribe' : 'Did not catch that', 2500);
  } catch (_) { dictLabel('Mac not reachable', 2500); }
}
// The source and mode are the Mac's settings: "mac" always presses the key, for a
// dictation app on the Mac; "ipad" records here when it can. Hold mode holds the key
// while the finger is down; toggle mode presses it once per tap (on between taps).
let dictOn = false;        // toggle mode, the Mac's app: listening, between two taps
let dictHow = null;        // how the current press started: 'mic' or 'key' (hold mode)
railResets.push(() => { if (rec) { rec = null; dictOn = false; dictLabel(); } });
railKey('dict', el => {
  dictHow = null;
  if (macSet('dictation_mode') === 'toggle') {
    if (micReady() || rec) { dictOn = !dictOn; return dictOn ? micStart(el) : micStop(el); }
    if (!send({t:'dict', s:1})) return; if (tour) return flash(el);
    dictOn = !dictOn; el.classList.toggle('on', dictOn);
  } else if (micReady()) { dictHow = 'mic'; micStart(el); }
  else if (send({t:'dict', s:1})) { dictHow = 'key'; el.classList.add('on'); }
}, el => {
  if (dictHow === 'mic') micStop(el);
  else if (dictHow === 'key') { el.classList.remove('on'); send({t:'dict', s:0}); }
  dictHow = null;
});

// ---- the keyboard, drawn in the lower part of the trackpad. Each touch presses its own
// key: down when it lands, up when it lifts, so shortcuts work and the Mac's own
// keyboard layout decides the character. Modifiers work like the iPad's Shift: tap
// for the next key, tap twice to lock, or hold one down while typing with another
// finger. The Mac only sees a modifier go down just before a key that uses it, so a
// tap on its own does nothing there (some apps act on a double-tapped Shift).
const ROWS = [
  '` 1 2 3 4 5 6 7 8 9 0 - = delete:1.5',
  'tab:1.5 q w e r t y u i o p [ ] \\',
  'esc:1.75 a s d f g h j k l ; \' return:1.75',
  'shift:2.25 z x c v b n m , . / shift:2.25',
  'control:1.25 option:1.25 command:1.5 space:4.75 command:1.5 option:1.25 arrows:3',
];
const KEYNAME = {'`':'grave', '-':'minus', '=':'equal', '[':'lbracket', ']':'rbracket', '\\':'backslash',
                 ';':'semicolon', "'":'quote', ',':'comma', '.':'period', '/':'slash'};
const SHIFTED = {'`':'~', 1:'!', 2:'@', 3:'#', 4:'$', 5:'%', 6:'^', 7:'&', 8:'*', 9:'(', 0:')', '-':'_', '=':'+',
                 '[':'{', ']':'}', '\\':'|', ';':':', "'":'"', ',':'<', '.':'>', '/':'?'};
const SHIFT = '<svg class="sh" viewBox="0 0 18 18"><path d="M9 2.5 2.5 9.5H6v5.5h6V9.5h3.5z"/></svg>';
const WORD = {shift:'<i>' + SHIFT + '</i><span>shift</span>', control:'<i>⌃</i><span>control</span>',
              option:'<i>⌥</i><span>option</span>', command:'<i>⌘</i><span>command</span>',
              delete:'delete', tab:'tab', return:'return', esc:'esc', space:''};
const ARROW = '<svg viewBox="0 0 14 14"><path d="M9.5 2.5v9L3 7z"/></svg>';
const MODS = ['shift', 'control', 'option', 'command'];
const LOCK_MS = 400, REPEAT_MS = 450, EVERY_MS = 55;
const keyboard = document.getElementById('keyboard');
for (const line of ROWS) {
  const row = document.createElement('div'); row.className = 'row';
  for (const tok of line.split(' ')) {
    const [c, w] = tok.length > 1 && tok.includes(':') ? tok.split(':') : [tok, '1'];
    if (c === 'arrows') {
      row.insertAdjacentHTML('beforeend', '<div class="arrows"><div class="cap" data-k="left">' + ARROW + '</div><div class="ud">'
        + '<div class="cap" data-k="up">' + ARROW + '</div><div class="cap" data-k="down">' + ARROW + '</div></div>'
        + '<div class="cap" data-k="right">' + ARROW + '</div></div>');
      continue;
    }
    const el = document.createElement('div');
    el.style.setProperty('--w', w);
    if (c.length === 1) { el.className = 'cap'; el.dataset.k = KEYNAME[c] || c; el.dataset.c = c; el.dataset.s = SHIFTED[c] || c.toUpperCase(); }
    else { el.className = 'cap word'; el.dataset.k = c; el.innerHTML = WORD[c]; }
    row.append(el);
  }
  keyboard.firstChild.append(row);
}
// each modifier: state off/armed/locked; used: a key was typed while it was held; t: its
// last tap; at: when it came on (they go down on the Mac in that order); mac: down there
const mod = {};
for (const n of MODS) mod[n] = {state: 'off', used: false, t: 0, at: 0, mac: false};
// pointerId -> {el, k, type, t, mods} for each touch pressing a key (mods: not for modifiers).
// Two fingers on one key are one press: it goes up when the last of them lifts.
const caps = new Map();
let rep = null;            // the key the pad repeats while it is held (macOS does not repeat posted keys)
function pressing(k) { for (const c of caps.values()) if (c.k === k && c.mods) return c; return null; }
function held(n) { for (const c of caps.values()) if (c.k === n) return true; return false; }
function kbdPen() { for (const c of caps.values()) if (c.type === 'pen') return true; return false; }
function needed(n) {
  if (held(n) || mod[n].state === 'locked' || ptr.includes(n)) return true;
  for (const c of caps.values()) if (c.mods && c.mods.includes(n)) return true;
  return false;
}
function macMod(n, down) { send({t:'k', k:n, d:down ? 1 : 0, m:MODS.filter(x => x !== n && mod[x].mac)}); mod[n].mac = down; }
function syncMods() { for (const n of MODS) if (mod[n].mac && !needed(n)) macMod(n, false); }
function stopRepeat() { if (rep) clearTimeout(rep.timer); rep = null; }
function drawKeys() {
  const shifted = mod.shift.state !== 'off' || held('shift');
  for (const el of keyboard.querySelectorAll('.cap[data-c]')) el.textContent = shifted ? el.dataset.s : el.dataset.c;
  for (const el of keyboard.querySelectorAll('.cap.word')) {
    const m = MODS.includes(el.dataset.k) && mod[el.dataset.k];
    el.classList.toggle('armed', !!m && m.state === 'armed');
    el.classList.toggle('locked', !!m && m.state === 'locked');
  }
}
function keyDown(id, type, el) {
  const k = el.dataset.k, t = performance.now();
  if (!tour && !macCan('k')) return outdated('The keyboard');    // nothing arms or locks either
  if (MODS.includes(k)) {
    if (!held(k)) { mod[k].used = false; if (mod[k].state === 'off') mod[k].at = t; }
    caps.set(id, {el, k, type, t}); el.classList.add('on');
    return drawKeys();
  }
  const same = pressing(k);
  if (same) { caps.set(id, {el, k, type, t, mods: same.mods}); el.classList.add('on'); return; }
  const mods = MODS.filter(n => mod[n].state !== 'off' || held(n)).sort((a, b) => mod[a].at - mod[b].at);
  for (const n of mods) if (!mod[n].mac) macMod(n, true);
  if (!send({t:'k', k, d:1, m:mods})) return syncMods();
  caps.set(id, {el, k, type, t, mods}); el.classList.add('on');
  for (const n of MODS) {
    if (held(n)) mod[n].used = true;                  // a held modifier, not a tap
    if (mod[n].state === 'armed') mod[n].state = 'off';   // a one-shot modifier is used up
  }
  stopRepeat();                                       // like a Mac, only the newest key repeats
  const r = rep = {k};
  r.timer = setTimeout(function again() {
    if (rep === r && send({t:'k', k, d:1, m:mods, r:1})) r.timer = setTimeout(again, EVERY_MS);
  }, REPEAT_MS);
  drawKeys();
}
function keyUp(id, lost) {
  const c = caps.get(id);
  if (!c) return;
  caps.delete(id);
  if (![...caps.values()].some(o => o.el === c.el)) c.el.classList.remove('on');
  if (c.mods) {
    if (pressing(c.k)) return;                        // another finger still holds it
    if (rep && rep.k === c.k) stopRepeat();
    send({t:'k', k:c.k, d:0, m:c.mods});
  } else if (!lost && !held(c.k) && !mod[c.k].used && performance.now() - c.t < HOLD_MS) {
    // a tap: off -> armed; armed and tapped again soon -> locked; otherwise -> off
    const m = mod[c.k];
    m.state = m.state === 'off' ? 'armed' : m.state === 'armed' && c.t - m.t < LOCK_MS ? 'locked' : 'off';
    m.t = performance.now();
  }
  syncMods(); drawKeys();
}
// let go of every key and modifier, here and on the Mac
function kbdRelease() {
  stopRepeat();
  const up = new Set();
  for (const c of caps.values()) {
    c.el.classList.remove('on');
    if (c.mods && !up.has(c.k)) { up.add(c.k); send({t:'k', k:c.k, d:0, m:c.mods}); }
  }
  caps.clear();
  for (const n of MODS) { mod[n].state = 'off'; mod[n].used = false; }
  syncMods(); drawKeys();
}
// Up or away. While it is up the trackpad goes on above it, down to the keyboard's top.
function kbdShow(on) {
  if (on === document.body.classList.contains('typing')) return;
  if (!on) kbdRelease();
  document.body.classList.toggle('typing', on);
  document.getElementById('kbd').classList.toggle('lit', on);
}
const kbh = () => document.body.style.setProperty('--kbh', keyboard.offsetHeight + 'px');
if (window.ResizeObserver) new ResizeObserver(kbh).observe(keyboard);
kbh();
keyboard.addEventListener('pointerdown', e => {
  e.preventDefault();
  if (document.body.classList.contains('picking')) return pickClose();   // this touch only closes the list
  const el = e.target.closest('.cap[data-k]');
  if (!el || (e.pointerType === 'touch' && penDown())) return;          // palm rejection
  // a primary pointer means nothing else of its kind is touching: what is still pressed lost its lift
  if (e.isPrimary) for (const [id, c] of [...caps]) if (c.type === e.pointerType) keyUp(id, true);
  try { el.setPointerCapture(e.pointerId); } catch (_) {}
  keyDown(e.pointerId, e.pointerType, el);
});
drawKeys();
keyboard.addEventListener('pointerup', e => keyUp(e.pointerId));
keyboard.addEventListener('pointercancel', e => keyUp(e.pointerId, true));   // not a tap

// ---- settings, from the gear. What the Mac acts on is kept on the Mac and shared by
// every pad (set with {t:'set'}, and sent back to them all); which keys show, the rail's
// side and pinch are this iPad's own.
const sheetBody = document.getElementById('sheetBody');
const KEYLABEL = {fn: 'Fn (Globe)', right_option: 'Right Option', right_command: 'Right Command',
                  right_control: 'Right Control', 'option+space': 'Option+Space', 'control+space': 'Control+Space'};
function keyLabel(k) {
  return KEYLABEL[k] || k.split('+').map(w => /^f\d+$/.test(w) ? w.toUpperCase()
    : w.replace('_', ' ').replace(/\b\w/g, c => c.toUpperCase())).join('+');
}
function hiddenKeys() { try { return JSON.parse(store('pencilpad-hidden') || '[]'); } catch (_) { return []; } }
function applyLocal() {
  const hide = hiddenKeys();
  for (const id of Object.keys(KEYNAMES)) document.getElementById(id).classList.toggle('off', hide.includes(id));
  document.body.classList.toggle('right', store('pencilpad-side') === 'right');
  // a key hidden while it is pressed is let go of (it cannot be lifted any more)
  for (const [id, c] of [...caps]) if (c.el.classList.contains('off')) keyUp(id, true);
  if (hide.includes('dict')) for (const r of railResets) r();
  groupGaps();
}
// a little more room between the rail's groups of keys; none above a group that is all hidden
const GROUPS = [['esc', 'space', 'sub'], ['copy', 'paste'], ['dict', 'shot'], ['mc', 'app', 'kbd']];
function groupGaps() {
  let before = false;
  for (const g of GROUPS) {
    const shown = g.map(id => document.getElementById(id)).filter(el => !el.classList.contains('off'));
    for (const el of g.map(id => document.getElementById(id))) el.classList.remove('gtop');
    if (shown.length && before) shown[0].classList.add('gtop');
    if (shown.length) before = true;
  }
}
applyLocal();
(function sheetBuild() {
  const seg = (k, opts, local) => '<div class="seg" data-' + (local ? 'local' : 'set') + '="' + k + '">'
    + opts.map(([v, t]) => '<button data-v="' + v + '">' + t + '</button>').join('') + '</div>';
  const sw = (k, name, local) => '<div class="set' + (local ? '' : ' mac') + '" data-' + (local ? 'local' : 'set') + '="' + k
    + '"><span class="name">' + name + '</span><i class="switch"></i></div>';
  const col = (name, inner, local) => '<div class="set col' + (local ? '' : ' mac') + '"><span class="name">' + name + '</span>' + inner + '</div>';
  const slider = (k, name, min, max) => '<div class="set col mac"><div class="line"><span class="name">'
    + name + '</span><span class="val" data-val="' + k + '"></span></div><div class="slider" data-set="' + k
    + '" data-min="' + min + '" data-max="' + max + '"><i></i><b></b></div></div>';
  const say = (text, id) => '<p class="say"' + (id ? ' id="' + id + '"' : '') + '>' + text + '</p>';
  sheetBody.innerHTML =
    '<p class="banner" id="oldMac" hidden><i></i><span></span></p>'
    + '<h2>Pointer</h2><div class="group">'
    + slider('pointer_speed', 'Tracking speed', 0.4, 4) + slider('scroll_speed', 'Scroll speed', 0.2, 5)
    + col('Scroll direction', seg('scroll_direction', [['natural', 'Natural'], ['reversed', 'Reversed']]))
    + sw('hover', 'Pencil hover moves the pointer') + sw('pinch', 'Pinch to zoom', true) + '</div>'
    + '<h2>Dictate</h2><div class="group">'
    + col('Who listens', seg('dictation_source', [['mac', 'The Mac'], ['ipad', 'This iPad']])
      + say('The Mac: Dictate presses your dictation app’s shortcut on the Mac, and that app types what you say. '
        + 'macOS Dictation, Handy and similar apps work this way. This iPad: the iPad’s microphone records and the Mac turns it into text.'))
    + col('The Mac’s dictation shortcut', '<div class="chips" data-set="dictation_key"></div>'
      + say('Pick the shortcut your dictation app on the Mac listens for.') + say('Also used whenever this iPad can’t listen.', 'alsoKey'))
    + col('Dictate key', seg('dictation_mode', [['hold', 'Hold to talk'], ['toggle', 'Tap to start and stop']])
      + say('Match your dictation app: hold if it records while the key is held, tap if one press starts it and the next stops it.'))
    + '</div><p class="note" id="micNote"></p>'
    + '<h2>Screenshot</h2><div class="group">' + sw('screenshot_paste', 'Paste after capture') + '</div>'
    + '<h2>Keys</h2><div class="group">'
    + col('Rail side', seg('side', [['left', 'Left'], ['right', 'Right']], true), true)
    + Object.entries(KEYNAMES).map(([id, n]) => sw('key-' + id, n, true)).join('') + '</div>'
    + '<h2>Help</h2><div class="group"><div class="set act" id="install">Install as an app</div>'
    + '<div class="set act" id="replay">Replay the tour</div></div>';
})();
function slide(el, v) {
  const min = +el.dataset.min, max = +el.dataset.max, f = (v - min) / (max - min) * 100;
  el.querySelector('i').style.width = f + '%'; el.querySelector('b').style.left = f + '%';
  sheetBody.querySelector('[data-val="' + el.dataset.set + '"]').textContent = (+v).toFixed(1) + '×';
}
function sheetSync() {
  const can = macCan('settings'), hide = hiddenKeys();
  const old = document.getElementById('oldMac');
  old.hidden = can;
  old.querySelector('span').textContent = macName() + ' has an older PencilPad. Update it on that Mac to change these settings.';
  document.getElementById('alsoKey').hidden = macSet('dictation_source') !== 'ipad';
  for (const r of sheetBody.querySelectorAll('.mac')) r.classList.toggle('dis', !can);
  const chips = sheetBody.querySelector('.chips');
  const list = (cur && cur.dictKeys) || CFG.dictKeys;
  if (chips.dataset.keys !== list.join()) {
    chips.dataset.keys = list.join();
    // in groups that each fill their rows: Fn and the F-keys, the right-hand modifiers, chords
    const groups = [[], [], []];
    for (const k of list) groups[/^(fn|f\d+)$/.test(k) ? 0 : k.includes('+') ? 2 : 1].push(k);
    chips.innerHTML = groups.filter(g => g.length).map(g => '<div style="--n:' + [4, 3, 2][groups.indexOf(g)] + '">'
      + g.map(k => '<button data-v="' + k + '">' + keyLabel(k) + '</button>').join('') + '</div>').join('');
  }
  for (const box of sheetBody.querySelectorAll('[data-set], [data-local]')) {
    const k = box.dataset.set || box.dataset.local;
    const v = box.dataset.set ? macSet(k) : k === 'side' ? (store('pencilpad-side') || 'left')
      : k === 'pinch' ? pinchOn() : !hide.includes(k.slice(4));
    if (box.classList.contains('slider')) slide(box, v);
    else if (box.classList.contains('set')) box.classList.toggle('on', !!v);
    else for (const b of box.querySelectorAll('button[data-v]')) b.classList.toggle('cur', b.dataset.v === String(v));
  }
  // the last key showing cannot be hidden
  const left = Object.keys(KEYNAMES).filter(id => !hide.includes(id));
  for (const id of Object.keys(KEYNAMES))
    sheetBody.querySelector('[data-local="key-' + id + '"]').classList.toggle('dis', left.length === 1 && left[0] === id);
  // installing as an app: not once the pad is one already (secure, from the Home Screen)
  const inst = document.getElementById('install');
  inst.hidden = !CFG.self.tls || (SECURE && navigator.standalone);
  inst.textContent = navigator.standalone ? 'Set up the secure connection' : 'Install as an app';
  document.getElementById('micNote').textContent = macSet('dictation_source') === 'ipad'
    && !(cur && cur.speech && window.isSecureContext)
    ? 'This iPad can listen once PencilPad is installed as an app (Help → Install as an app) and PencilPad’s menu bar app '
      + 'is running on the Mac. Until then, Dictate uses the Mac’s shortcut.' : '';
}
function setMac(k, v) {
  if (!macCan('settings') || !cur) return;
  cur.settings = Object.assign({}, cur.settings, {[k]: v});      // shown at once; the Mac sends it back
  send({t:'set', k, v}); settingsChanged();
}
let lastMode = null;
function settingsChanged() {
  const mode = macSet('dictation_mode');
  if (lastMode && mode !== lastMode && dictOn) { dictOn = false; document.getElementById('dict').classList.remove('on'); }
  lastMode = mode;
  dictLabel();
  if (document.body.classList.contains('setting')) sheetSync();
}
sheetBody.addEventListener('click', e => {
  if (e.target.closest('#replay')) { sheetClose(); return tourStart(); }
  if (e.target.closest('#install')) return goInstall();
  const b = e.target.closest('button[data-v]'), row = e.target.closest('.set[data-set], .set[data-local]');
  if (b) {
    const box = b.closest('[data-set], [data-local]');
    if (box.dataset.local === 'side') { store('pencilpad-side', b.dataset.v); applyLocal(); }
    else setMac(box.dataset.set, b.dataset.v);
  } else if (row && !row.classList.contains('dis')) {
    const k = row.dataset.set || row.dataset.local, on = !row.classList.contains('on');
    if (row.dataset.set) setMac(k, on);
    else if (k === 'pinch') store('pencilpad-pinch', on ? 'on' : 'off');
    else {
      const id = k.slice(4), hide = hiddenKeys().filter(x => x !== id);
      if (!on) hide.push(id);
      store('pencilpad-hidden', JSON.stringify(hide)); applyLocal();
    }
  }
  sheetSync();
});
// sliders follow the finger or the Pencil, and set the value on lift
let sliding = null;
sheetBody.addEventListener('pointerdown', e => {
  const el = e.target.closest('.slider');
  if (!el || el.closest('.dis')) return;
  e.preventDefault();
  sliding = {id: e.pointerId, el}; try { el.setPointerCapture(e.pointerId); } catch (_) {}
  el.classList.add('dragging');
  slideTo(e);
});
function slideTo(e) {
  const el = sliding.el, r = el.getBoundingClientRect(), min = +el.dataset.min, max = +el.dataset.max;
  sliding.v = Math.round((min + Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * (max - min)) * 10) / 10;
  slide(el, sliding.v);
}
sheetBody.addEventListener('pointermove', e => { if (sliding && sliding.id === e.pointerId) slideTo(e); });
sheetBody.addEventListener('pointerup', e => {
  if (!sliding || sliding.id !== e.pointerId) return;
  const {el, v} = sliding; sliding = null; el.classList.remove('dragging'); setMac(el.dataset.set, v);
});
sheetBody.addEventListener('pointercancel', () => {
  if (sliding) sliding.el.classList.remove('dragging');
  sliding = null; sheetSync();
});
function sheetOpen() {
  if (tour) tourEnd();
  pickClose(); resetInput(); sheetSync();
  document.body.classList.add('setting');
}
function sheetClose() { document.body.classList.remove('setting'); sliding = null; }
press('gear', () => document.body.classList.contains('setting') ? sheetClose() : sheetOpen());
document.getElementById('scrim').addEventListener('pointerdown', e => { e.preventDefault(); sheetClose(); });
document.getElementById('done').addEventListener('click', sheetClose);
settingsChanged();

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
// (except in the settings sheet, which scrolls, and whose buttons need their clicks)
for (const ev of ['touchstart', 'touchmove', 'gesturestart', 'gesturechange', 'contextmenu', 'dblclick', 'selectstart'])
  document.addEventListener(ev, e => {
    if ((ev === 'touchstart' || ev === 'touchmove') && e.target.closest && e.target.closest('#sheet')) return;
    e.preventDefault();
  }, {passive: false});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
