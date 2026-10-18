"""TESTS ONLY. A scripted stand-in for the model.

``ScriptedFakeProvider`` replays a fixed script of tool calls, resolving element refs against the
live snapshot it's shown. It exists so the discovery loop, compiler and validation replay can be
tested offline. Its ``name`` is "fake"; ``RunLog`` refuses to write fake runs into ``evidence/``, and
replays of artifacts it produced are refused too. Nothing it produces is evidence of anything.
"""

from __future__ import annotations

import re
from typing import Any

from agent_hands.llm.base import ToolCall, Turn

# A script entry: (tool name, input builder). The builder gets the latest observation text and
# returns the tool input; use ``ref_for`` to find an element by its snapshot line.
# An optional third element sets the turn's free text and stop_reason, e.g. {"text": "...",
# "stop_reason": "max_tokens"}, to test what happens to model narration and truncated turns.
Script = list[tuple[Any, ...]]


def ref_for(snapshot: str, pattern: str, nth: int = 0) -> str:
    """The ref of the nth snapshot line matching ``pattern`` (a regex)."""
    hits = [ln for ln in snapshot.splitlines() if re.search(pattern, ln) and "[ref=" in ln]
    if len(hits) <= nth:
        raise LookupError(f"fake provider: no snapshot line matches {pattern!r}")
    return hits[nth].split("[ref=")[1].split("]")[0]


class ScriptedFakeProvider:
    name = "fake"
    model = "scripted-fake"

    def __init__(self, script: Script) -> None:
        self.script = list(script)
        self.i = 0
        self.seen: list[list[dict[str, Any]]] = []

    def start(self, system: str, tools: list[dict[str, Any]]) -> None:
        self.system, self.tools = system, tools

    def step(self, user_content: list[dict[str, Any]]) -> Turn:
        self.seen.append(user_content)
        observation = "\n".join(
            c.get("text", "") if isinstance(c, dict) and c.get("type") == "text" else _tool_text(c)
            for c in user_content
        )
        if self.i >= len(self.script):
            return Turn([], "script exhausted", "end_turn", self.model)
        name, build, *extra = self.script[self.i]
        opts = extra[0] if extra else {}
        self.i += 1
        return Turn([ToolCall(f"fake-{self.i}", name, build(observation))], opts.get("text", ""),
                    opts.get("stop_reason", "tool_use"), self.model, f"fake-msg-{self.i}")


def _tool_text(block: Any) -> str:
    if isinstance(block, dict) and block.get("type") == "tool_result":
        content = block.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""
