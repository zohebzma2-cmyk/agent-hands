# Handoff

The fault `unknown_dialog` puts a "Supervisor Override Required" box over the member page. The
replay has no handler for it, so at s07 it raises an intervention and hands the live session to an
operator.

## handoff-supervisor-override-automated (simulated)

This one is **simulated**. A scripted operator acted on the same live Playwright page instead of a
person. It is
labelled as an automated operator in `taken_by`, the lease log and `invocation.json`. It has no
approval authority.

What the files show:

- The lease went automation → awaiting_human → operator → verifying → automation.
- The operator's 3 actions were captured: PIN entry (recorded as `[MASKED]`), the click, the submit.
  The PIN does not appear anywhere in the evidence.
- The check on return passed: "step s07 target reachable and step s06 still holds".
- Automation resumed at s07 and ran s07 to s12.
- It stopped at s13 with `needs_human approval_required`, because nobody with approval authority was
  present. Nothing was committed.

## handoff-supervisor-override and handoff-supervisor-override-2 (timeout path)

These were meant to be my real handoff. I never got to the browser.

- `handoff-supervisor-override`: I typed `take`, then didn't resume within 15 minutes. The lease went
  human → aborted. Result `failed operator_aborted`, nothing committed.
- `handoff-supervisor-override-2`: nobody took control within 15 minutes. The lease went
  awaiting_human → aborted. Same result.

I kept both because the lease paths differ.

There is no real human handoff here yet. The root README has the command to record one.
