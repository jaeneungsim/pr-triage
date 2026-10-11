#!/usr/bin/env python3
"""Measure one demo cycle: time and AI cost per reviewed commit, from real execution history.

    .venv/bin/python scripts/cycle_report.py --since 2026-10-12T00:00:00Z --profile jae-dev

Prints a Markdown table. Times come from the Step Functions history of each execution;
cost and tokens come from the review record the app wrote.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr


def seconds(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds(), 1)


def state_durations(sfn, execution_arn: str) -> dict[str, float]:
    """Seconds spent in each state, from StateEntered to StateExited."""
    entered: dict[str, datetime] = {}
    spent: dict[str, float] = {}
    paginator = sfn.get_paginator("get_execution_history")
    for page in paginator.paginate(executionArn=execution_arn, includeExecutionData=False):
        for event in page["events"]:
            if "stateEnteredEventDetails" in event:
                entered[event["stateEnteredEventDetails"]["name"]] = event["timestamp"]
            elif "stateExitedEventDetails" in event:
                name = event["stateExitedEventDetails"]["name"]
                if name in entered:
                    spent[name] = spent.get(name, 0) + seconds(entered.pop(name), event["timestamp"])
    return spent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", required=True, help="ISO time, e.g. 2026-10-12T00:00:00Z")
    parser.add_argument("--profile")
    parser.add_argument("--region", default="ap-southeast-2")
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    table = session.resource("dynamodb").Table("pr-triage-state")
    sfn = session.client("stepfunctions")
    machine = next(m for m in sfn.list_state_machines()["stateMachines"] if m["name"] == "pr-triage-review")
    since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))

    runs, kwargs = [], {"FilterExpression": Attr("sk").begins_with("RUN#") & Attr("finished_at").gte(since.isoformat())}
    while True:
        page = table.scan(**kwargs)
        runs += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    print("| PR | Decision | Webhook to result | Waiting for CI and previews | AI review | Tokens in / out | AI cost |")
    print("|---|---|---|---|---|---|---|")
    total = Decimal(0)
    for run in sorted(runs, key=lambda r: r["finished_at"]):
        arn = f"{machine['stateMachineArn'].replace(':stateMachine:', ':execution:')}:{run['execution']}"
        states = state_durations(sfn, arn)
        requested = datetime.fromisoformat(str(run.get("requested_at") or run["started_at"]).replace("Z", "+00:00"))
        finished = datetime.fromisoformat(str(run["finished_at"]).replace("Z", "+00:00"))
        cost = Decimal(str(run.get("cost_usd") or 0))
        total += cost
        tokens = f"{run['input_tokens']:,} / {run['output_tokens']:,}" if run.get("input_tokens") else "-"
        ai = f"{states['AIReview']:.1f} s" if "AIReview" in states else "not called"
        print(
            f"| {run['repo'].split('/')[-1]}#{run['pr']} | {run['decision']} | {seconds(requested, finished):.0f} s | "
            f"{states.get('WaitForCI', 0):.0f} s | {ai} | {tokens} | ${cost:.4f} |"
        )
    print(f"\n{len(runs)} reviews, AI cost ${total:.4f} in total. Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC.")


if __name__ == "__main__":
    main()
