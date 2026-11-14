"""Gesture tests: the real served page in Chromium, driven with real touch and
pen input via CDP, asserting on what the dry-run server would have posted."""

import json
import re
import time

import pytest

from conftest import CENTER, VIEW, Server, corner_xy, evs, open_pad, wait_until

POINTER = ("move", "drag", "ldown", "lup", "rdown", "rup", "scroll")
KEYS = ("keydown", "keyup", "flags", "screencapture")
CX, CY = CENTER


def settle(pad, mark, wait=0.35):
    """Everything recorded since mark, after the page has had time to flush."""
    time.sleep(wait)
    return pad.server.log(mark)


# ------------------------------------------------------------------ pointer basics

def test_move(pad):
    m = pad.server.mark()
    pad.down(CX, CY)
    pad.drag(1, CX, CY, CX + 150, CY + 50)
    pad.up()
    e = settle(pad, m)
    assert set(evs(e, *POINTER)) == {"move"}
    last = [x for x in e if x["ev"] == "move"][-1]
    assert last["x"] > 960 + 150 and last["y"] > 540 + 30


def test_tap_clicks(pad):
    m = pad.server.mark()
    pad.tap()
    e = settle(pad, m)
    assert [(x["ev"], x["clicks"]) for x in e if x["ev"] in POINTER] == [("ldown", 1), ("lup", 1)]


def test_double_and_triple_tap_click_state(pad):
    m = pad.server.mark()
    for _ in range(3):
        pad.tap()
        time.sleep(0.1)
    e = settle(pad, m)
    assert [(x["ev"], x["clicks"]) for x in e if x["ev"] in POINTER] == [
        ("ldown", 1), ("lup", 1), ("ldown", 2), ("lup", 2), ("ldown", 3), ("lup", 3)]


def test_hold_then_lift_right_clicks(pad):
    m = pad.server.mark()
    pad.down(CX, CY)
    pad.wait_armed()
    pad.up()
    e = settle(pad, m)
    assert evs(e, *POINTER) == ["rdown", "rup"]
    assert not pad.has_class("pad", "armed")


def test_hold_then_move_drags(pad):
    m = pad.server.mark()
    pad.down(CX, CY)
    pad.wait_armed()
    pad.drag(1, CX, CY, CX + 200, CY, steps=12)
    pad.up()                                   # no pause: lift in the same frame as the last move
    e = settle(pad, m)
    names = evs(e, *POINTER)
    assert names[0] == "ldown" and names[-1] == "lup"
    assert set(names[1:-1]) == {"drag"}, names
    drags = [x for x in e if x["ev"] == "drag"]
    assert all(d["dx"] >= 0 for d in drags) and sum(d["dx"] for d in drags) > 200
    up = [x for x in e if x["ev"] == "lup"][0]
    assert up["x"] == drags[-1]["x"]           # moves were flushed before the button-up


def test_pen_tap_clicks(pad):
    m = pad.server.mark()
    pad.pen_down(CX, CY)
    time.sleep(0.05)
    pad.pen_up(CX, CY)
    e = settle(pad, m)
    assert evs(e, *POINTER) == ["ldown", "lup"]


def test_pen_hover_does_not_move_by_default(pad):
    m = pad.server.mark()
    for i in range(10):
        pad.pen_hover(CX + 10 * i, CY)
        time.sleep(0.016)
    e = settle(pad, m)
    assert evs(e, *POINTER) == []


# ------------------------------------------------------------------ two fingers

def test_two_finger_drag_scrolls_without_click(pad):
    m = pad.server.mark()
    pad.down(CX - 60, CY - 100, 1)
    pad.down(CX + 60, CY - 100, 2)
    for i in range(1, 16):
        pad.move(CX - 60, CY - 100 + 12 * i, 1)
        pad.move(CX + 60, CY - 100 + 12 * i, 2)
        time.sleep(0.016)
    pad.up(2)
    pad.up(1)
    e = settle(pad, m)
    names = evs(e, *POINTER)
    assert set(names) == {"scroll"}, names
    assert sum(x["dy"] for x in e if x["ev"] == "scroll") > 100


def test_scroll_then_remaining_finger_does_nothing(pad):
    m = pad.server.mark()
    pad.down(CX - 60, CY - 100, 1)
    pad.down(CX + 60, CY - 100, 2)
    for i in range(1, 10):
        pad.move(CX - 60, CY - 100 + 12 * i, 1)
        pad.move(CX + 60, CY - 100 + 12 * i, 2)
        time.sleep(0.016)
    pad.up(2)
    pad.drag(1, CX - 60, CY + 8, CX + 140, CY + 8)
    pad.up(1)
    e = settle(pad, m)
    assert set(evs(e, *POINTER)) == {"scroll"}


def test_two_finger_tap_right_clicks(pad):
    m = pad.server.mark()
    pad.down(CX - 40, CY, 1)
    pad.down(CX + 40, CY, 2)
    time.sleep(0.06)
    pad.up(2)
    pad.up(1)
    e = settle(pad, m)
    assert evs(e, *POINTER) == ["rdown", "rup"]


def test_moving_then_second_finger_tap_is_not_right_click(pad):
    """Pointer move, a second finger briefly touches, all lifted within 400ms."""
    m = pad.server.mark()
    pad.down(CX, CY, 1)
    pad.drag(1, CX, CY, CX + 80, CY, steps=6, dt=0.016)
    pad.down(CX + 200, CY, 2)
    time.sleep(0.03)
    pad.up(2)
    pad.up(1)
    e = settle(pad, m)
    assert "rdown" not in evs(e) and "ldown" not in evs(e)


# ------------------------------------------------------------------ jitter

def test_jittery_finger_tap_still_clicks(pad):
    """A finger held 'still' reports contact-centroid jitter of a pixel or two."""
    m = pad.server.mark()
    pad.down(CX, CY)
    for i in range(8):
        pad.move(CX + (2 if i % 2 == 0 else 0), CY + (0 if i % 3 else 1))
        time.sleep(0.012)
    pad.up()
    e = settle(pad, m)
    assert evs(e, *POINTER) == ["ldown", "lup"]


def test_jittery_hold_still_right_clicks(pad):
    m = pad.server.mark()
    pad.down(CX, CY)
    t_end = time.time() + 5
    i = 0
    while not pad.has_class("pad", "armed"):
        assert time.time() < t_end, "never armed"
        pad.move(CX + (1.5 if i % 2 else 0), CY + (1.5 if i % 4 < 2 else 0))
        i += 1
        time.sleep(0.016)
    pad.up()
    e = settle(pad, m)
    assert evs(e, *POINTER) == ["rdown", "rup"]


# ------------------------------------------------------------------ cancel / interruptions

def test_pointercancel_mid_drag_releases_button(pad):
    m = pad.server.mark()
    pad.down(CX, CY)
    pad.wait_armed()
    pad.drag(1, CX, CY, CX + 100, CY)
    pad.server.wait_for(lambda e: "ldown" in evs(e), since=m)
    pad.cancel()
    e = settle(pad, m)
    names = evs(e, *POINTER)
    assert names[0] == "ldown" and names[-1] == "lup"
    assert not pad.has_class("pad", "armed")


def test_pointercancel_does_not_click(pad):
    """iPadOS cancels touches for system gestures (edge swipes, Control Center)."""
    m = pad.server.mark()
    pad.down(CX, CY)
    time.sleep(0.05)
    pad.cancel()
    e = settle(pad, m)
    assert evs(e, *POINTER) == []


def test_pointercancel_after_hold_does_not_right_click(pad):
    m = pad.server.mark()
    pad.down(CX, CY)
    pad.wait_armed()
    pad.cancel()
    e = settle(pad, m)
    assert evs(e, *POINTER) == []


# ------------------------------------------------------------------ palm rejection / pen

def test_palm_rejection_while_pen_down(pad):
    m = pad.server.mark()
    pad.pen_down(CX, CY)
    # pen starts moving right (moving right away, so it does not arm a drag)
    for i in range(1, 6):
        pad.pen_move(CX + 15 * i, CY)
        time.sleep(0.016)
    # palm lands and slides upward; a second palm contact taps
    pad.down(CX + 200, CY + 200, 1)
    pad.drag(1, CX + 200, CY + 200, CX + 200, CY + 50)
    pad.tap(CX - 300, CY + 250, id=2)
    # pen keeps moving to the right
    for i in range(6, 11):
        pad.pen_move(CX + 15 * i, CY)
        time.sleep(0.016)
    pad.up(1)
    pad.pen_up(CX + 150, CY)
    e = settle(pad, m)
    assert set(evs(e, *POINTER)) == {"move"}
    last = [x for x in e if x["ev"] == "move"][-1]
    assert last["x"] > 960 + 150 and last["y"] == 540


def test_pen_landing_during_finger_drag_releases_button(pad):
    m = pad.server.mark()
    pad.down(CX, CY)
    pad.wait_armed()
    pad.drag(1, CX, CY, CX + 60, CY)
    pad.server.wait_for(lambda e: "drag" in evs(e), since=m)
    pad.pen_down(CX - 200, CY)
    for i in range(1, 8):
        pad.pen_move(CX - 200, CY + 10 * i)
        time.sleep(0.016)
    pad.pen_up(CX - 200, CY + 70)
    pad.up(1)
    e = settle(pad, m)
    names = evs(e, *POINTER)
    i_up = names.index("lup")
    assert names[0] == "ldown" and set(names[1:i_up]) == {"drag"}
    assert set(names[i_up + 1:]) == {"move"}, names    # the pen just moves, no 2nd release


def test_corner_ignored_while_pen_down(pad):
    m = pad.server.mark()
    pad.pen_down(CX, CY)
    pad.tap(*corner_xy("sub"))
    pad.tap(*corner_xy("dict"))
    pad.pen_up(CX, CY)
    e = settle(pad, m)
    assert evs(e, *KEYS) == []


# ------------------------------------------------------------------ corners

@pytest.mark.parametrize("corner,expected", [
    ("esc", [("keydown", "esc"), ("keyup", "esc")]),
    ("sub", [("keydown", "return"), ("keyup", "return")]),
    ("shot", [("screencapture", None), ("keydown", "v"), ("keyup", "v")]),
])
def test_corner_buttons(pad, corner, expected):
    m = pad.server.mark()
    pad.tap(*corner_xy(corner))
    e = settle(pad, m, wait=0.8)
    assert [(x["ev"], x.get("key")) for x in e if x["ev"] in KEYS] == expected
    assert evs(e, *POINTER) == []


def test_dictate_hold(pad):
    m = pad.server.mark()
    pad.down(*corner_xy("dict"))
    pad.server.wait_for(lambda e: "keydown" in evs(e), since=m)
    assert pad.has_class("dict", "on")
    time.sleep(0.5)
    assert evs(pad.server.log(m), *KEYS) == ["keydown"]        # still held
    pad.up()
    e = settle(pad, m)
    assert [(x["ev"], x["key"]) for x in e if x["ev"] in KEYS] == [("keydown", "f13"), ("keyup", "f13")]
    assert not pad.has_class("dict", "on")


def test_slide_from_pad_onto_corner_stays_pad_gesture(pad):
    m = pad.server.mark()
    sx, sy = corner_xy("sub")
    pad.down(CX, CY)
    pad.drag(1, CX, CY, sx, sy, steps=15)
    pad.up()
    e = settle(pad, m)
    assert evs(e, *KEYS) == []
    assert set(evs(e, *POINTER)) == {"move"}


def test_slide_from_corner_onto_pad_stays_corner(pad):
    m = pad.server.mark()
    ex, ey = corner_xy("esc")
    pad.down(ex, ey)
    pad.drag(1, ex, ey, CX, CY, steps=15)
    pad.up()
    e = settle(pad, m)
    assert evs(e, *POINTER) == []
    assert evs(e, *KEYS) == ["keydown", "keyup"]


# ------------------------------------------------------------------ connection loss

def test_dictate_released_when_socket_drops_mid_hold(pad):
    s = pad.server
    m = s.mark()
    pad.down(*corner_xy("dict"))
    s.wait_for(lambda e: "keydown" in evs(e), since=m)
    s.drop()
    e = s.wait_for(lambda e: "disconnect" in evs(e), since=m)
    assert [(x["ev"], x.get("key")) for x in e if x["ev"] in KEYS] == [("keydown", "f13"), ("keyup", "f13")]
    # the page must not keep showing dictation as on
    pad.wait_offline()
    assert not pad.has_class("dict", "on")
    pad.wait_online()
    m2 = s.mark()
    pad.up()                       # finger finally lifts after the reconnect
    e2 = settle(pad, m2)
    assert evs(e2, *KEYS) == []


def test_drag_state_reset_when_socket_drops(pad):
    s = pad.server
    m = s.mark()
    pad.down(CX, CY)
    pad.wait_armed()
    pad.drag(1, CX, CY, CX + 60, CY)
    s.wait_for(lambda e: "drag" in evs(e), since=m)
    s.drop()
    s.wait_for(lambda e: "lup" in evs(e) and "disconnect" in evs(e), since=m)
    pad.wait_offline()
    pad.wait_online()
    assert not pad.has_class("pad", "armed")
    m2 = s.mark()
    pad.drag(1, CX + 60, CY, CX + 160, CY)     # same finger keeps moving after reconnect
    pad.up()
    e2 = settle(pad, m2)
    # it must not look like a drag (no button) and must not move the pointer
    # as if the user had started a new gesture
    assert evs(e2, *POINTER) == []


def test_dictate_released_when_page_hidden(pad):
    s = pad.server
    m = s.mark()
    pad.down(*corner_xy("dict"))
    s.wait_for(lambda e: "keydown" in evs(e), since=m)
    pad.set_hidden(True)
    e = s.wait_for(lambda e: "keyup" in evs(e), since=m, timeout=2)
    assert not pad.has_class("dict", "on")
    pad.set_hidden(False)
    m2 = s.mark()
    pad.up()
    assert evs(settle(pad, m2), *KEYS) == []


def test_drag_released_when_page_hidden(pad):
    s = pad.server
    m = s.mark()
    pad.down(CX, CY)
    pad.wait_armed()
    pad.drag(1, CX, CY, CX + 60, CY)
    s.wait_for(lambda e: "drag" in evs(e), since=m)
    pad.set_hidden(True)
    s.wait_for(lambda e: "lup" in evs(e), since=m, timeout=2)
    assert not pad.has_class("pad", "armed")


def test_returning_to_page_does_not_open_two_sockets(pad):
    s = pad.server
    m = s.mark()
    s.drop()
    pad.wait_offline()
    pad.set_hidden(False)          # page comes back to the foreground while reconnecting
    time.sleep(2.0)
    e = s.log(m)
    open_sockets = evs(e).count("connect") - evs(e).count("disconnect") + 1   # +1: the one dropped
    assert open_sockets == 1, evs(e, "connect", "disconnect")


def test_toggle_mode_ui_not_flipped_while_disconnected(browser, server):
    def to_toggle(page):
        def handler(route):
            resp = route.fetch()
            body = resp.text()
            assert '"dictMode": "hold"' in body
            route.fulfill(response=resp, body=body.replace('"dictMode": "hold"', '"dictMode": "toggle"'))
        page.route(server.url, handler)

    with open_pad(browser, server, route=to_toggle) as p:
        assert p.text("dictSub") == "Tap to start or stop"
        p.tap(*corner_xy("dict"))
        assert p.has_class("dict", "on")
        p.tap(*corner_xy("dict"))
        assert not p.has_class("dict", "on")
        server.stop()                  # Mac side gone: the tap cannot be delivered
        p.wait_offline()
        p.tap(*corner_xy("dict"))
        time.sleep(0.2)
        assert not p.has_class("dict", "on")


# ------------------------------------------------------------------ status line / markup

def test_status_text_and_offline_state(pad):
    mac = pad.page.evaluate("CFG.mac")
    connected = f"Connected to {mac}" if mac else "Connected"
    assert pad.text("statusText") == connected
    assert not pad.page.evaluate("document.body.classList.contains('offline')")
    pad.down(CX, CY)
    pad.wait_armed()
    assert pad.text("statusText") == "Lift to right-click · move to drag"
    pad.up()
    pad.wait_class("pad", "armed", False)
    assert pad.text("statusText") == connected
    pad.server.drop()
    pad.wait_offline()
    assert pad.text("statusText") == "Reconnecting…"
    pad.wait_online()
    assert pad.text("statusText") == connected


def test_pad_centre_is_touchable_the_moment_it_connects(browser, server):
    # record, at the instant the page goes online, which element a touch at the centre would hit
    probe = """document.addEventListener('DOMContentLoaded', () => {
        new MutationObserver(() => {
            if (!document.body.classList.contains('offline') && window.__hit === undefined)
                window.__hit = document.elementFromPoint(%d, %d).closest('[id]').id;
        }).observe(document.body, {attributes: true});
    });""" % (CX, CY)
    with open_pad(browser, server, init=[probe]) as p:
        assert p.page.evaluate("window.__hit") == "pad"


def test_page_starts_offline_until_connected(browser, server):
    """Before the socket opens the page is in the offline state."""
    def hold_back_script(page):
        page.add_init_script("window.WebSocket = class { constructor() { this.readyState = 0; } close() {} };")

    with open_pad(browser, server, route=hold_back_script, connected=False) as p:
        assert p.page.evaluate("document.body.classList.contains('offline')")
        assert p.text("statusText") == "Connecting…"
        assert p.page.evaluate("getComputedStyle(document.getElementById('pad')).touchAction") == "none"


def test_armed_status_cleared_when_drag_starts_and_ends(pad):
    pad.down(CX, CY)
    pad.wait_armed()
    pad.drag(1, CX, CY, CX + 80, CY)
    assert pad.has_class("pad", "armed")            # stays armed for the whole drag
    pad.up()
    pad.wait_class("pad", "armed", False)
    assert pad.text("statusText").startswith("Connected")


# ------------------------------------------------------------------ thresholds

def stroke(pad, dist, pen=False, angle=(1, 0)):
    """Land, wander `dist` px in a straight line from the landing point, lift."""
    m = pad.server.mark()
    ax, ay = angle
    n = (ax * ax + ay * ay) ** 0.5
    tx, ty = CX + dist * ax / n, CY + dist * ay / n
    if pen:
        pad.pen_down(CX, CY)
        for i in range(1, 6):
            pad.pen_move(CX + (tx - CX) * i / 5, CY + (ty - CY) * i / 5)
            time.sleep(0.016)
        pad.pen_up(tx, ty)
    else:
        pad.down(CX, CY)
        pad.drag(1, CX, CY, tx, ty, steps=5)
        pad.up()
    return evs(settle(pad, m), *POINTER)


@pytest.mark.parametrize("dist", [8, 15, 19])
def test_touch_wander_inside_slop_is_a_click(pad, dist):
    assert stroke(pad, dist) == ["ldown", "lup"]


def test_touch_slop_is_straight_line_distance(pad):
    # 14 px across and 14 px down is 19.8 px from the landing point: still a tap
    assert stroke(pad, 19.8, angle=(1, 1)) == ["ldown", "lup"]
    # 15 px across and 15 px down is 21.2 px: no longer one (the pointer starts to move)
    assert stroke(pad, 21.3, angle=(1, 1)) == ["move"]


def test_touch_past_slop_is_not_a_click(pad):
    m = pad.server.mark()
    pad.down(CX, CY)
    pad.drag(1, CX, CY, CX + 21, CY, steps=3)
    pad.drag(1, CX + 21, CY, CX + 60, CY, steps=5)
    pad.up()
    e = settle(pad, m)
    assert set(evs(e, *POINTER)) == {"move"}


@pytest.mark.parametrize("dist", [8, 17])
def test_pen_wander_inside_slop_is_a_click(pad, dist):
    assert stroke(pad, dist, pen=True) == ["ldown", "lup"]


def test_pen_slop_is_tighter_than_touch(pad):
    """19 px is past the Pencil's 18 px slop (a finger's is 20 px)."""
    assert stroke(pad, 19, pen=True) == ["move"]
    assert stroke(pad, 19) == ["ldown", "lup"]


def test_jitter_path_length_does_not_add_up(pad):
    """Back and forth 10 px many times: a long path, but never far from the landing."""
    m = pad.server.mark()
    pad.down(CX, CY)
    for i in range(12):                           # 12 x ~20 px = a 240 px path
        pad.move(CX + (10 if i % 2 else -10), CY + (6 if i % 3 else -6))
    armed = pad.has_class("pad", "armed")         # only if this machine took over 0.5 s
    pad.up()
    names = evs(settle(pad, m), *POINTER)
    assert names == (["rdown", "rup"] if armed else ["ldown", "lup"])


def test_slow_tap_still_clicks(pad):
    """No time limit on a tap: any still lift before the hold arms is a click."""
    m = pad.server.mark()
    pad.page.evaluate("window.__armedSeen = false; new MutationObserver(() => {"
                      " if (pad.classList.contains('armed')) window.__armedSeen = true; })"
                      ".observe(pad, {attributes: true})")
    pad.down(CX, CY)
    time.sleep(0.3)
    pad.up()
    e = settle(pad, m)
    if pad.page.evaluate("window.__armedSeen"):
        pytest.skip("machine too slow: the 0.3 s touch ran past the 0.5 s hold")
    assert evs(e, *POINTER) == ["ldown", "lup"]


def hold_time(pad, pen=False):
    """ms from the pointer landing to the pad arming, measured inside the page."""
    pad.page.evaluate("""() => {
        window.__h = {};
        pad.addEventListener('pointerdown', () => { __h.down = performance.now(); }, {capture: true, once: true});
        new MutationObserver(() => {
            if (pad.classList.contains('armed') && !__h.armed) __h.armed = performance.now();
        }).observe(pad, {attributes: true});
    }""")
    pad.pen_down(CX, CY) if pen else pad.down(CX, CY)
    pad.wait_armed()
    h = pad.page.evaluate("window.__h")
    pad.pen_up(CX, CY) if pen else pad.up()
    return h["armed"] - h["down"]


@pytest.mark.parametrize("pen", [False, True], ids=["touch", "pen"])
def test_hold_arms_after_half_a_second(pad, pen):
    m = pad.server.mark()
    ms = hold_time(pad, pen)
    assert 490 <= ms < 900, ms
    assert evs(settle(pad, m), *POINTER) == ["rdown", "rup"]


def test_page_thresholds(pad):
    got = pad.page.evaluate("[HOLD_MS, TWO_TAP_MS, SLOP_PEN, SLOP_TOUCH, TWO_SLOP]")
    assert got == [500, 600, 18, 20, 20]


def two_finger_tap(pad, hold, wander=0):
    m = pad.server.mark()
    pad.page.evaluate("window.__t0 = performance.now()")
    pad.down(CX - 40, CY, 1)
    pad.down(CX + 40, CY, 2)
    if wander:
        pad.move(CX - 40, CY + wander, 1)
        pad.move(CX + 40, CY + wander, 2)
    time.sleep(hold)
    pad.up(2)
    pad.up(1)
    took = pad.page.evaluate("performance.now() - window.__t0")
    return evs(settle(pad, m), *POINTER), took


def test_two_finger_tap_up_to_600ms_right_clicks(pad):
    names, took = two_finger_tap(pad, 0.3)
    if took >= 600:
        pytest.skip("machine too slow for this timing")
    assert names == ["rdown", "rup"]


def test_two_finger_rest_longer_than_600ms_does_nothing(pad):
    names, _ = two_finger_tap(pad, 0.8)
    assert names == []


def test_two_finger_tap_with_small_wander_right_clicks(pad):
    names, _ = two_finger_tap(pad, 0.05, wander=15)
    assert names == ["rdown", "rup"]


def test_two_finger_wander_past_slop_scrolls_instead(pad):
    names, _ = two_finger_tap(pad, 0.05, wander=30)
    assert "rdown" not in names and "ldown" not in names


def test_three_finger_tap_does_nothing(pad):
    m = pad.server.mark()
    pad.down(CX - 60, CY, 1)
    pad.down(CX, CY, 2)
    pad.down(CX + 60, CY, 3)
    time.sleep(0.05)
    pad.up(3)
    pad.up(2)
    pad.up(1)
    assert evs(settle(pad, m), *POINTER) == []


def test_double_tap_within_server_window(pad):
    """Two taps 0.3 s apart are a double-click (window 0.6 s)."""
    m = pad.server.mark()
    pad.tap()
    time.sleep(0.3)
    pad.tap()
    e = pad.server.wait_for(lambda e: evs(e).count("lup") == 2, since=m)
    assert [x["clicks"] for x in e if x["ev"] == "ldown"] == [1, 2]


def test_taps_far_apart_in_time_are_single_clicks(pad):
    m = pad.server.mark()
    pad.tap()
    time.sleep(0.8)
    pad.tap()
    e = pad.server.wait_for(lambda e: evs(e).count("lup") == 2, since=m)
    assert [x["clicks"] for x in e if x["ev"] == "ldown"] == [1, 1]


# ------------------------------------------------------------------ hold ring

def ring(pad):
    return pad.page.evaluate("""() => { const r = document.getElementById('ring');
        return {go: r.classList.contains('go'), full: r.classList.contains('full'),
                left: r.style.left, top: r.style.top}; }""")


def test_ring_starts_on_still_touch_and_fills_when_armed(pad):
    assert ring(pad)["go"] is False
    pad.down(CX + 33, CY - 21)
    pad.wait_class("ring", "go")
    r = ring(pad)
    assert (r["left"], r["top"]) == (f"{CX + 33}px", f"{CY - 21}px")   # drawn where the tip rests
    pad.wait_class("ring", "full")
    assert pad.has_class("pad", "armed") and pad.has_class("ring", "go")
    pad.up()
    pad.wait_class("ring", "go", False)
    r = ring(pad)
    assert not r["go"] and not r["full"]


def test_ring_not_full_before_the_pad_arms(pad):
    """`full` and `armed` appear together, never `full` first."""
    pad.page.evaluate("""() => {
        window.__bad = false;
        new MutationObserver(() => {
            if (ring.classList.contains('full') && !pad.classList.contains('armed')) window.__bad = true;
        }).observe(ring, {attributes: true});
    }""")
    pad.down(CX, CY)
    pad.wait_class("ring", "go")
    assert not pad.has_class("ring", "full")
    pad.wait_class("ring", "full")
    pad.up()
    assert pad.page.evaluate("window.__bad") is False


def test_ring_cleared_on_quick_lift(pad):
    pad.down(CX, CY)
    pad.wait_class("ring", "go")
    pad.up()
    pad.wait_class("ring", "go", False)
    assert not pad.has_class("ring", "full")


def test_ring_cleared_when_the_touch_moves(pad):
    pad.down(CX, CY)
    pad.wait_class("ring", "go")
    pad.drag(1, CX, CY, CX + 60, CY, steps=6)
    pad.wait_class("ring", "go", False)
    time.sleep(0.7)                               # well past HOLD_MS
    assert not pad.has_class("ring", "full") and not pad.has_class("pad", "armed")
    pad.up()
    assert not pad.has_class("ring", "go")


def test_ring_survives_jitter_inside_slop(pad):
    pad.down(CX, CY)
    pad.wait_class("ring", "go")
    pad.move(CX + 6, CY + 5)
    pad.move(CX - 4, CY + 2)
    assert pad.has_class("ring", "go")
    pad.wait_class("ring", "full")
    pad.up()


def test_ring_cleared_when_drag_starts_after_arming(pad):
    pad.down(CX, CY)
    pad.wait_class("ring", "full")
    pad.drag(1, CX, CY, CX + 60, CY, steps=6)
    pad.wait_class("ring", "full", False)
    assert not pad.has_class("ring", "go")
    assert pad.has_class("pad", "armed")          # the drag itself is still on
    pad.up()


def test_ring_cleared_when_second_finger_lands(pad):
    pad.down(CX - 40, CY, 1)
    pad.wait_class("ring", "go")
    pad.down(CX + 40, CY, 2)
    pad.wait_class("ring", "go", False)
    time.sleep(0.7)
    assert not pad.has_class("ring", "full") and not pad.has_class("pad", "armed")
    pad.up(2)
    pad.up(1)


def test_ring_cleared_on_pointercancel(pad):
    pad.down(CX, CY)
    pad.wait_class("ring", "full")
    pad.cancel()
    pad.wait_class("ring", "go", False)
    assert not pad.has_class("ring", "full")


def test_ring_shows_for_the_pencil_too(pad):
    pad.pen_down(CX, CY)
    pad.wait_class("ring", "go")
    pad.wait_class("ring", "full")
    pad.pen_up(CX, CY)
    pad.wait_class("ring", "go", False)


def test_ring_not_shown_for_a_corner_press(pad):
    pad.down(*corner_xy("dict"))
    time.sleep(0.7)
    assert not pad.has_class("ring", "go") and not pad.has_class("pad", "armed")
    pad.up()


def test_ring_cleared_when_connection_drops(pad):
    pad.down(CX, CY)
    pad.wait_class("ring", "full")
    pad.server.drop()
    pad.wait_offline()
    assert not pad.has_class("ring", "go") and not pad.has_class("ring", "full")
    pad.wait_online()
    pad.up()


# ------------------------------------------------------------------ boot id: reload after a restart

def loads(page):
    """Count full loads of the pad page."""
    return page.evaluate("performance.getEntriesByType('navigation')[0].type")


def test_stale_page_reloads_itself(browser, server):
    """A page carrying an old boot id (the server restarted since) reloads on connect."""
    served = []

    def stale_first(page):
        def handler(route):
            resp = route.fetch()
            body = resp.text()
            served.append(1)
            if len(served) == 1:
                body, n = re.subn(r'"boot": "[0-9a-f]+"', '"boot": "0ld0ld00"', body)
                assert n == 1
            route.fulfill(response=resp, body=body)
        page.route(server.url, handler)

    with open_pad(browser, server, route=stale_first, connected=False) as p:
        p.page.wait_for_function("CFG.boot !== '0ld0ld00'")
        p.wait_online()
        assert len(served) == 2                      # loaded, saw the new id, loaded once more
        real = json.loads(re.search(r"const CFG = (\{.*?\});", server.get()[2]).group(1))["boot"]
        assert p.page.evaluate("CFG.boot") == real
        time.sleep(1.0)
        assert len(served) == 2                      # and then stays put
        m = server.mark()
        p.tap()
        server.wait_for(lambda e: "lup" in evs(e), since=m)


def test_same_boot_id_does_not_reload(pad):
    """Reconnecting to the same server run keeps the page as it is."""
    pad.page.evaluate("window.__same = 1")
    for _ in range(2):
        pad.server.drop()
        pad.wait_offline()
        pad.wait_online()
    time.sleep(0.3)
    assert pad.page.evaluate("window.__same") == 1


def test_page_reloads_after_server_restart(browser):
    first = Server()
    second = None
    try:
        with open_pad(browser, first) as p:
            old = p.page.evaluate("CFG.boot")
            p.page.evaluate("window.__before = 1")
            first.stop()
            p.wait_offline()
            second = Server(port=first.port)
            p.page.wait_for_function("b => typeof CFG !== 'undefined' && CFG.boot !== b", arg=old,
                                     timeout=15000)
            p.wait_online(15000)
            assert p.page.evaluate("window.__before") is None        # a real reload happened
            m = second.mark()
            p.tap()
            second.wait_for(lambda e: "lup" in evs(e), since=m)
    finally:
        first.stop()
        if second:
            second.stop()


# ------------------------------------------------------------------ .local hostname switch

def test_no_local_redirect_on_loopback(pad):
    """On 127.0.0.1 the page connects where it is instead of switching to <name>.local."""
    assert pad.page.evaluate("location.hostname") == "127.0.0.1"
    assert pad.page.evaluate("location.pathname") == f"/{pad.server.url.rsplit('/', 1)[1]}"


def local_redirect_case(browser, server, ping_ok):
    """Serve the page as if opened by IP address; answer the .local ping or not."""
    ctx = browser.new_context(has_touch=True, viewport=VIEW)
    page = ctx.new_page()
    token = server.url.rsplit("/", 1)[1]
    fake = f"http://10.99.99.99:{server.port}/{token}"
    seen = {"ping": [], "local": []}

    def by_ip(route):
        route.fulfill(response=route.fetch(url=server.url))

    def anything_else(route):
        url = route.request.url
        if url.startswith("http://10.99.99.99"):
            return by_ip(route)
        if url.endswith("/ping"):
            seen["ping"].append(url)
            if ping_ok:
                return route.fulfill(response=route.fetch(url=server.url + "/ping"))
            return route.abort()
        seen["local"].append(url)
        route.fulfill(status=200, content_type="text/html", body="<title>moved</title>")

    local = json.loads(re.search(r"const CFG = (\{.*?\});", server.get()[2]).group(1))["local"]
    page.route("**/*", anything_else)
    page.goto(fake)
    return ctx, page, seen, local, token


def test_switches_to_local_hostname_when_it_answers(browser, server):
    ctx, page, seen, local, token = local_redirect_case(browser, server, ping_ok=True)
    try:
        if not local:
            pytest.skip("this Mac has no LocalHostName")
        want = f"http://{local.lower()}:{server.port}/{token}"
        page.wait_for_function("document.title === 'moved'", timeout=8000)
        assert page.url.lower() == want
        assert [u.lower() for u in seen["ping"]] == [want + "/ping"]
    finally:
        ctx.close()


def test_stays_on_ip_when_local_hostname_does_not_answer(browser, server):
    ctx, page, seen, local, token = local_redirect_case(browser, server, ping_ok=False)
    try:
        if not local:
            pytest.skip("this Mac has no LocalHostName")
        wait_until(lambda: seen["ping"], what="the .local ping")
        time.sleep(0.5)
        assert page.url == f"http://10.99.99.99:{server.port}/{token}"
        assert seen["local"] == []
    finally:
        ctx.close()
