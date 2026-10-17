"""The provider seam: the discovery loop talks to this, never to a vendor SDK.

A provider holds one append-only conversation. ``step`` sends the next user turn (tool results and a
fresh observation) and returns the model's next turn as plain data. The model is a config value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Turn:
    tool_calls: list[ToolCall]
    text: str
    stop_reason: str
    model: str  # the model that actually served this turn (fallbacks can change it)
    message_id: str | None = None
    request_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class ProviderUnavailable(RuntimeError):
    """Raised (without credentials in the message) when a provider can't start."""


class Provider(Protocol):
    name: str  # "anthropic" or "fake"; evidence writing keys off this
    model: str

    def start(self, system: str, tools: list[dict[str, Any]]) -> None: ...
    def step(self, user_content: list[dict[str, Any]]) -> Turn: ...
