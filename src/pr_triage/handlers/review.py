"""Step 5: the AI review. Runs only when every earlier gate passed and the limits allow it."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from .. import bedrock, caps, evidence, params, prompt, store

log = logging.getLogger(__name__)


def unavailable(reason: str, kind: str = "ai_unavailable") -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "kind": kind}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    state = event["state"]
    ctx, gate = state["ctx"], state["policy"]
    cfg = params.load_config()
    if not gate["ai_allowed"]:
        return unavailable(gate["ai_blocked_reason"])
    if not cfg.enabled:
        return unavailable("AI reviews are switched off")

    doc = evidence.get_json(ctx["s3_key"])
    request = prompt.build(doc, state.get("checks") or {}, cfg.limits)
    if request is None or request.estimated_tokens > cfg.limits.max_input_tokens:
        return unavailable("the change is too large for one AI review", kind="size")

    reservation, reason = caps.reserve(store, cfg, datetime.now(UTC))
    if reservation is None:
        return unavailable(reason or "a usage limit is reached", kind="limit")

    try:
        call = bedrock.call(
            cfg.model.id, request.system, request.user, prompt.tool_schema(), cfg.limits.max_output_tokens
        )
    except bedrock.ModelOutputError as err:
        caps.settle(store, reservation, caps.cost_micro(cfg.model, err.input_tokens, err.output_tokens))
        log.warning("model answered without the review tool: %s", err)
        return unavailable("the model did not return a structured review")
    except Exception as err:
        caps.release(store, reservation)
        log.exception("model call failed")
        return unavailable(f"the model call failed ({type(err).__name__})")

    cost = caps.cost_micro(cfg.model, call.input_tokens, call.output_tokens)
    caps.settle(store, reservation, cost)
    evidence.put_json(
        f"{ctx['prefix']}/review.json",
        {
            "model_id": cfg.model.id,
            "estimated_input_tokens": request.estimated_tokens,
            "omitted": list(request.omitted),
            "usage": {"input": call.input_tokens, "output": call.output_tokens},
            "latency_ms": call.latency_ms,
            "stop_reason": call.stop_reason,
            "review": call.review,
        },
    )
    log.info(
        "AI review of %s#%s: %s in, %s out, $%.4f",
        ctx["repo"],
        ctx["pr"],
        call.input_tokens,
        call.output_tokens,
        cost / 1_000_000,
    )
    return {
        "status": "ok",
        "result": call.review,
        "usage": {"input_tokens": call.input_tokens, "output_tokens": call.output_tokens},
        "cost_usd": cost / 1_000_000,
        "latency_ms": call.latency_ms,
        "model_id": cfg.model.id,
        "omitted": list(request.omitted),
    }
