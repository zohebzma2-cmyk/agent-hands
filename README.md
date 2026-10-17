# agent-hands

A small but real version of the layer that gives an AI agent hands inside legacy bank back-office
software that has no API:

1. **Discover**: an LLM (Claude Opus 5.5, here via OpenRouter) works out how to do a task in a live, deliberately
   hostile web UI (framesets, table layout, no ids or test ids, unlabeled inputs).
2. **Compile**: the successful run becomes a typed, versioned, reviewable **capability artifact**.
   Every step names its target several ways and carries its own checkpoint.
3. **Replay**: the artifact runs deterministically with **no model in the loop**, and returns one
   of four results that keep business outcomes, recoverable conditions and hard failures apart.
4. **Escalate**: when it can't safely continue, it hands the **same live browser** to a human,
   records what they did, verifies the page, and resumes.
5. **Guard**: an allowlist enforced at the network layer, risk classes with an approval gate for
   irreversible steps, and contract-driven redaction of everything that reaches disk.

Design write-up: [`REPORT.md`](REPORT.md). Evidence of real runs: [`evidence/`](evidence/).

## Setup

Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run playwright install chromium
uv run pytest            # offline: no API key needed (a scripted fake model drives the discovery tests)
```

The real discovery run needs a model key in the **macOS Keychain**. It's read only when the live
provider starts, never from env vars or files, never logged, and only ever sent to the provider's
hard-coded base URL.

```sh
# default: Claude Opus 5.5 through OpenRouter, pinned to Anthropic as the only upstream
security add-generic-password -s openrouter-api-key -a "$USER" -w   # prompts for the key, no echo
# or direct to Anthropic: --provider anthropic
security add-generic-password -s anthropic-api-key -a "$USER" -w
```

| `--provider` | Keychain service | Model | Notes |
|---|---|---|---|
| `openrouter` (default) | `openrouter-api-key` | `anthropic/claude-opus-5.5` | Anthropic-compatible Messages endpoint, same SDK and request as direct; routing `only: [anthropic]`, `allow_fallbacks: false`; each turn's OpenRouter generation id, upstream and cost recorded |
| `anthropic` | `anthropic-api-key` | `claude-opus-5-5` | direct; server-side refusal fallbacks enabled |

OpenRouter is used here because that's the key the author had; production would call Anthropic
directly with zero data retention. `scripts/smoke_openrouter.py` makes one tiny tool-use call to
check the key, endpoint and routing (output in `evidence/provider-smoke/`).

## The target app

`cu_sim` is a local stand-in for a credit-union teller system ("CoreLink Teller 4.6", fictional).
All members, accounts and credentials are synthetic. Flow: sign on → member search → member
detail with balances → open sub-account form → confirmation.

```sh
uv run hands target                       # http://127.0.0.1:8341/core/login.do  (teller01 / synthetic-teller-pass)
uv run hands target --tenant lakeside     # same vendor product, different branding and labels
```

Runtime faults can be switched on per run with `--fault name[:mode][:ms][@route]`
(mode `once` or `always`). They go through a control API on a separate port that no agent allowlist
includes.

| Fault | What happens | Expected replay result |
|---|---|---|
| `member_not_found` | search finds nobody | `outcome` / `member_not_found` |
| `validation_error` | submit rejected with a field error | `outcome` / `validation_rejected` |
| `permission_denied` | teller lacks authority | `outcome` / `permission_denied` |
| `announcement` | known "System Announcement" overlay | recovered: dismissed |
| `unknown_dialog` | "Supervisor Override" needs a PIN | `needs_human` (or handoff) |
| `session_expired` | session dropped, app bounces to sign-on | recovered: re-login + resume |
| `slow:once:9000` | page takes 9 s | recovered: one GET retry |
| `http500` | server error page | recovered: fixed-backoff retry; persistent → `failed`/`app_error` |
| `http500_after_commit` | submit commits, then 500 | `needs_human` / `commit_state_unknown`, never resubmitted |

## Demo path

```sh
P=(-p member_id=900001 -p account_type=Savings -p nickname="Rainy day" -p initial_deposit=25.00)

# 1. discovery with the real model (OpenRouter by default; --provider anthropic for direct).
#    Approve the "Open Account" click when prompted (twice: once in discovery, once in the
#    validation replay that promotes the draft)
uv run hands discover --spec specs/open_sub_account.json "${P[@]}" --headed --out runs

# 2. deterministic replay of the resulting artifact (no model). --approve s13 is the caller's
#    explicit approval of the irreversible step for this one run
uv run hands replay artifacts/cu_core.open_sub_account@1.0.0.json "${P[@]}" --approve s13

# 3. the same replay hitting runtime conditions
uv run hands replay artifacts/cu_core.open_sub_account@1.0.0.json "${P[@]}" --approve s13 --fault member_not_found:always
uv run hands replay artifacts/cu_core.open_sub_account@1.0.0.json "${P[@]}" --approve s13 --fault session_expired

# 4. human handoff on the live session: type 'take', clear the dialog in the browser, type 'resume'
uv run hands replay artifacts/cu_core.open_sub_account@1.0.0.json "${P[@]}" --approve s13 \
    --fault unknown_dialog --operator terminal --headed

# 5. stability: 20 replays, pass rate and timing
uv run hands stability artifacts/cu_core.open_sub_account@1.0.0.json "${P[@]}" --approve s13 --runs 20

# review view of any artifact
uv run hands show artifacts/cu_core.open_sub_account@1.0.0.json
```

Without the live service, everything except step 1 works offline. `tests/fixtures/` holds a
hand-written artifact for the same flow, and the test suite exercises discovery end to end with a
clearly labeled scripted fake model that is barred from writing to `evidence/`.

## Layout

```
src/agent_hands/
  artifact/   schema.py (the artifact), spec.py (the contract a discovery starts from),
              compiler.py (trace → artifact), render.py (review view)
  agent/      loop.py (observe → decide → act), pipeline.py (discover → compile → validate)
  llm/        base.py (provider seam), anthropic_provider.py, openrouter_provider.py, fake.py (tests only)
  replay/     engine.py (deterministic replay), result.py (the result contract)
  surface/    web.py (perceive/act on Playwright), locate.py (resolver), describe.py (record-time targets)
  handoff/    lease.py (who owns the session), operator.py (interventions, capture, operators)
  policy.py   allowlist and its enforcement points
  profile.py  vendor app profiles (conditions, outcomes, risk rules); profiles/cu_core.json
  redact.py   contract-first redaction; evidence.py (the only writer to disk)
src/cu_sim/   the hostile stand-in app and its fault switches
specs/        capability contracts for discovery
config/       tenant configs (allowlist, secret refs)
evidence/     real runs (see evidence/README.md)
```
