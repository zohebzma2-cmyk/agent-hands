# Second tenant

`cu_sim --tenant lakeside` runs the same vendor product with different branding and labels:
"Member No." instead of "Member #", "Primary Share" instead of "Base Share", "Share Account" instead
of "Savings", "Open Share" instead of "Open Account".

- `lakeside-base-artifact-no-override`: the ridgeline artifact as is. At s04 the label strategy finds
  nothing and the CSS fallback is used, which the result records as drift. At s05 the results header
  reads "Member No. Name Branch", so the checkpoint fails and the run stops there with expected,
  observed and a screenshot. Nothing was committed.
- `lakeside-with-tenant-override`: the same artifact with lakeside's overlay from
  `config/tenants/lakeside.json`. 12 steps, zero drift, every checkpoint held. It stopped at the
  commit gate because I didn't approve a commit for this run. The test suite runs the approved version
  to a committed "Share Account".
