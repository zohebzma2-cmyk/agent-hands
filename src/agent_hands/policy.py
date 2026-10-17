"""Allowlist policy and its enforcement points.

The allowlist is enforced in code, in three places, so a prompt can't talk its way past it:

1. **Network**: a context-wide route handler aborts every request to an origin or route outside the
   allowlist. That covers the agent, the replay engine, redirects, frames, popups *and* a human
   operator during handoff, because they all share the browser context.
2. **Action gate**: every action passes ``Policy.check_action`` (action kind allowed, frame URL
   allowlisted) before it touches the page.
3. **Post-action audit**: after each action every frame URL is re-checked; a frame that ended up
   somewhere unexpected is a policy violation, not something to carry on from.

New windows are not part of the model at all: popups are closed and recorded.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, Route

from agent_hands.artifact.schema import ActionKind, Model

ALWAYS_OK_URLS = ("", "about:blank", "about:srcdoc")  # "" = frame with no committed document yet


class Allowlist(Model):
    origins: list[str]  # "scheme://host:port"
    routes: list[str]  # fnmatch globs over the path, e.g. "/core/*"
    actions: list[ActionKind]


class PolicyViolation(Exception):
    def __init__(self, rule: str, detail: str) -> None:
        self.rule = rule
        self.detail = detail
        super().__init__(f"{rule}: {detail}")


@dataclass
class Decision:
    allowed: bool
    rule: str
    detail: str


def _origin(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


@dataclass
class Policy:
    allowlist: Allowlist
    blocked: list[Decision] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check_url(self, url: str) -> Decision:
        if url in ALWAYS_OK_URLS or url.startswith("javascript:"):
            return Decision(True, "url", "inert url")
        if url.startswith("chrome-error://"):  # the page Chromium shows after a request we aborted
            return Decision(True, "url", "browser error page (the request itself was blocked)")
        if _origin(url) not in self.allowlist.origins:
            return Decision(False, "origin_not_allowlisted", _origin(url))
        path = urlparse(url).path or "/"
        if not any(fnmatch(path, r) for r in self.allowlist.routes):
            return Decision(False, "route_not_allowlisted", path)
        return Decision(True, "url", path)

    def check_action(self, kind: ActionKind, frame_url: str) -> Decision:
        if kind not in self.allowlist.actions:
            return Decision(False, "action_not_allowlisted", str(kind))
        d = self.check_url(frame_url)
        if not d.allowed:
            return d
        return Decision(True, "action", f"{kind} on {urlparse(frame_url).path}")

    def require_action(self, kind: ActionKind, frame_url: str) -> None:
        d = self.check_action(kind, frame_url)
        if not d.allowed:
            self._record(d)
            raise PolicyViolation(d.rule, d.detail)

    def audit_frames(self, page: Page) -> None:
        for f in page.frames:
            d = self.check_url(f.url)
            if not d.allowed:
                self._record(d)
                raise PolicyViolation(d.rule, f"frame '{f.name or 'top'}' at {d.detail}")

    def _record(self, d: Decision) -> None:
        with self._lock:
            self.blocked.append(d)

    # -- network enforcement --------------------------------------------------

    def install(self, context: BrowserContext) -> None:
        def guard(route: Route) -> None:
            d = self.check_url(route.request.url)
            if d.allowed:
                route.continue_()
            else:
                self._record(d)
                route.abort("blockedbyclient")

        context.route("**/*", guard)

        pages: list[Page] = []

        def on_page(p: Page) -> None:
            pages.append(p)
            if len(pages) > 1:  # the first page is ours; anything after is a popup
                self._record(Decision(False, "popup_blocked", p.url or "about:blank"))
                p.close()

        context.on("page", on_page)


class CommitGuard:
    """A network-level backstop for the approval gate.

    Any document POST whose form action the profile classifies as irreversible is aborted unless an
    approval has armed the guard for exactly one such request. It doesn't depend on which element was
    clicked, so a click on a cell, a <b> inside a <button>, or a javascript: link that submits the
    form can't commit without approval. A human holding the session lease is the exception: their
    actions are theirs (and are captured).
    """

    def __init__(self, classify: Callable[[str], bool], human_holds: Callable[[], bool]) -> None:
        self._irreversible = classify  # path -> bool
        self._human_holds = human_holds
        self._armed = 0
        self.blocked: list[str] = []
        self.passed: list[str] = []

    def arm(self) -> None:
        self._armed = 1

    def disarm(self) -> None:
        self._armed = 0

    def install(self, context: BrowserContext) -> None:
        def guard(route: Route) -> None:
            req = route.request
            path = urlparse(req.url).path
            if req.method == "POST" and req.resource_type == "document" and self._irreversible(path):
                if self._human_holds():
                    self.passed.append(f"human:{path}")
                elif self._armed > 0:
                    self._armed -= 1
                    self.passed.append(path)
                else:
                    self.blocked.append(path)
                    route.abort("blockedbyclient")
                    return
            route.fallback()

        context.route("**/*", guard)  # registered after the allowlist route, so it runs first
