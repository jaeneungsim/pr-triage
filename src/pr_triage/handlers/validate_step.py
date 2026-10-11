"""Step 6: check the AI answer against the code."""

from __future__ import annotations

from typing import Any

from .. import evidence, validate


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    state = event["state"]
    ctx = state["ctx"]
    doc = evidence.get_json(ctx["s3_key"])
    result = validate.validate_review(
        state["review"]["result"],
        changed_files={f["filename"] for f in doc["files"] if f.get("status") != "removed"},
        line_counts={path: len(text.splitlines()) for path, text in doc["contents"].items()},
        expected_ids={item["id"] for item in doc["expected"]["items"]},
        requirement_ids=set((doc.get("ticket") or {}).get("requirements", {})),
        routes=validate.openapi_routes(doc.get("openapi")),
    )
    evidence.put_json(f"{ctx['prefix']}/validation.json", result)
    return result
