"""Perceive/act layer against the live hostile app: resolution, description, hit test, policy."""

from __future__ import annotations

import pytest
from browser_helpers import allowlist_for, login, main_goto

import cu_sim
from agent_hands.artifact.schema import (
    CssStrategy,
    LiteralValue,
    NearTextStrategy,
    ParamRef,
    RoleStrategy,
    TargetSpec,
    TextVisible,
)
from agent_hands.policy import Policy
from agent_hands.replay.result import FailureKind
from agent_hands.surface.describe import describe, role_and_name
from agent_hands.surface.locate import obscured_by, resolve
from agent_hands.surface.web import BrowserSession, condition_holds

pytestmark = pytest.mark.browser

MAIN = [{"name": "main"}]


@pytest.fixture(scope="module")
def sim():
    s = cu_sim.start()
    yield s
    s.stop()


@pytest.fixture
def session(sim):
    sim.state.reset()
    bs = BrowserSession(policy=Policy(allowlist_for(sim.url)))
    page = bs.start()
    login(page, sim.url)
    yield bs
    bs.close()


def spec(*strategies, min_agreement=1, frame=MAIN):
    return TargetSpec.model_validate(
        {"description": "t", "frame": frame, "strategies": list(strategies), "min_agreement": min_agreement}
    )


def lit(s):
    return {"literal": s}


def test_near_text_finds_the_unnamed_textbox(session):
    r = resolve(session.page, spec({"kind": "near_text", "anchor": lit("Member #:"), "role": "textbox"}), {})
    assert r.handle is not None
    assert r.handle.get_attribute("name") == "f1"
    assert r.resolution.used_index == 0


def test_nameless_role_is_ambiguous_not_first_match(session):
    r = resolve(session.page, spec({"kind": "css", "selector": "input[type=text]"}), {})
    assert r.handle is None
    assert r.failure == FailureKind.TARGET_AMBIGUOUS
    assert r.resolution.attempts[0].outcome == "ambiguous"


def test_fallback_is_recorded(session):
    r = resolve(
        session.page,
        spec(
            {"kind": "role", "role": "button", "name": lit("Search")},  # not there: drifted label
            {"kind": "role", "role": "button", "name": lit("Find")},
        ),
        {},
    )
    assert r.handle is not None
    assert r.resolution.used_index == 1
    assert [a.outcome for a in r.resolution.attempts] == ["none", "unique"]


def test_frame_path_scopes_resolution(session):
    r = resolve(session.page, spec({"kind": "role", "role": "button", "name": lit("Find")}, frame=[{"name": "nav"}]), {})
    assert r.failure == FailureKind.TARGET_NOT_FOUND


def test_agreement_catches_strategies_pointing_at_different_elements(session):
    r = resolve(
        session.page,
        spec(
            {"kind": "near_text", "anchor": lit("Member #:"), "role": "textbox"},
            {"kind": "css", "selector": "input[name=f2]"},
            min_agreement=2,
        ),
        {},
    )
    assert r.failure == FailureKind.TARGET_DISAGREEMENT
    assert r.handle is None


def test_agreement_passes_when_strategies_agree(session):
    r = resolve(
        session.page,
        spec(
            {"kind": "near_text", "anchor": lit("Member #:"), "role": "textbox"},
            {"kind": "css", "selector": "input[name=f1]"},
            min_agreement=2,
        ),
        {},
    )
    assert r.handle is not None and r.resolution.agreed == [0, 1]


def test_cell_name_includes_input_values_so_role_is_ambiguous(session, sim):
    # The search box still holds 900003, and a cell's accessible name includes its input's value.
    main_goto(session.page, sim.url, "/core/search.do?f1=900003")
    r = resolve(session.page, spec({"kind": "role", "role": "cell", "name": {"param": "member_id"}}), {"member_id": "900003"})
    assert r.failure == FailureKind.TARGET_AMBIGUOUS


def test_param_in_text_locator_finds_the_outermost_cell(session, sim):
    main_goto(session.page, sim.url, "/core/search.do?f1=900003")
    r = resolve(session.page, spec({"kind": "text", "text": {"param": "member_id"}}), {"member_id": "900003"})
    assert r.handle is not None
    assert r.handle.evaluate("e => e.tagName") == "TD"


def test_describe_unnamed_textbox(session):
    page = session.page
    snap = page.aria_snapshot(mode="ai")
    ref = next(line.split("[ref=")[1].split("]")[0] for line in snap.splitlines() if "- textbox [ref=" in line)
    handle = page.locator(f"aria-ref={ref}").element_handle()
    role, name = role_and_name(snap, ref)
    d = describe(page, handle, role, name, {}, reading=False)
    kinds = [s.kind for s in d.strategies]
    assert role == "textbox" and name is None
    assert kinds[0] == "near_text" and "role" not in kinds
    assert isinstance(d.strategies[0], NearTextStrategy)
    assert d.strategies[0].anchor == LiteralValue(literal="Member #:")
    assert any(isinstance(s, CssStrategy) and s.selector == 'input[name="f1"]' for s in d.strategies)
    assert [r.name for r in d.frame_path] == ["main"]


def test_describe_parameterises_member_cell(session, sim):
    page = session.page
    main_goto(page, sim.url, "/core/search.do?f1=900001")
    snap = page.aria_snapshot(mode="ai")
    ref = next(line.split("[ref=")[1].split("]")[0] for line in snap.splitlines() if 'cell "900001"' in line)
    handle = page.locator(f"aria-ref={ref}").element_handle()
    role, name = role_and_name(snap, ref)
    d = describe(page, handle, role, name, {"member_id": "900001"}, reading=False)
    texts = [s for s in d.strategies if s.kind == "text"]
    assert texts and texts[0].text == ParamRef(param="member_id")
    assert ("role", "2 matches") in d.rejected  # the search box's cell shares the name


def test_describe_read_cell_uses_row_and_column_not_the_value(session, sim):
    page = session.page
    main_goto(page, sim.url, "/core/member.do?mid=900001")
    snap = page.aria_snapshot(mode="ai")
    ref = next(line.split("[ref=")[1].split("]")[0] for line in snap.splitlines() if '"$812.40"' in line)
    handle = page.locator(f"aria-ref={ref}").element_handle()
    role, name = role_and_name(snap, ref)
    d = describe(page, handle, role, name, {}, reading=True)
    first = d.strategies[0]
    assert isinstance(first, NearTextStrategy)
    assert first.anchor == LiteralValue(literal="Base Share") and first.column == LiteralValue(literal="Balance")
    assert all("812" not in s.model_dump_json() for s in d.strategies)


def test_hit_test_reports_the_overlay(session, sim):
    sim.set_faults({"announcement": {"mode": "always"}})
    page = session.page
    main_goto(page, sim.url, "/core/member.do?mid=900001")
    r = resolve(page, spec({"kind": "role", "role": "button", "name": lit("Open Sub-Account")}), {})
    assert r.handle is not None
    assert "System Announcement" in (obscured_by(r.handle) or "")


def test_hit_test_clear_when_nothing_covers(session):
    r = resolve(session.page, spec({"kind": "role", "role": "button", "name": lit("Find")}), {})
    assert obscured_by(r.handle) is None


def test_text_visible_any_frame(session, sim):
    sim.set_faults({"http500": {"mode": "once"}})
    main_goto(session.page, sim.url, "/core/member.do?mid=900001")
    cond = TextVisible.model_validate({"kind": "text_visible", "frame": "any", "text": lit("HTTP Status 500")})
    assert condition_holds(session.page, cond, {})


def test_network_allowlist_blocks_off_origin(session):
    page = session.page
    with pytest.raises(Exception):
        page.goto("http://example.invalid/")
    assert any(d.rule == "origin_not_allowlisted" for d in session.policy.blocked)


def test_network_allowlist_blocks_off_route(session, sim):
    main = session.page.frame(name="main")
    with pytest.raises(Exception):
        main.goto(sim.url + "/admin/console")
    assert any(d.rule == "route_not_allowlisted" for d in session.policy.blocked)


def test_popups_are_closed(session):
    main = session.page.frame(name="main")
    main.evaluate("() => window.open('/core/search.do')")
    session.page.wait_for_timeout(300)
    assert len(session.context.pages) == 1
    assert any(d.rule == "popup_blocked" for d in session.policy.blocked)
