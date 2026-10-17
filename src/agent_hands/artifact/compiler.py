"""Compile a discovery trace into a draft capability artifact.

Decisions the compiler makes (and records in the compile report for review):

- **Which trace steps become artifact steps.** Clicks on a known interstitial's dismiss control are
  dropped (the profile handles those at replay).
- **Login phase.** The leading steps up to and including the first click that leaves the entry route
  form the login phase, if they use secrets. Re-login after session expiry re-runs exactly these.
- **Per-step checkpoints**, derived from what actually changed:
  fill/select → the control holds the value; read → the target is present; click → the frame whose
  route path changed matches its new route, plus the first new label-like line of text in that
  frame (no digits, money, emails or param/output values, so no member data lands in the artifact).
- **Targets.** The strategies the describer validated, in rank order. Irreversible steps need two
  that agree; a click on one with fewer is a compile error.
- **Resume anchors.** Every task step before the first irreversible one resumes from the first
  task step after a re-login; steps after a commit have no anchor (never re-run past a commit).
- **Outcomes.** Profile outcome templates bind to the click steps whose resulting page matches the
  template's routes.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any

from agent_hands import __version__
from agent_hands.agent.loop import TraceStep
from agent_hands.artifact.schema import (
    ActionKind,
    CapabilityArtifact,
    LiteralValue,
    ParamRef,
    Risk,
    SecretValue,
    TargetSpec,
)
from agent_hands.artifact.spec import CapabilitySpec
from agent_hands.profile import AppProfile

COMPILER_VERSION = f"agent-hands {__version__}"
DEFAULT_TIMEOUT_MS = 5000
NAV_TIMEOUT_MS = 8000
_NOT_LABEL = re.compile(r"[\d$@]")


class CompileError(ValueError):
    pass


@dataclass
class CompileReport:
    steps: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _path_glob(route: str) -> str:
    return route.split("?")[0] + "*"


def _label_lines(before: list[str], after: list[str], banned: list[str]) -> list[str]:
    seen = set(before)
    out = []
    for line in after:
        if line in seen or len(line) > 60 or _NOT_LABEL.search(line):
            continue
        if any(b and b in line for b in banned):
            continue
        out.append(line)
    return out


def _target_desc(t: TraceStep, params: dict[str, Any], banned: list[str]) -> str:
    """A reviewer-facing description that never carries a value: params become {name}."""
    name = t.name or ""
    for k, v in params.items():
        if len(str(v)) >= 3:
            name = name.replace(str(v), "{" + k + "}")
    plain = re.sub(r"\{[a-z_]+\}", "", name)
    if name and t.kind != ActionKind.READ and not any(b and b in name for b in banned) and not _NOT_LABEL.search(plain):
        return f'{t.role} "{name}"'
    for s in t.strategies:
        if s.kind == "near_text" and isinstance(s.anchor, LiteralValue):
            col = f", column {s.column.literal}" if isinstance(s.column, LiteralValue) else ""
            return f'{t.role} next to "{s.anchor.literal}"{col}'
    return t.intent


def _click_checkpoint(
    t: TraceStep, banned: list[str], identity: dict[str, str]
) -> tuple[list[dict[str, Any]], str]:
    conds: list[dict[str, Any]] = []
    why = []
    changed = [
        (name, after_route) for name, (after_route, _) in t.after.items()
        if name in t.before and t.before[name][0].split("?")[0] != after_route.split("?")[0]
    ]
    frames_for = lambda name: [] if name == "top" else [{"name": name}]  # noqa: E731
    for name, route in changed:
        conds.append({"kind": "route_matches", "frame": frames_for(name), "pattern": _path_glob(route)})
        why.append(f"{name} navigated to {route.split('?')[0]}")
    # New label-like text, preferring a frame that navigated, else the frame the click happened in.
    candidates = [n for n, _ in changed] or [t.frame_path[-1].name if t.frame_path else "top"]
    for name in candidates:
        if name not in t.after:
            continue
        lines = _label_lines(t.before.get(name, ("", []))[1], t.after[name][1], banned)
        if lines:
            conds.append({"kind": "text_visible", "frame": frames_for(name),
                          "text": {"literal": lines[0]}, "match": "contains"})
            why.append(f"new text '{lines[0]}' in {name}")
            break
    # Identity: if the resulting page shows an identifying param (the member number), require it, so
    # replay can never carry on against a different record than the one it was asked about.
    for name in [n for n, _ in changed]:
        shown = " ".join(t.after.get(name, ("", []))[1])
        for pname, pval in identity.items():
            if pval in shown:
                conds.append({"kind": "text_visible", "frame": frames_for(name), "text": {"param": pname},
                              "match": "contains"})
                why.append(f"{name} shows {{{pname}}}")
    if not conds:
        name = t.frame_path[-1].name if t.frame_path else "top"
        route = t.after.get(name or "top", ("/", []))[0]
        conds.append({"kind": "route_matches", "frame": frames_for(name or "top"), "pattern": _path_glob(route)})
        why.append("nothing observable changed; checkpoint is only 'still on this page'")
    return conds, "; ".join(why)


def compile_trace(
    spec: CapabilitySpec,
    trace: list[TraceStep],
    profile: AppProfile,
    *,
    params: dict[str, Any],
    outputs: dict[str, Any],
    run_id: str,
    provider: str,
    model: str,
) -> tuple[CapabilityArtifact, CompileReport]:
    report = CompileReport()
    banned = [str(v) for v in list(params.values()) + list(outputs.values())]
    identity = {p.name: str(params[p.name]) for p in spec.params
                if p.sensitivity.value == "pii" and len(str(params[p.name])) >= 3}
    kept: list[TraceStep] = []
    for t in trace:
        if t.interstitial:
            report.dropped.append({"trace_index": t.index, "intent": t.intent, "why": "known interstitial dismissal"})
        else:
            kept.append(t)
    if not kept:
        raise CompileError("no steps to compile")

    # login phase: leading steps through the first click that leaves the entry route
    login_end = -1
    if any(isinstance(t.value, SecretValue) for t in kept):
        for i, t in enumerate(kept):
            top_before = t.before.get("top", ("", []))[0].split("?")[0]
            top_after = t.after.get("top", ("", []))[0].split("?")[0]
            if t.kind == ActionKind.CLICK and top_before == spec.entry_route and top_after != spec.entry_route:
                login_end = i
                break
        if login_end < 0:
            report.warnings.append("secrets used but no sign-on click found; no login phase")

    first_task = login_end + 1
    first_commit = next((i for i, t in enumerate(kept) if t.risk == Risk.IRREVERSIBLE), len(kept))
    steps: list[dict[str, Any]] = []
    for i, t in enumerate(kept):
        sid = f"s{i + 1:02d}"
        frame = [r.model_dump(exclude_none=True) for r in t.frame_path]
        min_agree = 2 if t.risk == Risk.IRREVERSIBLE else 1
        if len(t.strategies) < min_agree:
            raise CompileError(f"{sid} ({t.intent}): {len(t.strategies)} validated strategies, need {min_agree}")
        target = TargetSpec.model_validate({
            "description": _target_desc(t, params, banned), "frame": frame,
            "strategies": [s.model_dump(mode="json") for s in t.strategies], "min_agreement": min_agree,
        }).model_dump(mode="json", exclude_none=True)
        action: dict[str, Any] = {"kind": t.kind.value}
        if t.kind in (ActionKind.FILL, ActionKind.SELECT):
            assert t.value is not None
            action["value"] = t.value.model_dump()
            expect = {"all_of": [{"kind": "value_equals", "target": target, "value": t.value.model_dump()}],
                      "timeout_ms": DEFAULT_TIMEOUT_MS}
            why = "control holds the value"
        elif t.kind == ActionKind.READ:
            action["output"] = t.output
            expect = {"all_of": [{"kind": "element_present", "target": target}], "timeout_ms": DEFAULT_TIMEOUT_MS}
            why = "value element present"
        else:
            conds, why = _click_checkpoint(t, banned, identity)
            navigated = any(c["kind"] == "route_matches" for c in conds)
            expect = {"all_of": conds, "timeout_ms": NAV_TIMEOUT_MS if navigated else DEFAULT_TIMEOUT_MS}
        step: dict[str, Any] = {
            "id": sid, "phase": "login" if i <= login_end else "task", "intent": t.intent, "action": action,
            "target": target, "risk": t.risk.value, "expect": expect,
            "provenance": {"run_id": run_id, "trace_index": t.index},
        }
        if first_task <= i < first_commit:
            step["resume_anchor"] = f"s{first_task + 1:02d}"
        steps.append(step)
        report.steps.append({
            "id": sid, "intent": t.intent, "risk": t.risk.value, "risk_note": t.risk_note,
            "strategies": [s.kind for s in t.strategies], "rejected": [list(r) for r in t.rejected],
            "checkpoint": why,
        })

    # success: the last click's checkpoint (the screen the goal ends on)
    last_click = next((s for s in reversed(steps) if s["action"]["kind"] == "click"), None)
    success = last_click["expect"] if last_click else steps[-1]["expect"]

    outcomes = []
    for tpl in profile.outcomes:
        at = [
            steps[i]["id"] for i, t in enumerate(kept)
            if t.kind == ActionKind.CLICK
            and any(fnmatch(route, r) for route, _ in t.after.values() for r in tpl.applies_to_routes)
        ]
        if at:
            o = tpl.model_dump(mode="json", exclude_none=True)
            o.pop("applies_to_routes")
            o["at_steps"] = at
            outcomes.append(o)

    used_params = {t.value.param for t in kept if isinstance(t.value, ParamRef)}
    for p in spec.params:
        if p.name not in used_params:
            report.warnings.append(f"param '{p.name}' is declared but no step uses it")
    read_outputs = {t.output for t in kept if t.kind == ActionKind.READ}
    missing = [o.name for o in spec.outputs if o.name not in read_outputs]
    if missing:
        raise CompileError(f"outputs never read: {missing}")

    risk = max((t.risk for t in kept), key=lambda r: ["safe", "reversible", "irreversible"].index(r.value))
    data = {
        "schema_version": "1.0",
        "capability": {"id": spec.id, "version": "1.0.0", "title": spec.title, "description": spec.description,
                       "status": "draft", "risk": risk.value},
        "app": {"profile": spec.profile, "profile_versions": spec.profile_versions, "entry_route": spec.entry_route},
        "params": [p.model_dump(mode="json", exclude_none=True) for p in spec.params],
        "secrets": [s.model_dump(mode="json") for s in spec.secrets],
        "outputs": [
            {**o.model_dump(mode="json"),
             "source_step": next(s["id"] for s in steps if s["action"].get("output") == o.name)}
            for o in spec.outputs
        ],
        "preconditions": spec.preconditions,
        "conditions": [c.id for c in profile.conditions],
        "outcomes": outcomes,
        "steps": steps,
        "success": success,
        "provenance": {"compiler_version": COMPILER_VERSION,
                       "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                       "discovered_by_run": run_id, "provider": provider, "model": model},
    }
    return CapabilityArtifact.model_validate(data), report
