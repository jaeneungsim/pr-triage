#!/usr/bin/env python3
"""Review a PR's current commit again without touching the PR (for example after a model outage).

It starts a new execution with the input of the PR's latest one. Webhook redeliveries do not work
for this, because the receiver ignores delivery ids it has already handled.

    .venv/bin/python scripts/rerun_review.py --repo demo-inventory-api --pr 1 --profile jae-dev
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime

import boto3

STATE_MACHINE = "pr-triage-review"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, help="repo name without the owner, e.g. demo-inventory-api")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--profile")
    parser.add_argument("--region", default="ap-southeast-2")
    args = parser.parse_args()
    sfn = boto3.Session(profile_name=args.profile, region_name=args.region).client("stepfunctions")
    machine = next(m for m in sfn.list_state_machines()["stateMachines"] if m["name"] == STATE_MACHINE)
    prefix = f"{args.repo}-{args.pr}-"
    runs = [
        e
        for page in sfn.get_paginator("list_executions").paginate(stateMachineArn=machine["stateMachineArn"])
        for e in page["executions"]
        if e["name"].startswith(prefix)
    ]
    if not runs:
        raise SystemExit(f"no earlier review of {args.repo}#{args.pr}")
    latest = max(runs, key=lambda e: e["startDate"])
    state = json.loads(sfn.describe_execution(executionArn=latest["executionArn"])["input"])
    state["request"]["reason"] = "operator-rerun"
    state["request"]["requested_at"] = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    sha = state["request"]["sha"]
    name = f"{args.repo}-{args.pr}-{sha[:7]}-rerun-{int(time.time())}"
    sfn.start_execution(stateMachineArn=machine["stateMachineArn"], name=name, input=json.dumps(state))
    print(f"Started {name} for commit {sha[:7]}. If the PR has a newer commit, the run skips itself.")


if __name__ == "__main__":
    main()
