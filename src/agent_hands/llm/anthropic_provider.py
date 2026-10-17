"""Anthropic Messages API provider for discovery runs.

The API key comes from the macOS Keychain (service ``anthropic-api-key``), read only when this
provider starts. It lives in memory only: it's registered as a process secret so every redactor
and the logging filter scrub it, and it never appears in an exception message. If the Keychain read
fails we stop; no other key source is consulted.

Request shape (Claude Opus 5.5): adaptive thinking is always on, so effort is the control and is set
explicitly; tool_choice stays "auto" (forced tool choice is rejected on this model); the conversation
is append-only, with every assistant turn appended exactly as returned; automatic prompt caching
keeps the growing prefix cheap; and server-side refusal fallbacks are enabled, with the serving model
recorded per turn in the evidence.
"""

from __future__ import annotations

from typing import Any

import anthropic

from agent_hands.llm.base import ProviderUnavailable, ToolCall, Turn
from agent_hands.redact import register_process_secret
from agent_hands.secrets import SecretUnavailable, keychain_read

KEYCHAIN_SERVICE = "anthropic-api-key"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-opus-5-5",
        *,
        effort: str = "high",
        max_tokens: int = 16000,
        fallbacks: bool = True,
    ) -> None:
        self.model, self.effort, self.max_tokens, self.fallbacks = model, effort, max_tokens, fallbacks
        try:
            key = keychain_read(KEYCHAIN_SERVICE)
        except SecretUnavailable:
            raise ProviderUnavailable(
                f"Keychain item '{KEYCHAIN_SERVICE}' could not be read; stopping. "
                "No other key source is consulted."
            ) from None
        register_process_secret(key)
        self._client = anthropic.Anthropic(api_key=key, max_retries=2, timeout=300.0)
        del key
        self.system = ""
        self.tools: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    def start(self, system: str, tools: list[dict[str, Any]]) -> None:
        self.system, self.tools, self.messages = system, tools, []

    def step(self, user_content: list[dict[str, Any]]) -> Turn:
        self.messages.append({"role": "user", "content": user_content})
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": self.system}],
            tools=self.tools,
            tool_choice={"type": "auto"},
            messages=self.messages,
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
        )
        try:
            if self.fallbacks:
                resp = self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            else:
                resp = self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError:
            raise ProviderUnavailable("the Anthropic API rejected the Keychain key (401)") from None
        # Append exactly what came back (thinking blocks included): the history must stay append-only.
        self.messages.append({"role": "assistant", "content": resp.content})
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        text = "\n".join(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        usage = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", None),
            "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", None),
        }
        return Turn(
            tool_calls=calls,
            text=text,
            stop_reason=str(resp.stop_reason),
            model=resp.model,
            message_id=resp.id,
            request_id=getattr(resp, "_request_id", None),
            usage=usage,
        )
