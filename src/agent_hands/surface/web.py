"""The web surface: perceiving and acting on a live Chromium page through Playwright.

This is the only module that knows it is talking to a browser. The agent loop, the replay engine and
the artifact speak in ``TargetSpec``/``Condition``/``Action`` terms; a desktop surface (UI
Automation, AX API) would implement the same operations against an OS accessibility tree.

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
    Text,
    TextVisible,
)
from agent_hands.policy import Policy


def text_of(t: Text, params: Mapping[str, Any]) -> str:
    if isinstance(t, LiteralValue):
        return t.literal
    if isinstance(t, ParamRef):
        return str(params[t.param])
    raise TypeError(t)  # pragma: no cover


def norm(s: str) -> str:
    return " ".join((s or "").replace(" ", " ").split())


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
        kids = [c for c in f.child_frames if _frame_matches(c, ref)]
        if len(kids) != 1:
            return None
        f = kids[0]
    return f


def frames_in(page: Page, scope: FrameScope) -> list[Frame]:
    if scope == "any":
        return list(page.frames)
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


def condition_holds(page: Page, cond: Condition, params: Mapping[str, Any]) -> bool:
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
    return f"element absent: {cond.target.description}"


def _path_str(path: list[FrameRef]) -> str:
    return "top" if not path else "/".join(r.name or r.url_pattern or "?" for r in path)


def page_summary(page: Page, limit: int = 600) -> str:
    """A short what-is-on-screen description for failures: frame routes plus leading text."""
    parts = []
    for f in page.frames:
        text = norm(visible_text(f))
        parts.append(f"[{f.name or 'top'} {route_of(f.url)}] {text[:limit]}")
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

    def loading(self) -> bool:
        with self._lock:
            return any(v > 0 for v in self._inflight.values())

    def close(self) -> None:
        for closer in (self.context, self.browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        if self.pw is not None:
            self.pw.stop()
