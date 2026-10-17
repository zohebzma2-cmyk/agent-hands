"""One tiny live call through OpenRouter: key, endpoint, slug, routing, tool use. Prints no secrets."""
import json, time
from agent_hands.llm.openrouter_provider import OpenRouterProvider
from agent_hands.llm.base import ProviderUnavailable
from agent_hands.redact import Redactor

TOOL = {"name": "echo", "description": "Echo a short word back.",
        "input_schema": {"type": "object", "properties": {"word": {"type": "string"}},
                         "required": ["word"], "additionalProperties": False}}
try:
    p = OpenRouterProvider(max_tokens=2000, max_retries=0)
except ProviderUnavailable as e:
    print("STOP:", e); raise SystemExit(1)
p.start("You are a smoke test. Reply only by calling the echo tool.", [TOOL])
t0 = time.monotonic()
try:
    turn = p.step([{"type": "text", "text": "Call echo with the word 'ok'."}])
except Exception as e:
    print("ERROR:", Redactor().scrub_exception(e)); raise SystemExit(1)
print(json.dumps({
    "latency_s": round(time.monotonic() - t0, 2), "requested_slug": p.model, "routing": p.routing,
    "served_model": turn.model, "message_id": turn.message_id, "generation_id": turn.generation_id, "stop_reason": turn.stop_reason,
    "tool_calls": [{"name": c.name, "input": c.input} for c in turn.tool_calls], "usage": turn.usage,
}, indent=2))
stats = p.generation_stats([turn.generation_id])
print("generation record:", json.dumps(stats, indent=2))
