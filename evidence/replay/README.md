# Replays

Deterministic replays of `cu_core.open_sub_account@1.0.0`, the artifact discovery produced. No model
is involved. Each folder has `invocation.json` with the artifact status, the faults injected and any
pre-approved steps.

- `unattended-*` ran with no approval. They show recovery and then stop at the irreversible step with
  `needs_human approval_required`, which is what unattended replay must do. These ran on the draft
  before promotion, flagged with `--allow-draft`.
- `approved-*` ran from my own command with `--approve s13`, my explicit approval for that one run.
  All four committed exactly one account each.
- `hard-failure-http500-persistent` shows the hard-failure bucket: the server returns 500 every time,
  the retry budget (2) runs out, and the result names the step, what was expected, what was observed,
  and points to a screenshot. `retryable` is true because nothing was committed.

The 500 retry budget is 2 with a fixed 500 ms then 1500 ms backoff. Why: a 500 on a GET is usually
transient, two tries cover that, and a fixed schedule keeps replay deterministic.
