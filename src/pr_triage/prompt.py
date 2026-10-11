"""Build the AI review request from the collected context.

Untrusted text goes inside tags, and any closing tag inside it is broken up so the text cannot
end its own section. The system prompt says the content of those tags is data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from typing import Any

from .env import package_root
from .params import Limits

CHARS_PER_TOKEN = 3.0  # code is dense; this overestimates tokens, which is the safe side
DIFF_SHARE = 0.6  # a diff larger than this share of the budget is not reviewed partially


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str
    estimated_tokens: int
    omitted: tuple[str, ...]


@cache
def system_prompt() -> str:
    return (package_root() / "prompts" / "system.md").read_text()


@cache
def tool_schema() -> dict[str, Any]:
    return json.loads((package_root() / "prompts" / "submit_review.schema.json").read_text())


def wrap(tag: str, content: str, **attrs: str) -> str:
    safe = content.replace(f"</{tag}", f"<\\/{tag}").replace(f"<{tag}", f"<\\{tag}")
    attributes = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return f"<{tag}{attributes}>\n{safe}\n</{tag}>"


def numbered(text: str) -> str:
    return "\n".join(f"{i:>5}| {line}" for i, line in enumerate(text.splitlines(), 1))


def format_diff(files: list[dict[str, Any]]) -> str:
    parts = []
    for f in files:
        header = f"### {f['filename']} ({f.get('status')}, +{f.get('additions', 0)} -{f.get('deletions', 0)})"
        patch = f.get("patch")
        parts.append(f"{header}\n```diff\n{patch}\n```" if patch else f"{header}\n(no text diff)")
    return "\n\n".join(parts)


def build(ctx: dict[str, Any], checks: dict[str, Any], limits: Limits) -> Prompt | None:
    """None when the diff alone is too large to review in one call."""
    budget = int(limits.max_input_tokens * CHARS_PER_TOKEN)
    system = system_prompt()
    diff = format_diff(ctx["files"])
    if len(diff) > budget * DIFF_SHARE:
        return None

    ticket = ctx.get("ticket")
    ticket_text = (
        "No ticket file was found."
        if ticket is None
        else "\n".join(
            [f"{ticket['key']}: {ticket['title']}", "Requirements:"]
            + [f"- {rid}: {text}" for rid, text in ticket["requirements"].items()]
        )
    )
    metadata = json.dumps(
        {
            "repo": ctx["repo"],
            "number": ctx["pr"],
            "title": ctx["title"],
            "author": ctx["author"],
            "type": ctx.get("pr_type"),
            "base": ctx["base_ref"],
            "head": ctx["head_ref"],
            "commit": ctx["sha"],
        },
        indent=1,
    )
    sections = [
        wrap("ticket", ticket_text, key=str(ctx.get("ticket_key") or "none")),
        wrap("pr_metadata", metadata),
        wrap("pr_description", (ctx.get("body") or "")[:6000]),
        wrap("expected_behavior", json.dumps(ctx["expected"], indent=1)),
        wrap("checks", json.dumps(_checks_for_prompt(checks), indent=1)),
        wrap("diff", diff),
    ]
    if ctx.get("openapi"):
        sections.append(wrap("openapi", ctx["openapi"][:8000]))

    used = len(system) + sum(len(s) for s in sections)
    omitted: list[str] = []
    file_sections = []
    for path, text in ctx.get("contents", {}).items():
        section = wrap("file", numbered(text), path=path)
        if used + len(section) > budget:
            omitted.append(path)
            continue
        file_sections.append(section)
        used += len(section)
    if file_sections:
        sections.append(wrap("files", "\n\n".join(file_sections)))

    related = _related(ctx.get("siblings", []))
    if related and used + len(related) <= budget:
        sections.append(wrap("related_prs", related))
        used += len(related)
    elif related:
        omitted.append("related PRs")

    if omitted:
        sections.append(f"Not included to stay within the size limit: {', '.join(omitted)}.")
    user = "Review this pull request.\n\n" + "\n\n".join(sections)
    return Prompt(system, user, int((len(system) + len(user)) / CHARS_PER_TOKEN), tuple(omitted))


def _checks_for_prompt(checks: dict[str, Any]) -> dict[str, Any]:
    return {
        "workflows": {name: (w or {}).get("conclusion") for name, w in (checks.get("workflows") or {}).items()},
        "test_relevance": checks.get("test_relevance"),
        "expected_behavior_ci": checks.get("behavior_ci"),
        "expected_behavior_preview": checks.get("behavior_preview"),
    }


def _related(siblings: list[dict[str, Any]]) -> str:
    parts = []
    for s in siblings:
        parts.append(
            f"## {s['repo']}#{s['number']}: {s.get('title', '')}\n"
            f"covers: {', '.join(s.get('covers', [])) or 'unknown'}\n" + (s.get("diff") or "(diff not available)")
        )
    return "\n\n".join(parts)
