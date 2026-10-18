"""hands: the command line.

  hands target                      run the hostile stand-in app (and its fault-control API)
  hands discover --spec ... -p k=v  real LLM discovery run → validated artifact + evidence
  hands replay ARTIFACT -p k=v      deterministic replay, no model
  hands validate DRAFT -p k=v       re-run a draft's validation replay (no model) and promote it
  hands stability ARTIFACT          replay the happy path N times; pass rate and timing
  hands show ARTIFACT               Markdown review view of an artifact
"""

from __future__ import annotations

import argparse
import getpass
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import cu_sim
from agent_hands.artifact.render import render
from agent_hands.artifact.schema import ActionKind, CapabilityArtifact, ParamError
from agent_hands.evidence import REPO_ROOT
from agent_hands.policy import Allowlist, Policy
from agent_hands.profile import load_profile
from agent_hands.redact import install_log_filter

TENANTS = REPO_ROOT / "config" / "tenants"
TENANT_NAMES = sorted(p.stem for p in TENANTS.glob("*.json"))
EXIT_CODES = {"succeeded": 0, "failed": 1, "outcome": 2, "needs_human": 3}


class UsageError(Exception):
    """Bad input from the command line: printed as one line, exit 2, nothing on stdout."""


class _Parser(argparse.ArgumentParser):
    """argparse, but its errors are one line like every other usage error."""

    def error(self, message: str):  # type: ignore[override]
        raise UsageError(f"{self.prog}: {message}")


def _load_artifact(path: str) -> CapabilityArtifact:
    try:
        return CapabilityArtifact.model_validate_json(Path(path).read_text())
    except FileNotFoundError:
        raise
    except (ValueError, OSError) as e:  # pydantic ValidationError and JSON errors are ValueErrors
        raise UsageError(f"{path} is not a valid capability artifact ({type(e).__name__})") from None


def _summary(result: Any, persisted: dict[str, Any]) -> str:
    """One line for a human: status, why, commit state, confirmation, recoveries. Built from the
    persisted (masked) view, so it shows outputs the same way the JSON does."""
    why = getattr(result, "code", None) or getattr(result, "reason", None) or ""
    failure = getattr(result, "failure", None)
    where = failure.step_id if failure is not None else (getattr(result, "step_id", None)
                                                          or getattr(result, "at_step", None))
    if failure is not None:
        why = failure.kind.value
    if where:
        why = f"{why} at {where}"
    outputs = persisted.get("outputs") or {}
    conf = f" confirmation={outputs['confirmation_number']}" if "confirmation_number" in outputs else ""
    rec = ",".join(f"{r.condition.split('.')[-1].split('@')[0]}:{r.action}" for r in result.recoveries) or "none"
    return f"{result.status} {why}".strip() + f" | commit_state={result.commit_state}{conf} | recoveries={rec}"


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime())


def _save_artifact(artifact: CapabilityArtifact, directory: str) -> Path | None:
    """Write artifacts/<id>@<version>.json, refusing to replace a different artifact under the same
    version (a changed artifact needs a version bump; see artifact.schema.required_bump)."""
    dest = Path(directory) / f"{artifact.ref}.json"
    if dest.exists():
        existing = CapabilityArtifact.model_validate_json(dest.read_text())
        if existing.content_hash() != artifact.content_hash():
            from agent_hands.artifact.schema import required_bump

            print(f"refusing to overwrite {dest}: different content under the same version "
                  f"(needs a {required_bump(existing, artifact)} bump)", file=sys.stderr)
            return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(artifact.canonical_json())
    dest.with_suffix(".md").write_text(render(artifact))
    return dest


def _params(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        k, sep, v = p.partition("=")
        if not sep:
            raise UsageError("each --param must be name=value")  # never echo the value: it may be PII
        out[k.strip()] = v
    return out


def _fault_spec(items: list[str]) -> dict[str, Any]:
    """name[:mode][:ms][@route], e.g. http500:always, slow:once:9000, session_expired@/core/confirm.do"""
    spec: dict[str, Any] = {}
    for item in items or []:
        body, _, at = item.partition("@")
        name, *rest = body.split(":")
        if name not in cu_sim.FAULTS:
            raise UsageError(f"unknown fault {name!r}; choose from {sorted(cu_sim.FAULTS)}")
        if rest and rest[0] not in ("once", "always"):
            raise UsageError(f"fault mode must be 'once' or 'always', got {rest[0]!r}")
        cfg: dict[str, Any] = {"mode": rest[0] if rest else "once"}
        if len(rest) > 1:
            if not rest[1].isdigit():
                raise UsageError(f"fault {name!r}: the third part is milliseconds, got {rest[1]!r}")
            cfg["ms"] = int(rest[1])
        if at:
            cfg["at"] = at
        spec[name] = cfg
    return spec


def _tenant(name: str) -> dict[str, Any]:
    path = TENANTS / f"{name}.json"
    if not path.exists():
        raise UsageError(f"unknown tenant {name!r}; choose from {TENANT_NAMES}")
    return json.loads(path.read_text())


class Target:
    """Either the embedded simulator or an already-running one."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.sim = None
        if args.target_url:
            self.url, self.control = args.target_url.rstrip("/"), args.control_url
        else:
            self.sim = cu_sim.start(tenant=args.tenant)
            self.url, self.control = self.sim.url, self.sim.control_url

    def faults(self, spec: dict[str, Any]) -> None:
        if not spec:
            return
        if self.sim is not None:
            self.sim.set_faults(spec)
        elif self.control:
            import urllib.request

            req = urllib.request.Request(self.control + "/faults", json.dumps(spec).encode(),
                                         {"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).read()
        else:
            raise UsageError("--fault with --target-url needs --control-url")

    def allowlist(self, tenant: dict[str, Any]) -> Allowlist:
        u = urlparse(self.url)
        return Allowlist(origins=[f"{u.scheme}://{u.netloc}"], routes=tenant["allowlist"]["routes"],
                         actions=[ActionKind(a) for a in tenant["allowlist"]["actions"]])

    def stop(self) -> None:
        if self.sim is not None:
            self.sim.stop()


def _target_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tenant", default="ridgeline", choices=TENANT_NAMES, help="tenant config (default: ridgeline)")
    p.add_argument("--target-url", help="use a running target instead of the embedded simulator")
    p.add_argument("--control-url", help="fault-control URL of a running target")
    p.add_argument("--fault", action="append", default=[], help="name[:mode][:ms][@route]")
    p.add_argument("--headed", action="store_true", help="show the browser (needed for a human takeover)")


# --- commands -------------------------------------------------------------------


def cmd_target(args: argparse.Namespace) -> int:
    sim = cu_sim.start(port=args.port, control_port=args.control_port, tenant=args.tenant)
    if args.fault:
        sim.set_faults(_fault_spec(args.fault))
    print(f"CoreLink Teller stand-in ({args.tenant}) at {sim.url}/core/login.do")
    print(f"fault control at {sim.control_url}/faults   (synthetic login: teller01 / synthetic-teller-pass)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        sim.stop()
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    from agent_hands.agent.pipeline import discover
    from agent_hands.artifact.spec import CapabilitySpec
    from agent_hands.handoff.operator import TerminalOperator
    from agent_hands.llm.base import ProviderUnavailable

    try:
        spec = CapabilitySpec.model_validate(json.loads(Path(args.spec).read_text()))
    except (ValueError, json.JSONDecodeError) as e:
        raise UsageError(f"{args.spec} is not a valid capability spec ({type(e).__name__})") from None
    if not sys.stdin.isatty():  # before reading any key or spending anything
        print("stopping: discover needs an interactive terminal (a human approves the irreversible step)",
              file=sys.stderr)
        return 1
    try:
        if args.provider == "openrouter":
            from agent_hands.llm.openrouter_provider import DEFAULT_SLUG, OpenRouterProvider

            provider: Any = OpenRouterProvider(model=args.model or DEFAULT_SLUG, effort=args.effort)
        else:
            from agent_hands.llm.anthropic_provider import AnthropicProvider

            provider = AnthropicProvider(model=args.model or "claude-opus-5-5", effort=args.effort)
    except ProviderUnavailable as e:
        print(f"stopping: {e}", file=sys.stderr)
        return 1
    tenant = _tenant(args.tenant)
    target = Target(args)
    run_id = args.run_id or f"disc-{_stamp()}"
    run_dir = Path(args.out) / run_id
    try:
        target.faults(_fault_spec(args.fault))
        via = f" via {provider.name}, routing {provider.routing}" if provider.name == "openrouter" else ""
        print(f"discovery {run_id}: {provider.model}{via} on {target.url} → {run_dir}")
        res = discover(spec, _params(args.param), provider, origin=target.url, allowlist=target.allowlist(tenant),
                       run_id=run_id, run_dir=run_dir, operator=TerminalOperator(), headed=args.headed,
                       max_steps=args.max_steps)
    finally:
        target.stop()
    print(f"discovery: {res.discovery.status} ({res.discovery.reason}); {len(res.discovery.trace)} actions")
    if res.artifact is not None:
        print(f"artifact: {res.artifact.ref} status={res.artifact.capability.status.value} "
              f"validation={getattr(res.validation, 'status', None)}")
        if res.artifact.capability.status.value == "validated":
            dest = _save_artifact(res.artifact, args.artifacts)
            if dest is not None:
                print(f"saved {dest}")
    if res.error:
        print(f"error: {res.error}", file=sys.stderr)
    return 0 if res.error is None else 1


def _run_replay(artifact: CapabilityArtifact, args: argparse.Namespace, target: Target, run_id: str,
                run_dir: Path | None, operator: Any = None, allow_draft: bool = False):
    from agent_hands.replay.engine import ReplayConfig, ReplayEngine, approve
    from agent_hands.surface.web import BrowserSession

    tenant = _tenant(args.tenant)
    params = _params(args.param)
    if not getattr(args, "no_override", False):
        from agent_hands.artifact.overlay import apply_overlay, overlay_for

        ov = overlay_for(tenant, artifact.capability.id)
        if ov is not None:
            artifact = apply_overlay(artifact, ov)
    try:
        approvals = [approve(artifact, sid, params, getpass.getuser(), run_id=run_id) for sid in args.approve or []]
    except ParamError:
        approvals = []  # the engine reports the bad param as a param_invalid result
    session = BrowserSession(policy=Policy(target.allowlist(tenant)), headed=args.headed)
    session.start()
    try:
        cfg = ReplayConfig(run_id=run_id, origin=target.url, approvals=approvals, allow_draft=allow_draft,
                           secret_overrides=tenant.get("secrets", {}))
        engine = ReplayEngine(artifact, load_profile(artifact.app.profile), session, cfg, operator=operator,
                              run_dir=run_dir)
        return engine.run(params), engine
    finally:
        session.close()


def cmd_replay(args: argparse.Namespace) -> int:
    from agent_hands.handoff.operator import TerminalOperator

    artifact = _load_artifact(args.artifact)
    if args.operator == "terminal" and not sys.stdin.isatty():
        print("stopping: --operator terminal needs an interactive terminal", file=sys.stderr)
        return 1
    if artifact.capability.status.value == "draft" and not args.allow_draft:
        raise UsageError("this artifact is a draft (pass --allow-draft for pre-promotion evidence runs)")
    if args.allow_draft and (args.approve or args.operator != "none"):
        raise UsageError("a draft can't be approved through replay; use 'hands validate'")
    target = Target(args)
    run_id = args.run_id or f"rp-{_stamp()}"
    run_dir = Path(args.out) / run_id if args.out else None
    try:
        target.faults(_fault_spec(args.fault))
        result, engine = _run_replay(artifact, args, target, run_id, run_dir,
                                     TerminalOperator() if args.operator == "terminal" else None,
                                     allow_draft=args.allow_draft)
    finally:
        target.stop()
    if run_dir is not None:
        (run_dir / "invocation.json").write_text(json.dumps({
            "artifact": artifact.ref, "artifact_status": artifact.capability.status.value,
            "artifact_hash": artifact.content_hash(), "tenant": args.tenant,
            "faults": _fault_spec(args.fault), "pre_approved_steps": args.approve,
            "operator": args.operator, "allow_draft": args.allow_draft,
            "tenant_overlay_applied": engine.a.provenance.overlay, "replayed_hash": engine.hash,
            "params": {k: engine.redactor.scrub(v) for k, v in _params(args.param).items()},
        }, indent=2) + "\n")
    # stdout: the masked result (outputs redacted as for disk). In-process callers of ReplayEngine.run
    # get the real outputs. stderr: one line for a human.
    persisted = result.persisted(artifact, engine.redactor)
    print(json.dumps(persisted, indent=2))
    print(_summary(result, persisted), file=sys.stderr)
    return EXIT_CODES.get(result.status, 1)


def cmd_stability(args: argparse.Namespace) -> int:
    artifact = _load_artifact(args.artifact)
    if args.out and artifact.provenance.provider == "fake":
        from agent_hands.evidence import EVIDENCE_ROOT

        if Path(args.out).resolve().is_relative_to(EVIDENCE_ROOT.resolve()):
            raise UsageError("artifacts from the fake backend can't produce evidence")
    target = Target(args)
    rows, prints = [], []
    try:
        for n in range(args.runs):
            if target.sim is not None:
                target.sim.state.reset()
            run_id = f"stab-{n + 1:02d}"
            t = time.monotonic()
            result, _ = _run_replay(artifact, args, target, run_id, None)
            wall = int((time.monotonic() - t) * 1000)
            fp = [(s.step_id, s.status, s.resolution.used_index if s.resolution else None) for s in result.steps]
            prints.append(json.dumps(fp))
            rows.append({"run": run_id, "status": result.status, "engine_ms": result.duration_ms, "wall_ms": wall,
                         "drift": len(result.drift), "recoveries": len(result.recoveries),
                         "steps_ms": {s.step_id: s.duration_ms for s in result.steps}})
            print(f"{run_id}: {result.status} {result.duration_ms} ms", file=sys.stderr)
    finally:
        target.stop()
    ok = [r for r in rows if r["status"] == "succeeded"]
    times = sorted(r["engine_ms"] for r in ok)

    def pct(p: float) -> int:
        return times[min(len(times) - 1, int(round(p * (len(times) - 1))))] if times else 0

    step_ids = list(rows[0]["steps_ms"]) if rows else []
    report = {
        "artifact": artifact.ref, "artifact_hash": artifact.content_hash(), "runs": len(rows),
        "succeeded": len(ok), "pass_rate": round(len(ok) / len(rows), 4) if rows else 0.0,
        "engine_ms": {"p50": pct(0.5), "p95": pct(0.95), "max": times[-1] if times else 0,
                      "mean": int(statistics.mean(times)) if times else 0},
        "identical_paths": len(set(prints)) == 1, "distinct_paths": len(set(prints)),
        "drift_events": sum(r["drift"] for r in rows), "recoveries": sum(r["recoveries"] for r in rows),
        "step_ms_median": (
            {s: int(statistics.median(r["steps_ms"].get(s, 0) for r in ok)) for s in step_ids} if ok else {}
        ),
        "runs_detail": rows,
        "note": "each run is explicitly approved by the caller (--approve) for its own run id",
    }
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "stability.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "runs_detail"}, indent=2))
    return 0 if report["pass_rate"] == 1.0 else 1


def cmd_validate(args: argparse.Namespace) -> int:
    """Re-run the validation replay of a compiled draft (no model) and promote it if it passes."""
    from agent_hands.agent.pipeline import lint
    from agent_hands.evidence import RunLog
    from agent_hands.handoff.operator import TerminalOperator
    from agent_hands.replay.result import Succeeded

    if not sys.stdin.isatty():
        print("stopping: validate needs an interactive terminal (a human approves the irreversible step)",
              file=sys.stderr)
        return 1
    draft = _load_artifact(args.draft)
    if draft.capability.status.value != "draft":
        print(f"{args.draft} is already {draft.capability.status.value}", file=sys.stderr)
        return 1
    if draft.provenance.provider == "fake":
        raise UsageError("drafts from the fake backend are never promoted")
    run_dir = Path(args.draft).parent
    run_id = args.run_id or f"{draft.provenance.discovered_by_run}-validate-{_stamp()}"
    target = Target(args)
    try:
        args.approve = []  # promotion needs a human at the prompt, never a pre-approval
        args.no_override = True  # validate and promote the same artifact: never a tenant-derived one
        result, engine = _run_replay(draft, args, target, run_id, run_dir / f"validation_{run_id}",
                                     TerminalOperator(), allow_draft=True)
    finally:
        target.stop()
    print(f"validation replay {run_id}: {result.status}")
    if not isinstance(result, Succeeded):
        print(json.dumps(result.persisted(draft, engine.redactor), indent=2))
        return 1
    data = json.loads(draft.canonical_json())
    data["capability"]["status"] = "validated"
    data["provenance"]["validated_runs"] = [*data["provenance"].get("validated_runs", []), run_id]
    artifact = CapabilityArtifact.model_validate(data)
    leaks = lint(artifact, engine.redactor)
    if leaks:
        print(f"refusing to save: artifact would carry {len(leaks)} sensitive value(s)", file=sys.stderr)
        return 1
    RunLog(run_dir, engine.redactor, backend="replay").write_raw("artifact.json", artifact.canonical_json())
    dest = _save_artifact(artifact, args.artifacts)
    if dest is None:
        return 1
    print(f"promoted to validated; saved {dest} and {run_dir / 'artifact.json'}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    print(render(_load_artifact(args.artifact)))
    return 0


def main(argv: list[str] | None = None) -> int:
    install_log_filter()
    ap = _Parser(prog="hands", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("target", help="run the hostile stand-in app until Ctrl-C")
    t.add_argument("--port", type=int, default=8341, help="app port (default: 8341)")
    t.add_argument("--control-port", type=int, default=8342, help="fault-control API port (default: 8342)")
    t.add_argument("--tenant", default="ridgeline", choices=TENANT_NAMES, help="tenant (default: ridgeline)")
    t.add_argument("--fault", action="append", default=[], help="name[:mode][:ms][@route], repeatable")
    t.set_defaults(fn=cmd_target)

    d = sub.add_parser("discover", help="real LLM discovery run")
    d.add_argument("--spec", required=True)
    d.add_argument("-p", "--param", action="append", default=[])
    d.add_argument("--provider", choices=["openrouter", "anthropic"], default="openrouter",
                   help="openrouter (Keychain openrouter-api-key) or anthropic (Keychain anthropic-api-key)")
    d.add_argument("--model", help="default: anthropic/claude-opus-5.5 (openrouter) or claude-opus-5-5 (anthropic)")
    d.add_argument("--effort", default="high")
    d.add_argument("--max-steps", type=int, default=40)
    d.add_argument("--run-id")
    d.add_argument("--out", default=str(REPO_ROOT / "runs"))
    d.add_argument("--artifacts", default=str(REPO_ROOT / "artifacts"))
    _target_args(d)
    d.set_defaults(fn=cmd_discover)

    r = sub.add_parser("replay", help="deterministic replay (no model)")
    r.add_argument("artifact")
    r.add_argument("-p", "--param", action="append", default=[])
    r.add_argument("--approve", action="append", default=[], help="step id to pre-approve for this run")
    r.add_argument("--operator", choices=["none", "terminal"], default="none")
    r.add_argument("--no-override", action="store_true", help="ignore the tenant's overlay (to see raw drift)")
    r.add_argument("--allow-draft", action="store_true",
                   help="replay a not-yet-promoted draft (evidence/testing only; never in production)")
    r.add_argument("--run-id")
    r.add_argument("--out", help="write evidence for this run under OUT/<run-id>")
    _target_args(r)
    r.set_defaults(fn=cmd_replay)

    s = sub.add_parser("stability", help="replay the happy path N times")
    s.add_argument("artifact")
    s.add_argument("-p", "--param", action="append", default=[])
    s.add_argument("--approve", action="append", default=[])
    s.add_argument("--runs", type=int, default=20)
    s.add_argument("--out")
    _target_args(s)
    s.set_defaults(fn=cmd_stability)

    v = sub.add_parser("validate", help="re-run the validation replay of a compiled draft and promote it")
    v.add_argument("draft", help="e.g. evidence/discovery/<run>/compiled_draft.json")
    v.add_argument("-p", "--param", action="append", default=[])
    v.add_argument("--run-id")
    v.add_argument("--artifacts", default=str(REPO_ROOT / "artifacts"))
    _target_args(v)
    v.set_defaults(fn=cmd_validate)

    w = sub.add_parser("show", help="Markdown review view of an artifact")
    w.add_argument("artifact")
    w.set_defaults(fn=cmd_show)

    try:
        args = ap.parse_args(argv)
        return int(args.fn(args))
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"error: no such file: {e.filename}", file=sys.stderr)
        return 2
    except OSError as e:  # e.g. the target's port is already in use
        print(f"error: {e.strerror or e}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

