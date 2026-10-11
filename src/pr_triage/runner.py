"""Run api and log items of the expected behavior against a deployed API (a ticket preview or dev).

The base URL and the log group always come from our own CloudFormation outputs, never from
the PR or a workflow artifact, so a PR cannot point these requests somewhere else.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from . import aws
from .expected import Item, check_api, check_log

HttpResult = tuple[int | None, Any, str | None, str | None]  # status, body, request id, error


def http_call(base_url: str, item: Item, timeout: float = 10) -> HttpResult:
    data = json.dumps(item.body).encode() if item.body is not None else None
    headers = {"Accept": "application/json", "User-Agent": "pr-triage-verify", **item.headers}
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(base_url.rstrip("/") + item.path, data=data, method=item.method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status, raw, resp_headers = resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as err:
        status, raw, resp_headers = err.code, err.read(), err.headers
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        return None, None, None, str(err)
    try:
        body: Any = json.loads(raw) if raw else None
    except ValueError:
        body = raw.decode("utf-8", "replace")
    return status, body, resp_headers.get("x-request-id"), None


def log_lines(log_group: str, request_id: str, since_ms: int, wait_seconds: float = 25) -> list[dict[str, Any]]:
    """JSON log lines for one request. Logs arrive a few seconds late, so poll for a while."""
    deadline = time.time() + wait_seconds
    while True:
        resp = aws.client("logs").filter_log_events(
            logGroupName=log_group,
            startTime=since_ms - 5_000,
            filterPattern=f'{{ $.request_id = "{request_id}" }}',
        )
        lines = []
        for event in resp.get("events", []):
            try:
                lines.append(json.loads(event["message"]))
            except ValueError:
                continue
        if lines or time.time() >= deadline:
            return lines
        time.sleep(3)


def run(
    items: list[Item],
    base_url: str,
    log_group: str | None,
    *,
    read_only: bool = False,
    http: Callable[[str, Item], HttpResult] = http_call,
    logs: Callable[[str, str, int], list[dict[str, Any]]] = log_lines,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    requests: dict[str, tuple[str | None, int]] = {}
    for item in items:
        if item.type == "api":
            if read_only and item.method != "GET":
                results.append(
                    {
                        "id": item.id,
                        "type": "api",
                        "request": item.request,
                        "ok": None,
                        "detail": "skipped: only GET runs after merge",
                    }
                )
                continue
            started = int(time.time() * 1000)
            status, body, request_id, error = http(base_url, item)
            ok, detail = (False, f"request failed: {error}") if error else check_api(item, status or 0, body)
            requests[item.id] = (request_id, started)
            results.append(
                {
                    "id": item.id,
                    "type": "api",
                    "request": item.request,
                    "ok": ok,
                    "status": status,
                    "request_id": request_id,
                    "detail": detail,
                }
            )
        elif item.type == "log":
            request_id, started = requests.get(item.after or "", (None, 0))
            if not request_id or not log_group:
                results.append({"id": item.id, "type": "log", "ok": None, "detail": f"no request id from {item.after}"})
                continue
            ok, detail = check_log(item, logs(log_group, request_id, started))
            results.append({"id": item.id, "type": "log", "ok": ok, "detail": detail})
        else:
            results.append({"id": item.id, "type": "ui", "ok": None, "detail": "checked by a person"})
    passed = all(r["ok"] is not False for r in results)
    return {"passed": passed, "error": None, "items": results}


def stack_outputs(stack_name: str) -> dict[str, str]:
    try:
        resp = aws.client("cloudformation").describe_stacks(StackName=stack_name)
    except Exception as err:
        if aws.error_code(err) == "ValidationError":  # the stack does not exist
            return {}
        raise
    return {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}
