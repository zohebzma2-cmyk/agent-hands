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

Python 3.12 and [uv](https://docs.astral.sh/uv/). Works on macOS and Linux.

```sh
uv sync
uv run playwright install chromium
```

## Running without live services

Nothing here needs a key or the network.

```sh
uv run pytest                 # discovery is driven by a clearly labelled scripted fake model
uv run hands show artifacts/cu_core.open_sub_account@1.0.0.json   # the discovered artifact, as a review page
```

The stand-in app runs until Ctrl-C, so start it in a second terminal if you want to click around:

```sh
uv run hands target           # http://127.0.0.1:8341/core/login.do  (teller01 / synthetic-teller-pass)
```

You don't need it running for the commands below: each starts its own copy unless you pass
`--target-url`.

## Demo, in order

No key? Start at step 3. Steps 3 to 7 run offline in about 45 seconds.

The `P=(...)` array syntax needs bash or zsh.

```sh
P=(-p member_id=900001 -p account_type=Savings -p "nickname=Rainy day" -p initial_deposit=25.00)

# 1. Discovery with the real model (needs a key, see below). A browser opens. You'll be asked to
#    approve the irreversible step twice: once when the model reaches it, once in the validation
#    replay that follows. Keep keyboard focus in the terminal.
uv run hands discover --spec specs/open_sub_account.json "${P[@]}" --headed --out runs --artifacts runs/artifacts

# 2. Only if validation in step 1 didn't pass: validate the draft again, no model. Type "approve".
uv run hands validate runs/RUN_ID/compiled_draft.json "${P[@]}" --artifacts runs/artifacts

# Replay the artifact you just discovered, or the committed one if you skipped steps 1 and 2.
A=runs/artifacts/cu_core.open_sub_account@1.0.0.json
[ -f "$A" ] || A=artifacts/cu_core.open_sub_account@1.0.0.json

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

`hands replay` prints the result as JSON on stdout, with outputs masked the way they are on disk,
and one summary line on stderr. Code that calls `ReplayEngine.run` in-process gets the real outputs.
Exit codes: 0 succeeded, 1 failed, 2 outcome, 3 needs_human. Bad input prints one `error: ...` line
on stderr and also exits 2, with nothing on stdout; an outcome always prints its JSON result.

Faults are `name[:mode][:ms][@route]`: `member_not_found`, `validation_error`, `permission_denied`,
`announcement`, `unknown_dialog`, `session_expired`, `slow`, `http500`, `http500_after_commit`. They
go through a control API on a separate port that no allowlist includes.

## Live discovery (needs a model key)

The key is read only from the macOS Keychain, on purpose. On other OSes, setup, tests and every
replay still work; only `hands discover` needs the key.

```sh
# default: Claude Opus 5.5 through OpenRouter, pinned to Anthropic as the only upstream
security add-generic-password -s openrouter-api-key -a "$USER" -w   # prompts for the key, no echo

# or direct to Anthropic, then pass --provider anthropic
security add-generic-password -s anthropic-api-key -a "$USER" -w
```

The provider reads the key when a live run starts, and the audit scripts read it only to check it
never appears in a file. It is never read from env vars or files, never printed or logged, and only
sent to the provider's hard-coded base URL. I used OpenRouter because that was the key I had.
Production would call Anthropic directly with zero data retention.

## Live human handoff

This records a handoff into `runs/handoff-try`. The one in
[`evidence/handoff/handoff-human-live`](evidence/handoff/README.md) is mine, recorded the same way.

```sh
uv run hands replay artifacts/cu_core.open_sub_account@1.0.0.json \
  -p member_id=900003 -p account_type=Savings -p "nickname=Rainy day" -p initial_deposit=25.00 \
  --approve s13 --fault unknown_dialog --operator terminal --headed \
  --out runs --run-id handoff-try
```

When the INTERVENTION box appears, click into the terminal and type `take`. In the browser, enter
Supervisor PIN `2468` and click Approve Override. Back in the terminal, type `resume`. It waits up to
15 minutes.

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
artifacts/    validated artifacts and their review pages
evidence/     real runs, each folder indexed
docs/         design-notes.md (detail behind the report), tdd/ (red and green test runs; local temp
              paths in them replaced with <tmp>)
tests/        the test suite and two artifact fixtures
scripts/      evidence audit, history scan, generation lookup, evidence recording, simulated handoff,
              provider smoke test
```
