"""Discovery → compile → validation replay, offline, with the scripted FAKE provider (tests only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from browser_helpers import allowlist_for

import cu_sim
from agent_hands.agent.pipeline import discover
from agent_hands.artifact.schema import ActionKind, ParamRef, SecretValue, Status
from agent_hands.artifact.spec import CapabilitySpec
from agent_hands.evidence import EVIDENCE_ROOT, EvidenceGuardError, RunLog
from agent_hands.handoff.operator import ScriptedOperator
from agent_hands.llm.fake import ScriptedFakeProvider, ref_for
from agent_hands.redact import Redactor
from agent_hands.replay.result import Succeeded

pytestmark = pytest.mark.browser

SPEC_PATH = Path(__file__).parents[1] / "specs" / "open_sub_account.json"
SPEC = CapabilitySpec.model_validate(json.loads(SPEC_PATH.read_text()))
PARAMS = {"member_id": "900001", "account_type": "Savings", "nickname": "Rainy day", "initial_deposit": "25.00"}


def happy_script(extra_after_member=()):
    r = ref_for
    return [
        ("fill", lambda s: {"ref": r(s, r"- textbox\b"), "intent": "Enter the teller user ID",
                            "secret": "teller_username"}),
        ("fill", lambda s: {"ref": r(s, r"- textbox\b", 1), "intent": "Enter the password",
                            "secret": "teller_password"}),
        ("click", lambda s: {"ref": r(s, r'button "Sign On"'), "intent": "Sign on"}),
        ("fill", lambda s: {"ref": r(s, r"- textbox\b"), "intent": "Type the member number", "param": "member_id"}),
        ("click", lambda s: {"ref": r(s, r'button "Find"'), "intent": "Run the search"}),
        ("click", lambda s: {"ref": r(s, r'cell "900001"'), "intent": "Open the member"}),
        *extra_after_member,
        ("read", lambda s: {"ref": r(s, r'cell "\$812\.40"'), "intent": "Read the base share balance",
                            "output": "share_balance"}),
        ("click", lambda s: {"ref": r(s, r'button "Open Sub-Account"'), "intent": "Open the sub-account form"}),
        ("select", lambda s: {"ref": r(s, r"- combobox\b"), "intent": "Choose the type", "param": "account_type"}),
        ("fill", lambda s: {"ref": r(s, r"- textbox\b"), "intent": "Enter the nickname", "param": "nickname"}),
        ("fill", lambda s: {"ref": r(s, r"- textbox\b", 1), "intent": "Enter the deposit", "literal": "25.00"}),
        ("select", lambda s: {"ref": r(s, r"- combobox\b", 1), "intent": "Fund from cash", "literal": "Cash"}),
        ("click", lambda s: {"ref": r(s, r'button "Open Account"'), "intent": "Submit to open the account"}),
        ("read", lambda s: {"ref": r(s, r'cell "SA-\d+"'), "intent": "Read the confirmation number",
                            "output": "confirmation_number"}),
        ("done", lambda s: {"summary": "opened"}),
    ]


@pytest.fixture(scope="module")
def sim():
    s = cu_sim.start()
    yield s
    s.stop()


def run(sim, script, tmp_path, faults=None, operator=None):
    sim.state.reset()
    if faults:
        sim.set_faults(faults)
    return discover(SPEC, PARAMS, ScriptedFakeProvider(script), origin=sim.url, allowlist=allowlist_for(sim.url),
                    run_id="fake-disc", run_dir=tmp_path, operator=operator or ScriptedOperator())


def test_discovery_compiles_and_validates(sim, tmp_path):
    res = run(sim, happy_script(), tmp_path)
    assert res.error is None, res.error
    a = res.artifact
    assert a is not None and a.capability.status == Status.VALIDATED
    assert isinstance(res.validation, Succeeded)
    assert len(a.steps) == 14
    assert [s.phase for s in a.steps[:4]] == ["login", "login", "login", "task"]
    # literal "25.00" typed by the model became the param it equals
    assert a.steps[10].action.value == ParamRef(param="initial_deposit")
    assert isinstance(a.steps[0].action.value, SecretValue)
    commit = a.irreversible_steps()
    assert [s.intent for s in commit] == ["Submit to open the account"] and commit[0].target.min_agreement == 2
    assert {o.code for o in a.outcomes} == {"member_not_found", "permission_denied", "validation_rejected"}
    # each step carries its own checkpoint, and none of them holds member data
    body = json.loads(a.canonical_json())
    text = json.dumps([body["steps"], body["outcomes"], body["success"]])  # params[].example is synthetic doc
    for raw in ("900001", "812.40", "SAMPLE", "synthetic-teller-pass"):
        assert raw not in text
    assert a.steps[5].resume_anchor == "s04" and a.steps[13].resume_anchor is None
    assert len(sim.state.snapshot()["subaccounts"]) == 2  # discovery + validation, each approved


def test_irreversible_click_needs_approval_in_discovery(sim, tmp_path):
    op = ScriptedOperator(approve=False)
    script = happy_script()[:13] + [("give_up", lambda s: {"reason": "rejected"})]
    res = run(sim, script, tmp_path, operator=op)
    assert res.discovery.status == "gave_up"
    assert op.approvals and sim.state.snapshot()["subaccounts"] == []


def test_interstitial_clicks_are_not_compiled(sim, tmp_path):
    ack = ("click", lambda s: {"ref": ref_for(s, r'link "Acknowledge"'), "intent": "Dismiss the announcement"})
    res = run(sim, happy_script([ack]), tmp_path, faults={"announcement": {"mode": "once"}})
    assert res.error is None, res.error
    assert len(res.artifact.steps) == 14  # the Acknowledge click was dropped
    assert res.report.dropped[0]["why"] == "known interstitial dismissal"


def test_model_cannot_invent_params_or_secrets(sim, tmp_path):
    script = [
        ("fill", lambda s: {"ref": ref_for(s, r"- textbox\b"), "intent": "x", "param": "ssn"}),
        ("fill", lambda s: {"ref": ref_for(s, r"- textbox\b"), "intent": "x", "secret": "admin_password"}),
        ("give_up", lambda s: {"reason": "n/a"}),
    ]
    res = run(sim, script, tmp_path)
    errs = [e for e in RunLogEvents(tmp_path) if e["kind"] == "tool_result" and e["is_error"]]
    assert len(errs) == 2 and res.discovery.trace == []


def test_done_requires_outputs(sim, tmp_path):
    res = run(sim, [("done", lambda s: {"summary": "x"}), ("give_up", lambda s: {"reason": "x"})], tmp_path)
    assert res.discovery.status == "gave_up"


def test_discovery_evidence_is_redacted(sim, tmp_path):
    run(sim, happy_script(), tmp_path)
    blob = "".join(p.read_text() for p in tmp_path.rglob("*") if p.suffix in (".json", ".jsonl"))
    for raw in ("900001", "812.40", "synthetic-teller-pass", "teller01"):
        assert raw not in blob, raw


def test_model_never_sees_secret_values(sim, tmp_path):
    provider = ScriptedFakeProvider(happy_script()[:3] + [("give_up", lambda s: {"reason": "x"})])
    sim.state.reset()
    discover(SPEC, PARAMS, provider, origin=sim.url, allowlist=allowlist_for(sim.url), run_id="s", run_dir=None,
             operator=ScriptedOperator())
    seen = json.dumps(provider.seen)
    assert "synthetic-teller-pass" not in seen and "teller01" not in seen
    assert "[SECRET:teller_password]" in seen


def test_fake_runs_cannot_write_evidence(tmp_path):
    with pytest.raises(EvidenceGuardError):
        RunLog(EVIDENCE_ROOT / "discovery" / "should-not-exist", Redactor(), backend="fake")
    assert not (EVIDENCE_ROOT / "discovery" / "should-not-exist").exists()


def RunLogEvents(d: Path):  # noqa: N802
    return [json.loads(line) for line in (d / "run.jsonl").read_text().splitlines()]


def test_fake_provider_is_labelled():
    assert ScriptedFakeProvider([]).name == "fake"
    assert ActionKind.CLICK.value == "click"


# --- second review: what the model says, what ships in the artifact, what the page sees ----------


def test_model_narration_and_intents_are_scrubbed(sim, tmp_path):
    chatty = happy_script()
    chatty[6] = (chatty[6][0], chatty[6][1],
                 {"text": "ADA SAMPLE lives at 1 Example Way; Share Draft shows 1,204.11 and base 812.40"})
    chatty[3] = ("fill", lambda s: {"ref": ref_for(s, r"- textbox\b"), "intent": "Type 900001 into the search",
                                    "param": "member_id"})
    # names can only be scrubbed once the model has been shown them, i.e. after the member page
    chatty[6] = ("read", lambda s: {"ref": ref_for(s, r'cell "\$812\.40"'), "output": "share_balance",
                                    "intent": "Read ADA SAMPLE's base share"}, chatty[6][2])
    res = run(sim, chatty, tmp_path)
    assert res.error is None, res.error
    blob = "".join(p.read_text() for p in tmp_path.rglob("*") if p.suffix in (".json", ".jsonl"))
    for raw in ("ADA SAMPLE", "Example Way", "1,204.11", "812.40", "900001"):
        assert raw not in blob, raw
    intents = [s.intent for s in res.artifact.steps]
    assert not any("900001" in i or "SAMPLE" in i for i in intents)


def test_outcomes_bind_to_the_steps_where_they_happen(sim, tmp_path):
    res = run(sim, happy_script(), tmp_path)
    at = {o.code: o.at_steps for o in res.artifact.outcomes}
    assert "s05" in at["member_not_found"]  # the Find click
    assert "s13" in at["validation_rejected"]  # the submit
    assert "s03" not in at["member_not_found"]  # not the sign-on click


def test_identity_checkpoints_use_only_identifier_params(sim, tmp_path):
    res = run(sim, happy_script(), tmp_path)
    refs = {c.text.param for s in res.artifact.steps for c in s.expect.all_of
            if c.kind == "text_visible" and hasattr(c.text, "param")}
    assert refs == {"member_id"}


def test_literal_amount_is_matched_to_the_param_by_value(sim, tmp_path):
    script = happy_script()
    script[10] = ("fill", lambda s: {"ref": ref_for(s, r"- textbox\b", 1), "intent": "Enter the deposit",
                                     "literal": "$25.00"})
    res = run(sim, script, tmp_path)
    assert res.artifact.steps[10].action.value == ParamRef(param="initial_deposit")


def test_unused_param_blocks_compilation(sim, tmp_path):
    script = happy_script()
    script[9] = ("fill", lambda s: {"ref": ref_for(s, r"- textbox\b"), "intent": "Enter the nickname",
                                    "literal": "Something else"})
    res = run(sim, script, tmp_path)
    assert res.artifact is None and "never used" in res.error


def test_truncated_turn_is_not_executed(sim, tmp_path):
    script = [("click", lambda s: {"ref": ref_for(s, r'button "Sign On"'), "intent": "x"},
               {"stop_reason": "max_tokens"}),
              ("give_up", lambda s: {"reason": "x"})]
    res = run(sim, script, tmp_path)
    assert res.discovery.trace == []


def test_provenance_records_the_models_that_served(sim, tmp_path):
    res = run(sim, happy_script(), tmp_path)
    assert res.artifact.provenance.models_served == ["scripted-fake"]


def test_page_never_receives_secret_values(sim, tmp_path, monkeypatch):
    from playwright.sync_api import Frame

    from agent_hands.redact import clear_process_secrets, register_process_secret

    register_process_secret("not-a-real-api-key-1234567890")
    seen = []
    orig = Frame.evaluate

    def spy(self, expression, arg=None, *a, **k):
        seen.append(repr(arg))
        return orig(self, expression, arg, *a, **k)

    monkeypatch.setattr(Frame, "evaluate", spy)
    try:
        run(sim, happy_script(), tmp_path)
    finally:
        clear_process_secrets()
    blob = " ".join(seen)
    assert "not-a-real-api-key" not in blob and "synthetic-teller-pass" not in blob and "teller01" not in blob


def test_model_screenshots_mask_the_operator_banner(sim):
    from browser_helpers import login

    from agent_hands.evidence import mask_locators
    from agent_hands.policy import Policy
    from agent_hands.surface.web import BrowserSession

    session = BrowserSession(policy=Policy(allowlist_for(sim.url)))
    page = session.start()
    try:
        login(page, sim.url)
        banner = page.frame(name="banner")
        masks = mask_locators(page, ["teller01"], structural=False)
        cell = banner.locator("td", has_text="teller01").last.element_handle()
        handles = [h for m in masks for h in m.element_handles() if h.owner_frame() == banner]
        assert any(h.evaluate("(m, c) => m.contains(c)", cell) for h in handles)
    finally:
        session.close()


def test_commit_guard_blocks_unapproved_submits_but_not_the_human(sim):
    from browser_helpers import login

    from agent_hands.policy import CommitGuard, Policy
    from agent_hands.profile import load_profile, post_is_irreversible
    from agent_hands.surface.web import BrowserSession

    sim.state.reset()
    human = {"holds": False}
    session = BrowserSession(policy=Policy(allowlist_for(sim.url)))
    page = session.start()
    guard = CommitGuard(lambda p: post_is_irreversible(load_profile("cu_core"), p), lambda: human["holds"])
    guard.install(session.context)
    try:
        login(page, sim.url)
        main = page.frame(name="main")
        main.goto(sim.url + "/core/subacct.do?mid=900001")
        main.select_option("select[name=f7]", label="Savings")
        main.fill("input[name=f9]", "25.00")
        main.evaluate("() => document.forms[0].submit()")  # not even a click on the button
        page.wait_for_timeout(500)
        assert guard.blocked == ["/core/subacct.do"] and sim.state.snapshot()["subaccounts"] == []
        human["holds"] = True
        main.goto(sim.url + "/core/subacct.do?mid=900001")
        main.select_option("select[name=f7]", label="Savings")
        main.fill("input[name=f9]", "25.00")
        main.evaluate("() => document.forms[0].submit()")
        page.wait_for_timeout(500)
        assert len(sim.state.snapshot()["subaccounts"]) == 1
    finally:
        session.close()


def test_stability_refuses_fake_artifacts_into_evidence(sim, tmp_path):
    from agent_hands.cli import main
    from agent_hands.evidence import EVIDENCE_ROOT

    res = run(sim, happy_script(), tmp_path)
    path = tmp_path / "fake-artifact.json"
    path.write_text(res.artifact.canonical_json())
    assert main(["stability", str(path), "--runs", "1", "--out", str(EVIDENCE_ROOT / "should-not-exist")]) == 2
    assert not (EVIDENCE_ROOT / "should-not-exist").exists()
