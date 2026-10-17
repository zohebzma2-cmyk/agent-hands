# Stability

`stability.json`: the happy path replayed 20 times from my command (`--approve s13`, one approval per
run, each bound to its own run id), against a fresh simulator state each time.

- 20 of 20 succeeded.
- One distinct path: every run used the same steps and the same locator strategy at every step.
- No drift, no recoveries.
- Engine time p50 956 ms, p95 958 ms, max 959 ms. Per-step medians are in the file.

This is a local simulator, so the timing says the engine is consistent, not how fast a real core
banking system is.
