"""Layout by measurement, in the iPad sizes PencilPad meets, rail open and folded, each
side, in each state of the page: every target on screen, big enough, unclipped, not
overlapping another, and every card over the trackpad rather than the rail."""

import time

import pytest

from conftest import open_pad

VIEWS = [(1180, 820), (820, 1180), (1024, 768), (1133, 744), (744, 1133)]
NAME = "Someone’s MacBook Pro with a long name (2)"

MEASURE = r"""
() => {
  const W = innerWidth, H = innerHeight, out = [];
  const vis = el => { for (let p = el; p; p = p.parentElement) { const c = getComputedStyle(p);
      if (c.display === 'none' || c.visibility === 'hidden' || +c.opacity === 0) return false; } return true; };
  const R = el => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, r: r.right, b: r.bottom, w: r.width, h: r.height}; };
  const name = el => el.id ? '#' + el.id : el.dataset.k ? 'cap:' + el.dataset.k : el.className;
  const all = sel => [...document.querySelectorAll(sel)].filter(vis).filter(el => R(el).w > 0);
  // the keys and buttons themselves (#status and the round buttons are 36px; their touch area is 44).
  // Folded, the handle's touch area is the whole strip, #rail.
  const folded = document.body.classList.contains('collapsed');
  const targets = all('#rail .key, #keyboard .cap[data-k], #status, #help, #gear, ' + (folded ? '#rail' : '#railToggle'));
  for (const el of targets) {
    const r = R(el), cap = el.classList.contains('cap'), round = ['status', 'help', 'gear'].includes(el.id);
    if (r.x < -0.5 || r.y < -0.5 || r.r > W + 0.5 || r.b > H + 0.5) out.push(['offscreen', name(el)]);
    // keyboard keys may be narrower than 44px in portrait; they are never shorter
    if (!round && (r.h < 44 - 0.01 || (!cap && r.w < 44 - 0.01))) out.push(['small', name(el), r.w + 'x' + r.h]);
    if (!round && (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1)) out.push(['clipped', name(el)]);
  }
  for (let i = 0; i < targets.length; i++) for (let j = i + 1; j < targets.length; j++) {
    const a = R(targets[i]), b = R(targets[j]);
    if (targets[i].contains(targets[j]) || targets[j].contains(targets[i])) continue;
    if (Math.min(a.r, b.r) - Math.max(a.x, b.x) > 0.5 && Math.min(a.b, b.b) - Math.max(a.y, b.y) > 0.5)
      out.push(['overlap', name(targets[i]), name(targets[j])]);
  }
  const rail = R(document.getElementById('rail')), right = document.body.classList.contains('right');
  for (const id of ['top', 'macs', 'lost', 'tour', 'keyboard', 'sheet']) {
    const el = document.getElementById(id); if (!vis(el)) continue;
    const r = R(el);
    if (right ? r.r > rail.x + 0.5 : r.x < rail.r - 0.5) out.push(['over-rail', '#' + id]);
    if (r.r > W + 0.5 || r.x < -0.5 || r.b > H + 0.5 || r.y < -0.5) out.push(['offscreen', '#' + id]);
  }
  if (document.body.classList.contains('typing')) {
    const kb = R(document.getElementById('keyboard')), top = R(document.getElementById('top'));
    if (kb.y < top.b + 40) out.push(['no-band', top.b, kb.y]);
  }
  return out;
}
"""


def states(p):
    yield "plain"
    p.page.evaluate("kbdShow(true)")
    p.page.wait_for_function("document.body.classList.contains('typing')")
    time.sleep(0.35)
    yield "keyboard"
    p.page.evaluate("n => { cur.name = n; showStatus(false); }", NAME)
    yield "keyboard+longname"
    p.page.evaluate("outdated('Windows')")
    yield "keyboard+longname+notice"
    p.page.evaluate("kbdShow(false); sheetOpen()")
    p.page.wait_for_function("getComputedStyle(document.getElementById('sheet')).opacity === '1'")
    yield "settings"
    p.page.evaluate("sheetClose(); pickOpen()")
    p.page.wait_for_function("getComputedStyle(document.getElementById('macs')).opacity === '1'")
    yield "maclist"
    p.page.evaluate("pickClose(); tourStart()")
    p.wait_step("move")
    time.sleep(0.3)
    yield "tour"
    p.page.evaluate("tourEnd(); setOnline(false)")
    p.page.wait_for_function("getComputedStyle(document.getElementById('lost')).opacity === '1'")
    yield "offline"
    p.page.evaluate("setOnline(true)")


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize("folded", [False, True], ids=["open", "folded"])
@pytest.mark.parametrize("wh", VIEWS, ids=[f"{w}x{h}" for w, h in VIEWS])
def test_layout(browser, server, wh, folded, side):
    w, h = wh
    init = ["try { localStorage.setItem('pencilpad-side', '%s'); %s } catch (_) {}" % (
        side, "localStorage.setItem('pencilpad-rail', 'collapsed');" if folded else "")]
    problems = []
    with open_pad(browser, server, init=init, view={"width": w, "height": h}) as p:
        time.sleep(0.3)
        for st in states(p):
            problems += [(st, *prob) for prob in p.page.evaluate(MEASURE)]
    assert problems == []
