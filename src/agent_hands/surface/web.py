"""The web surface: perceiving and acting on a live Chromium page through Playwright.

This module does the perceiving and acting: observation, frame lookup, condition checks, and the
browser session. The agent loop, the replay engine and the artifact speak in
``TargetSpec``/``Condition``/``Action`` terms, but they still call Playwright directly for
navigation, waits, frame lookup and the commit guard. A desktop surface (UI Automation, AX API)
means moving those behind the same protocol first.

Perception uses Playwright's AI-mode ARIA snapshot: the page's accessibility tree, across every
frame, with element refs. Its role vocabulary is the same one ``get_by_role`` resolves against, so
what the model saw and what replay resolves agree by construction.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Frame, Page, Playwright, sync_playwright

from agent_hands.artifact.schema import (
    Condition,
    ElementAbsent,
    ElementPresent,
    FrameRef,
    FrameScope,
    LiteralValue,
    ParamRef,
    RouteMatches,
    SecretValue,
    Text,
    TextVisible,
    Value,
    ValueEquals,
)
from agent_hands.policy import Policy


def text_of(t: Text, params: Mapping[str, Any]) -> str:
    if isinstance(t, LiteralValue):
        return t.literal
    if isinstance(t, ParamRef):
        return str(params[t.param])
    raise TypeError(t)  # pragma: no cover


def value_of(v: Value, params: Mapping[str, Any], secrets: Mapping[str, str]) -> str:
    if isinstance(v, SecretValue):
        return secrets[v.secret]
    return text_of(v, params)


def control_value(handle: Any) -> str:
    """What a control currently holds: an input's value, or a select's chosen option label."""
    return handle.evaluate(
        "e => e.tagName === 'SELECT' ? ((e.selectedOptions[0] || {}).text || '') : (e.value || '')"
    )


def norm(s: str) -> str:
    return " ".join((s or "").split())  # str.split() already treats NBSP as whitespace


def route_of(url: str) -> str:
    u = urlparse(url)
    return u.path + (f"?{u.query}" if u.query else "")


# --- frames -----------------------------------------------------------------


def _frame_matches(f: Frame, ref: FrameRef) -> bool:
    if ref.name is not None and f.name != ref.name:
        return False
    if ref.url_pattern is not None and not fnmatch(route_of(f.url), ref.url_pattern):
        return False
    return True


def find_frame(page: Page, path: list[FrameRef]) -> Frame | None:
    """Walk a frame path from the top document. Each hop must match exactly one child frame."""
    f = page.main_frame
    for ref in path:
        # Playwright keeps detached frames in child_frames after a frame-busting top navigation.
        kids = [c for c in f.child_frames if not c.is_detached() and _frame_matches(c, ref)]
        if len(kids) != 1:
            return None
        f = kids[0]
    return f


def frames_in(page: Page, scope: FrameScope) -> list[Frame]:
    if scope == "any":
        return [f for f in page.frames if not f.is_detached()]
    f = find_frame(page, scope)
    return [f] if f is not None else []


def frame_path(frame: Frame) -> list[FrameRef]:
    """The path a TargetSpec should record for ``frame``: names where available, else routes."""
    hops: list[FrameRef] = []
    f: Frame | None = frame
    while f is not None and f.parent_frame is not None:
        if f.name:
            hops.append(FrameRef(name=f.name))
        else:
            hops.append(FrameRef(url_pattern=urlparse(f.url).path + "*"))
        f = f.parent_frame
    return list(reversed(hops))


def visible_text(frame: Frame) -> str:
    """The frame's rendered text (``innerText`` skips hidden elements). Empty if mid-navigation."""
    try:
        return frame.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:  # frame navigating or detached: treat as no text this tick
        return ""


# --- conditions -------------------------------------------------------------


def condition_holds(
    page: Page, cond: Condition, params: Mapping[str, Any], secrets: Mapping[str, str] | None = None
) -> bool:
    from agent_hands.surface.locate import resolve  # local import: locate depends on this module

    if isinstance(cond, TextVisible):
        want = norm(text_of(cond.text, params))
        for f in frames_in(page, cond.frame):
            body = visible_text(f)
            if cond.match == "contains" and want in norm(body):
                return True
            if cond.match == "exact" and any(norm(line) == want for line in body.splitlines()):
                return True
        return False
    if isinstance(cond, RouteMatches):
        return any(fnmatch(route_of(f.url), cond.pattern) for f in frames_in(page, cond.frame))
    if isinstance(cond, ElementPresent):
        return resolve(page, cond.target, params).handle is not None
    if isinstance(cond, ElementAbsent):
        return resolve(page, cond.target, params).handle is None
    if isinstance(cond, ValueEquals):
        r = resolve(page, cond.target, params)
        if r.handle is None:
            return False
        try:
            return norm(control_value(r.handle)) == norm(value_of(cond.value, params, secrets or {}))
        except Exception:
            return False
    raise TypeError(cond)  # pragma: no cover


def describe_condition(cond: Condition) -> str:
    if isinstance(cond, TextVisible):
        where = "any frame" if cond.frame == "any" else (_path_str(cond.frame))
        t = cond.text.literal if isinstance(cond.text, LiteralValue) else f"{{{cond.text.param}}}"
        return f"text {cond.match} '{t}' visible in {where}"
    if isinstance(cond, RouteMatches):
        where = "any frame" if cond.frame == "any" else _path_str(cond.frame)
        return f"{where} route matches {cond.pattern}"
    if isinstance(cond, ElementPresent):
        return f"element present: {cond.target.description}"
    if isinstance(cond, ValueEquals):
        v = cond.value
        if isinstance(v, LiteralValue):
            shown = v.literal
        else:
            shown = f"{{{v.param}}}" if isinstance(v, ParamRef) else "{secret}"
        return f"{cond.target.description} holds {shown}"
    return f"element absent: {cond.target.description}"


def _path_str(path: list[FrameRef]) -> str:
    return "top" if not path else "/".join(r.name or r.url_pattern or "?" for r in path)


def page_summary(page: Page, limit: int = 600) -> str:
    """A short what-is-on-screen description for failures: frame routes plus leading text."""
    from agent_hands.redact import mask_cells

    parts = []
    for f in page.frames:
        if f.is_detached():
            continue
        text = norm(mask_cells(visible_text(f)))  # structural masking before anything else
        parts.append(f"[{f.name or 'top'} {route_of(f.url).split('?')[0]}] {text[:limit]}")
    return " | ".join(parts)


# --- browser session --------------------------------------------------------


@dataclass
class BrowserSession:
    """One browser context and page, guarded by the policy from the first request.

    Tracks in-flight document loads per frame so the replay engine can tell a slow page
    (transient) apart from a page that loaded and is simply wrong (checkpoint failure).
    """

    policy: Policy
    headed: bool = False
    viewport: tuple[int, int] = (1280, 860)
    pw: Playwright | None = None
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    _inflight: dict[Any, int] = field(default_factory=dict)
    _pending: dict[Any, tuple[str, str]] = field(default_factory=dict)  # frame → (method, url)
    _last: dict[Any, str] = field(default_factory=dict)  # frame → method of its last document request
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def start(self) -> Page:
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=not self.headed)
        self.context = self.browser.new_context(
            viewport={"width": self.viewport[0], "height": self.viewport[1]},
            locale="en-US",
            timezone_id="America/New_York",
        )
        self.policy.install(self.context)
        self.page = self.context.new_page()
        self.page.on("request", lambda r: self._track(r, +1))
        self.page.on("requestfinished", lambda r: self._track(r, -1))
        self.page.on("requestfailed", lambda r: self._track(r, -1))
        return self.page

    def _track(self, request: Any, delta: int) -> None:
        if request.resource_type != "document":
            return
        with self._lock:
            key = request.frame
            self._inflight[key] = max(0, self._inflight.get(key, 0) + delta)
            if delta > 0:
                self._pending[key] = (request.method, request.url)
                self._last[key] = request.method
            elif self._inflight[key] == 0:
                self._pending.pop(key, None)

    def loading(self) -> bool:
        with self._lock:
            return any(v > 0 for v in self._inflight.values())

    def pending_document(self) -> tuple[Frame, str, str] | None:
        """(frame, method, url) of a document load still in flight, if any."""
        with self._lock:
            loading = [(f, m, u) for f, (m, u) in self._pending.items() if self._inflight.get(f, 0) > 0]
        if not loading:
            return None
        return sorted(loading, key=lambda x: (x[0].name, x[2]))[0]  # by frame name, not arrival order

    def last_method(self, frame: Frame) -> str | None:
        with self._lock:
            return self._last.get(frame)

    def close(self) -> None:
        for closer in (self.context, self.browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        if self.pw is not None:
            self.pw.stop()
