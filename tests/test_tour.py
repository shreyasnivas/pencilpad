"""The first-run guided tour, in a real browser with real touch input.

The tour starts by itself the first time the pad connects. Each step waits for
one gesture; clicks, drags and corner keys made while it runs are practice and
must not reach the Mac, while pointer moves and scrolling do.
"""

import time

import pytest

from conftest import CONNECTED, ONLINE, VIEW, corner_xy, evs, open_pad

STEPS = ["move", "tap", "hold", "drag", "scroll", "corners", "home"]
TITLES = {
    "move": "Drag to move the pointer", "tap": "Tap to click",
    "hold": "Hold, then lift to right-click", "drag": "Hold, then slide to drag",
    "scroll": "Two fingers to scroll", "corners": "The corners are keys",
    "home": "Add PencilPad to your Home Screen",
}
# what a practice gesture would have posted had it been sent to the Mac
PRACTICE = ("ldown", "lup", "rdown", "rup", "drag", "keydown", "keyup", "flags", "screencapture")
# a clear part of the pad: left of the tour card, between the Escape and Dictate corners
FX, FY = 200, 410
STANDALONE = "Object.defineProperty(Navigator.prototype, 'standalone', {get: () => true});"


def settle(pad, mark, wait=0.4):
    time.sleep(wait)
    return pad.server.log(mark)


def stored(pad):
    return pad.page.evaluate("localStorage.getItem('pencilpad-tour')")


def step_label(pad):
    return pad.text("tourStep")


def dots(pad):
    return pad.page.evaluate("[...document.querySelectorAll('#dots i')].map(i => i.className)")


def card_visible(pad):
    return pad.page.evaluate("getComputedStyle(document.getElementById('tour')).visibility") == "visible"


def skip_to(pad, name):
    """Press Skip until the tour is on step `name`."""
    for _ in STEPS:
        cur = pad.tour_step()
        if cur == name:
            return
        nxt = STEPS[STEPS.index(cur) + 1]
        pad.press("tourSkip")
        pad.wait_step(nxt)
    assert pad.tour_step() == name


# ---- the gesture each step asks for

def slide_until_done(pad, fingers):
    """Slide the given contacts up and down until the step shows its tick."""
    for i, (x, y) in fingers.items():
        pad.down(x, y, i)
    deadline = time.time() + 8
    n = 0
    while not pad.step_done():
        assert time.time() < deadline, "step never completed"
        n += 1
        off = abs((n * 8) % 240 - 120) - 120          # 0 .. -120 .. 0, 8 px a step
        for i, (x, y) in fingers.items():
            pad.move(x, y + off, i)
        time.sleep(0.016)
    for i in list(fingers)[::-1]:
        pad.up(i)


def do_move(pad):
    slide_until_done(pad, {1: (FX, FY + 60)})


def do_tap(pad):
    pad.tap(FX, FY)


def do_hold(pad):
    pad.down(FX, FY)
    pad.wait_armed()
    pad.up()


def do_drag(pad):
    pad.down(FX - 60, FY)
    pad.wait_armed()
    pad.drag(1, FX - 60, FY, FX + 80, FY, steps=10)
    pad.up()


def do_scroll(pad):
    slide_until_done(pad, {1: (FX - 50, FY + 60), 2: (FX + 50, FY + 60)})


def do_corner(pad, name="esc"):
    pad.tap(*corner_xy(name))


GESTURE = {"move": do_move, "tap": do_tap, "hold": do_hold, "drag": do_drag,
           "scroll": do_scroll, "corners": do_corner}


# ------------------------------------------------------------------ starting

def test_tour_starts_on_first_connect(first_run):
    p = first_run
    assert p.touring() and p.tour_step() == "move"
    assert card_visible(p)
    assert step_label(p) == "Step 1 of 7"
    assert p.text("tourTitle") == TITLES["move"]
    assert p.text("tourNote") == "Watch the pointer on your Mac."
    assert p.text("tourSkip") == "Skip" and p.text("tourEnd") == "End Tour"
    assert dots(p) == ["cur"] + [""] * 6
    assert stored(p) is None                      # not marked as seen until it ends


def test_tour_does_not_start_once_seen(pad):
    assert not pad.touring()
    assert not card_visible(pad)
    assert pad.tour_step() is None


def test_tour_card_does_not_cover_the_pad(first_run):
    """The card is see-through to touches except for its two buttons."""
    p = first_run
    got = p.page.evaluate("""() => {
        const r = document.getElementById('tour').getBoundingClientRect();
        const at = (x, y) => document.elementFromPoint(x, y).id;
        return [at(r.x + r.width / 2, r.y + 60), at(%d, %d)];
    }""" % (FX, FY))
    assert got == ["pad", "pad"]


def test_unfinished_tour_starts_again_after_reload(first_run):
    p = first_run
    skip_to(p, "hold")
    p.page.reload()
    p.page.wait_for_function(ONLINE)
    p.wait_step("move")
    assert step_label(p) == "Step 1 of 7"


def test_tour_keeps_its_place_across_a_reconnect(first_run):
    p = first_run
    skip_to(p, "hold")
    p.server.drop()
    p.wait_offline()
    p.page.wait_for_function("getComputedStyle(document.getElementById('tour')).visibility === 'hidden'")
    p.wait_online()
    assert p.tour_step() == "hold" and step_label(p) == "Step 3 of 7"
    assert card_visible(p)


# ------------------------------------------------------------------ each step

@pytest.mark.parametrize("step", STEPS[:-1])
def test_step_completes_on_its_gesture_and_advances(first_run, step):
    p = first_run
    skip_to(p, step)
    i = STEPS.index(step)
    assert p.text("tourTitle") == TITLES[step]
    assert step_label(p) == f"Step {i + 1} of 7"
    m = p.server.mark()
    GESTURE[step](p)
    p.wait_step_done()
    assert p.text("tourTitle") == "Nice"
    p.wait_step(STEPS[i + 1])
    assert step_label(p) == f"Step {i + 2} of 7"
    assert p.text("tourTitle") == TITLES[STEPS[i + 1]]
    d = dots(p)
    assert d[i] == "ok" and d[i + 1] == "cur"
    e = settle(p, m)
    # practice: nothing but pointer travel and scrolling reaches the Mac
    assert evs(e, *PRACTICE) == [], e
    if step == "move":
        assert len(evs(e, "move")) >= 10 and set(evs(e, "move", "scroll")) == {"move"}
    if step == "scroll":
        assert len(evs(e, "scroll")) >= 4
    if step == "drag":
        assert "move" in evs(e)                    # the pointer still travels, button up


@pytest.mark.parametrize("step", STEPS[:-1])
def test_step_note_says_what_reaches_the_mac(first_run, step):
    p = first_run
    skip_to(p, step)
    want = ("Watch the pointer on your Mac." if step in ("move", "scroll")
            else "Practice only: this is not sent to your Mac.")
    assert p.text("tourNote") == want


def test_wrong_gesture_does_not_complete_a_step(first_run):
    p = first_run
    m = p.server.mark()
    do_tap(p)                                      # step 1 wants a move, not a tap
    do_corner(p, "sub")
    time.sleep(0.3)
    assert p.tour_step() == "move" and not p.step_done()
    skip_to(p, "tap")
    p.down(FX, FY + 60)                            # step 2 wants a tap, not a move
    p.drag(1, FX, FY + 60, FX, FY - 60, steps=12)
    p.up()
    time.sleep(0.3)
    assert p.tour_step() == "tap" and not p.step_done()
    assert evs(settle(p, m), *PRACTICE) == []


def test_move_step_needs_more_than_a_flick(first_run):
    p = first_run
    p.down(FX, FY)
    p.drag(1, FX, FY, FX + 40, FY, steps=3)        # at most 3 move messages; it wants 10
    p.up()
    time.sleep(0.3)
    assert p.tour_step() == "move" and not p.step_done()


@pytest.mark.parametrize("corner", ["esc", "shot", "dict", "sub"])
def test_any_corner_completes_the_corners_step(first_run, corner):
    p = first_run
    skip_to(p, "corners")
    m = p.server.mark()
    do_corner(p, corner)
    p.wait_step_done()
    p.wait_step("home")
    assert evs(settle(p, m, 0.6), *PRACTICE) == []


def test_two_finger_tap_also_completes_the_hold_step(first_run):
    """Step 3 is about right-clicking; the two-finger tap is the other way to do it."""
    p = first_run
    skip_to(p, "hold")
    m = p.server.mark()
    p.down(FX - 40, FY, 1)
    p.down(FX + 40, FY, 2)
    time.sleep(0.05)
    p.up(2)
    p.up(1)
    p.wait_step_done()
    assert evs(settle(p, m), *PRACTICE) == []


def test_practice_gestures_record_nothing_on_the_server(first_run):
    """Every click-like gesture and every corner, while the tour runs."""
    p = first_run
    m = p.server.mark()
    do_tap(p)
    do_tap(p)
    do_hold(p)
    do_drag(p)
    p.down(FX - 40, FY, 1)                          # two-finger tap
    p.down(FX + 40, FY, 2)
    time.sleep(0.05)
    p.up(2)
    p.up(1)
    p.pen_down(FX, FY)                             # Pencil tap
    time.sleep(0.05)
    p.pen_up(FX, FY)
    for c in ("esc", "sub", "shot"):
        do_corner(p, c)
    p.down(*corner_xy("dict"))                     # Dictate, held then let go
    p.wait_class("dict", "on")
    time.sleep(0.2)
    p.up()
    p.wait_class("dict", "on", False)
    assert p.touring()
    e = settle(p, m, 0.8)
    assert evs(e, *PRACTICE) == [], e
    assert "move" in evs(e)                        # the slide during the practice drag


def test_hold_feedback_still_shows_during_practice(first_run):
    p = first_run
    skip_to(p, "hold")
    p.down(FX, FY)
    p.wait_class("ring", "go")
    p.wait_armed()
    assert p.has_class("ring", "full")
    assert p.text("statusText") == "Lift to right-click · move to drag"
    p.up()
    p.wait_class("pad", "armed", False)
    assert not p.has_class("ring", "go")


# ------------------------------------------------------------------ buttons

def test_skip_moves_on_without_ticking_the_step(first_run):
    p = first_run
    m = p.server.mark()
    p.press("tourSkip")
    p.wait_step("tap")
    assert step_label(p) == "Step 2 of 7"
    assert dots(p)[:2] == ["", "cur"]
    time.sleep(1.2)                                # no delayed second advance
    assert p.tour_step() == "tap"
    assert evs(settle(p, m), *PRACTICE, "move") == []


def test_last_step_is_manual_and_done_finishes(first_run):
    p = first_run
    skip_to(p, "home")
    assert step_label(p) == "Step 7 of 7"
    assert p.text("tourTitle") == TITLES["home"]
    assert p.text("tourSkip") == "Done" and p.has_class("tourSkip", "primary")
    assert p.text("tourNote") == ""
    do_tap(p)                                      # no gesture completes this step
    do_corner(p, "esc")
    time.sleep(0.3)
    assert p.tour_step() == "home" and not p.step_done()
    assert stored(p) is None
    p.press("tourSkip")
    p.wait_step(None)
    assert stored(p) == "1"


def test_skip_button_label_resets_after_reopening(first_run):
    p = first_run
    skip_to(p, "home")
    p.press("tourSkip")
    p.wait_step(None)
    p.press("help")
    p.wait_step("move")
    assert p.text("tourSkip") == "Skip" and not p.has_class("tourSkip", "primary")


def test_end_tour_button(first_run):
    p = first_run
    skip_to(p, "tap")
    m = p.server.mark()
    p.press("tourEnd")
    p.wait_step(None)
    assert stored(p) == "1"
    p.page.wait_for_function("getComputedStyle(document.getElementById('tour')).visibility === 'hidden'")
    assert evs(settle(p, m), *PRACTICE, "move") == []      # pressing the button is not a click
    # and from here gestures are real again
    m = p.server.mark()
    do_tap(p)
    e = p.server.wait_for(lambda e: "lup" in evs(e), since=m)
    assert evs(e, *PRACTICE) == ["ldown", "lup"]
    do_corner(p, "esc")
    p.server.wait_for(lambda e: "keyup" in evs(e), since=m)


def test_help_button_reopens_the_tour(first_run):
    p = first_run
    skip_to(p, "drag")
    p.press("tourEnd")
    p.wait_step(None)
    m = p.server.mark()
    p.press("help")
    p.wait_step("move")                            # from the beginning
    assert step_label(p) == "Step 1 of 7" and dots(p) == ["cur"] + [""] * 6
    assert card_visible(p)
    do_tap(p)                                      # practice again: not sent
    assert evs(settle(p, m), *PRACTICE, "move") == []


def test_help_button_opens_the_tour_for_a_returning_user(pad):
    m = pad.server.mark()
    pad.press("help")
    pad.wait_step("move")
    pad.press("help")                              # and pressing it again closes it
    pad.wait_step(None)
    assert evs(settle(pad, m), *PRACTICE, "move") == []
    do_tap(pad)
    pad.server.wait_for(lambda e: "lup" in evs(e), since=m)


def test_help_button_works_with_the_pencil(pad):
    x, y = pad.center("help")
    pad.pen_down(x, y)
    pad.pen_up(x, y)
    pad.wait_step("move")


def test_opening_the_tour_releases_a_held_dictate_key(pad):
    """Dictate held for real, then the tour opens: the key must not stay down."""
    s = pad.server
    m = s.mark()
    pad.down(*corner_xy("dict"), id=1)
    s.wait_for(lambda e: "keydown" in evs(e), since=m)
    pad.press("help", id=2)
    pad.wait_step("move")
    e = s.wait_for(lambda e: "keyup" in evs(e), since=m)
    assert [(x["ev"], x["key"]) for x in e if x["ev"] in PRACTICE] == [("keydown", "f13"), ("keyup", "f13")]
    assert not pad.has_class("dict", "on")
    pad.up(1)
    assert evs(settle(pad, m), "keydown", "keyup") == ["keydown", "keyup"]


def test_opening_the_tour_releases_a_drag(pad):
    s = pad.server
    m = s.mark()
    pad.down(FX, FY, id=1)
    pad.wait_armed()
    pad.drag(1, FX, FY, FX + 80, FY)
    s.wait_for(lambda e: "drag" in evs(e), since=m)
    pad.press("help", id=2)
    pad.wait_step("move")
    s.wait_for(lambda e: "lup" in evs(e), since=m)
    pad.up(1)
    e = settle(pad, m)
    assert evs(e, "ldown", "lup") == ["ldown", "lup"]


# ------------------------------------------------------------------ finishing

def test_whole_tour_by_gestures_then_it_stays_away(first_run):
    p = first_run
    m = p.server.mark()
    for i, step in enumerate(STEPS[:-1]):
        p.wait_step(step)
        GESTURE[step](p)
        p.wait_step_done()
    p.wait_step("home")
    assert dots(p) == ["ok"] * 6 + ["cur"]
    assert stored(p) is None
    p.press("tourSkip")                            # "Done"
    p.wait_step(None)
    assert stored(p) == "1"
    e = settle(p, m)
    assert evs(e, *PRACTICE) == [], e
    assert "move" in evs(e) and "scroll" in evs(e)
    # a reconnect does not bring it back
    p.server.drop()
    p.wait_offline()
    p.wait_online()
    assert not p.touring()
    # nor does opening the page again
    p.page.reload()
    p.page.wait_for_function(ONLINE)
    time.sleep(0.3)
    assert not p.touring() and stored(p) == "1"
    m = p.server.mark()
    do_tap(p)
    p.server.wait_for(lambda e: "lup" in evs(e), since=m)


def test_home_screen_app_has_no_home_screen_step(browser, server):
    """Opened from the Home Screen (navigator.standalone) the tour has six steps
    and ends by itself after the corners step."""
    with open_pad(browser, server, tour_seen=False, init=[STANDALONE]) as p:
        assert step_label(p) == "Step 1 of 6"
        skip_to(p, "corners")
        assert step_label(p) == "Step 6 of 6"
        do_corner(p, "sub")
        p.wait_step_done()
        p.wait_step(None)
        assert stored(p) == "1"


# ------------------------------------------------------------------ suspected product bugs

def test_skip_while_nice_is_showing_advances_only_one_step(first_run):
    p = first_run
    skip_to(p, "tap")
    do_tap(p)
    p.wait_step_done()
    p.press("tourSkip")                            # impatient: do not wait for the auto-advance
    p.wait_step("hold")
    time.sleep(1.3)
    assert p.tour_step() == "hold"


def test_reopened_tour_is_not_advanced_by_a_stale_timer(first_run):
    p = first_run
    skip_to(p, "tap")
    do_tap(p)
    p.wait_step_done()
    p.press("tourEnd")
    p.wait_step(None)
    p.press("help")
    p.wait_step("move")
    time.sleep(1.3)
    assert p.tour_step() == "move"


def test_ending_tour_with_dictate_held_sends_no_stray_key(first_run):
    p = first_run
    m = p.server.mark()
    p.down(*corner_xy("dict"), id=1)               # practice press: nothing sent
    p.wait_class("dict", "on")
    p.press("tourEnd", id=2)
    p.wait_step(None)
    p.up(1)
    e = settle(p, m)
    assert evs(e, "keydown", "keyup", "flags") == [], e
