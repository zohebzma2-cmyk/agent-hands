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
