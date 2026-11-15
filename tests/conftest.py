"""Test harness for PencilPad.

Every test gets its own PencilPad server in dry-run mode (PENCILPAD_DRY_RUN=1)
on a free port, with its token file in a temporary directory outside the
project. Nothing is posted to the real Mac: dry run records each CGEvent it
would have posted, and the tests read that record from GET /<token>/dry.

The suite never talks to port 8765 (a live PencilPad may be running there) and
only ever stops the server processes it started itself.

PENCILPAD_UNDER_TEST=<path> runs the suite against another copy of the script.
"""

import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = Path(os.environ.get("PENCILPAD_UNDER_TEST") or HERE.parent / "pencilpad.py").resolve()
TMP = Path(tempfile.mkdtemp(prefix="pencilpad-tests-"))
atexit.register(shutil.rmtree, TMP, ignore_errors=True)
TOKEN = "t0ken-for-tests"
LIVE_PORT = 8765          # the owner's real pad: never used by a test


def free_port():
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port != LIVE_PORT:
            return port


def server_env(port, token_file, **extra):
    assert int(port) != LIVE_PORT
    env = dict(os.environ, PENCILPAD_DRY_RUN="1", PENCILPAD_PORT=str(port),
               PENCILPAD_TOKEN_FILE=str(token_file), PENCILPAD_NO_OPEN="1",
               PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
    env.update(extra)
    return env


class Server:
    def __init__(self, port=None, extra_env=None):
        # An auto-picked port can be taken by something else between the probe and
        # the server binding it; when that happens, pick another and start again.
        for attempt in range(5):
            self._start(port or free_port(), extra_env)
            deadline = time.time() + 30
            while self.proc.poll() is None:
                try:
                    urllib.request.urlopen(self.url, timeout=1).read()
                    return
                except Exception:
                    if time.time() > deadline:
                        self.stop()
                        raise
                    time.sleep(0.1)
            if port or attempt == 4 or "already in use" not in self.output():
                raise RuntimeError("server exited:\n" + self.output())

    def _start(self, port, extra_env):
        self.port = port
        self.token_file = TMP / f"token-{self.port}"
        self.token_file.write_text(TOKEN)
        self.out_path = TMP / f"server-{self.port}-{time.monotonic_ns()}.log"
        env = server_env(self.port, self.token_file, **(extra_env or {}))
        self.out = open(self.out_path, "w")
        self.proc = subprocess.Popen([sys.executable, str(SCRIPT)], env=env, cwd=TMP,
                                     stdout=self.out, stderr=subprocess.STDOUT)
        self.base = f"http://127.0.0.1:{self.port}"
        self.url = f"{self.base}/{TOKEN}"

    def output(self):
        self.out.flush()
        return self.out_path.read_text()

    def get(self, path="", timeout=3):
        """GET <base>/<token><path>; returns (status, headers, body text)."""
        with urllib.request.urlopen(self.url + path, timeout=timeout) as r:
            return r.status, r.headers, r.read().decode()

    def state(self):
        return json.loads(self.get("/state")[2])

    def log(self, since=0):
        with urllib.request.urlopen(f"{self.url}/dry?since={since}", timeout=3) as r:
            return json.loads(r.read())

    def mark(self):
        return len(self.log())

    def wait_for(self, pred, since=0, timeout=5.0, settle=0.0):
        """Poll the dry-run record until pred(entries) is true; return entries."""
        deadline = time.time() + timeout
        while True:
            entries = self.log(since)
            if pred(entries):
                if settle:
                    time.sleep(settle)
                    entries = self.log(since)
                return entries
            if time.time() > deadline:
                raise AssertionError(f"timed out; record since {since}: {entries}")
            time.sleep(0.03)

    def drop(self):
        req = urllib.request.Request(f"{self.url}/dry/drop", method="POST")
        urllib.request.urlopen(req, timeout=3).read()

    def stop(self):
        # only ever this server's own process, by its Popen handle
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.out.close()
        self.token_file.unlink(missing_ok=True)
        self.out_path.unlink(missing_ok=True)


@pytest.fixture
def server():
    s = Server()
    yield s
    s.stop()


def evs(entries, *names):
    """Event names, optionally filtered to the given names."""
    return [e["ev"] for e in entries if not names or e["ev"] in names]


def wait_until(pred, timeout=5.0, what="condition"):
    deadline = time.time() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.03)


# ------------------------------------------------------------------ browser

@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


VIEW = {"width": 1180, "height": 820}     # iPad Air 11" landscape, CSS px
CENTER = (590, 410)
TOUR_SEEN = "try { localStorage.setItem('pencilpad-tour', '1'); } catch (_) {}"
CONNECTED = "!document.body.classList.contains('offline')"
# Connected and usable: the "Looking for your Mac" card sits over the middle of the
# pad and keeps taking touches for the 250 ms it takes to fade out after a connect.
ONLINE = (CONNECTED + " && getComputedStyle(document.getElementById('lost')).visibility === 'hidden'")
OFFLINE = "document.body.classList.contains('offline')"


class Pad:
    """Drives the served page with real input through Chrome DevTools Protocol.

    Touches go through Input.dispatchTouchEvent (pointerType "touch", distinct
    pointerIds, real multi-touch); the Pencil through Input.dispatchMouseEvent
    with pointerType "pen". Both produce genuine browser pointer events, so the
    page's own handlers, capture and timers run exactly as they would.
    """

    def __init__(self, page, cdp, server=None):
        self.page, self.cdp, self.server = page, cdp, server
        self.touches = {}

    def _t(self, typ, pts):
        self.cdp.send("Input.dispatchTouchEvent", {
            "type": typ,
            "touchPoints": [{"x": x, "y": y, "id": i} for i, (x, y) in pts.items()]})

    def down(self, x, y, id=1):
        self.touches[id] = (x, y)
        self._t("touchStart", self.touches)

    def move(self, x, y, id=1):
        self.touches[id] = (x, y)
        self._t("touchMove", self.touches)

    def up(self, id=1):
        pt = self.touches.pop(id)
        self._t("touchEnd", {id: pt})

    def cancel(self):
        self.touches.clear()
        self._t("touchCancel", {})

    def drag(self, id, x0, y0, x1, y1, steps=10, dt=0.016):
        for i in range(1, steps + 1):
            self.move(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps, id)
            time.sleep(dt)

    def tap(self, x=CENTER[0], y=CENTER[1], id=1, hold=0.05):
        self.down(x, y, id)
        time.sleep(hold)
        self.up(id)

    # ---- Pencil
    def _pen(self, typ, x, y, pressed):
        self.cdp.send("Input.dispatchMouseEvent", {
            "type": typ, "x": x, "y": y, "pointerType": "pen", "button": "left",
            "buttons": 1 if pressed else 0, "clickCount": 1})

    def pen_down(self, x, y):
        self._pen("mousePressed", x, y, True)

    def pen_move(self, x, y):
        self._pen("mouseMoved", x, y, True)

    def pen_up(self, x, y):
        self._pen("mouseReleased", x, y, False)

    def pen_hover(self, x, y):
        self.cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y, "pointerType": "pen", "button": "none",
            "buttons": 0})

    # ---- page state
    def has_class(self, el_id, cls):
        return self.page.evaluate(
            "([i, c]) => document.getElementById(i).classList.contains(c)", [el_id, cls])

    def wait_class(self, el_id, cls, present=True, timeout=5000):
        self.page.wait_for_function(
            "([i, c, p]) => document.getElementById(i).classList.contains(c) === p",
            arg=[el_id, cls, present], timeout=timeout)

    def wait_armed(self):
        """Until the hold timer has armed the pad (HOLD_MS after a still touch)."""
        self.wait_class("pad", "armed")

    def text(self, el_id):
        return self.page.evaluate("i => document.getElementById(i).textContent", el_id)

    def center(self, el_id):
        return tuple(self.page.evaluate(
            "i => { const r = document.getElementById(i).getBoundingClientRect();"
            " return [r.x + r.width / 2, r.y + r.height / 2]; }", el_id))

    def press(self, el_id, id=9):
        """Touch an on-screen button (tour buttons, the ? button)."""
        self.tap(*self.center(el_id), id=id)

    def wait_online(self, timeout=8000):
        self.page.wait_for_function(ONLINE, timeout=timeout)

    def wait_offline(self, timeout=8000):
        self.page.wait_for_function(OFFLINE, timeout=timeout)

    def set_hidden(self, hidden):
        """Simulate the page being backgrounded / foregrounded."""
        self.page.evaluate("""h => {
            Object.defineProperty(document, 'hidden', {configurable: true, get: () => h});
            Object.defineProperty(document, 'visibilityState', {configurable: true,
                get: () => h ? 'hidden' : 'visible'});
            document.dispatchEvent(new Event('visibilitychange'));
        }""", hidden)

    # ---- guided tour
    def touring(self):
        return self.page.evaluate("document.body.classList.contains('touring')")

    def tour_step(self):
        """Name of the tour step on screen, or None when the tour is not running."""
        return self.page.evaluate(
            "document.body.classList.contains('touring')"
            " ? document.getElementById('demo').dataset.s : null")

    def wait_step(self, name, timeout=5000):
        """Until the tour shows step `name`, ready for its gesture (name None: tour over)."""
        if name is None:
            self.page.wait_for_function("!document.body.classList.contains('touring')",
                                        timeout=timeout)
        else:
            self.page.wait_for_function(
                "s => document.body.classList.contains('touring')"
                " && document.getElementById('demo').dataset.s === s"
                " && !document.getElementById('demo').classList.contains('ok')",
                arg=name, timeout=timeout)

    def wait_step_done(self, timeout=5000):
        """Until the current step shows its tick (the gesture was recognised)."""
        self.wait_class("demo", "ok", timeout=timeout)

    def step_done(self):
        return self.has_class("demo", "ok")


def corner_xy(name):
    w, h = VIEW["width"], VIEW["height"]
    return {"esc": (60, 60), "shot": (w - 60, 60),
            "dict": (60, h - 60), "sub": (w - 60, h - 60)}[name]


@contextmanager
def open_pad(browser, server, tour_seen=True, init=(), route=None, url=None, connected=True,
             settled=True):
    """A fresh browser context with the pad page open.

    tour_seen=True marks the first-run tour as already done (before the page's
    own script runs) so that gestures go to the server; False gives a first-run
    page, where the tour starts on connect.
    """
    ctx = browser.new_context(has_touch=True, viewport=VIEW)
    if tour_seen:
        ctx.add_init_script(TOUR_SEEN)
    for script in init:
        ctx.add_init_script(script)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    if route:
        route(page)
    page.goto(url or server.url)
    if connected:
        page.wait_for_function(ONLINE if settled else CONNECTED)
        server.wait_for(lambda e: "connect" in evs(e))
    try:
        yield Pad(page, ctx.new_cdp_session(page), server)
    finally:
        ctx.close()
    assert not errors, f"page JS errors: {errors}"


@pytest.fixture
def pad(browser, server):
    """The pad with the tour out of the way: gestures reach the (dry-run) server."""
    with open_pad(browser, server) as p:
        yield p


@pytest.fixture
def first_run(browser, server):
    """The pad as a brand-new iPad sees it: the guided tour starts on connect."""
    with open_pad(browser, server, tour_seen=False) as p:
        yield p
