"""Replay result contract. Written before the implementation (TDD).

Four terminal statuses. Recoverable conditions are never a status: they are handled and listed
on whatever the final result is.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent_hands.artifact.schema import CapabilityArtifact
from agent_hands.redact import Redactor
from agent_hands.replay.result import (
    Failed,
    Failure,
    FailureKind,
    NeedsHuman,
    Outcome,
    Recovery,
    Resolution,
    StepRecord,
    StrategyAttempt,
    Succeeded,
    parse_result,
)

COMMON = {
    "run_id": "rp-0001",
    "capability": "cu_core.open_sub_account@1.0.0",
    "artifact_hash": "sha256:abc",
    "started_at": "2026-10-17T12:00:00Z",
    "duration_ms": 1234,
    "params": {"member_id": "[PII:member_id]"},
}


def resolution(used: int | None, *outcomes: str) -> Resolution:
    kinds = ["near_text", "css", "xpath"]
    return Resolution(
        description="Member # box",
        used_index=used,
        used_kind=None if used is None else kinds[used],
        attempts=[StrategyAttempt(index=i, kind=kinds[i], outcome=o) for i, o in enumerate(outcomes)],
    )


def step(sid: str, used: int = 0, *outcomes: str) -> StepRecord:
    return StepRecord(
        step_id=sid,
        intent="do " + sid,
        status="ok",
        resolution=resolution(used, *(outcomes or ("unique",))),
        duration_ms=10,
    )


# --- shape ------------------------------------------------------------------


def test_success_round_trips_through_the_union():
    r = Succeeded(**COMMON, steps=[step("s01")], outputs={"confirmation_number": "SA-77120"})
    back = parse_result(r.model_dump_json())
    assert isinstance(back, Succeeded)
    assert back.status == "succeeded"


def test_outcome_is_a_distinct_status_not_a_failure():
    r = Outcome(**COMMON, steps=[], code="member_not_found", message="No member found", at_step="s04")
    back = parse_result(r.model_dump_json())
    assert isinstance(back, Outcome)
    assert back.code == "member_not_found"


def test_failed_carries_step_expected_and_observed():
    f = Failure(
        kind=FailureKind.CHECKPOINT_FAILED,
        step_id="s04",
        intent="Search for the member",
        expected="text_visible 'Member Detail' in main",
        observed="main frame shows 'Member Search'",
    )
    r = Failed(**COMMON, steps=[], failure=f)
    back = parse_result(r.model_dump_json())
    assert isinstance(back, Failed)
    assert back.failure.step_id == "s04"
    assert back.failure.expected and back.failure.observed


def test_needs_human_carries_reason_and_step():
    r = NeedsHuman(**COMMON, steps=[], reason="approval_required", step_id="s08")
    back = parse_result(r.model_dump_json())
    assert isinstance(back, NeedsHuman)
    assert back.reason == "approval_required"


def test_unknown_status_is_rejected():
    payload = json.loads(Succeeded(**COMMON, steps=[], outputs={}).model_dump_json())
    payload["status"] = "kinda_worked"
    with pytest.raises(ValidationError):
        parse_result(json.dumps(payload))


def test_failed_requires_a_failure():
    with pytest.raises(ValidationError):
        Failed(**COMMON, steps=[])


def test_outcome_requires_code_and_step():
    with pytest.raises(ValidationError):
        Outcome(**COMMON, steps=[], message="x")


def test_results_are_immutable():
    r = Succeeded(**COMMON, steps=[], outputs={})
    with pytest.raises(ValidationError):
        r.run_id = "other"


def test_recoveries_ride_along_on_any_status():
    rec = Recovery(step_id="s05", condition="cu_core.announcement@1", action="dismissed", attempt=1)
    r = Succeeded(**COMMON, steps=[], outputs={}, recoveries=[rec])
    assert parse_result(r.model_dump_json()).recoveries[0].condition == "cu_core.announcement@1"


# --- retryability is a property of the kind ---------------------------------


@pytest.mark.parametrize(
    "kind, retryable",
    [
        (FailureKind.TIMEOUT, True),
        (FailureKind.APP_ERROR, True),
        (FailureKind.CHECKPOINT_FAILED, False),
        (FailureKind.POLICY_DENIED, False),
        (FailureKind.COMMIT_STATE_UNKNOWN, False),
        (FailureKind.PARAM_INVALID, False),
        (FailureKind.TARGET_NOT_FOUND, False),
    ],
)
def test_retryable_follows_kind(kind, retryable):
    f = Failure(kind=kind, step_id="s01", expected="x", observed="y")
    assert f.retryable is retryable
    assert json.loads(f.model_dump_json())["retryable"] is retryable


# --- drift signal -----------------------------------------------------------


def test_primary_strategy_is_not_drift():
    r = Succeeded(**COMMON, steps=[step("s01", 0)], outputs={})
    assert r.drift == []


def test_fallback_strategy_is_reported_as_drift():
    r = Succeeded(**COMMON, steps=[step("s01", 0), step("s04", 1, "none", "unique")], outputs={})
    assert len(r.drift) == 1
    d = r.drift[0]
    assert d.step_id == "s04"
    assert d.used_index == 1 and d.used_kind == "css"
    assert d.primary_kind == "near_text" and d.primary_outcome == "none"


def test_drift_is_serialised():
    r = Succeeded(**COMMON, steps=[step("s04", 1, "ambiguous", "unique")], outputs={})
    assert json.loads(r.model_dump_json())["drift"][0]["primary_outcome"] == "ambiguous"


# --- persistence redaction --------------------------------------------------


def test_persisted_view_masks_outputs_by_contract(artifact_dict):
    artifact = CapabilityArtifact.model_validate(artifact_dict)
    r = Succeeded(
        **COMMON,
        steps=[],
        outputs={"share_balance": Decimal("812.40"), "confirmation_number": "SA-77120"},
    )
    redactor = Redactor()
    persisted = r.persisted(artifact, redactor)
    text = json.dumps(persisted)
    assert "812.40" not in text
    assert persisted["outputs"]["share_balance"] == "[REDACTED]"
    assert persisted["outputs"]["confirmation_number"] == "SA-77120"
    assert r.outputs["share_balance"] == Decimal("812.40")  # caller still gets the real value


def test_persisted_view_scrubs_free_text(artifact_dict):
    artifact = CapabilityArtifact.model_validate(artifact_dict)
    r = Failed(
        **COMMON,
        steps=[],
        failure=Failure(
            kind=FailureKind.CHECKPOINT_FAILED,
            step_id="s04",
            expected="Member Detail",
            observed="row: Ada Sample 123-45-6789",
        ),
    )
    assert "123-45-6789" not in json.dumps(r.persisted(artifact, Redactor()))
