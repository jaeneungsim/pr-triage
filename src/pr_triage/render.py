"""The PR comment and the check run text."""

from __future__ import annotations

from typing import Any

from .gh import COMMENT_MARKER

TITLES = {
    "approvable": "Approvable",
    "changes_requested": "Changes requested",
    "needs_human": "Needs human review",
}
ICONS = {"approvable": "✅", "changes_requested": "✋", "needs_human": "👀"}
CONCLUSIONS = {"approvable": "success", "changes_requested": "failure", "needs_human": "neutral"}
LABEL_KEYS = {"approvable": "approvable", "changes_requested": "changes_requested", "needs_human": "needs_human"}
OK_MARK = {True: "✔", False: "✘", None: "–"}


def cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def model_name(model_id: str) -> str:
    """`global.anthropic.claude-haiku-4-5-20251001-v1:0` -> `Claude Haiku 4.5`."""
    if "claude-" not in model_id:
        return model_id
    tokens = model_id.split("claude-", 1)[1].split("-")
    family = next((t for t in tokens if t.isalpha()), "")
    version = ".".join(t for t in tokens if t.isdigit() and len(t) <= 2)
    return f"Claude {family.capitalize()} {version}".strip()


def title(decision: dict[str, Any]) -> str:
    return TITLES[decision["decision"]]


def summary(
    state: dict[str, Any], doc: dict[str, Any], *, approved: bool = False, merge: dict[str, Any] | None = None
) -> str:
    ctx, decision = state["ctx"], state["decision"]
    outcome = decision["decision"]
    sha7 = ctx["sha"][:7]
    mode = _mode(ctx, decision, approved, merge)
    parts = [
        COMMENT_MARKER,
        f"### pr-triage: {ICONS[outcome]} {TITLES[outcome]}",
        f"Commit `{sha7}` · Ticket {ctx.get('ticket_key') or 'none'} · Level {ctx.get('level')} · {mode}",
    ]
    if decision["hits"]:
        parts.append("**Why**\n" + "\n".join(f"- {cell(h['message'])}" for h in decision["hits"]))
    elif outcome == "approvable":
        parts.append("**Why**\n- Every check passed and the AI review found nothing to fix.")
    if decision["notes"]:
        parts.append("**Notes**\n" + "\n".join(f"- {cell(h['message'])}" for h in decision["notes"]))
    parts += [
        _ai_section(state),
        _coverage_section(state, doc),
        _checks_section(state),
        _steps_section(state, doc),
        f"<sub>This result applies to commit {sha7} only; a new commit starts a new review. "
        "The AI suggests and code decides, using policy.yaml in pr-triage.</sub>",
    ]
    return "\n\n".join(p for p in parts if p)


def _mode(ctx: dict[str, Any], decision: dict[str, Any], approved: bool, merge: dict[str, Any] | None) -> str:
    base = ctx.get("base_ref") or "dev"
    if approved:
        if merge is None:
            return "Auto-approved by pr-triage."
        if not merge.get("approved", True):
            return (
                f"GitHub refused pr-triage's approval ({cell(merge.get('error'))}). A person can approve and merge it."
            )
        if merge.get("merged"):
            return f"Auto-approved and merged into `{base}` by pr-triage; no person was needed."
        return f"Auto-approved, but GitHub did not merge it ({cell(merge.get('error'))}). A person can merge it."
    if ctx.get("mode") != "auto-approve":
        return "Advisory mode: a person approves and merges."
    outcome = decision["decision"]
    if outcome == "approvable":
        blocker = decision.get("auto_blocker") or "it is not eligible"
        return f"Auto-approve mode: this PR waits for a person because {blocker}."
    if outcome == "changes_requested":
        return "Auto-approve mode: fix the findings and push; the new commit is reviewed again."
    return "Auto-approve mode: a person reviews this PR."


def approval(state: dict[str, Any]) -> str:
    """The body of pr-triage's approving review."""
    ctx = state["ctx"]
    if (state.get("policy") or {}).get("category") == "code":
        where = "in CI and on the AWS preview" if ctx.get("level") == "v3" else "in CI"
        why = (
            f"every check passed, the expected behavior ran and passed {where}, and the AI review found nothing to fix"
        )
    else:
        why = "it changes only docs or tests, and every check passed"
    return f"Approved by pr-triage for commit {ctx['sha'][:7]}: {why}."


def _ai_section(state: dict[str, Any]) -> str:
    review = state.get("review")
    if review is None:
        return "**AI review:** not run; an earlier check already decided the outcome."
    if review.get("status") != "ok":
        return f"**AI review:** not run ({cell(review.get('reason'))})."
    ctx = state["ctx"]
    usage = review["usage"]
    result = (state.get("validation") or {}).get("review") or review["result"]
    lines = [
        f"**AI review** · {model_name(review['model_id'])} on Amazon Bedrock · "
        f"{usage['input_tokens']:,} in / {usage['output_tokens']:,} out tokens · "
        f"${review['cost_usd']:.4f} · {review['latency_ms'] / 1000:.1f} s",
        f"Suggestion: **{result['suggestion']}** (confidence {float(result['confidence']):.2f})",
        "> " + cell(result.get("summary", "")),
    ]
    if result.get("findings"):
        lines.append("| Severity | Where | Finding |\n|---|---|---|")
        for f in result["findings"]:
            link = f"https://github.com/{ctx['repo']}/blob/{ctx['sha']}/{f['file']}#L{f['line']}"
            text = cell(f["title"]) + (f": {cell(f['evidence'])}" if f.get("evidence") else "")
            lines.append(f"| {f['severity']} | [{cell(f['file'])}:{f['line']}]({link}) | {text} |")
    for issue in result.get("cross_repo_issues", []):
        lines.append(f"- Cross-repo: {cell(issue.get('description', ''))}")
    problems = (state.get("validation") or {}).get("problems") or []
    if problems:
        items = "\n".join(f"- {cell(p)}" for p in problems)
        lines.append(f"<details><summary>Removed from the AI answer ({len(problems)})</summary>\n\n{items}\n</details>")
    return "\n".join(lines)


def _coverage_section(state: dict[str, Any], doc: dict[str, Any]) -> str:
    ticket = doc.get("ticket")
    policy = state.get("policy") or {}
    if not ticket or not ticket.get("requirements"):
        return ""
    coverage = policy.get("coverage") or {}
    result = (state.get("validation") or {}).get("review") or ((state.get("review") or {}).get("result")) or {}
    alignment = {a["requirement"]: a["status"] for a in result.get("ticket_alignment", [])}
    rows = [f"**Ticket {ticket['key']}**", "| Requirement | Expected behavior | AI check |", "|---|---|---|"]
    for rid, text in ticket["requirements"].items():
        if rid in coverage.get("here", []):
            covered = "this PR"
        elif rid in coverage.get("siblings", []):
            covered = "another PR of this ticket"
        else:
            covered = "not covered yet"
        rows.append(f"| {rid}: {cell(text)} | {covered} | {alignment.get(rid, '–')} |")
    return "\n".join(rows)


def _checks_section(state: dict[str, Any]) -> str:
    checks = state.get("checks")
    if not checks:
        return "**Checks:** not run; an earlier check already decided the outcome."
    if checks.get("timed_out"):
        return "**Checks:** CI did not finish within the waiting time."
    lines = ["**Checks**", "| Check | Result |", "|---|---|"]
    for name, run in (checks.get("workflows") or {}).items():
        result = "not found" if not run else f"[{run.get('conclusion') or run.get('status')}]({run.get('url')})"
        lines.append(f"| {name} | {result} |")
    relevance = checks.get("test_relevance")
    if relevance:
        lines.append(f"| test relevance | {relevance.get('verdict')}: {cell(relevance.get('message', ''))} |")
    for key, label in (("behavior_ci", "expected behavior in CI"), ("behavior_preview", "expected behavior on AWS")):
        lines.append(_behavior_row(checks.get(key), label))
    preview = checks.get("preview") or {}
    if preview.get("web_url"):
        lines.append(f"| web preview | {preview['web_url']} |")
    if preview.get("api_url"):
        lines.append(f"| API preview | `{preview.get('stack')}` |")
    return "\n".join(line for line in lines if line)


def _behavior_row(result: dict[str, Any] | None, label: str) -> str:
    if not result:
        return ""
    if result.get("error"):
        return f"| {label} | ✘ {cell(result['error'])} |"
    items = [i for i in result.get("items", []) if i.get("type") != "ui"]
    if not items:
        return f"| {label} | no api or log items |"
    passed = sum(1 for i in items if i.get("ok") is True)
    detail = "; ".join(f"{OK_MARK[i.get('ok')]} {i['id']} {cell(i.get('detail', ''))}" for i in items)
    return f"| {label} | {passed}/{len(items)} passed. {detail} |"


def _steps_section(state: dict[str, Any], doc: dict[str, Any]) -> str:
    checks = state.get("checks") or {}
    verified = {
        item.get("request")
        for key in ("behavior_preview", "behavior_ci")
        for item in (checks.get(key) or {}).get("items", [])
        if item.get("ok") is True and item.get("request")
    }
    steps = []
    result = (state.get("validation") or {}).get("review") or ((state.get("review") or {}).get("result")) or {}
    for step in result.get("verification_steps", []):
        text = f"{step['kind'].upper()}: {cell(step['action'])} → {cell(step['expected'])}"
        if step["kind"] == "api" and any(r and r in step["action"] for r in verified):
            text += " (already verified automatically)"
        steps.append(text)
    web_url = (checks.get("preview") or {}).get("web_url")
    for item in (doc.get("expected") or {}).get("items", []):
        if item["type"] != "ui":
            continue
        where = ""
        if web_url and item.get("page"):
            where = f" Open {web_url.rsplit('/', 1)[0]}{item['page']}"
        steps.append(f"UI ({item['id']}): {cell(item['step'])}{where}")
    if not steps:
        return ""
    return "**For the human reviewer**\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
