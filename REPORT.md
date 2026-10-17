# Report

## Architecture

The flow is: contract, discovery, compile, validate, replay.

- A human writes the contract (`specs/open_sub_account.json`): params with types and sensitivity, secrets as references, outputs. The model never decides what goes in or out.
- Discovery: Claude Opus 5.5 drives a live browser through an observe, decide, act loop until it calls `done`.
- The compiler turns the run into a draft artifact. A validation replay with no model must pass before the draft is promoted.
- Replay runs the artifact with no model and returns one typed result.

Decisions and why:

- One Python process, synchronous Playwright, no services. The brief rewards a small correct system, and every service seam is an interface.
- The target is a local stand-in for a teller system I built to be hostile: framesets, table layout, no ids or test ids, unlabeled inputs, clickable cells. A public site would not let me inject the runtime faults.
- Perception is Playwright's AI-mode ARIA snapshot, which is the accessibility tree across every frame with element refs. Its roles match what the resolver uses, so what the model saw and what replay finds agree.
- The model acts on element refs, not screen coordinates. Refs can be described as locators. Coordinates can't.
- Module boundaries:
  - `surface/` is the only code that knows it's a browser.
  - `replay/` never imports a model provider, and a test enforces that.
  - `policy.py` and `handoff/` sit between every action and the page.
- The provider is a small interface. The run used OpenRouter for the take-home because that's the key I had; production would go direct with zero data retention. Routing was pinned to Anthropic with fallbacks off, and every turn's generation id, served model and cost are recorded.

## Artifact schema

The artifact is JSON with a pydantic schema (`artifact/schema.py`). A Markdown view is generated for reviewers.

| Field | Why |
|---|---|
| `schema_version` | So old files are migrated or refused, never guessed. |
| `capability.id`, `version` | The name an agent calls. Semver: major if params, outputs or outcomes change, minor if steps change, patch for metadata. `required_bump()` computes it. |
| `capability.status` | draft, validated, approved, deprecated. Replay refuses drafts. |
| `app.profile`, `profile_versions` | The vendor product, not the tenant. That's the unit of reuse. |
| `params[]` | Type, pattern, enum, bounds, `sensitivity`, `identifies_record`. Sensitivity drives redaction. Validated before the browser opens. |
| `secrets[]` | References only (`sim:`, `env:`, `keychain:`). |
| `outputs[]` | Type, sensitivity, how it may appear in logs, which step reads it. |
| `outcomes[]` | Business answers like `member_not_found`, with a detector and the steps they can occur at. |
| `conditions[]` | Recoverable states, referenced from the vendor profile (`cu_core.session_expired@1`). |
| `steps[].action` | Values are `{param}`, `{secret}` or `{literal}`. The artifact holds no member data. |
| `steps[].target` | Ordered strategies, a frame path, `min_agreement`. |
| `steps[].risk` | safe, reversible or irreversible, from profile rules, never from the model. |
| `steps[].expect` | Required on every step. Replay fails at the first step where reality stops matching. |
| `steps[].resume_anchor` | Where to resume after re-login. It can never cross an irreversible step; the schema rejects it. |
| `provenance` | Provider, requested and served models, generation ids, overlay and base hash. The content hash excludes it, so approving doesn't change what was approved. |

Locators, in order: role and accessible name, label, `near_text`, own text, CSS, XPath.

- A strategy counts only if it matches exactly one visible element. There is no "first match".
- The compiler keeps a strategy only if it resolved to the same element during discovery.
- Reads never use the value being read as a locator. The balance cell is found by row "Base Share" and column "Balance".
- Irreversible steps need two strategies to agree, one of them semantic.

## Determinism & error handling

Same artifact, params and app state give the same actions.

- Waits are on conditions, bounded by config. Retry schedules are fixed. No randomness. Time only bounds waits.
- Detectors run in a fixed order on every tick: server error and session expiry, then this step's business outcomes, then known dialogs.
- The result is one of four statuses:
  - `succeeded`
  - `outcome`: a business answer
  - `failed`: kind, step, expected, observed, screenshot, `retryable`
  - `needs_human`

  Recoveries are listed on every result. They are never a status.
- Every result carries `commit_state` (none, in_doubt, committed). Once anything irreversible was submitted, nothing is retryable and nothing is resubmitted.

Retry budgets, all fixed:

| Condition | Handling | Why |
|---|---|---|
| HTTP 500 | 2 retries, 500 ms then 1500 ms, GET only | A 500 on a GET is usually transient, and two tries cover that. Never after a submit. |
| Slow load | 1 GET retry after the step timeout | Covers one slow page without hiding a dead one. |
| Session expired | 1 re-login, resume at the anchor | One bounce is normal. Two means something is wrong. |
| Known dialog | dismiss, up to 2 | Interstitials repeat at most once in practice. |
| Unapproved submit blocked | 1 restore from the anchor | The guard proves nothing committed, so redoing the form is safe. |

What we ran:
- 20 of 20 identical replays.
- A persistent 500 fails at s06 as `app_error`, retryable, nothing committed.
- A 500 after the commit returns `commit_state_unknown`, never a resubmit.

## Heterogeneity & multi-tenant

**Legacy web vs desktop.** The seam is the surface: `observe()` returns a role and name tree with refs, `resolve()` turns a target into exactly one element, and `act()` acts on it.

- A desktop app has the same kind of tree through UI Automation on Windows or the AX API on macOS.
- Role and name strategies carry over as they are. `near_text` becomes "the control next to this label in the same container". The frame path becomes a window and pane path. CSS and XPath give way to AutomationId or control paths.
- When there is no accessibility tree at all (a remote desktop bitmap), screenshot and OCR is the last resort. It would always require agreement with a second signal before any irreversible step.
- The schema, replay engine, result contract and policy don't change. I built and ran only the web surface.

**One artifact across hundreds of tenants.** I keep one base artifact per vendor product and version, and a small overlay per tenant.

- Built: an overlay rewrites label text in locators and checkpoints ("Member #" to "Member No.") and maps how a param value is displayed ("Savings" to "Share Account"). Positional selectors are never touched.
- The derived artifact keeps the base contract and version, gets its own hash, and records the base hash. Approvals bind to what actually runs.
- Version pinning: the artifact names the profile versions it was validated on. Conditions are versioned (`@1`). A tenant config would pin an artifact version.
- Drift: every replay records which strategy matched at every step. Anything below the primary is a drift signal. Aggregated per tenant and step, a rising fallback rate means the tenant changed a label. The fix is an overlay proposed and reviewed by a human, not a re-recording.
- What we ran:
  - The tenant A artifact on tenant B drifts at s04 and fails its checkpoint at s05.
  - With lakeside's overlay it runs 12 steps with zero drift to the approval gate.
  - In the tests, the approved version commits a "Share Account".

## Escalation & handoff

What counts as stuck:
- **During discovery:** the model calls `request_human`, the same snapshot shows up 4 actions in a row, or it hits the step limit.
- **During replay:** an unknown overlay covers the target (a pre-click hit test), a checkpoint or target fails, a commit's state is unknown, or an unapproved submit has no safe anchor.

The intervention request carries:
- the capability and step
- the reason
- a masked screenshot
- what the human is asked to do
- what automation will check on return

Control is a lease with an epoch.
- Automation checks it before every action, so it can't act while the operator holds the session.
- The operator works in the same headed browser.
- Trusted input events are captured and redacted while the operator holds the lease. Password fields are recorded as masked.
- On return, automation re-checks the allowlist, the session, and the step. It retries only if the previous step's checkpoint, including the member number, still holds.

Taking over and approving commits are separate permissions. An operator that may take over isn't allowed to approve unless it says so.

What's mocked: the operator console is a terminal prompt. What's simulated: the handoff in `evidence/` was done by a scripted operator, labelled as such, because I didn't get to the browser in time. It shows the mechanism: verified, resumed at s07, stopped at the commit gate. My two real attempts timed out, and that timeout path is in the evidence too.

## Safety

- **Allowlist, enforced three times:**
  1. A browser-wide network route aborts any off-list origin or route, including popups, redirects and the human operator.
  2. Every action passes a gate.
  3. Every frame URL is re-checked after each action.
- **Approval gate.** Irreversible steps need an approval bound to the run id, the artifact hash and an HMAC of the params, and used once. Unattended replay stops there.
- **Commit guard.** A network backstop aborts any irreversible POST that no approval armed, whatever element triggered it.
- **Redaction.** Contract values first, then every data cell the model was shown, then patterns.
  - Screenshots mask inputs and value cells by matching in Python. No values are ever sent into the page.
  - The model never sees secret values. I found that Playwright's ARIA snapshot exposes password fields in plain text, so observations are scrubbed before they're sent.
- **API key.** Keychain only. It's registered as a process secret that every log record is scrubbed of. The base URL is pinned. Startup is refused if an env var could redirect the key.

Where the guardrails stop working:

- The model sees member PII on screen. Here it's synthetic. In production that needs a zero-retention agreement or tokenized observations.
- Free-text redaction is a heuristic. A name that never appeared in a table cell, or new wording, can get through.
- Screenshot masking is tuned to table layouts. A different app needs its own regions.
- The commit guard only watches document POSTs to actions the profile names. A commit by GET or a JSON XHR would get past it.
- Risk rules are only as good as the profile. An unlisted POST fails closed, but a commit that isn't a POST isn't caught.
- A human operator is exempt from the commit guard. Their actions are recorded, not blocked.
- Approvals are in-memory per process. There is no durable approval store or expiry yet.

Findings worth writing up:

- **Labels share rows.** Rows hold several label and input pairs, so "same row" had to mean "the nearest cell with a control".
- **Cell names include input values.** A table cell's accessible name includes the value typed into its input, so role strategies became ambiguous after typing. Uniqueness caught it.
- **Detached frames linger.** Playwright keeps detached frames in `child_frames` after a frame-busting navigation.
- **Password values leak into the snapshot.** Playwright's ARIA snapshot shows password values in plain text.
- **My first validation run failed for a reason that wasn't a bug in the approval.** Input reached the browser during a 9-minute approval wait. The guard blocked the submit, and the engine mislabelled it as `target_not_found`. I reproduced it at the exact commit and fixed it: focus is cleared before prompts, and a blocked submit restores from a safe anchor.
- **Redaction mangled the audit trail.** The account-number pattern matched the unix time inside OpenRouter generation ids. It's fixed, and the affected run is annotated.

## Cuts

- **The operator console** is a terminal. Next: a small web queue with claim, release and timeouts.
- **No real human handoff in the evidence yet.** The README has the one command to record it.
- **Desktop surface:** designed, not built.
- **No recompiling from a trace.** I don't persist the raw trace because it holds page text. Next: persist a redacted trace so a better compiler can be re-run without a new model run. That's why the discovered s14 locator is CSS only; the compiler fix applies to future runs.
- **Not built:** LLM-assisted single-step recovery on replay failure, a capability catalog an agent can call, and drift aggregation across runs (each run reports its own drift).
- **Approval store:** no expiry, nothing durable across processes.
- **Timing:** numbers come from a local simulator. They show consistency, not real-world speed.
