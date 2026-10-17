"""The replay result contract: what a calling agent gets back.

Exactly one of four terminal statuses:

- ``succeeded``    the capability did its job; typed outputs are attached.
- ``outcome``      a legitimate business answer ("no such member"). Not a crash, not retryable.
- ``failed``       a hard failure, with the step, what was expected and what was observed.
- ``needs_human``  a person must act (approve an irreversible step, resolve an unknown state).

Recoverable conditions (a dismissed dialog, a re-login, a retried slow load) are never a status.
They're handled and listed in ``recoveries`` on whatever the final result is. Which locator
strategy matched on every step is recorded too; anything below the primary is surfaced as ``drift``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, computed_field, model_validator

from agent_hands.redact import mask_value

if TYPE_CHECKING:
    from agent_hands.artifact.schema import CapabilityArtifact
    from agent_hands.redact import Redactor


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _drop_computed(*names: str):
    """Computed fields are serialised; ignore them when parsing a result back."""

    def strip(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if k not in names}
        return data

    return model_validator(mode="before")(classmethod(strip))


# --- per-step telemetry -----------------------------------------------------

AttemptOutcome = Literal["unique", "none", "ambiguous", "disagree", "not_tried"]


class StrategyAttempt(Model):
    index: int
    kind: str
    outcome: AttemptOutcome


class Resolution(Model):
    """How one target was resolved: every strategy tried, and which one was used."""

    description: str
    used_index: int | None
    used_kind: str | None
    attempts: list[StrategyAttempt]
    agreed: list[int] = []  # strategy indices that resolved to the same element


class StepRecord(Model):
    step_id: str
    intent: str
    status: Literal["ok", "completed_by_human", "failed", "outcome", "blocked"]
    resolution: Resolution | None = None
    duration_ms: int
    attempts: int = 1


class Recovery(Model):
    step_id: str | None
    condition: str  # e.g. "cu_core.announcement@1"
    action: str  # e.g. "dismissed", "re_login", "retried"
    attempt: int


class Drift(Model):
    step_id: str
    target: str
    primary_kind: str
    primary_outcome: AttemptOutcome
    used_index: int
    used_kind: str


class ApprovalRecord(Model):
    """Who approved an irreversible step, when, and exactly what it was bound to."""

    step_id: str
    intent: str
    target: str
    decision: Literal["approved", "rejected"]
    approver: str
    approved_at: str
    channel: str  # "terminal", "caller_token"
    bound_to: str  # artifact hash (replay) or run id (discovery)
    params_hash: str | None = None


class HandoffRecord(Model):
    intervention_id: str
    step_id: str | None
    reason: str
    requested_at: str
    taken_by: str | None = None
    taken_at: str | None = None
    released_at: str | None = None
    resolution: Literal["resumed", "aborted", "timed_out"]
    human_actions: int = 0
    verified: bool = False
    verification: str = ""


# --- failures ---------------------------------------------------------------


class FailureKind(StrEnum):
    PARAM_INVALID = "param_invalid"  # caught before the browser was touched
    ARTIFACT_INCOMPATIBLE = "artifact_incompatible"  # wrong schema/profile version or status
    POLICY_DENIED = "policy_denied"  # allowlist said no
    TARGET_NOT_FOUND = "target_not_found"  # no strategy matched: likely UI drift
    TARGET_AMBIGUOUS = "target_ambiguous"  # strategies matched several elements
    TARGET_DISAGREEMENT = "target_disagreement"  # strategies matched different elements
    CHECKPOINT_FAILED = "checkpoint_failed"  # acted, but the page isn't what the step expects
    UNEXPECTED_STATE = "unexpected_state"  # page matches nothing known
    APP_ERROR = "app_error"  # server error persisted past the retry budget
    TIMEOUT = "timeout"  # slow load persisted past the retry budget
    SESSION_UNRECOVERABLE = "session_unrecoverable"  # re-login failed or budget spent
    COMMIT_STATE_UNKNOWN = "commit_state_unknown"  # an irreversible action may or may not have landed
    APPROVAL_REJECTED = "approval_rejected"
    OPERATOR_ABORTED = "operator_aborted"


RETRYABLE = frozenset({FailureKind.APP_ERROR, FailureKind.TIMEOUT, FailureKind.SESSION_UNRECOVERABLE})


class Failure(Model):
    kind: FailureKind
    step_id: str | None
    intent: str | None = None
    expected: str
    observed: str
    screenshot: str | None = None
    snapshot: str | None = None
    after_commit: bool = False  # an irreversible step was already submitted in this run

    _strip = _drop_computed("retryable")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def retryable(self) -> bool:
        """Could the *caller* reasonably try the same invocation again later?

        Never once a commit was attempted: retrying would commit a second time.
        """
        return self.kind in RETRYABLE and not self.after_commit


# --- results ----------------------------------------------------------------


class _Base(Model):
    run_id: str
    capability: str  # "id@version"
    artifact_hash: str
    started_at: str
    duration_ms: int
    params: dict[str, str]  # already redacted
    steps: list[StepRecord]
    recoveries: list[Recovery] = []
    handoffs: list[HandoffRecord] = []
    approvals: list[ApprovalRecord] = []
    # none: nothing irreversible was submitted; in_doubt: submitted, outcome unknown; committed: confirmed
    commit_state: Literal["none", "in_doubt", "committed"] = "none"
    evidence_dir: str | None = None

    _strip = _drop_computed("drift", "retryable")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def drift(self) -> list[Drift]:
        out = []
        for s in self.steps:
            r = s.resolution
            if r is None or r.used_index is None or r.used_index == 0 or not r.attempts:
                continue
            out.append(
                Drift(
                    step_id=s.step_id,
                    target=r.description,
                    primary_kind=r.attempts[0].kind,
                    primary_outcome=r.attempts[0].outcome,
                    used_index=r.used_index,
                    used_kind=r.used_kind or "",
                )
            )
        return out

    def persisted(self, artifact: CapabilityArtifact, redactor: Redactor) -> dict[str, Any]:
        """A JSON-safe copy fit for disk: outputs masked by contract, free text scrubbed."""
        data = self.model_dump(mode="json")
        if "outputs" in data:
            masked = {}
            for name, value in getattr(self, "outputs").items():
                spec = artifact.output_spec(name)
                masked[name] = mask_value(value, spec.redaction.value if spec else "mask")
            data["outputs"] = masked
        return redactor.scrub_obj(data)


class Succeeded(_Base):
    status: Literal["succeeded"] = "succeeded"
    outputs: dict[str, Any]


class Outcome(_Base):
    status: Literal["outcome"] = "outcome"
    code: str
    message: str = ""
    at_step: str
    details: dict[str, str] = {}


class Failed(_Base):
    status: Literal["failed"] = "failed"
    failure: Failure


class NeedsHuman(_Base):
    status: Literal["needs_human"] = "needs_human"
    reason: Literal["approval_required", "unexpected_state", "commit_state_unknown", "stuck"]
    step_id: str | None
    intervention_id: str | None = None
    detail: str = ""


ReplayResult = Annotated[Union[Succeeded, Outcome, Failed, NeedsHuman], Field(discriminator="status")]
_ADAPTER: TypeAdapter[ReplayResult] = TypeAdapter(ReplayResult)


def parse_result(text: str | bytes) -> Succeeded | Outcome | Failed | NeedsHuman:
    return _ADAPTER.validate_json(text)
