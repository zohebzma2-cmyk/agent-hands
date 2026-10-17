"""Discovery: an LLM accomplishes a goal on the live surface, and we keep a trace fit to compile.

observe → decide → act, until the model calls ``done`` or a stop condition hits (max steps, wall
clock, no progress, refusal, give-up). The model sees the page's accessibility snapshot (all frames,
element refs) plus screenshots on request, and acts by ref through a small tool set. Every action
goes through the same policy gate, session lease and risk classification as replay.

What the model doesn't get to decide: params and outputs (fixed by the spec), risk (profile rules),
secret values (it passes names; values are filled in here), and whether an irreversible click
happens (a human approves it).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from playwright.sync_api import ElementHandle, Page

from agent_hands.artifact.schema import (
    ActionKind,
    LiteralValue,
    OutputType,
    ParamError,
    ParamRef,
    Risk,
    SecretValue,
    Sensitivity,
    Strategy,
    Value,
)
from agent_hands.artifact.spec import CapabilitySpec
from agent_hands.evidence import RunLog
from agent_hands.handoff.lease import Lease, Owner
from agent_hands.handoff.operator import (
    ApprovalRequest,
    HumanCapture,
    InterventionRequest,
    Operator,
    now,
)
from agent_hands.llm.base import Provider, ToolCall
from agent_hands.evidence import mask_locators
from agent_hands.policy import CommitGuard, PolicyViolation
from agent_hands.profile import AppProfile, post_is_irreversible
from agent_hands.redact import DEFAULT_PATTERNS, Redactor, compile_patterns, mask_cells
from agent_hands.secrets import resolve_secret
from agent_hands.surface.describe import describe, role_and_name
from agent_hands.surface.locate import obscured_by, resolve, same_element
from agent_hands.surface.web import BrowserSession, norm, page_summary, route_of, visible_text

SYSTEM_PROMPT = """\
You operate a legacy credit-union teller web application to work out how to accomplish one task.
This is a synthetic test system: every member, account and credential is fake.

Your successful run will be compiled into a deterministic automation that replays without you, so:
- Take the direct path to the goal. Don't explore unrelated screens or open things "to check".
- Act only through the tools. Target elements by the ref shown in the latest snapshot ([ref=...]).
  Refs change after every action, so always use refs from the most recent snapshot.
- When you type or choose a value the task gives you, pass it as a param reference (param="member_id"),
  not as literal text. Credentials are secrets: pass secret="teller_password"; you never see their values.
- Use `read` on the element that shows each value the contract asks you to return.
- Give every action a short imperative `intent` written for a human reviewer, like
  "Search for the member by number".
- If an announcement or interstitial covers the page, dismiss it and carry on.
- Some actions commit changes (for example a submit that opens an account). The system will ask a
  human to approve those before they happen; if one is rejected, stop with give_up.
- If you're stuck, or the page needs something you shouldn't do (a supervisor PIN, for instance),
  call request_human with the reason.
- Call done once the goal is reached and every output has been read.

The snapshot is the page's accessibility tree across all frames (this app uses framesets). Table
layouts are common: a label is often a cell next to an unnamed textbox in the same row. Cells marked
[cursor=pointer] are clickable.
"""


def _tool(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": props, "required": required, "additionalProperties": False},
    }


REF = {"type": "string", "description": "Element ref from the latest snapshot, e.g. f4e14"}
INTENT = {"type": "string", "description": "Short imperative description of this step for a human reviewer"}

TOOLS = [
    _tool("click", "Click an element.", {"ref": REF, "intent": INTENT}, ["ref", "intent"]),
    _tool(
        "fill",
        "Type into a text box. Give exactly one of param, secret or literal.",
        {"ref": REF, "intent": INTENT, "param": {"type": "string"}, "secret": {"type": "string"},
         "literal": {"type": "string"}},
        ["ref", "intent"],
    ),
    _tool(
        "select",
        "Choose an option in a dropdown by its visible label. Give exactly one of param or literal.",
        {"ref": REF, "intent": INTENT, "param": {"type": "string"}, "literal": {"type": "string"}},
        ["ref", "intent"],
    ),
    _tool(
        "read",
        "Read the text of an element as one of the contract's outputs.",
        {"ref": REF, "intent": INTENT, "output": {"type": "string", "description": "Output name from the contract"}},
        ["ref", "intent", "output"],
    ),
    _tool("screenshot", "Get a screenshot of the current page.", {}, []),
    _tool("request_human", "Ask a human operator to take over the live browser.",
          {"reason": {"type": "string"}}, ["reason"]),
    _tool("done", "The goal is reached and all outputs are read.", {"summary": {"type": "string"}}, ["summary"]),
    _tool("give_up", "Stop: the goal can't be reached safely.", {"reason": {"type": "string"}}, ["reason"]),
]


@dataclass
class TraceStep:
    """One successful action, with everything the compiler needs. Never persisted raw."""

    index: int
    kind: ActionKind
    intent: str
    value: Value | None
    output: str | None
    role: str
    name: str | None
    strategies: list[Strategy]
    rejected: list[tuple[str, str]]
    frame_path: list[Any]
    risk: Risk
    risk_note: str
    before: dict[str, tuple[str, list[str]]]
    after: dict[str, tuple[str, list[str]]]
    interstitial: bool = False


@dataclass
class DiscoveryResult:
    status: str  # done | gave_up | stuck | max_steps | timeout | refused | error
    reason: str
    trace: list[TraceStep]
    outputs: dict[str, Any]
    turns: list[dict[str, Any]] = field(default_factory=list)
    run_dir: Path | None = None


class _Finish(Exception):
    def __init__(self, status: str, reason: str) -> None:
        self.status, self.reason = status, reason


class DiscoveryAgent:
    def __init__(
        self,
        spec: CapabilitySpec,
        raw_params: dict[str, Any],
        provider: Provider,
        session: BrowserSession,
        profile: AppProfile,
        *,
        origin: str,
        run_id: str,
        operator: Operator | None = None,
        run_dir: Path | None = None,
        max_steps: int = 40,
        max_seconds: int = 900,
    ) -> None:
        self.spec, self.provider, self.session, self.profile = spec, provider, session, profile
        self.origin, self.run_id, self.operator = origin, run_id, operator
        self.max_steps, self.max_seconds = max_steps, max_seconds
        self.page: Page = session.page  # type: ignore[assignment]
        self.params = self._bind(raw_params)
        self.secrets = {s.name: resolve_secret(s.ref) for s in spec.secrets}
        # Everything persisted about a discovery run goes through this. Beyond the contract it learns
        # every data-looking cell the model was shown (names, addresses, balances), because the
        # model's own words can repeat them.
        bare_amount = ("bare_amount", re.compile(r"\b\d{1,3}(?:,\d{3})*\.\d{2}\b"), "[AMOUNT]")
        self.redactor = Redactor((*DEFAULT_PATTERNS, *compile_patterns(profile.pii_patterns), bare_amount))
        for p in spec.params:
            self.redactor.add(p.name, self.params[p.name], p.sensitivity)
        for name, v in self.secrets.items():
            self.redactor.add(name, v, Sensitivity.SECRET)
        # What the model sees: the live page, minus secret values. Playwright's ARIA snapshot shows
        # typed values, password fields included, so observations are scrubbed of secrets.
        self.model_scrub = Redactor(patterns=())
        for name, v in self.secrets.items():
            self.model_scrub.add(name, v, Sensitivity.SECRET)
        self.log = RunLog(run_dir, self.redactor, backend=provider.name)  # refuses fake runs in evidence/
        self.run_dir = run_dir
        self.lease = Lease()
        self.capture = HumanCapture(self.lease, self.redactor)
        if operator is not None and session.context is not None:
            self.capture.install(session.context)
        self.guard = CommitGuard(lambda path: post_is_irreversible(profile, path),
                                 lambda: self.lease.owner == Owner.HUMAN)
        if session.context is not None:
            self.guard.install(session.context)
        self.trace: list[TraceStep] = []
        self.outputs: dict[str, Any] = {}
        self.turns: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.snapshot = ""
        self._stale = 0

    def _bind(self, raw: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for p in self.spec.params:
            if p.name not in raw:
                raise ParamError(p.name, "is required")
            out[p.name] = p.coerce(raw[p.name])
        for k in raw:
            if k not in out:
                raise ParamError(k, "is not declared by this capability")
        return out

    # ------------------------------------------------------------------ run

    def run(self) -> DiscoveryResult:
        t0 = time.monotonic()
        self.log.event("discovery_start", run_id=self.run_id, capability=self.spec.id, provider=self.provider.name,
                       model=self.provider.model)
        self.session.policy.require_action(ActionKind.NAVIGATE, self.origin + self.spec.entry_route)
        self.page.goto(self.origin + self.spec.entry_route)
        self.provider.start(SYSTEM_PROMPT, TOOLS)
        content: list[dict[str, Any]] = [{"type": "text", "text": self._briefing()}, *self._observe(screenshot=True)]
        status, reason = "max_steps", f"no done() within {self.max_steps} model turns"
        nudged = False
        try:
            for turn_no in range(1, self.max_steps + 1):
                if time.monotonic() - t0 > self.max_seconds:
                    raise _Finish("timeout", f"exceeded {self.max_seconds}s")
                turn = self.provider.step(content)
                self._log_turn(turn_no, turn)
                if turn.stop_reason == "refusal":
                    raise _Finish("refused", "the model declined the request")
                if turn.stop_reason == "max_tokens":  # tool inputs may be truncated: never run them
                    content = [self._result(c, "Your turn was cut off; nothing was executed. Try again, briefly.",
                                            True) for c in turn.tool_calls] or [
                        {"type": "text", "text": "Your turn was cut off. Continue, briefly."}]
                    continue
                if not turn.tool_calls:
                    if nudged:
                        raise _Finish("stuck", "the model stopped calling tools before done()")
                    nudged = True
                    content = [{"type": "text", "text": "Continue with a tool call; call done when the goal is reached."}]
                    continue
                results: list[dict[str, Any]] = []
                finish: _Finish | None = None
                for call in turn.tool_calls:
                    if finish is not None:  # answer every tool_use block, even after done()
                        results.append(self._result(call, "not executed: run already finished", True))
                        continue
                    try:
                        results.append(self._execute(call))
                    except _Finish as f:
                        finish = f
                        results.append(self._result(call, f"finished: {f.status}", False))
                if finish is not None:
                    raise finish
                content = results + self._observe()
                if self._stale >= 4:
                    raise _Finish("stuck", "no progress across 4 consecutive actions")
        except _Finish as f:
            status, reason = f.status, f.reason
        except PolicyViolation as e:
            status, reason = "error", f"policy: {e.detail}"
        except Exception as e:  # provider outage, browser error: still record what happened
            status, reason = "error", self.redactor.scrub_exception(e)[:400]
        self.log.event("discovery_end", status=status, reason=reason, steps=len(self.trace))
        return DiscoveryResult(status, self.redactor.scrub(reason), self.trace, dict(self.outputs), self.turns, self.run_dir)

    def _briefing(self) -> str:
        goal = self.spec.goal
        for p in self.spec.params:
            goal = goal.replace("{" + p.name + "}", str(self.params[p.name]))
        lines = [f"Task: {goal}", "", "Contract params (refer to these by name with param=...):"]
        lines += [f"- {p.name}: {self.params[p.name]}  ({p.description})" for p in self.spec.params]
        lines += ["", "Secrets you may use (by name only, with secret=...):"]
        lines += [f"- {s.name}: {s.description}" for s in self.spec.secrets]
        lines += ["", "Outputs you must read (with read(output=...)):"]
        lines += [f"- {o.name} ({o.type.value}): {o.description}" for o in self.spec.outputs]
        return "\n".join(lines)

    # ------------------------------------------------------------ observation

    def _learn_observed(self) -> None:
        """Register every data-looking cell on screen as PII for persistence (see __init__)."""
        for f in self.page.frames:
            if f.is_detached():
                continue
            for line in visible_text(f).splitlines():
                for raw, masked in zip(line.split("\t"), mask_cells(line).split("\t")):
                    if masked.strip() == "▒" and len(raw.strip()) >= 3:
                        value = norm(raw)
                        variants = {value, value.lstrip("$ ")}
                        variants |= {part.strip() for part in value.split(",") if len(part.strip()) >= 4}
                        for v in variants:
                            self.redactor.add("observed", v, Sensitivity.PII)

    def _observe(self, screenshot: bool = False) -> list[dict[str, Any]]:
        snap = ""
        for attempt in range(3):
            try:
                snap = self.model_scrub.scrub(self.page.aria_snapshot(mode="ai", timeout=10_000))
                break
            except Exception:
                if attempt == 2:
                    raise
                self._settle()
        self._learn_observed()
        if len(snap) > 40_000:
            snap = snap[:40_000] + "\n... (snapshot truncated)"
        digest = hashlib.sha256(snap.encode()).hexdigest()[:12]
        self._stale = self._stale + 1 if snap == self.snapshot else 0
        self.snapshot = snap
        frames = ", ".join(f"{f.name or 'top'}={route_of(f.url)}" for f in self.page.frames if not f.is_detached())
        self.log.event("observe", frames=frames, snapshot_sha=digest, snapshot_lines=snap.count("\n") + 1)
        blocks: list[dict[str, Any]] = [{"type": "text", "text": f"Frames: {frames}\n\nSnapshot:\n{snap}"}]
        if screenshot:
            blocks.append(self._screenshot_block())
        return blocks

    def _screenshot_block(self) -> dict[str, Any]:
        # The model sees the real page except inputs holding secrets; evidence copies mask far more.
        masks = self._secret_inputs() + mask_locators(self.page, list(self.secrets.values()), structural=False)
        png = self.page.screenshot(timeout=10_000, mask=masks, mask_color="#222222")
        self.log.screenshot(self.page, "observe", [([r.name for r in reg.frame if r.name], reg.css)
                                                   for reg in self.profile.sensitive_regions])
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": base64.standard_b64encode(png).decode()}}

    def _secret_inputs(self) -> list[Any]:
        masks = []
        secret_values = set(self.secrets.values())
        for f in self.page.frames:
            if f.is_detached():
                continue
            try:
                kinds = f.evaluate("() => Array.from(document.querySelectorAll('input')).map(i => [i.type, i.value])")
            except Exception:
                continue
            for i, (typ, val) in enumerate(kinds):
                if typ == "password" or val in secret_values:
                    masks.append(f.locator("input").nth(i))
        return masks

    def _frames_state(self) -> dict[str, tuple[str, list[str]]]:
        state = {}
        for f in self.page.frames:
            if f.is_detached():
                continue
            lines = [norm(x) for x in visible_text(f).splitlines() if norm(x)]
            state[f.name or "top"] = (route_of(f.url), lines)
        return state

    def _settle(self) -> None:
        deadline = time.monotonic() + 10
        self.page.wait_for_timeout(250)
        while self.session.loading() and time.monotonic() < deadline:
            self.page.wait_for_timeout(100)
        for f in self.page.frames:
            try:
                f.wait_for_load_state("load", timeout=5000)
            except Exception:
                pass

    # ---------------------------------------------------------------- tools

    def _result(self, call: ToolCall, text: str, is_error: bool, image: dict | None = None) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": self.model_scrub.scrub(text)}]
        if image is not None:
            content.append(image)
        self.log.event("tool_result", tool=call.name, is_error=is_error, text=text[:400])
        return {"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": is_error}

    def _execute(self, call: ToolCall) -> dict[str, Any]:
        args = call.input
        self.log.event("tool_call", tool=call.name, args=args)
        name = call.name
        if name == "done":
            missing = [o.name for o in self.spec.outputs if o.name not in self.outputs]
            if missing:
                return self._result(call, f"Not done yet: outputs not read: {missing}", True)
            raise _Finish("done", str(args.get("summary", "")))
        if name == "give_up":
            raise _Finish("gave_up", str(args.get("reason", "")))
        if name == "screenshot":
            return self._result(call, "Screenshot attached.", False, self._screenshot_block())
        if name == "request_human":
            return self._human(call, str(args.get("reason", "")))
        if name not in ("click", "fill", "select", "read"):
            return self._result(call, f"Unknown tool {name}", True)
        return self._act(call, ActionKind(name), args)

    def _value(self, kind: ActionKind, args: dict[str, Any]) -> Value:
        given = [k for k in ("param", "secret", "literal") if args.get(k) not in (None, "")]
        if len(given) != 1:
            raise ValueError("give exactly one of param, secret or literal")
        k = given[0]
        if k == "param":
            if args["param"] not in self.params:
                raise ValueError(f"unknown param '{args['param']}'; declared: {sorted(self.params)}")
            return ParamRef(param=args["param"])
        if k == "secret":
            if kind != ActionKind.FILL or args["secret"] not in self.secrets:
                raise ValueError(f"unknown secret '{args['secret']}'")
            return SecretValue(secret=args["secret"])
        literal = str(args["literal"])
        for pname, pval in self.params.items():  # a literal equal to a param value IS that param
            if str(pval) == literal:
                return ParamRef(param=pname)
            if isinstance(pval, Decimal):
                try:
                    if Decimal(literal.replace("$", "").replace(",", "").strip()) == pval:
                        return ParamRef(param=pname)
                except InvalidOperation:
                    pass
        return LiteralValue(literal=literal)

    def _resolve_value(self, v: Value) -> str:
        if isinstance(v, ParamRef):
            return str(self.params[v.param])
        if isinstance(v, SecretValue):
            return self.secrets[v.secret]
        return v.literal

    def _act(self, call: ToolCall, kind: ActionKind, args: dict[str, Any]) -> dict[str, Any]:
        ref = str(args.get("ref", ""))
        intent = str(args.get("intent", "")).strip() or f"{kind.value} {ref}"
        try:
            value = self._value(kind, args) if kind in (ActionKind.FILL, ActionKind.SELECT) else None
        except ValueError as e:
            return self._result(call, f"Invalid value: {e}", True)
        if kind == ActionKind.READ and not any(o.name == args.get("output") for o in self.spec.outputs):
            return self._result(call, f"Unknown output '{args.get('output')}'; contract outputs: "
                                      f"{[o.name for o in self.spec.outputs]}", True)
        try:
            handle = self.page.locator(f"aria-ref={ref}").element_handle(timeout=2000)
            role, name = role_and_name(self.snapshot, ref)
        except Exception:
            return self._result(call, f"No element with ref {ref} in the latest snapshot.", True)
        assert handle is not None
        frame = handle.owner_frame()
        assert frame is not None
        cover = obscured_by(handle)
        if cover:
            return self._result(call, f"That element is covered by: {cover}. Deal with that first.", True)
        d = describe(self.page, handle, role, name, self.params, reading=kind == ActionKind.READ)
        risk, note = self._risk(kind, d.info, name)
        interstitial = self._is_interstitial(handle) if kind == ActionKind.CLICK else False
        if risk == Risk.IRREVERSIBLE and not self._approve(intent, role, name):
            return self._result(call, "A human rejected this irreversible action. Do not retry it; call give_up.", True)
        if risk == Risk.IRREVERSIBLE:
            self.guard.arm()
        before = self._frames_state()
        try:
            self.session.policy.require_action(kind, frame.url)
            self.lease.assert_automation(self.lease.epoch)
            if kind == ActionKind.CLICK:
                handle.click(timeout=5000)
            elif kind == ActionKind.FILL:
                assert value is not None
                handle.fill(self._resolve_value(value), timeout=5000)
            elif kind == ActionKind.SELECT:
                assert value is not None
                handle.select_option(label=self._resolve_value(value), timeout=5000)
            elif kind == ActionKind.READ:
                text = self._read(str(args["output"]), handle)
        except PolicyViolation as e:
            self.guard.disarm()
            return self._result(call, f"Blocked by policy: {e.detail}", True)
        except Exception as e:
            self.guard.disarm()
            if risk == Risk.IRREVERSIBLE:  # it may have landed; never let the model click it again
                raise _Finish("commit_state_unknown", f"the approved irreversible click failed mid-flight: "
                                                      f"{self.redactor.scrub_exception(e)[:200]}") from None
            return self._result(call, f"Action failed: {self.redactor.scrub_exception(e)[:300]}", True)
        self._settle()
        self.guard.disarm()
        if self.guard.blocked:
            raise _Finish("error", f"commit guard blocked an unapproved submit to {self.guard.blocked[-1]}")
        self.session.policy.audit_frames(self.page)
        step = TraceStep(
            index=len(self.trace), kind=kind, intent=intent, value=value,
            output=str(args["output"]) if kind == ActionKind.READ else None,
            role=role, name=name, strategies=d.strategies, rejected=d.rejected, frame_path=d.frame_path,
            risk=risk, risk_note=note, before=before, after=self._frames_state(), interstitial=interstitial,
        )
        self.trace.append(step)
        self.log.event("trace_step", index=step.index, action=kind.value, intent=intent, role=role, name=name,
                       risk=risk.value, risk_note=note, interstitial=interstitial,
                       strategies=[s.model_dump(mode="json") for s in d.strategies],
                       rejected=[list(r) for r in d.rejected])
        msg = {ActionKind.CLICK: "Clicked.", ActionKind.FILL: "Filled.", ActionKind.SELECT: "Selected."}.get(kind)
        if kind == ActionKind.READ:
            msg = f"Read {args['output']} = {text}"
        return self._result(call, f"{msg} Fresh snapshot follows.", False)

    def _read(self, output: str, handle: ElementHandle) -> str:
        decl = next(o for o in self.spec.outputs if o.name == output)
        text = norm(handle.inner_text())
        if decl.type == OutputType.DECIMAL:
            try:
                value: Any = Decimal(text.replace("$", "").replace(",", ""))
            except InvalidOperation:
                raise ValueError(f"'{text}' is not a decimal") from None
        elif decl.type == OutputType.INTEGER:
            value = int(text.replace(",", ""))
        else:
            value = text
        self.outputs[output] = value
        self.redactor.add(output, value, decl.sensitivity)
        return text

    def _risk(self, kind: ActionKind, info: dict[str, Any], name: str | None) -> tuple[Risk, str]:
        if kind in (ActionKind.FILL, ActionKind.SELECT):
            return Risk.REVERSIBLE, "changes form state only"
        if kind == ActionKind.READ:
            return Risk.SAFE, "read only"
        return self.profile.classify_click(
            name=name or info.get("own") or "", is_post_submit=bool(info.get("is_post_submit")),
            form_action=info.get("form_action"))

    def _is_interstitial(self, handle: ElementHandle) -> bool:
        """Clicking a known interstitial's dismiss control is the profile's job at replay, not a step."""
        for cond in self.profile.conditions:
            if cond.handler == "dismiss" and cond.dismiss is not None:
                r = resolve(self.page, cond.dismiss, self.params)
                if r.handle is not None and same_element(r.handle, handle):
                    return True
        return False

    def _approve(self, intent: str, role: str, name: str | None) -> bool:
        if self.operator is None:
            self.log.event("approval_unavailable", intent=intent)
            return False
        req = ApprovalRequest(run_id=self.run_id, step_id=f"trace-{len(self.trace)}", intent=intent,
                              target=f"{role} \"{name or ''}\"", bound_to=self.run_id,
                              summary=self._approval_display())  # terminal only, never persisted
        for f in self.page.frames:  # stray keystrokes into the browser must not submit a filled form
            try:
                f.evaluate("() => document.activeElement && document.activeElement.blur && "
                           "document.activeElement.blur()")
            except Exception:
                pass
        decision = self.operator.approve(req)
        rec = {"step": req.step_id, "intent": intent, "target": req.target, "decision": decision.decision,
               "approver": decision.approver, "approved_at": decision.at, "channel": self.operator.name,
               "bound_to": self.run_id}
        self.approvals.append(rec)
        self.log.event("approval", **rec)
        return decision.decision == "approved"

    def _approval_display(self) -> str:
        shown = []
        for spec in self.spec.params:
            v = self.params[spec.name]
            if spec.sensitivity == Sensitivity.PII and isinstance(v, str):
                v = "•" * max(0, len(v) - 2) + v[-2:]
            shown.append(f"{spec.name}={v}")
        return f"{self.spec.title}: " + ", ".join(shown)

    def _human(self, call: ToolCall, reason: str) -> dict[str, Any]:
        if self.operator is None:
            raise _Finish("stuck", f"model asked for a human and none is attached: {reason}")
        iid = f"INT-{self.run_id}-{sum(1 for e in self.log.events if e['kind'] == 'intervention') + 1}"
        shot = self.log.screenshot(self.page, f"{iid}-before")
        req = InterventionRequest(
            id=iid, run_id=self.run_id, capability=f"discovery: {self.spec.id}", step_id=None, step_intent=None,
            reason="stuck", detail=self.redactor.scrub(reason), ask=self.redactor.scrub(reason),
            resume_check="the model re-observes the page and continues", screenshot=shot,
            page_summary=self.redactor.scrub(page_summary(self.page, 300)), requested_at=now())
        self.log.write_json(f"handoff/{iid}.json", req.model_dump())
        self.log.event("intervention", id=iid, reason=reason)
        self.lease.transition(Owner.AWAITING_HUMAN, "automation", reason)
        self.capture.arm(self.page)
        n0 = len(self.capture.actions)

        class Ctx:
            page = self.page

            def take(_self, by: str) -> None:  # noqa: N805
                self.lease.transition(Owner.HUMAN, by, "operator took control")

            def pump(_self, ms: int = 100) -> None:  # noqa: N805
                self.page.wait_for_timeout(ms)

        outcome = self.operator.handle(req, Ctx())
        if self.lease.owner == Owner.HUMAN:
            self.lease.transition(Owner.VERIFYING, outcome.taken_by or "human", outcome.resolution)
        if outcome.resolution != "resumed":
            if self.lease.owner != Owner.ABORTED:
                self.lease.transition(Owner.ABORTED, "operator", outcome.resolution)
            raise _Finish("gave_up", f"operator {outcome.resolution} the handoff")
        self.session.policy.audit_frames(self.page)
        self.lease.transition(Owner.AUTOMATION, "automation", "resumed after handoff")
        actions = self.capture.actions[n0:]
        self.log.write_json(f"handoff/{iid}-human_actions.json", [a.model_dump() for a in actions])
        self.log.event("handoff", id=iid, resolution=outcome.resolution, taken_by=outcome.taken_by,
                       human_actions=len(actions))
        return self._result(call, f"The human finished and handed control back ({len(actions)} actions). "
                                  "Look at the fresh snapshot and continue.", False)

    def _log_turn(self, n: int, turn: Any) -> None:
        rec = {"turn": n, "model": turn.model, "message_id": turn.message_id, "request_id": turn.request_id,
               "generation_id": getattr(turn, "generation_id", None),
               "stop_reason": turn.stop_reason, "usage": turn.usage, "text": turn.text[:2000],
               "tool_calls": [{"name": c.name, "input": c.input} for c in turn.tool_calls]}
        self.turns.append(rec)
        self.log.event("model_turn", **rec)


def utc_stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
