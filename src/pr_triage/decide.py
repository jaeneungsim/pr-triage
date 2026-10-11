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
    auto = decision == APPROVABLE and mode == "auto-approve" and result is not None and _auto_ok(state, policy, result)
    return {"decision": decision, "hits": hits, "notes": notes, "auto_approve": auto}


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


def _auto_ok(state: dict[str, Any], policy: dict[str, Any], result: dict[str, Any]) -> bool:
    ctx, gate = state["ctx"], state["policy"]
    rules = policy["auto_approve"]
    return (
        gate["category"] in rules["categories"]
        and float(result["confidence"]) >= rules["min_confidence"]
        and not ctx["fork"]
        and ctx["base_ref"] in rules["target_branches"]
    )
