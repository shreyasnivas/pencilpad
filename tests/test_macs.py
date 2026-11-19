"""Several Macs, the secure port, and Dictate through the iPad's microphone.

All against dry-run servers (see conftest.py); nothing reaches the real Mac.
"""

import json
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from conftest import SCRIPT, TMP, TOKEN, Server, evs, free_port, open_pad, server_env, wait_until

FAKE = TMP / "fake-transcribe.sh"


def home(server):
    return TMP / f"home-{server.port}"


def post(url, data=b"", headers=None):
    req = urllib.request.Request(url, data=data, method="POST", headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.headers, r.read().decode()


def pair_cli(port, link):
    """Run `pencilpad.py --pair <link>` as the Mac that will listen on `port`."""
    env = server_env(port, TMP / f"token-{port}")
    return subprocess.run([sys.executable, str(SCRIPT), "--pair", link], env=env, cwd=TMP,
                          capture_output=True, text=True, timeout=20)


@pytest.fixture
def two_macs():
    """Two paired servers. The second was paired with the first before it started."""
    a = Server()
    port_b = free_port()
    done = pair_cli(port_b, a.url)
    assert done.returncode == 0, done.stdout + done.stderr
    b = Server(port=port_b)
    servers = [a, b]
    yield servers
    for s in servers:
        s.stop()


# ------------------------------------------------------------------ pairing

def test_pair_introduces_both_macs(two_macs):
    a, b = two_macs
    seen_by_a = json.loads(a.get("/macs")[2])
    seen_by_b = json.loads(b.get("/macs")[2])
    assert [p["port"] for p in seen_by_a["peers"]] == [b.port]
    assert [p["port"] for p in seen_by_b["peers"]] == [a.port]
    assert seen_by_a["self"]["id"] == seen_by_b["peers"][0]["id"] != seen_by_b["self"]["id"]
    assert a.state()["peers"] and b.state()["peers"]


def test_pair_rejects_a_link_that_is_not_pencilpad(server):
    done = pair_cli(free_port(), "http://127.0.0.1:9/nothing")
    assert done.returncode != 0 and "Could not reach" in done.stderr
    done = pair_cli(free_port(), "not a link")
    assert done.returncode != 0 and "isn't a PencilPad link" in done.stderr


def test_peer_needs_the_token_and_a_sane_record(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        post(f"{server.base}/wrong/peer", b"{}")
    assert e.value.code == 404
    for bad in (b"{}", b"nonsense", json.dumps({"id": "x", "hosts": [], "port": 1}).encode(),
                json.dumps({"id": "x", "hosts": ["h"], "port": 99999}).encode()):
        with pytest.raises(urllib.error.HTTPError) as e:
            post(server.url + "/peer", bad, {"Content-Type": "application/json"})
        assert e.value.code == 400
    assert json.loads(server.get("/macs")[2])["peers"] == []


# ------------------------------------------------------------------ the secure port

def test_secure_port_uses_this_macs_own_certificate():
    tls = free_port()
    s = Server(extra_env={"PENCILPAD_TLS_PORT": str(tls)})
    try:
        assert s.state()["tls"] == tls
        trusted = ssl.create_default_context(cafile=str(home(s) / "ca.pem"))
        with urllib.request.urlopen(f"https://127.0.0.1:{tls}/{TOKEN}/ping", context=trusted, timeout=5) as r:
            assert json.loads(r.read())["tls"] == tls
        with pytest.raises(urllib.error.URLError):             # nobody else trusts it
            urllib.request.urlopen(f"https://127.0.0.1:{tls}/{TOKEN}/ping",
                                   context=ssl.create_default_context(), timeout=5)
        with urllib.request.urlopen(s.url + "/ca.crt", timeout=5) as r:
            assert r.headers["Content-Type"] == "application/x-x509-ca-cert"
            der = r.read()
        assert ssl.DER_cert_to_PEM_cert(der).strip() == (home(s) / "ca.pem").read_text().strip()
        assert (home(s) / "ca.key").stat().st_mode & 0o077 == 0
        assert "Download Certificate" in s.get("/secure")[2]
        status, headers, body = s.get("/sw.js")
        assert headers["Service-Worker-Allowed"] == "/" and "caches" in body
    finally:
        s.stop()


def test_no_secure_port_when_switched_off(server):
    assert server.state()["tls"] == 0
    assert not (home(server) / "ca.pem").exists()


# ------------------------------------------------------------------ speech

@pytest.fixture
def speech_server():
    FAKE.write_text('#!/bin/sh\n[ -s "$1" ] || exit 5\necho "  hello   from the iPad "\n')
    FAKE.chmod(0o755)
    s = Server(extra_env={"PENCILPAD_TRANSCRIBE": str(FAKE)})
    yield s
    s.stop()


def test_speech_types_what_was_said(speech_server):
    s = speech_server
    assert s.state()["speech"] is True
    status, headers, body = post(s.url + "/speech?rate=16000", b"\0\0" * 8000)
    assert json.loads(body) == {"text": "hello from the iPad", "error": ""}
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert [e for e in s.log() if e["ev"] == "text"] == [{"ev": "text", "s": "hello from the iPad "}]


def test_speech_refuses_bad_requests(speech_server):
    for path in ("/speech", "/speech?rate=12", "/speech?rate=abc"):
        with pytest.raises(urllib.error.HTTPError) as e:
            post(speech_server.url + path, b"\0\0" * 100)
        assert e.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as e:
        post(f"{speech_server.base}/wrong/speech?rate=16000", b"\0\0")
    assert e.value.code == 404
    assert evs(speech_server.log(), "text") == []


def test_speech_without_a_transcriber_types_nothing(server):
    status, headers, body = post(server.url + "/speech?rate=16000", b"\0\0" * 8000)
    assert json.loads(body) == {"text": "", "error": "unavailable"}
    assert evs(server.log(), "text") == []


def test_dictate_uses_the_ipad_microphone(browser, speech_server):
    """With a transcriber on the Mac, holding Dictate records on the pad and types the text."""
    s = speech_server
    with open_pad(browser, s) as p:
        p.page.wait_for_function("cur && cur.speech")
        m = s.mark()
        p.down(*p.center("dict"))
        p.page.wait_for_function("document.getElementById('dictSub').textContent === 'Listening…'")
        # "Listening…" shows when the microphone starts, not when audio arrives: hold until
        # the pad really has 0.4 s of it (it sends nothing under 0.3 s)
        p.page.wait_for_function("rec && mic && rec.n / mic.ctx.sampleRate >= 0.4", timeout=8000)
        p.up()
        got = s.wait_for(lambda e: "text" in evs(e), since=m, timeout=8)
        assert [e["s"] for e in got if e["ev"] == "text"] == ["hello from the iPad "]
        assert evs(got, "keydown", "keyup") == []          # the dictation key was not pressed
        p.page.wait_for_function("document.getElementById('dictSub').textContent === 'Hold to talk'")
        assert not p.has_class("dict", "on")


def test_dictate_with_the_macs_app_never_opens_the_microphone(browser, speech_server):
    """Source "Mac's dictation app": Dictate presses the key even when the iPad could record."""
    s = speech_server
    with open_pad(browser, s) as p:
        p.page.wait_for_function("cur && cur.speech")
        p.page.evaluate("""() => { window.__mic = 0; const gum = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
            navigator.mediaDevices.getUserMedia = c => { window.__mic++; return gum(c); }; }""")
        p.page.evaluate("setMac('dictation_source', 'mac')")
        s.wait_for(lambda e: True)
        time.sleep(0.2)
        m = s.mark()
        p.down(*p.center("dict"))
        s.wait_for(lambda e: "keydown" in evs(e), since=m)
        p.up()
        e = s.wait_for(lambda e: "keyup" in evs(e), since=m)
        assert [x["key"] for x in e if x["ev"] == "keydown"] == ["f13"] and "text" not in evs(e)
        assert p.page.evaluate("window.__mic") == 0 and p.page.evaluate("mic") is None


def test_dictate_released_at_once_sends_nothing(browser, speech_server):
    s = speech_server
    with open_pad(browser, s) as p:
        p.page.wait_for_function("cur && cur.speech")
        m = s.mark()
        p.tap(*p.center("dict"), hold=0.02)
        time.sleep(1.0)
        assert evs(s.log(m), "text", "keydown") == []


def test_dictate_presses_the_key_without_a_transcriber(pad):
    m = pad.server.mark()
    pad.down(*pad.center("dict"))
    pad.server.wait_for(lambda e: "keydown" in evs(e), since=m)
    pad.up()
    pad.server.wait_for(lambda e: "keyup" in evs(e), since=m)


# ------------------------------------------------------------------ the pad and several Macs

def row_xy(p, n):
    return tuple(p.page.evaluate(
        "n => { const r = document.querySelectorAll('#macList .mac')[n].getBoundingClientRect();"
        " return [r.x + r.width / 2, r.y + r.height / 2]; }", n))


def rows(p):
    return p.page.evaluate("[...document.querySelectorAll('#macList .mac span')].map(s => s.textContent)")


def choose(p, n):
    p.press("status")
    p.page.wait_for_function("document.body.classList.contains('picking')")
    wait_until(lambda: rows(p)[n] == "Available", what="the other Mac to show as available")
    p.tap(*row_xy(p, n), id=8)


def on(p, server):
    p.page.wait_for_function("p => !!cur && cur.base.endsWith(':' + p)", arg=server.port, timeout=10000)
    p.wait_online()


def test_pad_lists_both_macs_and_switches(browser, two_macs):
    a, b = two_macs
    with open_pad(browser, a) as p:
        on(p, a)
        p.press("status")
        wait_until(lambda: rows(p) == ["Connected", "Available"], what="both Macs listed")
        mark_a, mark_b = a.mark(), b.mark()
        p.tap(*row_xy(p, 1), id=8)
        on(p, b)
        assert not p.page.evaluate("document.body.classList.contains('picking')")
        b.wait_for(lambda e: "connect" in evs(e), since=mark_b)
        a.wait_for(lambda e: "disconnect" in evs(e), since=mark_a)
        mark_a, mark_b = a.mark(), b.mark()
        p.tap()
        b.wait_for(lambda e: "lup" in evs(e), since=mark_b)
        assert evs(a.log(mark_a), "ldown", "lup") == []       # the click went to one Mac only
        p.page.reload()                                       # the choice is remembered
        on(p, b)


def test_pad_falls_back_and_returns_to_the_chosen_mac(browser, two_macs):
    a, b = two_macs
    with open_pad(browser, a) as p:
        choose(p, 1)
        on(p, b)
        b.stop()
        on(p, a)                                              # the chosen Mac is gone: use the other
        m = a.mark()
        p.tap()
        a.wait_for(lambda e: "lup" in evs(e), since=m)
        two_macs[1] = b = Server(port=b.port)                 # it comes back
        p.set_hidden(True)
        p.set_hidden(False)                                   # ...and so does the pad, next time it is opened
        on(p, b)


def test_tapping_the_status_with_one_mac_just_shows_it(pad):
    pad.press("status")
    wait_until(lambda: rows(pad) == ["Connected"], what="the one Mac listed")
    m = pad.server.mark()
    pad.tap()                                                 # a touch on the pad only closes the list
    pad.page.wait_for_function("!document.body.classList.contains('picking')")
    time.sleep(0.3)
    assert evs(pad.server.log(m), "ldown", "lup") == []
    pad.tap()                                                 # the next one clicks as usual
    pad.server.wait_for(lambda e: "lup" in evs(e), since=m)


# ------------------------------------------------------------------ the Home Screen app, over HTTPS

def test_saved_pad_opens_without_its_mac_and_finds_the_other(browser):
    """Over HTTPS the pad keeps a copy of itself. With the Mac it was added from switched
    off, it still opens, and connects to the other paired Mac."""
    tls_a, tls_b, port_b = free_port(), free_port(), free_port()
    a = Server(extra_env={"PENCILPAD_TLS_PORT": str(tls_a)})
    env = server_env(port_b, TMP / f"token-{port_b}", PENCILPAD_TLS_PORT=str(tls_b))
    done = subprocess.run([sys.executable, str(SCRIPT), "--pair", a.url], env=env, cwd=TMP,
                          capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stdout + done.stderr
    b = Server(port=port_b, extra_env={"PENCILPAD_TLS_PORT": str(tls_b)})
    # its own browser: only this flag lets a service worker load from a test certificate
    own = browser.browser_type.launch(args=["--ignore-certificate-errors"])
    ctx = own.new_context(has_touch=True, viewport={"width": 1180, "height": 820},
                          ignore_https_errors=True)
    try:
        ctx.add_init_script("try { localStorage.setItem('pencilpad-tour', '1'); } catch (_) {}")
        page = ctx.new_page()
        page.goto(f"https://127.0.0.1:{tls_a}/{TOKEN}")
        page.wait_for_function("!!cur && cur.base === location.origin")
        assert a.state()["secure"] is True
        page.wait_for_function(
            "navigator.serviceWorker.ready.then(() => caches.open('pencilpad-1'))"
            ".then(c => c.keys()).then(k => k.length === 1)", timeout=10000)
        page.wait_for_function("!!navigator.serviceWorker.controller", timeout=10000)
        a.stop()
        mark = b.mark()
        page.reload()                                         # served from the saved copy
        page.wait_for_function("p => !!cur && cur.base === 'https://127.0.0.1:' + p", arg=tls_b, timeout=15000)
        b.wait_for(lambda e: "connect" in evs(e), since=mark)
        assert b.state()["secure"] is True
    finally:
        own.close()
        a.stop()
        b.stop()
