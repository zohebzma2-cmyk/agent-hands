# Second tenant

`cu_sim --tenant lakeside` runs the same vendor product with different branding and labels:
"Member No." instead of "Member #", "Primary Share" instead of "Base Share", "Share Account" instead
of "Savings", "Open Share" instead of "Open Account".

## lakeside-base-artifact-no-override

The ridgeline artifact as is. At s04 the primary `near_text` strategy (the "Member #:" label in the
same row) finds nothing, so the CSS fallback is used and the result records drift. At s05 the results
header reads "Member No. Name Branch", so the checkpoint fails. The run stops there with expected,
observed and a screenshot. Nothing was committed.

## lakeside-with-tenant-override

The same artifact with lakeside's overlay from `config/tenants/lakeside.json`. It got past both
places the base artifact had trouble: s04 used its primary strategy (no drift) and s05's checkpoint
held. 12 steps ran with zero drift.

It stopped at s13, the irreversible "Open Share" click, with `needs_human approval_required`. That is
by design: I ran it without `--approve`, and unattended replay never commits on its own. The test
suite runs the approved version to a committed "Share Account".

The derived artifact's hash includes the tenant's display labels ("Savings" shown as "Share
Account"), so an approval covers exactly what gets selected on screen.
