"""Catch-all: any step failed, so tell the PR a person has to review it.

Without this, a failed run would leave the required check unfinished and the PR stuck.
The error name is shown; details stay in the logs.
"""

from __future__ import annotations

from typing import Any

from .. import gh, github, store
from ..gh import COMMENT_MARKER


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    state, execution = event["state"], event["execution"]
    request, ctx = state["request"], state.get("ctx") or {}
    error = (state.get("error") or {}).get("Error", "unknown error")
    repo, number, sha = request["repo"], request["pr"], request["sha"]
    c = github.client(repo)
    body = (
        f"{COMMENT_MARKER}\n### pr-triage: 👀 Needs human review\n"
        f"The automatic review of commit `{sha[:7]}` failed ({error}), so a person has to review this PR. "
        "The failure is logged and raises an alarm for the maintainers."
    )
    check_run_id = ctx.get("check_run_id") or gh.create_check_run(c, repo, sha, "Reviewing…", "")
    gh.complete_check_run(c, repo, check_run_id, "neutral", "Review failed: needs human review", body)
    pr = gh.get_pull(c, repo, number)
    if pr["state"] == "open" and pr["head"]["sha"] == sha:
        gh.set_triage_label(c, repo, number, gh.LABELS["needs_human"], [lbl["name"] for lbl in pr.get("labels", [])])
        gh.upsert_comment(c, repo, number, body)
    store.record_run(
        {
            "repo": repo,
            "pr": number,
            "sha": sha,
            "ticket": request.get("ticket"),
            "decision": "needs_human",
            "error": error,
            "reason": request["reason"],
            "requested_at": request.get("requested_at"),
            "started_at": execution["start_time"],
            "finished_at": gh.now_iso(),
            "execution": execution["name"],
        }
    )
    return {"published": True}
