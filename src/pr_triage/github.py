"""A small GitHub REST client authenticated as the GitHub App (stdlib HTTP, PyJWT for RS256)."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import jwt

API = "https://api.github.com"
API_VERSION = "2022-11-28"
log = logging.getLogger(__name__)


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


class TransientGitHubError(GitHubError):
    """Server errors and rate limits: worth retrying later."""


@dataclass
class Response:
    status: int
    data: Any
    headers: dict[str, str]


def app_jwt(app_id: str, private_key: str, now: int | None = None) -> str:
    issued = int(time.time()) if now is None else now
    payload = {"iat": issued - 60, "exp": issued + 540, "iss": str(app_id)}
    return jwt.encode(payload, private_key, algorithm="RS256")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


def _send(request: urllib.request.Request, *, follow: bool = True, timeout: float = 20):
    opener = urllib.request.urlopen if follow else _NO_REDIRECT.open
    try:
        with opener(request, timeout=timeout) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as err:
        headers = {k.lower(): v for k, v in (err.headers or {}).items()}
        return err.code, err.read(), headers


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")


def _next_link(link: str | None) -> str | None:
    for part in (link or "").split(","):
        url, _, rel = part.partition(";")
        if 'rel="next"' in rel:
            return url.strip().strip("<>")
    return None


class GitHubClient:
    def __init__(self, token: str, api: str = API):
        self._token = token
        self._api = api

    def _headers(self, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": accept,
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "pr-triage",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
        retries: int = 2,
    ) -> Response:
        url = path if path.startswith("https://") else f"{self._api}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = self._headers(accept)
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            status, raw, resp_headers = _send(req)
            if status < 400:
                return Response(status, _decode(raw), resp_headers)
            limited = status == 429 or (status == 403 and resp_headers.get("x-ratelimit-remaining") == "0")
            transient = status >= 500 or limited
            if transient and attempt < retries:
                time.sleep(min(2**attempt, 4))
                continue
            detail = _decode(raw)
            message = detail.get("message", "") if isinstance(detail, dict) else str(detail)[:200]
            raise (TransientGitHubError if transient else GitHubError)(status, message)
        raise AssertionError("unreachable")

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs).data

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return self.request("POST", path, body=body if body is not None else {}, **kwargs).data

    def patch(self, path: str, body: Any, **kwargs: Any) -> Any:
        return self.request("PATCH", path, body=body, **kwargs).data

    def put(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return self.request("PUT", path, body=body, **kwargs).data

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs).data

    def paginate(
        self, path: str, params: dict[str, Any] | None = None, *, key: str | None = None, max_pages: int = 30
    ) -> list[Any]:
        items: list[Any] = []
        url: str | None = path
        query: dict[str, Any] | None = {"per_page": 100, **(params or {})}
        for _ in range(max_pages):
            if url is None:
                break
            resp = self.request("GET", url, params=query)
            items.extend(resp.data[key] if key else resp.data)
            url, query = _next_link(resp.headers.get("link")), None
        return items

    def download(self, path: str) -> bytes:
        """Follow GitHub's redirect to blob storage without sending our token there."""
        req = urllib.request.Request(f"{self._api}{path}", headers=self._headers("application/vnd.github+json"))
        status, raw, headers = _send(req, follow=False)
        if status in (301, 302, 303, 307, 308) and headers.get("location"):
            status, raw, _ = _send(urllib.request.Request(headers["location"], headers={"User-Agent": "pr-triage"}))
        if status >= 400:
            raise GitHubError(status, f"download of {path} failed")
        return raw


class GitHubApp:
    def __init__(self, app_id: str, private_key: str, api: str = API):
        self.app_id = app_id
        self._private_key = private_key
        self._api = api
        self._installations: dict[str, int] = {}
        self._tokens: dict[int, tuple[str, float]] = {}

    def _as_app(self) -> GitHubClient:
        return GitHubClient(app_jwt(self.app_id, self._private_key), self._api)

    def installation_id(self, repo: str) -> int:
        if repo not in self._installations:
            self._installations[repo] = self._as_app().get(f"/repos/{repo}/installation")["id"]
        return self._installations[repo]

    def client_for(self, repo: str) -> GitHubClient:
        installation = self.installation_id(repo)
        token, expires = self._tokens.get(installation, ("", 0.0))
        if not token or expires - time.time() < 300:
            data = self._as_app().post(f"/app/installations/{installation}/access_tokens")
            token = data["token"]
            expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
            self._tokens[installation] = (token, expires)
        return GitHubClient(token, self._api)


_app: GitHubApp | None = None


def app() -> GitHubApp:
    global _app
    if _app is None:
        from . import params

        _app = GitHubApp(params.secret("app-id"), params.secret("private-key"))
    return _app


def client(repo: str) -> GitHubClient:
    return app().client_for(repo)
