"""After a merge is deployed to dev, run the PR's read-only expected behavior against dev.

Only GET requests run, because dev is shared. A failure is reported on the merged PR and a
person decides whether to revert.
"""

from __future__ import annotations

from typing import Any

from .. import expected, gh, github, policy, runner
from ..render import OK_MARK, cell

DEV_STACK = "inv-api-dev"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    repo, sha = event["repo"], event["sha"]
    repo_rules = policy.repo_policy(policy.load(), repo)
    if not repo_rules or repo_rules.get("kind") != "api":
        return {"skipped": "only the API repo has a post-merge check"}
    c = github.client(repo)
    merged = [p for p in gh.pulls_for_commit(c, repo, sha) if p.get("merged_at")]
    if not merged:
        return {"skipped": "no merged PR for this commit"}
    pr = merged[0]
    parsed = expected.parse_body(pr.get("body"))
    items = [expected.item_from_dict(i) for i in parsed["items"] if i["type"] in ("api", "log")]
    if parsed["error"] or not any(i.type == "api" and i.method == "GET" for i in items):
        return {"skipped": "no read-only expected behavior"}

    outputs = runner.stack_outputs(DEV_STACK)
    if not outputs.get("ApiUrl"):
        return {"skipped": f"{DEV_STACK} has no ApiUrl output"}
    result = runner.run(items, outputs["ApiUrl"], outputs.get("LogGroupName"), read_only=True)

    verdict = "passed" if result["passed"] else "failed"
    lines = [f"**pr-triage post-merge check on dev ({sha[:7]}): {verdict}**", "| Item | Result |", "|---|---|"]
    lines += [
        f"| {i['id']} {cell(i.get('request') or '')} | {OK_MARK[i['ok']]} {cell(i['detail'])} |"
        for i in result["items"]
    ]
    if not result["passed"]:
        lines.append(
            "The merged change does not behave as its PR described on dev. A person should decide whether to revert it."
        )
        gh.add_label(c, repo, pr["number"], gh.LABELS["post_merge_failed"])
    gh.comment(c, repo, pr["number"], "\n".join(lines))
    return {"pr": pr["number"], "passed": result["passed"]}
