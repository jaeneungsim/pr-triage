"""Steps 3-4 results: turn CI results into findings, and run the expected behavior on AWS (v3).

Artifacts come from workflows a same-repo PR could edit, so only their verdicts are read.
URLs are never taken from them: the preview API comes from our own stack outputs and the web
preview link is built from our own domain.
"""

from __future__ import annotations

from typing import Any

from .. import evidence, expected, gh, github, runner
from ..ci import PASSING
from ..env import optional
from ..github import GitHubClient
from ..policy import BLOCKING, CHANGES, HUMAN, NOTE

BAD_RELEVANCE = ("meaningless", "no_tests", "unexpected_failure")


def _hit(kind: str, outcome: str, message: str) -> dict[str, str]:
    return {"kind": kind, "outcome": outcome, "message": message, "source": "checks"}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    state = event["state"]
    ctx, ci_result = state["ctx"], state.get("ci") or {}
    hits: list[dict[str, str]] = []
    out: dict[str, Any] = {
        "blocking": False,
        "hits": hits,
        "timed_out": bool(ci_result.get("timed_out")),
        "workflows": ci_result.get("workflows") or {},
        "test_relevance": None,
        "behavior_ci": None,
        "behavior_preview": None,
        "preview": None,
    }
    if out["timed_out"]:
        hits.append(_hit("timeout", HUMAN, "CI did not finish within the waiting time."))
        out["blocking"] = True
        return out

    repo = ctx["repo"]
    c = github.client(repo)
    for name, run in out["workflows"].items():
        if run is None:
            hits.append(_hit("ci", HUMAN, f"Workflow {name} did not run for this commit."))
            continue
        conclusion = run.get("conclusion")
        if name == "test-relevance":
            out["test_relevance"] = _relevance(c, repo, run, hits)
        elif name == "behavior":
            out["behavior_ci"] = _behavior_ci(c, repo, run, hits)
        elif name in ("preview-deploy", "web-preview"):
            out["preview"] = _preview(ctx, name, run, hits)
        elif conclusion not in PASSING:
            hits.append(_hit("ci", CHANGES, f"Workflow {name} ended with {conclusion}: {run.get('url')}"))

    preview = out["preview"] or {}
    if preview.get("api_url") and not any(h["kind"] == "preview" for h in hits):
        doc = evidence.get_json(ctx["s3_key"])
        items = [expected.item_from_dict(i) for i in doc["expected"]["items"]]
        if any(item.type == "api" for item in items):
            result = runner.run(items, preview["api_url"], preview.get("log_group"))
            out["behavior_preview"] = result
            failed = [f"{i['id']} {i['detail']}" for i in result["items"] if i["ok"] is False]
            if failed:
                hits.append(
                    _hit("behavior", CHANGES, "Expected behavior failed on the AWS preview: " + "; ".join(failed))
                )

    out["blocking"] = any(h["outcome"] in BLOCKING for h in hits)
    evidence.put_json(f"{ctx['prefix']}/checks.json", out)
    return out


def _relevance(c: GitHubClient, repo: str, run: dict[str, Any], hits: list[dict[str, str]]) -> dict[str, Any] | None:
    if run.get("conclusion") == "skipped":
        return None
    data = gh.artifact_json(c, repo, run["run_id"], "test-relevance", "test-relevance.json")
    if not isinstance(data, dict):
        if run.get("conclusion") not in PASSING:
            hits.append(
                _hit("test_relevance", CHANGES, f"test-relevance failed without a result file: {run.get('url')}")
            )
        return None
    verdict, message = data.get("verdict"), str(data.get("message", ""))[:300]
    if verdict in BAD_RELEVANCE:
        hits.append(_hit("test_relevance", CHANGES, f"Tests do not show the change ({verdict}): {message}"))
    elif verdict == "undetermined":
        hits.append(_hit("test_relevance", NOTE, f"Test relevance could not be decided: {message}"))
    return {"verdict": verdict, "message": message, "tests": len(data.get("tests", []))}


def _behavior_ci(c: GitHubClient, repo: str, run: dict[str, Any], hits: list[dict[str, str]]) -> dict[str, Any] | None:
    if run.get("conclusion") == "skipped":
        return None
    data = gh.artifact_json(c, repo, run["run_id"], "behavior-results", "behavior-results.json")
    if not isinstance(data, dict):
        if run.get("conclusion") not in PASSING:
            hits.append(_hit("behavior", CHANGES, f"behavior failed without a result file: {run.get('url')}"))
        return None
    items = [
        {k: item.get(k) for k in ("id", "type", "ok", "request", "status", "detail")}
        for item in data.get("items", [])[:30]
        if isinstance(item, dict)
    ]
    result = {"passed": bool(data.get("passed")), "error": data.get("error"), "items": items}
    if data.get("error"):
        hits.append(_hit("behavior", CHANGES, f"Expected behavior could not run in CI: {str(data['error'])[:200]}"))
    elif not result["passed"]:
        failed = [f"{i['id']} {i.get('detail')}" for i in items if i.get("ok") is False]
        hits.append(_hit("behavior", CHANGES, "Expected behavior failed in CI: " + "; ".join(failed)))
    return result


def _preview(ctx: dict[str, Any], name: str, run: dict[str, Any], hits: list[dict[str, str]]) -> dict[str, Any] | None:
    conclusion = run.get("conclusion")
    if conclusion == "skipped" and not ctx.get("fork"):
        hits.append(_hit("preview", NOTE, f"The {name} workflow was skipped, so nothing ran on AWS."))
    if conclusion == "skipped" or not ctx.get("stage"):
        return None
    if conclusion not in PASSING:
        hits.append(_hit("preview", CHANGES, f"The {name} workflow failed: {run.get('url')}"))
        return None
    stage = ctx["stage"]
    if name == "web-preview":
        domain = optional("WEB_PREVIEW_DOMAIN")
        return {"stage": stage, "web_url": f"https://{domain}/{stage}/index.html" if domain else None}
    stack = f"inv-api-preview-{stage}"
    outputs = runner.stack_outputs(stack)
    if not outputs.get("ApiUrl"):
        hits.append(_hit("preview", HUMAN, f"The preview stack {stack} has no ApiUrl output."))
        return None
    return {"stage": stage, "stack": stack, "api_url": outputs["ApiUrl"], "log_group": outputs.get("LogGroupName")}
