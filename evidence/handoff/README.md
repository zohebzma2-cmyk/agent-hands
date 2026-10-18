# Handoff

The fault `unknown_dialog` puts a "Supervisor Override Required" box over the member page. Replay
has no handler for it, so at s07 it raises an intervention and hands the live session to an operator.

## handoff-human-live (the real one)

I ran this myself in my terminal with a headed browser.

- Replay stopped at s07 and raised `INT-handoff-human-live-1`.
- I typed `take` (22:59:03 UTC). The lease went automation → awaiting_human → human (`zoheb`).
- In the browser I entered the supervisor PIN and clicked Approve Override. 4 actions were captured:
  a click on the PIN field, the PIN change (recorded as `[MASKED]`), the click on Approve Override,
  the form submit. The PIN appears nowhere in the evidence.
- I typed `resume` (22:59:20). The check on return passed: "step s07 target reachable and step s06
  still holds".
- Automation resumed at s07 and finished s07 to s14. Result `succeeded`, `commit_state: committed`.
  The commit was approved by my `--approve s13` on the command line.

## handoff-supervisor-override-automated (simulated, the test version)

This one is **simulated**. A scripted operator acts on the same live page in place of a human,
without approval authority, to test separation of duties. It is labelled
`scripted-operator (simulated; no approval authority)` in `taken_by`, the lease log and
`invocation.json`.

The operator takes over and tries the supervisor override. The override is an irreversible,
human-only action in the profile, so the commit guard blocks it at the network (`network_blocked`,
`/core/override.do` in `run.jsonl`). The blocked submit leaves the main frame on Chromium's error
page, so the check on return fails twice and the run ends `failed unexpected_state`. Nothing is
committed.

An earlier version of this run let that override through. The final review caught it (fix in commit
c4c64bb), and this folder was re-recorded after the fix.

## handoff-supervisor-override (timeout path)

My first attempt. I typed `take` and didn't get to the browser. After 15 minutes the lease went
human → aborted and the run ended `failed operator_aborted`. Nothing was committed.
