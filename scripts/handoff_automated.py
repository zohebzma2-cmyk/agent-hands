"""Handoff evidence with a SCRIPTED operator in place of a human, clearly labelled as simulated.

usage: uv run python scripts/handoff_automated.py [OUT_DIR]

Same mechanism as the terminal handoff: replay hits the unknown "Supervisor Override" dialog, raises an
intervention, the lease moves to the operator, the operator acts on the same live page, capture
records it, automation verifies the page and resumes.

What differs is who the operator is: a scripted operator acts on the same live page in place of a
human, without approval authority (can_approve=False), to test separation of duties. It is recorded
as simulated in taken_by. The supervisor override it tries is itself an irreversible, human-only
action, so the commit guard blocks it at the network. The blocked submit leaves the main frame on
Chromium's error page, the check on return fails twice, and nothing is committed.
"""

import json
import sys
from pathlib import Path

import cu_sim
from agent_hands.artifact.schema import ActionKind, CapabilityArtifact
from agent_hands.handoff.operator import HandoffOutcome, now
from agent_hands.policy import Allowlist, Policy
from agent_hands.profile import load_profile
from agent_hands.replay.engine import ReplayConfig, ReplayEngine
from agent_hands.surface.web import BrowserSession

OPERATOR = "scripted-operator (simulated; no approval authority)"
ART = Path("artifacts/cu_core.open_sub_account@1.0.0.json")
out = Path(sys.argv[1] if len(sys.argv) > 1 else "evidence/handoff")
run_id = "handoff-supervisor-override-automated"
params = {"member_id": "900003", "account_type": "Savings", "nickname": "Rainy day", "initial_deposit": "25.00"}


class SimulatedOperator:
    name = "scripted"
    can_approve = False  # may take over the session; may never approve a commit

    def approve(self, req):  # pragma: no cover - the engine never asks an operator that can't approve
        raise RuntimeError("this operator has no approval authority")

    def handle(self, req, ctx):
        ctx.take(OPERATOR)
        taken = now()
        main = ctx.page.frame(name="main")
        if main.locator("input[name=pin]").count():  # nothing to do if the dialog is already gone
            main.locator("input[name=pin]").fill(cu_sim.app.SUPERVISOR_PIN)  # the synthetic sim PIN
            main.locator("input[value='Approve Override']").click()
            main.wait_for_load_state()
        ctx.pump(500)
        return HandoffOutcome("resumed", OPERATOR, taken, now(), note="simulated operator")


artifact = CapabilityArtifact.model_validate_json(ART.read_text())
sim = cu_sim.start()
sim.set_faults({"unknown_dialog": {"mode": "once"}})
session = BrowserSession(policy=Policy(Allowlist(origins=[sim.url], routes=["/", "/core/*"], actions=list(ActionKind))))
session.start()
try:
    engine = ReplayEngine(artifact, load_profile("cu_core"), session, ReplayConfig(run_id=run_id, origin=sim.url),
                          operator=SimulatedOperator(), run_dir=out / run_id)
    result = engine.run(params)
    committed = len(sim.state.snapshot()["subaccounts"])
finally:
    session.close()
    sim.stop()
(out / run_id / "invocation.json").write_text(json.dumps({
    "artifact": artifact.ref, "artifact_status": artifact.capability.status.value,
    "fault": "unknown_dialog (once)", "operator": OPERATOR, "pre_approved_steps": [],
    "note": "Simulated: a scripted operator acts on the same live Playwright page in place of a human, without "
            "approval authority, to test separation of duties. Its supervisor override is blocked at the network.",
}, indent=2) + "\n")
print(result.status, getattr(result, "reason", ""), "| commit_state", result.commit_state, "| sub-accounts", committed)
print([(h.step_id, h.reason, h.taken_by, h.resolution, h.verified, h.human_actions) for h in result.handoffs])
