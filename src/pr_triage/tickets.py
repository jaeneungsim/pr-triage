"""Ticket keys and ticket files (`tickets/INV-123.md` in the ticket repo)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

import yaml

from . import markdown

_REQUIREMENT = re.compile(r"^\s*[-*]\s*(R\d+)\s*[:.)]\s*(.+?)\s*$")


class TicketFormatError(ValueError):
    pass


def key_pattern(prefix: str) -> re.Pattern[str]:
    """Same rule as the commit hook and the workflows: `\\bINV-(\\d+)\\b`, case-sensitive."""
    return re.compile(rf"\b{re.escape(prefix)}-(\d+)\b")


def extract_key(texts: Iterable[str | None], prefix: str = "INV") -> str | None:
    """The first ticket key found, in the order given (branch name before PR title)."""
    pattern = key_pattern(prefix)
    for text in texts:
        if text and (match := pattern.search(text)):
            return f"{prefix}-{match.group(1)}"
    return None


def stage_for(key: str) -> str:
    return key.lower()


@dataclass(frozen=True)
class Ticket:
    key: str
    title: str
    status: str
    repos: tuple[str, ...]
    requirements: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "repos": list(self.repos),
            "requirements": self.requirements,
        }


def parse_ticket(text: str) -> Ticket:
    lines = markdown.normalize(text).split("\n")
    if not lines or lines[0].strip() != "---":
        raise TicketFormatError("the ticket has no front matter")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise TicketFormatError("the front matter is not closed")
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as err:
        raise TicketFormatError(f"the front matter is not valid YAML: {err}") from err
    if not isinstance(meta, dict):
        raise TicketFormatError("the front matter is not a mapping")
    repos = meta.get("repos") or []
    if not isinstance(repos, list):
        raise TicketFormatError("repos must be a list")
    return Ticket(
        key=str(meta.get("key", "")),
        title=str(meta.get("title", "")),
        status=str(meta.get("status", "")),
        repos=tuple(str(r) for r in repos),
        requirements=parse_requirements("\n".join(lines[end + 1 :])),
    )


def parse_requirements(body: str) -> dict[str, str]:
    requirements: dict[str, str] = {}
    for line in (markdown.section(body, "Requirements") or "").splitlines():
        if match := _REQUIREMENT.match(line):
            requirements.setdefault(match.group(1), match.group(2))
    return requirements
