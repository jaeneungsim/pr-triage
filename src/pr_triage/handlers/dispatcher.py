"""Queue consumer. Starts one review per commit, wakes reviews waiting for CI, tidies up on close."""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

from .. import aws, ci, gh, github, store
from ..env import required
from ..github import GitHubError

log = logging.getLogger(__name__)
SIBLING_REASONS = ("opened", "reopened", "synchronize")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    failures = []
    for record in event.get("Records", []):
        try:
            handle(json.loads(record["body"]))
        except Exception:
            log.exception("message %s failed", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def handle(message: dict[str, Any]) -> None:
    kind = message["type"]
    if kind == "review":
        start_review(message)
    elif kind == "workflow":
        on_workflow(message)
    elif kind == "closed":
        on_closed(message)


def execution_name(repo: str, number: int, sha: str) -> str:
    return f"{repo.rsplit('/', 1)[-1]}-{number}-{sha[:7]}-{int(time.time())}-{secrets.token_hex(2)}"[:80]


def start_review(message: dict[str, Any]) -> None:
    repo, number, sha = message["repo"], message["pr"], message["sha"]
    if message["reason"] == "ai-review-ok":
        store.set_ai_ok(repo, number, sha)

    head = store.get_head(repo, number)
    if head and head.get("sha") != sha:
        _supersede(repo, head)

    started = aws.client("stepfunctions").start_execution(
        stateMachineArn=required("STATE_MACHINE_ARN"),
        name=execution_name(repo, number, sha),
        input=json.dumps({"request": {**message, "requested_at": gh.now_iso()}}),
    )
    store.put_head(repo, number, sha, started["executionArn"])

    ticket = message.get("ticket")
    if ticket:
        store.upsert_ticket_pr(
            ticket, repo, number, sha=sha, title=message["title"], url=message["url"], branch=message["head_ref"]
        )
        if message["reason"] in SIBLING_REASONS:
            _rerun_siblings(message)


def _supersede(repo: str, head: dict[str, Any]) -> None:
    """A newer commit arrived: stop the old review if it is still running."""
    arn = head.get("execution_arn")
    if not arn:
        return
    sfn = aws.client("stepfunctions")
    if sfn.describe_execution(executionArn=arn)["status"] != "RUNNING":
        return
    sfn.stop_execution(executionArn=arn, cause="A newer commit was pushed.")
    if head.get("check_run_id"):
        try:
            gh.complete_check_run(
                github.client(repo),
                repo,
                head["check_run_id"],
                "cancelled",
                "Superseded",
                "A newer commit was pushed; it gets its own review.",
            )
        except GitHubError as err:
            log.warning("could not cancel check run %s: %s", head["check_run_id"], err)


def _rerun_siblings(message: dict[str, Any]) -> None:
    """Another PR of the same ticket changed, so the others are reviewed again."""
    queue = required("QUEUE_URL")
    for sibling in store.ticket_prs(message["ticket"]):
        if sibling.get("state") != "open" or (sibling["repo"], sibling["number"]) == (message["repo"], message["pr"]):
            continue
        aws.client("sqs").send_message(
            QueueUrl=queue,
            MessageBody=json.dumps(
                {
                    "type": "review",
                    "repo": sibling["repo"],
                    "pr": sibling["number"],
                    "sha": sibling["sha"],
                    "reason": "related-pr-changed",
                    "ticket": message["ticket"],
                    "title": sibling["title"],
                    "url": sibling["url"],
                    "head_ref": sibling["branch"],
                    "updated_at": gh.now_iso(),
                    "delivery": f"related-{message['delivery']}",
                }
            ),
        )


def on_workflow(message: dict[str, Any]) -> None:
    repo, sha = message["repo"], message["sha"]
    if message["workflow"] == "deploy-dev":
        if message.get("event") == "push" and message.get("conclusion") == "success":
            aws.client("lambda").invoke(
                FunctionName=required("POSTMERGE_FUNCTION"),
                InvocationType="Event",
                Payload=json.dumps({"repo": repo, "sha": sha}).encode(),
            )
        return
    if store.waits_for(repo, sha):
        resumed = ci.resolve_waits(github.client(repo), repo, sha)
        log.info("workflow %s on %s@%s resumed %d review(s)", message["workflow"], repo, sha[:7], resumed)


def on_closed(message: dict[str, Any]) -> None:
    if message.get("ticket"):
        store.close_ticket_pr(message["ticket"], message["repo"], message["pr"], message["merged"])
    store.delete_head(message["repo"], message["pr"])
