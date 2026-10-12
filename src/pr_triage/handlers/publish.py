"""Step 8: publish the result on the commit, and on the PR only if the commit is still its head.

The check run belongs to the reviewed commit. Labels and the comment belong to the whole PR, so
a run that finishes after a newer commit arrived must not overwrite them.

In auto-approve mode an eligible PR is approved and merged here. The check run is completed
first, because branch protection needs it green, and the merge names the reviewed commit, so
GitHub refuses it if a newer commit arrived in between.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import evidence, gh, github, policy, render, store
from ..github import GitHubError

log = logging.getLogger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    state, execution = event["state"], event["execution"]
    ctx, decision = state["ctx"], state["decision"]
    repo, number, sha = ctx["repo"], ctx["pr"], ctx["sha"]
    outcome = decision["decision"]
    c = github.client(repo)
    doc = evidence.get_json(ctx["s3_key"])

    pr = gh.get_pull(c, repo, number)
    current = pr["state"] == "open" and pr["head"]["sha"] == sha
    approved = current and decision["auto_approve"]

    body = render.summary(state, doc, approved=approved)
    gh.complete_check_run(c, repo, ctx["check_run_id"], render.CONCLUSIONS[outcome], render.title(decision), body)
    merge = None
    if approved:
        merge = _approve_and_merge(c, repo, pr, sha, render.approval(state))
        body = render.summary(state, doc, approved=True, merge=merge)
    if current:
        gh.set_triage_label(
            c, repo, number, gh.LABELS[render.LABEL_KEYS[outcome]], [lbl["name"] for lbl in pr.get("labels", [])]
        )
        gh.upsert_comment(c, repo, number, body)

    review = state.get("review") or {}
    store.record_run(
        {
            "repo": repo,
            "pr": number,
            "sha": sha,
            "ticket": ctx.get("ticket_key"),
            "decision": outcome,
            "auto_approved": approved and (merge or {}).get("approved", True),
            "merged": bool(merge and merge["merged"]),
            "merge_error": (merge or {}).get("error"),
            "level": ctx["level"],
            "mode": ctx["mode"],
            "reason": state["request"]["reason"],
            "author": ctx["author"],
            "requested_at": state["request"].get("requested_at"),
            "started_at": execution["start_time"],
            "finished_at": gh.now_iso(),
            "execution": execution["name"],
            "hit_kinds": sorted({h["kind"] for h in decision["hits"]}),
            "ai_status": review.get("status"),
            "cost_usd": review.get("cost_usd"),
            "input_tokens": (review.get("usage") or {}).get("input_tokens"),
            "output_tokens": (review.get("usage") or {}).get("output_tokens"),
            "ai_latency_ms": review.get("latency_ms"),
            "model_id": review.get("model_id"),
            "current": current,
        }
    )
    if ctx.get("ticket_key"):
        summary = ((state.get("validation") or {}).get("review") or review.get("result") or {}).get("summary", "")
        store.set_ticket_decision(ctx["ticket_key"], repo, number, sha, outcome, summary)
    return {"published": True, "current": current, "approved": approved, "merged": bool(merge and merge["merged"])}


def _approve_and_merge(
    c: github.GitHubClient, repo: str, pr: dict[str, Any], sha: str, text: str
) -> dict[str, Any] | None:
    """Approve, then merge. A refusal is reported on the PR, and a person can still finish."""
    try:
        gh.approve(c, repo, pr["number"], sha, text)
    except GitHubError as err:
        log.warning("approval of %s#%s refused: %s", repo, pr["number"], err)
        return {"approved": False, "merged": False, "error": str(err)[:300]}
    method = policy.load()["auto_approve"].get("merge_method")
    if not method:
        return None
    try:
        data = gh.merge(c, repo, pr, sha, method) or {}
    except GitHubError as err:
        log.warning("auto-merge of %s#%s refused: %s", repo, pr["number"], err)
        return {"approved": True, "merged": False, "error": str(err)[:300]}
    return {"approved": True, "merged": bool(data.get("merged")), "sha": data.get("sha"), "error": None}
