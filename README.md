# agent-hands

Built by Zoheb Alvi with Claude Code (Anthropic) as the coding assistant; I made the design calls and reviewed the code.

This is a small version of the layer that lets an AI agent work inside bank back-office software
that has no API.

1. An LLM works out how to do a task in a live, deliberately hostile web UI.
2. The run is compiled into a typed, versioned capability artifact. Every step names its target
   several ways and has its own checkpoint.
3. The artifact replays with no model in the loop. The result keeps business outcomes, recoverable
   conditions and hard failures apart.
4. When replay is stuck, it hands the same live browser to a human, records what they did, checks
   the page and resumes.
5. An allowlist, an approval gate for irreversible steps and contract-driven redaction apply
   throughout.

The design write-up is [`REPORT.md`](REPORT.md). Real runs are in [`evidence/`](evidence/README.md).

## Setup

Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run playwright install chromium
```

## Model key (only for a live discovery run)

The key lives in the macOS Keychain. It is read once, inside the provider, when a live run starts.
It is never read from env vars or files, never logged, and only sent to the provider's hard-coded
base URL.

```sh
# default: Claude Opus 5.5 through OpenRouter, pinned to Anthropic as the only upstream
security add-generic-password -s openrouter-api-key -a "$USER" -w   # prompts for the key, no echo

# or direct to Anthropic, then pass --provider anthropic
security add-generic-password -s anthropic-api-key -a "$USER" -w
```

I used OpenRouter because that was the key I had. Production would call Anthropic directly with
zero data retention.

## Running without live services

Nothing below needs a key or the network.

```sh
uv run pytest                 # 209 tests; discovery is driven by a clearly labelled scripted fake model
uv run hands target           # the stand-in app at http://127.0.0.1:8341/core/login.do (teller01 / synthetic-teller-pass)
uv run hands show artifacts/cu_core.open_sub_account@1.0.0.json   # the discovered artifact, as a review page
```

The replay commands below also run offline. Each starts its own copy of the stand-in app unless you
pass `--target-url`.

## Demo, in order

```sh
P=(-p member_id=900001 -p account_type=Savings -p "nickname=Rainy day" -p initial_deposit=25.00)
A=artifacts/cu_core.open_sub_account@1.0.0.json

# 1. Discovery with the real model. A browser opens. Type "approve" when the irreversible
#    step comes up. Writes evidence and a draft artifact.
uv run hands discover --spec specs/open_sub_account.json "${P[@]}" --headed --out runs

# 2. Validate the draft with no model and promote it. Type "approve" once.
uv run hands validate runs/<run-id>/compiled_draft.json "${P[@]}"

# 3. Replay. --approve s13 is your explicit approval of the commit for this one run.
uv run hands replay $A "${P[@]}" --approve s13

# 4. Replay without approval: it stops at the commit with needs_human / approval_required.
uv run hands replay $A "${P[@]}"

# 5. Business outcome, recoveries and a hard failure.
uv run hands replay $A -p member_id=912345 -p account_type=Savings -p "nickname=Rainy day" -p initial_deposit=25.00
uv run hands replay $A "${P[@]}" --approve s13 --fault announcement
uv run hands replay $A "${P[@]}" --approve s13 --fault session_expired
uv run hands replay $A "${P[@]}" --approve s13 --fault http500
uv run hands replay $A "${P[@]}" --approve s13 --fault http500:always

# 6. Second tenant: drift without the overlay, clean with it.
uv run hands replay $A "${P[@]}" --tenant lakeside --no-override
uv run hands replay $A "${P[@]}" --tenant lakeside --approve s13

# 7. Stability.
uv run hands stability $A "${P[@]}" --approve s13 --runs 20
```

Faults are `name[:mode][:ms][@route]`: `member_not_found`, `validation_error`, `permission_denied`,
`announcement`, `unknown_dialog`, `session_expired`, `slow`, `http500`, `http500_after_commit`. They
go through a control API on a separate port that no allowlist includes.

## Live human handoff

This records a real handoff into `evidence/handoff/handoff-human-live`.

```sh
uv run hands replay artifacts/cu_core.open_sub_account@1.0.0.json \
  -p member_id=900003 -p account_type=Savings -p "nickname=Rainy day" -p initial_deposit=25.00 \
  --approve s13 --fault unknown_dialog --operator terminal --headed \
  --out evidence/handoff --run-id handoff-human-live
```

When the INTERVENTION box appears: type `take`. In the browser, enter Supervisor PIN `2468` and click
Approve Override. Back in the terminal, type `resume`. It waits up to 15 minutes.

## Layout

```
src/agent_hands/
  artifact/   schema.py (the artifact), spec.py (the contract discovery starts from),
              compiler.py, overlay.py (per-tenant overrides), render.py (review page)
  agent/      loop.py (observe, decide, act), pipeline.py (discover, compile, validate)
  llm/        base.py (provider seam), anthropic_provider.py, openrouter_provider.py, fake.py (tests only)
  replay/     engine.py (deterministic replay), result.py (the result contract)
  surface/    web.py (perceive and act), locate.py (resolver), describe.py (record-time targets)
  handoff/    lease.py (who owns the session), operator.py (interventions, capture, operators)
  policy.py   allowlist, action gate, commit guard
  profile.py  vendor app profile: conditions, outcomes, risk rules (profiles/cu_core.json)
  redact.py   contract-first redaction; evidence.py (the only writer to disk)
src/cu_sim/   the hostile stand-in app and its fault switches
specs/        capability contracts
config/       tenant configs: allowlist, secret refs, overlays
artifacts/    validated artifacts
evidence/     real runs, each folder indexed
scripts/      evidence audit, generation lookup, evidence recording, simulated handoff, provider smoke test
```
