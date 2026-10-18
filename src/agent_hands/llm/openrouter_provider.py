"""OpenRouter provider: Claude Opus 5.5 through OpenRouter's Anthropic-compatible Messages endpoint.

Used for this take-home because an OpenRouter key is the key I had. Production would call
Anthropic directly, with zero data retention.

Why the Messages endpoint (and not OpenRouter's OpenAI-compatible chat completions): it takes the
same request the Anthropic provider sends, through the same SDK. Typed tools, ``tool_result`` with
``is_error``, image blocks and the append-only history need no translation, so the discovery loop is
byte-for-byte the same code on both providers, and there is no mapping layer to get subtly wrong.

Key handling, same rules as the Anthropic provider: read from the macOS Keychain (service
``openrouter-api-key``) only here, at startup; registered as a process secret so every redactor and
every log record scrubs it; never printed, logged, written or put in an exception message. If the
read fails we stop, and no other source is consulted. The key is sent only as a bearer token, only
to the hard-coded base URL. Environment variables that would make the SDK send it elsewhere, or add
headers, make startup fail.

Routing is pinned: ``provider: {only: ["anthropic"], allow_fallbacks: false}``, so every turn is
served by Anthropic or fails. Each turn's OpenRouter generation id (the ``X-Generation-Id`` response
header; the body ``id`` is Anthropic's own ``msg_`` id) is recorded, and
``generation_stats`` looks each one up afterwards for the upstream that actually served it and what
it cost. Anthropic-only betas (server-side refusal fallbacks) are not sent on this path.
"""

from __future__ import annotations

import os
import time
from typing import Any

import anthropic
import httpx2

from agent_hands.llm.anthropic_provider import turn_from_message
from agent_hands.llm.base import ProviderUnavailable, Turn
from agent_hands.redact import install_global_secret_scrub, register_process_secret
from agent_hands.secrets import SecretUnavailable, keychain_read

KEYCHAIN_SERVICE = "openrouter-api-key"
BASE_URL = "https://openrouter.ai/api"  # the SDK appends /v1/messages
GENERATION_URL = "https://openrouter.ai/api/v1/generation"
DEFAULT_SLUG = "anthropic/claude-opus-5.5"
_REDIRECTING_ENV = ("ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS")


class OpenRouterProvider:
    name = "openrouter"

    def __init__(
        self,
        model: str = DEFAULT_SLUG,
        *,
        effort: str = "high",
        max_tokens: int = 16000,
        upstream: tuple[str, ...] = ("anthropic",),
        prompt_cache: bool = True,
        max_retries: int = 2,
        http_client: httpx2.Client | None = None,  # tests inject a fake transport here
        key_reader: Any = keychain_read,
    ) -> None:
        self.model, self.effort, self.max_tokens, self.prompt_cache = model, effort, max_tokens, prompt_cache
        self.routing = {"only": list(upstream), "allow_fallbacks": False}
        try:
            key = key_reader(KEYCHAIN_SERVICE)
        except SecretUnavailable as e:
            raise ProviderUnavailable(
                f"Keychain item '{KEYCHAIN_SERVICE}' could not be read ({e}); stopping. "
                "No other key source is consulted."
            ) from None
        register_process_secret(key)
        install_global_secret_scrub()
        for var in _REDIRECTING_ENV:
            if os.environ.get(var):
                raise ProviderUnavailable(f"{var} is set; refusing to send the Keychain key anywhere but {BASE_URL}")
        # auth_token= is an explicit credential: the SDK then reads no credential env vars or profiles,
        # so an ANTHROPIC_API_KEY lying around is never sent to OpenRouter.
        # trust_env=False: no HTTPS_PROXY / ALL_PROXY / SSL_CERT_FILE from the environment can route the
        # key through a proxy or swap the CA it's verified against.
        http = http_client or anthropic.DefaultHttpxClient(trust_env=False)
        self._client = anthropic.Anthropic(
            auth_token=key, base_url=BASE_URL, max_retries=max_retries, timeout=300.0, http_client=http
        )
        self._http = http_client or httpx2.Client(timeout=30.0, trust_env=False)
        self._bearer = {"Authorization": f"Bearer {key}"}
        del key
        self.system = ""
        self.tools: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    def __repr__(self) -> str:  # never let a debug print show the headers
        return f"OpenRouterProvider(model={self.model!r}, routing={self.routing!r})"

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
            extra_body={"provider": self.routing},
        )
        if self.prompt_cache:
            kwargs["cache_control"] = {"type": "ephemeral"}
        try:
            raw = self._client.messages.with_raw_response.create(**kwargs)
            resp = raw.parse()
        except anthropic.AuthenticationError:
            raise ProviderUnavailable("OpenRouter rejected the Keychain key (401)") from None
        except anthropic.APIStatusError as e:
            # Keep the status and OpenRouter's own message; never the request (headers carry the key).
            raise ProviderUnavailable(f"OpenRouter returned {e.status_code}: {_message(e)}") from None
        self.messages.append({"role": "assistant", "content": resp.content})
        turn = turn_from_message(resp)
        # The body id is the upstream (Anthropic) message id; OpenRouter's own id is in a header.
        turn.generation_id = raw.headers.get("x-generation-id")
        return turn

    def generation_stats(self, ids: list[str], *, attempts: int = 6) -> list[dict[str, Any]]:
        """Per-turn upstream provider, served model and cost, from OpenRouter's own records."""
        keep = ("id", "model", "provider_name", "total_cost", "tokens_prompt", "tokens_completion",
                "native_tokens_reasoning", "native_tokens_cached", "finish_reason", "latency")
        out = []
        for gid in ids:
            row: dict[str, Any] = {"id": gid, "error": "unavailable"}
            for n in range(attempts):  # stats can lag the response by a few seconds
                try:
                    r = self._http.get(GENERATION_URL, params={"id": gid}, headers=self._bearer)
                except httpx2.HTTPError:
                    r = None
                if r is not None and r.status_code == 200:
                    data = r.json().get("data", {})
                    row = {k: data.get(k) for k in keep}
                    break
                if r is not None and r.status_code not in (404, 429, 500, 502, 503):
                    row = {"id": gid, "error": f"status {r.status_code}"}
                    break
                time.sleep(min(1.0 * (n + 1), 3.0))
            out.append(row)
        return out


def _message(e: Exception) -> str:
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"][:300]
    return type(e).__name__
