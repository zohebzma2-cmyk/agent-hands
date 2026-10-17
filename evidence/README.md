# Evidence

Everything here came from real runs on 2026-10-17 against the local stand-in app (`cu_sim`). All
member data is synthetic. Every file passed `scripts/audit_evidence.py`: no API key, no `sk-or-` or
`sk-ant-` key shapes, no `/Users/` paths, no member names, numbers, balances, addresses or
credentials, and the supervisor PIN never appears.

Each run folder has `result.json` (the replay result contract), `run.jsonl` (the structured log) and,
where a screen mattered, masked screenshots in `screens/`. Folders below have their own index.

## The real discovery run

| Run | Result |
|---|---|
| `discovery/disc-20261017T200007` | Claude Opus 5.5 via OpenRouter, served by Anthropic on all 8 turns. 14 actions, `done`. $0.156849. I approved the irreversible submit live. |
| `.../validation_replay` | First validation attempt: `failed operator_aborted` at s13. Stray input reached the browser during the approval wait, the commit guard blocked the unapproved submit, nothing was committed. Root cause and fix in commit e837dfd. |
| `.../validation_disc-...-validate-20261017T204352` | Second validation: `succeeded`, committed, my live approval. This promoted the draft to `artifacts/cu_core.open_sub_account@1.0.0.json`. |

## Replays of the discovered artifact (no model)

| Run | Result | What it shows |
|---|---|---|
| `replay/unattended-member-not-found` | `outcome member_not_found` at s05 | A business outcome, not a crash. Member 912345 doesn't exist. |
| `replay/unattended-announcement-dialog` | `needs_human approval_required` at s13 | Known dialog dismissed, then unattended replay stops at the commit gate. |
| `replay/unattended-session-expired` | `needs_human approval_required` at s13 | Session dropped mid-run: signed on again, resumed at s04, stopped at the gate. |
| `replay/unattended-http500-member-detail` | `needs_human approval_required` at s13 | One 500 on the detail page, one retry, stopped at the gate. |
| `replay/approved-happy-path-900003` | `succeeded`, committed | A different member than discovery used. My caller approval. |
| `replay/approved-announcement-dialog` | `succeeded`, committed | Dialog dismissed, then committed. |
| `replay/approved-session-expired` | `succeeded`, committed | Re-login and resume, then committed. Exactly one account opened. |
| `replay/approved-http500-member-detail` | `succeeded`, committed | One-off 500 retried, then committed. |
| `replay/hard-failure-http500-persistent` | `failed app_error` at s06 | The hard-failure bucket: 500 every time, two retries spent, then a failure with step, expected, observed and a screenshot. `retryable: true`, `commit_state: none`. |

## Stability

| Run | Result |
|---|---|
| `stability/stability.json` | 20 of 20 succeeded. One path across all runs, no drift, no recoveries. Engine time p50 956 ms, p95 958 ms, max 959 ms. |

## Handoff

| Run | Result | What it shows |
|---|---|---|
| `handoff/handoff-supervisor-override-automated` | `needs_human approval_required` at s13 | **Simulated.** A scripted operator, labelled as such, cleared the supervisor dialog on the live page. Verified, resumed at s07. It has no approval authority, so the run stopped at the commit gate. |
| `handoff/handoff-supervisor-override` | `failed operator_aborted` at s07 | Timeout path: I took control and never resumed within 15 minutes. Nothing committed. |
| `handoff/handoff-supervisor-override-2` | `failed operator_aborted` at s07 | Timeout path: nobody took control within 15 minutes. Nothing committed. |

There is no real human handoff yet. `README.md` at the repo root has the one command to record one.

## Second tenant

| Run | Result | What it shows |
|---|---|---|
| `tenants/lakeside-base-artifact-no-override` | `failed checkpoint_failed` at s05 | The tenant A artifact on tenant B. Drift at s04 (label missing, CSS fallback used), then the first screen with different wording fails its checkpoint. |
| `tenants/lakeside-with-tenant-override` | `needs_human approval_required` at s13 | Same artifact plus lakeside's overlay: 12 steps, zero drift, stopped at the commit gate because I didn't approve a commit here. |

## Provider

| Run | Result |
|---|---|
| `provider-smoke/openrouter-smoke.json` | One tiny tool-use call through OpenRouter. Served by Anthropic, $0.002568. |
