"""Mixed versions, for real: the PencilPad that was released before the rail (git commit
ae5cd6a), run as its own dry-run server next to this one. One Home Screen app can meet
either: an older Mac, or its own cached older page on an updated Mac.

Skipped when that commit is not available (not a git checkout)."""

import json
import re
import subprocess
import time
import urllib.request

import pytest

import conftest
from conftest import HERE, TMP, Server, evs, open_pad

OLD_COMMIT = "ae5cd6ac22fea95109dfb5d271fe3fc973736599"
OLD = TMP / "pencilpad-ae5cd6a.py"


def old_script():
    if not OLD.exists():
        try:
            src = subprocess.run(["git", "show", f"{OLD_COMMIT}:pencilpad.py"], cwd=HERE.parent,
                                 capture_output=True, text=True, timeout=20, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            pytest.skip("the released version is not available here")
        OLD.write_text(src)
    return OLD


@pytest.fixture
def old_server():
    script = old_script()
    saved, conftest.SCRIPT = conftest.SCRIPT, script
    try:
        s = Server()
    finally:
        conftest.SCRIPT = saved
    yield s
    s.stop()


def page_with(page_from, cfg_from):
    """One version's page, with the other server's settings in it."""
    cfg = re.search(r"const CFG = (\{.*?\});\n", urllib.request.urlopen(cfg_from.url).read().decode()).group(1)
    html = urllib.request.urlopen(page_from.url).read().decode()
    return re.sub(r"const CFG = \{.*?\};\n", lambda _: "const CFG = " + cfg + ";\n", html, count=1)


def test_new_page_on_an_older_mac(browser, old_server, server):
    html = page_with(server, old_server)
    route = lambda page: page.route(old_server.url, lambda r: r.fulfill(body=html, content_type="text/html"))
    with open_pad(browser, old_server, route=route) as p:
        p.page.wait_for_function("cur && Array.isArray(cur.caps)")
        assert p.page.evaluate("cur.caps") == [] and p.page.evaluate("cur.settings") is None
        m = old_server.mark()
        for k in ("mc", "app", "copy", "space", "kbd"):
            p.press(k)
            time.sleep(0.15)
            assert "needs PencilPad updated on" in p.text("statusText")
        for k in ("esc", "sub", "shot"):
            p.press(k)
            time.sleep(0.3)
        p.down(*p.center("dict"))
        time.sleep(0.2)
        p.up()
        time.sleep(0.4)
        p.tap(700, 410)
        time.sleep(0.4)
        e = old_server.log(m)
        assert [x["key"] for x in e if x["ev"] == "keydown"] == ["esc", "return", "v", "f13"]
        assert "ldown" in evs(e) and "bad message" not in old_server.output()


def test_older_page_on_an_updated_mac(browser, old_server, server):
    """The Home Screen app's cached copy of the old page, talking to this version."""
    html = page_with(old_server, server)
    route = lambda page: page.route(server.url, lambda r: r.fulfill(body=html, content_type="text/html"))
    with open_pad(browser, server, route=route) as p:
        assert p.page.evaluate("typeof railKey") == "undefined"          # really the old page
        m = server.mark()
        w, h = conftest.VIEW["width"], conftest.VIEW["height"]
        corners = {"esc": (60, 60), "shot": (w - 60, 60), "dict": (60, h - 60), "sub": (w - 60, h - 60)}
        for k in ("esc", "sub", "shot"):
            p.tap(*corners[k])
            time.sleep(0.3)
        p.down(*corners["dict"])
        time.sleep(0.2)
        p.up()
        time.sleep(0.4)
        p.tap(590, 410)
        time.sleep(0.7)
        p.down(550, 410, 1)
        p.down(630, 410, 2)
        p.drag(2, 630, 410, 630, 300)
        p.up(2)
        p.up(1)
        time.sleep(0.4)
        e = server.log(m)
        assert [x["key"] for x in e if x["ev"] == "keydown"] == ["esc", "return", "v", "f13"]
        assert "ldown" in evs(e) and "scroll" in evs(e) and "bad message" not in server.output()


def test_the_two_versions_can_pair(old_server, server):
    for target, other in ((old_server, server), (server, old_server)):
        me = json.loads(urllib.request.urlopen(other.url + "/ping").read())
        req = urllib.request.Request(target.url + "/peer", data=json.dumps(me).encode(),
                                     headers={"Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req, timeout=5).read())["id"]
