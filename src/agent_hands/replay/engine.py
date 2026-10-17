"""Deterministic replay: an artifact plus params → one ``ReplayResult``. No model anywhere.

Per step:

1. **pre-check**: run the detectors (fixed priority, below) and handle what they find
2. **gate**: irreversible steps need a bound approval (caller token, or a live operator decision)
3. **resolve**: wait, up to the step's timeout, for its target to resolve uniquely (with agreement)
4. **hit test**: if something covers the target, dismiss it if it's a known interstitial; otherwise
   it's an unexpected state and goes to a human
5. **act**, behind the policy gate and the session lease, then re-audit every frame URL
6. **checkpoint**: wait for the step's own checkpoint, running the detectors on every tick. On
   timeout, a document still loading is a slow load (one fixed retry on GET); a settled page that
   doesn't match is a checkpoint failure reported with expected and observed.

Detector priority, every tick, in this order: profile conditions with ``retry``/``re_login``
handlers (server error, session expiry) → the artifact's business outcomes bound to this step →
``dismiss`` conditions (known interstitials). The order is data, not chance.

Determinism: same artifact + params + app state ⇒ same sequence of actions. Waits are on
conditions with bounded timeouts; retry schedules are fixed; resolution never takes a first match.
Wall-clock time appears only in logs and in timeout bounds.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets as pysecrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from playwright.sync_api import ElementHandle, Frame
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from agent_hands.artifact.schema import (
    ActionKind,
    CapabilityArtifact,
    Checkpoint,
    Model,
    OutputType,
    ParamError,
    Risk,
    Status,
    Step,
    LiteralValue,
    TextVisible,
)
from agent_hands.evidence import RunLog
from agent_hands.handoff.lease import Lease, LeaseError, Owner
from agent_hands.handoff.operator import (
    ApprovalRequest,
    HandoffOutcome,
    HumanCapture,
    InterventionRequest,
    Operator,
    now,
)
from agent_hands.policy import CommitGuard, PolicyViolation
from agent_hands.profile import AppProfile, ConditionSpec, post_is_irreversible
from agent_hands.redact import DEFAULT_PATTERNS, Redactor, compile_patterns
from agent_hands.replay.result import (
    ApprovalRecord,
    Failed,
    Failure,
    FailureKind,
    HandoffRecord,
    NeedsHuman,
    Outcome,
    Recovery,
    Resolution,
    StepRecord,
    Succeeded,
)
from agent_hands.secrets import resolve_secret
from agent_hands.surface.locate import obscured_by, resolve
from agent_hands.surface.web import (
    BrowserSession,
    condition_holds,
    describe_condition,
    frames_in,
    norm,
    page_summary,
    value_of,
    visible_text,
)

# Approvals bind to params through an HMAC keyed per process, so a persisted params_hash can't be
# brute-forced back into a member number, and an approval can't be replayed against other params.
_APPROVAL_KEY = pysecrets.token_bytes(32)


def params_digest(artifact_hash: str, params: Mapping[str, Any]) -> str:
    body = json.dumps({k: str(v) for k, v in sorted(params.items())}, separators=(",", ":"))
    return "hmac:" + hmac.new(_APPROVAL_KEY, f"{artifact_hash}|{body}".encode(), hashlib.sha256).hexdigest()[:32]


class Approval(Model):
    """An explicit, caller-supplied approval for one irreversible step of ONE invocation.

    Bound to the run id, the artifact's content hash and the params; used up when the step runs.
    """

    run_id: str
    step_id: str
    artifact_hash: str
    params_hash: str
    approver: str
    approved_at: str


def approve(
    artifact: CapabilityArtifact, step_id: str, raw_params: Mapping[str, Any], approver: str, *, run_id: str
) -> Approval:
    bound = artifact.bind_params(raw_params)
    h = artifact.content_hash()
    return Approval(run_id=run_id, step_id=step_id, artifact_hash=h, params_hash=params_digest(h, bound),
                    approver=approver, approved_at=now())


@dataclass
class ReplayConfig:
    run_id: str
    origin: str  # tenant origin, e.g. http://127.0.0.1:8341
    poll_ms: int = 150
    slow_retries: int = 1
    max_handoffs: int = 2
    approvals: list[Approval] = field(default_factory=list)
    allow_draft: bool = False  # only the post-compile validation replay runs drafts
    secret_overrides: dict[str, str] = field(default_factory=dict)  # secret name → ref (tenant config)
    nav_timeout_ms: int = 15_000  # entry navigation and frame reloads
    dismiss_timeout_ms: int = 3_000  # clicking a known interstitial's dismiss control


# Control flow inside the engine. Not part of the public contract.
class _Stop(Exception):
    def __init__(self, result_kind: str, **data: Any) -> None:
        self.result_kind, self.data = result_kind, data


class _Jump(Exception):
    def __init__(self, index: int) -> None:
        self.index = index


class _Retry(Exception):
    pass


ESCALATE_ONLY = {"unexpected_state", "commit_state_unknown"}
ESCALATABLE_FAILURES = {FailureKind.CHECKPOINT_FAILED, FailureKind.TARGET_NOT_FOUND, FailureKind.TARGET_AMBIGUOUS}


class ReplayEngine:
    def __init__(
        self,
        artifact: CapabilityArtifact,
        profile: AppProfile,
        session: BrowserSession,
        config: ReplayConfig,
        *,
        operator: Operator | None = None,
        run_dir: Path | None = None,
        secret_resolver: Callable[[str], str] = resolve_secret,
    ) -> None:
        self.a, self.profile, self.session, self.cfg = artifact, profile, session, config
        self.operator = operator
        self.run_dir = run_dir
        self.resolve_secret = secret_resolver
        self.page = session.page
        assert self.page is not None, "start the browser session first"
        self.hash = artifact.content_hash()
        self.lease = Lease()
        self.epoch = self.lease.epoch
        self.redactor = Redactor(tuple((*DEFAULT_PATTERNS, *compile_patterns(profile.pii_patterns))))
        self.log = RunLog(run_dir, self.redactor, backend="replay" if artifact.provenance.provider != "fake" else "fake")
        self.capture = HumanCapture(self.lease, self.redactor)
        if operator is not None:
            assert session.context is not None
            self.capture.install(session.context)
        # Network backstop for the approval gate: an irreversible POST goes out only when armed.
        self.guard = CommitGuard(lambda path: post_is_irreversible(profile, path),
                                 lambda: self.lease.owner == Owner.HUMAN)
        if session.context is not None:
            self.guard.install(session.context)
        self.guard_seen = 0
        self.params: dict[str, Any] = {}
        self.secrets: dict[str, str] = {}
        self.outputs: dict[str, Any] = {}
        self.records: list[StepRecord] = []
        self.recoveries: list[Recovery] = []
        self.handoffs: list[HandoffRecord] = []
        self.approvals: list[ApprovalRecord] = []
        self.budget_used: dict[str, int] = {}
        self.slow_retries_used = 0
        # Per irreversible step: "in_doubt" from the moment it's clicked, "committed" once its checkpoint holds.
        self.commits: dict[str, str] = {}
        self.gate_passed: set[str] = set()
        self.used_approvals: set[int] = set()
        self.patterns = (*DEFAULT_PATTERNS, *compile_patterns(profile.pii_patterns))
        self.iterations = 0
        conds = [profile.condition(c) for c in artifact.conditions if c in {x.id for x in profile.conditions}]
        self.hard_conditions = [c for c in conds if c.handler != "dismiss"]
        self.dismiss_conditions = [c for c in conds if c.handler == "dismiss"]

    # ------------------------------------------------------------------ public

    def run(self, raw_params: Mapping[str, Any]):
        t0 = time.monotonic()
        started = now()
        self.log.event("replay_start", capability=self.a.ref, artifact_hash=self.hash, run_id=self.cfg.run_id)
        try:
            self._check_compatible()
            try:
                self.params = self.a.bind_params(raw_params)
            except ParamError as e:
                raise _Stop("failed", failure=Failure(
                    kind=FailureKind.PARAM_INVALID, step_id=None,
                    expected="params matching the capability contract", observed=str(e)))
            self.secrets = {s.name: self.resolve_secret(self.cfg.secret_overrides.get(s.name, s.ref)) for s in self.a.secrets}
            self.redactor = Redactor.from_contract(self.a, params=self.params, secrets=self.secrets, patterns=self.patterns)
            self.log.redactor = self.redactor
            self.capture.redactor = self.redactor
            self._execute()
            self._final_checkpoint()
            result_kind, data = "succeeded", {"outputs": dict(self.outputs)}
        except _Stop as s:
            result_kind, data = s.result_kind, s.data
        except PolicyViolation as e:
            result_kind, data = "failed", {"failure": Failure(
                kind=FailureKind.POLICY_DENIED, step_id=self._current_step_id(), expected=f"stay within allowlist",
                observed=e.detail)}
        except PlaywrightTimeout as e:
            result_kind, data = "failed", {"failure": Failure(
                kind=FailureKind.TIMEOUT, step_id=self._current_step_id(), expected="the browser to respond in time",
                observed=self.redactor.scrub_exception(e)[:300])}
        except LeaseError as e:
            result_kind, data = "failed", {"failure": Failure(
                kind=FailureKind.UNEXPECTED_STATE, step_id=self._current_step_id(), expected="automation holds the session",
                observed=str(e))}
        except Exception as e:  # the engine always returns a result; never a raw traceback with data in it
            result_kind, data = "failed", {"failure": Failure(
                kind=FailureKind.UNEXPECTED_STATE, step_id=self._current_step_id(), expected="no internal error",
                observed=self.redactor.scrub_exception(e)[:500])}
        return self._finish(result_kind, data, started, t0)

    # --------------------------------------------------------------- control

    def _check_compatible(self) -> None:
        problems = []
        if self.a.app.profile != self.profile.profile:
            problems.append(f"artifact is for profile '{self.a.app.profile}', not '{self.profile.profile}'")
        missing = [c for c in self.a.conditions if c not in {x.id for x in self.profile.conditions}]
        if missing:
            problems.append(f"profile lacks conditions {missing}")
        if self.a.capability.status == Status.DEPRECATED:
            problems.append("artifact is deprecated")
        if self.a.capability.status == Status.DRAFT and not self.cfg.allow_draft:
            problems.append("artifact is a draft; only the validation replay may run it")
        if problems:
            raise _Stop("failed", failure=Failure(
                kind=FailureKind.ARTIFACT_INCOMPATIBLE, step_id=None,
                expected="a validated artifact for this app profile", observed="; ".join(problems)))

    def _execute(self) -> None:
        self._goto_entry()
        i = 0
        guard = len(self.a.steps) * 12
        while i < len(self.a.steps):
            self.iterations += 1
            if self.iterations > guard:
                raise _Stop("failed", failure=Failure(
                    kind=FailureKind.UNEXPECTED_STATE, step_id=self.a.steps[i].id,
                    expected="forward progress", observed="loop guard tripped: too many retries/jumps"))
            self._current = i
            try:
                self._run_step(i)
                i += 1
            except _Jump as j:
                self.log.event("jump", to=self.a.steps[j.index].id)
                i = j.index
            except _Retry:
                self.log.event("retry_step", step_id=self.a.steps[i].id)

    def _current_step_id(self) -> str | None:
        i = getattr(self, "_current", None)
        return None if i is None else self.a.steps[i].id

    def _goto_entry(self) -> None:
        url = self.cfg.origin + self.a.app.entry_route
        self.session.policy.require_action(ActionKind.NAVIGATE, url)
        self.lease.assert_automation(self.epoch)
        self.page.goto(url, timeout=self.cfg.nav_timeout_ms)

    # ------------------------------------------------------------------ steps

    def _run_step(self, i: int) -> None:
        step = self.a.steps[i]
        t = time.monotonic()
        self._check(step, "pre")
        if step.risk == Risk.IRREVERSIBLE:
            self._gate(step)
        resolution: Resolution | None = None
        if step.action.kind == ActionKind.NAVIGATE:
            assert step.action.route is not None
            url = self.cfg.origin + step.action.route
            self.session.policy.require_action(ActionKind.NAVIGATE, url)
            self.lease.assert_automation(self.epoch)
            self.page.goto(url, timeout=step.expect.timeout_ms)
        else:
            got = self._resolve_waiting(i, step)
            if got is None:
                return self._record(step, "completed_by_human", None, t)
            handle, frame, resolution = got
            cover = obscured_by(handle)
            if cover:
                self.log.event("obscured", step_id=step.id, by=cover)
                if self._dismiss_known(step):
                    raise _Retry
                verdict = self._escalate(
                    i, step, "unexpected_state", f"target covered by: {cover}",
                    ask=f"Clear what is covering '{step.target.description}' so the page is ready for: {step.intent}. "
                        "Don't do the step itself.",
                    failure=None)
                if verdict == "completed":
                    return self._record(step, "completed_by_human", resolution, t)
                raise _Retry
            self.session.policy.require_action(step.action.kind, frame.url)
            self.lease.assert_automation(self.epoch)
            if step.risk == Risk.IRREVERSIBLE:
                if step.id in self.commits:  # belt and braces: the schema already forbids re-running a commit
                    raise _Stop("failed", failure=Failure(
                        kind=FailureKind.UNEXPECTED_STATE, step_id=step.id, intent=step.intent,
                        expected="each irreversible step runs at most once", observed="the step was about to run again"))
                self.log.screenshot(self.page, f"{step.id}-before-commit", self._masks())
                self.commits[step.id] = "in_doubt"
                self.guard.arm()
            blocked_before = len(self.guard.blocked)
            try:
                self._act(step, handle)
            finally:
                if step.risk == Risk.IRREVERSIBLE:
                    self.page.wait_for_timeout(0)  # let the route handler see the POST before disarming
            if len(self.guard.blocked) > blocked_before:  # only blocks caused by THIS action
                raise _Stop("failed", failure=Failure(
                    kind=FailureKind.POLICY_DENIED, step_id=step.id, intent=step.intent,
                    expected="irreversible submits only under an armed approval",
                    observed=f"commit guard blocked a submit to {self.guard.blocked[-1]}"))
            self.session.policy.audit_frames(self.page)
        verdict = self._await_checkpoint(i, step)
        if step.risk == Risk.IRREVERSIBLE:
            self.guard.disarm()
            self.commits[step.id] = "committed"
        self._record(step, "completed_by_human" if verdict == "completed" else "ok", resolution, t)

    def _record(self, step: Step, status: str, resolution: Resolution | None, t: float) -> None:
        rec = StepRecord(step_id=step.id, intent=step.intent, status=status, resolution=resolution,  # type: ignore[arg-type]
                         duration_ms=int((time.monotonic() - t) * 1000))
        self.records.append(rec)
        self.log.event("step", **rec.model_dump(mode="json"))

    def _resolve_waiting(self, i: int, step: Step) -> tuple[ElementHandle, Frame, Resolution] | None:
        assert step.target is not None
        deadline = time.monotonic() + step.expect.timeout_ms / 1000
        while True:
            r = resolve(self.page, step.target, self.params)
            if r.handle is not None and r.frame is not None:
                return r.handle, r.frame, r.resolution
            self._check(step, "pre")
            if time.monotonic() >= deadline:
                if self._slow_load(step):
                    deadline = time.monotonic() + step.expect.timeout_ms / 1000
                    continue
                failure = Failure(
                    kind=r.failure or FailureKind.TARGET_NOT_FOUND, step_id=step.id, intent=step.intent,
                    expected=f"{step.target.description}: one element via "
                             + ", ".join(s.kind for s in step.target.strategies)
                             + f" (min_agreement {step.target.min_agreement})",
                    observed=f"{r.detail}; attempts {[a.outcome for a in r.resolution.attempts]}; "
                             + page_summary(self.page),
                    screenshot=self.log.screenshot(self.page, f"{step.id}-target-missing", self._masks()))
                verdict = self._escalate(i, step, failure.kind.value,
                                         f"{failure.expected}; got {[a.outcome for a in r.resolution.attempts]}",
                                         ask=f"Get the page to where this step can run: {step.intent}. "
                                             "Don't do the step itself.", failure=failure)
                if verdict == "completed":
                    return None
                raise _Retry
            self.page.wait_for_timeout(self.cfg.poll_ms)

    def _act(self, step: Step, handle: ElementHandle) -> None:
        a, timeout = step.action, step.expect.timeout_ms
        if a.kind == ActionKind.CLICK:
            handle.click(timeout=timeout)
        elif a.kind == ActionKind.FILL:
            assert a.value is not None
            handle.fill(value_of(a.value, self.params, self.secrets), timeout=timeout)
        elif a.kind == ActionKind.SELECT:
            assert a.value is not None
            handle.select_option(label=value_of(a.value, self.params, self.secrets), timeout=timeout)
        elif a.kind == ActionKind.PRESS:
            assert a.key is not None
            handle.press(a.key, timeout=timeout)
        elif a.kind == ActionKind.READ:
            assert a.output is not None
            self._read(step, a.output, handle.inner_text())
        self.log.event("act", step_id=step.id, action=a.kind.value)

    def _read(self, step: Step, name: str, raw: str) -> None:
        spec = self.a.output_spec(name)
        assert spec is not None
        text = norm(raw)
        try:
            if spec.type == OutputType.DECIMAL:
                value: Any = Decimal(text.replace("$", "").replace(",", "").strip())
            elif spec.type == OutputType.INTEGER:
                value = int(text.replace(",", ""))
            else:
                value = text
        except (InvalidOperation, ValueError):
            raise _Stop("failed", failure=Failure(
                kind=FailureKind.CHECKPOINT_FAILED, step_id=step.id, intent=step.intent,
                expected=f"{name} readable as {spec.type.value}", observed=self.redactor.scrub(text)[:200])) from None
        self.outputs[name] = value
        self.redactor.add(name, value, spec.sensitivity)

    # ------------------------------------------------------------ checkpoints

    def _holds(self, cp: Checkpoint) -> bool:
        ok_all = all(condition_holds(self.page, c, self.params, self.secrets) for c in cp.all_of)
        ok_any = not cp.any_of or any(condition_holds(self.page, c, self.params, self.secrets) for c in cp.any_of)
        return ok_all and ok_any

    def _await_checkpoint(self, i: int, step: Step) -> str:
        deadline = time.monotonic() + step.expect.timeout_ms / 1000
        while True:
            self._check(step, "post")
            if self._holds(step.expect):
                return "ok"
            if time.monotonic() >= deadline:
                if self._slow_load(step):
                    deadline = time.monotonic() + step.expect.timeout_ms / 1000
                    continue
                failure = Failure(
                    kind=FailureKind.CHECKPOINT_FAILED, step_id=step.id, intent=step.intent,
                    expected=self._describe_cp(step.expect), observed=page_summary(self.page),
                    screenshot=self.log.screenshot(self.page, f"{step.id}-checkpoint-failed", self._masks()))
                if step.risk == Risk.IRREVERSIBLE and self.commits.get(step.id) == "in_doubt":
                    return self._escalate(i, step, "commit_state_unknown", "submitted, but the confirmation never appeared",
                                          ask="Check whether the commit landed. If it did, bring up its confirmation and "
                                              "resume; if not, abort.", failure=failure, allow_retry=False)
                verdict = self._escalate(i, step, "checkpoint_failed", failure.expected,
                                         ask=f"Finish this step by hand if it's safe: {step.intent}", failure=failure)
                if verdict == "retry":
                    raise _Retry
                return verdict
            self.page.wait_for_timeout(self.cfg.poll_ms)

    def _final_checkpoint(self) -> None:
        deadline = time.monotonic() + self.a.success.timeout_ms / 1000
        last = self.a.steps[-1]
        while True:
            self._check(last, "final")
            if self._holds(self.a.success):
                break
            if time.monotonic() >= deadline:
                raise _Stop("failed", failure=Failure(
                    kind=FailureKind.CHECKPOINT_FAILED, step_id="success", intent="final success condition",
                    expected=self._describe_cp(self.a.success), observed=page_summary(self.page),
                    screenshot=self.log.screenshot(self.page, "success-failed", self._masks())))
            self.page.wait_for_timeout(self.cfg.poll_ms)
        self.log.screenshot(self.page, "final", self._masks())

    @staticmethod
    def _describe_cp(cp: Checkpoint) -> str:
        parts = [describe_condition(c) for c in cp.all_of]
        if cp.any_of:
            parts.append("one of: " + " / ".join(describe_condition(c) for c in cp.any_of))
        return "; ".join(parts) + f" (within {cp.timeout_ms} ms)"

    # ------------------------------------------------------------- detectors

    def _detected(self, cond_list) -> bool:
        return any(condition_holds(self.page, c, self.params, self.secrets) for c in cond_list)

    def _check(self, step: Step, phase: str) -> None:
        if len(self.guard.blocked) > self.guard_seen:
            # A submit nobody approved was aborted at the network layer: input from outside the engine
            # (keystrokes or a click in the browser while automation holds it). Nothing was committed:
            # the request never left the browser. The page is now Chromium's blocked-request page.
            for path in self.guard.blocked[self.guard_seen:]:
                self.log.event("network_blocked", by="commit_guard", method="POST", path=path, step_id=step.id)
            self.guard_seen = len(self.guard.blocked)
            i = self.a.step_index(step.id)
            anchor = self._anchor_for(i)
            if anchor is not None and self.budget_used.get("engine.unapproved_submit", 0) < 1:
                self.budget_used["engine.unapproved_submit"] = 1
                self._recover(step, "engine.unapproved_submit_blocked", "restored_from_anchor", 1)
                self._restore(anchor)  # sign on again, resume at the anchor; raises _Jump
            verdict = self._escalate(
                i, step, "unexpected_state",
                f"blocked an unapproved submit to {self.guard.blocked[-1]} (input from outside automation?)",
                ask=f"Nothing was committed. Bring the page back to where this step can run: {step.intent}. "
                    "Don't submit anything.",
                failure=Failure(kind=FailureKind.POLICY_DENIED, step_id=step.id, intent=step.intent,
                                expected="no submits except approved ones",
                                observed=f"commit guard blocked a submit to {self.guard.blocked[-1]}"))
            if verdict == "completed":
                raise _Jump(i + 1)
            raise _Retry
        for cond in self.hard_conditions:
            if self._detected(cond.detect):
                self._handle(cond, step, phase)
        for oc in self.a.outcomes:
            if (not oc.at_steps or step.id in oc.at_steps) and self._detected(oc.detect):
                message = ""
                if oc.message_from is not None:
                    r = resolve(self.page, oc.message_from, self.params)
                    message = norm(r.handle.inner_text()) if r.handle is not None else ""
                self.log.screenshot(self.page, f"{step.id}-outcome-{oc.code}", self._masks())
                raise _Stop("outcome", code=oc.code, message=message, at_step=step.id)
        for cond in self.dismiss_conditions:
            if self._detected(cond.detect):
                self._handle(cond, step, phase)

    def _dismiss_known(self, step: Step) -> bool:
        for cond in self.dismiss_conditions:
            if self._detected(cond.detect):
                self._handle(cond, step, "hit_test")
                return True
        return False

    def _in_doubt(self) -> bool:
        return "in_doubt" in self.commits.values()

    def _commit_state(self) -> str:
        if self._in_doubt():
            return "in_doubt"
        return "committed" if self.commits else "none"

    def _spend(self, cond: ConditionSpec, step: Step, kind: FailureKind) -> int:
        n = self.budget_used.get(cond.id, 0)
        if n >= cond.budget:
            raise _Stop("failed", failure=Failure(
                kind=kind, step_id=step.id, intent=step.intent,
                expected=f"{cond.id} to clear within {cond.budget} attempt(s)", observed=page_summary(self.page),
                screenshot=self.log.screenshot(self.page, f"{step.id}-{kind.value}", self._masks())))
        self.budget_used[cond.id] = n + 1
        return n

    def _handle(self, cond: ConditionSpec, step: Step, phase: str) -> None:
        self.log.event("condition", id=cond.id, step_id=step.id, phase=phase)
        if cond.handler in ("retry", "re_login") and self._in_doubt():
            # Never retry or re-login past a submitted commit: that is how accounts get opened twice.
            i = self.a.step_index(step.id)
            self._escalate(i, step, "commit_state_unknown",
                           f"{cond.id} after the irreversible step was submitted",
                           ask="The commit may or may not have landed. Check the member's accounts. If it was "
                               "opened, bring up its confirmation and resume; otherwise abort.",
                           failure=Failure(kind=FailureKind.COMMIT_STATE_UNKNOWN, step_id=step.id, intent=step.intent,
                                           expected=self._describe_cp(step.expect), observed=page_summary(self.page)),
                           allow_retry=False)
            for sid, state in self.commits.items():
                if state == "in_doubt":
                    self.commits[sid] = "committed"  # the human confirmed it landed (verified checkpoint)
            self._record(step, "completed_by_human", None, time.monotonic())
            raise _Jump(i + 1)
        if cond.handler == "retry":
            n = self._spend(cond, step, FailureKind.APP_ERROR)
            frames = sorted((f for f in frames_in(self.page, "any") if self._shows(cond, f)), key=lambda f: f.name)
            own = step.target.frame[-1].name if step.target is not None and step.target.frame else None
            target = next((f for f in frames if f.name == own), frames[0] if frames else self.page.main_frame)
            if self.session.last_method(target) not in (None, "GET"):
                raise _Stop("failed", failure=Failure(
                    kind=FailureKind.APP_ERROR, step_id=step.id, intent=step.intent,
                    expected="a GET to retry", observed="server error after a POST; not resubmitting"))
            backoff = cond.backoff_ms[min(n, len(cond.backoff_ms) - 1)] if cond.backoff_ms else 0
            self.page.wait_for_timeout(backoff)
            self.session.policy.require_action(ActionKind.NAVIGATE, target.url)
            self.lease.assert_automation(self.epoch)
            target.goto(target.url, timeout=self.cfg.nav_timeout_ms)
            self._recover(step, cond.id, "retried", n + 1)
        elif cond.handler == "re_login":
            n = self._spend(cond, step, FailureKind.SESSION_UNRECOVERABLE)
            if step.resume_anchor is None:
                raise _Stop("failed", failure=Failure(
                    kind=FailureKind.SESSION_UNRECOVERABLE, step_id=step.id, intent=step.intent,
                    expected="a resume anchor for this step", observed="session expired with no safe resume point"))
            anchor = self.a.step_index(step.resume_anchor)
            if any(self.a.step_index(sid) >= anchor for sid in self.commits):
                raise _Stop("failed", failure=Failure(
                    kind=FailureKind.SESSION_UNRECOVERABLE, step_id=step.id, intent=step.intent,
                    expected="a resume point after every submitted commit", observed="resuming would re-run a commit"))
            self._recover(step, cond.id, "re_login", n + 1)
            self._restore(anchor)
        elif cond.handler == "dismiss":
            n = self._spend(cond, step, FailureKind.UNEXPECTED_STATE)
            assert cond.dismiss is not None
            r = resolve(self.page, cond.dismiss, self.params)
            if r.handle is None or r.frame is None:
                return  # detected but not dismissable yet; next tick
            self.session.policy.require_action(ActionKind.CLICK, r.frame.url)
            self.lease.assert_automation(self.epoch)
            r.handle.click(timeout=self.cfg.dismiss_timeout_ms)
            self._recover(step, cond.id, "dismissed", n + 1)

    def _anchor_for(self, i: int) -> int | None:
        """Where it's safe to resume step i from: its own anchor, else the previous step's (a commit
        step has none; its form starts at the previous step's anchor). Never at or before a commit."""
        for k in (i, i - 1):
            if k >= 0 and self.a.steps[k].resume_anchor is not None:
                a = self.a.step_index(self.a.steps[k].resume_anchor)
                if not any(self.a.step_index(sid) >= a for sid in self.commits):
                    return a
        return None

    def _restore(self, anchor: int) -> None:
        """Get back to a known state: entry page, the login steps, then resume at ``anchor``."""
        self._goto_entry()
        for j, s in enumerate(self.a.steps):
            if s.phase == "login":
                self._run_step(j)
        raise _Jump(anchor)

    def _quiet_page(self) -> None:
        """Before waiting on a human: blur whatever has focus in every frame, so keystrokes that land
        in the browser window (it can take focus) go nowhere instead of submitting a filled form."""
        for f in self.page.frames:
            try:
                f.evaluate("() => { if (document.activeElement && document.activeElement.blur) "
                           "document.activeElement.blur(); }")
            except Exception:
                pass

    def _shows(self, cond: ConditionSpec, frame: Frame) -> bool:
        """Which frame is showing a text condition (to reload that frame, not the whole page)."""
        body = norm(visible_text(frame))
        literals = [norm(c.text.literal) for c in cond.detect
                    if isinstance(c, TextVisible) and isinstance(c.text, LiteralValue) and c.text.literal.strip()]
        return any(t in body for t in literals)

    def _slow_load(self, step: Step) -> bool:
        """At a timeout: if a document is still loading, retry it once (GET only). True = retried."""
        pending = self.session.pending_document()
        if pending is None:
            return False
        frame, method, url = pending
        if method != "GET" or self._in_doubt():
            return False
        if self.slow_retries_used >= self.cfg.slow_retries:
            raise _Stop("failed", failure=Failure(
                kind=FailureKind.TIMEOUT, step_id=step.id, intent=step.intent,
                expected=f"page load within {step.expect.timeout_ms} ms (after {self.cfg.slow_retries} retry)",
                observed=f"document still loading: {self.redactor.scrub(url)}"))
        self.slow_retries_used += 1
        self._recover(step, "engine.slow_load", "retried", self.slow_retries_used)
        self.session.policy.require_action(ActionKind.NAVIGATE, url)
        self.lease.assert_automation(self.epoch)
        try:
            frame.goto(url, timeout=step.expect.timeout_ms)
        except PlaywrightError:
            raise _Stop("failed", failure=Failure(
                kind=FailureKind.TIMEOUT, step_id=step.id, intent=step.intent,
                expected=f"page load within {step.expect.timeout_ms} ms", observed="retry also timed out")) from None
        return True

    def _recover(self, step: Step, condition: str, action: str, attempt: int = 1) -> None:
        rec = Recovery(step_id=step.id, condition=condition, action=action, attempt=attempt)
        self.recoveries.append(rec)
        self.log.event("recovery", **rec.model_dump(mode="json"))

    # ------------------------------------------------------- approval gate

    def _gate(self, step: Step) -> None:
        assert step.target is not None
        if step.id in self.gate_passed:  # e.g. a retry after dismissing an overlay, before the click
            return
        ph = params_digest(self.hash, self.params)
        for n, ap in enumerate(self.cfg.approvals):
            if n in self.used_approvals:
                continue
            if (ap.run_id, ap.step_id, ap.artifact_hash, ap.params_hash) == (self.cfg.run_id, step.id, self.hash, ph):
                self.used_approvals.add(n)
                self.gate_passed.add(step.id)
                return self._approved(step, "approved", ap.approver, ap.approved_at, "caller_token", ph)
        if self.operator is None:
            self.log.event("approval_required", step_id=step.id)
            raise _Stop("needs_human", reason="approval_required", step_id=step.id,
                        detail=f"irreversible step '{step.intent}' needs an explicit approval bound to this artifact and params")
        self._quiet_page()
        decision = self.operator.approve(ApprovalRequest(
            run_id=self.cfg.run_id, step_id=step.id, intent=step.intent, target=step.target.description,
            bound_to=self.hash, summary=self._commit_summary()))  # terminal only; never persisted
        self._approved(step, decision.decision, decision.approver, decision.at, self.operator.name, ph)
        if decision.decision == "approved":
            self.gate_passed.add(step.id)
        else:
            raise _Stop("failed", failure=Failure(
                kind=FailureKind.APPROVAL_REJECTED, step_id=step.id, intent=step.intent,
                expected="operator approval", observed=f"rejected by {decision.approver}"))

    def _approved(self, step: Step, decision: str, approver: str, at: str, channel: str, ph: str) -> None:
        assert step.target is not None
        rec = ApprovalRecord(step_id=step.id, intent=step.intent, target=step.target.description, decision=decision,  # type: ignore[arg-type]
                             approver=approver, approved_at=at, channel=channel, bound_to=self.hash, params_hash=ph)
        self.approvals.append(rec)
        self.log.event("approval", **rec.model_dump(mode="json"))

    def _commit_summary(self) -> str:
        """What the approver sees: identifiers partly masked, amounts and choices exact.

        Shown on the operator's terminal only; the persisted ApprovalRecord carries no values.
        """
        shown = []
        for spec in self.a.params:
            v = self.params.get(spec.name)
            if v is None:
                continue
            if spec.sensitivity.value == "pii" and isinstance(v, str):
                v = "•" * max(0, len(v) - 2) + v[-2:]
            shown.append(f"{spec.name}={v}")
        return f"{self.a.capability.title}: " + ", ".join(shown)

    # ---------------------------------------------------------- escalation

    def _masks(self) -> list[tuple[list[str], str]]:
        return [([r.name for r in reg.frame if r.name], reg.css) for reg in self.profile.sensitive_regions]

    def _escalate(
        self, i: int, step: Step, reason: str, detail: str, *, ask: str, failure: Failure | None, allow_retry: bool = True
    ) -> str:
        """Hand the live session to a human. Returns "completed" (human did the step) or "retry".

        Without an operator: escalate-only reasons become ``needs_human``; failures stop as failures.
        """
        if self.operator is None or len(self.handoffs) >= self.cfg.max_handoffs or (
            failure is not None and failure.kind not in ESCALATABLE_FAILURES and reason not in ESCALATE_ONLY
        ):
            if reason in ESCALATE_ONLY:
                req = self._intervention(step, reason, detail, ask)
                raise _Stop("needs_human", reason=reason, step_id=step.id, intervention_id=req.id, detail=detail)
            assert failure is not None
            raise _Stop("failed", failure=failure)

        for attempt in (1, 2):
            req = self._intervention(step, reason, detail, ask)
            if self.lease.owner == Owner.AUTOMATION:
                self.lease.transition(Owner.AWAITING_HUMAN, "automation", reason)
            self.capture.arm(self.page)
            before = len(self.capture.actions)
            ctx = _Ctx(self)
            outcome: HandoffOutcome = self.operator.handle(req, ctx)
            if self.lease.owner == Owner.AWAITING_HUMAN:
                self.lease.transition(Owner.VERIFYING if outcome.resolution == "resumed" else Owner.ABORTED,
                                      "operator", outcome.resolution)
            elif self.lease.owner == Owner.HUMAN:
                self.lease.transition(Owner.VERIFYING if outcome.resolution == "resumed" else Owner.ABORTED,
                                      outcome.taken_by or "human", outcome.resolution)
            actions = self.capture.actions[before:]
            self.log.write_json(f"handoff/{req.id}-human_actions.json", [a.model_dump() for a in actions])
            self.log.screenshot(self.page, f"{req.id}-after", self._masks())
            if outcome.resolution != "resumed":
                self._handoff_record(req, outcome, len(actions), False, f"operator {outcome.resolution}")
                raise _Stop("failed", failure=Failure(
                    kind=FailureKind.OPERATOR_ABORTED, step_id=step.id, intent=step.intent,
                    expected="operator resumes", observed=f"handoff {outcome.resolution}"))
            verdict, why = self._verify_after_handoff(i, step)
            if verdict == "retry" and not allow_retry:
                verdict, why = "failed", f"{why}, but this step must not be retried (commit state unknown)"
            self._handoff_record(req, outcome, len(actions), verdict != "failed", why)
            if verdict != "failed":
                self.epoch = self.lease.transition(Owner.AUTOMATION, "automation", f"verified: {why}")
                return verdict
            if attempt == 1:
                self.lease.transition(Owner.AWAITING_HUMAN, "automation", f"verification failed: {why}")
                detail = f"after handoff: {why}"
                continue
        self.lease.transition(Owner.ABORTED, "automation", "verification failed twice")
        raise _Stop("failed", failure=Failure(
            kind=FailureKind.UNEXPECTED_STATE, step_id=step.id, intent=step.intent,
            expected="after handoff: " + self._describe_cp(step.expect), observed=page_summary(self.page)))

    def _verify_after_handoff(self, i: int, step: Step) -> tuple[str, str]:
        """Don't trust where the human left things: allowlist, session, then the step itself.

        "retry" needs the target reachable AND the previous step's checkpoint to hold, so a human who
        left the browser on a different member (or screen) can't have automation carry on there.
        """
        try:
            self.session.policy.audit_frames(self.page)
        except PolicyViolation as e:
            return "failed", f"page outside allowlist ({e.detail})"
        for cond in self.hard_conditions:
            if cond.handler == "re_login" and self._detected(cond.detect):
                return "failed", "session expired during handoff"
        reachable = False
        if step.target is not None:
            r = resolve(self.page, step.target, self.params)
            reachable = r.handle is not None and obscured_by(r.handle) is None
        # A read's checkpoint ("the value is on screen") says nothing about whether it was read.
        if step.action.kind != ActionKind.READ and self._holds(step.expect):
            return "completed", f"step {step.id} checkpoint holds"
        if reachable:
            prev = self.a.steps[i - 1] if i > 0 else None
            if prev is not None and not self._holds(prev.expect):
                return "failed", f"step {step.id} target reachable, but step {prev.id}'s checkpoint no longer holds"
            return "retry", f"step {step.id} target reachable and step {prev.id if prev else '-'} still holds"
        return "failed", f"neither step {step.id} checkpoint nor a reachable target"

    def _intervention(self, step: Step, reason: str, detail: str, ask: str) -> InterventionRequest:
        n = len(self.handoffs) + 1
        iid = f"INT-{self.cfg.run_id}-{n}"
        shot = self.log.screenshot(self.page, f"{iid}-before", self._masks())
        req = InterventionRequest(
            id=iid, run_id=self.cfg.run_id, capability=self.a.ref, step_id=step.id, step_intent=step.intent,
            reason=reason, detail=self.redactor.scrub(detail), ask=ask,
            resume_check=self._describe_cp(step.expect) + f" (or the target of {step.id} is reachable)",
            screenshot=shot, page_summary=self.redactor.scrub(page_summary(self.page, 300)), requested_at=now())
        self.log.write_json(f"handoff/{iid}.json", req.model_dump())
        self.log.event("intervention", id=iid, reason=reason, step_id=step.id)
        return req

    def _handoff_record(self, req: InterventionRequest, out: HandoffOutcome, n_actions: int, ok: bool, why: str) -> None:
        rec = HandoffRecord(intervention_id=req.id, step_id=req.step_id, reason=req.reason, requested_at=req.requested_at,
                            taken_by=out.taken_by, taken_at=out.taken_at, released_at=out.released_at,
                            resolution=out.resolution, human_actions=n_actions, verified=ok, verification=why)
        self.handoffs.append(rec)
        self.log.event("handoff", **rec.model_dump(mode="json"))

    # --------------------------------------------------------------- result

    def _finish(self, kind: str, data: dict[str, Any], started: str, t0: float):
        common = dict(
            run_id=self.cfg.run_id, capability=self.a.ref, artifact_hash=self.hash, started_at=started,
            duration_ms=int((time.monotonic() - t0) * 1000),
            params={k: self.redactor.scrub(str(v)) for k, v in self.params.items()},
            steps=self.records, recoveries=self.recoveries, handoffs=self.handoffs, approvals=self.approvals,
            commit_state=self._commit_state(), evidence_dir=str(self.run_dir) if self.run_dir else None,
        )
        if "failure" in data:
            f: Failure = data["failure"]
            kind_ = f.kind
            observed = self.redactor.scrub(f.observed)
            if self._in_doubt() and kind_ != FailureKind.COMMIT_STATE_UNKNOWN:
                observed = f"{kind_.value} while a commit was in doubt: {observed}"
                kind_ = FailureKind.COMMIT_STATE_UNKNOWN
            data = {**data, "failure": f.model_copy(update={
                "kind": kind_, "observed": observed, "expected": self.redactor.scrub(f.expected),
                "after_commit": bool(self.commits)})}
        if "message" in data:
            data = {**data, "message": self.redactor.scrub(data["message"])}
        if "detail" in data:
            data = {**data, "detail": self.redactor.scrub(data["detail"])}
        if kind == "succeeded":
            result: Any = Succeeded(**common, **data)
        elif kind == "outcome":
            result = Outcome(**common, **data)
        elif kind == "needs_human":
            result = NeedsHuman(**common, **data)
        else:
            result = Failed(**common, **data)
        for d in self.session.policy.blocked:
            self.log.event("network_blocked", by="allowlist", rule=d.rule, detail=d.detail)
        self.log.event("replay_end", status=result.status)
        self.log.write_json("result.json", result.persisted(self.a, self.redactor), already_redacted=True)
        if self.lease.log:
            self.log.write_json("lease.json", [e.__dict__ for e in self.lease.log])
        return result


class _Ctx:
    """What an operator gets: the live page, a way to take the lease, and an event pump."""

    def __init__(self, engine: ReplayEngine) -> None:
        self.engine = engine
        self.page = engine.page

    def take(self, by: str) -> None:
        self.engine.lease.transition(Owner.HUMAN, by, "operator took control")
        self.engine.log.event("lease", owner="human", by=by)

    def pump(self, ms: int = 100) -> None:
        self.page.wait_for_timeout(ms)

