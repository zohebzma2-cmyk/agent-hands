"""Shared fixtures. All data here is synthetic."""

from __future__ import annotations

import copy

import pytest

MAIN = [{"name": "main"}]


def lit(text: str) -> dict:
    return {"literal": text}


def param(name: str) -> dict:
    return {"param": name}


def role(r: str, name: dict, match: str = "exact") -> dict:
    return {"kind": "role", "role": r, "name": name, "match": match}


def near(anchor: str, r: str) -> dict:
    return {"kind": "near_text", "anchor": lit(anchor), "relation": "same_row", "role": r}


def css(sel: str) -> dict:
    return {"kind": "css", "selector": sel}


def text_visible(text: str, frame=MAIN) -> dict:
    return {"kind": "text_visible", "frame": frame, "text": lit(text), "match": "contains"}


def checkpoint(*conds: dict, timeout_ms: int = 5000) -> dict:
    return {"all_of": list(conds), "timeout_ms": timeout_ms}


def target(desc: str, *strategies: dict, min_agreement: int = 1, frame=MAIN) -> dict:
    return {
        "description": desc,
        "frame": frame,
        "strategies": list(strategies),
        "min_agreement": min_agreement,
    }


EXAMPLE = {
    "schema_version": "1.0",
    "capability": {
        "id": "cu_core.open_sub_account",
        "version": "1.0.0",
        "title": "Open a sub-account for a member",
        "description": "Finds a member by number, opens a new sub-account and returns the confirmation number.",
        "status": "draft",
        "risk": "irreversible",
    },
    "app": {"profile": "cu_core", "profile_versions": ">=4.2,<5", "entry_route": "/core/login.do"},
    "params": [
        {
            "name": "member_id",
            "type": "string",
            "pattern": r"^9\d{5}$",
            "sensitivity": "pii",
            "description": "Six-digit member number",
            "example": "900004",
            "identifies_record": True,
        },
        {
            "name": "account_type",
            "type": "enum",
            "enum": ["Savings", "Holiday Club", "Certificate"],
            "sensitivity": "internal",
            "description": "Sub-account product",
        },
        {
            "name": "initial_deposit",
            "type": "decimal",
            "minimum": "5.00",
            "maximum": "5000.00",
            "sensitivity": "pii",
            "description": "Opening deposit in USD",
        },
    ],
    "secrets": [
        {"name": "teller_username", "ref": "sim:teller_username", "description": "Synthetic teller login"},
        {"name": "teller_password", "ref": "sim:teller_password", "description": "Synthetic teller password"},
    ],
    "outputs": [
        {
            "name": "share_balance",
            "type": "decimal",
            "sensitivity": "pii",
            "redaction": "mask",
            "source_step": "s05",
            "description": "Balance of the base share account",
        },
        {
            "name": "confirmation_number",
            "type": "string",
            "sensitivity": "internal",
            "redaction": "none",
            "source_step": "s09",
            "description": "Confirmation number of the new sub-account",
        },
    ],
    "preconditions": ["teller role with sub-account permission"],
    "conditions": ["cu_core.session_expired@1", "cu_core.announcement@1", "cu_core.server_error@1"],
    "outcomes": [
        {
            "code": "member_not_found",
            "description": "No member with that number exists",
            "detect": [text_visible("No member found")],
            "at_steps": ["s04"],
        }
    ],
    "steps": [
        {
            "id": "s01",
            "phase": "login",
            "intent": "Enter the teller username",
            "action": {"kind": "fill", "value": {"secret": "teller_username"}},
            "target": target("User ID box", near("User ID", "textbox"), css("input[name=uid]"), frame=[]),
            "risk": "reversible",
            "expect": checkpoint(
                {"kind": "element_present", "target": target("Password box", near("Password", "textbox"), frame=[])}
            ),
        },
        {
            "id": "s02",
            "phase": "login",
            "intent": "Enter the teller password",
            "action": {"kind": "fill", "value": {"secret": "teller_password"}},
            "target": target("Password box", near("Password", "textbox"), frame=[]),
            "risk": "reversible",
            "expect": checkpoint(
                {"kind": "element_present",
                 "target": target("Sign on button", role("button", lit("Sign On")), frame=[])}
            ),
        },
        {
            "id": "s03",
            "phase": "login",
            "intent": "Sign on",
            "action": {"kind": "click"},
            "target": target("Sign on button", role("button", lit("Sign On")), frame=[]),
            "risk": "safe",
            "expect": checkpoint({"kind": "route_matches", "frame": MAIN, "pattern": "/core/search.do*"}),
        },
        {
            "id": "s04",
            "intent": "Search for the member by number",
            "action": {"kind": "fill", "value": param("member_id")},
            "target": target("Member # box", near("Member #", "textbox"), css("input[name=f1]")),
            "risk": "reversible",
            "expect": checkpoint(text_visible("Member Detail")),
            "resume_anchor": "s04",
        },
        {
            "id": "s05",
            "intent": "Read the base share balance",
            "action": {"kind": "read", "output": "share_balance"},
            "target": target("Share balance cell", near("Base Share", "cell")),
            "risk": "safe",
            "expect": checkpoint(text_visible("Base Share")),
            "resume_anchor": "s04",
        },
        {
            "id": "s06",
            "intent": "Choose the sub-account type",
            "action": {"kind": "select", "value": param("account_type")},
            "target": target("Account type list", near("Account Type", "combobox")),
            "risk": "reversible",
            "expect": checkpoint(text_visible("Open Sub-Account")),
            "resume_anchor": "s04",
        },
        {
            "id": "s07",
            "intent": "Enter the opening deposit",
            "action": {"kind": "fill", "value": param("initial_deposit")},
            "target": target("Deposit box", near("Opening Deposit", "textbox")),
            "risk": "reversible",
            "expect": checkpoint(text_visible("Open Sub-Account")),
            "resume_anchor": "s04",
        },
        {
            "id": "s08",
            "intent": "Submit and open the account",
            "action": {"kind": "click"},
            "target": target(
                "Open account button",
                role("button", lit("Open Account")),
                css("input[value='Open Account']"),
                min_agreement=2,
            ),
            "risk": "irreversible",
            "expect": checkpoint(text_visible("Confirmation")),
        },
        {
            "id": "s09",
            "intent": "Read the confirmation number",
            "action": {"kind": "read", "output": "confirmation_number"},
            "target": target("Confirmation number", near("Confirmation #", "cell")),
            "risk": "safe",
            "expect": checkpoint(text_visible("Confirmation")),
        },
    ],
    "success": checkpoint(text_visible("Sub-account opened")),
    "provenance": {
        "compiler_version": "0.1.0",
        "created_at": "2026-10-17T00:00:00Z",
    },
}


@pytest.fixture
def artifact_dict() -> dict:
    return copy.deepcopy(EXAMPLE)
