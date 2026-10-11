"""Webhook receiver. Check the signature, drop duplicates, queue the event, answer at once.

GitHub waits 10 seconds for an answer, so nothing slow happens here. This function can only
read the webhook secret, record delivery ids and send to the queue.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Any

from .. import aws, params, store
from ..env import managed_repos, required
from ..tickets import extract_key

log = logging.getLogger(__name__)

REVIEW_ACTIONS = ("opened", "reopened", "synchronize", "ready_for_review")
FORK_LABEL = "ai-review-ok"
TICKET_PREFIX = "INV"


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def route(event: str, payload: dict[str, Any], delivery: str, repos: set[str]) -> dict[str, Any] | None:
    """Turn a webhook into a queue message, or None when there is nothing to do."""
    repo = (payload.get("repository") or {}).get("full_name")
    if repo not in repos:
        return None

    if event == "pull_request":
        action = payload.get("action")
        pr = payload["pull_request"]
        ticket = extract_key([pr["head"]["ref"], pr["title"]], TICKET_PREFIX)
        if action == "closed":
            return {
                "type": "closed",
                "repo": repo,
                "pr": pr["number"],
                "ticket": ticket,
                "merged": bool(pr.get("merged")),
                "delivery": delivery,
            }
        changes = set((payload.get("changes") or {}).keys())
        if action in REVIEW_ACTIONS:
            reason = action
        elif action == "edited" and changes & {"body", "title"}:
            reason = "edited"
        elif action == "labeled" and (payload.get("label") or {}).get("name") == FORK_LABEL:
            reason = "ai-review-ok"
        else:
            return None
        if pr.get("draft") and reason != "ready_for_review":
            return None
        return {
            "type": "review",
            "repo": repo,
            "pr": pr["number"],
            "sha": pr["head"]["sha"],
            "reason": reason,
            "ticket": ticket,
            "title": pr["title"],
            "url": pr["html_url"],
            "head_ref": pr["head"]["ref"],
            "updated_at": pr["updated_at"],
            "delivery": delivery,
        }

    if event == "workflow_run" and payload.get("action") == "completed":
        run = payload["workflow_run"]
        return {
            "type": "workflow",
            "repo": repo,
            "sha": run["head_sha"],
            "workflow": run["name"],
            "conclusion": run.get("conclusion"),
            "run_id": run["id"],
            "event": run.get("event"),
            "head_branch": run.get("head_branch"),
            "delivery": delivery,
        }
    return None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw = event.get("body") or ""
    body = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()

    if not verify_signature(params.secret("webhook-secret"), body, headers.get("x-hub-signature-256")):
        log.warning("rejected a webhook with a missing or wrong signature")
        return _response(401, {"error": "invalid signature"})
    delivery, name = headers.get("x-github-delivery"), headers.get("x-github-event")
    if not delivery or not name:
        return _response(400, {"error": "missing GitHub headers"})
    try:
        payload = json.loads(body)
    except ValueError:
        return _response(400, {"error": "body is not JSON"})

    message = route(name, payload, delivery, managed_repos())
    if message is None:
        return _response(202, {"ignored": name})
    if not store.claim_delivery(delivery):
        return _response(200, {"duplicate": delivery})
    try:
        aws.client("sqs").send_message(QueueUrl=required("QUEUE_URL"), MessageBody=json.dumps(message))
    except Exception:
        store.release_delivery(delivery)  # let a redelivery through
        raise
    log.info("queued %s for %s", message["type"], message["repo"])
    return _response(202, {"queued": message["type"]})


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}
