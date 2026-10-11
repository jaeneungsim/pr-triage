"""Step 2: the code-only policy gate."""

from __future__ import annotations

from typing import Any

from .. import evidence, params, policy


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    ctx = event["state"]["ctx"]
    doc = evidence.get_json(ctx["s3_key"])
    result = policy.evaluate(doc, policy.load(), params.load_config().allowed_authors)
    evidence.put_json(f"{ctx['prefix']}/policy.json", result)
    return result
