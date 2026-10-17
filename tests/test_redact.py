"""Redactor contract. Written before the implementation (TDD).

Redaction is driven by the capability contract first (declared sensitivity of params, secrets and
outputs) and by regex patterns second, as a backstop for values nobody declared.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from agent_hands.artifact.schema import CapabilityArtifact, Sensitivity
from agent_hands.redact import (
    Redactor,
    clear_process_secrets,
    install_log_filter,
    mask_value,
    register_process_secret,
)


@pytest.fixture(autouse=True)
def _clean_process_secrets():
    clear_process_secrets()
    yield
    clear_process_secrets()


# --- contract-driven --------------------------------------------------------


def test_pii_value_is_replaced_with_a_named_token():
    r = Redactor()
    r.add("member_id", "900001", Sensitivity.PII)
    assert r.scrub("Member 900001 found") == "Member [PII:member_id] found"


def test_secret_value_is_replaced():
    r = Redactor()
    r.add("teller_password", "s3cret-synthetic", Sensitivity.SECRET)
    assert r.scrub("typed s3cret-synthetic") == "typed [SECRET:teller_password]"


def test_public_and_internal_values_are_kept():
    r = Redactor()
    r.add("account_type", "Savings", Sensitivity.INTERNAL)
    r.add("branch", "Main", Sensitivity.PUBLIC)
    assert r.scrub("Savings at Main") == "Savings at Main"


def test_longer_values_win_over_overlapping_shorter_ones():
    r = Redactor()
    r.add("a", "9000", Sensitivity.PII)
    r.add("b", "900012", Sensitivity.PII)
    assert r.scrub("id 900012") == "id [PII:b]"


def test_tiny_values_are_not_registered():
    r = Redactor()
    r.add("flag", "1", Sensitivity.PII)
    assert r.scrub("step 1 of 10") == "step 1 of 10"


def test_decimal_values_are_scrubbed_in_common_renderings():
    r = Redactor()
    r.add("initial_deposit", Decimal("1250.00"), Sensitivity.PII)
    assert "1250.00" not in r.scrub("deposit 1250.00")
    assert "1,250.00" not in r.scrub("deposit $1,250.00")


def test_from_contract_uses_declared_sensitivity(artifact_dict):
    artifact = CapabilityArtifact.model_validate(artifact_dict)
    params = artifact.bind_params({"member_id": "900001", "account_type": "Savings", "initial_deposit": "25.00"})
    r = Redactor.from_contract(
        artifact,
        params=params,
        secrets={"teller_username": "teller01", "teller_password": "synthetic-pass-1"},
        outputs={"share_balance": Decimal("812.40"), "confirmation_number": "SA-77120"},
    )
    out = r.scrub("member 900001 Savings 25.00 teller01 synthetic-pass-1 812.40 SA-77120")
    assert "900001" not in out
    assert "teller01" not in out and "synthetic-pass-1" not in out
    assert "812.40" not in out
    assert "Savings" in out  # internal
    assert "SA-77120" in out  # internal output, redaction none


def test_scrub_obj_walks_nested_structures():
    r = Redactor()
    r.add("member_id", "900001", Sensitivity.PII)
    data = {"a": ["x 900001", {"b": "900001"}], "n": 5, "t": ("900001",)}
    out = r.scrub_obj(data)
    assert out == {"a": ["x [PII:member_id]", {"b": "[PII:member_id]"}], "n": 5, "t": ["[PII:member_id]"]}
    assert data["a"][0] == "x 900001"  # input untouched


# --- pattern backstop -------------------------------------------------------


@pytest.mark.parametrize(
    "raw, leaked",
    [
        ("ssn 123-45-6789", "123-45-6789"),
        ("acct 4401887722019", "4401887722019"),
        ("mail ada.sample@example.test", "ada.sample@example.test"),
        ("call (603) 555-0142", "555-0142"),
        ("DOB 04/12/1961", "04/12/1961"),
        ("key sk-ant-api03-AbC_dEf-123456789", "sk-ant-api03-AbC_dEf-123456789"),
        ("key sk-or-v1-0123456789abcdef0123", "sk-or-v1-0123456789abcdef0123"),
    ],
)
def test_patterns_catch_undeclared_values(raw, leaked):
    assert leaked not in Redactor().scrub(raw)


def test_account_numbers_keep_last_four_for_debugging():
    assert Redactor().scrub("acct 4401887722019").endswith("2019]")


def test_router_generation_ids_survive_redaction():
    gid = "gen-1792267210-621MUyYWx0wKPp0wF7B2"
    assert Redactor().scrub(f"id {gid}") == f"id {gid}"
    assert "4401887722019" not in Redactor().scrub("acct 4401887722019")


def test_patterns_can_be_disabled_for_unit_debugging():
    assert Redactor(patterns=()).scrub("ssn 123-45-6789") == "ssn 123-45-6789"


# --- process-wide secrets (the API key) ------------------------------------


def test_process_secrets_are_scrubbed_by_every_redactor():
    register_process_secret("not-a-real-key-but-long-enough")
    assert Redactor().scrub("x not-a-real-key-but-long-enough y") == "x [SECRET] y"


def test_log_filter_scrubs_records(caplog):
    register_process_secret("another-fake-secret-value")
    logger = logging.getLogger("agent_hands.test")
    install_log_filter(logger)
    with caplog.at_level(logging.INFO, logger="agent_hands.test"):
        logger.info("leaking %s now", "another-fake-secret-value")
    assert "another-fake-secret-value" not in caplog.text
    assert "[SECRET]" in caplog.text


def test_scrub_exception_message():
    register_process_secret("fake-secret-in-exception")
    err = RuntimeError("auth failed for fake-secret-in-exception")
    assert "fake-secret-in-exception" not in Redactor().scrub_exception(err)


# --- output masking modes ---------------------------------------------------


@pytest.mark.parametrize(
    "value, mode, expected",
    [
        (Decimal("812.40"), "none", "812.40"),
        (Decimal("812.40"), "mask", "[REDACTED]"),
        ("4401887722019", "last4", "••••2019"),
        ("SA-77120", "drop", None),
    ],
)
def test_mask_value(value, mode, expected):
    assert mask_value(value, mode) == expected


def test_run_log_keeps_generation_ids_intact_but_still_redacts_account_numbers(tmp_path):
    import json as _json

    from agent_hands.evidence import RunLog

    gid = "gen-1792267236-dA4x9pvwq9ve1i5Kq88B"
    log = RunLog(tmp_path, Redactor(), backend="openrouter")
    log.event("model_turn", generation_id=gid, request_id=gid, text=f"turn {gid} for acct 4401887722019")
    rec = _json.loads((tmp_path / "run.jsonl").read_text())
    assert rec["generation_id"] == gid and rec["request_id"] == gid
    assert gid in rec["text"] and "4401887722019" not in rec["text"] and "2019]" in rec["text"]
