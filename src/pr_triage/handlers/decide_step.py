"""Step 7: the decision, by code."""

from __future__ import annotations

from typing import Any

from .. import decide, policy


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    state = event["state"]
    return decide.decide(state, policy.load(), state["ctx"]["mode"])
