"""DynamoDB access. One table; the key prefix says what an item is.

| pk                       | sk                    | what                                   |
|--------------------------|-----------------------|----------------------------------------|
| DELIVERY#<id>            | -                     | webhook delivery already handled (TTL) |
| PR#<repo>#<n>            | HEAD                  | latest commit and running execution    |
| PR#<repo>#<n>            | AIOK                  | commit a maintainer allowed for a fork |
| PR#<repo>#<n>            | RUN#<time>#<sha7>     | a finished review (gsi1: DAY#<date>)   |
| TICKET#<key>             | PR#<repo>#<n>         | PRs that belong to a ticket            |
| WAIT#<repo>#<sha>        | EXEC#<name>           | a review waiting for CI (TTL)          |
| USAGE#DAY#<date>         | -                     | AI reviews started that day            |
| USAGE#MONTH#<yyyy-mm>    | -                     | AI cost, in micro-dollars              |
| META#<name>              | -                     | small values, such as the digest issue |
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key

from .aws import error_code, table


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _plain(value: Any) -> Any:
    """DynamoDB returns Decimal; turn it back into int or float."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _decimals(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _decimals(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_decimals(v) for v in value]
    return value


def _conditional(fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
        return True
    except Exception as err:
        if error_code(err) == "ConditionalCheckFailedException":
            return False
        raise


# --- webhook deliveries -----------------------------------------------------------------


def claim_delivery(delivery_id: str, ttl_days: int = 7) -> bool:
    return _conditional(
        table().put_item,
        Item={"pk": f"DELIVERY#{delivery_id}", "sk": "-", "ttl": int(time.time()) + ttl_days * 86400},
        ConditionExpression="attribute_not_exists(pk)",
    )


def release_delivery(delivery_id: str) -> None:
    table().delete_item(Key={"pk": f"DELIVERY#{delivery_id}", "sk": "-"})


# --- pull requests ----------------------------------------------------------------------


def _pr(repo: str, number: int) -> str:
    return f"PR#{repo}#{number}"


def get_head(repo: str, number: int) -> dict[str, Any] | None:
    item = table().get_item(Key={"pk": _pr(repo, number), "sk": "HEAD"}).get("Item")
    return _plain(item) if item else None


def put_head(repo: str, number: int, sha: str, execution_arn: str) -> None:
    table().put_item(
        Item={
            "pk": _pr(repo, number),
            "sk": "HEAD",
            "sha": sha,
            "execution_arn": execution_arn,
            "updated_at": _now(),
        }
    )


def set_head_check_run(repo: str, number: int, sha: str, check_run_id: int) -> None:
    _conditional(
        table().update_item,
        Key={"pk": _pr(repo, number), "sk": "HEAD"},
        UpdateExpression="SET check_run_id = :c",
        ConditionExpression="sha = :s",
        ExpressionAttributeValues={":c": check_run_id, ":s": sha},
    )


def delete_head(repo: str, number: int) -> None:
    table().delete_item(Key={"pk": _pr(repo, number), "sk": "HEAD"})


def set_ai_ok(repo: str, number: int, sha: str) -> None:
    table().put_item(Item={"pk": _pr(repo, number), "sk": "AIOK", "sha": sha, "updated_at": _now()})


def get_ai_ok(repo: str, number: int) -> str | None:
    item = table().get_item(Key={"pk": _pr(repo, number), "sk": "AIOK"}).get("Item")
    return item["sha"] if item else None


# --- tickets ----------------------------------------------------------------------------


def upsert_ticket_pr(ticket: str, repo: str, number: int, *, sha: str, title: str, url: str, branch: str) -> None:
    table().update_item(
        Key={"pk": f"TICKET#{ticket}", "sk": _pr(repo, number)},
        UpdateExpression=(
            "SET repo = :r, #n = :n, sha = :s, title = :t, #u = :u, branch = :b, #st = :open, updated_at = :now"
        ),
        ExpressionAttributeNames={"#n": "number", "#u": "url", "#st": "state"},
        ExpressionAttributeValues={
            ":r": repo,
            ":n": number,
            ":s": sha,
            ":t": title,
            ":u": url,
            ":b": branch,
            ":open": "open",
            ":now": _now(),
        },
    )


def ticket_prs(ticket: str) -> list[dict[str, Any]]:
    resp = table().query(KeyConditionExpression=Key("pk").eq(f"TICKET#{ticket}"))
    return [_plain(item) for item in resp.get("Items", [])]


def close_ticket_pr(ticket: str, repo: str, number: int, merged: bool) -> None:
    _conditional(
        table().update_item,
        Key={"pk": f"TICKET#{ticket}", "sk": _pr(repo, number)},
        UpdateExpression="SET #st = :closed, merged = :m, updated_at = :now",
        ConditionExpression="attribute_exists(pk)",
        ExpressionAttributeNames={"#st": "state"},
        ExpressionAttributeValues={":closed": "closed", ":m": merged, ":now": _now()},
    )


def set_ticket_decision(ticket: str, repo: str, number: int, sha: str, decision: str, summary: str) -> None:
    _conditional(
        table().update_item,
        Key={"pk": f"TICKET#{ticket}", "sk": _pr(repo, number)},
        UpdateExpression="SET decision = :d, decision_sha = :s, summary = :m, updated_at = :now",
        ConditionExpression="attribute_exists(pk)",
        ExpressionAttributeValues={":d": decision, ":s": sha, ":m": summary[:500], ":now": _now()},
    )


def open_tickets() -> set[str]:
    """Tickets with at least one open PR. A scan is fine at demo scale."""
    tickets: set[str] = set()
    kwargs: dict[str, Any] = {
        "FilterExpression": "begins_with(pk, :p) AND #st = :open",
        "ExpressionAttributeNames": {"#st": "state"},
        "ExpressionAttributeValues": {":p": "TICKET#", ":open": "open"},
        "ProjectionExpression": "pk",
    }
    while True:
        resp = table().scan(**kwargs)
        tickets.update(item["pk"].removeprefix("TICKET#") for item in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return tickets
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


# --- CI waits ---------------------------------------------------------------------------


def put_wait(
    repo: str,
    sha: str,
    execution: str,
    token: str,
    required: list[str],
    not_before: dict[str, str],
    ttl_seconds: int = 7200,
) -> None:
    table().put_item(
        Item={
            "pk": f"WAIT#{repo}#{sha}",
            "sk": f"EXEC#{execution}",
            "token": token,
            "required": required,
            "not_before": not_before,
            "repo": repo,
            "sha": sha,
            "ttl": int(time.time()) + ttl_seconds,
        }
    )


def waits_for(repo: str, sha: str) -> list[dict[str, Any]]:
    resp = table().query(KeyConditionExpression=Key("pk").eq(f"WAIT#{repo}#{sha}"))
    return [_plain(item) for item in resp.get("Items", [])]


def delete_wait(repo: str, sha: str, execution: str) -> None:
    table().delete_item(Key={"pk": f"WAIT#{repo}#{sha}", "sk": f"EXEC#{execution}"})


# --- runs -------------------------------------------------------------------------------


def record_run(run: dict[str, Any]) -> None:
    finished = run["finished_at"]
    table().put_item(
        Item=_decimals(
            {
                **run,
                "pk": _pr(run["repo"], run["pr"]),
                "sk": f"RUN#{run['started_at']}#{run['sha'][:7]}",
                "gsi1pk": f"DAY#{finished[:10]}",
                "gsi1sk": finished,
            }
        )
    )


def runs_between(start: datetime, end: datetime) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    day = start.date()
    while day <= end.date():
        resp = table().query(
            IndexName="gsi1",
            KeyConditionExpression=Key("gsi1pk").eq(f"DAY#{day.isoformat()}")
            & Key("gsi1sk").between(start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")),
        )
        runs += [_plain(item) for item in resp.get("Items", [])]
        day += timedelta(days=1)
    return runs


# --- AI usage ---------------------------------------------------------------------------


def reserve_daily(day: str, limit: int) -> bool:
    return _conditional(
        table().update_item,
        Key={"pk": f"USAGE#DAY#{day}", "sk": "-"},
        UpdateExpression="ADD reviews :one SET #ttl = :ttl",
        ConditionExpression="attribute_not_exists(reviews) OR reviews < :limit",
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={":one": 1, ":limit": limit, ":ttl": int(time.time()) + 40 * 86400},
    )


def release_daily(day: str) -> None:
    table().update_item(
        Key={"pk": f"USAGE#DAY#{day}", "sk": "-"},
        UpdateExpression="ADD reviews :minus",
        ExpressionAttributeValues={":minus": -1},
    )


def reserve_monthly(month: str, amount_micro: int, threshold_micro: int) -> bool:
    """Add the worst-case cost of one call, unless that could cross the monthly limit.

    DynamoDB conditions cannot add numbers, so the caller passes limit minus amount.
    """
    return _conditional(
        table().update_item,
        Key={"pk": f"USAGE#MONTH#{month}", "sk": "-"},
        UpdateExpression="ADD committed_micro :amount",
        ConditionExpression="attribute_not_exists(committed_micro) OR committed_micro <= :threshold",
        ExpressionAttributeValues={":amount": amount_micro, ":threshold": threshold_micro},
    )


def settle_monthly(month: str, reserved_micro: int, actual_micro: int, *, called: bool) -> None:
    table().update_item(
        Key={"pk": f"USAGE#MONTH#{month}", "sk": "-"},
        UpdateExpression="ADD committed_micro :delta, spent_micro :spent, calls :calls",
        ExpressionAttributeValues={
            ":delta": actual_micro - reserved_micro,
            ":spent": actual_micro,
            ":calls": 1 if called else 0,
        },
    )


def month_usage(month: str) -> dict[str, Any]:
    item = table().get_item(Key={"pk": f"USAGE#MONTH#{month}", "sk": "-"}).get("Item")
    return _plain(item) if item else {}


# --- small values -----------------------------------------------------------------------


def get_meta(name: str) -> Any:
    item = table().get_item(Key={"pk": f"META#{name}", "sk": "-"}).get("Item")
    return _plain(item["value"]) if item else None


def put_meta(name: str, value: Any) -> None:
    table().put_item(Item={"pk": f"META#{name}", "sk": "-", "value": _decimals(value)})
