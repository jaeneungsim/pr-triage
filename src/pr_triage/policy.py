"""Gate 1 and 2: rules that code checks before any AI call. Same input, same answer."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from .env import package_root
from .globs import matching

CHANGES = "changes_requested"
HUMAN = "needs_human"
NOTE = "note"
BLOCKING = (CHANGES, HUMAN)


@dataclass(frozen=True)
class Hit:
    kind: str
    outcome: str
    message: str
    source: str = "policy"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@cache
def load(path: str | None = None) -> dict[str, Any]:
    file = Path(path) if path else package_root() / "policy" / "policy.yaml"
    return yaml.safe_load(file.read_text())


def repo_policy(policy: dict[str, Any], repo: str) -> dict[str, Any] | None:
    return policy["repos"].get(repo.rsplit("/", 1)[-1])


def required_workflows(policy: dict[str, Any], repo: str, level: str) -> list[str]:
    settings = repo_policy(policy, repo) or {}
    return list(settings.get("required_workflows", {}).get(level, []))


def categorize(paths: list[str], policy: dict[str, Any]) -> str:
    categories = policy["categories"]
    if paths and all(matching(p, categories["docs_only"]) for p in paths):
        return "docs_only"
    if paths and all(matching(p, categories["tests_only"]) for p in paths):
        return "tests_only"
    return "code"


def added_lines(files: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for file in files:
        for line in (file.get("patch") or "").splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                lines.append(line[1:])
    return lines


def tripwire(texts: list[str], patterns: list[str]) -> list[str]:
    """Short quotes of text that looks aimed at the reviewer."""
    quotes: list[str] = []
    for pattern in patterns:
        regex = re.compile(pattern, re.I)
        for text in texts:
            if match := regex.search(text or ""):
                quotes.append(match.group(0)[:80])
                break
    return quotes


def evaluate(ctx: dict[str, Any], policy: dict[str, Any], allowed_authors: tuple[str, ...]) -> dict[str, Any]:
    """`ctx` is the collected context document (see handlers/context.py)."""
    hits: list[Hit] = []
    files = ctx["files"]
    paths = [f["filename"] for f in files] + [f["previous_filename"] for f in files if f.get("previous_filename")]
    category = categorize(paths, policy)
    repo_name = ctx["repo"].rsplit("/", 1)[-1]
    ticket = ctx.get("ticket")
    key = ctx.get("ticket_key")

    if not key:
        hits.append(Hit("ticket", CHANGES, "No ticket key (for example INV-123) in the branch name or PR title."))
    elif ticket is None:
        error = ctx.get("ticket_error") or "it was not found"
        hits.append(
            Hit("ticket", CHANGES, f"tickets/{key}.md on the default branch of {policy['ticket']['repo']}: {error}.")
        )
    elif repo_name not in ticket["repos"]:
        hits.append(Hit("ticket", NOTE, f"{key} does not list {repo_name} in its repos."))

    expected = ctx["expected"]
    optional = ctx.get("pr_type") in policy["expected_behavior"]["optional_for_types"] or category == "docs_only"
    if expected["error"]:
        hits.append(Hit("expected", CHANGES, f"The expected-behavior block is invalid: {expected['error']}"))
    elif expected["missing"] and not optional:
        hits.append(Hit("expected", CHANGES, "The PR description has no ```yaml expected-behavior block."))
    elif not expected["items"] and not optional:
        hits.append(Hit("expected", CHANGES, "The expected-behavior block is empty."))

    coverage = _coverage(ctx, ticket, expected["items"])
    if coverage["unknown"]:
        hits.append(
            Hit(
                "expected",
                CHANGES,
                "Expected behavior covers requirement ids that the ticket does not have: "
                + ", ".join(coverage["unknown"])
                + ".",
            )
        )
    if coverage["missing"]:
        hits.append(Hit("coverage", NOTE, "No PR covers " + ", ".join(coverage["missing"]) + " yet."))
    for repo in coverage["repos_without_pr"]:
        hits.append(Hit("coverage", NOTE, f"{key} lists {repo}, which has no open PR for it yet."))

    sensitive = sorted({p for p in paths if matching(p, policy["sensitive_paths"])})
    if sensitive:
        hits.append(Hit("sensitive", HUMAN, "Touches sensitive paths: " + ", ".join(sensitive[:10]) + "."))

    changed = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
    if changed > policy["size"]["max_changed_lines"]:
        hits.append(Hit("size", HUMAN, f"{changed} changed lines (limit {policy['size']['max_changed_lines']})."))
    if len(files) > policy["size"]["max_files"]:
        hits.append(Hit("size", HUMAN, f"{len(files)} changed files (limit {policy['size']['max_files']})."))

    quotes = tripwire([ctx.get("title", ""), ctx.get("body", ""), *added_lines(files)], policy["injection_tripwire"])
    if quotes:
        hits.append(
            Hit("injection", HUMAN, "Text addressed to the reviewer was found: " + "; ".join(f'"{q}"' for q in quotes))
        )

    ai_allowed, reason = True, None
    if ctx["fork"]:
        if ctx.get("ai_ok_sha") != ctx["sha"]:
            ai_allowed = False
            reason = f"fork PR; a maintainer has not added `{policy['ai']['fork_label']}` for this commit"
    elif ctx["author"] not in allowed_authors:
        ai_allowed, reason = False, f"{ctx['author']} is not on the AI review allowlist"

    return {
        "blocking": any(h.outcome in BLOCKING for h in hits),
        "hits": [h.to_dict() for h in hits],
        "ai_allowed": ai_allowed,
        "ai_blocked_reason": reason,
        "category": category,
        "coverage": coverage,
        "changed_lines": changed,
    }


def _coverage(ctx: dict[str, Any], ticket: dict[str, Any] | None, items: list[dict[str, Any]]) -> dict[str, Any]:
    empty = {"required": [], "here": [], "siblings": [], "missing": [], "unknown": [], "repos_without_pr": []}
    if ticket is None:
        return empty
    required = list(ticket["requirements"])
    here = sorted({c for item in items for c in item.get("covers", [])})
    siblings = sorted({c for s in ctx.get("siblings", []) for c in s.get("covers", [])})
    repos_with_pr = {ctx["repo"].rsplit("/", 1)[-1]} | {s["repo"].rsplit("/", 1)[-1] for s in ctx.get("siblings", [])}
    return {
        "required": required,
        "here": here,
        "siblings": siblings,
        "missing": [r for r in required if r not in here and r not in siblings],
        "unknown": [c for c in here if c not in required],
        "repos_without_pr": [r for r in ticket["repos"] if r not in repos_with_pr],
    }
