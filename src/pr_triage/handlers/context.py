"""Step 1: collect everything the review needs and store it in S3.

The state machine only carries a short summary; the full context goes to S3 because Step
Functions passes at most 256 KB between steps.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import evidence, expected, gh, github, params, policy, store, tickets
from ..env import github_owner
from ..github import GitHubClient

log = logging.getLogger(__name__)

MAX_PATCH_CHARS = 20_000
MAX_FILE_BYTES = 60_000
MAX_CONTENT_BYTES = 250_000
MAX_SIBLINGS = 3
MAX_SIBLING_DIFF = 12_000


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    state, execution = event["state"], event["execution"]
    request = state["request"]
    repo, number, sha = request["repo"], request["pr"], request["sha"]
    rules = policy.load()
    repo_rules = policy.repo_policy(rules, repo)
    if repo_rules is None:
        return {"skip": True, "reason": f"{repo} is not in policy.yaml"}

    c = github.client(repo)
    pr = gh.get_pull(c, repo, number)
    if pr["state"] != "open" or pr["head"]["sha"] != sha:
        return {"skip": True, "reason": "the PR was closed or has a newer commit"}

    cfg = params.load_config()
    head_repo = (pr["head"].get("repo") or {}).get("full_name")
    fork = head_repo != repo
    level = "v1" if fork else cfg.level  # forks never reach CI artifacts or AWS previews

    check_run_id = gh.create_check_run(c, repo, sha, "Reviewing…", f"pr-triage is reviewing commit {sha[:7]}.")
    store.set_head_check_run(repo, number, sha, check_run_id)
    gh.set_triage_label(c, repo, number, gh.LABELS["checking"], [lbl["name"] for lbl in pr.get("labels", [])])

    files = gh.list_files(c, repo, number)
    paths = [f["filename"] for f in files]
    ticket_key = tickets.extract_key([pr["head"]["ref"], pr["title"]], rules["ticket"]["prefix"])
    ticket, ticket_error = _ticket(rules, ticket_key)
    body = pr.get("body") or ""
    contents = _contents(c, repo, sha, files)
    siblings = _siblings(ticket_key, repo, number)

    doc = {
        "repo": repo,
        "pr": number,
        "sha": sha,
        "url": pr["html_url"],
        "title": pr["title"],
        "body": body,
        "author": pr["user"]["login"],
        "base_ref": pr["base"]["ref"],
        "head_ref": pr["head"]["ref"],
        "fork": fork,
        "labels": [lbl["name"] for lbl in pr.get("labels", [])],
        "files": [_trim(f) for f in files],
        "contents": contents,
        "ticket_key": ticket_key,
        "ticket": ticket,
        "ticket_error": ticket_error,
        "expected": expected.parse_body(body),
        "pr_type": expected.pr_type(body) or _inferred_type(paths, rules),
        "siblings": siblings,
        "openapi": _openapi(c, repo, sha, repo_rules, contents, siblings),
        "ai_ok_sha": store.get_ai_ok(repo, number) if fork else None,
    }
    prefix = evidence.run_prefix(repo, number, sha, execution["name"])
    key = evidence.put_json(f"{prefix}/context.json", doc)

    required = policy.required_workflows(rules, repo, level)
    not_before = {}
    if request["reason"] == "edited":
        not_before = {w: request["updated_at"] for w in repo_rules.get("rerun_on_edit", []) if w in required}
    return {
        "skip": False,
        "s3_key": key,
        "prefix": prefix,
        "repo": repo,
        "pr": number,
        "sha": sha,
        "url": pr["html_url"],
        "title": pr["title"],
        "author": doc["author"],
        "base_ref": doc["base_ref"],
        "head_ref": doc["head_ref"],
        "fork": fork,
        "ticket_key": ticket_key,
        "stage": tickets.stage_for(ticket_key) if ticket_key else None,
        "level": level,
        "mode": cfg.mode,
        "pr_type": doc["pr_type"],
        "check_run_id": check_run_id,
        "required_workflows": required,
        "not_before": not_before,
    }


def _trim(file: dict[str, Any]) -> dict[str, Any]:
    patch = file.get("patch")
    if patch and len(patch) > MAX_PATCH_CHARS:
        patch = patch[:MAX_PATCH_CHARS] + "\n… (patch cut)"
    return {
        "filename": file["filename"],
        "status": file.get("status"),
        "additions": file.get("additions", 0),
        "deletions": file.get("deletions", 0),
        "patch": patch,
        "previous_filename": file.get("previous_filename"),
    }


def _inferred_type(paths: list[str], rules: dict[str, Any]) -> str:
    category = policy.categorize(paths, rules)
    return {"docs_only": "docs", "tests_only": "tests"}.get(category, "feature")


def _ticket(rules: dict[str, Any], key: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Read the ticket from the default branch of the ticket repo (no ref = default branch)."""
    if not key:
        return None, None
    ticket_repo = f"{github_owner()}/{rules['ticket']['repo']}"
    text = gh.get_text(github.client(ticket_repo), ticket_repo, f"{rules['ticket']['dir']}/{key}.md")
    if text is None:
        return None, "it was not found"
    try:
        ticket = tickets.parse_ticket(text)
    except tickets.TicketFormatError as err:
        return None, f"it could not be read ({err})"
    if ticket.key != key:
        return None, f"its key is {ticket.key or 'missing'}"
    return ticket.to_dict(), None


def _contents(c: GitHubClient, repo: str, sha: str, files: list[dict[str, Any]]) -> dict[str, str]:
    """Full text of changed files at the PR commit, for line numbers and context."""
    contents: dict[str, str] = {}
    total = 0
    for file in files:
        path = file["filename"]
        if file.get("status") == "removed" or not gh.is_text_path(path):
            continue
        text = gh.get_text(c, repo, path, ref=sha, max_bytes=MAX_FILE_BYTES)
        if text is None or total + len(text) > MAX_CONTENT_BYTES:
            continue
        contents[path] = text
        total += len(text)
    return contents


def _siblings(ticket_key: str | None, repo: str, number: int) -> list[dict[str, Any]]:
    if not ticket_key:
        return []
    found = []
    for entry in store.ticket_prs(ticket_key):
        if entry.get("state") != "open" or (entry["repo"], entry["number"]) == (repo, number):
            continue
        try:
            c = github.client(entry["repo"])
            pr = gh.get_pull(c, entry["repo"], entry["number"])
            diff = "\n".join(
                f"### {f['filename']}\n{f.get('patch') or ''}" for f in gh.list_files(c, entry["repo"], entry["number"])
            )
        except github.GitHubError as err:
            log.warning("sibling %s#%s unavailable: %s", entry["repo"], entry["number"], err)
            continue
        items = expected.parse_body(pr.get("body"))["items"]
        found.append(
            {
                "repo": entry["repo"],
                "number": entry["number"],
                "title": pr["title"],
                "sha": pr["head"]["sha"],
                "covers": sorted({c for item in items for c in item.get("covers", [])}),
                "decision": entry.get("decision"),
                "diff": diff[:MAX_SIBLING_DIFF],
            }
        )
        if len(found) >= MAX_SIBLINGS:
            break
    return found


def _openapi(
    c: GitHubClient,
    repo: str,
    sha: str,
    repo_rules: dict[str, Any],
    contents: dict[str, str],
    siblings: list[dict[str, Any]],
) -> str | None:
    """The API description the PR must match: the PR's own for the API repo, else the API's."""
    if repo_rules.get("kind") == "api":
        path = repo_rules.get("openapi", "openapi.yaml")
        return contents.get(path) or gh.get_text(c, repo, path, ref=sha)
    rules = policy.load()
    api_name = next((name for name, r in rules["repos"].items() if r.get("kind") == "api"), None)
    if api_name is None:
        return None
    api_repo = f"{github_owner()}/{api_name}"
    api_pr = next((s for s in siblings if s["repo"] == api_repo), None)
    path = rules["repos"][api_name].get("openapi", "openapi.yaml")
    return gh.get_text(github.client(api_repo), api_repo, path, ref=api_pr["sha"] if api_pr else None)
