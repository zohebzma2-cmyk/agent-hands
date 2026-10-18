"""OpenRouter provider, against a FAKE HTTP transport (tests only, no network, no real key).

Written before the implementation. The fake transport plays OpenRouter's Anthropic-compatible
Messages endpoint and its generation-stats endpoint; behind it, the scripted fake model decides.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx2
import pytest

from agent_hands.llm.base import ProviderUnavailable
from agent_hands.llm.fake import ScriptedFakeProvider
from agent_hands.redact import Redactor, clear_process_secrets
from agent_hands.secrets import SecretUnavailable

FAKE_KEY = "sk-or-v1-" + "0123456789abcdef" * 4  # shaped like a real key; obviously not one
SLUG = "anthropic/claude-opus-5.5"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    clear_process_secrets()
    yield
    clear_process_secrets()


class FakeOpenRouter:
    """Plays https://openrouter.ai/api/v1/messages and /api/v1/generation."""

    def __init__(self, script=None, status: int = 200):
        self.fake = ScriptedFakeProvider(script or [("done", lambda s: {"summary": "ok"})])
        self.status = status
        self.requests: list[httpx2.Request] = []
        self.n = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path == "/api/v1/generation":
            gid = request.url.params["id"]
            return httpx2.Response(200, json={"data": {"id": gid, "model": SLUG, "provider_name": "Anthropic",
                                                       "total_cost": 0.0021, "tokens_prompt": 10,
                                                       "tokens_completion": 5}})
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"code": self.status, "message": "nope"}})
        body = json.loads(request.content)
        turn = self.fake.step(body["messages"][-1]["content"])
        self.n += 1
        content = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in turn.tool_calls]
        if turn.text:
            content.insert(0, {"type": "text", "text": turn.text})
        return httpx2.Response(200, headers={"X-Generation-Id": f"gen-test{self.n:04d}"}, json={
            "id": f"msg_test{self.n:04d}", "type": "message", "role": "assistant", "model": SLUG,
            "content": content or [{"type": "text", "text": "done"}],
            "stop_reason": turn.stop_reason if turn.tool_calls else "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 20},
        })

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self))


def provider(fake: FakeOpenRouter, **kw):
    from agent_hands.llm.openrouter_provider import OpenRouterProvider

    return OpenRouterProvider(http_client=fake.client(), key_reader=lambda service: FAKE_KEY, max_retries=0, **kw)


# --- startup ----------------------------------------------------------------------


def test_missing_keychain_item_stops_cleanly():
    from agent_hands.llm.openrouter_provider import OpenRouterProvider

    def missing(service):
        raise SecretUnavailable(f"Keychain item '{service}' not found or unreadable")

    with pytest.raises(ProviderUnavailable) as ei:
        OpenRouterProvider(key_reader=missing)
    msg = str(ei.value)
    assert "openrouter-api-key" in msg and "\n" not in msg and "sk-or" not in msg


def test_reads_only_the_openrouter_keychain_service():
    from agent_hands.llm.openrouter_provider import OpenRouterProvider

    asked = []
    OpenRouterProvider(key_reader=lambda s: asked.append(s) or FAKE_KEY, http_client=FakeOpenRouter().client())
    assert asked == ["openrouter-api-key"]


@pytest.mark.parametrize("var", ["ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS"])
def test_env_override_that_could_redirect_the_key_is_refused(monkeypatch, var):
    monkeypatch.setenv(var, "https://attacker.invalid")
    with pytest.raises(ProviderUnavailable, match=var):
        provider(FakeOpenRouter())


def test_an_anthropic_key_in_the_env_is_never_sent(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-should-never-be-sent-anywhere")
    fake = FakeOpenRouter()
    p = provider(fake)
    p.start("system", [])
    p.step([{"type": "text", "text": "hi"}])
    req = fake.requests[0]
    assert "x-api-key" not in req.headers
    assert "should-never-be-sent" not in json.dumps(dict(req.headers))


# --- request shape -----------------------------------------------------------------


def test_request_goes_to_the_pinned_endpoint_with_pinned_routing():
    fake = FakeOpenRouter()
    p = provider(fake)
    p.start("system", [{"name": "done", "description": "d", "input_schema": {"type": "object", "properties": {}}}])
    turn = p.step([{"type": "text", "text": "hi"}])
    req = fake.requests[0]
    assert str(req.url) == "https://openrouter.ai/api/v1/messages"
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert "anthropic-beta" not in req.headers  # no Anthropic-only betas on this path
    body = json.loads(req.content)
    assert body["model"] == SLUG
    assert body["provider"] == {"only": ["anthropic"], "allow_fallbacks": False}
    assert "fallbacks" not in body
    assert body["output_config"] == {"effort": "high"}
    assert body["tool_choice"] == {"type": "auto"}
    assert turn.model == SLUG
    assert turn.message_id.startswith("msg_")  # the upstream (Anthropic) message id, as live
    assert turn.generation_id.startswith("gen-")  # OpenRouter's id, from the X-Generation-Id header


def test_generation_stats_report_upstream_and_cost():
    fake = FakeOpenRouter()
    p = provider(fake)
    p.start("system", [])
    turn = p.step([{"type": "text", "text": "hi"}])
    stats = p.generation_stats([turn.generation_id])
    assert stats[0]["provider_name"] == "Anthropic" and stats[0]["total_cost"] == 0.0021
    assert stats[0]["model"] == SLUG


# --- the key never leaks -----------------------------------------------------------


@pytest.mark.parametrize("status", [401, 500])
def test_key_never_in_exception_messages(status):
    p = provider(FakeOpenRouter(status=status))
    p.start("system", [])
    with pytest.raises(Exception) as ei:
        p.step([{"type": "text", "text": "hi"}])
    assert FAKE_KEY not in str(ei.value) and FAKE_KEY not in repr(ei.value)
    if status == 401:
        assert isinstance(ei.value, ProviderUnavailable)


def test_key_never_in_sdk_or_http_debug_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        p = provider(FakeOpenRouter())
        p.start("system", [])
        p.step([{"type": "text", "text": "hi"}])
    assert FAKE_KEY not in caplog.text


def test_key_is_a_process_secret_and_matches_the_pattern():
    provider(FakeOpenRouter())
    assert Redactor(patterns=()).scrub(f"x {FAKE_KEY} y") == "x [SECRET] y"
    clear_process_secrets()
    assert FAKE_KEY not in Redactor().scrub(f"leaked {FAKE_KEY}")  # pattern backstop alone


# --- end to end: provenance says openrouter, and the key appears nowhere ---------------


@pytest.mark.browser
def test_discovery_through_openrouter_records_provenance_and_never_leaks_the_key(tmp_path, caplog):
    from browser_helpers import allowlist_for
    from test_discovery import PARAMS, SPEC, happy_script

    import cu_sim
    from agent_hands.agent.pipeline import discover
    from agent_hands.handoff.operator import ScriptedOperator

    sim = cu_sim.start()
    fake = FakeOpenRouter(happy_script())
    try:
        with caplog.at_level(logging.DEBUG):
            res = discover(SPEC, PARAMS, provider(fake), origin=sim.url, allowlist=allowlist_for(sim.url),
                           run_id="or-test", run_dir=tmp_path, operator=ScriptedOperator())
    finally:
        sim.stop()
    assert res.error is None, res.error
    prov = res.artifact.provenance
    assert prov.provider == "openrouter" and prov.model == SLUG
    assert prov.models_served == [SLUG]
    assert prov.upstream_providers == ["Anthropic"]
    assert prov.generation_ids and all(g.startswith("gen-") for g in prov.generation_ids)
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["provider"] == "openrouter" and meta["model_requested"] == SLUG
    assert meta["routing"] == {"only": ["anthropic"], "allow_fallbacks": False}
    assert meta["upstream_providers"] == ["Anthropic"] and meta["total_cost_usd"] > 0
    assert all(m.startswith("msg_") for m in meta["message_ids"])
    assert meta["generation_ids"] == prov.generation_ids
    everything = "".join(p.read_text(errors="ignore") for p in Path(tmp_path).rglob("*") if p.is_file())
    assert FAKE_KEY not in everything and FAKE_KEY not in res.artifact.canonical_json()
    assert FAKE_KEY not in caplog.text



def test_clients_ignore_proxy_and_ca_environment(monkeypatch):
    from agent_hands.llm.openrouter_provider import OpenRouterProvider

    monkeypatch.setenv("HTTPS_PROXY", "http://mitm.invalid:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/attacker-ca.pem")
    p = OpenRouterProvider(key_reader=lambda s: FAKE_KEY)
    assert p._http.trust_env is False
    assert p._client._client.trust_env is False
