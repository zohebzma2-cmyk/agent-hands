# Design notes

The longer detail behind [`REPORT.md`](../REPORT.md).

## Artifact fields

| Field | Why |
|---|---|
| `schema_version` | So old files are migrated or refused, never guessed. |
| `capability.id`, `version` | The name an agent calls. Semver: major if params, outputs or outcomes change, minor if steps change, patch for metadata. `required_bump()` computes it, and promotion refuses to overwrite a version with different content. |
| `capability.status` | draft, validated, approved, deprecated. Replay refuses drafts; only `hands validate` runs one to completion. |
| `app.profile`, `profile_versions` | The vendor product, not the tenant. That's the unit of reuse. |
| `params[]` | Type, pattern, enum, bounds, `sensitivity`, `identifies_record`. Validated before the browser opens. |
| `secrets[]` | References only (`sim:`, `env:`, `keychain:`). |
| `outputs[]` | Type, sensitivity, how it may appear in logs, which step reads it. |
| `outcomes[]` | Business answers like `member_not_found`, with a detector and the steps they can occur at. |
| `conditions[]` | Recoverable states, referenced from the vendor profile (`cu_core.session_expired@1`). |
| `steps[].action` | Values are `{param}`, `{secret}` or `{literal}`. The artifact holds no member data. |
| `steps[].target` | Ordered strategies, a frame path, `min_agreement`. |
| `steps[].risk` | safe, reversible or irreversible, from the profile's rules, never from the model. |
| `steps[].expect` | Required on every step. |
| `steps[].resume_anchor` | Where to resume after re-login. The schema rejects an anchor that would re-run an irreversible step. |
| `display_labels` | Set by a tenant overlay: how param values appear on that tenant's screens. Part of the hash. |
| `provenance` | Provider, requested and served models, generation ids, overlay, base hash, validation runs. Excluded from the content hash with `status`, so approving doesn't change what was approved. |

## Locators

Ranked: role and accessible name, label, `near_text` (the control nearest a label in the same table
row, or a cell by row label and column header), own text, CSS, XPath.

- Unique or it's a miss: zero matches and several matches are both misses. There is no first match.
- Validated at record time: the compiler keeps a strategy only if it resolved to the same element.
- Reads are never keyed on the value being read.
- Text equal to a param value is stored as `{param}`.
- Irreversible steps need two strategies to agree, one of them semantic.
- Every replay records each strategy's outcome per step. Anything below the primary is drift.

## Retry budgets

| Condition | Handling | Why |
|---|---|---|
| HTTP 500 | 2 retries, 500 ms then 1500 ms, GET only | A 500 on a GET is usually transient. Two tries cover that. Never after a submit. |
| Slow load | 1 GET retry after the step timeout | Covers one slow page without hiding a dead one. |
| Session expired | 1 re-login, resume at the anchor | One bounce is normal. Two means something is wrong. |
| Known dialog | dismiss, up to 2 | Interstitials repeat at most once in practice. |
| Unapproved submit blocked | 1 restore from the anchor | The guard proves nothing committed, so redoing the form is safe. |
| Handoffs | 2 per run | A second failed verification ends the run. |

## Fault mapping

| Fault | Result |
|---|---|
| member not found | `outcome member_not_found` |
| validation error | `outcome validation_rejected` |
| permission denied | `outcome permission_denied` |
| announcement | recovered: dismissed |
| unknown dialog | handoff, or `needs_human` with no operator |
| session expired | recovered: re-login, resume |
| slow load | recovered: waited or one retry; persistent: `failed timeout` |
| 500 once | recovered: retried |
| 500 every time | `failed app_error`, retryable, nothing committed |
| 500 after the submit | `needs_human commit_state_unknown`, never resubmitted |

## Tenant overlays

An overlay (`config/tenants/<tenant>.json`, `overrides`) has `text` replacements for label literals
and `param_labels` for how a param value is displayed. `apply_overlay` writes a derived artifact with
the base contract and version, rewritten literals, `display_labels`, and provenance naming the base
hash and the tenant. Positional selectors are never touched. Validation always runs the base artifact.

## Findings

- Rows hold several label and input pairs, so "same row" had to mean "nearest cell with a control".
- A table cell's accessible name includes the value typed into its input, so role strategies became
  ambiguous after typing. The uniqueness rule caught it.
- Playwright keeps detached frames in `child_frames` after a frame-busting navigation.
- Playwright's AI-mode ARIA snapshot shows password values in plain text. Observations are scrubbed
  of secrets before the model sees them.
- The first validation run failed because input reached the browser during a 9-minute approval wait
  and submitted the form. The guard blocked it, and the engine mislabelled it `target_not_found`.
  Reproduced at the exact commit; fixed by clearing focus before prompts and restoring from a safe
  anchor after a blocked submit.
- The account-number redaction pattern matched the unix time inside OpenRouter generation ids. Fixed;
  the affected run is annotated.
- The final review found that an operator without approval authority could still commit during a
  handoff, because the guard exempted whoever held the lease. Fixed: only operators with approval
  authority are exempt, and every pass and block is logged.

## Guardrail limits, in full

- The model sees member PII on screen. Synthetic here. Production needs zero retention or tokenized
  observations.
- Free-text redaction is a heuristic. A name that never appeared in a table cell, or new wording, can
  get through.
- Screenshot masking is tuned to table layouts.
- The commit guard watches document POSTs to actions the profile names. A commit by GET or a JSON XHR
  would get past it. An unlisted POST fails closed.
- A human with approval authority is exempt from the commit guard during a handoff. Their submits are
  recorded and tracked as commits, not blocked.
- Approvals are in memory per process. No durable store, no expiry.
- The key is read only by the provider at startup, and by the audit scripts to check it never
  appears in files. It is never printed.
