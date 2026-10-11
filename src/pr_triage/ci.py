"""Gate 3: have the required workflows for a commit finished, and how did they end?"""

from __future__ import annotations

from typing import Any

from . import aws, gh, store
from .github import GitHubClient

PASSING = ("success", "skipped", "neutral")


def latest_runs(
    runs: list[dict[str, Any]], required: list[str], not_before: dict[str, str] | None = None
) -> dict[str, dict[str, Any] | None]:
    """The newest pull_request run of each required workflow.

    `not_before` drops runs created before a time, per workflow. It is used after the PR
    description is edited, so an old run of a workflow that reads the description is ignored.
    """
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        name = run.get("name")
        if name not in required or run.get("event") != "pull_request":
            continue
        limit = (not_before or {}).get(name)
        if limit and run.get("created_at", "") < limit:
            continue
        current = latest.get(name)
        if current is None or (run["run_number"], run.get("run_attempt", 1)) > (
            current["run_number"],
            current.get("run_attempt", 1),
        ):
            latest[name] = run
    return {name: latest.get(name) for name in required}


def summarize(latest: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    workflows = {
        name: None
        if run is None
        else {
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "run_id": run.get("id"),
            "url": run.get("html_url"),
        }
        for name, run in latest.items()
    }
    complete = all(w is not None and w["status"] == "completed" for w in workflows.values())
    return {"complete": complete, "timed_out": False, "workflows": workflows}


def status(
    c: GitHubClient, repo: str, sha: str, required: list[str], not_before: dict[str, str] | None = None
) -> dict[str, Any]:
    if not required:
        return {"complete": True, "timed_out": False, "workflows": {}}
    return summarize(latest_runs(gh.workflow_runs(c, repo, sha), required, not_before))


def resolve_waits(c: GitHubClient, repo: str, sha: str) -> int:
    """Wake every review waiting on this commit whose workflows are all done."""
    resumed = 0
    for wait in store.waits_for(repo, sha):
        result = status(c, repo, sha, wait["required"], wait.get("not_before"))
        if result["complete"]:
            send_success(wait["token"], result)
            store.delete_wait(repo, sha, wait["sk"].removeprefix("EXEC#"))
            resumed += 1
    return resumed


def send_success(token: str, output: dict[str, Any]) -> None:
    import json

    try:
        aws.client("stepfunctions").send_task_success(taskToken=token, output=json.dumps(output))
    except Exception as err:
        # Another path already resumed it, or the wait timed out. Both are fine.
        if aws.error_code(err) not in ("TaskTimedOut", "TaskDoesNotExist", "InvalidToken"):
            raise
