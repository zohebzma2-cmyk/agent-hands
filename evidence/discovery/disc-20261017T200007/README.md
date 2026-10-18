# disc-20261017T200007

The one real LLM discovery run.

- Command: `uv run hands discover --spec specs/open_sub_account.json -p member_id=900001
  -p account_type=Savings -p nickname="Rainy day" -p initial_deposit=25.00 --headed --out evidence/discovery`
- Model: `anthropic/claude-opus-5.5` through OpenRouter, routing pinned to Anthropic with fallbacks
  off. OpenRouter's own records say every turn was served by Anthropic as
  `anthropic/claude-opus-5.5-20260921`.
- 8 model turns, 14 browser actions, ended with `done`. Total cost $0.156849.
- I approved the irreversible "Open Account" click live in my terminal at 20:09:37 UTC. The commit
  guard let exactly that one submit through.

## Files

| File | What it is |
|---|---|
| `meta.json` | Provider, requested model, served models, routing, per-turn usage and cost, approvals, lease log. |
| `run.jsonl` | Every observation, model turn, tool call, tool result and trace step, redacted. |
| `screens/` | The one screenshot the model asked for (login page, inputs masked). |
| `compile_report.json` | Per step: strategies kept and rejected, checkpoint chosen, risk class and why. |
| `compiled_draft.json` | The artifact as compiled, before validation. |
| `artifact.json` | The same artifact after validation, status `validated`. |
| `generations.json` | OpenRouter's record of each turn, looked up by generation id. |
| `validation_replay/` | First validation attempt. Aborted, see below. |
| `validation_disc-...-20261017T204352/` | Second validation attempt. Succeeded and promoted the artifact. |

## Known issue: generation ids in run.jsonl and meta.json

The OpenRouter generation ids in `run.jsonl` and `meta.json` read like `gen-[ACCT …7210]-...`. The
redactor's account-number pattern matched the 10-digit unix time inside each id. That bug was fixed
in commit e837dfd, after this run. I left these files as written.

The real ids are intact in `compiled_draft.json` and `artifact.json` provenance. Those files were
written verbatim during the run, after a redaction lint, so they are the original record, not a
rebuild. `generations.json` lists the same 8 ids with OpenRouter's answer for each. The Anthropic
message ids (`msg_...`) and the per-turn costs in `meta.json` were never affected.

## The first validation attempt

The validation replay filled the form, then waited 9 minutes for my approval. During that wait,
input reached the browser and submitted the form. No approval had armed the commit guard yet, so it
blocked the submit. Nothing was committed. The form became Chromium's blocked page, and the engine reported
`target_not_found` at s13 because the button was gone. I aborted the handoff.

Commit e837dfd has the reproduction and the fixes: focus is cleared before any human prompt, and a
blocked submit now recovers from a safe resume point. The second attempt succeeded.
