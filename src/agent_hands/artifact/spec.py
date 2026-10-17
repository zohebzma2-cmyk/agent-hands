"""The capability contract a discovery run starts from.

A human declares *what* the capability is: its name, typed params with
sensitivity, the secrets it may use and the outputs it must return. The model only discovers *how*:
the steps, the targets and the checkpoints. It can't invent parameters or decide what's sensitive.
"""

from __future__ import annotations

from pydantic import Field

from agent_hands.artifact.schema import (
    CAPABILITY_ID,
    IDENT,
    Model,
    OutputType,
    ParamSpec,
    Redaction,
    SecretSpec,
    Sensitivity,
)


class OutputDecl(Model):
    name: str = Field(pattern=IDENT)
    type: OutputType
    description: str
    sensitivity: Sensitivity
    redaction: Redaction


class CapabilitySpec(Model):
    id: str = Field(pattern=CAPABILITY_ID)
    title: str
    description: str
    goal: str  # the natural-language goal; may mention params as {name}
    profile: str = Field(pattern=IDENT)
    profile_versions: str
    entry_route: str = Field(pattern=r"^/")
    params: list[ParamSpec]
    secrets: list[SecretSpec] = []
    outputs: list[OutputDecl]
    preconditions: list[str] = []
