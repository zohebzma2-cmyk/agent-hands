# Provider smoke test

`openrouter-smoke.json`: one tiny call through the OpenRouter provider before the real run, to prove
the key, endpoint, model slug, routing and tool use. No browser, no target app.

Served by Anthropic as `anthropic/claude-opus-5.5-20260921`. One `echo` tool call. Cost $0.002568.

An earlier identical call proved the same path, but its generation lookup failed because the
generation id comes back in a response header, not the body. The provider reads the header now.
