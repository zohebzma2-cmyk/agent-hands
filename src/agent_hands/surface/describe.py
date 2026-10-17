"""Record-time target description: one live element → an ordered, validated set of strategies.

The model points at an element by snapshot ref. That ref is meaningless next week, so we describe
the element every way that might still work, in order of how well each survives change:

    role+name → label → near_text (same table row / column) → own text → CSS → positional XPath

and keep only strategies that resolve, right now, to exactly this element through the same
resolver replay will use. Literal text equal to a param value becomes a ``{"param": ...}``
reference. For ``read`` steps, strategies keyed on the element's own text are dropped: that text is
the data being read, and it will be different next time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import ElementHandle, Page

from agent_hands.artifact.schema import (
    CssStrategy,
    LabelStrategy,
    LiteralValue,
    NearTextStrategy,
    ParamRef,
    RoleStrategy,
    Strategy,
    Text,
    TextStrategy,
    XPathStrategy,
)
from agent_hands.surface.locate import candidates, same_element
from agent_hands.surface.web import frame_path

NEAR_ROLES = {"textbox", "combobox", "listbox", "button", "link", "checkbox", "radio", "cell"}
NAMELESS_ROLES = {"generic", "text", "none", "presentation", "rowgroup", "row", "table", "iframe"}

_INFO_JS = r"""
el => {
  const norm = s => (s || '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();
  const cellsOf = tr => Array.from(tr.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
  const tag = el.tagName.toLowerCase();
  const cell = (tag === 'td' || tag === 'th') ? el : el.closest('td,th');
  const tr = cell ? cell.parentElement : null;
  const cells = tr ? cellsOf(tr) : [];
  const me = cell ? cells.indexOf(cell) : -1;
  const interactive = 'input:not([type=hidden]),select,textarea,button,a[href]';
  let anchors = cells.map((c, i) => ({i, text: norm(c.textContent), live: !!c.querySelector(interactive)}))
    .filter(a => a.i !== me && a.text && !a.live);
  if (el === cell) {
    anchors = anchors.filter(a => a.i === 0);         // a data cell is named by its row label
  } else {
    anchors.sort((a, b) => (a.i < me ? me - a.i : 100 + a.i - me) - (b.i < me ? me - b.i : 100 + b.i - me));
  }
  let column = null;
  if (el === cell && tr) {
    const table = tr.closest('table');
    const head = table && table.rows.length ? cellsOf(table.rows[0]) : [];
    if (table.rows[0] !== tr && head[me]) column = norm(head[me].textContent) || null;
  }
  const formish = ['input', 'select', 'textarea'].includes(tag);
  const shownText = formish ? '' : norm(el.innerText);
  const own = shownText.length <= 80 ? shownText : '';
  const label = (el.labels && el.labels.length) ? norm(el.labels[0].textContent) : (el.getAttribute('aria-label') || null);
  const cssPath = e => {
    const nm = e.getAttribute && e.getAttribute('name');
    if (nm) {
      const s = `${e.tagName.toLowerCase()}[name="${nm}"]`;
      if (document.querySelectorAll(s).length === 1) return s;
    }
    const parts = [];
    for (let n = e; n && n.nodeType === 1 && n !== document.documentElement; n = n.parentElement) {
      const sibs = Array.from(n.parentElement ? n.parentElement.children : []).filter(s => s.tagName === n.tagName);
      parts.unshift(sibs.length > 1 ? `${n.tagName.toLowerCase()}:nth-of-type(${sibs.indexOf(n) + 1})` : n.tagName.toLowerCase());
    }
    return parts.join(' > ');
  };
  const xpath = e => {
    const parts = [];
    for (let n = e; n && n.nodeType === 1; n = n.parentElement) {
      const sibs = Array.from(n.parentElement ? n.parentElement.children : []).filter(s => s.tagName === n.tagName);
      parts.unshift(`${n.tagName.toLowerCase()}[${sibs.indexOf(n) + 1}]`);
    }
    return '/' + parts.join('/');
  };
  const type = (el.getAttribute('type') || '').toLowerCase();
  const form = el.form || el.closest('form');
  const isSubmit = (tag === 'button' && (type === '' || type === 'submit')) || (tag === 'input' && (type === 'submit' || type === 'image'));
  const jsSubmit = /\.submit\(/.test((el.getAttribute('onclick') || '') + ' ' + (el.getAttribute('href') || ''))
    || !!(el.querySelector && el.querySelector('input[type=submit],input[type=image],button:not([type=button])'))
    || !!(el.closest && el.closest('button:not([type=button])'));
  const method = form ? (form.getAttribute('method') || 'get').toLowerCase() : null;
  const action = form ? new URL(form.getAttribute('action') || location.href, location.href).pathname : null;
  return {
    tag, type, own, label, column,
    anchors: anchors.slice(0, 2).map(a => a.text),
    css: cssPath(el), xpath: xpath(el),
    is_post_submit: !!form && method === 'post' && (isSubmit || jsSubmit),
    form_action: (isSubmit || jsSubmit) ? action : null,
    sensitive_input: tag === 'input' && type === 'password',
  };
}
"""

_SNAP_LINE = re.compile(r'^(?P<role>[A-Za-z]+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?')


def role_and_name(snapshot: str, ref: str) -> tuple[str, str | None]:
    """Find ``ref`` in an AI-mode ARIA snapshot and return its role and accessible name."""
    tag = f"[ref={ref}]"
    for line in snapshot.splitlines():
        if tag not in line:
            continue
        s = line.strip()
        s = s[2:] if s.startswith("- ") else s
        if s.startswith("'"):
            s = s[1:]
        m = _SNAP_LINE.match(s)
        if m:
            name = m.group("name")
            return m.group("role"), (name.replace('\\"', '"') if name is not None else None)
    raise LookupError(f"ref {ref} not in snapshot")


@dataclass
class Described:
    strategies: list[Strategy]
    rejected: list[tuple[str, str]]  # (kind, why) for evidence
    role: str
    name: str | None
    frame_path: list
    info: dict[str, Any] = field(default_factory=dict)


def _text(value: str, params: Mapping[str, Any]) -> Text:
    for k, v in params.items():
        if str(v) == value:
            return ParamRef(param=k)
    return LiteralValue(literal=value)


def describe(
    page: Page, handle: ElementHandle, role: str, name: str | None, params: Mapping[str, Any], *, reading: bool
) -> Described:
    frame = handle.owner_frame()
    assert frame is not None
    info = handle.evaluate(_INFO_JS)
    proposed: list[Strategy] = []
    if name and role not in NAMELESS_ROLES and not reading:
        proposed.append(RoleStrategy(kind="role", role=role, name=_text(name, params)))
    if info["label"]:
        proposed.append(LabelStrategy(kind="label", label=_text(info["label"], params)))
    if role in NEAR_ROLES:
        for anchor in info["anchors"]:
            column = _text(info["column"], params) if role == "cell" and info["column"] else None
            if role == "cell" and column is None:
                continue
            proposed.append(NearTextStrategy(kind="near_text", anchor=_text(anchor, params), role=role, column=column))
    if info["own"] and not reading:
        proposed.append(TextStrategy(kind="text", text=_text(info["own"], params)))
    proposed.append(CssStrategy(kind="css", selector=info["css"]))
    proposed.append(XPathStrategy(kind="xpath", xpath=info["xpath"]))

    kept: list[Strategy] = []
    rejected: list[tuple[str, str]] = []
    for s in proposed:
        try:
            hs = candidates(frame, s, params)
        except Exception as e:  # a malformed selector is just a rejected strategy
            rejected.append((s.kind, f"error {type(e).__name__}"))
            continue
        if len(hs) != 1:
            rejected.append((s.kind, "no match" if not hs else f"{len(hs)} matches"))
        elif not same_element(hs[0], handle):
            rejected.append((s.kind, "matches a different element"))
        elif s in kept:
            rejected.append((s.kind, "duplicate"))
        else:
            kept.append(s)
    return Described(kept, rejected, role, name, frame_path(frame), info)
