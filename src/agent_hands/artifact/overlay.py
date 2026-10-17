"""Per-tenant overrides: one base artifact per vendor product, a small overlay per tenant.

Tenants running the same vendor product differ mostly in vocabulary ("Member #" vs "Member No.",
"Savings" vs "Share Account"), not in structure. So an overlay is data, not a fork:

- ``text``: replacements applied to text literals in locators, checkpoints and outcome detectors
  (one pass, longest match first). Positional CSS/XPath strategies are never touched.
- ``param_labels``: how a param value is shown on this tenant's screens (the option label to select,
  the value a checkpoint expects). The caller still passes and is billed for the base value.

The derived artifact keeps the base contract (params, outputs, outcomes), so its semver is the base
version; its content hash differs, and provenance records the base hash and the overlay. Approvals
bind to the derived hash, i.e. to exactly what runs.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import Field

from agent_hands.artifact.schema import CapabilityArtifact, Model


class TenantOverlay(Model):
    tenant: str
    capability: str  # capability id it applies to
    base_versions: str = "1.x"  # which base versions it was checked against
    text: dict[str, str] = {}
    param_labels: dict[str, dict[str, str]] = Field(default_factory=dict)


def _walk_literals(node: Any, fn) -> Any:
    """Rewrite every {"literal": ...} inside locator/checkpoint structures."""
    if isinstance(node, dict):
        if set(node) == {"literal"} and isinstance(node["literal"], str):
            return {"literal": fn(node["literal"])}
        return {k: _walk_literals(v, fn) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk_literals(v, fn) for v in node]
    return node


def apply_overlay(base: CapabilityArtifact, overlay: TenantOverlay) -> CapabilityArtifact:
    if overlay.capability != base.capability.id:
        raise ValueError(f"overlay is for {overlay.capability}, not {base.capability.id}")
    data = json.loads(base.canonical_json())
    if overlay.text:
        keys = sorted(overlay.text, key=len, reverse=True)
        rx = re.compile("|".join(re.escape(k) for k in keys))

        def swap(s: str) -> str:
            return rx.sub(lambda m: overlay.text[m.group()], s)

        for step in data["steps"]:
            if "target" in step:
                step["target"] = _walk_literals(step["target"], swap)
            step["expect"] = _walk_literals(step["expect"], swap)
            if "value" in step["action"]:
                step["action"]["value"] = _walk_literals(step["action"]["value"], swap)
        data["success"] = _walk_literals(data["success"], swap)
        for o in data["outcomes"]:
            o["detect"] = _walk_literals(o["detect"], swap)
            if "message_from" in o:
                o["message_from"] = _walk_literals(o["message_from"], swap)
    data["provenance"]["overlay"] = overlay.tenant
    data["provenance"]["base_hash"] = base.content_hash()
    return CapabilityArtifact.model_validate(data)


def overlay_for(tenant_config: dict[str, Any], capability_id: str) -> TenantOverlay | None:
    spec = (tenant_config.get("overrides") or {}).get(capability_id)
    if spec is None:
        return None
    return TenantOverlay.model_validate({"tenant": tenant_config["tenant"], "capability": capability_id, **spec})
