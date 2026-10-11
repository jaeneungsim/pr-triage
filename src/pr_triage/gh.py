"""GitHub operations the review steps need, on top of GitHubClient."""

from __future__ import annotations

import base64
import io
import json
import urllib.parse
import zipfile
from datetime import UTC, datetime
from typing import Any

from .github import GitHubClient, GitHubError

CHECK_NAME = "pr-triage"
COMMENT_MARKER = "<!-- pr-triage:summary -->"
LABEL_PREFIX = "triage: "
LABELS = {
    "checking": "triage: checking",
    "approvable": "triage: approvable",
    "changes_requested": "triage: changes requested",
    "needs_human": "triage: needs human review",
    "post_merge_failed": "triage: post-merge failed",
}
TEXT_SUFFIXES = (
    ".py",
    ".ts",
    ".js",
    ".mjs",
    ".json",
    ".yaml",
    ".yml",
    ".md",
    ".html",
    ".css",
    ".toml",
    ".txt",
    ".cfg",
    ".ini",
    ".sh",
    ".tsx",
    ".jsx",
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_pull(c: GitHubClient, repo: str, number: int) -> dict[str, Any]:
    return c.get(f"/repos/{repo}/pulls/{number}")


def list_files(c: GitHubClient, repo: str, number: int) -> list[dict[str, Any]]:
    return c.paginate(f"/repos/{repo}/pulls/{number}/files")


def get_text(c: GitHubClient, repo: str, path: str, ref: str | None = None, max_bytes: int = 200_000) -> str | None:
    """A file as text, or None if it does not exist, is too big or is not text."""
    params = {"ref": ref} if ref else None
    try:
        data = c.get(f"/repos/{repo}/contents/{urllib.parse.quote(path)}", params=params)
    except GitHubError as err:
        if err.status == 404:
            return None
        raise
    if not isinstance(data, dict) or data.get("type") != "file" or data.get("size", 0) > max_bytes:
        return None
    try:
        return base64.b64decode(data.get("content", "")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def is_text_path(path: str) -> bool:
    return path.endswith(TEXT_SUFFIXES) or "." not in path.rsplit("/", 1)[-1]


def create_check_run(c: GitHubClient, repo: str, sha: str, title: str, summary: str) -> int:
    data = c.post(
        f"/repos/{repo}/check-runs",
        {
            "name": CHECK_NAME,
            "head_sha": sha,
            "status": "in_progress",
            "started_at": now_iso(),
            "output": {"title": title, "summary": summary},
        },
    )
    return int(data["id"])


def complete_check_run(
    c: GitHubClient, repo: str, check_run_id: int, conclusion: str, title: str, summary: str
) -> None:
    c.patch(
        f"/repos/{repo}/check-runs/{check_run_id}",
        {
            "status": "completed",
            "conclusion": conclusion,
            "completed_at": now_iso(),
            "output": {"title": title[:255], "summary": summary[:65000]},
        },
    )


def set_triage_label(c: GitHubClient, repo: str, number: int, label: str, current: list[str] | None = None) -> None:
    """Replace any triage label with `label`."""
    if current is None:
        current = [lbl["name"] for lbl in c.get(f"/repos/{repo}/issues/{number}/labels")]
    for name in current:
        if name.startswith(LABEL_PREFIX) and name != label and name != LABELS["post_merge_failed"]:
            try:
                c.delete(f"/repos/{repo}/issues/{number}/labels/{urllib.parse.quote(name)}")
            except GitHubError as err:
                if err.status != 404:
                    raise
    if label not in current:
        c.post(f"/repos/{repo}/issues/{number}/labels", {"labels": [label]})


def add_label(c: GitHubClient, repo: str, number: int, label: str) -> None:
    c.post(f"/repos/{repo}/issues/{number}/labels", {"labels": [label]})


def upsert_comment(c: GitHubClient, repo: str, number: int, body: str, marker: str = COMMENT_MARKER) -> int:
    """Keep one bot comment per PR and edit it, instead of adding a new one per run."""
    for comment in c.paginate(f"/repos/{repo}/issues/{number}/comments", max_pages=10):
        user = comment.get("user") or {}
        if user.get("type") == "Bot" and marker in (comment.get("body") or ""):
            c.patch(f"/repos/{repo}/issues/comments/{comment['id']}", {"body": body})
            return int(comment["id"])
    return int(c.post(f"/repos/{repo}/issues/{number}/comments", {"body": body})["id"])


def comment(c: GitHubClient, repo: str, number: int, body: str) -> None:
    c.post(f"/repos/{repo}/issues/{number}/comments", {"body": body})


def approve(c: GitHubClient, repo: str, number: int, sha: str, body: str) -> None:
    c.post(f"/repos/{repo}/pulls/{number}/reviews", {"commit_id": sha, "event": "APPROVE", "body": body})


def workflow_runs(c: GitHubClient, repo: str, sha: str) -> list[dict[str, Any]]:
    return c.paginate(f"/repos/{repo}/actions/runs", {"head_sha": sha}, key="workflow_runs", max_pages=3)


def artifact_json(c: GitHubClient, repo: str, run_id: int, artifact: str, filename: str) -> Any:
    """Read one JSON file from a workflow run's artifact. None when it is not there."""
    listing = c.get(f"/repos/{repo}/actions/runs/{run_id}/artifacts", params={"name": artifact})
    for item in listing.get("artifacts", []):
        if item.get("name") != artifact or item.get("expired"):
            continue
        archive = zipfile.ZipFile(io.BytesIO(c.download(f"/repos/{repo}/actions/artifacts/{item['id']}/zip")))
        names = [n for n in archive.namelist() if n.rsplit("/", 1)[-1] == filename]
        if names:
            return json.loads(archive.read(names[0]))
    return None


def pulls_for_commit(c: GitHubClient, repo: str, sha: str) -> list[dict[str, Any]]:
    return c.get(f"/repos/{repo}/commits/{sha}/pulls")
