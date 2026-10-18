# Report

Longer detail: [`docs/design-notes.md`](docs/design-notes.md). Evidence: [`evidence/`](evidence/README.md).

## 1. Architecture

A human writes the contract: typed params with sensitivity, secrets as references, outputs. Claude Opus 5.5 discovers how to do the task in a live browser. A compiler turns the run into a draft, and a validation replay with no model must pass before it's promoted. Replay then runs it with no model.

- One Python process, no services. Every seam is an interface I can split out later.
- Python because Playwright and the Anthropic SDK are first class there. Pydantic because the schema is the contract, so validation and JSON come from one definition.
- The target is a hostile stand-in teller app I built (framesets, table layout, no ids) so I could inject faults.
- Perception is Playwright's ARIA snapshot: the accessibility tree across all frames, with element refs, in the same role vocabulary the resolver uses.
- The agent loop gives the model 8 tools: click, fill, select, read, screenshot, request_human, done and give_up. It acts on element refs, not coordinates, because refs become locators. Fill and select take a param or secret name and the runtime substitutes the value. That's how the compiler parameterizes without guessing, and why the model doesn't see the password. It stops on done (only once every output is read), give_up, a refusal, 40 turns, 15 minutes, 4 actions with no change on screen, no tool call after a nudge, and safety stops.
- `surface/` does the perceiving and acting. Replay still calls Playwright directly for navigation, waits, frame lookup and the commit guard. `replay/` never imports a model provider, and a test enforces that.
- I used OpenRouter for the take-home because that's the key I had. Production would go direct with zero data retention. Routing was pinned to Anthropic, and OpenRouter's records show all 8 turns served there.

## 2. Artifact schema

Artifacts are stored as canonical JSON with a rendered `.md` review page. Schema: [`schema.py`](src/agent_hands/artifact/schema.py). Example: [the discovered artifact](artifacts/cu_core.open_sub_account@1.0.0.json) and [its review page](artifacts/cu_core.open_sub_account@1.0.0.md).

- The contract (params, outputs, outcomes) is separate from the steps. Changing it is a major version.
- Params carry `sensitivity`. Redaction is driven by the contract first.
- Values in steps are `{param}`, `{secret}` or `{literal}`. The artifact holds no member data.
- Every step has its own checkpoint. Replay fails at the first step where reality stops matching.
- Each step has a risk class from the vendor profile's rules, not from the model.
- Business outcomes live in the artifact. Recoverable conditions live in the vendor profile, because they're the same for every capability and tenant.
- A target is an ordered list of strategies: role and name, label, label in the same table row, text, CSS, XPath. A strategy counts only if it matches exactly one element, and the compiler keeps it only if it matched during discovery. Irreversible steps need two to agree.
- The content hash excludes status and provenance, so approving doesn't change what was approved.

## 3. Determinism & error handling

Same artifact, params and app state give the same actions. Waits are on conditions with bounded timeouts. Retry schedules are fixed. Detectors run in a fixed order. Nothing picks a first match.

Every result is one of four statuses:

- `succeeded`, with outputs.
- `outcome`: a business answer, like `member_not_found`. Not a crash.
- `failed`: kind, step, expected, observed, screenshot, `retryable`.
- `needs_human`: approval required, unexpected state, or commit state unknown.

Recoverable conditions are handled and listed on the result, not reported as a status. Every result also carries `commit_state`. A 500 after a submit is not retried.

The persistent 500 returns `retryable: true`: nothing was committed, so the caller can try again later. Once anything irreversible was submitted, `retryable` is always false.

Retry budgets: a 500 gets 2 retries at 500 ms then 1500 ms, GET only, because a 500 on a GET is usually transient. A session expiry gets 1 re-login. A known dialog gets 2 dismissals.

What I ran: 20 of 20 identical replays, a persistent 500 failing at s06, and three recoveries that each committed once.

## 4. Heterogeneity & multi-tenant

**Legacy web vs desktop.** The seam is the surface. `observe()` returns a role and name tree with refs, `resolve()` turns a target into exactly one element, and `act()` acts on it.

- A desktop app exposes the same kind of tree through UI Automation on Windows or the AX API on macOS.
- Role and name strategies carry over. "Label in the same row" becomes "control next to this label in the same container". The frame path becomes a window and pane path. AutomationId replaces CSS.
- With no accessibility tree (a remote desktop bitmap), screenshots and OCR are the last resort, and an irreversible step needs a second agreeing signal.
- The schema, result contract and policy stay the same. Replay's own Playwright calls (navigation, waits, frame lookup, the commit guard) move behind the protocol first. I built only the web surface.

**One artifact across hundreds of tenants.** One base artifact per vendor product and version, plus a small overlay per tenant.

- An overlay rewrites label text in locators and checkpoints, and records how param values are displayed. It doesn't touch positional selectors.
- The derived artifact has its own hash, so approvals cover exactly what runs.
- Artifacts pin the vendor versions they were validated on.
- Drift: every replay records which strategy matched at every step. A rising fallback rate for a tenant and step means a label changed. The fix is a reviewed overlay, not a re-recording.
- What I ran: tenant A's artifact on tenant B drifts at s04 and fails at s05. With the overlay, 12 steps run with zero drift.

## 5. Escalation & handoff

- **Stuck during discovery:** the model calls `request_human`, or a stop condition above hits.
- **Stuck during replay:** an unknown overlay covers the target, a checkpoint or target fails, a commit's state is unknown, or an unapproved submit can't be safely redone.

The intervention request carries the step, reason, masked screenshot, the ask, and what automation checks on return.

- **Control is a lease with an epoch.** Automation checks it before every action, so it can't act while a human holds the session.
- **The human works in the same headed browser.** Their input is captured and redacted.
- **On return,** automation re-checks the allowlist, the session, and the step. It continues only if the previous step's checkpoint, including the member number, still holds.
- **Taking over and approving commits are separate permissions.** An operator without approval authority is blocked at the network from irreversible actions.

What I ran: a real handoff. I took control at s07, entered the supervisor PIN in the live browser and resumed. 4 actions were captured with the PIN masked, the check passed, and the run committed. A simulated run shows separation of duties, and a timeout run shows nobody returning.

## 6. Safety

- **The allowlist is enforced three times:** a browser-wide network route (covering popups, redirects and the human), a gate on every action, and a re-check of every frame after each action.
- **Irreversible steps need an approval** bound to the run id, the artifact hash and an HMAC of the params, used once. Unattended replay stops there.
- **A network commit guard** aborts any irreversible POST no approval armed, whatever element triggered it.
- **Redaction** covers contract values, every data cell the model was shown, and patterns. Screenshots are masked by matching in Python, so no value enters the page. Playwright's ARIA snapshot exposes password fields, so secrets are scrubbed before the model sees anything.
- **The key** comes from the Keychain, is scrubbed from every log record, and only goes to a pinned base URL over a client that ignores proxy settings.

Where the guardrails stop working:

- The model sees member PII on screen. Production needs zero retention or tokenized observations.
- Free-text redaction is a heuristic. A name that never appeared in a table cell can get through.
- The commit guard only watches POSTs to actions the profile names. A commit by GET or a JSON call would get past it.
- A human with approval authority is exempt from the commit guard. Their submits are recorded.
- Approvals live in memory per process, with no durable store or expiry.

## 7. Cuts

- Stretch goals, exactly two. Confidence and approval: the draft to validated gate, with the 20 replay stability run as the reliability score. Cross-tenant reuse: the lakeside overlay.
- The operator console is a terminal prompt. The lease, capture and verification are the hard part, and a terminal exercises all three. Next: a web queue.
- The artifact-level `approved` status isn't wired. Per-run approvals already cover the commit, so I left it out.
- No raw trace is persisted, because it holds page text, so recompiling needs a new model run. That's why the discovered s14 locator is CSS only. Next: a redacted trace.
- Not built: single-step LLM recovery, a callable capability catalog, drift aggregation across runs.
