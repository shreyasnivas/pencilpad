"""The rail and the keyboard: the real served page in Chromium, driven with real
touch and Pencil input via CDP, asserting on what the dry-run server would have posted."""

import json
import re
import time

import pytest

from conftest import CENTER, VIEW, evs, open_pad
from test_server import load_module

KEYS = ("keydown", "keyup", "flags")
POINTER = ("move", "drag", "ldown", "lup", "rdown", "rup", "scroll")
CX, CY = CENTER


def settle(pad, mark, wait=0.35):
    time.sleep(wait)
    return pad.server.log(mark)


def keys(e):
    """(event, key, mods) for every key event, with a + for an auto-repeat."""
    return [(x["ev"] + ("+" if x.get("repeat") else ""), x["key"], x["mods"])
            for x in e if x["ev"] in KEYS]


def rect(pad, sel):
    return pad.page.evaluate(
        "s => { const r = document.querySelector(s).getBoundingClientRect();"
        " return {x: r.x, y: r.y, w: r.width, h: r.height, r: r.right, b: r.bottom}; }", sel)


def typing_on(pad):
    pad.press("kbd")
    pad.page.wait_for_function("document.body.classList.contains('typing')")


def tap_cap(pad, k, id=1, n=0, hold=0.05):
    pad.tap(*pad.cap(k, n), id=id, hold=hold)


def legend(pad, k):
    return pad.page.evaluate(
        "k => document.querySelector('#keyboard .cap[data-k=\"' + k + '\"]').textContent", k)


def mod_class(pad, k):
    return pad.page.evaluate("""k => { const c = document.querySelector('#keyboard .cap[data-k="' + k + '"]').classList;
        return c.contains('locked') ? 'locked' : c.contains('armed') ? 'armed' : ''; }""", k)


# ------------------------------------------------------------------ the rail

RAIL = ["esc", "space", "sub", "copy", "paste", "dict", "shot", "mc", "app", "kbd"]


def rail_keys(pad):
    return pad.page.evaluate("""[...document.querySelectorAll('#rail .key')].map(k => {
        const r = k.getBoundingClientRect(), s = k.querySelector('span');
        return {id: k.id, label: s.textContent, h: r.height, w: r.width,
                clipped: [...k.children].some(c => getComputedStyle(c).display !== 'none' && (
                    c.getBoundingClientRect().top < r.top - 0.5 || c.getBoundingClientRect().bottom > r.bottom + 0.5
                    || c.scrollWidth > c.clientWidth + 1))}; })""")


def test_rail_has_ten_keys_of_equal_height_in_order(pad):
    got = rail_keys(pad)
    assert [(k["id"], k["label"]) for k in got] == [
        ("esc", "Escape"), ("space", "Space"), ("sub", "Return"), ("copy", "Copy"), ("paste", "Paste"),
        ("dict", "Dictate"), ("shot", "Screenshot"), ("mc", "Windows"), ("app", "Switch"), ("kbd", "Keyboard")]
    heights = [k["h"] for k in got]
    assert max(heights) - min(heights) < 1 and min(heights) >= 44


@pytest.mark.parametrize("view", [{"width": 1024, "height": 768}, {"width": 1133, "height": 744},
                                  {"width": 744, "height": 1133}, VIEW], ids=lambda v: f"{v['width']}x{v['height']}")
def test_rail_keys_fit_without_clipping(browser, server, view):
    with open_pad(browser, server, view=view) as p:
        p.page.evaluate("dictLabel('Allow Speech on the Mac')")        # the longest note
        for k in rail_keys(p):
            assert k["h"] >= 44 and k["w"] >= 44 and not k["clipped"], k
        assert p.page.evaluate("getComputedStyle(document.querySelector('#dict svg')).display") == "none"


def test_windows_key_toggles_mission_control(pad):
    m = pad.server.mark()
    pad.press("mc")
    assert pad.has_class("mc", "on")                     # the press shows
    e = settle(pad, m, 0.6)
    assert evs(e, "missioncontrol", *KEYS, *POINTER) == ["missioncontrol"]


def test_switch_key_sends_one_command_tab(pad):
    m = pad.server.mark()
    pad.press("app")
    e = settle(pad, m)
    assert keys(e) == [("flags", "command", ["command"]), ("keydown", "tab", ["command"]),
                       ("keyup", "tab", ["command"]), ("flags", "command", [])]
    assert evs(e, *POINTER) == []


@pytest.mark.parametrize("view", [VIEW, {"width": 820, "height": 1180}], ids=["landscape", "portrait"])
@pytest.mark.parametrize("folded", [False, True], ids=["open", "folded"])
def test_everything_sits_over_the_trackpad(browser, server, view, folded):
    """The rail on the left, the trackpad to its right, and the top bar, the Mac list,
    the "Looking for your Mac" card and the tour card over the trackpad, clear of the rail."""
    init = ["try { localStorage.setItem('pencilpad-rail', 'collapsed'); } catch (_) {}"] if folded else []
    with open_pad(browser, server, init=init, view=view) as p:
        time.sleep(0.3)
        rail, surface = rect(p, "#rail"), rect(p, "#surface")
        assert surface["x"] > rail["r"] and surface["r"] <= view["width"]
        assert (rail["r"] == 44) == folded
        assert p.page.evaluate("document.elementFromPoint(%d, %d).id" % (rail["r"] + 4, view["height"] / 2)) == "pad"
        p.press("status")
        p.page.wait_for_function("getComputedStyle(document.getElementById('macs')).opacity === '1'")
        p.press("help")
        p.wait_step("move")
        p.page.evaluate("setOnline(false)")
        p.page.wait_for_function("getComputedStyle(document.getElementById('lost')).opacity === '1'")
        for sel in ("#top", "#macs", "#lost", "#tour"):
            r = rect(p, sel)
            assert surface["x"] < r["x"] and r["r"] < surface["r"], (sel, r, surface)
            assert abs((r["x"] + r["r"]) / 2 - (surface["x"] + surface["r"]) / 2) < 2, sel   # centred on it
        p.page.evaluate("setOnline(true)")


def visible_keys(pad):
    return pad.page.evaluate("getComputedStyle(document.querySelector('#rail .keys')).visibility") == "visible"


def test_rail_folds_and_the_trackpad_takes_its_width(pad):
    open_left = rect(pad, "#surface")["x"]
    ex, ey = pad.center("esc")
    handle = rect(pad, "#railToggle")
    assert handle["h"] == 44 and handle["b"] > rect(pad, "#kbd")["b"]       # below the last key
    pad.press("railToggle")
    pad.page.wait_for_function("document.getElementById('rail').getBoundingClientRect().width === 44")
    assert pad.page.evaluate("document.body.classList.contains('collapsed')")
    assert pad.page.evaluate("localStorage.getItem('pencilpad-rail')") == "collapsed"
    assert rect(pad, "#rail")["w"] == 44 and rect(pad, "#surface")["x"] < open_left - 60
    pad.page.wait_for_function("getComputedStyle(document.querySelector('#rail .keys')).visibility === 'hidden'")
    # no rail key can be reached: where Escape was is trackpad now
    m = pad.server.mark()
    pad.tap(ex, ey)
    e = settle(pad, m)
    assert evs(e, *KEYS) == [] and evs(e, *POINTER) == ["ldown", "lup"]
    # the folded state survives a reload
    pad.page.reload()
    pad.wait_online()
    assert pad.page.evaluate("document.body.classList.contains('collapsed')")
    assert rect(pad, "#surface")["x"] < 60
    # and a tap anywhere along the strip, right from the screen's edge, brings the rail back
    pad.tap(2, VIEW["height"] / 3)
    pad.page.wait_for_function("document.getElementById('surface').getBoundingClientRect().x > 120")
    assert pad.page.evaluate("localStorage.getItem('pencilpad-rail')") == "open"
    pad.page.wait_for_function("getComputedStyle(document.querySelector('#rail .keys')).visibility === 'visible'")
    m = pad.server.mark()
    pad.press("esc")
    assert keys(settle(pad, m)) == [("keydown", "esc", []), ("keyup", "esc", [])]


def test_handle_folds_on_lift_and_only_if_the_touch_ends_on_it(pad):
    x, y = pad.center("railToggle")
    pad.down(x, y)
    time.sleep(0.2)
    assert not pad.page.evaluate("document.body.classList.contains('collapsed')")     # not on touch-down
    pad.move(x + 200, y - 200)                     # slides off: no fold
    pad.up()
    time.sleep(0.3)
    assert not pad.page.evaluate("document.body.classList.contains('collapsed')")
    pad.tap(x, y)
    pad.page.wait_for_function("document.body.classList.contains('collapsed')")
    # folded: a touch that starts on the strip but ends on the pad does not unfold it, nor click
    m = pad.server.mark()
    pad.down(20, 400)
    pad.drag(1, 20, 400, 400, 400)
    pad.up()
    time.sleep(0.3)
    assert pad.page.evaluate("document.body.classList.contains('collapsed')")
    assert evs(pad.server.log(m), *POINTER) == []
    # the strip's touch area is 44px wide; the trackpad starts there
    assert pad.page.evaluate("document.elementFromPoint(43, 400).id") == "rail"
    assert pad.page.evaluate("document.elementFromPoint(45, 400).id") == "pad"


def test_rail_starts_open(pad):
    assert not pad.page.evaluate("document.body.classList.contains('collapsed')")
    assert rect(pad, "#rail")["w"] > 100


def test_folding_the_rail_lets_go_of_a_held_key(pad):
    s = pad.server
    m = s.mark()
    pad.down(*pad.center("space"), id=1)
    s.wait_for(lambda e: "keydown" in evs(e), since=m)
    pad.press("railToggle", id=2)
    s.wait_for(lambda e: "keyup" in evs(e), since=m)
    pad.up(1)
    e = keys(settle(pad, m))
    assert e[0] == ("keydown", "space", []) and e[-1] == ("keyup", "space", []) and len([x for x in e if x[0] == "keyup"]) == 1


def test_folding_the_rail_lets_go_of_a_held_dictate(pad):
    s = pad.server
    m = s.mark()
    pad.down(*pad.center("dict"), id=1)
    s.wait_for(lambda e: "keydown" in evs(e), since=m)
    pad.press("railToggle", id=2)
    s.wait_for(lambda e: "keyup" in evs(e), since=m)
    pad.up(1)
    assert keys(settle(pad, m)) == [("keydown", "f13", ["fn"]), ("keyup", "f13", ["fn"])]


# ------------------------------------------------------------------ the keyboard

def test_keyboard_shares_the_trackpad(pad):
    """The keyboard sits at the bottom; above it the trackpad goes on working."""
    typing_on(pad)
    assert pad.has_class("kbd", "lit")
    k, surface, p = rect(pad, "#keyboard"), rect(pad, "#surface"), rect(pad, "#pad")
    assert k["x"] == surface["x"] and k["r"] == surface["r"]
    assert abs(surface["b"] - k["y"]) < 1 and abs(p["b"] - k["y"]) < 1          # the pad ends where it begins
    assert pad.page.evaluate("getComputedStyle(document.getElementById('hint')).visibility") == "hidden"
    band = (k["y"] + rect(pad, "#top")["b"]) / 2
    assert k["y"] - rect(pad, "#top")["b"] > 100
    # no pad gestures from touches on the keyboard (a blank part of it, between rows)
    m = pad.server.mark()
    gap_y = rect(pad, "#keyboard .row")["b"] + 3
    pad.down(CX, gap_y)
    pad.drag(1, CX, gap_y, CX + 150, gap_y)
    pad.up()
    assert evs(settle(pad, m), *POINTER, *KEYS) == []
    # gestures in the band still work: tap, slide, two-finger scroll, hold
    m = pad.server.mark()
    pad.tap(CX, band)
    time.sleep(0.7)
    pad.down(CX, band)
    pad.drag(1, CX, band, CX + 150, band - 20)
    pad.up()
    pad.down(CX - 40, band, 1)
    pad.down(CX + 40, band, 2)
    pad.drag(2, CX + 40, band, CX + 40, band - 80)
    pad.move(CX - 40, band - 80, 1)
    pad.drag(1, CX - 40, band - 80, CX - 40, band - 90)
    pad.up(2)
    pad.up(1)
    got = evs(settle(pad, m), *POINTER)
    assert got[:2] == ["ldown", "lup"] and "move" in got and "scroll" in got
    # the armed tint is the band only
    pad.down(CX, band)
    pad.wait_armed()
    assert abs(rect(pad, "#surface")["b"] - k["y"]) < 1
    pad.up()
    # and the trackpad comes back whole
    pad.press("kbd")
    pad.page.wait_for_function("!document.body.classList.contains('typing')")
    assert not pad.has_class("kbd", "lit")
    m = pad.server.mark()
    pad.tap()
    assert evs(settle(pad, m), *POINTER) == ["ldown", "lup"]


def test_keyboard_rows_and_arrows_are_big_enough(browser, server):
    for view in ({"width": 1180, "height": 820}, {"width": 820, "height": 1180}, {"width": 1024, "height": 768}):
        with open_pad(browser, server, view=view) as p:
            typing_on(p)
            for k in ("up", "down", "a"):
                assert rect(p, f'#keyboard .cap[data-k="{k}"]')["h"] >= 44, (view, k)
            row = rect(p, "#keyboard .row")["h"]
            assert 96 <= row <= 112 or row < 96 and view["height"] < 700, (view, row)
            assert rect(p, "#keyboard")["y"] - rect(p, "#top")["b"] > 60          # room for the band


def test_keyboard_layout(pad):
    typing_on(pad)
    rows = pad.page.evaluate("""[...document.querySelectorAll('#keyboard .row')].map(r =>
        [...r.querySelectorAll('.cap[data-k]')].map(c => c.dataset.k))""")
    assert rows == [
        ["grave", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "minus", "equal", "delete"],
        ["tab", "q", "w", "e", "r", "t", "y", "u", "i", "o", "p", "lbracket", "rbracket", "backslash"],
        ["esc", "a", "s", "d", "f", "g", "h", "j", "k", "l", "semicolon", "quote", "return"],
        ["shift", "z", "x", "c", "v", "b", "n", "m", "comma", "period", "slash", "shift"],
        ["control", "option", "command", "space", "command", "option", "left", "up", "down", "right"]]
    # every row is as wide as the keyboard
    widths = pad.page.evaluate("""[...document.querySelectorAll('#keyboard .row')].map(r => {
        const k = [...r.children], a = k[0].getBoundingClientRect(), b = k[k.length - 1].getBoundingClientRect();
        return b.right - a.left; })""")
    assert max(widths) - min(widths) < 1.5, widths
    known = load_module().KEYCODES                       # every key is one the Mac accepts
    assert all(k in known for row in rows for k in row)


def test_typing_a_letter(pad):
    typing_on(pad)
    m = pad.server.mark()
    tap_cap(pad, "a")
    assert keys(settle(pad, m)) == [("keydown", "a", []), ("keyup", "a", [])]


def test_key_shows_while_pressed(pad):
    typing_on(pad)
    pad.down(*pad.cap("j"))
    pad.page.wait_for_function("document.querySelector('.cap[data-k=j]').classList.contains('on')")
    pad.up()
    pad.page.wait_for_function("!document.querySelector('.cap[data-k=j]').classList.contains('on')")


def test_shift_tap_types_one_shifted_character(pad):
    typing_on(pad)
    assert legend(pad, "1") == "1" and legend(pad, "q") == "q"
    m = pad.server.mark()
    tap_cap(pad, "shift")
    assert mod_class(pad, "shift") == "armed"
    assert legend(pad, "1") == "!" and legend(pad, "q") == "Q" and legend(pad, "slash") == "?"
    time.sleep(0.2)
    assert keys(pad.server.log(m)) == []                 # a modifier on its own sends nothing yet
    tap_cap(pad, "1")
    tap_cap(pad, "2")
    assert keys(settle(pad, m)) == [
        ("flags", "shift", ["shift"]), ("keydown", "1", ["shift"]), ("keyup", "1", ["shift"]),
        ("flags", "shift", []), ("keydown", "2", []), ("keyup", "2", [])]
    assert mod_class(pad, "shift") == "" and legend(pad, "1") == "1"


def test_command_chord_with_a_held_modifier(pad):
    """Command held with one finger while another taps C: a plain held modifier."""
    typing_on(pad)
    m = pad.server.mark()
    pad.down(*pad.cap("command"), id=1)
    tap_cap(pad, "c", id=2)
    tap_cap(pad, "v", id=2)
    pad.up(1)
    assert keys(settle(pad, m)) == [
        ("flags", "command", ["command"]), ("keydown", "c", ["command"]), ("keyup", "c", ["command"]),
        ("keydown", "v", ["command"]), ("keyup", "v", ["command"]), ("flags", "command", [])]
    assert mod_class(pad, "command") == ""               # held and used: not left armed
    m = pad.server.mark()
    tap_cap(pad, "c")
    assert keys(settle(pad, m)) == [("keydown", "c", []), ("keyup", "c", [])]


def test_command_shift_4_and_option_left(pad):
    typing_on(pad)
    m = pad.server.mark()
    pad.down(*pad.cap("command", 1), id=1)               # the right-hand Command
    pad.down(*pad.cap("shift"), id=2)
    tap_cap(pad, "4", id=3)
    pad.up(2)
    pad.up(1)
    tap_cap(pad, "option")                               # one-shot this time
    tap_cap(pad, "left")
    assert keys(settle(pad, m)) == [
        ("flags", "command", ["command"]), ("flags", "shift", ["command", "shift"]),
        ("keydown", "4", ["command", "shift"]), ("keyup", "4", ["command", "shift"]),
        ("flags", "shift", ["command"]), ("flags", "command", []),
        # arrow keys carry fn, as they do from a real Mac keyboard
        ("flags", "option", ["option"]), ("keydown", "left", ["fn", "option"]),
        ("keyup", "left", ["fn", "option"]), ("flags", "option", [])]


def test_double_tap_locks_a_modifier_until_tapped_again(pad):
    typing_on(pad)
    m = pad.server.mark()
    tap_cap(pad, "shift")
    tap_cap(pad, "shift")
    assert mod_class(pad, "shift") == "locked"
    assert pad.page.evaluate("document.querySelectorAll('#keyboard .cap.locked').length") == 2   # both Shift keys
    tap_cap(pad, "a")
    tap_cap(pad, "b")
    assert mod_class(pad, "shift") == "locked" and legend(pad, "a") == "A"
    time.sleep(0.2)
    assert keys(pad.server.log(m)) == [
        ("flags", "shift", ["shift"]), ("keydown", "a", ["shift"]), ("keyup", "a", ["shift"]),
        ("keydown", "b", ["shift"]), ("keyup", "b", ["shift"])]          # Shift stays down meanwhile
    tap_cap(pad, "shift", n=1)                           # either Shift unlocks it
    assert mod_class(pad, "shift") == ""
    tap_cap(pad, "c")
    assert keys(settle(pad, m))[5:] == [("flags", "shift", []), ("keydown", "c", []), ("keyup", "c", [])]


def test_armed_and_locked_look_different(pad):
    typing_on(pad)
    # wait past the colour change, and past the 400 ms in which a second tap locks
    look = lambda: (time.sleep(0.45),
                    pad.page.evaluate("getComputedStyle(document.querySelector('.cap[data-k=control]')).backgroundColor"))[1]
    tap_cap(pad, "control")
    armed = look()
    tap_cap(pad, "control")                              # a slow second tap: off again
    tap_cap(pad, "control")
    tap_cap(pad, "control")                              # tapped twice quickly: locked
    locked = look()
    tap_cap(pad, "control")
    off = look()
    assert len({armed, locked, off}) == 3


def test_tapping_a_modifier_alone_sends_nothing(pad):
    """Tap, lock, unlock: the Mac sees nothing (some apps act on a double-tapped Shift)."""
    typing_on(pad)
    m = pad.server.mark()
    for _ in range(3):
        tap_cap(pad, "shift")
        time.sleep(0.05)
    tap_cap(pad, "command")
    time.sleep(0.6)                                      # past the double-tap time
    tap_cap(pad, "command")                              # a second, slow tap: off again, not locked
    assert mod_class(pad, "shift") == "" and mod_class(pad, "command") == ""
    assert keys(settle(pad, m)) == []


def test_held_key_repeats_then_one_key_up(pad):
    typing_on(pad)
    m = pad.server.mark()
    pad.page.evaluate("window.__t0 = performance.now()")
    pad.down(*pad.cap("delete"))
    time.sleep(1.0)
    pad.up()
    held = pad.page.evaluate("performance.now() - window.__t0")
    e = keys(settle(pad, m))
    assert e[0] == ("keydown", "delete", []) and e[-1] == ("keyup", "delete", [])
    repeats = e[1:-1]
    assert all(x == ("keydown+", "delete", []) for x in repeats)
    # first repeat after 450 ms, then one every 55 ms
    assert (held - 450) / 55 - 4 <= len(repeats) <= (held - 450) / 55 + 1, (held, len(repeats))
    assert [x[0] for x in e].count("keyup") == 1


def test_repeat_carries_the_modifiers(pad):
    typing_on(pad)
    m = pad.server.mark()
    tap_cap(pad, "option")
    pad.down(*pad.cap("delete"))
    time.sleep(0.7)
    pad.up()
    e = keys(settle(pad, m))
    assert e[0] == ("flags", "option", ["option"]) and e[-1] == ("flags", "option", [])
    assert e[1] == ("keydown", "delete", ["option"]) and e[-2] == ("keyup", "delete", ["option"])
    assert len(e) > 4 and all(x == ("keydown+", "delete", ["option"]) for x in e[2:-2])


def test_only_the_newest_key_repeats(pad):
    typing_on(pad)
    m = pad.server.mark()
    pad.down(*pad.cap("a"), id=1)
    time.sleep(0.1)
    pad.down(*pad.cap("l"), id=2)
    time.sleep(0.8)
    pad.up(2)
    pad.up(1)
    e = keys(settle(pad, m))
    assert ("keydown+", "a", []) not in e and ("keydown+", "l", []) in e
    assert [x for x in e if x[0] == "keyup"] == [("keyup", "l", []), ("keyup", "a", [])]


def test_each_finger_presses_its_own_key(pad):
    typing_on(pad)
    m = pad.server.mark()
    pad.down(*pad.cap("f"), id=1)
    pad.down(*pad.cap("j"), id=2)
    pad.up(1)
    pad.up(2)
    assert keys(settle(pad, m)) == [("keydown", "f", []), ("keydown", "j", []),
                                    ("keyup", "f", []), ("keyup", "j", [])]


def test_keys_let_go_when_the_keyboard_is_put_away(pad):
    typing_on(pad)
    s = pad.server
    m = s.mark()
    pad.down(*pad.cap("command"), id=1)
    pad.down(*pad.cap("s"), id=2)
    s.wait_for(lambda e: "keydown" in evs(e), since=m)
    tap_cap(pad, "shift", id=3)                          # and an armed Shift
    pad.press("kbd", id=4)
    pad.page.wait_for_function("!document.body.classList.contains('typing')")
    pad.up(2)
    pad.up(1)
    assert keys(settle(pad, m)) == [("flags", "command", ["command"]), ("keydown", "s", ["command"]),
                                    ("keyup", "s", ["command"]), ("flags", "command", [])]
    typing_on(pad)                                       # nothing is left armed or pressed
    assert mod_class(pad, "shift") == "" and pad.page.evaluate("document.querySelectorAll('.cap.on').length") == 0


def test_keys_let_go_when_the_connection_drops(pad):
    typing_on(pad)
    s = pad.server
    m = s.mark()
    pad.down(*pad.cap("option"), id=1)
    pad.down(*pad.cap("d"), id=2)
    s.wait_for(lambda e: "keydown" in evs(e), since=m)
    s.drop()
    e = s.wait_for(lambda e: "disconnect" in evs(e), since=m, settle=0.2)
    assert keys(e) == [("flags", "option", ["option"]), ("keydown", "d", ["option"]),
                       ("keyup", "d", ["option"]), ("flags", "option", [])]
    pad.wait_offline()
    assert pad.page.evaluate("document.querySelectorAll('.cap.on').length") == 0
    pad.wait_online()
    m2 = s.mark()
    pad.up(2)                                            # the fingers lift after the reconnect
    pad.up(1)
    assert keys(settle(pad, m2)) == []


def test_keys_let_go_when_the_page_is_hidden(pad):
    typing_on(pad)
    s = pad.server
    m = s.mark()
    pad.down(*pad.cap("e"))
    s.wait_for(lambda e: "keydown" in evs(e), since=m)
    pad.set_hidden(True)
    s.wait_for(lambda e: "keyup" in evs(e), since=m, timeout=2)
    pad.set_hidden(False)
    pad.up()
    assert keys(settle(pad, m)) == [("keydown", "e", []), ("keyup", "e", [])]


def test_keyboard_palm_rejection(pad):
    """While the Pencil presses a key, finger touches on the keyboard and the rail are ignored."""
    typing_on(pad)
    m = pad.server.mark()
    x, y = pad.cap("k")
    pad.pen_down(x, y)
    tap_cap(pad, "s", id=2)
    pad.tap(*pad.center("esc"), id=3)
    pad.pen_up(x, y)
    tap_cap(pad, "s", id=2)                              # after the Pencil lifts, fingers type again
    assert keys(settle(pad, m)) == [("keydown", "k", []), ("keyup", "k", []),
                                    ("keydown", "s", []), ("keyup", "s", [])]


def test_rail_keeps_working_while_typing(pad):
    typing_on(pad)
    m = pad.server.mark()
    pad.press("esc")
    pad.press("app")
    e = keys(settle(pad, m))
    assert e[:2] == [("keydown", "esc", []), ("keyup", "esc", [])] and ("keydown", "tab", ["command"]) in e
    assert pad.typing()


def test_the_tour_puts_the_keyboard_away(pad):
    typing_on(pad)
    pad.press("help")
    pad.wait_step("move")
    assert not pad.typing() and not pad.has_class("kbd", "lit")


# ------------------------------------------------------------------ a Mac with an older PencilPad

def as_older_mac(page):
    """The pad's socket, with the Mac's hello stripped of what an older PencilPad leaves out."""
    def handler(ws):
        mac = ws.connect_to_server()

        def from_mac(msg):
            try:
                hello = json.loads(msg)
                hello.pop("caps", None)
                msg = json.dumps(hello)
            except (ValueError, TypeError, AttributeError):
                pass
            ws.send(msg)
        mac.on_message(from_mac)
        ws.on_message(lambda msg: mac.send(msg))
    page.route_web_socket(re.compile(r".*/ws(\?.*)?$"), handler)      # (the page names itself: /ws?pad=…)


def test_older_mac_asks_to_be_updated(browser, server):
    with open_pad(browser, server, route=as_older_mac) as p:
        p.page.wait_for_function("cur && Array.isArray(cur.caps)")
        name = p.page.evaluate("cur.name")
        # the keys an older Mac cannot do say so
        for key in ("space", "copy", "paste", "mc", "app", "kbd"):
            assert p.has_class(key, "old") and p.text(key).endswith("Update the Mac"), key
        for key in ("esc", "sub", "dict", "shot"):
            assert not p.has_class(key, "old"), key
        m = server.mark()
        for key in ("mc", "app", "copy", "paste", "space", "kbd"):
            p.press(key)
            want = {"mc": "Windows", "app": "Switch", "copy": "Copy", "paste": "Paste", "space": "Space",
                    "kbd": "The keyboard"}[key]
            assert p.text("statusText") == f"{want} needs PencilPad updated on {name}"
            assert p.page.evaluate("document.body.classList.contains('notice')")
            p.page.wait_for_function("['rgb(255, 159, 10)', 'rgb(255, 149, 0)'].includes("
                                     "getComputedStyle(document.getElementById('dot')).backgroundColor)")   # orange
            assert not p.has_class(key, "on")
        assert not p.typing()                            # the keyboard does not open
        # nor does a pinch zoom, and nothing armed a modifier
        p.down(CX - 40, CY, 1)
        p.down(CX + 40, CY, 2)
        for i in range(1, 16):
            p.move(CX - 40 - 8 * i, CY, 1)
            p.move(CX + 40 + 8 * i, CY, 2)
            p.page.wait_for_timeout(16)
        p.up(2)
        p.up(1)
        assert p.text("statusText") == f"Zoom needs PencilPad updated on {name}"
        p.page.wait_for_timeout(400)
        assert evs(server.log(m), "missioncontrol", *KEYS, "scroll") == []
        # Escape, Return, Screenshot and Dictate still work: their messages have not changed
        for key in ("esc", "sub", "shot"):
            p.press(key)
            p.page.wait_for_timeout(300)
        p.down(*p.center("dict"))
        p.page.wait_for_timeout(200)
        p.up()
        p.page.wait_for_timeout(600)
        e = server.log(m)
        assert [x["key"] for x in e if x["ev"] == "keydown"] == ["esc", "return", "v", "f13"]
        assert "screencapture" in evs(e)
        # and the notice goes away by itself
        p.page.wait_for_function("document.getElementById('statusText').textContent.startsWith('Connected')",
                                 timeout=6000)
        assert not p.page.evaluate("document.body.classList.contains('notice')")


def test_older_mac_settings_rows_are_disabled(browser, server):
    with open_pad(browser, server, route=as_older_mac) as p:
        p.page.wait_for_function("cur && Array.isArray(cur.caps)")
        name = p.page.evaluate("cur.name")
        p.press("gear")
        p.page.wait_for_function("document.body.classList.contains('setting')")
        banner = p.page.evaluate("(() => { const b = document.getElementById('oldMac'); return b.hidden ? null : b.textContent; })()")
        assert banner == f"{name} has an older PencilPad. Update it on that Mac to change these settings."
        assert p.page.evaluate("document.querySelectorAll('#sheet .banner').length") == 1
        assert p.page.evaluate("[...document.querySelectorAll('#sheet .set.mac')].every(r => r.classList.contains('dis'))")
        assert not p.page.evaluate("document.querySelector('[data-local=pinch]').classList.contains('dis')")


# ------------------------------------------------------------------ rail keys that are keys

def test_space_is_a_real_key_that_repeats(pad):
    m = pad.server.mark()
    pad.down(*pad.center("space"))
    time.sleep(0.7)
    pad.up()
    e = keys(settle(pad, m))
    assert e[0] == ("keydown", "space", []) and e[-1] == ("keyup", "space", [])
    assert ("keydown+", "space", []) in e and [x[0] for x in e].count("keyup") == 1


def test_rail_return_takes_a_one_shot_shift(pad):
    typing_on(pad)
    tap_cap(pad, "shift")
    m = pad.server.mark()
    pad.press("sub")
    pad.press("esc")
    assert keys(settle(pad, m)) == [("flags", "shift", ["shift"]), ("keydown", "return", ["shift"]),
                                    ("keyup", "return", ["shift"]), ("flags", "shift", []),
                                    ("keydown", "esc", []), ("keyup", "esc", [])]
    assert mod_class(pad, "shift") == ""


def test_fixed_actions_ignore_and_keep_the_modifiers(pad):
    """Copy, Paste and Switch post exactly their chord; a locked Shift stays locked."""
    typing_on(pad)
    tap_cap(pad, "shift")
    tap_cap(pad, "shift")
    tap_cap(pad, "a")                                     # Shift now down on the Mac
    m = pad.server.mark()
    pad.press("copy")
    time.sleep(0.15)
    pad.press("app")
    time.sleep(0.15)
    pad.press("paste")
    e = keys(settle(pad, m))
    for k in ("c", "tab", "v"):
        assert ("keydown", k, ["command"]) in e, (k, e)
    assert mod_class(pad, "shift") == "locked"
    tap_cap(pad, "shift")
    assert keys(settle(pad, m))[-1] == ("flags", "shift", [])


def test_copy_and_paste_keys(pad):
    m = pad.server.mark()
    pad.press("copy")
    time.sleep(0.2)
    pad.press("paste")
    assert keys(settle(pad, m)) == [
        ("flags", "command", ["command"]), ("keydown", "c", ["command"]), ("keyup", "c", ["command"]),
        ("flags", "command", []),
        ("flags", "command", ["command"]), ("keydown", "v", ["command"]), ("keyup", "v", ["command"]),
        ("flags", "command", [])]


def test_cancelled_touch_on_a_modifier_is_not_a_tap(pad):
    """iPadOS palm rejection cancels touches; that must not arm (or lock) a modifier."""
    typing_on(pad)
    for _ in range(2):
        pad.down(*pad.cap("shift"))
        time.sleep(0.05)
        pad.cancel()
        time.sleep(0.05)
    assert mod_class(pad, "shift") == ""
    m = pad.server.mark()
    tap_cap(pad, "a")
    assert keys(settle(pad, m)) == [("keydown", "a", []), ("keyup", "a", [])]


def test_two_fingers_on_one_key_are_one_press(pad):
    typing_on(pad)
    m = pad.server.mark()
    x, y = pad.cap("a")
    pad.down(x, y, id=1)
    time.sleep(0.05)
    pad.down(x + 3, y + 3, id=2)
    time.sleep(0.05)
    pad.up(1)
    time.sleep(0.8)
    pad.up(2)
    e = keys(settle(pad, m))
    assert [x[0] for x in e].count("keydown") == 1 and [x[0] for x in e].count("keyup") == 1
    assert e[-1] == ("keyup", "a", [])                   # no repeats after it


def test_esc_where_caps_lock_would_be(pad):
    typing_on(pad)
    m = pad.server.mark()
    tap_cap(pad, "esc")
    assert keys(settle(pad, m)) == [("keydown", "esc", []), ("keyup", "esc", [])]
    assert rect(pad, '#keyboard .cap[data-k="esc"]')["x"] == rect(pad, '#keyboard .cap[data-k="tab"]')["x"]


def test_shift_glyph_is_filled_when_on(pad):
    typing_on(pad)
    fill = lambda: pad.page.evaluate("getComputedStyle(document.querySelector('.cap[data-k=shift] svg')).fill")
    assert fill() == "none"
    tap_cap(pad, "shift")
    assert fill() != "none"


# ------------------------------------------------------------------ pinch

def pinch(pad, start, end, steps=24, cx=CX, cy=CY):
    """Two fingers from `start` px apart to `end` px apart, about the same middle."""
    pad.down(cx - start / 2, cy, 1)
    pad.down(cx + start / 2, cy, 2)
    for i in range(1, steps + 1):
        d = start + (end - start) * i / steps
        pad.move(cx - d / 2, cy, 1)
        pad.move(cx + d / 2, cy, 2)
        time.sleep(0.02)
    pad.up(2)
    pad.up(1)


def zooms(e):
    return [x["key"] for x in e if x["ev"] == "keydown" and x["key"] in ("equal", "minus")]


def test_pinch_out_zooms_in_in_a_few_steps(pad):
    m = pad.server.mark()
    pinch(pad, 100, 350)
    e = settle(pad, m, 0.5)
    assert 3 <= len(zooms(e)) <= 5 and set(zooms(e)) == {"equal"}, zooms(e)
    assert evs(e, "scroll", "rdown", "ldown") == []


def test_pinch_in_zooms_out(pad):
    m = pad.server.mark()
    pinch(pad, 350, 100)
    e = settle(pad, m, 0.5)
    assert 3 <= len(zooms(e)) <= 5 and set(zooms(e)) == {"minus"}, zooms(e)
    assert evs(e, "scroll") == []


ZOOM_TIMES = """() => { window.__z = []; const orig = send;
    window.send = o => { if (o.t === 'zoom') __z.push(performance.now()); return orig(o); }; }"""


def test_a_fast_pinch_is_not_a_burst(pad):
    """A whole pinch in four moves zooms as much as a slow one of the same size (the
    steps still owed when the fingers lift follow), one step per 120 ms at most."""
    pad.page.evaluate(ZOOM_TIMES)
    m = pad.server.mark()
    pinch(pad, 100, 420)
    slow = len(zooms(settle(pad, m, 1.0)))
    pad.page.evaluate("window.__z = []")
    m = pad.server.mark()
    pinch(pad, 100, 420, steps=4)
    fast = zooms(settle(pad, m, 1.2))
    assert slow == 5 and abs(len(fast) - slow) <= 1 and set(fast) == {"equal"}, (slow, fast)
    times = pad.page.evaluate("window.__z")
    assert all(b - a >= 115 for a, b in zip(times, times[1:])), times


def test_a_pinch_zooms_at_most_six_steps(pad):
    m = pad.server.mark()
    pinch(pad, 40, 900, steps=30)
    assert len(zooms(settle(pad, m, 1.2))) == 6


def test_scrolling_never_zooms(pad):
    m = pad.server.mark()
    pad.down(CX - 60, CY - 100, 1)
    pad.down(CX + 60, CY - 100, 2)
    for i in range(1, 16):
        pad.move(CX - 60 + i, CY - 100 + 12 * i, 1)          # a little uneven, as fingers are
        pad.move(CX + 60 - i, CY - 100 + 12 * i, 2)
        time.sleep(0.016)
    pad.up(2)
    pad.up(1)
    e = settle(pad, m)
    assert zooms(e) == [] and "scroll" in evs(e)


def test_pinch_can_be_turned_off(browser, server):
    with open_pad(browser, server, init=["try { localStorage.setItem('pencilpad-pinch', 'off'); } catch (_) {}"]) as p:
        m = server.mark()
        pinch(p, 100, 350)
        assert zooms(settle(p, m, 0.5)) == []


def test_pinch_in_the_tour_is_practice(first_run):
    m = first_run.server.mark()
    pinch(first_run, 100, 350, cx=300)
    assert zooms(settle(first_run, m, 0.5)) == []


# ------------------------------------------------------------------ the status line

def test_long_mac_name_keeps_the_buttons_on_screen(browser, server):
    with open_pad(browser, server, view={"width": 820, "height": 1180}) as p:
        p.page.evaluate("n => { cur.name = n; showStatus(false); }", "Someone’s MacBook Pro with a really very long name, 16-inch")
        surface = rect(p, "#surface")
        for el in ("#help", "#gear"):
            r = rect(p, el)
            assert surface["x"] < r["x"] and r["r"] < surface["r"], (el, r)
        assert p.page.evaluate("(() => { const t = document.getElementById('statusText'); return t.scrollWidth > t.clientWidth; })()")


def test_hidden_secure_row_is_not_shown(pad):
    pad.press("status")
    pad.page.wait_for_function("document.body.classList.contains('picking')")
    assert pad.page.evaluate("document.getElementById('secure').hidden")
    assert pad.page.evaluate("getComputedStyle(document.getElementById('secure')).display") == "none"


# ------------------------------------------------------------------ stuck keys (from QA)

def final_flags(e):
    f = [x for x in keys(e) if x[0] == "flags"]
    return f[-1] if f else None


def test_switching_mac_with_locked_modifiers_leaves_none_down(pad):
    """reconnect() cannot send the key-ups; the Mac's own release must end with no modifier."""
    typing_on(pad)
    s = pad.server
    m = s.mark()
    for k in ("command", "command", "shift", "shift"):
        tap_cap(pad, k)
    tap_cap(pad, "4")
    s.wait_for(lambda e: "keyup" in evs(e), since=m)
    pad.page.evaluate("reconnect()")
    e = s.wait_for(lambda e: "disconnect" in evs(e), since=m, settle=0.3)
    after = e[evs(e).index("disconnect"):]
    assert final_flags(after)[2] == []
    assert pad.page.evaluate("MODS.every(n => mod[n].state === 'off' && !mod[n].mac)")


def test_many_fingers_then_cancel_lets_go_of_everything(pad):
    typing_on(pad)
    m = pad.server.mark()
    for i, k in enumerate(["command", "shift", "a", "s", "d", "f"], 1):
        pad.down(*pad.cap(k), id=i)
        time.sleep(0.02)
    time.sleep(0.3)
    pad.cancel()
    raw = settle(pad, m, 0.5)
    e = keys(raw)
    downs = {k[1] for k in e if k[0] == "keydown"}
    assert downs == {k[1] for k in e if k[0] == "keyup"} and final_flags(raw)[2] == []
    assert pad.page.evaluate("MODS.every(n => mod[n].state === 'off')")      # cancels are not taps


def test_folding_the_rail_does_not_let_go_of_a_keyboard_key(pad):
    typing_on(pad)
    m = pad.server.mark()
    pad.down(*pad.cap("k"), id=1)
    time.sleep(0.05)
    pad.press("railToggle", id=2)
    time.sleep(0.3)
    pad.up(1)
    e = keys(settle(pad, m))
    assert e[0] == ("keydown", "k", []) and e[-1] == ("keyup", "k", []) and [x[0] for x in e].count("keyup") == 1


def test_turning_the_ipad_with_a_key_held(pad):
    typing_on(pad)
    m = pad.server.mark()
    x, y = pad.cap("j")
    pad.down(x, y)
    time.sleep(0.1)
    pad.page.set_viewport_size({"width": 820, "height": 1180})
    time.sleep(0.3)
    pad.up()
    e = keys(settle(pad, m))
    assert e[-1][0] == "keyup" and [k[0] for k in e].count("keyup") == 1
    pad.page.set_viewport_size(VIEW)


def test_a_touch_while_the_mac_list_is_open_only_closes_it(pad):
    typing_on(pad)
    m = pad.server.mark()
    pad.down(*pad.cap("a"), id=1)
    pad.press("status", id=2)
    pad.page.wait_for_function("document.body.classList.contains('picking')")
    tap_cap(pad, "s", id=3)
    pad.up(1)
    assert [k for k in keys(settle(pad, m)) if not k[0].endswith("+")] == [("keydown", "a", []), ("keyup", "a", [])]


# ------------------------------------------------------------------ hiding a key while it is pressed

def test_hiding_a_pressed_key_lets_go_of_it(pad):
    m = pad.server.mark()
    pad.down(*pad.center("space"), id=1)
    time.sleep(0.6)                                     # repeating by now
    pad.page.evaluate("""() => { localStorage.setItem('pencilpad-hidden', JSON.stringify(['space'])); applyLocal(); }""")
    time.sleep(0.2)
    n = len([x for x in pad.server.log(m) if x["ev"] == "keydown"])
    time.sleep(0.5)
    assert len([x for x in pad.server.log(m) if x["ev"] == "keydown"]) == n          # stopped
    pad.up(1)
    e = keys(settle(pad, m))
    assert e[-1] == ("keyup", "space", []) and [x[0] for x in e].count("keyup") == 1


# ------------------------------------------------------------------ clicks take the keyboard's modifiers

def band_y(pad):
    return (rect(pad, "#keyboard")["y"] + rect(pad, "#top")["b"]) / 2


def mouse(e):
    return [(x["ev"], x["mods"]) for x in e if x["ev"] in ("ldown", "lup", "drag", "rdown", "rup", "scroll")]


def test_shift_click_with_a_locked_shift(pad):
    typing_on(pad)
    tap_cap(pad, "shift")
    tap_cap(pad, "shift")
    m = pad.server.mark()
    pad.tap(CX, band_y(pad))
    e = settle(pad, m)
    assert mouse(e) == [("ldown", ["shift"]), ("lup", ["shift"])]
    assert mod_class(pad, "shift") == "locked"


def test_command_click_with_a_held_command(pad):
    typing_on(pad)
    m = pad.server.mark()
    pad.down(*pad.cap("command"), id=1)
    pad.tap(CX, band_y(pad), id=2)
    pad.up(1)
    e = settle(pad, m)
    assert mouse(e) == [("ldown", ["command"]), ("lup", ["command"])]
    assert keys(e)[-1] == ("flags", "command", [])


def test_option_drag_with_a_one_shot_option(pad):
    typing_on(pad)
    tap_cap(pad, "option")
    m = pad.server.mark()
    y = band_y(pad)
    pad.down(CX, y)
    pad.wait_armed()
    pad.drag(1, CX, y, CX + 120, y)
    pad.up()
    e = settle(pad, m)
    got = mouse(e)
    assert got[0] == ("ldown", ["option"]) and got[-1] == ("lup", ["option"])
    assert {mods for _, mods in [(a, tuple(b)) for a, b in got]} == {("option",)}
    assert mod_class(pad, "option") == ""              # a one-shot modifier is used up
    assert keys(e)[-1] == ("flags", "option", [])


def test_clicks_carry_nothing_without_a_modifier(pad):
    m = pad.server.mark()
    pad.tap()
    assert mouse(settle(pad, m)) == [("ldown", []), ("lup", [])]


# ------------------------------------------------------------------ round 5: held for a click, not a tap

def test_command_held_for_a_click_is_not_left_armed(pad):
    typing_on(pad)
    for hold in (0.15, 0.35):
        m = pad.server.mark()
        pad.down(*pad.cap("command"), id=2)
        time.sleep(0.05)
        pad.tap(CX, band_y(pad), id=1)
        time.sleep(hold - 0.05)
        pad.up(2)
        time.sleep(0.15)
        assert mod_class(pad, "command") == "", hold
        tap_cap(pad, "w")
        e = settle(pad, m)
        assert ("keydown", "w", []) in keys(e) and ("keydown", "w", ["command"]) not in keys(e), (hold, keys(e))


def test_shift_held_for_a_scroll_is_not_left_armed(pad):
    typing_on(pad)
    y = band_y(pad)
    m = pad.server.mark()
    pad.down(*pad.cap("shift"), id=2)
    time.sleep(0.05)
    pad.down(CX - 40, y, 1)
    pad.down(CX + 40, y, 3)
    pad.drag(3, CX + 40, y, CX + 40, y - 80, steps=6)
    pad.move(CX - 40, y - 80, 1)
    pad.up(3)
    pad.up(1)
    pad.up(2)
    time.sleep(0.15)
    assert mod_class(pad, "shift") == ""
    tap_cap(pad, "a")
    e = settle(pad, m)
    assert {tuple(x["mods"]) for x in e if x["ev"] == "scroll"} == {("shift",)}
    assert ("keydown", "a", []) in keys(e)


def test_a_one_shot_modifier_is_used_up_by_a_click(pad):
    typing_on(pad)
    tap_cap(pad, "shift")
    m = pad.server.mark()
    pad.tap(CX, band_y(pad))
    tap_cap(pad, "a")
    e = settle(pad, m)
    assert mouse(e) == [("ldown", ["shift"]), ("lup", ["shift"])] and ("keydown", "a", []) in keys(e)
    assert mod_class(pad, "shift") == ""


def test_a_locked_modifier_stays_locked_after_a_click(pad):
    typing_on(pad)
    tap_cap(pad, "shift")
    tap_cap(pad, "shift")
    pad.tap(CX, band_y(pad))
    time.sleep(0.2)
    m = pad.server.mark()
    tap_cap(pad, "a")
    assert mod_class(pad, "shift") == "locked" and ("keydown", "a", ["shift"]) in keys(settle(pad, m))


def test_held_through_a_click_then_a_key_is_a_plain_hold(pad):
    typing_on(pad)
    m = pad.server.mark()
    pad.down(*pad.cap("command"), id=2)
    pad.tap(CX, band_y(pad), id=1)
    tap_cap(pad, "c", id=1)
    pad.up(2)
    time.sleep(0.15)
    assert mod_class(pad, "command") == ""
    e = settle(pad, m)
    assert mouse(e) == [("ldown", ["command"]), ("lup", ["command"])]
    assert ("keydown", "c", ["command"]) in keys(e) and keys(e)[-1] == ("flags", "command", [])
