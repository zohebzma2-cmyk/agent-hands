"""Capability artifact schema, format version 1.0.

An artifact is the reviewable, versioned, parameterised description of one capability: what it
needs (params, secrets), what it returns (outputs, business outcomes), and the exact steps that do
it, where every step names its target several ways and carries its own checkpoint.

It deliberately holds no browser handles, no model transcript and no member data. Values that vary
per invocation are references (`{"param": ...}`, `{"secret": ...}`), resolved only at replay time.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"

IDENT = r"^[a-z][a-z0-9_]*$"
STEP_ID = r"^s\d{2,3}$"
SEMVER = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
CAPABILITY_ID = r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"
CONDITION_REF = r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*@\d+$"
# Secrets are references into a store, never values. `sim:` is the stand-in app's published,
# synthetic demo credentials (a deliberate mock of a vault).
SECRET_REF = r"^(sim|env|keychain|vault):[A-Za-z0-9_./-]+$"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- enums ------------------------------------------------------------------


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"
    SECRET = "secret"


class Risk(StrEnum):
    SAFE = "safe"  # read or navigate; no side effects
    REVERSIBLE = "reversible"  # changes form state only; nothing is committed
    IRREVERSIBLE = "irreversible"  # commits something in the system of record


RISK_ORDER = {Risk.SAFE: 0, Risk.REVERSIBLE: 1, Risk.IRREVERSIBLE: 2}


class Status(StrEnum):
    DRAFT = "draft"  # compiled from a discovery run, not yet replayed
    VALIDATED = "validated"  # replayed cleanly with no model in the loop
    APPROVED = "approved"  # a human reviewed it and signed the content hash
    DEPRECATED = "deprecated"


class ParamType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    ENUM = "enum"


class OutputType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"


class Redaction(StrEnum):
    NONE = "none"
    MASK = "mask"
    LAST4 = "last4"
    DROP = "drop"


class ActionKind(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    SELECT = "select"
    PRESS = "press"
    READ = "read"


# --- values -----------------------------------------------------------------


class LiteralValue(Model):
    literal: str


class ParamRef(Model):
    param: str = Field(pattern=IDENT)


class SecretValue(Model):
    secret: str = Field(pattern=IDENT)


Text = Union[LiteralValue, ParamRef]
Value = Union[LiteralValue, ParamRef, SecretValue]
Match = Literal["exact", "contains"]


# --- targets ----------------------------------------------------------------


class FrameRef(Model):
    """One hop in a frame path, matched by name or by route pattern, never by index."""

    name: str | None = None
    url_pattern: str | None = None

    @model_validator(mode="after")
    def _one_key(self) -> FrameRef:
        if not self.name and not self.url_pattern:
            raise ValueError("frame ref needs a name or a url_pattern")
        return self


class RoleStrategy(Model):
    """Accessibility role plus accessible name. Best when the app exposes names at all."""

    kind: Literal["role"]
    role: str
    name: Text
    match: Match = "exact"


class LabelStrategy(Model):
    """Form control whose label (``<label>``, aria-label, aria-labelledby) matches."""

    kind: Literal["label"]
    label: Text
    match: Match = "exact"


class NearTextStrategy(Model):
    """The control of ``role`` nearest the anchor text in the same table row.

    Legacy table layouts put the visible label in one cell and an unnamed input in the next, often
    several label/input pairs per row. "Nearest" means the closest cell to the anchor that holds a
    matching control, following cells before preceding ones. For ``role="cell"`` it is the cell in
    the anchor's row under the ``column`` header (or the next cell when no column is given).
    This is how a person finds the field, and it survives markup reshuffles inside the row.
    """

    kind: Literal["near_text"]
    anchor: Text
    relation: Literal["same_row"] = "same_row"
    role: str
    column: Text | None = None  # for role "cell": the header text of the column to read
    match: Match = "exact"


class TextStrategy(Model):
    """An element whose own visible text matches (e.g. a clickable table cell with no role)."""

    kind: Literal["text"]
    text: Text
    match: Match = "exact"


class CssStrategy(Model):
    kind: Literal["css"]
    selector: str


class XPathStrategy(Model):
    kind: Literal["xpath"]
    xpath: str


Strategy = Annotated[
    Union[RoleStrategy, LabelStrategy, NearTextStrategy, TextStrategy, CssStrategy, XPathStrategy],
    Field(discriminator="kind"),
]


SEMANTIC_KINDS = frozenset({"role", "label", "near_text", "text"})


class TargetSpec(Model):
    """How to find one control. Strategies are tried in order; each must match exactly one element.

    ``min_agreement`` > 1 means that many strategies must independently resolve to the same element
    before acting. Irreversible steps require at least 2.
    """

    description: str
    frame: list[FrameRef] = []
    strategies: list[Strategy] = Field(min_length=1)
    min_agreement: int = Field(1, ge=1)

    @model_validator(mode="after")
    def _agreement_possible(self) -> TargetSpec:
        if self.min_agreement > len(self.strategies):
            raise ValueError(
                f"min_agreement {self.min_agreement} exceeds the {len(self.strategies)} strategies given"
            )
        return self


# --- checkpoints ------------------------------------------------------------


# A frame path, or "any" to look in every frame (vendor-wide pages like errors land anywhere).
FrameScope = Union[list[FrameRef], Literal["any"]]


class TextVisible(Model):
    kind: Literal["text_visible"]
    frame: FrameScope = []
    text: Text
    match: Match = "contains"


class ElementPresent(Model):
    kind: Literal["element_present"]
    target: TargetSpec


class ElementAbsent(Model):
    kind: Literal["element_absent"]
    target: TargetSpec


class ValueEquals(Model):
    """A form control currently holds ``value`` (input value, or a select's chosen option label).

    The checkpoint for fill/select steps: legacy apps reformat or swallow keystrokes often enough
    that "I typed it" isn't evidence.
    """

    kind: Literal["value_equals"]
    target: TargetSpec
    value: Value


class RouteMatches(Model):
    kind: Literal["route_matches"]
    frame: FrameScope = []
    pattern: str  # fnmatch-style glob over path + query


Condition = Annotated[
    Union[TextVisible, ElementPresent, ElementAbsent, ValueEquals, RouteMatches],
    Field(discriminator="kind"),
]


class Checkpoint(Model):
    """What must be true, within ``timeout_ms``, for the step (or the run) to count as done."""

    all_of: list[Condition] = []
    any_of: list[Condition] = []
    timeout_ms: int = Field(5000, ge=100, le=120_000)

    @model_validator(mode="after")
    def _non_empty(self) -> Checkpoint:
        if not self.all_of and not self.any_of:
            raise ValueError("checkpoint needs at least one condition")
        return self


# --- steps ------------------------------------------------------------------


class Action(Model):
    kind: ActionKind
    value: Value | None = None  # fill / select
    route: str | None = None  # navigate
    key: str | None = None  # press
    output: str | None = None  # read

    @model_validator(mode="after")
    def _shape(self) -> Action:
        k = self.kind
        if k in (ActionKind.FILL, ActionKind.SELECT):
            if self.value is None:
                raise ValueError(f"{k} action needs a value")
        elif self.value is not None:
            raise ValueError(f"{k} action must not carry a value")
        if (k == ActionKind.NAVIGATE) != (self.route is not None):
            raise ValueError("route is required for navigate and only for navigate")
        if (k == ActionKind.PRESS) != (self.key is not None):
            raise ValueError("key is required for press and only for press")
        if (k == ActionKind.READ) != (self.output is not None):
            raise ValueError("output is required for read and only for read")
        return self


class StepProvenance(Model):
    run_id: str
    trace_index: int


class Step(Model):
    id: str = Field(pattern=STEP_ID)
    phase: Literal["login", "task"] = "task"
    intent: str = Field(min_length=3)
    action: Action
    target: TargetSpec | None = None
    risk: Risk
    expect: Checkpoint
    resume_anchor: str | None = None
    provenance: StepProvenance | None = None

    @model_validator(mode="after")
    def _target_needed(self) -> Step:
        if self.action.kind != ActionKind.NAVIGATE and self.target is None:
            raise ValueError(f"step {self.id}: {self.action.kind} needs a target")
        return self


# --- contract ---------------------------------------------------------------


class ParamError(ValueError):
    """A caller-supplied param failed validation. Never echoes the offending value."""

    def __init__(self, param: str, reason: str) -> None:
        self.param = param
        self.reason = reason
        super().__init__(f"param '{param}': {reason}")


_INT = re.compile(r"^-?\d+$")
_DEC = re.compile(r"^-?\d+(\.\d+)?$")


class ParamSpec(Model):
    name: str = Field(pattern=IDENT)
    type: ParamType
    description: str
    sensitivity: Sensitivity
    required: bool = True
    pattern: str | None = None
    enum: list[str] | None = None
    max_length: int | None = Field(None, ge=1)
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    example: str | None = None  # synthetic only
    # This param names the record the capability acts on (a member number). Checkpoints may require
    # it on screen, so replay can't carry on against a different record.
    identifies_record: bool = False

    @field_validator("sensitivity")
    @classmethod
    def _no_secret_params(cls, v: Sensitivity) -> Sensitivity:
        if v == Sensitivity.SECRET:
            raise ValueError("secret values belong in secrets[], not params")
        return v

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v

    @model_validator(mode="after")
    def _enum_iff_enum(self) -> ParamSpec:
        if (self.type == ParamType.ENUM) != bool(self.enum):
            raise ValueError(f"param {self.name}: enum values are required for, and only for, type enum")
        return self

    def coerce(self, raw: Any) -> str | int | Decimal:
        text = str(raw).strip()
        if self.max_length is not None and len(text) > self.max_length:
            raise ParamError(self.name, f"longer than {self.max_length} characters")
        if self.pattern is not None and not re.fullmatch(self.pattern, text):
            raise ParamError(self.name, "does not match the declared pattern")
        if self.type == ParamType.ENUM:
            if text not in (self.enum or []):
                raise ParamError(self.name, f"must be one of {self.enum}")
            return text
        if self.type == ParamType.STRING:
            return text
        if self.type == ParamType.INTEGER:
            if not _INT.match(text):
                raise ParamError(self.name, "is not an integer")
            value: int | Decimal = int(text)
        else:
            if not _DEC.match(text):
                raise ParamError(self.name, "is not a plain decimal number")
            try:
                value = Decimal(text)
            except InvalidOperation:  # pragma: no cover - regex already guards this
                raise ParamError(self.name, "is not a plain decimal number") from None
        if self.minimum is not None and value < self.minimum:
            raise ParamError(self.name, f"is below the minimum {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ParamError(self.name, f"is above the maximum {self.maximum}")
        return value


class SecretSpec(Model):
    name: str = Field(pattern=IDENT)
    ref: str = Field(pattern=SECRET_REF)
    description: str = ""


class OutputSpec(Model):
    name: str = Field(pattern=IDENT)
    type: OutputType
    description: str
    sensitivity: Sensitivity
    redaction: Redaction  # how the value may appear in anything persisted
    source_step: str = Field(pattern=STEP_ID)


class OutcomeSpec(Model):
    """A business outcome: a legitimate answer the caller needs, not an error."""

    code: str = Field(pattern=IDENT)
    description: str
    detect: list[Condition] = Field(min_length=1)  # any one matching means this outcome
    at_steps: list[str] = []  # where it can occur; empty means any step
    message_from: TargetSpec | None = None  # optional text to return as the message


class CapabilityInfo(Model):
    id: str = Field(pattern=CAPABILITY_ID)
    version: str = Field(pattern=SEMVER)
    title: str
    description: str
    status: Status = Status.DRAFT
    risk: Risk


class AppInfo(Model):
    profile: str = Field(pattern=IDENT)  # vendor product: the unit of reuse across tenants
    profile_versions: str  # e.g. ">=4.2,<5"
    entry_route: str = Field(pattern=r"^/")  # origin comes from tenant config


class Provenance(Model):
    compiler_version: str
    created_at: str
    discovered_by_run: str | None = None
    provider: str | None = None
    model: str | None = None
    models_served: list[str] = []  # every model that actually served a discovery turn (fallbacks)
    upstream_providers: list[str] = []  # via a router (OpenRouter): who actually ran the model
    generation_ids: list[str] = []  # the router's per-turn ids, to audit the run with the router
    validated_runs: list[str] = []
    approved_by: str | None = None
    approved_at: str | None = None
    approved_hash: str | None = None


# Fields that describe the artifact's lifecycle rather than its behaviour. Excluded from the
# content hash so approving an artifact doesn't change the thing that was approved.
_HASH_EXCLUDE: dict[str, Any] = {"capability": {"status"}, "provenance": True}


class CapabilityArtifact(Model):
    schema_version: Literal["1.0"]
    capability: CapabilityInfo
    app: AppInfo
    params: list[ParamSpec] = []
    secrets: list[SecretSpec] = []
    outputs: list[OutputSpec] = []
    preconditions: list[str] = []
    conditions: list[Annotated[str, Field(pattern=CONDITION_REF)]] = []
    outcomes: list[OutcomeSpec] = []
    steps: list[Step] = Field(min_length=1)
    success: Checkpoint
    provenance: Provenance

    # -- integrity -----------------------------------------------------------

    @model_validator(mode="after")
    def _integrity(self) -> CapabilityArtifact:
        _unique("step id", [s.id for s in self.steps])
        _unique("param", [p.name for p in self.params])
        _unique("secret", [s.name for s in self.secrets])
        _unique("output", [o.name for o in self.outputs])
        _unique("outcome", [o.code for o in self.outcomes])

        phases = [s.phase for s in self.steps]
        if "login" in phases and phases.index("task") < max(
            i for i, p in enumerate(phases) if p == "login"
        ):
            raise ValueError("login steps must come before all task steps")

        params = {p.name for p in self.params}
        secrets = {s.name for s in self.secrets}
        for ref in _walk(self, (ParamRef, SecretValue)):
            if isinstance(ref, ParamRef) and ref.param not in params:
                raise ValueError(f"unknown param '{ref.param}'")
            if isinstance(ref, SecretValue) and ref.secret not in secrets:
                raise ValueError(f"unknown secret '{ref.secret}'")

        index = {s.id: i for i, s in enumerate(self.steps)}
        outputs = {o.name: o for o in self.outputs}
        for i, s in enumerate(self.steps):
            if s.action.kind == ActionKind.READ and s.action.output not in outputs:
                raise ValueError(f"step {s.id} reads unknown output '{s.action.output}'")
            if s.resume_anchor is not None:
                a = index.get(s.resume_anchor, i + 1)
                if a > i:
                    raise ValueError(f"step {s.id}: resume_anchor must name this or an earlier step")
                if any(self.steps[k].risk == Risk.IRREVERSIBLE for k in range(a, i)):
                    raise ValueError(f"step {s.id}: resume_anchor {s.resume_anchor} would re-run an irreversible step")
            if s.risk == Risk.IRREVERSIBLE and (s.target is None or s.target.min_agreement < 2):
                raise ValueError(f"irreversible step {s.id} needs min_agreement >= 2")
            if s.risk == Risk.IRREVERSIBLE and not any(
                st.kind in SEMANTIC_KINDS for st in s.target.strategies[: s.target.min_agreement]
            ):
                raise ValueError(f"irreversible step {s.id}: the agreeing strategies need a semantic one, "
                                 "not only positional css/xpath")
            if RISK_ORDER[s.risk] > RISK_ORDER[self.capability.risk]:
                raise ValueError(f"capability risk '{self.capability.risk}' is lower than step {s.id} risk")
        for o in self.outputs:
            src = self.steps[index[o.source_step]] if o.source_step in index else None
            if src is None or src.action.kind != ActionKind.READ or src.action.output != o.name:
                raise ValueError(f"output '{o.name}' must come from a read step that reads it")
        for oc in self.outcomes:
            for sid in oc.at_steps:
                if sid not in index:
                    raise ValueError(f"outcome '{oc.code}' names unknown step {sid}")
        return self

    # -- helpers -------------------------------------------------------------

    @property
    def ref(self) -> str:
        return f"{self.capability.id}@{self.capability.version}"

    def step_index(self, step_id: str) -> int:
        for i, s in enumerate(self.steps):
            if s.id == step_id:
                return i
        raise KeyError(step_id)

    def irreversible_steps(self) -> list[Step]:
        return [s for s in self.steps if s.risk == Risk.IRREVERSIBLE]

    def output_spec(self, name: str) -> OutputSpec | None:
        return next((o for o in self.outputs if o.name == name), None)

    def bind_params(self, raw: Mapping[str, Any]) -> dict[str, str | int | Decimal]:
        """Validate and type caller params against the contract, before the browser is touched."""
        declared = {p.name: p for p in self.params}
        for name in raw:
            if name not in declared:
                raise ParamError(name, "is not declared by this capability")
        bound: dict[str, str | int | Decimal] = {}
        for p in self.params:
            if raw.get(p.name) is None or str(raw[p.name]).strip() == "":
                if p.required:
                    raise ParamError(p.name, "is required")
                continue
            bound[p.name] = p.coerce(raw[p.name])
        return bound

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json", exclude_none=True), sort_keys=True, indent=2) + "\n"

    def content_hash(self) -> str:
        body = self.model_dump(mode="json", exclude_none=True, exclude=_HASH_EXCLUDE)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return f"sha256:{digest}"


def _unique(what: str, names: list[str]) -> None:
    seen: set[str] = set()
    for n in names:
        if n in seen:
            raise ValueError(f"duplicate {what} '{n}'" if what != "step id" else f"duplicate step id '{n}'")
        seen.add(n)


def _walk(obj: Any, types: tuple[type, ...]) -> Iterator[Any]:
    if isinstance(obj, types):
        yield obj
    if isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            yield from _walk(getattr(obj, name), types)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _walk(item, types)


# --- versioning -------------------------------------------------------------

_CONTRACT = ("params", "secrets", "outputs", "outcomes", "preconditions")
_BEHAVIOUR = ("steps", "success", "conditions", "app")


def required_bump(old: CapabilityArtifact, new: CapabilityArtifact) -> Literal["major", "minor", "patch", "none"]:
    """The smallest semver bump that honestly describes the change from ``old`` to ``new``.

    major: what callers pass or get back changed. minor: how it's done changed. patch: metadata.
    """
    a, b = old.model_dump(mode="json"), new.model_dump(mode="json")
    if any(a[k] != b[k] for k in _CONTRACT) or a["capability"]["id"] != b["capability"]["id"]:
        return "major"
    if any(a[k] != b[k] for k in _BEHAVIOUR) or a["capability"]["risk"] != b["capability"]["risk"]:
        return "minor"
    if old.content_hash() != new.content_hash():
        return "patch"
    return "none"
