"""Replay engine against the live hostile app: happy path, fault matrix, approvals, determinism,
the no-LLM guarantee, and human handoff on the same live session (scripted human)."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from browser_helpers import allowlist_for

import cu_sim
from agent_hands.artifact.schema import CapabilityArtifact
from agent_hands.handoff.operator import ScriptedOperator
from agent_hands.policy import Policy
from agent_hands.profile import load_profile
from agent_hands.replay.engine import ReplayConfig, ReplayEngine, approve
from agent_hands.replay.result import Failed, FailureKind, NeedsHuman, Outcome, Succeeded
from agent_hands.surface.web import BrowserSession

pytestmark = pytest.mark.browser

FIXTURE = Path(__file__).parent / "fixtures" / "open_sub_account.json"
PARAMS = {"member_id": "900001", "account_type": "Savings", "nickname": "Rainy day", "initial_deposit": "25.00"}


@pytest.fixture(scope="module")
def sim():
    s = cu_sim.start()
    yield s
    s.stop()


@pytest.fixture
def artifact() -> CapabilityArtifact:
    return CapabilityArtifact.model_validate_json(FIXTURE.read_text())


def replay(sim, artifact, *, faults=None, params=PARAMS, approved=True, operator=None, run_dir=None, run_id="t"):
    sim.state.reset()
    if faults:
        sim.set_faults(faults)
    session = BrowserSession(policy=Policy(allowlist_for(sim.url)))
    session.start()
    try:
        approvals = [approve(artifact, "s13", params, "pytest", run_id=run_id)] if approved else []
        cfg = ReplayConfig(run_id=run_id, origin=sim.url, approvals=approvals)
        engine = ReplayEngine(artifact, load_profile("cu_core"), session, cfg, operator=operator, run_dir=run_dir)
        return engine.run(params), engine
    finally:
        session.close()


# --- happy path -------------------------------------------------------------


def test_happy_path(sim, artifact, tmp_path):
    result, engine = replay(sim, artifact, run_dir=tmp_path)
    assert isinstance(result, Succeeded), result
    assert result.outputs["share_balance"] == Decimal("812.40")
    assert result.outputs["confirmation_number"] == "SA-70001"
    assert [s.step_id for s in result.steps] == [f"s{i:02d}" for i in range(1, 15)]
    assert result.drift == []
    assert result.approvals[0].channel == "caller_token" and result.approvals[0].step_id == "s13"
    assert len(sim.state.snapshot()["subaccounts"]) == 1


SENSITIVE = ("900001", "812.40", "synthetic-teller-pass", "teller01", "SAMPLE, ADA", "ADA SAMPLE",
             "Example Way", "04/12/1961", "555-0101")


def _all_text(d):
    return "".join(p.read_text() for p in d.rglob("*") if p.suffix in (".json", ".jsonl"))


def test_evidence_is_redacted(sim, artifact, tmp_path):
    result, _ = replay(sim, artifact, run_dir=tmp_path)
    assert isinstance(result, Succeeded)
    everything = _all_text(tmp_path)
    for raw in SENSITIVE:
        assert raw not in everything, raw
    persisted = json.loads((tmp_path / "result.json").read_text())
    assert persisted["outputs"]["share_balance"] == "[REDACTED]"
    assert persisted["outputs"]["confirmation_number"] == "SA-70001"
    assert persisted["params"]["member_id"] == "[PII:member_id]"


def _uncovered(page, masks, needles):
    """Leaf elements showing a sensitive string that no mask covers."""
    from agent_hands.evidence import mask_locators  # noqa: F401  (masks come from it)

    bad = []
    for f in page.frames:
        if f.is_detached():
            continue
        handles = [h for loc in masks for h in loc.element_handles() if h.owner_frame() == f]
        found = f.evaluate_handle(
            """(needles) => Array.from(document.querySelectorAll('body *')).filter(el =>
                 Array.from(el.childNodes).some(n => n.nodeType === 3 && needles.some(x => n.textContent.includes(x))))""",
            list(needles),
        )
        for _, prop in found.get_properties().items():
            el = prop.as_element()
            if el and not any(h.evaluate("(m, c) => m.contains(c)", el) for h in handles):
                bad.append((f.name, el.evaluate("e => e.outerHTML.slice(0, 80)")))
    return bad


@pytest.mark.parametrize("route", ["/core/member.do?mid=900001", "/core/subacct.do?mid=900001", "/core/search.do?f1=900001"])
def test_screenshot_masks_cover_every_sensitive_value(sim, artifact, route):
    from agent_hands.evidence import mask_locators
    from browser_helpers import login

    sim.state.reset()
    session = BrowserSession(policy=Policy(allowlist_for(sim.url)))
    page = session.start()
    try:
        login(page, sim.url)
        page.frame(name="main").goto(sim.url + route)
        masks = mask_locators(page, ["900001", "teller01"])
        assert _uncovered(page, masks, SENSITIVE) == []
    finally:
        session.close()


def test_confirmation_screenshot_masks(sim, artifact, tmp_path):
    from agent_hands.evidence import mask_locators

    result, engine = replay(sim, artifact)
    assert isinstance(result, Succeeded)
    # replay() closed its browser; redo the flow's end state with the same masks
    from browser_helpers import login

    session = BrowserSession(policy=Policy(allowlist_for(sim.url)))
    page = session.start()
    try:
        login(page, sim.url)
        page.frame(name="main").goto(sim.url + "/core/confirm.do?c=SA-70001")
        assert _uncovered(page, mask_locators(page, ["900001", "25.00", "teller01"]), SENSITIVE + ("$25.00",)) == []
    finally:
        session.close()


# --- business outcomes --------------------------------------------------------


def test_unknown_member_is_an_outcome_not_a_failure(sim, artifact):
    result, _ = replay(sim, artifact, params={**PARAMS, "member_id": "999999"})
    assert isinstance(result, Outcome)
    assert result.code == "member_not_found" and result.at_step == "s05"
    assert "CL-1404" in result.message


@pytest.mark.parametrize(
    "fault, code, step",
    [
        ("member_not_found", "member_not_found", "s05"),
        ("permission_denied", "permission_denied", "s08"),
        ("validation_error", "validation_rejected", "s13"),
    ],
)
def test_business_outcomes(sim, artifact, fault, code, step):
    result, _ = replay(sim, artifact, faults={fault: {"mode": "always"}})
    assert isinstance(result, Outcome), result
    assert (result.code, result.at_step) == (code, step)
    assert sim.state.snapshot()["subaccounts"] == []


def test_real_validation_is_caught_before_the_browser(sim, artifact):
    result, _ = replay(sim, artifact, params={**PARAMS, "initial_deposit": "1.00"}, approved=False)
    assert isinstance(result, Failed)
    assert result.failure.kind == FailureKind.PARAM_INVALID and result.steps == []


# --- recoverable conditions -------------------------------------------------


@pytest.mark.parametrize(
    "fault, condition, action",
    [
        ({"announcement": {"mode": "once"}}, "cu_core.announcement@1", "dismissed"),
        ({"http500": {"mode": "once"}}, "cu_core.server_error@1", "retried"),
        ({"session_expired": {"mode": "once"}}, "cu_core.session_expired@1", "re_login"),
        ({"slow": {"mode": "once", "ms": 9000}}, "engine.slow_load", "retried"),
    ],
)
def test_recoverable_conditions_are_handled_and_reported(sim, artifact, fault, condition, action):
    result, _ = replay(sim, artifact, faults=fault)
    assert isinstance(result, Succeeded), result
    assert any(r.condition == condition and r.action == action for r in result.recoveries), result.recoveries
    assert len(sim.state.snapshot()["subaccounts"]) == 1  # exactly one account, even after re-login


def test_short_slow_load_is_just_waited_for(sim, artifact):
    result, _ = replay(sim, artifact, faults={"slow": {"mode": "once", "ms": 3500}})
    assert isinstance(result, Succeeded)


# --- hard failures ------------------------------------------------------------


def test_persistent_500_exhausts_budget(sim, artifact):
    result, _ = replay(sim, artifact, faults={"http500": {"mode": "always"}})
    assert isinstance(result, Failed)
    assert result.failure.kind == FailureKind.APP_ERROR and result.failure.retryable
    assert result.failure.step_id == "s06"
    assert sum(r.condition == "cu_core.server_error@1" for r in result.recoveries) == 2


def test_persistent_slowness_times_out(sim, artifact):
    result, _ = replay(sim, artifact, faults={"slow": {"mode": "always", "ms": 9000}})
    assert isinstance(result, Failed)
    assert result.failure.kind == FailureKind.TIMEOUT and result.failure.step_id == "s06"


def test_drifted_locator_fails_at_the_exact_step_with_expected_and_observed(sim, artifact):
    data = json.loads(artifact.canonical_json())
    data["steps"][7]["target"]["strategies"] = [
        {"kind": "role", "role": "button", "name": {"literal": "Open Subaccount"}, "match": "exact"}
    ]
    drifted = CapabilityArtifact.model_validate(data)
    result, _ = replay(sim, drifted)
    assert isinstance(result, Failed)
    f = result.failure
    assert f.kind == FailureKind.TARGET_NOT_FOUND and f.step_id == "s08"
    assert "Open Sub-Account button" in f.expected and "Member Detail" in f.observed


def test_fallback_locator_succeeds_and_reports_drift(sim, artifact):
    data = json.loads(artifact.canonical_json())
    data["steps"][7]["target"]["strategies"][0]["name"] = {"literal": "Open Subaccount"}  # primary drifted
    result, _ = replay(sim, CapabilityArtifact.model_validate(data))
    assert isinstance(result, Succeeded)
    assert [(d.step_id, d.used_kind, d.primary_outcome) for d in result.drift] == [("s08", "css", "none")]


# --- the commit boundary ------------------------------------------------------


def test_irreversible_step_stops_without_approval(sim, artifact):
    result, _ = replay(sim, artifact, approved=False)
    assert isinstance(result, NeedsHuman)
    assert result.reason == "approval_required" and result.step_id == "s13"
    assert sim.state.snapshot()["subaccounts"] == []


def test_approval_is_bound_to_params(sim, artifact):
    sim.state.reset()
    other = approve(artifact, "s13", {**PARAMS, "initial_deposit": "4000.00"}, "pytest", run_id="t")
    session = BrowserSession(policy=Policy(allowlist_for(sim.url)))
    session.start()
    try:
        cfg = ReplayConfig(run_id="t", origin=sim.url, approvals=[other])
        result = ReplayEngine(artifact, load_profile("cu_core"), session, cfg).run(PARAMS)
    finally:
        session.close()
    assert isinstance(result, NeedsHuman) and result.reason == "approval_required"


def test_500_after_commit_is_never_resubmitted(sim, artifact):
    result, _ = replay(sim, artifact, faults={"http500_after_commit": {"mode": "always"}})
    assert isinstance(result, NeedsHuman)
    assert result.reason == "commit_state_unknown" and result.step_id == "s13"
    assert len(sim.state.snapshot()["subaccounts"]) == 1  # committed once, not twice


def test_unknown_dialog_needs_a_human_when_unattended(sim, artifact):
    result, _ = replay(sim, artifact, faults={"unknown_dialog": {"mode": "always"}})
    assert isinstance(result, NeedsHuman)
    assert result.reason == "unexpected_state" and result.step_id == "s07"
    assert result.intervention_id


def test_session_expiry_after_submit_is_commit_state_unknown(sim, artifact):
    result, _ = replay(sim, artifact, faults={"session_expired": {"mode": "once", "at": "/core/confirm.do"}})
    assert isinstance(result, NeedsHuman) and result.reason == "commit_state_unknown"
    assert result.commit_state == "in_doubt"
    assert len(sim.state.snapshot()["subaccounts"]) == 1


def test_500_on_confirmation_is_never_retried_into_a_second_commit(sim, artifact):
    result, _ = replay(sim, artifact, faults={"http500": {"mode": "always", "at": "/core/confirm.do"}})
    assert isinstance(result, NeedsHuman) and result.reason == "commit_state_unknown"
    assert len(sim.state.snapshot()["subaccounts"]) == 1


def test_happy_path_reports_committed(sim, artifact):
    result, _ = replay(sim, artifact)
    assert result.commit_state == "committed"


def test_approval_for_another_run_is_refused(sim, artifact):
    sim.state.reset()
    other_run = approve(artifact, "s13", PARAMS, "pytest", run_id="some-other-run")
    session = BrowserSession(policy=Policy(allowlist_for(sim.url)))
    session.start()
    try:
        cfg = ReplayConfig(run_id="t", origin=sim.url, approvals=[other_run])
        result = ReplayEngine(artifact, load_profile("cu_core"), session, cfg).run(PARAMS)
    finally:
        session.close()
    assert isinstance(result, NeedsHuman) and result.reason == "approval_required"


def test_draft_artifacts_dont_replay(sim, artifact):
    data = json.loads(artifact.canonical_json())
    data["capability"]["status"] = "draft"
    result, _ = replay(sim, CapabilityArtifact.model_validate(data))
    assert isinstance(result, Failed) and result.failure.kind == FailureKind.ARTIFACT_INCOMPATIBLE


# --- handoff on the same live session ------------------------------------------


def supervisor(page):
    """The scripted 'human': resolves the supervisor override in the live page."""
    main = page.frame(name="main")
    main.locator("input[name=pin]").fill(cu_sim.app.SUPERVISOR_PIN)
    main.locator("input[value='Approve Override']").click()
    main.wait_for_load_state()


def test_handoff_resolves_unknown_dialog_and_resumes(sim, artifact, tmp_path):
    op = ScriptedOperator(supervisor)
    result, engine = replay(sim, artifact, faults={"unknown_dialog": {"mode": "once"}}, operator=op, run_dir=tmp_path)
    assert isinstance(result, Succeeded), result
    h = result.handoffs[0]
    assert (h.step_id, h.reason, h.resolution, h.verified) == ("s07", "unexpected_state", "resumed", True)
    assert h.human_actions >= 2  # the PIN change and the submit were captured
    owners = [e.to.value for e in engine.lease.log]
    assert owners == ["awaiting_human", "human", "verifying", "automation"]
    assert result.outputs["share_balance"] == Decimal("812.40")  # read by automation after resume
    actions = json.loads(next((tmp_path / "handoff").glob("*-human_actions.json")).read_text())
    assert any("[MASKED]" == a["value"] for a in actions)  # the PIN was never captured
    assert cu_sim.app.SUPERVISOR_PIN not in json.dumps(actions)
    assert (tmp_path / "handoff" / f"{h.intervention_id}.json").exists()
    everything = _all_text(tmp_path)
    for raw in SENSITIVE:
        assert raw not in everything, raw


def test_human_who_leaves_a_different_member_on_screen_fails_verification(sim, artifact):
    def wrong_member(page):
        if page.frame(name="main").locator("input[name=pin]").count():
            supervisor(page)
        page.frame(name="main").goto(page.frame(name="main").url.split("/core/")[0] + "/core/member.do?mid=900002")

    result, _ = replay(sim, artifact, faults={"unknown_dialog": {"mode": "once"}}, operator=ScriptedOperator(wrong_member))
    assert isinstance(result, Failed) and result.failure.kind == FailureKind.UNEXPECTED_STATE
    assert "checkpoint no longer holds" in result.handoffs[0].verification
    assert sim.state.snapshot()["subaccounts"] == []


def test_automation_cannot_act_while_human_holds_the_lease(sim, artifact):
    seen = {}

    def human_tries_to_let_automation_act(page):
        eng = engine_ref["e"]
        try:
            eng._goto_entry()  # a real engine action path, mid-handoff
        except Exception as e:
            seen["blocked"] = type(e).__name__
        supervisor(page)

    engine_ref: dict = {}
    op = ScriptedOperator(human_tries_to_let_automation_act)
    orig = ReplayEngine.__init__

    def spy(self, *a, **k):
        orig(self, *a, **k)
        engine_ref["e"] = self

    ReplayEngine.__init__ = spy  # type: ignore[method-assign]
    try:
        result, _ = replay(sim, artifact, faults={"unknown_dialog": {"mode": "once"}}, operator=op)
    finally:
        ReplayEngine.__init__ = orig  # type: ignore[method-assign]
    assert isinstance(result, Succeeded)
    assert seen["blocked"] == "LeaseError"


def test_operator_abort_is_reported(sim, artifact):
    op = ScriptedOperator(None, resolution="aborted")
    result, _ = replay(sim, artifact, faults={"unknown_dialog": {"mode": "always"}}, operator=op)
    assert isinstance(result, Failed) and result.failure.kind == FailureKind.OPERATOR_ABORTED


def test_human_who_fixes_nothing_fails_verification(sim, artifact):
    op = ScriptedOperator(lambda page: None)
    result, _ = replay(sim, artifact, faults={"unknown_dialog": {"mode": "always"}}, operator=op)
    assert isinstance(result, Failed)
    assert result.failure.kind == FailureKind.UNEXPECTED_STATE
    assert len(result.handoffs) == 2 and not any(h.verified for h in result.handoffs)


def test_live_operator_approves_the_commit(sim, artifact):
    op = ScriptedOperator()
    result, _ = replay(sim, artifact, approved=False, operator=op)
    assert isinstance(result, Succeeded)
    assert result.approvals[0].channel == "scripted" and op.approvals[0].step_id == "s13"
    summary = op.approvals[0].summary
    assert "900001" not in summary and "••••01" in summary and "25.00" in summary  # enough to decide, not more
    assert not any("900001" in json.dumps(a.model_dump()) for a in result.approvals)


# --- determinism --------------------------------------------------------------


def fingerprint(result):
    return [
        (s.step_id, s.status, s.resolution.used_index if s.resolution else None,
         tuple(a.outcome for a in s.resolution.attempts) if s.resolution else ())
        for s in result.steps
    ]


def test_five_replays_take_identical_paths(sim, artifact):
    prints = []
    for i in range(5):
        result, engine = replay(sim, artifact, run_id=f"det-{i}")
        assert isinstance(result, Succeeded)
        acts = [(e["kind"], e.get("step_id"), e.get("action")) for e in engine.log.events if e["kind"] in ("act", "step")]
        prints.append((fingerprint(result), acts, result.outputs["confirmation_number"]))
    assert all(p == prints[0] for p in prints)


def test_replay_never_imports_a_model_provider():
    code = (
        "import sys, agent_hands.replay.engine, agent_hands.replay.result;"
        "bad=[m for m in sys.modules if m.split('.')[0]=='anthropic' or m.startswith(('agent_hands.llm','agent_hands.agent'))];"
        "print(bad); sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_terminal_operator_treats_end_of_input_as_reject_not_a_hang():
    import io

    from agent_hands.handoff.operator import ApprovalRequest, TerminalOperator

    op = TerminalOperator(stream_in=io.StringIO(""), stream_out=io.StringIO())
    d = op.approve(ApprovalRequest(run_id="r", step_id="s13", intent="x", target="t", bound_to="h", summary="s"))
    assert d.decision == "rejected" and d.approver == "no-terminal"


def test_discover_refuses_without_an_interactive_terminal(monkeypatch, capsys):
    import io

    from agent_hands.cli import main

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["discover", "--spec", "specs/open_sub_account.json", "-p", "member_id=900001"]) == 1
    assert "interactive terminal" in capsys.readouterr().err
