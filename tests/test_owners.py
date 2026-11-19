"""Who holds what on the Mac, and in what order things reach it.

Several things press keys on the Mac: each pad's keyboard and rail, Dictate, chords
(Copy, Paste, Switch, zoom), and the Mac's own presses. These tests replay what they send
and check the Mac is never left with something held, never gets a key-up it did not get
a key-down for, and never sees other input inside a chord. Mostly in-process, in dry-run
mode; where the connection matters, over a real socket to a dry-run server."""

import asyncio
import itertools
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import aiohttp
import pytest

from conftest import SCRIPT, TMP, TOKEN, Server, evs, free_port, server_env
from test_server import load_module, run

MOD = {"shift": "shift", "control": "control", "option": "option", "command": "command", "fn": "fn",
       "right_option": "option", "right_command": "command", "right_control": "control"}


@pytest.fixture
def pp():
    """The script as a module (dry run), recording what it would post."""
    pp = load_module()
    pp.rec = []
    pp.dry_record = pp.rec.append
    return pp


def play(pp, *script):
    """Messages (as a pad sends them), pauses (seconds) and calls, in order."""
    async def go():
        for s in script:
            if isinstance(s, (int, float)):
                await asyncio.sleep(s)
            elif callable(s):
                s()
            else:
                pp.handle(s)
        await asyncio.sleep(0.15)
    run(go())
    return pp.rec


def K(k, d, m=(), r=None):
    o = {"t": "k", "k": k, "d": d, "m": list(m)}
    if r is not None:
        o["r"] = r
    return o


def mac_state(rec):
    """Replay the posted events as the Mac would see them: what is down at the end. A
    key-up (or a modifier going up) for something that is not down fails."""
    down = set()
    for e in rec:
        if e["ev"] == "keydown" and not e.get("repeat"):
            assert e["key"] not in down, f"{e['key']} down twice"
            down.add(e["key"])
        elif e["ev"] == "keyup":
            assert e["key"] in down, f"key-up for {e['key']}, which is not down"
            down.discard(e["key"])
        elif e["ev"] == "flags":
            if MOD[e["key"]] in e["mods"]:
                assert e["key"] not in down, f"{e['key']} down twice"
                down.add(e["key"])
            else:
                assert e["key"] in down, f"{e['key']} up, but it is not down"
                down.discard(e["key"])
    return down


def keys(rec):
    return [(e["ev"] + ("+" if e.get("repeat") else ""), e["key"], e["mods"]) for e in rec
            if e["ev"] in ("keydown", "keyup", "flags")]


# ------------------------------------------------------------------ A1: two owners of one key

def test_dictate_option_space_with_a_space_tapped_meanwhile(pp):
    """QA's case: hold Dictate (Option+Space), tap Space, lift Dictate: Option must come up."""
    pp.change_setting("dictation_key", "option+space")
    rec = play(pp, {"t": "dict", "s": 1}, K("space", 1), K("space", 0), {"t": "dict", "s": 0})
    assert mac_state(rec) == set() and pp.mac.owners == {}
    assert keys(rec) == [("flags", "option", ["option"]), ("keydown", "space", ["option"]),
                         ("keyup", "space", ["option"]), ("flags", "option", [])]


def test_dictate_release_leaves_the_keyboards_own_key_down(pp):
    """The reverse: the keyboard holds Option; Dictate (Option+Space) comes and goes."""
    pp.change_setting("dictation_key", "option+space")
    rec = play(pp, K("option", 1), {"t": "dict", "s": 1}, {"t": "dict", "s": 0})
    assert "option" in mac_state(rec)                      # still the keyboard's
    rec = play(pp, K("option", 0))
    assert mac_state(rec) == set() and pp.mac.owners == {}


DICT_KEYS = ["fn", "f13", "option+space", "control+space"]


def shared(dkey):
    """What the keyboard can hold that Dictate also presses: the key, and the modifier."""
    *mods, key = dkey.split("+")
    return [key] + mods


@pytest.mark.parametrize("dkey", DICT_KEYS)
@pytest.mark.parametrize("mode", ["hold", "toggle"])
@pytest.mark.parametrize("tap", [False, True], ids=["holds", "taps"])
@pytest.mark.parametrize("order", ["dictate-first", "keyboard-first"])
def test_dictate_and_keyboard_share_keys(pp, dkey, mode, tap, order):
    pp.change_setting("dictation_key", dkey)
    pp.change_setting("dictation_mode", mode)
    for k in shared(dkey):
        del pp.rec[:]
        pp.mac.release_all()
        del pp.rec[:]
        down, up = K(k, 1), K(k, 0)
        press, lift = {"t": "dict", "s": 1}, {"t": "dict", "s": 0}
        if tap:                                    # the keyboard taps it while Dictate holds
            script = [press, 0.01, down, up, 0.06, lift] if order == "dictate-first" else [down, press, up, 0.06, lift]
        elif order == "dictate-first":             # ...or holds it, and lets go after Dictate
            script = [press, 0.01, down, 0.06, lift, up]
        else:                                      # ...or before it
            script = [down, press, 0.06, up, lift]
        rec = play(pp, *script)
        assert mac_state(rec) == set(), (k, keys(rec))
        assert pp.mac.owners == {}, (k, pp.mac.owners)
        flags = [e for e in rec if e["ev"] == "flags"]
        assert not flags or flags[-1]["mods"] == [], (k, keys(rec))


def test_release_all_clears_every_owner(pp):
    pp.change_setting("dictation_key", "option+space")
    play(pp, K("option", 1), K("shift", 1), {"t": "dict", "s": 1}, K("a", 1, ["option", "shift"]))
    del pp.rec[:]
    pp.mac.release_all()
    assert pp.mac.owners == {}
    assert [e["mods"] for e in pp.rec][-1] == []
    del pp.rec[:]
    rec = play(pp, {"t": "dict", "s": 0}, K("option", 0), K("a", 0))     # late lifts: nothing
    assert keys(rec) == []


# ------------------------------------------------------------------ A3: nothing inside a chord

def test_input_during_a_chord_waits_for_it(pp):
    rec = play(pp, {"t": "copy"}, 0.005, K("a", 1), K("a", 0), {"t": "c"}, {"t": "s", "x": 0, "y": 30})
    names = [(e["ev"], e.get("key")) for e in rec]
    up = names.index(("flags", "command"), 1)               # Command's second flags event: up
    assert names[:up + 1] == [("flags", "command"), ("keydown", "c"), ("keyup", "c"), ("flags", "command")]
    assert names[up + 1:] == [("keydown", "a"), ("keyup", "a"), ("ldown", None), ("lup", None), ("scroll", None)]


def test_dictate_waits_for_a_chord_too(pp):
    rec = play(pp, {"t": "paste"}, 0.005, {"t": "dict", "s": 1}, {"t": "dict", "s": 0})
    assert [(e["ev"], e["key"]) for e in rec] == [
        ("flags", "command"), ("keydown", "v"), ("keyup", "v"), ("flags", "command"),
        ("keydown", "f13"), ("keyup", "f13")]


def test_screenshot_paste_never_lands_inside_a_chord(pp):
    rec = play(pp, {"t": "shot"}, 0.245, {"t": "copy"}, 0.2)
    ks = keys(rec)
    assert ks[:4] == [("flags", "command", ["command"]), ("keydown", "c", ["command"]),
                      ("keyup", "c", ["command"]), ("flags", "command", [])]
    assert ks[4:] == [("keydown", "v", ["control"]), ("keyup", "v", ["control"])]


def test_pointer_events_carry_exactly_the_keyboards_modifiers(pp):
    """Shift-click and Option-drag from the pad's keyboard; Dictate's modifiers are not the
    keyboard's, and nothing else leaks in."""
    rec = play(pp, {"t": "c"}, K("shift", 1), {"t": "c"}, K("shift", 0),
               K("option", 1), {"t": "d"}, {"t": "m", "x": 10, "y": 0}, {"t": "u"}, K("option", 0))
    mouse = [(e["ev"], e["mods"]) for e in rec if e["ev"] in ("ldown", "lup", "drag", "move")]
    assert mouse == [("ldown", []), ("lup", []), ("ldown", ["shift"]), ("lup", ["shift"]),
                     ("ldown", ["option"]), ("drag", ["option"]), ("lup", ["option"])]
    pp.change_setting("dictation_key", "option+space")
    del pp.rec[:]
    rec = play(pp, {"t": "dict", "s": 1}, {"t": "c"}, {"t": "s", "x": 0, "y": 20}, {"t": "dict", "s": 0})
    assert [e["mods"] for e in rec if e["ev"] in ("ldown", "lup", "scroll")] == [[], [], []]


# ------------------------------------------------------------------ A2: chords belong to their pad

async def flood_then_close(port, msgs):
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(f"ws://127.0.0.1:{port}/{TOKEN}/ws")
        await ws.receive()
        for m in msgs:
            await ws.send_str(json.dumps(m))
        await asyncio.sleep(0.05)
        await ws.close()


def test_chords_stop_when_their_pad_goes_away(server):
    m = server.mark()
    run(flood_then_close(server.port, [{"t": "zoom", "d": 1}] * 200 + [{"t": "copy"}] * 50))
    time.sleep(1.5)
    e = server.log(m)
    i = evs(e).index("disconnect")
    after = e[i + 1:]
    assert not [x for x in after if x["ev"] == "keydown"], after            # no key-with-Command
    assert mac_state([x for x in e if x["ev"] in ("keydown", "keyup", "flags")]) == set()
    # at most one of each kind was waiting: a handful of chords ran, not 250
    assert len([x for x in e[:i] if x["ev"] == "keydown"]) <= 6


def test_a_pad_has_one_chord_of_each_kind_waiting(pp):
    rec = play(pp, {"t": "copy"}, {"t": "zoom", "d": 1}, {"t": "zoom", "d": 1}, {"t": "zoom", "d": 1},
               {"t": "zoom", "d": -1}, {"t": "copy"}, {"t": "copy"}, 0.5)
    # the first Copy runs; then one zoom in, one zoom out and one Copy were waiting
    assert [e["key"] for e in rec if e["ev"] == "keydown"] == ["c", "equal", "minus", "c"]


def test_release_all_stops_a_chord_under_way(pp):
    rec = play(pp, {"t": "copy"}, 0.005, pp.mac.release_all, 0.1)
    assert keys(rec) == [("flags", "command", ["command"]), ("flags", "command", [])]
    assert pp.mac.owners == {}


# ------------------------------------------------------------------ B8

@pytest.mark.parametrize("s", ["0", "1", "", 2, 1.0, None, [], "true"])
def test_dictate_s_must_be_0_1_or_a_bool(pp, s):
    rec = play(pp, {"t": "dict", "s": s}, {"t": "dict", "s": 0})
    assert keys(rec) == []


def test_one_mac(pp):
    assert open(SCRIPT).read().count("\nmac = Mac()") == 1


# ------------------------------------------------------------------ B4: peer records

@pytest.mark.parametrize("host,ok", [
    ("studio.local", True), ("Studio.LOCAL", True), ("mac.tail1234.ts.net", True), ("localhost", True),
    ("192.168.1.20", True), ("10.1.2.3", True), ("172.20.0.1", True), ("100.101.102.103", True),
    ("169.254.3.4", True), ("127.0.0.1", True),
    ("8.8.8.8", False), ("example.com", False), ("evil.example/x?y#", False), ("a" * 300, False),
    ("javascript:alert(1)", False), ("172.32.0.1", False), ("local", False), (".local", False),
    ("x..local", False), ("-x.local", False), (5, False), ("", False), ("::1", False)])
def test_peer_hosts_are_only_what_the_certificate_can_vouch_for(host, ok):
    assert load_module().peer_host(host) is ok


@pytest.mark.parametrize("bad", [
    {"id": "x", "hosts": ["example.com"], "port": 8765},           # no host left
    {"id": "x", "hosts": ["studio.local"], "port": "8765"},         # port as text
    {"id": "x", "hosts": ["studio.local"], "port": 0},
    {"id": "x", "hosts": ["studio.local"], "port": 70000},
    {"id": "x", "hosts": ["studio.local"], "port": 8765, "tls": -7},
    {"id": "x", "hosts": ["studio.local"], "port": 8765, "tls": 65536},
    {"id": "x", "hosts": ["studio.local"], "port": True},
    {"id": "../../../tmp/evil", "hosts": ["studio.local"], "port": 8765},
    {"id": "", "hosts": ["studio.local"], "port": 8765}])
def test_bad_peer_records_are_refused(bad):
    with pytest.raises(ValueError):
        load_module().clean_peer(bad)


def test_bad_hosts_are_dropped_from_a_good_peer():
    p = load_module().clean_peer({"id": "abc", "name": "Mac", "port": 8765, "tls": 0,
                                  "hosts": ["evil.example", "Studio.local", "8.8.8.8", "192.168.1.9"]})
    assert p["hosts"] == ["studio.local", "192.168.1.9"]


def test_peer_endpoint_and_macs_json_refuse_bad_records(server):
    def post(body):
        req = urllib.request.Request(server.url + "/peer", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code
    assert post({"id": "evil", "hosts": ["evil.example.com"], "port": 80}) == 400
    assert post({"id": "evil", "hosts": ["studio.local"], "port": 80, "tls": -7}) == 400
    assert json.loads(server.get("/macs")[2])["peers"] == []


def test_macs_json_with_bad_records_loads_the_good_ones(pp, tmp_path):
    pp.HOME = tmp_path
    (tmp_path / "macs.json").write_text(json.dumps({
        "a": {"id": "a", "hosts": ["studio.local"], "port": 8765},
        "b": {"id": "b", "hosts": ["evil.example"], "port": 8765},
        "c": {"id": "c", "hosts": ["10.0.0.2"], "port": "x"}}))
    assert list(pp.load_peers()) == ["a"]


# ------------------------------------------------------------------ B2: unreadable files

# (a garbled port file is checked in-process below: started for real, it would look at 8765)
@pytest.mark.parametrize("name,text", [("settings.json", "[" * 200000 + "]" * 200000),
                                       ("settings.json", "\udcff not text"),
                                       ("macs.json", "{" * 100000)])
def test_an_unreadable_file_never_stops_the_start(name, text):
    port = free_port()
    home = TMP / f"home-bad-{port}"
    home.mkdir()
    (home / name).write_bytes(text.encode("utf-8", "surrogateescape"))
    tok = TMP / f"token-bad-{port}"
    tok.write_text(TOKEN)
    env = server_env(port, tok, PENCILPAD_HOME=str(home))
    p = subprocess.Popen([sys.executable, str(SCRIPT)], env=env, cwd=TMP,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        lines = []
        deadline = time.time() + 30
        while time.time() < deadline:
            line = p.stdout.readline()
            if not line:
                break
            lines.append(line)
            if "Ctrl+C here to stop" in line:
                break
        out = "".join(lines)
        assert "Traceback" not in out and "Ctrl+C here to stop" in out, out
        said = [l for l in lines if "could not be read" in l.lower() or "could not read" in l.lower()]
        assert len(said) == 1, out
    finally:
        p.terminate()                  # this test's own process only
        p.wait(5)


# ------------------------------------------------------------------ B3, B5: pairing requests

def pair_post(url, body, headers=None):
    req = urllib.request.Request(url + "/pair", data=body if isinstance(body, bytes) else json.dumps(body).encode(),
                                 method="POST", headers=headers if headers is not None else {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("headers,code", [
    ({"Content-Type": "text/plain"}, 415),
    ({}, 415),
    ({"Content-Type": "application/json", "Origin": "http://evil.example"}, 403),
    ({"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}, 403),
    ({"Content-Type": "application/json", "Sec-Fetch-Site": "same-site"}, 403)])
def test_pair_only_takes_json_from_its_own_page(server, headers, code):
    before = server.token_file.read_text()
    assert pair_post(server.url, b'{"link": "http://127.0.0.1:1/x"}', headers) == code
    assert server.token_file.read_text() == before


def test_pair_from_its_own_page_is_allowed(server):
    h = {"Content-Type": "application/json", "Origin": server.base, "Sec-Fetch-Site": "same-origin"}
    assert pair_post(server.url, {"link": "http://127.0.0.1:1/x"}, h) == 400        # reached: a bad link


def test_two_pair_requests_at_once(server):
    others = [Server(token="tok-one-111111"), Server(token="tok-two-222222")]
    try:
        res = {}
        ths = [threading.Thread(target=lambda o=o: res.update({o.token: pair_post(server.url, {"link": o.url})}))
               for o in others]
        [t.start() for t in ths]
        [t.join() for t in ths]
        assert sorted(res.values()) in ([200, 409], [200, 200]), res
        file_tok = server.token_file.read_text().strip()
        live = []
        for o in others:
            try:
                urllib.request.urlopen(f"{server.base}/{o.token}/ping", timeout=3)
                live.append(o.token)
            except urllib.error.HTTPError:
                pass
        assert live == [file_tok]                       # the link in use is the one saved
    finally:
        for o in others:
            o.stop()


# ------------------------------------------------------------------ B6: ports

@pytest.fixture
def ports(monkeypatch, tmp_path):
    pp = load_module()
    monkeypatch.delenv("PENCILPAD_PORT", raising=False)
    monkeypatch.delenv("PENCILPAD_TLS_PORT", raising=False)
    pp.HOME = tmp_path
    pp.pencilpad_at = lambda port: False
    while True:
        p = free_port()
        if p < 65000 and all(pp.port_free(p + i) for i in range(3)):
            break
    pp.DEFAULT_PORT = p
    return pp


@pytest.mark.parametrize("value", ["abc", "99999", "0", "-5", "8765x"])
def test_a_bad_pencilpad_port_says_so(ports, monkeypatch, value):
    monkeypatch.setenv("PENCILPAD_PORT", value)
    with pytest.raises(SystemExit) as e:
        ports.choose_port()
    assert "PENCILPAD_PORT must be a port number from 1 to 65535" in str(e.value)


@pytest.mark.parametrize("text", ["", "abc", "0", "70000", "-5"])
def test_an_unreadable_saved_port_is_chosen_again_and_said(ports, text):
    (ports.HOME / "port").write_text(text)
    port, note = ports.choose_port()
    assert port == ports.DEFAULT_PORT and "could not be read" in note and str(port) in note
    assert (ports.HOME / "port").read_text().strip() == str(port)


def test_a_saved_port_with_spaces_is_read(ports):
    (ports.HOME / "port").write_text("  9123 \n")
    assert ports.choose_port() == (9123, "")


# ------------------------------------------------------------------ round 5: a pad leaving

async def pads(server, n):
    s = aiohttp.ClientSession()
    out = []
    for _ in range(n):
        ws = await s.ws_connect(f"ws://127.0.0.1:{server.port}/{TOKEN}/ws")
        await ws.receive_json(timeout=3)
        out.append(ws)
    return s, out


def two_pads(server, script):
    """script(a, b) sends what the two pads do; returns everything recorded meanwhile."""
    async def go():
        s, (a, b) = await pads(server, 2)
        try:
            await script(a, b)
            await asyncio.sleep(0.3)
        finally:
            await a.close()
            await b.close()
            await s.close()
    m = server.mark()
    run(go())
    time.sleep(0.2)
    return server.log(m)


def after_first_leaves(e):
    i = evs(e).index("disconnect")
    j = [n for n, x in enumerate(e) if x["ev"] == "disconnect"][1] if evs(e).count("disconnect") > 1 else len(e)
    return [(x["ev"], x.get("key"), x.get("mods")) for x in e[i:j] if x["ev"] in ("flags", "keydown", "keyup", "lup")]


def send(ws, *msgs):
    async def go():
        for m in msgs:
            await ws.send_json(m)
        await asyncio.sleep(0.15)
    return go()


def test_a_pad_leaving_keeps_a_modifier_another_pad_holds(server):
    async def script(a, b):
        await send(a, K("shift", 1))
        await send(b, K("shift", 1))
        await a.close()                                  # A goes: B still holds Shift
        await asyncio.sleep(0.2)
        await send(b, K("x", 1, ["shift"]), K("x", 0, ["shift"]))
    e = two_pads(server, script)
    assert after_first_leaves(e) == [("keydown", "x", ["shift"]), ("keyup", "x", ["shift"])]
    assert mac_state([x for x in e if x["ev"] in ("flags", "keydown", "keyup")]) == set()   # the last pad: all up


def test_a_pad_leaving_keeps_a_key_another_pad_holds(server):
    async def script(a, b):
        await send(a, K("a", 1))
        await send(b, K("a", 1))
        await a.close()
        await asyncio.sleep(0.2)
        await send(b, K("a", 1, r=1), K("a", 0))
    e = two_pads(server, script)
    assert after_first_leaves(e) == [("keydown+", "a", []), ("keyup", "a", [])] or \
        after_first_leaves(e) == [("keydown", "a", []), ("keyup", "a", [])]
    assert [(x["ev"], x.get("key")) for x in e if x["ev"] == "keyup"] == [("keyup", "a")]


@pytest.mark.parametrize("leaves", ["A", "B"])
def test_dictate_goes_with_the_pad_that_pressed_it(server, leaves):
    async def script(a, b):
        await send(a, {"t": "dict", "s": 1})
        await (a if leaves == "A" else b).close()
        await asyncio.sleep(0.2)
    e = two_pads(server, script)
    released = ("keyup", "f13", ["fn"]) in after_first_leaves(e)
    assert released == (leaves == "A")
    assert mac_state([x for x in e if x["ev"] in ("flags", "keydown", "keyup")]) == set()


def test_the_last_pad_leaving_lets_go_of_everything(server):
    async def script(a, b):
        await send(b, K("command", 1), K("option", 1, ["command"]), {"t": "dict", "s": 1}, {"t": "d"})
        await b.close()
        await asyncio.sleep(0.2)
        await send(a, K("shift", 1))
    e = two_pads(server, script)
    assert mac_state([x for x in e if x["ev"] in ("flags", "keydown", "keyup")]) == set()
    assert evs(e)[-1] == "flags" and e[-1]["mods"] == []          # A, the last, took Shift with it


# ------------------------------------------------------------------ round 5: the queue

def test_queued_moves_and_scrolls_add_up(pp):
    rec = play(pp, {"t": "copy"}, {"t": "m", "x": 1, "y": 2}, {"t": "m", "x": 3, "y": 4},
               {"t": "c"}, {"t": "m", "x": 5, "y": 5}, {"t": "s", "x": 0, "y": 10}, {"t": "s", "x": 0, "y": 10})
    assert [x["ev"] for x in rec if x["ev"] in ("move", "ldown", "lup", "scroll")] == \
        ["move", "ldown", "lup", "move", "scroll"]                 # in order, one move per run of moves
    first = [x for x in rec if x["ev"] == "move"][0]
    # (1, 2) + (3, 4) arrived as one move of (4, 6): its direction says both were added
    assert abs((first["y"] - 540) / (first["x"] - 960) - 1.5) < 0.01, first


def test_a_flood_behind_a_chord_stays_small(pp):
    async def go():
        pp.handle({"t": "copy"})
        for _ in range(20000):
            pp.handle({"t": "m", "x": 1, "y": 0})
        n = len(pp.waiting)
        for i in range(500):                                       # nothing to sum: up to the cap
            pp.handle({"t": "m" if i % 2 else "s", "x": 1, "y": 1})
        full = len(pp.waiting)
        for i in range(300):
            pp.handle(K("q", 1))
        kept = len(pp.waiting)
        pp.handle(K("q", 0))                                       # a release is always taken
        pp.handle({"t": "u"})
        last = [m["t"] for _, m, _ in list(pp.waiting)[-2:]]
        await asyncio.sleep(0.3)
        return n, full, kept, len(pp.waiting), last
    n, full, kept, after, last = run(go())
    assert n == 1 and full == pp.MAX_WAITING and kept == pp.MAX_WAITING
    assert last == ["k", "u"] and after == 0


# ------------------------------------------------------------------ round 5: a fixed action's own key

def test_copy_while_the_keyboard_holds_c_still_copies(pp):
    rec = play(pp, K("c", 1), {"t": "copy"}, 0.1, K("c", 0))
    assert keys(rec) == [("keydown", "c", []), ("flags", "command", ["command"]), ("keyup", "c", []),
                         ("keydown", "c", ["command"]), ("keyup", "c", ["command"]), ("flags", "command", [])]
    assert mac_state(rec) == set() and pp.mac.owners == {}


def test_screenshot_paste_while_the_keyboard_holds_v_still_pastes(pp):
    rec = play(pp, K("v", 1), {"t": "shot"}, 0.4, K("v", 0))
    assert keys(rec)[-3:] == [("keyup", "v", []), ("keydown", "v", ["control"]), ("keyup", "v", ["control"])]
    assert mac_state(rec) == set() and pp.mac.owners == {}


# ------------------------------------------------------------------ the same page on a new socket

async def pad_socket(s, server, pad_id=None):
    q = "" if pad_id is None else "?pad=" + pad_id
    ws = await s.ws_connect(f"ws://127.0.0.1:{server.port}/{TOKEN}/ws{q}", heartbeat=None)
    await ws.receive_json(timeout=3)
    return ws


def held_moves(e, i):
    return [(x["ev"], x.get("key"), x.get("mods")) for x in e[i:] if x["ev"] in ("flags", "keyup", "lup")]


def test_the_same_page_back_on_a_new_socket_lets_go_at_once(server):
    """Wi-Fi blips with Shift locked mid-drag: the old socket is left half-open (never
    closed by this side), the same page reconnects, and the Mac lets go straight away."""
    async def go():
        async with aiohttp.ClientSession() as s:
            old = await pad_socket(s, server, "page-one")
            await old.send_json(K("shift", 1))
            await old.send_json(K("a", 1, ["shift"]))
            await old.send_json({"t": "d"})
            await asyncio.sleep(0.2)
            before = len(await asyncio.to_thread(server.log))
            new = await pad_socket(s, server, "page-one")        # the old one is still open
            await asyncio.sleep(0.2)
            e = await asyncio.to_thread(server.log)
            await new.send_json({"t": "c"})                        # and the new one works
            await asyncio.sleep(0.2)
            e2 = await asyncio.to_thread(server.log)
            await new.close()
            return e, before, e2
    e, before, e2 = run(go())
    # the button comes up first (still with Shift, as a person lets go), then a, then Shift
    assert held_moves(e, before) == [("lup", None, ["shift"]), ("keyup", "a", ["shift"]), ("flags", "shift", [])]
    assert "ldown" in evs(e2[len(e):])


def test_a_different_pad_keeps_what_it_holds(server):
    async def go():
        async with aiohttp.ClientSession() as s:
            other = await pad_socket(s, server, "page-two")
            await other.send_json(K("shift", 1))
            old = await pad_socket(s, server, "page-one")
            await old.send_json(K("shift", 1))
            await old.send_json(K("q", 1, ["shift"]))
            await asyncio.sleep(0.2)
            before = len(await asyncio.to_thread(server.log))
            new = await pad_socket(s, server, "page-one")
            await asyncio.sleep(0.2)
            e = await asyncio.to_thread(server.log)
            for w in (new, other, old):
                await w.close()
            return e, before
    e, before = run(go())
    assert held_moves(e, before) == [("keyup", "q", ["shift"])]     # page-two's Shift stays down


@pytest.mark.parametrize("pad_id", [None, "", "../evil", "a" * 41, "has space", "ü"])
def test_a_page_without_a_usable_id_is_left_as_it_is(server, pad_id):
    """An older page (no id) or a malformed id: two such sockets are two pads."""
    async def go():
        async with aiohttp.ClientSession() as s:
            first = await pad_socket(s, server, pad_id)
            await first.send_json(K("shift", 1))
            await asyncio.sleep(0.2)
            before = len(await asyncio.to_thread(server.log))
            second = await pad_socket(s, server, pad_id)
            await second.send_json({"t": "c"})
            await asyncio.sleep(0.2)
            e = await asyncio.to_thread(server.log)
            await first.send_json(K("x", 1, ["shift"]))          # the first still works
            await first.send_json(K("x", 0, ["shift"]))
            await asyncio.sleep(0.2)
            e2 = await asyncio.to_thread(server.log)
            await second.close()
            await first.close()
            return e, before, e2
    e, before, e2 = run(go())
    # nothing of the first's was let go (the second's own click is all there is)
    assert [x for x in held_moves(e, before) if x[0] != "lup"] == []
    assert evs(e[before:], "ldown", "lup") == ["ldown", "lup"]
    assert ("keydown", "x") in [(x["ev"], x.get("key")) for x in e2[len(e):]]


def test_the_page_keeps_its_id_across_reconnects(browser, server):
    from conftest import open_pad
    urls = []
    with open_pad(browser, server, route=lambda page: page.on("websocket", lambda w: urls.append(w.url))) as p:
        server.drop()
        p.wait_offline()
        p.wait_online()
    ids = {u.split("?pad=")[1] for u in urls}
    assert len(urls) == 2 and len(ids) == 1 and __import__("re").fullmatch(r"[a-z0-9]{9}", ids.pop())
