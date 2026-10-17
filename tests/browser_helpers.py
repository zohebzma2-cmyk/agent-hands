"""Shared helpers for tests that drive Chromium against cu_sim."""

from __future__ import annotations

from urllib.parse import urlparse

from playwright.sync_api import Page

from agent_hands.artifact.schema import ActionKind
from agent_hands.policy import Allowlist, Policy


def allowlist_for(url: str) -> Allowlist:
    u = urlparse(url)
    return Allowlist(origins=[f"{u.scheme}://{u.netloc}"], routes=["/", "/core/*"], actions=list(ActionKind))


def login(page: Page, base: str) -> None:
    """Test setup only: get to the frameset without going through the engine."""
    page.goto(base + "/core/login.do")
    page.locator("input[name=uid]").fill("teller01")
    page.locator("input[name=pwd]").fill("synthetic-teller-pass")
    page.locator("input[type=submit]").click()
    page.wait_for_url("**/frameset.do")
    main = page.frame(name="main")
    assert main is not None
    main.wait_for_load_state()
    main.locator("input[name=f1]").wait_for()


def main_goto(page: Page, base: str, route: str) -> None:
    main = page.frame(name="main")
    assert main is not None
    main.goto(base + route)


__all__ = ["Policy", "allowlist_for", "login", "main_goto"]
