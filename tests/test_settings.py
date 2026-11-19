"""The Settings sheet, and the layout as it changes: the real served page in Chromium,
driven with real touch and Pencil input, against dry-run servers."""

import json
import time

import pytest

from conftest import CENTER, TMP, VIEW, evs, open_pad, wait_until

POINTER = ("move", "drag", "ldown", "lup", "rdown", "rup", "scroll")
CX, CY = CENTER


def settle(pad, mark, wait=0.35):
    time.sleep(wait)
    return pad.server.log(mark)


def rect(pad, sel):
    return pad.page.evaluate(
        "s => { const r = document.querySelector(s).getBoundingClientRect();"
        " return {x: r.x, y: r.y, w: r.width, h: r.height, r: r.right, b: r.bottom}; }", sel)


def sheet_open(pad):
    pad.press("gear")
    pad.page.wait_for_function("getComputedStyle(document.getElementById('sheet')).opacity === '1'")


def tap_in_sheet(pad, sel, pen=False):
    """Scroll `sel` into the sheet's view, then touch it (or tap it with the Pencil)."""
    pad.page.evaluate("s => document.querySelector(s).scrollIntoView({block: 'center'})", sel)
    time.sleep(0.1)
    x, y = pad.page.evaluate("s => { const r = document.querySelector(s).getBoundingClientRect();"
                             " return [r.x + r.width / 2, r.y + r.height / 2]; }", sel)
    if pen:
        pad.pen_down(x, y)
        pad.pen_up(x, y)
    else:
        pad.tap(x, y)
    time.sleep(0.15)


def settings_file(server):
    f = TMP / f"home-{server.port}" / "settings.json"
    return json.loads(f.read_text()) if f.exists() else None


def mac_setting(pad, k):
    return pad.page.evaluate("k => macSet(k)", k)


# ------------------------------------------------------------------ opening and closing

def test_gear_opens_the_sheet_and_it_blocks_the_pad(pad):
    sheet_open(pad)
    m = pad.server.mark()
    pad.tap(CX + 400, CY + 300)                         # outside the sheet: only closes it
    pad.page.wait_for_function("!document.body.classList.contains('setting')")
    assert evs(settle(pad, m), *POINTER) == []
    sheet_open(pad)
    tap_in_sheet(pad, "#done")
    pad.page.wait_for_function("!document.body.classList.contains('setting')")
    m = pad.server.mark()
    pad.tap()
    assert evs(settle(pad, m), *POINTER) == ["ldown", "lup"]


def test_opening_the_sheet_lets_go_of_a_held_key(pad):
    s = pad.server
    m = s.mark()
    pad.down(*pad.center("dict"), id=1)
    s.wait_for(lambda e: "keydown" in evs(e), since=m)
    pad.press("gear", id=2)
    s.wait_for(lambda e: "keyup" in evs(e), since=m)
    pad.up(1)


def test_sheet_sections_in_order(pad):
    sheet_open(pad)
    heads = pad.page.evaluate("[...document.querySelectorAll('#sheet h2')].map(h => h.textContent)")
    assert heads == ["Pointer", "Dictate", "Screenshot", "Keys", "Help"]


def test_sheet_targets_are_big_enough(browser, server):
    for view in (VIEW, {"width": 820, "height": 1180}):
        with open_pad(browser, server, view=view) as p:
            sheet_open(p)
            small = p.page.evaluate("""[...document.querySelectorAll(
                '#sheet .set:not(.col), #sheet .seg button, #sheet .chips button, #sheet .slider, #done')]
                .map(e => { const r = e.getBoundingClientRect(); return [e.textContent.trim().slice(0, 20), r.width, r.height]; })
                .filter(([, w, h]) => (w || h) && (w < 44 || h < 44))""")      # (hidden rows are 0 by 0)
            assert small == [], small
            spill = p.page.evaluate("""[...document.querySelectorAll('#sheet .chips button, #sheet .seg button')]
                .filter(b => b.scrollWidth > b.clientWidth + 1).map(b => b.textContent)""")
            assert spill == [], spill
            sh, surface = rect(p, "#sheet"), rect(p, "#surface")
            assert surface["x"] < sh["x"] and sh["r"] < surface["r"] and sh["b"] <= surface["b"]


def test_sheet_scrolls(pad):
    sheet_open(pad)
    body = "document.getElementById('sheetBody')"
    assert pad.page.evaluate(body + ".scrollHeight > " + body + ".clientHeight")
    y = rect(pad, "#sheetBody")["y"] + 300
    pad.down(CX, y)
    pad.drag(1, CX, y, CX, y - 250, steps=12)
    pad.up()
    wait_until(lambda: pad.page.evaluate(body + ".scrollTop") > 50, what="the sheet to scroll")


# ------------------------------------------------------------------ the Mac's settings

def test_a_mac_setting_is_saved_on_the_mac(pad):
    sheet_open(pad)
    tap_in_sheet(pad, '[data-set=dictation_source] [data-v=mac]')
    tap_in_sheet(pad, '[data-set=dictation_key] [data-v=fn]')
    tap_in_sheet(pad, '[data-set=dictation_mode] [data-v=toggle]', pen=True)     # the Pencil works too
    tap_in_sheet(pad, '[data-set=screenshot_paste]')
    wait_until(lambda: settings_file(pad.server) == {"dictation_source": "mac", "dictation_key": "fn",
                                                     "dictation_mode": "toggle", "screenshot_paste": False},
               what="the settings file")
    assert pad.page.evaluate("document.querySelector('[data-set=dictation_key] [data-v=fn]').classList.contains('cur')")
    assert pad.text("dictSub") == "Tap to start or stop"
    # and Dictate now taps fn
    tap_in_sheet(pad, "#done")
    m = pad.server.mark()
    pad.tap(*pad.center("dict"))
    e = pad.server.wait_for(lambda e: "flags" in evs(e) and evs(e).count("flags") == 2, since=m)
    assert [(x["ev"], x["key"], x["mods"]) for x in e if x["ev"] == "flags"] == [("flags", "fn", ["fn"]), ("flags", "fn", [])]
    assert pad.has_class("dict", "on")                 # on until the next tap


def test_slider_sets_tracking_speed_on_lift(pad):
    sheet_open(pad)
    pad.page.evaluate("document.querySelector('.slider[data-set=pointer_speed]').scrollIntoView({block: 'center'})")
    r = rect(pad, ".slider[data-set=pointer_speed]")
    pad.down(r["x"] + r["w"] * 0.5, r["y"] + r["h"] / 2)
    pad.drag(1, r["x"] + r["w"] * 0.5, r["y"] + r["h"] / 2, r["r"] - 2, r["y"] + r["h"] / 2, steps=6)
    assert settings_file(pad.server) is None                     # not while the finger is down
    pad.up()
    wait_until(lambda: (settings_file(pad.server) or {}).get("pointer_speed", 0) >= 3.9, what="the new speed")
    assert pad.text("sheetBody").count("4.0×") == 1


def test_two_pads_stay_in_step(browser, server):
    with open_pad(browser, server) as a, open_pad(browser, server) as b:
        sheet_open(b)
        a.page.evaluate("setMac('hover', true)")
        b.page.wait_for_function("macSet('hover') === true")
        b.page.wait_for_function("document.querySelector('[data-set=hover]').classList.contains('on')")


# ------------------------------------------------------------------ this iPad's settings

def test_hidden_keys_give_their_room_to_the_others(pad):
    h0 = rect(pad, "#esc")["h"]
    sheet_open(pad)
    for k in ("copy", "paste", "mc", "app"):
        tap_in_sheet(pad, f"[data-local=key-{k}]")
    assert json.loads(pad.page.evaluate("localStorage.getItem('pencilpad-hidden')")) == ["copy", "paste", "mc", "app"]
    tap_in_sheet(pad, "#done")
    time.sleep(0.2)
    shown = pad.page.evaluate("[...document.querySelectorAll('#rail .key')].filter(k => k.offsetHeight).map(k => k.id)")
    assert shown == ["esc", "space", "sub", "dict", "shot", "kbd"]
    assert rect(pad, "#esc")["h"] > h0 * 1.4
    pad.page.reload()
    pad.wait_online()
    assert pad.page.evaluate("[...document.querySelectorAll('#rail .key')].filter(k => k.offsetHeight).length") == 6


def test_the_last_key_cannot_be_hidden(pad):
    pad.page.evaluate("localStorage.setItem('pencilpad-hidden', JSON.stringify(%s)); applyLocal()" % json.dumps(
        ["esc", "space", "sub", "copy", "paste", "dict", "shot", "mc", "app"]))
    sheet_open(pad)
    tap_in_sheet(pad, "[data-local=key-kbd]")
    assert pad.page.evaluate("document.getElementById('kbd').offsetHeight") > 400


def test_rail_on_the_right(pad):
    sheet_open(pad)
    tap_in_sheet(pad, "[data-local=side] [data-v=right]")
    tap_in_sheet(pad, "#done")
    time.sleep(0.4)
    rail, surface = rect(pad, "#rail"), rect(pad, "#surface")
    assert rail["r"] == VIEW["width"] and surface["r"] < rail["x"] and surface["x"] < 20
    top = rect(pad, "#top")
    assert abs((top["x"] + top["r"]) / 2 - (surface["x"] + surface["r"]) / 2) < 2
    assert pad.page.evaluate("document.elementFromPoint(%d, 400).id" % (rail["x"] - 4)) == "pad"
    m = pad.server.mark()
    pad.press("esc")
    assert [x["key"] for x in settle(pad, m) if x["ev"] == "keydown"] == ["esc"]
    # folded, the strip runs to the right-hand edge of the screen
    pad.press("railToggle")
    pad.page.wait_for_function("document.getElementById('rail').getBoundingClientRect().width === 44")
    assert rect(pad, "#rail")["w"] == 44 and rect(pad, "#surface")["r"] > VIEW["width"] - 60
    pad.tap(VIEW["width"] - 2, 400)
    pad.page.wait_for_function("!document.body.classList.contains('collapsed')")
    # the keyboard sits beside the rail too
    pad.press("kbd")
    pad.page.wait_for_function("document.body.classList.contains('typing')")
    assert rect(pad, "#keyboard")["r"] < rect(pad, "#rail")["x"]


def test_replay_the_tour(pad):
    sheet_open(pad)
    tap_in_sheet(pad, "#replay")
    pad.wait_step("move")
    assert not pad.page.evaluate("document.body.classList.contains('setting')")


# ------------------------------------------------------------------ the second design review

def test_rail_groups_have_a_little_more_room_between_them(pad):
    top = lambda: pad.page.evaluate("[...document.querySelectorAll('#rail .key.gtop')].map(k => k.id)")
    assert top() == ["copy", "dict", "mc"]
    gap = lambda a, b: rect(pad, f"#{b}")["y"] - rect(pad, f"#{a}")["b"]
    assert gap("sub", "copy") - gap("esc", "space") > 6           # 14 px between groups, 6 within
    pad.page.evaluate("localStorage.setItem('pencilpad-hidden', JSON.stringify(['copy', 'paste'])); applyLocal()")
    assert top() == ["dict", "mc"]                                 # no room above a hidden group
    pad.page.evaluate("localStorage.setItem('pencilpad-hidden', JSON.stringify(['esc', 'space', 'sub'])); applyLocal()")
    assert top() == ["dict", "mc"]                                 # the first group shown has none


def test_dictate_section_says_who_listens(pad):
    sheet_open(pad)
    text = pad.text("sheetBody")
    for words in ("Who listens", "The Mac’s dictation shortcut", "Dictate key", "Tap to start and stop",
                  "macOS Dictation, Handy and similar apps work this way",
                  "Pick the shortcut your dictation app on the Mac listens for."):
        assert words in text, words
    assert [b.strip() for b in pad.page.evaluate(
        "[...document.querySelectorAll('[data-set=dictation_source] button')].map(b => b.textContent)")] == ["The Mac", "This iPad"]
    also = "document.getElementById('alsoKey').hidden"
    note = "document.getElementById('micNote').textContent"
    assert pad.page.evaluate(also) is False                       # "This iPad" is the default
    assert pad.page.evaluate(note).startswith("This iPad can listen once PencilPad is installed as an app (Help → Install as an app)")
    tap_in_sheet(pad, "[data-set=dictation_source] [data-v=mac]")
    assert pad.page.evaluate(also) is True and pad.page.evaluate(note) == ""


def test_rail_side_comes_first_among_the_keys(pad):
    sheet_open(pad)
    first = pad.page.evaluate("""(() => { const h = [...document.querySelectorAll('#sheet h2')].find(h => h.textContent === 'Keys');
        return h.nextElementSibling.firstElementChild.querySelector('.name').textContent; })()""")
    assert first == "Rail side"


@pytest.mark.parametrize("scheme,want", [("dark", "rgb(99, 99, 102)"), ("light", "rgb(255, 255, 255)")])
def test_selected_segment_stands_out(browser, server, scheme, want):
    ctx = browser.new_context(has_touch=True, viewport=VIEW, color_scheme=scheme)
    ctx.add_init_script("try { localStorage.setItem('pencilpad-tour', '1'); } catch (_) {}")
    page = ctx.new_page()
    try:
        page.goto(server.url)
        page.wait_for_function("!!cur")
        page.evaluate("sheetOpen()")
        got = page.evaluate("getComputedStyle(document.querySelector('#sheet .seg button.cur')).backgroundColor")
        scrim = page.evaluate("getComputedStyle(document.getElementById('scrim')).backgroundColor")
        assert got == want and scrim == ("rgba(0, 0, 0, 0.35)" if scheme == "dark" else "rgba(0, 0, 0, 0.18)")
    finally:
        ctx.close()


def test_slider_knob_grows_while_dragged(pad):
    sheet_open(pad)
    pad.page.evaluate("document.querySelector('.slider[data-set=scroll_speed]').scrollIntoView({block: 'center'})")
    r = rect(pad, ".slider[data-set=scroll_speed]")
    pad.down(r["x"] + r["w"] / 2, r["y"] + r["h"] / 2)
    assert pad.page.evaluate("document.querySelector('.slider[data-set=scroll_speed]').classList.contains('dragging')")
    pad.up()
    assert not pad.page.evaluate("document.querySelector('.slider[data-set=scroll_speed]').classList.contains('dragging')")


def test_older_mac_keys_keep_their_note_readable(browser, server):
    from test_keys import as_older_mac
    with open_pad(browser, server, route=as_older_mac) as p:
        p.page.wait_for_function("cur && Array.isArray(cur.caps)")
        style = p.page.evaluate("""(() => { const k = document.getElementById('mc');
            return [getComputedStyle(k).opacity, getComputedStyle(k.querySelector('span')).opacity]; })()""")
        assert style == ["1", "0.45"]


def test_mac_list_offers_install_as_an_app(browser, server):
    from conftest import Server as S
    s = S(extra_env={"PENCILPAD_TLS_PORT": str(__import__("conftest").free_port())})
    try:
        with open_pad(browser, s) as p:
            p.press("status")
            p.page.wait_for_function("document.body.classList.contains('picking')")
            assert p.text("secure") == "Install as an app…"
    finally:
        s.stop()
