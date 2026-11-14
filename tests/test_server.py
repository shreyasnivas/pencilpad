"""Server-side tests: HTTP/WebSocket handling and the events each message posts.

All against a dry-run server (see conftest.py); nothing reaches the real Mac.
"""

import asyncio
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import aiohttp
import pytest

from conftest import SCRIPT, TOKEN, TMP, Server, evs, free_port, server_env, wait_until


# ------------------------------------------------------------------ helpers

def run(coro):
    # in its own thread: Playwright's sync API leaves an event loop running in the main one
    box = {}

    def target():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as e:
            box["error"] = e

    th = threading.Thread(target=target)
    th.start()
    th.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


async def send_all(server, msgs, raw=False, pause=0.05, close=True):
    """Open a pad WebSocket, send messages, optionally close."""
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(f"ws://127.0.0.1:{server.port}/{TOKEN}/ws")
        for m in msgs:
            await ws.send_str(m if raw else json.dumps(m))
            await asyncio.sleep(pause)
        await asyncio.sleep(0.1)
        if close:
            await ws.close()
            await asyncio.sleep(0.1)


def session(server, msgs, **kw):
    mark = server.mark()
    run(send_all(server, msgs, **kw))
    return server.wait_for(lambda e: "disconnect" in evs(e), since=mark)


def load_module():
    """Import the script as a module (no server started), always in dry-run mode."""
    os.environ["PENCILPAD_DRY_RUN"] = "1"
    os.environ["PENCILPAD_TOKEN_FILE"] = str(TMP / "token-unit")
    os.environ["PENCILPAD_PORT"] = "1"           # never bound: main() is not run
    sys.dont_write_bytecode = True               # no __pycache__ next to the script
    spec = importlib.util.spec_from_file_location("pencilpad_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.DRY_RUN
    return mod


# ------------------------------------------------------------------ token / routing

def test_right_token_serves_page(server):
    with urllib.request.urlopen(server.url) as r:
        body = r.read().decode()
    assert r.status == 200 and "<title>PencilPad</title>" in body
    assert "__CFG__" not in body


@pytest.mark.parametrize("path", [
    "/wrong", "/wrong/ws", f"/{TOKEN}x", f"/{TOKEN}x/ws", "/",
    "/wrong/connect", "/wrong/state", "/wrong/ping", "/wrong/dry", f"/{TOKEN}/nothing-here"])
def test_wrong_token_404(server, path):
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(server.base + path)
    assert ei.value.code == 404


def test_wrong_token_websocket_rejected(server):
    async def go():
        async with aiohttp.ClientSession() as s:
            with pytest.raises(aiohttp.WSServerHandshakeError) as ei:
                await s.ws_connect(f"ws://127.0.0.1:{server.port}/nottoken/ws")
            assert ei.value.status == 404
    run(go())
    assert "connect" not in evs(server.log())


# ------------------------------------------------------------------ message types

def test_move(server):
    e = session(server, [{"t": "m", "x": 10, "y": -5}])
    moves = [x for x in e if x["ev"] == "move"]
    assert len(moves) == 1
    m = moves[0]
    assert m["x"] > 960 + 10 and m["y"] < 540 - 5      # gain >= 1 applied
    assert m["dx"] > 0 and m["dy"] < 0                 # deltas carried


def test_scroll(server):
    e = session(server, [{"t": "s", "x": 0, "y": 20}])
    sc = [x for x in e if x["ev"] == "scroll"]
    assert len(sc) == 1 and sc[0]["dy"] > 0 and sc[0]["dx"] == 0


def test_scroll_sub_pixel_accumulates(server):
    e = session(server, [{"t": "s", "x": 0, "y": 0.4}] * 5)
    assert sum(x["dy"] for x in e if x["ev"] == "scroll") == 2   # 5 * 0.4 * 1.4 = 2.8


def test_click_and_click_count(server):
    e = session(server, [{"t": "c"}, {"t": "c"}, {"t": "c"}])
    clicks = [(x["ev"], x["clicks"]) for x in e if x["ev"] in ("ldown", "lup")]
    assert clicks == [("ldown", 1), ("lup", 1), ("ldown", 2), ("lup", 2), ("ldown", 3), ("lup", 3)]


def test_click_count_resets_after_pause(server):
    mark = server.mark()
    run(send_all(server, [{"t": "c"}], close=False, pause=0))
    time.sleep(0.8)                              # the double-click window is 0.6 s
    run(send_all(server, [{"t": "c"}]))
    e = server.wait_for(lambda e: evs(e).count("disconnect") == 2, since=mark)
    assert [x["clicks"] for x in e if x["ev"] == "ldown"] == [1, 1]


def test_right_click(server):
    e = session(server, [{"t": "r"}])
    assert evs(e, "rdown", "rup", "ldown", "lup") == ["rdown", "rup"]


def test_drag_down_move_up(server):
    e = session(server, [{"t": "d"}, {"t": "m", "x": 20, "y": 0}, {"t": "m", "x": 20, "y": 0}, {"t": "u"}])
    assert evs(e, "ldown", "drag", "move", "lup") == ["ldown", "drag", "drag", "lup"]
    drags = [x for x in e if x["ev"] == "drag"]
    assert all(d["dx"] > 0 for d in drags)
    up = [x for x in e if x["ev"] == "lup"][0]
    assert up["x"] == drags[-1]["x"]          # released where the drag ended


@pytest.mark.parametrize("k,key", [("esc", "esc"), ("return", "return")])
def test_keys(server, k, key):
    e = session(server, [{"t": "key", "k": k}])
    ks = [(x["ev"], x["key"], x["mods"]) for x in e if x["ev"].startswith("key")]
    assert ks == [("keydown", key, []), ("keyup", key, [])]


def test_key_not_on_allowlist_ignored(server):
    e = session(server, [{"t": "key", "k": "tab"}, {"t": "key", "k": "command"}])
    assert not [x for x in e if x["ev"] in ("keydown", "keyup", "flags")]


def test_screenshot_then_ctrl_v(server):
    e = session(server, [{"t": "shot"}], pause=0.5)
    assert evs(e, "screencapture", "keydown", "keyup") == ["screencapture", "keydown", "keyup"]
    kd = [x for x in e if x["ev"] == "keydown"][0]
    assert kd["key"] == "v" and kd["mods"] == ["control"]


def test_dictate_hold_down_up(server):
    e = session(server, [{"t": "dict", "s": 1}, {"t": "dict", "s": 0}])
    ks = [(x["ev"], x["key"]) for x in e if x["ev"].startswith("key")]
    assert ks == [("keydown", "f13"), ("keyup", "f13")]


def test_function_key_keeps_fn_flag(server):
    """A real F13 key event carries the SecondaryFn flag; ours should too."""
    e = session(server, [{"t": "dict", "s": 1}, {"t": "dict", "s": 0}])
    for x in e:
        if x["ev"].startswith("key"):
            assert x["mods"] == ["fn"], x


# ------------------------------------------------------------------ robustness

def test_malformed_messages_do_not_kill_connection(server):
    bad = ["not json", "[1,2]", "null", "42", '{"t":"m"}', '{"t":"m","x":"abc","y":1}',
           '{"t":"m","x":NaN,"y":1}', '{"t":"m","x":Infinity,"y":1}', '{"t":"s","x":null,"y":1}',
           '{"t":"zzz"}', '{"t":"key"}', '{"x":1}', '"str"']
    e = session(server, bad + ['{"t":"c"}'], raw=True, pause=0.01)
    # the click after all the junk still went through on the same connection
    assert evs(e, "ldown", "lup") == ["ldown", "lup"]
    # and no junk produced an event (in particular no NaN/inf pointer position)
    assert not [x for x in e if x["ev"] == "move"]


def test_disconnect_releases_mouse_button(server):
    e = session(server, [{"t": "d"}, {"t": "m", "x": 5, "y": 5}])
    # the button is let go as the connection ends (recorded just after "disconnect")
    assert evs(e, "ldown", "drag", "lup") == ["ldown", "drag", "lup"]
    assert evs(e)[-2:] == ["disconnect", "lup"]


def test_disconnect_releases_dictation_key(server):
    e = session(server, [{"t": "dict", "s": 1}])
    assert [(x["ev"], x["key"]) for x in e if x["ev"].startswith("key")] == [
        ("keydown", "f13"), ("keyup", "f13")]
    assert evs(e)[-2:] == ["disconnect", "keyup"]


def test_new_connection_releases_stale_holds(server):
    """A half-open old socket (Wi-Fi blip) must not keep the button/key held
    until its heartbeat times out: a new pad connection releases them."""
    async def go():
        async with aiohttp.ClientSession() as s:
            old = await s.ws_connect(f"ws://127.0.0.1:{server.port}/{TOKEN}/ws")
            await old.send_json({"t": "dict", "s": 1})
            await old.send_json({"t": "d"})
            await asyncio.sleep(0.2)
            new = await s.ws_connect(f"ws://127.0.0.1:{server.port}/{TOKEN}/ws")
            await asyncio.sleep(0.3)
            entries = server.log()
            await new.close()
            await old.close()
            return entries
    mark = server.mark()
    e = run(go())[mark:]
    i = [n for n, x in enumerate(e) if x["ev"] == "connect"][1]
    after_second_connect = evs(e[i:])
    assert "lup" in after_second_connect and "keyup" in after_second_connect


# ------------------------------------------------------------------ startup

def test_port_in_use_is_friendly():
    port = free_port()
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("0.0.0.0", port))
    blocker.listen()
    tok = TMP / f"token-{port}"
    tok.write_text(TOKEN)
    try:
        r = subprocess.run([sys.executable, str(SCRIPT)], env=server_env(port, tok), cwd=TMP,
                           capture_output=True, text=True, timeout=60)
    finally:
        blocker.close()
        tok.unlink(missing_ok=True)
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "already running" in out
    assert "Traceback" not in out


def test_token_file_created_in_overridden_location():
    port = free_port()
    tok = TMP / f"fresh-token-{port}"
    tok.unlink(missing_ok=True)
    # start by hand so the token file does not exist beforehand
    p = subprocess.Popen([sys.executable, str(SCRIPT)], env=server_env(port, tok), cwd=TMP,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 30
        while not (tok.exists() and tok.read_text().strip()):
            assert time.time() < deadline and p.poll() is None
            time.sleep(0.1)
        assert oct(tok.stat().st_mode & 0o777) == "0o600"
    finally:
        p.terminate()                            # this test's own process only
        p.wait(5)
        tok.unlink(missing_ok=True)


# ------------------------------------------------------------------ unit: Mac logic

def test_clamp_multiple_displays():
    pp = load_module()
    Q = pp.Quartz
    main, right = Q.CGRectMake(0, 0, 1000, 800), Q.CGRectMake(1000, -200, 500, 400)
    m = pp.Mac()
    m.bounds = lambda: [main, right]
    m.dry_pos = (990.0, 500.0)
    assert m.clamp(1010, 100) == (1010, 100)          # onto the second display: allowed
    assert m.clamp(1010, 500) == (999, 500)           # below the second display: stay on main
    m.dry_pos = (1200.0, 0.0)
    assert m.clamp(1200, -300) == (1200, -200)        # above the top of the second display


def test_modifier_key_events():
    pp = load_module()
    rec = []
    pp.dry_record = rec.append
    m = pp.Mac()
    m.key("option", True)
    m.key("option", False)
    m.key("v", True, ["control", "shift"])
    m.key("v", False, ["control", "shift"])
    assert [(r["ev"], r["key"], r["mods"]) for r in rec] == [
        ("flags", "option", ["option"]), ("flags", "option", []),
        ("keydown", "v", ["control", "shift"]), ("keyup", "v", ["control", "shift"])]
    assert m.held_keys == set()


def test_release_all_releases_held_modifier():
    pp = load_module()
    rec = []
    pp.dry_record = rec.append
    m = pp.Mac()
    m.key("right_option", True)
    m.release_all()
    assert [(r["ev"], r["key"], r["mods"]) for r in rec] == [
        ("flags", "right_option", ["option"]), ("flags", "right_option", [])]



def test_click_window_and_distance():
    """Clicks within 0.6 s and 12 px of each other count up (double, triple click)."""
    pp = load_module()
    rec = []
    pp.dry_record = rec.append
    now = [100.0]

    class Clock:
        monotonic = staticmethod(lambda: now[0])
    pp.time = Clock
    m = pp.Mac()
    m.dry_pos = (500.0, 500.0)

    def click(dt, dx=0.0, dy=0.0):
        now[0] += dt
        m.dry_pos = (m.dry_pos[0] + dx, m.dry_pos[1] + dy)
        del rec[:]
        m.click()
        assert [r["ev"] for r in rec] == ["ldown", "lup"]
        assert rec[0]["clicks"] == rec[1]["clicks"]
        return rec[0]["clicks"]

    assert click(5) == 1
    assert click(0.59) == 2                 # just inside the window
    assert click(0.59) == 3
    assert click(0.61) == 1                 # just outside: starts again
    assert click(0.1, dx=11) == 2           # 11 px away still counts
    assert click(0.1, dx=12) == 1           # 12 px away does not
    assert click(0.1, dy=-11.5) == 2
    assert click(0.1, dy=13) == 1


def test_click_count_resets_after_pointer_moves_away(server):
    e = session(server, [{"t": "c"}, {"t": "m", "x": 2, "y": 0}, {"t": "c"},
                         {"t": "m", "x": 30, "y": 0}, {"t": "c"}])
    assert [x["clicks"] for x in e if x["ev"] == "ldown"] == [1, 2, 1]


def test_second_screenshot_while_one_is_running_is_ignored(server):
    e = session(server, [{"t": "shot"}, {"t": "shot"}], pause=0.02, close=False)
    e = server.wait_for(lambda e: "keyup" in evs(e), settle=0.5)
    assert evs(e, "screencapture", "keydown", "keyup") == ["screencapture", "keydown", "keyup"]


# ------------------------------------------------------------------ boot id

def page_cfg(server):
    body = server.get()[2]
    return json.loads(re.search(r"const CFG = (\{.*?\});", body).group(1))


def test_page_config(server):
    cfg = page_cfg(server)
    assert set(cfg) >= {"hover", "dictMode", "boot", "mac", "local"}
    assert cfg["dictMode"] == "hold" and cfg["hover"] is False
    assert re.fullmatch(r"[0-9a-f]{8}", cfg["boot"])
    assert server.get()[1]["Cache-Control"] == "no-store"


def test_websocket_sends_boot_id_first(server):
    async def go():
        async with aiohttp.ClientSession() as s:
            ws = await s.ws_connect(f"ws://127.0.0.1:{server.port}/{TOKEN}/ws")
            msg = await ws.receive_json(timeout=3)
            await ws.close()
            return msg
    assert run(go()) == {"boot": page_cfg(server)["boot"]}


def test_boot_id_changes_on_restart():
    a = Server()
    try:
        first = page_cfg(a)["boot"]
        assert page_cfg(a)["boot"] == first          # stable while it runs
    finally:
        a.stop()
    b = Server(port=a.port)
    try:
        assert page_cfg(b)["boot"] != first
    finally:
        b.stop()


# ------------------------------------------------------------------ /state, /ping, /connect, /settings

def test_state_shape(server):
    status, headers, body = server.get("/state")
    st = json.loads(body)
    assert status == 200 and headers["Content-Type"].startswith("application/json")
    assert st["clients"] == 0
    assert isinstance(st["access"], bool) and isinstance(st["screen"], bool)


def test_state_counts_connected_pads(server):
    async def go():
        url = f"ws://127.0.0.1:{server.port}/{TOKEN}/ws"
        seen = []
        async with aiohttp.ClientSession() as s:
            a = await s.ws_connect(url)
            await a.receive_json(timeout=3)          # the boot message: fully registered
            seen.append(await asyncio.to_thread(lambda: server.state()["clients"]))
            b = await s.ws_connect(url)
            await b.receive_json(timeout=3)
            seen.append(await asyncio.to_thread(lambda: server.state()["clients"]))
            await a.close()
            await asyncio.to_thread(wait_until, lambda: server.state()["clients"] == 1)
            seen.append(1)
            await b.close()
        return seen
    assert run(go()) == [1, 2, 1]
    wait_until(lambda: server.state()["clients"] == 0, what="clients back to 0")


def test_state_after_server_side_drop(server):
    async def go():
        async with aiohttp.ClientSession() as s:
            ws = await s.ws_connect(f"ws://127.0.0.1:{server.port}/{TOKEN}/ws")
            await ws.receive_json(timeout=3)
            assert (await asyncio.to_thread(server.state))["clients"] == 1
            await asyncio.to_thread(server.drop)
            await asyncio.to_thread(wait_until, lambda: server.state()["clients"] == 0)
            await ws.close()
    run(go())


def test_ping(server):
    status, headers, body = server.get("/ping")
    assert (status, body) == (200, "ok")
    assert headers["Access-Control-Allow-Origin"] == "*"     # the pad pings across origins


def test_connect_page_has_qr_and_pad_url(server):
    status, headers, body = server.get("/connect", timeout=60)
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert headers["Cache-Control"] == "no-store"
    for placeholder in ("__QR__", "__URL__", "__MAC__"):
        assert placeholder not in body
    qr = re.search(r'<div class="qr">(.*?)</div>', body, re.S).group(1)
    assert qr.startswith("<svg") and qr.rstrip().endswith("</svg>") and "<path" in qr
    # the address typed by hand is the pad page itself (not /connect), on this server
    url = re.search(r"<code>(.*?)</code>", body).group(1)
    assert re.fullmatch(rf"http://\d+\.\d+\.\d+\.\d+:{server.port}/{TOKEN}", url), url


def test_connect_qr_encodes_the_pad_url(server):
    """The QR on /connect is the QR of the address shown beside it."""
    import segno
    body = server.get("/connect", timeout=60)[2]
    url = re.search(r"<code>(.*?)</code>", body).group(1)
    qr = re.search(r'<div class="qr">(.*?)</div>', body, re.S).group(1)
    want = segno.make(url, error="m").svg_inline(scale=6, border=1, dark="#000", light="#fff")
    assert qr == want


def test_connect_bad_token_404(server):
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(f"{server.base}/not-the-token/connect")
    assert ei.value.code == 404


def test_settings_unknown_pane_404(server):
    # only an unknown pane: the real ones open System Settings on this Mac
    req = urllib.request.Request(f"{server.url}/settings/no-such-pane", method="POST")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req)
    assert ei.value.code == 404


# ------------------------------------------------------------------ unit: LAN address

class FakeIpconfig:
    """Stands in for subprocess.run inside the module: answers `ipconfig getifaddr enN`."""

    def __init__(self, addrs, fail=()):
        self.addrs, self.fail, self.asked = addrs, fail, []

    def __call__(self, cmd, **kw):
        assert cmd[:2] == ["ipconfig", "getifaddr"], cmd
        self.asked.append(cmd[2])
        if cmd[2] in self.fail:
            raise subprocess.TimeoutExpired(cmd, 3)
        out = self.addrs.get(cmd[2], "")
        return subprocess.CompletedProcess(cmd, 0 if out else 1, stdout=out + "\n" if out else "", stderr="")


def lan_ip_with(addrs, fail=()):
    pp = load_module()
    fake = FakeIpconfig(addrs, fail)

    class Sub:
        run = fake
    pp.subprocess = Sub
    return pp.lan_ip(), fake.asked


def test_lan_ip_first_hardware_port_with_an_address():
    ip, asked = lan_ip_with({"en0": "192.168.1.20", "en7": "10.0.0.23"})
    assert ip == "192.168.1.20" and asked == ["en0"]


def test_lan_ip_skips_ports_without_address():
    ip, asked = lan_ip_with({"en7": "10.0.0.23"})
    assert ip == "10.0.0.23" and asked == [f"en{i}" for i in range(8)]


def test_lan_ip_skips_link_local():
    assert lan_ip_with({"en0": "169.254.10.20", "en1": "192.168.1.50"})[0] == "192.168.1.50"


def test_lan_ip_only_asks_hardware_ports_never_tunnels():
    ip, asked = lan_ip_with({})
    assert ip == "127.0.0.1"                       # nothing found: loopback, not a VPN address
    assert asked and all(re.fullmatch(r"en\d+", a) for a in asked)


def test_lan_ip_survives_ipconfig_failure():
    assert lan_ip_with({"en1": "192.168.1.50"}, fail=("en0",))[0] == "192.168.1.50"


def test_pad_url_uses_lan_ip_port_and_token():
    pp = load_module()
    pp.lan_ip = lambda: "192.168.1.50"
    pp.PORT = 9999
    assert pp.pad_url({"token": "abc"}) == "http://192.168.1.50:9999/abc"
