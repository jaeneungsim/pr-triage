"""Step 3: wait for CI with a task token.

Store the token first, then look at CI. If CI already finished, resume right away. Checking
first and storing second would miss a CI run that ends between the two calls.
"""

from __future__ import annotations

from typing import Any

from .. import ci, github, store


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    ctx, token = event["state"]["ctx"], event["token"]
    repo, sha = ctx["repo"], ctx["sha"]
    required, not_before = ctx["required_workflows"], ctx.get("not_before") or {}
    store.put_wait(repo, sha, event["execution"]["name"], token, required, not_before)
    result = ci.status(github.client(repo), repo, sha, required, not_before)
    if result["complete"]:
        ci.send_success(token, result)
        store.delete_wait(repo, sha, event["execution"]["name"])
    return {"waiting": not result["complete"]}
