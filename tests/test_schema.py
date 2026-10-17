"""Artifact schema contract. Written before the implementation (TDD)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agent_hands.artifact.schema import (
    CapabilityArtifact,
    ParamError,
    required_bump,
)


def load(d: dict) -> CapabilityArtifact:
    return CapabilityArtifact.model_validate(d)


# --- happy path -------------------------------------------------------------


def test_example_parses(artifact_dict):
    a = load(artifact_dict)
    assert a.capability.id == "cu_core.open_sub_account"
    assert [s.id for s in a.steps][:3] == ["s01", "s02", "s03"]


def test_canonical_json_round_trips(artifact_dict):
    a = load(artifact_dict)
    text = a.canonical_json()
    b = CapabilityArtifact.model_validate_json(text)
    assert a == b
    assert b.canonical_json() == text


def test_content_hash_ignores_key_order_and_status(artifact_dict):
    a = load(artifact_dict)
    reordered = json.loads(json.dumps(artifact_dict, sort_keys=True))
    reordered["capability"]["status"] = "approved"
    reordered["provenance"]["approved_by"] = "someone"
    b = load(reordered)
    assert a.content_hash() == b.content_hash()
    assert a.content_hash().startswith("sha256:")


def test_content_hash_changes_when_a_step_changes(artifact_dict):
    a = load(artifact_dict)
    artifact_dict["steps"][3]["intent"] = "Search for the member"
    assert load(artifact_dict).content_hash() != a.content_hash()


# --- required structure -----------------------------------------------------


def test_every_step_needs_its_own_checkpoint(artifact_dict):
    del artifact_dict["steps"][4]["expect"]
    with pytest.raises(ValidationError, match="expect"):
        load(artifact_dict)


def test_checkpoint_needs_at_least_one_condition(artifact_dict):
    artifact_dict["steps"][4]["expect"] = {"all_of": [], "timeout_ms": 1000}
    with pytest.raises(ValidationError, match="at least one condition"):
        load(artifact_dict)


def test_params_must_declare_sensitivity(artifact_dict):
    del artifact_dict["params"][0]["sensitivity"]
    with pytest.raises(ValidationError, match="sensitivity"):
        load(artifact_dict)


def test_outputs_must_declare_sensitivity(artifact_dict):
    del artifact_dict["outputs"][0]["sensitivity"]
    with pytest.raises(ValidationError, match="sensitivity"):
        load(artifact_dict)


def test_secret_sensitivity_is_not_allowed_on_params(artifact_dict):
    artifact_dict["params"][0]["sensitivity"] = "secret"
    with pytest.raises(ValidationError, match="secrets"):
        load(artifact_dict)


def test_extra_fields_are_rejected(artifact_dict):
    artifact_dict["steps"][0]["selector"] = "#oops"
    with pytest.raises(ValidationError):
        load(artifact_dict)


def test_schema_version_is_pinned(artifact_dict):
    artifact_dict["schema_version"] = "2.0"
    with pytest.raises(ValidationError):
        load(artifact_dict)


def test_version_must_be_semver(artifact_dict):
    artifact_dict["capability"]["version"] = "v1"
    with pytest.raises(ValidationError):
        load(artifact_dict)


# --- referential integrity --------------------------------------------------


def test_duplicate_step_ids_rejected(artifact_dict):
    artifact_dict["steps"][5]["id"] = "s04"
    with pytest.raises(ValidationError, match="duplicate step id"):
        load(artifact_dict)


def test_unknown_param_in_action_rejected(artifact_dict):
    artifact_dict["steps"][3]["action"]["value"] = {"param": "ssn"}
    with pytest.raises(ValidationError, match="unknown param 'ssn'"):
        load(artifact_dict)


def test_unknown_param_inside_locator_rejected(artifact_dict):
    artifact_dict["steps"][4]["target"]["strategies"].append(
        {"kind": "text", "text": {"param": "member_name"}, "match": "exact"}
    )
    with pytest.raises(ValidationError, match="unknown param 'member_name'"):
        load(artifact_dict)


def test_unknown_secret_rejected(artifact_dict):
    artifact_dict["steps"][1]["action"]["value"] = {"secret": "admin_password"}
    with pytest.raises(ValidationError, match="unknown secret 'admin_password'"):
        load(artifact_dict)


def test_secret_ref_scheme_is_checked(artifact_dict):
    artifact_dict["secrets"][1]["ref"] = "hunter2"
    with pytest.raises(ValidationError):
        load(artifact_dict)


def test_output_source_step_must_read_that_output(artifact_dict):
    artifact_dict["outputs"][0]["source_step"] = "s04"
    with pytest.raises(ValidationError, match="share_balance"):
        load(artifact_dict)


def test_read_step_must_name_a_declared_output(artifact_dict):
    artifact_dict["steps"][4]["action"]["output"] = "ssn"
    with pytest.raises(ValidationError, match="unknown output 'ssn'"):
        load(artifact_dict)


def test_resume_anchor_must_be_an_earlier_step(artifact_dict):
    artifact_dict["steps"][4]["resume_anchor"] = "s09"
    with pytest.raises(ValidationError, match="resume_anchor"):
        load(artifact_dict)


def test_resume_anchor_cannot_cross_a_commit(artifact_dict):
    artifact_dict["steps"][8]["resume_anchor"] = "s04"  # s09 is after the irreversible s08
    with pytest.raises(ValidationError, match="re-run an irreversible step"):
        load(artifact_dict)


def test_outcome_steps_must_exist(artifact_dict):
    artifact_dict["outcomes"][0]["at_steps"] = ["s99"]
    with pytest.raises(ValidationError, match="s99"):
        load(artifact_dict)


def test_login_steps_must_come_first(artifact_dict):
    artifact_dict["steps"][5]["phase"] = "login"
    with pytest.raises(ValidationError, match="login"):
        load(artifact_dict)


# --- action shape -----------------------------------------------------------


def test_fill_requires_a_value(artifact_dict):
    del artifact_dict["steps"][3]["action"]["value"]
    with pytest.raises(ValidationError, match="fill"):
        load(artifact_dict)


def test_click_must_not_carry_a_value(artifact_dict):
    artifact_dict["steps"][2]["action"]["value"] = {"literal": "x"}
    with pytest.raises(ValidationError, match="click"):
        load(artifact_dict)


def test_non_navigate_steps_need_a_target(artifact_dict):
    del artifact_dict["steps"][2]["target"]
    with pytest.raises(ValidationError, match="target"):
        load(artifact_dict)


# --- risk -------------------------------------------------------------------


def test_irreversible_step_needs_two_agreeing_strategies(artifact_dict):
    artifact_dict["steps"][7]["target"]["min_agreement"] = 1
    with pytest.raises(ValidationError, match="irreversible"):
        load(artifact_dict)


def test_min_agreement_cannot_exceed_strategy_count(artifact_dict):
    artifact_dict["steps"][3]["target"]["min_agreement"] = 3
    with pytest.raises(ValidationError, match="min_agreement"):
        load(artifact_dict)


def test_capability_risk_covers_every_step(artifact_dict):
    artifact_dict["capability"]["risk"] = "mutating"
    with pytest.raises(ValidationError, match="risk"):
        load(artifact_dict)


def test_irreversible_steps_listed(artifact_dict):
    assert [s.id for s in load(artifact_dict).irreversible_steps()] == ["s08"]


# --- params -----------------------------------------------------------------


def test_bind_params_coerces_types(artifact_dict):
    a = load(artifact_dict)
    bound = a.bind_params({"member_id": "900001", "account_type": "Savings", "initial_deposit": "25.50"})
    assert bound["initial_deposit"] == Decimal("25.50")
    assert bound["member_id"] == "900001"


@pytest.mark.parametrize(
    "params, field",
    [
        ({"member_id": "12", "account_type": "Savings", "initial_deposit": "25"}, "member_id"),
        ({"member_id": "900001", "account_type": "Checking", "initial_deposit": "25"}, "account_type"),
        ({"member_id": "900001", "account_type": "Savings", "initial_deposit": "1.00"}, "initial_deposit"),
        ({"member_id": "900001", "account_type": "Savings", "initial_deposit": "abc"}, "initial_deposit"),
        ({"member_id": "900001", "account_type": "Savings"}, "initial_deposit"),
        ({"member_id": "900001", "account_type": "Savings", "initial_deposit": "25", "x": "1"}, "x"),
    ],
)
def test_bind_params_rejects_bad_input(artifact_dict, params, field):
    a = load(artifact_dict)
    with pytest.raises(ParamError) as ei:
        a.bind_params(params)
    assert ei.value.param == field


def test_param_error_never_echoes_sensitive_values(artifact_dict):
    a = load(artifact_dict)
    with pytest.raises(ParamError) as ei:
        a.bind_params({"member_id": "123-45-6789", "account_type": "Savings", "initial_deposit": "25"})
    assert "123-45-6789" not in str(ei.value)


# --- versioning -------------------------------------------------------------


def test_required_bump_major_on_contract_change(artifact_dict):
    a = load(artifact_dict)
    artifact_dict["params"][1]["enum"].append("Checking")
    assert required_bump(a, load(artifact_dict)) == "major"


def test_required_bump_minor_on_locator_change(artifact_dict):
    a = load(artifact_dict)
    artifact_dict["steps"][3]["target"]["strategies"][1]["selector"] = "input[name=f2]"
    assert required_bump(a, load(artifact_dict)) == "minor"


def test_required_bump_patch_on_description_change(artifact_dict):
    a = load(artifact_dict)
    artifact_dict["capability"]["description"] = "Opens a sub-account."
    assert required_bump(a, load(artifact_dict)) == "patch"
