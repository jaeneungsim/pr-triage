"""The decision. Code, not the model, turns the evidence into one of three outcomes.

The model can push a PR toward "changes requested" or "needs human review" on its own, but it
can never reach "approvable" alone: every earlier gate has to pass as well.
"""

from __future__ import annotations

from typing import Any

from .policy import BLOCKING, CHANGES, HUMAN

APPROVABLE = "approvable"
SECURITY_KINDS = ("sensitive", "injection")


def _hit(kind: str, outcome: str, message: str, source: str) -> dict[str, str]:
    return {"kind": kind, "outcome": outcome, "message": message, "source": source}


def final(hits: list[dict[str, Any]]) -> str:
    """Security beats everything; otherwise let the author fix things before a person looks."""
    blocking = [h for h in hits if h["outcome"] in BLOCKING]
    if any(h["kind"] in SECURITY_KINDS for h in blocking):
        return HUMAN
    if any(h["outcome"] == CHANGES for h in blocking):
        return CHANGES
    if blocking:
        return HUMAN
    return APPROVABLE


def decide(state: dict[str, Any], policy: dict[str, Any], mode: str) -> dict[str, Any]:
    hits: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    for part in ("policy", "checks"):
        for hit in (state.get(part) or {}).get("hits", []):
            (hits if hit["outcome"] in BLOCKING else notes).append(hit)

    review, validation = state.get("review"), state.get("validation")
    result = None
    if review is not None:
        if review.get("status") != "ok":
            hits.append(
                _hit(review.get("kind") or "ai_unavailable", HUMAN, f"No AI review: {review.get('reason')}.", "ai")
            )
        elif validation is not None and validation.get("critical"):
            problems = "; ".join(validation.get("problems", [])[:3])
            hits.append(
                _hit("validation", HUMAN, f"The AI answer did not hold up against the code ({problems}).", "validation")
            )
        else:
            result = (validation or {}).get("review") or review["result"]
            hits += _ai_hits(result, policy)
    elif not hits:
        hits.append(_hit("ai_missing", HUMAN, "There is no AI review result.", "decide"))

    decision = final(hits)
    auto, why_not = False, None
    if decision == APPROVABLE and mode == "auto-approve" and result is not None:
        why_not = auto_blocker(state, policy, result)
        auto = why_not is None
    return {"decision": decision, "hits": hits, "notes": notes, "auto_approve": auto, "auto_blocker": why_not}


def _ai_hits(result: dict[str, Any], policy: dict[str, Any]) -> list[dict[str, str]]:
    findings = result.get("findings", [])
    if any(f.get("category") == "prompt_injection" for f in findings):
        return [_hit("injection", HUMAN, "AI review: the PR contains text aimed at the reviewer.", "ai")]
    suggestion, confidence = result["suggestion"], float(result["confidence"])
    if suggestion == "changes_requested":
        titles = "; ".join(f["title"] for f in findings[:3]) or result.get("summary", "")
        return [_hit("ai", CHANGES, f"AI review: {titles}", "ai")]
    if suggestion == "needs_human":
        return [_hit("ai", HUMAN, f"AI review: {result.get('summary', 'it could not judge this change')}", "ai")]
    if any(f.get("severity") == "high" for f in findings):
        return [
            _hit("ai_inconsistent", HUMAN, "AI review suggested approval but reported a high-severity finding.", "ai")
        ]
    minimum = policy["ai"]["min_confidence"]
    if confidence < minimum:
        return [
            _hit(
                "ai_low_confidence",
                HUMAN,
                f"AI review suggested approval with confidence {confidence:.2f}, below {minimum}.",
                "ai",
            )
        ]
    return []


def auto_blocker(state: dict[str, Any], policy: dict[str, Any], result: dict[str, Any]) -> str | None:
    """Why an approvable PR still waits for a person in auto-approve mode, or None if it does not.

    Code approves only what it can vouch for: docs and tests on their own, and code changes
    whose expected behavior actually ran and passed. The AI's approval is necessary, never enough.
    """
    ctx, gate = state["ctx"], state["policy"]
    rules = policy["auto_approve"]
    if ctx["fork"]:
        return "it comes from a fork"
    if ctx["base_ref"] not in rules["target_branches"]:
        return f"it targets {ctx['base_ref']}; only {', '.join(rules['target_branches'])} merges on its own"
    if gate["category"] not in rules["categories"]:
        return f"{gate['category']} changes are not auto-approved"
    if float(result["confidence"]) < rules["min_confidence"]:
        return f"the AI confidence is below {rules['min_confidence']}"
    if gate["category"] != "code":
        return None
    repo_name = ctx["repo"].rsplit("/", 1)[-1]
    if gate.get("ticket_repos") != [repo_name]:
        return "the ticket spans several repos, so a person merges the parts together"
    if "ui" in gate.get("expected_types", []):
        return "the expected behavior has UI steps that a person has to look at"
    return _evidence_gap(state.get("checks") or {}, ctx["level"])


def _evidence_gap(checks: dict[str, Any], level: str) -> str | None:
    """None if the expected behavior ran and passed: in CI from v2, and on the AWS preview at v3."""
    if level == "v1":
        return "v1 does not run the code"
    if (checks.get("test_relevance") or {}).get("verdict") != "ok":
        return "the new tests were not shown to fail on the old code"
    ci = checks.get("behavior_ci") or {}
    if not (ci.get("passed") and any(i.get("ok") for i in ci.get("items", []))):
        return "the expected behavior did not run in CI"
    if level == "v3":
        items = (checks.get("behavior_preview") or {}).get("items", [])
        if not any(i.get("ok") for i in items) or any(i.get("ok") is False for i in items):
            return "the expected behavior did not run on the AWS preview"
    return None
