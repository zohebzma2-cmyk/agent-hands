# Report

Longer detail: [`docs/design-notes.md`](docs/design-notes.md). Evidence: [`evidence/`](evidence/README.md).

## Architecture

A human writes the contract: params with types and sensitivity, secrets as references, outputs. Claude Opus 5.5 discovers how to do the task in a live browser. A compiler turns the run into a draft artifact. A validation replay with no model must pass before the draft is promoted. Replay then runs the artifact with no model.

- One Python process, no services. A small correct system beats infrastructure.
- The target is a hostile stand-in teller app I built (framesets, table layout, no ids), so I could inject runtime faults.
- Perception is Playwright's ARIA snapshot: the accessibility tree across all frames, with element refs. Its roles match the resolver's, so what the model saw is what replay finds.
- The model acts on element refs, not coordinates, because refs become locators.
- `surface/` is the only code that knows it's a browser. `replay/` never imports a model provider; a test enforces it.
- I used OpenRouter for the take-home because that's the key I had. Production would go direct with zero data retention. Routing was pinned to Anthropic with fallbacks off; OpenRouter's own records show all 8 turns served by Anthropic.

## Artifact schema

- The contract (params, outputs, business outcomes) is separate from the steps. A contract change is a major version.
- Params carry `sensitivity`. Redaction is driven by the contract first.
- Values in steps are `{param}`, `{secret}` or `{literal}`. The artifact holds no member data.
- Every step has its own checkpoint. Replay fails at the first step where reality stops matching.
- Each step has a risk class from the vendor profile's rules, never from the model.
- Business outcomes live in the artifact. Recoverable conditions live in the vendor profile, because they're the same for every capability and tenant.
- A target is an ordered list of strategies: role and name, label, label in the same table row, text, CSS, XPath. A strategy counts only if it matches exactly one element, and the compiler keeps it only if it matched during discovery. Irreversible steps need two to agree.
- The content hash excludes status and provenance, so approving doesn't change what was approved.

## Determinism & error handling

Same artifact, params and app state give the same actions. Waits are on conditions with bounded timeouts. Retry schedules are fixed. Detectors run in a fixed order. Nothing picks a first match.

Every result is one of four statuses:

- `succeeded`, with outputs.
- `outcome`: a business answer, like `member_not_found`. Not a crash.
- `failed`: kind, step, expected, observed, screenshot, `retryable`.
- `needs_human`: approval required, unexpected state, or commit state unknown.

Recoverable conditions are handled and listed on the result. They are never a status.

Every result also carries `commit_state`. A 500 after a submit is never retried.

The persistent 500 returns `retryable: true` because nothing was committed and the caller can safely try the whole invocation later. Once anything irreversible was submitted, `retryable` is always false.

Retry budgets: a 500 gets 2 retries at 500 ms then 1500 ms, GET only, because a 500 on a GET is usually transient. A session expiry gets 1 re-login. A known dialog gets 2 dismissals.

What I ran: 20 of 20 identical replays; a persistent 500 failing at s06 with nothing committed; and an announcement, a session expiry and a one-off 500, each recovered and then committed once.

## Heterogeneity & multi-tenant

**Legacy web vs desktop.** The seam is the surface. `observe()` returns a role and name tree with refs, `resolve()` turns a target into exactly one element, and `act()` acts on it.

- A desktop app exposes the same kind of tree through UI Automation on Windows or the AX API on macOS.
- Role and name strategies carry over. "Label in the same row" becomes "control next to this label in the same container". The frame path becomes a window and pane path. AutomationId replaces CSS.
- With no accessibility tree at all, such as a remote desktop bitmap, screenshots and OCR are the last resort, and an irreversible step would need a second agreeing signal.
- The schema, replay engine, result contract and policy stay the same. I built only the web surface.

**One artifact across hundreds of tenants.** One base artifact per vendor product and version, plus a small overlay per tenant.

- An overlay rewrites label text in locators and checkpoints, and records how param values are displayed. It never touches positional selectors.
- The derived artifact has its own hash, so approvals cover exactly what runs.
- Artifacts pin the vendor versions they were validated on.
- Drift: every replay records which strategy matched at every step. Aggregated per tenant and step, a rising fallback rate means a tenant changed a label. The fix is a reviewed overlay, not a re-recording.
- What I ran: the tenant A artifact on tenant B drifts at s04 and fails its checkpoint at s05. With the overlay it runs 12 steps with zero drift to the approval gate.

## Escalation & handoff

- **Stuck during discovery:** the model calls `request_human`, the page doesn't change for 4 actions, or it hits the step limit.
- **Stuck during replay:** an unknown overlay covers the target, a checkpoint or target fails, a commit's state is unknown, or an unapproved submit can't be safely redone.

The intervention request carries the step, reason, masked screenshot, the ask, and what automation checks on return.

- **Control is a lease with an epoch.** Automation checks it before every action, so it can't act while a human holds the session.
- **The human works in the same headed browser.** Their input events are captured and redacted.
- **On return,** automation re-checks the allowlist, the session, and the step. It continues only if the previous step's checkpoint, including the member number, still holds.
- **Taking over and approving commits are separate permissions.** An operator without approval authority is blocked at the network from irreversible actions.

What I ran: a real handoff. I took control at s07, entered the supervisor PIN in the live browser and resumed. 4 actions were captured with the PIN masked, the check passed, and the run finished and committed. The operator console is a terminal prompt, by design. A simulated operator run shows the separation of duties. A timeout run shows what happens when nobody returns.

## Safety

- **The allowlist is enforced three times:** a browser-wide network route (covering popups, redirects and the human), a gate on every action, and a re-check of every frame after each action.
- **Irreversible steps need an approval** bound to the run id, the artifact hash and an HMAC of the params, used once. Unattended replay stops there.
- **A network commit guard** aborts any irreversible POST no approval armed, whatever element triggered it.
- **Redaction** covers contract values, every data cell the model was shown, and patterns. Screenshots are masked by matching in Python; no value is ever sent into the page. The model never sees secret values. Playwright's ARIA snapshot exposes password fields, so observations are scrubbed first.
- **The key** comes from the Keychain, is scrubbed from every log record, and only goes to a pinned base URL over a client that ignores proxy settings.

Where the guardrails stop working:

- The model sees member PII on screen. Production needs zero retention or tokenized observations.
- Free-text redaction is a heuristic. A name that never appeared in a table cell can get through.
- The commit guard only watches POSTs to actions the profile names. A commit by GET or a JSON call would get past it.
- A human with approval authority is exempt from the commit guard. Their submits are recorded, not blocked.
- Approvals live in memory per process, with no durable store or expiry.

## Cuts

- The operator console is a terminal. Next: a web queue with claim, release and timeouts.
- Desktop surface: designed, not built.
- I don't persist the raw trace because it holds page text, so an artifact can't be recompiled without a new model run. That's why the discovered s14 locator is CSS only; the compiler fix applies to future runs. Next: a redacted trace.
- Not built: LLM-assisted single-step recovery, a capability catalog an agent can call, and drift aggregation across runs.
