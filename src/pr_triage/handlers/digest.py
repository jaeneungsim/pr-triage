"""Morning digest of the PRs reviewed overnight. Built from stored results; no AI call."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import github, policy, store
from ..env import github_owner

ORDER = {"needs_human": 0, "changes_requested": 1, "approvable": 2}
TEXT = {"needs_human": "needs human review", "changes_requested": "changes requested", "approvable": "approvable"}
ISSUE_TITLE = "PR morning digest"


def build(runs: list[dict[str, Any]], now: datetime) -> str | None:
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for run in runs:
        key = (run["repo"], run["pr"])
        if key not in latest or run["finished_at"] > latest[key]["finished_at"]:
            latest[key] = run
    if not latest:
        return None
    rows = sorted(latest.values(), key=lambda r: (ORDER.get(r["decision"], 0), r.get("ticket") or "", r["repo"]))
    lines = [f"### PRs reviewed since yesterday evening ({now:%Y-%m-%d})", "| PR | Ticket | Result |", "|---|---|---|"]
    for run in rows:
        lines.append(
            f"| {run['repo'].rsplit('/', 1)[-1]}#{run['pr']} | {run.get('ticket') or '–'} | "
            f"{TEXT.get(run['decision'], run['decision'])} |"
        )
    waiting = sum(1 for r in rows if r["decision"] == "needs_human")
    cost = sum(r.get("cost_usd") or 0 for r in runs)
    lines.append(f"\n{waiting} PR(s) wait for a person. Reviews cost ${cost:.2f} in AI calls.")
    return "\n".join(lines)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    hours = int(os.environ.get("DIGEST_HOURS", "16"))
    body = build(store.runs_between(now - timedelta(hours=hours), now), now)
    if body is None:
        return {"posted": False}
    repo = f"{github_owner()}/{policy.load()['ticket']['repo']}"
    c = github.client(repo)
    issue = store.get_meta("digest-issue")
    if not issue:
        created = c.post(
            f"/repos/{repo}/issues",
            {
                "title": ISSUE_TITLE,
                "body": "pr-triage posts a summary of the PRs it reviewed overnight here every morning.",
            },
        )
        issue = created["number"]
        store.put_meta("digest-issue", issue)
    c.post(f"/repos/{repo}/issues/{issue}/comments", {"body": body})
    return {"posted": True, "issue": issue}
