"""goal → discovery run → compiled draft → validation replay (no model) → validated artifact."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_hands.agent.loop import DiscoveryAgent, DiscoveryResult
from agent_hands.artifact.compiler import CompileError, CompileReport, compile_trace
from pydantic import ValidationError

from agent_hands.artifact.schema import CapabilityArtifact
from agent_hands.artifact.spec import CapabilitySpec
from agent_hands.evidence import RunLog
from agent_hands.handoff.operator import Operator
from agent_hands.llm.base import Provider
from agent_hands.policy import Allowlist, Policy
from agent_hands.profile import load_profile
from agent_hands.replay.engine import ReplayConfig, ReplayEngine
from agent_hands.replay.result import Succeeded
from agent_hands.surface.web import BrowserSession

PRICES = {"claude-opus-5-5": (4.00, 20.00, 0.20, 5.00)}  # $/MTok: input, output, cache read, cache write


@dataclass
class PipelineResult:
    discovery: DiscoveryResult
    artifact: CapabilityArtifact | None
    report: CompileReport | None
    validation: Any | None
    error: str | None = None


def lint(artifact: CapabilityArtifact, redactor: Any) -> list[str]:
    """Every sensitive value the run knows (contract, secrets, observed data cells) that appears
    anywhere in the artifact. Artifacts are written verbatim only when this is empty."""
    text = artifact.canonical_json()
    return [v for v in redactor.sensitive_values() if v in text]


def git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=Path(__file__).parent, check=False).stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def estimate_cost(model: str, turns: list[dict[str, Any]]) -> float | None:
    if model not in PRICES:
        return None
    pin, pout, pread, pwrite = PRICES[model]
    total = 0.0
    for t in turns:
        u = t.get("usage") or {}
        total += (u.get("input_tokens") or 0) * pin + (u.get("output_tokens") or 0) * pout
        total += (u.get("cache_read_input_tokens") or 0) * pread + (u.get("cache_creation_input_tokens") or 0) * pwrite
    return round(total / 1_000_000, 4)


def discover(
    spec: CapabilitySpec,
    raw_params: dict[str, Any],
    provider: Provider,
    *,
    origin: str,
    allowlist: Allowlist,
    run_id: str,
    run_dir: Path | None,
    operator: Operator | None,
    headed: bool = False,
    max_steps: int = 40,
) -> PipelineResult:
    profile = load_profile(spec.profile)
    session = BrowserSession(policy=Policy(allowlist), headed=headed)
    session.start()
    agent = None
    try:
        agent = DiscoveryAgent(spec, raw_params, provider, session, profile, origin=origin, run_id=run_id,
                               operator=operator, run_dir=run_dir, max_steps=max_steps)
        result = agent.run()
    finally:
        blocked = [d.__dict__ for d in session.policy.blocked]
        session.close()
    redactor = agent.redactor
    log = RunLog(run_dir, redactor, backend=provider.name)
    models_served = sorted({t["model"] for t in result.turns})
    gen_ids = [t["generation_id"] for t in result.turns if t.get("generation_id")]
    stats = provider.generation_stats(gen_ids) if hasattr(provider, "generation_stats") and gen_ids else []
    upstream = sorted({s["provider_name"] for s in stats if s.get("provider_name")})
    billed = [s["total_cost"] for s in stats if isinstance(s.get("total_cost"), (int, float))]
    meta = {
        "run_id": run_id, "capability": spec.id, "provider": provider.name, "model_requested": provider.model,
        "models_served": models_served, "git_sha": git_sha(),
        "commit_guard": {"passed": agent.guard.passed, "blocked": agent.guard.blocked},
        "status": result.status, "reason": result.reason, "turns": len(result.turns),
        "message_ids": [t["message_id"] for t in result.turns], "request_ids": [t["request_id"] for t in result.turns],
        "generation_ids": gen_ids,
        "usage": [t["usage"] for t in result.turns],
        "routing": getattr(provider, "routing", None),
        "generations": stats,  # the router's own record of each turn: upstream, served model, cost
        "upstream_providers": upstream,
        "total_cost_usd": round(sum(billed), 6) if billed else None,
        "estimated_cost_usd": estimate_cost(provider.model, result.turns),
        "approvals": agent.approvals, "policy_blocked": blocked, "trace_steps": len(result.trace),
        "lease": [e.__dict__ for e in agent.lease.log],
    }
    log.write_json("meta.json", meta)
    if result.status != "done":
        return PipelineResult(result, None, None, None, f"discovery ended: {result.status}: {result.reason}")

    try:
        artifact, report = compile_trace(spec, result.trace, profile, params=agent.params, outputs=result.outputs,
                                         run_id=run_id, provider=provider.name, model=provider.model,
                                         models_served=models_served, scrub=redactor.scrub,
                                         upstream_providers=upstream,
                                         generation_ids=gen_ids if provider.name == "openrouter" else [])
    except (CompileError, ValidationError) as e:
        log.write_json("compile_error.json", {"error": redactor.scrub(str(e))[:2000]})
        return PipelineResult(result, None, None, None, f"compile failed: {redactor.scrub(str(e))[:300]}")
    leaks = lint(artifact, redactor)
    if leaks:
        log.write_json("compile_error.json", {"error": "artifact would carry sensitive values", "count": len(leaks)})
        return PipelineResult(result, None, None, None, f"artifact lint failed: {len(leaks)} sensitive value(s)")
    log.write_raw("compiled_draft.json", artifact.canonical_json())
    log.write_json("compile_report.json", report.__dict__)

    # Validation replay: fresh browser, no model anywhere. Only a clean pass promotes the draft.
    vsession = BrowserSession(policy=Policy(allowlist), headed=headed)
    vsession.start()
    try:
        engine = ReplayEngine(artifact, profile, vsession,
                              ReplayConfig(run_id=f"{run_id}-validate", origin=origin, allow_draft=True),
                              operator=operator, run_dir=None if run_dir is None else run_dir / "validation_replay")
        validation = engine.run(raw_params)
    finally:
        vsession.close()
    if isinstance(validation, Succeeded):
        data = json.loads(artifact.canonical_json())
        data["capability"]["status"] = "validated"
        data["provenance"]["validated_runs"] = [validation.run_id]
        artifact = CapabilityArtifact.model_validate(data)
        log.write_raw("artifact.json", artifact.canonical_json())
    return PipelineResult(result, artifact, report, validation,
                          None if isinstance(validation, Succeeded) else f"validation replay: {validation.status}")

