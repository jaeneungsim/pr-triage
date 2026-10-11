"""Step 8: publish the result on the commit, and on the PR only if the commit is still its head.

The check run belongs to the reviewed commit. Labels and the comment belong to the whole PR, so
a run that finishes after a newer commit arrived must not overwrite them.
"""

from __future__ import annotations

from typing import Any

from .. import evidence, gh, github, render, store


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    state, execution = event["state"], event["execution"]
    ctx, decision = state["ctx"], state["decision"]
    repo, number, sha = ctx["repo"], ctx["pr"], ctx["sha"]
    outcome = decision["decision"]
    c = github.client(repo)
    doc = evidence.get_json(ctx["s3_key"])

    pr = gh.get_pull(c, repo, number)
    current = pr["state"] == "open" and pr["head"]["sha"] == sha
    approved = False
    if current and decision["auto_approve"]:
        gh.approve(c, repo, number, sha, "Approved by pr-triage: docs or tests only, and every check passed.")
        approved = True

    body = render.summary(state, doc, approved=approved)
    gh.complete_check_run(c, repo, ctx["check_run_id"], render.CONCLUSIONS[outcome], render.title(decision), body)
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
            "auto_approved": approved,
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
    return {"published": True, "current": current, "approved": approved}
