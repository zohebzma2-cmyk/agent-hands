"""Human-in-the-loop: intervention requests, operator channels and capture of what the human did.

The operator *console* is deliberately minimal (a terminal prompt, or a scripted operator in tests).
What's real is the mechanism: automation pauses on the same live browser, the lease moves to the
human, every trusted click/change/submit they make is captured (redacted), and control comes back
only through verification.
"""

from __future__ import annotations

import datetime as dt
import getpass
import select
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TextIO

from playwright.sync_api import BrowserContext, Page

from agent_hands.artifact.schema import Model
from agent_hands.handoff.lease import Lease, Owner
from agent_hands.redact import Redactor, mask_cells


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")


class InterventionRequest(Model):
    id: str
    run_id: str
    capability: str  # "id@version" for replay, "goal: ..." for discovery
    step_id: str | None
    step_intent: str | None
    reason: str  # machine code: unexpected_state, commit_state_unknown, checkpoint_failed, stuck, ...
    detail: str  # redacted
    ask: str  # what the human is asked to do, in plain words
    resume_check: str  # what automation will verify before continuing
    screenshot: str | None
    page_summary: str  # redacted
    requested_at: str


class ApprovalRequest(Model):
    run_id: str
    step_id: str
    intent: str
    target: str
    bound_to: str
    summary: str  # redacted description of what will be committed


class ApprovalDecision(Model):
    decision: Literal["approved", "rejected"]
    approver: str
    at: str


class HumanAction(Model):
    at: str
    kind: str  # click | change | submit
    frame: str
    path: str
    target: str  # redacted description
    value: str | None = None  # redacted; passwords never captured


@dataclass
class HandoffOutcome:
    resolution: Literal["resumed", "aborted", "timed_out"]
    taken_by: str | None = None
    taken_at: str | None = None
    released_at: str | None = None
    note: str = ""


# --- capture ------------------------------------------------------------------

_CAPTURE_JS = r"""
(() => {
  if (window.__handsCaptureInstalled) return;
  window.__handsCaptureInstalled = true;
  const norm = s => (s || '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();
  const describe = el => {
    const tag = (el.tagName || '').toLowerCase();
    const type = ((el.getAttribute && el.getAttribute('type')) || '').toLowerCase();
    const isBtn = tag === 'input' && ['button', 'submit', 'reset'].includes(type);
    const text = isBtn ? el.value : norm(el.innerText || '').slice(0, 80);
    const cell = el.closest && el.closest('td,th');
    const row = cell && cell.parentElement
      ? Array.from(cell.parentElement.children).map(c => norm(c.innerText)).join('\t').slice(0, 160) : '';
    const desc = `${tag}${type ? '[' + type + ']' : ''}${el.getAttribute && el.getAttribute('name') ? ' name=' + el.getAttribute('name') : ''}`;
    return {desc, text, row};
  };
  const send = (kind, el, value) => {
    try { window.__handsCapture({kind, frame: window.name || 'top', path: location.pathname, ...describe(el), value}); } catch (e) {}
  };
  document.addEventListener('click', e => { if (e.isTrusted) send('click', e.target, null); }, true);
  document.addEventListener('change', e => {
    if (!e.isTrusted) return;
    const el = e.target, type = ((el.getAttribute && el.getAttribute('type')) || '').toLowerCase();
    const v = type === 'password' ? '[MASKED]' : (el.tagName === 'SELECT' ? (el.selectedOptions[0] || {}).text : el.value);
    send('change', el, v);
  }, true);
  document.addEventListener('submit', e => send('submit', e.target, null), true);
})();
"""


@dataclass
class HumanCapture:
    """Records trusted input events, but only while the lease says a human holds the session."""

    lease: Lease
    redactor: Redactor
    actions: list[HumanAction] = field(default_factory=list)

    def install(self, context: BrowserContext) -> None:
        context.expose_binding("__handsCapture", self._on_event)
        context.add_init_script(_CAPTURE_JS)

    def arm(self, page: Page) -> None:
        """Make sure documents loaded before install also capture."""
        for f in page.frames:
            try:
                f.evaluate(_CAPTURE_JS)
            except Exception:
                pass

    def _on_event(self, source: Any, payload: dict[str, Any]) -> None:
        if self.lease.owner != Owner.HUMAN:
            return
        value = payload.get("value")
        text, row = mask_cells(str(payload.get("text") or "")), mask_cells(str(payload.get("row") or ""))
        target = str(payload.get("desc")) + (f' "{text}"' if text else "") + (
            f' in row "{row.replace(chr(9), " | ")}"' if row and row != text else "")
        self.actions.append(
            HumanAction(
                at=now(),
                kind=str(payload.get("kind")),
                frame=str(payload.get("frame")),
                path=str(payload.get("path")),
                target=self.redactor.scrub(target),
                value=None if value is None else self.redactor.scrub(mask_cells(str(value))),
            )
        )


# --- operator channel ---------------------------------------------------------


class HandoffContext(Protocol):
    page: Page

    def take(self, by: str) -> None: ...
    def pump(self, ms: int = 100) -> None: ...


class Operator(Protocol):
    name: str
    # May this operator approve irreversible steps, and commit during a handoff? Taking over the
    # session is separate. The engine treats a missing attribute as False.
    can_approve: bool

    def approve(self, req: ApprovalRequest) -> ApprovalDecision: ...
    def handle(self, req: InterventionRequest, ctx: HandoffContext) -> HandoffOutcome: ...


class TerminalOperator:
    """The deliberate mock of an operator console: the human uses the headed browser window,
    and talks to automation through this terminal."""

    name = "terminal"
    can_approve = True

    def __init__(self, stream_in: TextIO = sys.stdin, stream_out: TextIO = sys.stdout, timeout_s: int = 900) -> None:
        self.inp, self.out, self.timeout_s = stream_in, stream_out, timeout_s

    def _say(self, text: str) -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def approve(self, req: ApprovalRequest) -> ApprovalDecision:
        self._say(
            "\n" + "=" * 72 + "\n APPROVAL REQUIRED: irreversible step\n" + "=" * 72
            + f"\n step:    {req.step_id}  {req.intent}\n target:  {req.target}\n effect:  {req.summary}"
            + f"\n run:     {req.run_id}\n\n Keep the focus in this terminal; don't type into the browser window"
            + " (Enter in a filled form would try to submit it).\n Type 'approve' to let automation click it, or 'reject' to stop."
        )
        while True:
            self.out.write(" > ")
            self.out.flush()
            raw = self.inp.readline()
            if raw == "":  # end of input: nobody can answer, so the answer is no
                self._say(" No terminal input; treating as 'reject'.")
                return ApprovalDecision(decision="rejected", approver="no-terminal", at=now())
            line = raw.strip().lower()
            if line in ("approve", "reject"):
                return ApprovalDecision(
                    decision="approved" if line == "approve" else "rejected", approver=getpass.getuser(), at=now()
                )
            self._say(" Please type exactly 'approve' or 'reject'.")

    def _readline(self, ctx: HandoffContext, deadline: float) -> str | None:
        import time

        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.inp], [], [], 0)
            if ready:
                raw = self.inp.readline()
                return "abort" if raw == "" else raw.strip().lower()  # end of input means abort
            ctx.pump(200)  # keep the browser's event loop turning while we wait
        return None

    def handle(self, req: InterventionRequest, ctx: HandoffContext) -> HandoffOutcome:
        import time

        deadline = time.monotonic() + self.timeout_s
        self._say(
            "\n" + "=" * 72 + f"\n INTERVENTION {req.id}\n" + "=" * 72
            + f"\n why:      {req.reason}: {req.detail}\n step:     {req.step_id} {req.step_intent or ''}"
            + f"\n asked:    {req.ask}\n on return automation checks: {req.resume_check}"
            + (f"\n screenshot: {req.screenshot}" if req.screenshot else "")
            + "\n\n Type 'take' to take control of the browser window, or 'abort'."
        )
        while True:
            line = self._readline(ctx, deadline)
            if line is None:
                return HandoffOutcome("timed_out")
            if line == "abort":
                return HandoffOutcome("aborted", note="aborted before taking control")
            if line == "take":
                break
            self._say(" Type 'take' or 'abort'.")
        by = getpass.getuser()
        ctx.take(by)
        taken_at = now()
        self._say(" You have control. Work in the browser window. Type 'resume' when done, or 'abort'.")
        while True:
            line = self._readline(ctx, deadline)
            if line is None:
                return HandoffOutcome("timed_out", by, taken_at, now())
            if line in ("resume", "abort"):
                return HandoffOutcome("resumed" if line == "resume" else "aborted", by, taken_at, now())
            self._say(" Type 'resume' or 'abort'.")


class ScriptedOperator:
    """Tests only: a 'human' scripted as a function that drives the same live page."""

    name = "scripted"
    can_approve = True

    def __init__(
        self,
        act: Callable[[Page], None] | None = None,
        *,
        approve: bool = True,
        resolution: Literal["resumed", "aborted"] = "resumed",
    ) -> None:
        self.act, self.approve_all, self.resolution = act, approve, resolution
        self.requests: list[InterventionRequest] = []
        self.approvals: list[ApprovalRequest] = []

    def approve(self, req: ApprovalRequest) -> ApprovalDecision:
        self.approvals.append(req)
        return ApprovalDecision(decision="approved" if self.approve_all else "rejected", approver="scripted-test", at=now())

    def handle(self, req: InterventionRequest, ctx: HandoffContext) -> HandoffOutcome:
        self.requests.append(req)
        ctx.take("scripted-human")
        taken = now()
        if self.act is not None:
            self.act(ctx.page)
            ctx.pump(300)
        return HandoffOutcome(self.resolution, "scripted-human", taken, now())
