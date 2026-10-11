"""Gate 7: check the AI answer against the code before anyone sees it.

The model can invent files, lines, endpoints and log groups. Anything that does not exist is
removed and reported. If the answer leans on something invented, it is not trusted at all.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

SUGGESTIONS = ("approve", "changes_requested", "needs_human")
SEVERITIES = ("high", "medium", "low")
COVERAGE = ("implemented", "missing", "contradicted")
ALIGNMENT = ("met", "partial", "not_met", "not_applicable")
STEP_KINDS = ("ui", "api", "log")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
LIST_FIELDS = ("findings", "behavior_coverage", "ticket_alignment", "cross_repo_issues", "verification_steps")

LOG_GROUP = re.compile(r"^/aws/lambda/inv-api-(?:preview-inv-\d+|dev)-handler$")
LOG_GROUP_IN_TEXT = re.compile(r"/aws/lambda/[A-Za-z0-9_\-./]+")
ROUTE_IN_TEXT = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[^\s`'\"),;]*)")

Route = tuple[str, re.Pattern[str]]


def openapi_routes(text: str | None) -> list[Route] | None:
    """(method, path regex) pairs from an OpenAPI document. None when it cannot be read."""
    if not text:
        return None
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    paths = doc.get("paths") if isinstance(doc, dict) else None
    if not isinstance(paths, dict):
        return None
    routes: list[Route] = []
    for template, operations in paths.items():
        if not isinstance(operations, dict):
            continue
        segments = [
            "[^/]+" if seg.startswith("{") and seg.endswith("}") else re.escape(seg)
            for seg in str(template).strip("/").split("/")
        ]
        regex = re.compile("^/" + "/".join(segments) + "$") if segments != [""] else re.compile("^/$")
        routes += [(m.upper(), regex) for m in operations if m.upper() in METHODS]
    return routes


def route_exists(routes: list[Route], method: str, path: str) -> bool:
    clean = path.split("?", 1)[0].rstrip(".:").rstrip("/") or "/"
    return any(m == method and regex.match(clean) for m, regex in routes)


def validate_review(
    review: Any,
    *,
    changed_files: set[str],
    line_counts: dict[str, int],
    expected_ids: set[str],
    requirement_ids: set[str],
    routes: list[Route] | None,
) -> dict[str, Any]:
    schema = _schema_problems(review)
    if schema:
        return {"ok": False, "critical": True, "problems": schema, "review": None}

    problems: list[str] = []
    cleaned = dict(review)

    findings, high_dropped = [], False
    for finding in review["findings"]:
        reason = _finding_problem(finding, changed_files, line_counts)
        if reason:
            where = f"{finding.get('file')}:{finding.get('line')}" if isinstance(finding, dict) else "?"
            problems.append(f"finding at {where} removed: {reason}")
            high_dropped = high_dropped or (isinstance(finding, dict) and finding.get("severity") == "high")
        else:
            findings.append(finding)
    cleaned["findings"] = findings

    cleaned["behavior_coverage"] = _keep(
        review["behavior_coverage"], "id", expected_ids, COVERAGE, "expected-behavior id", problems
    )
    cleaned["ticket_alignment"] = _keep(
        review["ticket_alignment"], "requirement", requirement_ids, ALIGNMENT, "requirement id", problems
    )

    steps = []
    for step in review["verification_steps"]:
        reason = _step_problem(step, routes)
        if reason:
            problems.append(f"verification step removed: {reason}")
        else:
            steps.append(step)
    cleaned["verification_steps"] = steps

    unsupported = review["suggestion"] == "changes_requested" and bool(review["findings"]) and not findings
    total_steps = len(review["verification_steps"])
    steps_unreliable = total_steps >= 2 and len(steps) * 2 < total_steps
    return {
        "ok": not problems,
        "critical": high_dropped or unsupported or steps_unreliable,
        "problems": problems[:20],
        "review": cleaned,
    }


def _schema_problems(review: Any) -> list[str]:
    if not isinstance(review, dict):
        return ["the answer is not a JSON object"]
    problems = []
    if review.get("suggestion") not in SUGGESTIONS:
        problems.append(f"suggestion {review.get('suggestion')!r} is not one of {', '.join(SUGGESTIONS)}")
    confidence = review.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float) or not 0 <= confidence <= 1:
        problems.append("confidence is not a number between 0 and 1")
    if not isinstance(review.get("summary"), str):
        problems.append("summary is missing")
    for name in LIST_FIELDS:
        if not isinstance(review.get(name), list):
            problems.append(f"{name} is not a list")
    return problems


def _finding_problem(finding: Any, changed_files: set[str], line_counts: dict[str, int]) -> str | None:
    if not isinstance(finding, dict) or finding.get("severity") not in SEVERITIES:
        return "malformed"
    file, line = finding.get("file"), finding.get("line")
    if not isinstance(file, str) or isinstance(line, bool) or not isinstance(line, int) or line < 1:
        return "malformed"
    if file not in changed_files:
        return "the file is not part of this PR"
    count = line_counts.get(file)
    if count is not None and line > count:
        return f"line {line} is past the end of the file ({count} lines)"
    return None


def _keep(
    items: list[Any], key: str, allowed: set[str], statuses: tuple[str, ...], label: str, problems: list[str]
) -> list[Any]:
    kept = []
    for item in items:
        if not isinstance(item, dict) or item.get("status") not in statuses:
            problems.append(f"malformed {label} entry removed")
        elif item.get(key) not in allowed:
            problems.append(f"unknown {label} {item.get(key)!r} removed")
        else:
            kept.append(item)
    return kept


def _step_problem(step: Any, routes: list[Route] | None) -> str | None:
    if not isinstance(step, dict) or step.get("kind") not in STEP_KINDS:
        return "malformed"
    text = f"{step.get('action', '')} {step.get('expected', '')}"
    if step["kind"] == "api":
        match = ROUTE_IN_TEXT.search(str(step.get("action", "")))
        if not match:
            return "an api step without a method and path"
        method, path = match.group(1), match.group(2)
        if routes is not None and not route_exists(routes, method, path):
            return f"{method} {path} is not in the OpenAPI document"
    if step["kind"] == "log":
        groups = LOG_GROUP_IN_TEXT.findall(text)
        if not groups:
            return "a log step without a log group"
        bad = [g for g in groups if not LOG_GROUP.match(g.rstrip("."))]
        if bad:
            return f"log group {bad[0]} does not follow the naming rule"
    return None
