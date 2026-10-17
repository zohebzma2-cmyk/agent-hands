"""Deterministic target resolution: TargetSpec → exactly one element, or a precise reason why not.

Rules (the same at record time and at replay):

- Resolution happens inside the spec's frame path; a missing frame is "not found".
- Strategies are tried in their recorded order. A strategy counts only if it matches exactly one
  *visible* element. Zero matches and several matches are both misses; there is no "first match".
- Stop as soon as ``min_agreement`` strategies have matched uniquely; those must all be the
  same element, or the result is a disagreement (never act on a coin flip).
- Every attempt is reported, so the caller can see which strategy won and treat anything below the
  primary as drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from playwright.sync_api import ElementHandle, Frame, Page

from agent_hands.artifact.schema import (
    CssStrategy,
    LabelStrategy,
    NearTextStrategy,
    RoleStrategy,
    Strategy,
    TargetSpec,
    TextStrategy,
    XPathStrategy,
)
from agent_hands.replay.result import FailureKind, Resolution, StrategyAttempt
from agent_hands.surface.web import find_frame, norm, text_of

# Same-row lookup for table layouts. Runs inside the target frame.
_NEAR_TEXT_JS = """
({anchor, match, role, column}) => {
  const norm = s => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
  const hit = t => match === 'exact' ? norm(t) === anchor : norm(t).includes(anchor);
  const sel = {
    textbox: 'input:not([type]),input[type=text],input[type=password],input[type=email],input[type=number],input[type=tel],input[type=search],textarea',
    combobox: 'select', listbox: 'select[multiple]',
    button: 'button,input[type=button],input[type=submit],input[type=reset]',
    link: 'a[href]', checkbox: 'input[type=checkbox]', radio: 'input[type=radio]',
  }[role];
  const cellsOf = tr => Array.from(tr.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
  const shown = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const out = [];
  for (const tr of document.querySelectorAll('tr')) {
    const cells = cellsOf(tr);
    const a = cells.findIndex(c => hit(c.textContent));
    if (a < 0) continue;
    if (role === 'cell') {
      let idx = a + 1;
      if (column !== null) {
        const table = tr.closest('table');
        const head = table && table.rows.length ? cellsOf(table.rows[0]) : [];
        idx = head.findIndex(c => norm(c.textContent) === column);
        if (idx < 0 || idx === a) continue;
      }
      if (cells[idx] && shown(cells[idx])) out.push(cells[idx]);
    } else if (sel) {
      // nearest cell to the anchor that holds a matching control: following cells first, then preceding
      const order = cells.map((_, i) => i).filter(i => i !== a)
        .sort((x, y) => (x > a ? x - a : 100 + a - x) - (y > a ? y - a : 100 + a - y));
      for (const i of order) {
        const els = Array.from(cells[i].querySelectorAll(sel)).filter(shown);
        if (els.length) { out.push(...els); break; }
      }
    }
  }
  return out;
}
"""


# Text match on rendered text. "exact": the OUTERMOST element whose text equals the target (the
# cell, not the <font><u> inside it). "contains": the INNERMOST elements containing it.
_TEXT_JS = """
({text, match}) => {
  const norm = s => (s || '').replace(/\\u00a0/g, ' ').replace(/\\s+/g, ' ').trim();
  const hit = el => match === 'exact' ? norm(el.innerText) === text : norm(el.innerText).includes(text);
  const out = [];
  for (const el of document.body ? document.body.querySelectorAll('*') : []) {
    if (['SCRIPT', 'STYLE', 'OPTION', 'SELECT', 'TEXTAREA'].includes(el.tagName) || !hit(el)) continue;
    if (match === 'exact') {
      if (!el.parentElement || el.parentElement === document.body || !hit(el.parentElement)) out.push(el);
    } else if (!Array.from(el.children).some(hit)) {
      out.push(el);
    }
  }
  return out;
}
"""


@dataclass
class Resolved:
    handle: ElementHandle | None
    frame: Frame | None
    resolution: Resolution
    failure: FailureKind | None = None
    detail: str = ""


def _visible(handles: list[ElementHandle]) -> list[ElementHandle]:
    out = []
    for h in handles:
        try:
            if h.is_visible():
                out.append(h)
        except Exception:  # detached mid-check: not a candidate
            continue
    return out


def candidates(frame: Frame, strat: Strategy, params: Mapping[str, Any]) -> list[ElementHandle]:
    """All visible elements a single strategy matches in ``frame``."""
    if isinstance(strat, RoleStrategy):
        loc = frame.get_by_role(strat.role, name=text_of(strat.name, params), exact=strat.match == "exact")  # type: ignore[arg-type]
    elif isinstance(strat, LabelStrategy):
        loc = frame.get_by_label(text_of(strat.label, params), exact=strat.match == "exact")
    elif isinstance(strat, TextStrategy):
        return _js_candidates(frame, _TEXT_JS, {"text": norm(text_of(strat.text, params)), "match": strat.match})
    elif isinstance(strat, CssStrategy):
        loc = frame.locator(strat.selector)
    elif isinstance(strat, XPathStrategy):
        loc = frame.locator(f"xpath={strat.xpath}")
    elif isinstance(strat, NearTextStrategy):
        return _js_candidates(
            frame,
            _NEAR_TEXT_JS,
            {
                "anchor": norm(text_of(strat.anchor, params)),
                "match": strat.match,
                "role": strat.role,
                "column": None if strat.column is None else norm(text_of(strat.column, params)),
            },
        )
    else:  # pragma: no cover
        raise TypeError(strat)
    return _visible(loc.element_handles())


def _js_candidates(frame: Frame, js: str, arg: dict[str, Any]) -> list[ElementHandle]:
    arr = frame.evaluate_handle(js, arg)
    props = arr.get_properties()
    handles = [p.as_element() for _, p in sorted(props.items(), key=lambda kv: int(kv[0]))]
    arr.dispose()
    return _visible([h for h in handles if h is not None])


def same_element(a: ElementHandle, b: ElementHandle) -> bool:
    return bool(a.evaluate("(x, y) => x === y", b))


def resolve(page: Page, spec: TargetSpec, params: Mapping[str, Any]) -> Resolved:
    frame = find_frame(page, spec.frame)
    attempts: list[StrategyAttempt] = []

    def done(handle, failure=None, detail="", used=None, agreed=()):
        for i in range(len(attempts), len(spec.strategies)):
            attempts.append(StrategyAttempt(index=i, kind=spec.strategies[i].kind, outcome="not_tried"))
        return Resolved(
            handle,
            frame,
            Resolution(
                description=spec.description,
                used_index=used,
                used_kind=None if used is None else spec.strategies[used].kind,
                attempts=attempts,
                agreed=list(agreed),
            ),
            failure,
            detail,
        )

    if frame is None:
        return done(None, FailureKind.TARGET_NOT_FOUND, "frame path did not resolve")

    found: list[tuple[int, ElementHandle]] = []
    ambiguous = False
    for i, strat in enumerate(spec.strategies):
        try:
            hs = candidates(frame, strat, params)
        except Exception:  # frame navigated mid-query; a miss this tick
            hs = []
        if len(hs) == 1:
            outcome = "unique"
            found.append((i, hs[0]))
        else:
            outcome = "none" if not hs else "ambiguous"
            ambiguous = ambiguous or bool(hs)
        attempts.append(StrategyAttempt(index=i, kind=strat.kind, outcome=outcome))  # type: ignore[arg-type]
        if len(found) >= spec.min_agreement:
            break

    if len(found) < spec.min_agreement:
        kind = FailureKind.TARGET_AMBIGUOUS if ambiguous and not found else FailureKind.TARGET_NOT_FOUND
        detail = f"{len(found)} of {spec.min_agreement} required strategies matched uniquely"
        return done(None, kind, detail)

    first_i, first = found[0]
    for i, h in found[1:]:
        if not same_element(first, h):
            attempts[i] = StrategyAttempt(index=i, kind=spec.strategies[i].kind, outcome="disagree")
            return done(None, FailureKind.TARGET_DISAGREEMENT, f"strategy {first_i} and {i} point at different elements")
    return done(first, used=first_i, agreed=[i for i, _ in found])


# --- pre-action hit test ----------------------------------------------------

_OBSCURED_JS = """
el => {
  el.scrollIntoView({block: 'center', inline: 'center'});
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  if (!hit || hit === el || el.contains(hit) || hit.contains(el)) return null;
  const layers = Array.from(document.querySelectorAll('body *')).filter(n => {
    const s = getComputedStyle(n);
    return (s.position === 'absolute' || s.position === 'fixed') && s.display !== 'none' &&
           s.visibility !== 'hidden' && parseInt(s.zIndex || '0', 10) > 0 && n.innerText.trim();
  });
  const text = layers.map(n => n.innerText.trim()).join(' | ') || (hit.innerText || hit.tagName);
  return text.replace(/\\s+/g, ' ').slice(0, 300);
}
"""


def obscured_by(handle: ElementHandle) -> str | None:
    """If something covers the element's centre, describe it; otherwise None."""
    try:
        return handle.evaluate(_OBSCURED_JS)
    except Exception:
        return None
