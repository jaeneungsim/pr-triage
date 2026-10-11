"""Remove ticket previews that are older than a day or whose ticket has no open PR.

The demo repos also destroy a preview when its PR closes. This catches the ones that slip
through, so a forgotten preview cannot keep costing money.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import aws, store

log = logging.getLogger(__name__)
PREFIX = "inv-api-preview-inv-"
STATUSES = [
    "CREATE_COMPLETE",
    "CREATE_FAILED",
    "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED",
    "UPDATE_COMPLETE",
    "UPDATE_ROLLBACK_COMPLETE",
    "UPDATE_ROLLBACK_FAILED",
    "DELETE_FAILED",
    "IMPORT_COMPLETE",
]


def expired(updated: datetime, ticket: str, open_tickets: set[str], now: datetime, max_age: timedelta) -> bool:
    return now - updated > max_age or ticket not in open_tickets


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    max_age = timedelta(hours=int(os.environ.get("PREVIEW_MAX_AGE_HOURS", "24")))
    open_tickets = store.open_tickets()
    stacks = _stacks(now, max_age, open_tickets)
    prefixes = _web_prefixes(now, max_age, open_tickets)
    log.info("deleted stacks %s and web previews %s", stacks, prefixes)
    return {"stacks": stacks, "web_previews": prefixes}


def _stacks(now: datetime, max_age: timedelta, open_tickets: set[str]) -> list[str]:
    cfn = aws.client("cloudformation")
    deleted = []
    for page in cfn.get_paginator("list_stacks").paginate(StackStatusFilter=STATUSES):
        for summary in page["StackSummaries"]:
            name = summary["StackName"]
            if not name.startswith(PREFIX):
                continue
            ticket = name.removeprefix("inv-api-preview-").upper()
            updated = summary.get("LastUpdatedTime") or summary["CreationTime"]
            if expired(updated, ticket, open_tickets, now, max_age):
                cfn.delete_stack(StackName=name)
                deleted.append(name)
    return deleted


def _web_prefixes(now: datetime, max_age: timedelta, open_tickets: set[str]) -> list[str]:
    bucket = os.environ.get("WEB_PREVIEW_BUCKET")
    if not bucket:
        return []
    s3 = aws.client("s3")
    deleted = []
    listing = s3.list_objects_v2(Bucket=bucket, Delimiter="/")
    for common in listing.get("CommonPrefixes", []):
        prefix = common["Prefix"]
        stage = prefix.rstrip("/")
        if not stage.startswith("inv-"):
            continue
        objects = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
        if not objects:
            continue
        newest = max(o["LastModified"] for o in objects)
        if expired(newest, stage.upper(), open_tickets, now, max_age):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": o["Key"]} for o in objects[:1000]]})
            deleted.append(prefix)
    return deleted
