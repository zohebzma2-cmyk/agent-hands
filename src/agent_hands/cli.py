"""hands: the command line.

  hands target                      run the hostile stand-in app (and its fault-control API)
  hands discover --spec ... -p k=v  real LLM discovery run → validated artifact + evidence
  hands replay ARTIFACT -p k=v      deterministic replay, no model
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


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime())


def _params(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        k, sep, v = p.partition("=")
        if not sep:
            raise SystemExit(f"--param expects name=value, got {p!r}")
        out[k.strip()] = v
    return out


def _fault_spec(items: list[str]) -> dict[str, Any]:
    """name[:mode][:ms][@route], e.g. http500:always, slow:once:9000, session_expired@/core/confirm.do"""
    spec: dict[str, Any] = {}
    for item in items or []:
        body, _, at = item.partition("@")
        name, *rest = body.split(":")
        cfg: dict[str, Any] = {"mode": rest[0] if rest else "once"}
        if len(rest) > 1:
            cfg["ms"] = int(rest[1])
        if at:
            cfg["at"] = at
        spec[name] = cfg
    return spec


def _tenant(name: str) -> dict[str, Any]:
    path = TENANTS / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"unknown tenant {name!r}; see {TENANTS}")
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
            raise SystemExit("--fault with --target-url needs --control-url")

    def allowlist(self, tenant: dict[str, Any]) -> Allowlist:
        u = urlparse(self.url)
        return Allowlist(origins=[f"{u.scheme}://{u.netloc}"], routes=tenant["allowlist"]["routes"],
                         actions=[ActionKind(a) for a in tenant["allowlist"]["actions"]])

    def stop(self) -> None:
        if self.sim is not None:
            self.sim.stop()


def _target_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tenant", default="ridgeline")
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

    spec = CapabilitySpec.model_validate(json.loads(Path(args.spec).read_text()))
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
            dest = Path(args.artifacts) / f"{res.artifact.ref}.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(res.artifact.canonical_json())
            dest.with_suffix(".md").write_text(render(res.artifact))
            print(f"saved {dest}")
    if res.error:
        print(f"error: {res.error}", file=sys.stderr)
    return 0 if res.error is None else 1


def _run_replay(artifact: CapabilityArtifact, args: argparse.Namespace, target: Target, run_id: str,
                run_dir: Path | None, operator: Any = None):
    from agent_hands.replay.engine import ReplayConfig, ReplayEngine, approve
    from agent_hands.surface.web import BrowserSession

    tenant = _tenant(args.tenant)
    params = _params(args.param)
    try:
        approvals = [approve(artifact, sid, params, getpass.getuser(), run_id=run_id) for sid in args.approve or []]
    except ParamError:
        approvals = []  # the engine reports the bad param as a param_invalid result
    session = BrowserSession(policy=Policy(target.allowlist(tenant)), headed=args.headed)
    session.start()
    try:
        cfg = ReplayConfig(run_id=run_id, origin=target.url, approvals=approvals,
                           secret_overrides=tenant.get("secrets", {}))
        engine = ReplayEngine(artifact, load_profile(artifact.app.profile), session, cfg, operator=operator,
                              run_dir=run_dir)
        return engine.run(params), engine
    finally:
        session.close()


def cmd_replay(args: argparse.Namespace) -> int:
    from agent_hands.handoff.operator import TerminalOperator

    artifact = CapabilityArtifact.model_validate_json(Path(args.artifact).read_text())
    target = Target(args)
    run_id = args.run_id or f"rp-{_stamp()}"
    run_dir = Path(args.out) / run_id if args.out else None
    try:
        target.faults(_fault_spec(args.fault))
        result, engine = _run_replay(artifact, args, target, run_id, run_dir,
                                     TerminalOperator() if args.operator == "terminal" else None)
    finally:
        target.stop()
    print(json.dumps(result.persisted(artifact, engine.redactor), indent=2))
    return {"succeeded": 0, "outcome": 2, "needs_human": 3}.get(result.status, 1)


def cmd_stability(args: argparse.Namespace) -> int:
    artifact = CapabilityArtifact.model_validate_json(Path(args.artifact).read_text())
    if args.out and artifact.provenance.provider == "fake":
        from agent_hands.evidence import EVIDENCE_ROOT

        if Path(args.out).resolve().is_relative_to(EVIDENCE_ROOT.resolve()):
            print("refusing: artifacts from the fake backend can't produce evidence", file=sys.stderr)
            return 1
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
            print(f"{run_id}: {result.status} {result.duration_ms} ms")
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
        "step_ms_median": {s: int(statistics.median(r["steps_ms"].get(s, 0) for r in ok)) for s in step_ids} if ok else {},
        "runs_detail": rows,
        "note": "each run is explicitly approved by the caller (--approve) for its own run id",
    }
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "stability.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "runs_detail"}, indent=2))
    return 0 if report["pass_rate"] == 1.0 else 1


def cmd_show(args: argparse.Namespace) -> int:
    print(render(CapabilityArtifact.model_validate_json(Path(args.artifact).read_text())))
    return 0


def main(argv: list[str] | None = None) -> int:
    install_log_filter()
    ap = argparse.ArgumentParser(prog="hands", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("target", help="run the hostile stand-in app")
    t.add_argument("--port", type=int, default=8341)
    t.add_argument("--control-port", type=int, default=8342)
    t.add_argument("--tenant", default="ridgeline")
    t.add_argument("--fault", action="append", default=[])
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

    w = sub.add_parser("show", help="Markdown review view of an artifact")
    w.add_argument("artifact")
    w.set_defaults(fn=cmd_show)

    args = ap.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

