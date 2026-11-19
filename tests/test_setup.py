"""Getting set up: the Python the scripts pick, the port, the Mac's setup page and its
Pair box, the iPad's "Install as an app" page, and where the pad leads to them.

All against dry-run servers on their own ports, with their own token files and homes."""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from conftest import HERE, SCRIPT, TMP, TOKEN, TOUR_SEEN, VIEW, Server, evs, free_port, open_pad, server_env, wait_until
import aiohttp

from test_server import load_module, run

REPO = HERE.parent
STANDALONE = "Object.defineProperty(Navigator.prototype, 'standalone', {get: () => true});"


def post(url, body, timeout=10):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, (json.loads(e.read() or b"null") if e.code != 404 else None)


def status(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return None                       # not listening (yet)


# ------------------------------------------------------------------ the Python the scripts use

FAKE = """#!/bin/sh
# a pretend python {v}: answers the version check, says whether pip is beside it, and
# makes a (whole) virtualenv of itself
case "$1" in
  -c) exit {code} ;;
  --version) echo "Python {v}" ;;
  -m) if [ "$2" = pip ]; then [ -e "$(dirname "$0")/pip" ]; exit $?; fi
      mkdir -p "$3/bin" && cp "$0" "$3/bin/python" && touch "$3/bin/pip" ;;
esac
"""


def fake_python(dir, name, version):
    dir.mkdir(parents=True, exist_ok=True)
    good = tuple(int(x) for x in version.split(".")[:2]) >= (3, 10)
    p = dir / name
    p.write_text(FAKE.format(v=version, code=0 if good else 1))
    p.chmod(0o755)
    return p


def finder(script, path, dirs, tmp):
    """Run a line of zsh with find_python.sh sourced, on a made-up PATH and search dirs."""
    env = {"PATH": f"{path}:/usr/bin:/bin" if path else "/usr/bin:/bin", "HOME": str(tmp),
           "PENCILPAD_PYTHON_DIRS": ":".join(map(str, dirs))}
    return subprocess.run(["/bin/zsh", "-f", "-c", f"source {REPO / 'find_python.sh'}; {script}"],
                          env=env, cwd=tmp, capture_output=True, text=True, timeout=20)


@pytest.fixture
def pytmp():
    d = TMP / f"py-{time.monotonic_ns()}"
    d.mkdir()
    return d


def test_finder_skips_an_old_python3_for_a_newer_one(pytmp):
    bin = pytmp / "bin"
    fake_python(bin, "python3", "3.9.6")
    good = fake_python(bin, "python3.12", "3.12.4")
    out = finder('find_python && print -r -- "$PY"', bin, [], pytmp)
    assert out.returncode == 0 and out.stdout.strip() == str(good)


def test_finder_looks_in_homebrews_folders_too(pytmp):
    bin, brew = pytmp / "bin", pytmp / "homebrew"
    fake_python(bin, "python3", "3.9.6")
    good = fake_python(brew, "python3", "3.13.1")
    out = finder('find_python && print -r -- "$PY"', bin, [pytmp / "nowhere", brew], pytmp)
    assert out.returncode == 0 and out.stdout.strip() == str(good)


def test_finder_says_what_is_needed_when_there_is_none(pytmp):
    bin = pytmp / "bin"
    fake_python(bin, "python3", "3.9.6")
    out = finder('find_python', bin, [pytmp / "nowhere"], pytmp)
    assert out.returncode == 1
    assert "needs Python 3.10 or newer" in out.stderr and "brew install python" in out.stderr


def test_finder_takes_search_folders_with_spaces_in_them(pytmp):
    bin, odd = pytmp / "bin", pytmp / "My Pythons" / "bin"
    fake_python(bin, "python3", "3.9.6")
    good = fake_python(odd, "python3.12", "3.12.1")
    out = finder('find_python && print -r -- "$PY"', bin, [pytmp / "nowhere", odd], pytmp)
    assert out.returncode == 0 and out.stdout.strip() == str(good)


def test_a_half_made_virtualenv_is_made_again(pytmp):
    """Python there but no pip (an interrupted first run): made again, not left broken."""
    bin = pytmp / "bin"
    fake_python(bin, "python3.12", "3.12.4")
    fake_python(pytmp / "venv" / "bin", "python", "3.12.4")              # no pip beside it
    out = finder('make_venv venv && venv/bin/python -m pip --version && print whole', bin, [], pytmp)
    assert out.returncode == 0 and "Making venv again" in out.stdout and out.stdout.strip().endswith("whole")


def test_the_scripts_work_through_a_link(pytmp):
    """run_tests.sh started through a symbolic link still finds its own folder."""
    link = pytmp / "tests-link.sh"
    link.symlink_to(REPO / "run_tests.sh")
    out = subprocess.run(["/bin/zsh", str(link), "--co", "-q", "-k", "no_test_has_this_name"], cwd=pytmp,
                         capture_output=True, text=True, timeout=120)
    # 5: pytest ran and found no test by that name (127 would be: the folder was not found)
    assert out.returncode == 5 and "No such file" not in out.stderr, (out.returncode, out.stdout + out.stderr)
    for script in ("run.sh", "run_tests.sh", "app/build.sh"):
        assert '"${0:A:h}' in (REPO / script).read_text() and 'dirname "$0"' not in (REPO / script).read_text()


def test_a_virtualenv_on_an_old_python_is_made_again(pytmp):
    bin = pytmp / "bin"
    fake_python(bin, "python3.11", "3.11.9")
    old = fake_python(pytmp / "venv" / "bin", "python", "3.9.6")
    out = finder('make_venv venv && venv/bin/python --version', bin, [], pytmp)
    assert out.returncode == 0, out.stderr
    assert "Making venv again" in out.stdout and out.stdout.strip().endswith("Python 3.11.9")
    # and a good one is left alone
    again = finder('make_venv venv && print done', bin, [], pytmp)
    assert again.stdout.strip() == "done"


def test_scripts_use_the_finder():
    for script in ("run.sh", "run_tests.sh", "app/build.sh"):
        text = (REPO / script).read_text()
        assert "source ./find_python.sh" in text and "make_venv" in text, script
        assert "python3 -m venv" not in text, script


# ------------------------------------------------------------------ the port

@pytest.fixture
def ports(monkeypatch):
    """The module, with its own home and no PENCILPAD_PORT, and a free pair of ports."""
    pp = load_module()
    monkeypatch.delenv("PENCILPAD_PORT", raising=False)
    monkeypatch.delenv("PENCILPAD_TLS_PORT", raising=False)
    pp.HOME = TMP / f"home-port-{time.monotonic_ns()}"
    pp.pencilpad_at = lambda port: False
    while True:
        p = free_port()
        if p < 65000 and pp.port_free(p) and pp.port_free(p + 1) and pp.port_free(p + 2) and pp.port_free(p + 3):
            break
    pp.DEFAULT_PORT = p
    return pp


def saved(pp):
    f = pp.HOME / "port"
    return int(f.read_text()) if f.exists() else None


def test_port_from_the_environment_is_used_as_is(ports, monkeypatch):
    monkeypatch.setenv("PENCILPAD_PORT", "9123")
    assert ports.choose_port() == (9123, "")
    assert saved(ports) is None


def test_first_run_takes_the_default_and_saves_it(ports):
    assert ports.choose_port() == (ports.DEFAULT_PORT, "")
    assert saved(ports) == ports.DEFAULT_PORT


@pytest.mark.parametrize("taken", [0, 1], ids=["port", "its secure neighbour"])
def test_first_run_moves_off_a_taken_port_and_saves_the_new_one(ports, taken):
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("0.0.0.0", ports.DEFAULT_PORT + taken))
    blocker.listen()
    try:
        port, note = ports.choose_port()
    finally:
        blocker.close()
    assert port == ports.DEFAULT_PORT + taken + 1
    assert saved(ports) == port
    assert f"Port {ports.DEFAULT_PORT} is in use" in note and f"uses port {port}" in note
    assert ports.choose_port() == (port, "")            # and stays there


def test_a_saved_port_never_moves(ports):
    ports.HOME.mkdir(parents=True)
    (ports.HOME / "port").write_text("9345\n")
    assert ports.choose_port() == (9345, "")


def test_first_run_beside_its_own_running_copy_does_not_move(ports):
    """8765 taken by this Mac's PencilPad: say it is already running, not move and save."""
    ports.pencilpad_at = lambda port: port == ports.DEFAULT_PORT
    assert ports.choose_port() == (ports.DEFAULT_PORT, "")
    assert saved(ports) is None


def start_without_port(home, token_file):
    env = server_env(1, token_file, PENCILPAD_HOME=str(home))
    del env["PENCILPAD_PORT"]
    return env


def test_server_starts_on_its_saved_port_and_says_so_when_it_is_busy():
    port = free_port()
    home = TMP / f"home-saved-{port}"
    home.mkdir()
    (home / "port").write_text(f"{port}\n")
    tok = TMP / f"token-saved-{port}"
    tok.write_text(TOKEN)
    env = start_without_port(home, tok)
    p = subprocess.Popen([sys.executable, str(SCRIPT)], env=env, cwd=TMP,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_until(lambda: status(f"http://127.0.0.1:{port}/{TOKEN}/ping") == 200, timeout=30,
                   what="the server on its saved port")
        again = subprocess.run([sys.executable, str(SCRIPT)], env=env, cwd=TMP,
                               capture_output=True, text=True, timeout=60)
        assert again.returncode != 0 and "already running" in again.stdout + again.stderr
        assert (home / "port").read_text().strip() == str(port)       # not moved
    finally:
        p.terminate()                     # this test's own process only
        p.wait(5)


# ------------------------------------------------------------------ pairing from the setup page

@pytest.fixture
def two():
    """Mac A, whose link the iPad app has; Mac B, a new Mac with a link of its own."""
    a = Server()
    b = Server(token="b-own-token")
    yield a, b
    a.stop()
    b.stop()


def test_pair_button_pairs_at_once(two):
    a, b = two
    old = b.url

    async def pair_with_a_pad_on_b():      # a pad on B's old link while it pairs
        async with aiohttp.ClientSession() as s:
            ws = await s.ws_connect(f"ws://127.0.0.1:{b.port}/b-own-token/ws")
            await ws.receive_json(timeout=3)
            done = await asyncio.to_thread(post, old + "/pair", {"link": a.url + "/connect"})
            msg = await ws.receive(timeout=5)
            return done, msg.type
    (code, out), closed = run(pair_with_a_pad_on_b())
    assert code == 200 and out["url"] == f"/{TOKEN}/connect" and out["name"] == json.loads(a.get("/ping")[2])["name"]
    # the old link stops working and the shared one works, with no restart
    assert status(old) == 404 and status(old + "/macs") == 404
    b.url = f"{b.base}/{TOKEN}"
    assert status(b.url) == 200
    assert b.token_file.read_text() == TOKEN
    a_id, b_id = json.loads(a.get("/ping")[2])["id"], json.loads(b.get("/ping")[2])["id"]
    assert [p["id"] for p in json.loads(b.get("/macs")[2])["peers"]] == [a_id]
    assert [p["id"] for p in json.loads(a.get("/macs")[2])["peers"]] == [b_id]
    # its setup page and QR code now carry the shared link
    page = b.get("/connect", timeout=60)[2]
    assert re.search(r"<code>(.*?)</code>", page).group(1).replace("<wbr>", "").endswith(f":{b.port}/{TOKEN}")
    # and the pad that was on the old link was let go
    assert closed in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED)
    # the hello a pad now gets names A as a peer
    with urllib.request.urlopen(b.url, timeout=5) as r:
        cfg = json.loads(re.search(r"const CFG = (\{.*?\});", r.read().decode()).group(1))
    assert [p["id"] for p in cfg["peers"]] == [a_id]


@pytest.mark.parametrize("link,said", [
    ("hello", "That isn't a PencilPad link"),
    ("http://127.0.0.1:9/nobody", "Could not reach PencilPad on that Mac"),
    ("own", "That is this Mac's own link"),
    ("", "That isn't a PencilPad link"),
])
def test_pair_button_says_what_went_wrong(two, link, said):
    a, b = two
    code, out = post(b.url + "/pair", {"link": b.url if link == "own" else link})
    assert code == 400 and out["error"].startswith(said)
    assert status(b.url) == 200 and b.token_file.read_text() == "b-own-token"   # nothing changed


def test_pair_button_works_only_from_the_mac_itself(two):
    a, b = two
    lan = load_module().lan_ip()
    if lan == "127.0.0.1":
        pytest.skip("this Mac has no network address to come from")
    code, _ = post(f"http://{lan}:{b.port}/b-own-token/pair", {"link": a.url})
    assert code == 404 and b.token_file.read_text() == "b-own-token"


def test_pair_from_the_command_line_still_works(two):
    a, _ = two
    port = free_port()
    env = server_env(port, TMP / f"token-{port}")
    (TMP / f"token-{port}").write_text("c-own-token")
    done = subprocess.run([sys.executable, str(SCRIPT), "--pair", a.url], env=env, cwd=TMP,
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0 and "Paired with" in done.stdout
    assert (TMP / f"token-{port}").read_text() == TOKEN


# ------------------------------------------------------------------ the setup page

def test_setup_page_leads_with_the_code_and_says_that_is_all(server):
    page = server.get("/connect", timeout=60)[2]
    lead = page.index('class="qr"')
    assert page.index("That is all the setup there is") > lead
    assert page.index("Optional") > page.index("That is all the setup there is")
    for text in ("Make it an app on the iPad", "Install as an app", "Already use the PencilPad app with another Mac?",
                 "click Copy next to This Mac's link", "delete it after pairing", 'id="pairLink"', 'id="copy"'):
        assert text in page, text


def test_setup_page_copy_and_pair_buttons(browser, two):
    a, b = two
    ctx = browser.new_context(viewport={"width": 900, "height": 1000})
    ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=b.base)
    page = ctx.new_page()
    try:
        page.goto(b.url + "/connect")
        page.click("#copy")
        page.wait_for_function("document.getElementById('copy').textContent === 'Copied'")
        assert page.evaluate("navigator.clipboard.readText()") == page.text_content("#myLink")
        page.fill("#pairLink", "not a link")
        page.click("#pairGo")
        page.wait_for_function("document.getElementById('pairSay').classList.contains('bad')")
        assert page.text_content("#pairSay").startswith("That isn't a PencilPad link")
        page.fill("#pairLink", a.url)
        page.click("#pairGo")
        page.wait_for_url(f"**/{TOKEN}/connect", timeout=15000)
        assert TOKEN in page.text_content("#myLink")
    finally:
        ctx.close()


# ------------------------------------------------------------------ "Install as an app" on the iPad

@pytest.fixture
def tls_server():
    s = Server(extra_env={"PENCILPAD_TLS_PORT": str(free_port())})
    s.tls = s.state()["tls"]
    yield s
    s.stop()


def install_page(browser, server, trusted=False, query="", clock=False):
    ctx = browser.new_context(has_touch=True, viewport=VIEW, ignore_https_errors=False)
    page = ctx.new_page()
    if clock:
        page.clock.install()
    if trusted:            # as if the iPad trusted the certificate: the secure ping answers
        page.route(f"https://127.0.0.1:{server.tls}/**", lambda r: r.fulfill(status=200, body="{}"))
    page.goto(server.url + "/secure" + query)
    return ctx, page


def shown(page):
    return page.evaluate("['checking', 'steps', 'waiting', 'hint', 'done', 'back', 'off']"
                         ".filter(id => !document.getElementById(id).hidden)")


def test_install_page_walks_through_the_steps(browser, tls_server):
    ctx, page = install_page(browser, tls_server, clock=True)
    try:
        page.wait_for_function("!document.getElementById('steps').hidden")
        assert shown(page) == ["steps", "waiting"]
        assert page.title() == "Install PencilPad as an App"
        steps = page.evaluate("[...document.querySelectorAll('#steps li')].map(l => l.textContent)")
        assert "On this iPad, open the Settings app" in steps[1]
        assert "Still on this iPad, not on the Mac" in steps[2] and "Certificate Trust Settings" in steps[2]
        assert page.evaluate("document.querySelector('#steps li.trust') === document.querySelectorAll('#steps li')[2]")
        # back on the page after 20 seconds and still not trusted: say where the switch is
        page.clock.fast_forward("00:21")
        page.wait_for_function("!document.getElementById('hint').hidden", timeout=10000)
        assert "Installing the profile is not enough" in page.text_content("#hint")
    finally:
        ctx.close()


def test_install_page_skips_to_the_end_when_already_trusted(browser, tls_server):
    ctx, page = install_page(browser, tls_server, trusted=True)
    try:
        page.wait_for_function("!document.getElementById('done').hidden")
        assert shown(page) == ["done"]
        assert page.get_attribute("#open", "href") == f"https://127.0.0.1:{tls_server.tls}/{TOKEN}#install"
        assert "Last step: open PencilPad, then tap Share → Add to Home Screen" in page.text_content("#done")
        assert page.evaluate("document.getElementById('intro').hidden")         # done: no more pitch
    finally:
        ctx.close()


def test_install_page_opened_from_the_app_says_go_back(browser, tls_server):
    ctx, page = install_page(browser, tls_server, trusted=True, query="?from=app")
    try:
        page.wait_for_function("!document.getElementById('back').hidden")
        assert shown(page) == ["back"] and "Go back to PencilPad" in page.text_content("#back")
    finally:
        ctx.close()


def test_install_page_without_a_secure_port(browser, server):
    ctx, page = install_page(browser, server)
    try:
        page.wait_for_function("!document.getElementById('off').hidden")
        assert shown(page) == ["off"]
    finally:
        ctx.close()


# ------------------------------------------------------------------ where the pad leads there

def test_tour_ends_with_install_as_an_app_in_the_browser(browser, tls_server):
    with open_pad(browser, tls_server, tour_seen=False) as p:
        p.page.evaluate("tour.i = tour.steps.length - 1; tourShow()")
        p.wait_step("done")
        assert p.text("tourTitle") == "That’s it" and p.text("tourText") == "PencilPad works right here in the browser."
        assert p.text("tourSkip") == "Done" and p.text("tourEnd") == "Install as an app"
        assert p.has_class("tourSkip", "primary") and not p.has_class("tourEnd", "primary")
        p.press("tourEnd")
        p.page.wait_for_url(f"**/{TOKEN}/secure", timeout=10000)
        assert p.page.evaluate("localStorage.getItem('pencilpad-tour')") == "1"


def test_tour_done_just_ends(browser, tls_server):
    with open_pad(browser, tls_server, tour_seen=False) as p:
        p.page.evaluate("tour.i = tour.steps.length - 1; tourShow()")
        p.wait_step("done")
        p.press("tourSkip")
        p.wait_step(None)
        assert p.page.url == tls_server.url


def secure_pad(browser, server, hash="", init=()):
    ctx = browser.new_context(has_touch=True, viewport=VIEW, ignore_https_errors=True)
    for i in init:
        ctx.add_init_script(i)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"https://127.0.0.1:{server.tls}/{TOKEN}{hash}")
    page.wait_for_function("!!cur && !document.body.classList.contains('offline')", timeout=10000)
    return ctx, page, errors


def test_secure_tour_ends_with_add_to_home_screen(browser, tls_server):
    ctx, page, errors = secure_pad(browser, tls_server)
    try:
        page.wait_for_function("document.body.classList.contains('touring')")
        assert page.evaluate("tour.steps[tour.steps.length - 1].s") == "home"
        assert errors == []
    finally:
        ctx.close()


def test_coming_back_from_installing_asks_once_to_add_to_home_screen(browser, tls_server):
    ctx, page, errors = secure_pad(browser, tls_server, "#install", init=[TOUR_SEEN])
    try:
        page.wait_for_function("document.body.classList.contains('touring')")
        assert page.evaluate("[tour.steps.length, tour.steps[0].s]") == [1, "home"]
        assert page.text_content("#tourStep") == "" and page.evaluate("document.getElementById('tourEnd').hidden")
        assert "delete it first" in page.text_content("#tourText")
        assert page.evaluate("location.hash") == ""
        page.evaluate("tourEnd(); reconnect()")
        page.wait_for_function("!!cur && !document.body.classList.contains('offline')")
        time.sleep(0.3)
        assert not page.evaluate("document.body.classList.contains('touring')")       # once
        assert errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("where,standalone,want", [
    ("plain", False, "Install as an app"),
    ("plain", True, "Set up the secure connection"),
    ("secure", False, "Install as an app"),
    ("secure", True, None),
])
def test_settings_install_row(browser, tls_server, where, standalone, want):
    init = [TOUR_SEEN] + ([STANDALONE] if standalone else [])
    if where == "plain":
        with open_pad(browser, tls_server, init=init[1:]) as p:
            got = p.page.evaluate("sheetOpen(); const r = document.getElementById('install'); r.hidden ? null : r.textContent")
    else:
        ctx, page, _ = secure_pad(browser, tls_server, init=init)
        try:
            got = page.evaluate("sheetOpen(); const r = document.getElementById('install'); r.hidden ? null : r.textContent")
        finally:
            ctx.close()
    assert got == want


def test_settings_install_row_hidden_without_a_secure_port(pad):
    assert pad.page.evaluate("sheetOpen(); document.getElementById('install').hidden")


def test_settings_install_row_opens_the_install_page(browser, tls_server):
    with open_pad(browser, tls_server) as p:
        p.page.evaluate("sheetOpen()")
        p.page.evaluate("document.getElementById('install').scrollIntoView({block: 'center'})")
        time.sleep(0.1)
        p.tap(*p.center("install"))
        p.page.wait_for_url(f"**/{TOKEN}/secure", timeout=10000)


def test_mac_list_offers_to_set_up_an_untrusted_macs_certificate(browser, tls_server):
    """Over HTTPS, a paired Mac with a secure port that cannot be reached: its install page."""
    other = {"id": "0123456789ab", "name": "Other Mac", "hosts": ["127.0.0.1"], "port": free_port(), "tls": free_port()}
    seed = "localStorage.setItem('pencilpad-macs', JSON.stringify([%s]))" % json.dumps(other)
    ctx, page, errors = secure_pad(browser, tls_server, init=[TOUR_SEEN, seed])
    try:
        went = []
        page.route(f"http://127.0.0.1:{other['port']}/**", lambda r: (went.append(r.request.url),
                   r.fulfill(status=200, content_type="text/html", body="<title>install</title>")))
        page.evaluate("pickOpen()")
        page.wait_for_function("!!document.querySelector('#macList .fix')", timeout=10000)
        x, y = page.evaluate("(() => { const r = document.querySelector('#macList .fix').getBoundingClientRect();"
                             " return [r.x + r.width / 2, r.y + r.height / 2]; })()")
        cdp = ctx.new_cdp_session(page)
        for typ in ("touchStart", "touchEnd"):
            cdp.send("Input.dispatchTouchEvent", {"type": typ, "touchPoints": [{"x": x, "y": y, "id": 1}] if typ == "touchStart" else []})
        page.wait_for_function("document.title === 'install'", timeout=10000)
        assert went == [f"http://127.0.0.1:{other['port']}/{TOKEN}/secure?from=app"]
    finally:
        ctx.close()


def test_mac_list_has_no_certificate_action_on_the_plain_connection(browser, tls_server):
    other = {"id": "0123456789ab", "name": "Other Mac", "hosts": ["127.0.0.1"], "port": free_port(), "tls": free_port()}
    seed = "localStorage.setItem('pencilpad-macs', JSON.stringify([%s]))" % json.dumps(other)
    with open_pad(browser, tls_server, init=[seed]) as p:
        p.page.evaluate("pickOpen()")
        p.page.wait_for_function("[...document.querySelectorAll('#macList .mac span')].some(s => s.textContent === 'Not reachable')",
                                 timeout=10000)
        assert p.page.evaluate("document.querySelector('#macList .fix')") is None


# ------------------------------------------------------------------ the second design review

def test_setup_page_asks_for_accessibility_first(browser, server):
    ctx = browser.new_context(viewport={"width": 900, "height": 1000})
    page = ctx.new_page()
    def state(route):
        body = json.loads(route.fetch().text())
        body.update(access=False, screen=False)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    page.route("**/state", state)
    try:
        page.goto(server.url + "/connect")
        page.wait_for_function("document.getElementById('leadLine').textContent.startsWith('One more step')")
        assert page.text_content("#leadLine") == "One more step on this Mac: allow Accessibility below, or the pointer won't move."
        cls = page.evaluate("['dAcc', 'dScr'].map(i => document.getElementById(i).className)")
        assert cls == ["dot", "dot opt"]                   # orange only for Accessibility
        assert page.text_content("#tScr") == "Screen Recording is off"
        page.unroute("**/state")
        page.wait_for_function("document.getElementById('leadLine').textContent.startsWith('Tap the link')", timeout=8000)
    finally:
        ctx.close()


def test_setup_page_link_breaks_only_where_it_should(server):
    page = server.get("/connect", timeout=60)[2]
    code = re.search(r"<code>(.*?)</code>", page).group(1)
    assert code.count("<wbr>") == 3 and code.startswith("http://<wbr>") and code.endswith(f"<wbr>/{TOKEN}")
    assert 'placeholder="Paste the other Mac\'s link"' in page


def test_install_page_off_state_leads_back(browser, server):
    ctx, page = install_page(browser, server)
    try:
        page.wait_for_function("!document.getElementById('off').hidden")
        assert page.get_attribute("#plain", "href") == f"/{TOKEN}"
        assert page.evaluate("[document.getElementById('intro').hidden, document.getElementById('foot').hidden]") == [True, True]
        assert "start PencilPad again with ./run.sh, then reload this page" in page.text_content("#off")
    finally:
        ctx.close()


def test_install_page_steps_offer_not_now(browser, tls_server):
    ctx, page = install_page(browser, tls_server)
    try:
        page.wait_for_function("!document.getElementById('steps').hidden")
        assert not page.evaluate("document.getElementById('notnow').hidden")
        assert page.get_attribute("#notnow", "href") == f"/{TOKEN}"
        assert page.text_content("#intro").startswith("Optional, about two minutes. PencilPad already works in Safari.")
    finally:
        ctx.close()
