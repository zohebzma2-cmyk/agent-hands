"""Vendor app profiles: knowledge about a product that every capability and tenant shares.

A profile holds what is true of "CoreLink Teller 4.x" regardless of which credit union runs it or
which capability is replaying:

- **conditions**: recoverable or escalating runtime states (server error, session expiry, known
  dialogs), with detectors and handlers, checked in a fixed priority order.
- **outcomes**: the vendor's business-answer screens ("No member found", validation rejection,
  permission denied). The compiler binds them to the steps of a capability where they can occur.
- **risk_rules**: deterministic classification of clicks into safe / irreversible. The model never
  decides risk.
- **sensitive_regions**: what to mask in screenshots.

Profiles are reviewed data, versioned like artifacts and referenced as ``profile.condition@N``.
"""

from __future__ import annotations

import json
import re
from functools import cache
from importlib import resources
from typing import Literal

from pydantic import Field

from agent_hands.artifact.schema import (
    CONDITION_REF,
    IDENT,
    Condition,
    FrameRef,
    Model,
    Risk,
    TargetSpec,
)


class ConditionSpec(Model):
    id: str = Field(pattern=CONDITION_REF)
    description: str
    detect: list[Condition] = Field(min_length=1)  # any one matching means the condition is present
    handler: Literal["retry", "re_login", "dismiss"]
    dismiss: TargetSpec | None = None
    budget: int = Field(ge=0)  # max times this handler may run in one replay
    backoff_ms: list[int] = []  # fixed schedule, no jitter: determinism over thundering-herd tuning


class OutcomeTemplate(Model):
    code: str = Field(pattern=IDENT)
    description: str
    detect: list[Condition] = Field(min_length=1)
    applies_to_routes: list[str]  # bind to steps whose resulting page matches one of these globs
    message_from: TargetSpec | None = None


class RiskRule(Model):
    """First matching rule wins. Unmatched POST submits default to irreversible (fail closed)."""

    form_action: str | None = None  # glob over the form's action path
    name_pattern: str | None = None  # regex over the control's accessible name
    risk: Risk
    note: str = ""


class SensitiveRegion(Model):
    frame: list[FrameRef] = []
    css: str


class AppProfile(Model):
    profile: str = Field(pattern=IDENT)
    vendor_product: str
    versions: str
    conditions: list[ConditionSpec]
    outcomes: list[OutcomeTemplate]
    risk_rules: list[RiskRule]
    sensitive_regions: list[SensitiveRegion] = []
    pii_patterns: list[tuple[str, str]] = []  # (name, regex) for the vendor's identifier formats

    def condition(self, ref: str) -> ConditionSpec:
        for c in self.conditions:
            if c.id == ref:
                return c
        raise KeyError(ref)

    def classify_click(self, *, name: str, is_post_submit: bool, form_action: str | None) -> tuple[Risk, str]:
        from fnmatch import fnmatch

        for rule in self.risk_rules:
            if rule.form_action is not None and not (form_action and fnmatch(form_action, rule.form_action)):
                continue
            if rule.name_pattern is not None and not re.search(rule.name_pattern, name or ""):
                continue
            return rule.risk, rule.note or "profile rule"
        if is_post_submit:
            return Risk.IRREVERSIBLE, "unlisted POST submit: fail closed"
        return Risk.SAFE, "no form submission"


@cache
def load_profile(name: str) -> AppProfile:
    text = resources.files("agent_hands.profiles").joinpath(f"{name}.json").read_text()
    return AppProfile.model_validate(json.loads(text))
