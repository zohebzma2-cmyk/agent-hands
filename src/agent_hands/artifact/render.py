"""A Markdown view of an artifact for human review. Generated, never edited by hand."""

from __future__ import annotations

from agent_hands.artifact.schema import (
    CapabilityArtifact,
    LiteralValue,
    ParamRef,
    SecretValue,
    Strategy,
)
from agent_hands.surface.web import describe_condition


def _v(v) -> str:
    if isinstance(v, ParamRef):
        return f"`{{{v.param}}}`"
    if isinstance(v, SecretValue):
        return f"`secret:{v.secret}`"
    if isinstance(v, LiteralValue):
        return f'"{v.literal}"'
    return ""


def _strategy(s: Strategy) -> str:
    k = s.kind
    if k == "role":
        return f"role {s.role} {_v(s.name)}"
    if k == "label":
        return f"label {_v(s.label)}"
    if k == "near_text":
        col = f", column {_v(s.column)}" if s.column is not None else ""
        return f"{s.role} nearest {_v(s.anchor)} in its row{col}"
    if k == "text":
        return f"text {s.match} {_v(s.text)}"
    if k == "css":
        return f"css `{s.selector}`"
    return f"xpath `{s.xpath}`"


def render(a: CapabilityArtifact) -> str:
    c = a.capability
    out = [
        f"# {c.title}",
        "",
        f"`{a.ref}` · status **{c.status.value}** · risk **{c.risk.value}** · `{a.content_hash()}`",
        "",
        c.description,
        "",
        f"App profile `{a.app.profile}` ({a.app.profile_versions}), entry `{a.app.entry_route}`.",
        "",
        "## Contract",
        "",
        "| Param | Type | Sensitivity | Constraint |",
        "|---|---|---|---|",
    ]
    for p in a.params:
        cons = p.pattern or (", ".join(p.enum) if p.enum else "") or (
            f"{p.minimum}–{p.maximum}" if p.minimum is not None else "")
        out.append(f"| `{p.name}` | {p.type.value} | {p.sensitivity.value} | {cons} |")
    out += ["", "| Output | Type | Sensitivity | In logs | From |", "|---|---|---|---|---|"]
    out += [f"| `{o.name}` | {o.type.value} | {o.sensitivity.value} | {o.redaction.value} | {o.source_step} |"
            for o in a.outputs]
    out += ["", "Business outcomes: " + (", ".join(f"`{o.code}` (at {', '.join(o.at_steps)})" for o in a.outcomes)
                                          or "none"),
            "", "Recoverable conditions: " + ", ".join(f"`{c}`" for c in a.conditions), "", "## Steps", ""]
    for s in a.steps:
        val = f" {_v(s.action.value)}" if s.action.value is not None else ""
        val += f" → `{s.action.output}`" if s.action.output else ""
        out.append(f"### {s.id} · {s.intent}  ({s.phase}, {s.risk.value})")
        out.append("")
        out.append(f"- **do:** {s.action.kind.value}{val}")
        if s.target is not None:
            where = "/".join(r.name or r.url_pattern or "?" for r in s.target.frame) or "top"
            agree = f", {s.target.min_agreement} must agree" if s.target.min_agreement > 1 else ""
            out.append(f"- **target:** {s.target.description} in `{where}`{agree}")
            out += [f"  {i + 1}. {_strategy(st)}" for i, st in enumerate(s.target.strategies)]
        out.append("- **checkpoint:** " + "; ".join(describe_condition(x) for x in s.expect.all_of)
                   + f" within {s.expect.timeout_ms} ms")
        if s.resume_anchor:
            out.append(f"- **after re-login resume at:** {s.resume_anchor}")
        out.append("")
    out += ["## Success", "", "; ".join(describe_condition(x) for x in a.success.all_of), "",
            "## Provenance", "", f"```json\n{a.provenance.model_dump_json(indent=2)}\n```", ""]
    return "\n".join(out)
