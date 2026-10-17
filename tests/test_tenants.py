"""One base artifact, two tenants running the same vendor product: drift without an overlay, a
clean run with one."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from browser_helpers import allowlist_for

import cu_sim
from agent_hands.artifact.overlay import TenantOverlay, apply_overlay, overlay_for
from agent_hands.artifact.schema import CapabilityArtifact, CssStrategy, LiteralValue
from agent_hands.policy import Policy
from agent_hands.profile import load_profile
from agent_hands.replay.engine import ReplayConfig, ReplayEngine, approve
from agent_hands.replay.result import Failed, FailureKind, NeedsHuman, Succeeded
from agent_hands.surface.web import BrowserSession

ROOT = Path(__file__).parents[1]
BASE = CapabilityArtifact.model_validate_json((ROOT / "tests/fixtures/discovered_open_sub_account.json").read_text())
LAKESIDE = json.loads((ROOT / "config/tenants/lakeside.json").read_text())
PARAMS = {"member_id": "900001", "account_type": "Savings", "nickname": "Rainy day", "initial_deposit": "25.00"}


def test_overlay_rewrites_text_literals_only():
    ov = overlay_for(LAKESIDE, BASE.capability.id)
    derived = apply_overlay(BASE, ov)
    s04 = derived.steps[3].target
    assert s04.strategies[0].anchor == LiteralValue(literal="Member No.:")
    assert isinstance(s04.strategies[1], CssStrategy) and s04.strategies[1] == BASE.steps[3].target.strategies[1]
    assert derived.content_hash() != BASE.content_hash()
    assert derived.provenance.overlay == "lakeside" and derived.provenance.base_hash == BASE.content_hash()
    assert derived.capability.version == BASE.capability.version
    assert derived.params == BASE.params and derived.outputs == BASE.outputs  # same contract


def test_overlay_for_another_capability_is_refused():
    with pytest.raises(ValueError):
        apply_overlay(BASE, TenantOverlay(tenant="x", capability="cu_core.other"))


@pytest.fixture(scope="module")
def lakeside():
    s = cu_sim.start(tenant="lakeside")
    yield s
    s.stop()


def run(sim, artifact, approvals=()):
    sim.state.reset()
    session = BrowserSession(policy=Policy(allowlist_for(sim.url)))
    session.start()
    try:
        cfg = ReplayConfig(run_id="tenant", origin=sim.url, allow_draft=True, approvals=list(approvals))
        return ReplayEngine(artifact, load_profile("cu_core"), session, cfg).run(PARAMS)
    finally:
        session.close()


@pytest.mark.browser
def test_base_artifact_on_tenant_b_drifts_then_fails_at_the_first_wrong_screen(lakeside):
    r = run(lakeside, BASE)
    assert isinstance(r, Failed)
    # s04 still works through its CSS fallback (drift); s05's checkpoint pins the results header,
    # which reads "Member No. Name Branch" here: the first screen that's wrong for this tenant
    assert r.failure.kind == FailureKind.CHECKPOINT_FAILED and r.failure.step_id == "s05"
    assert "Member # Name Branch" in r.failure.expected and "Member No." in r.failure.observed
    drifted = {d.step_id: (d.primary_kind, d.primary_outcome, d.used_kind) for d in r.drift}
    assert drifted == {"s04": ("near_text", "none", "css")}  # "Member #:" isn't on this tenant's screen
    assert lakeside.state.snapshot()["subaccounts"] == []


@pytest.mark.browser
def test_with_the_tenant_overlay_it_runs_clean(lakeside):
    ov = overlay_for(LAKESIDE, BASE.capability.id)
    derived = apply_overlay(BASE, ov)
    gated = run(lakeside, derived)
    assert isinstance(gated, NeedsHuman) and gated.step_id == "s13" and gated.drift == []
    ok = run(lakeside, derived, [approve(derived, "s13", PARAMS, "pytest", run_id="tenant")])
    assert isinstance(ok, Succeeded) and ok.drift == []
    assert lakeside.state.snapshot()["subaccounts"][0]["product"] == "Share Account"



def test_display_labels_are_covered_by_the_hash_and_the_approval(lakeside):
    ov = overlay_for(LAKESIDE, BASE.capability.id)
    derived = apply_overlay(BASE, ov)
    assert derived.display_labels == {"account_type": {"Savings": "Share Account"}}
    tampered = apply_overlay(BASE, ov.model_copy(update={"param_labels": {"account_type": {"Savings": "Certificate"}}}))
    assert tampered.content_hash() != derived.content_hash()
    # an approval for the reviewed labels doesn't authorise the tampered ones
    r = run(lakeside, tampered, [approve(derived, "s13", PARAMS, "pytest", run_id="tenant")])
    assert isinstance(r, NeedsHuman) and r.reason == "approval_required"
    assert lakeside.state.snapshot()["subaccounts"] == []


def test_adding_display_labels_did_not_change_existing_artifact_hashes():
    promoted = CapabilityArtifact.model_validate_json(
        (ROOT / "artifacts/cu_core.open_sub_account@1.0.0.json").read_text())
    assert promoted.display_labels is None
    # the hash every committed evidence run recorded
    assert promoted.content_hash() == "sha256:2ccdf82ba1b52b90f08d33ad394dce8394fd3136b7ddeb46a1278dbf16681d5a"
