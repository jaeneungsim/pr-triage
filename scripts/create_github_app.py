#!/usr/bin/env python3
"""Create the pr-triage GitHub App from a manifest and store its credentials in SSM.

GitHub's manifest flow fills in every setting. You only click "Create GitHub App" in the
browser. The private key and the webhook secret go straight to SSM Parameter Store as
SecureString values; this script never prints them.

    python3 scripts/create_github_app.py --webhook-url https://…/webhook --profile jae-dev

Then install the app on demo-inventory-api and demo-inventory-web with the printed link.
"""

from __future__ import annotations

import argparse
import html
import http.server
import json
import os
import secrets
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request
import webbrowser

PORT = 8765
PREFIX = "/pr-triage/github"
PERMISSIONS = {
    "pull_requests": "write",  # labels and the summary comment
    "checks": "write",  # the pr-triage check run on each commit
    "issues": "write",  # the morning digest issue
    "contents": "read",  # tickets, changed files, openapi.yaml
    "actions": "read",  # workflow results and their artifacts
    "metadata": "read",
}
EVENTS = ["pull_request", "workflow_run"]


def manifest(owner: str, name: str, webhook_url: str) -> dict:
    return {
        "name": name,
        "url": f"https://github.com/{owner}/pr-triage",
        "description": "AI-first PR triage: cheap checks first, the AI suggests, code decides.",
        "hook_attributes": {"url": webhook_url, "active": True},
        "redirect_url": f"http://127.0.0.1:{PORT}/callback",
        "public": False,
        "default_permissions": PERMISSIONS,
        "default_events": EVENTS,
    }


def put_parameter(name: str, value: str, secure: bool, aws_args: list[str]) -> None:
    """Pass the value through a private temp file so it never appears in a process list."""
    fd, path = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(value)
        kind = "SecureString" if secure else "String"
        subprocess.run(
            [
                "aws",
                "ssm",
                "put-parameter",
                "--name",
                name,
                "--type",
                kind,
                "--overwrite",
                "--value",
                f"file://{path}",
                *aws_args,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
    finally:
        os.remove(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--webhook-url", required=True, help="WebhookUrl output of the pr-triage-core stack")
    parser.add_argument("--owner", default="jaeneungsim")
    parser.add_argument("--name", help="app name; must be unique on GitHub (default: pr-triage-<owner>)")
    parser.add_argument("--profile")
    parser.add_argument("--region", default="ap-southeast-2")
    args = parser.parse_args()
    aws_args = ["--region", args.region] + (["--profile", args.profile] if args.profile else [])
    state = secrets.token_urlsafe(16)
    form = json.dumps(manifest(args.owner, args.name or f"pr-triage-{args.owner}", args.webhook_url))
    done = threading.Event()
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            pass

        def reply(self, body: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self) -> None:
            url = urllib.parse.urlparse(self.path)
            if url.path == "/":
                action = f"https://github.com/settings/apps/new?state={state}"
                self.reply(
                    f'<form id="f" method="post" action="{action}">'
                    f'<input type="hidden" name="manifest" value="{html.escape(form)}"></form>'
                    "<p>Opening GitHub…</p><script>document.getElementById('f').submit()</script>"
                )
                return
            query = urllib.parse.parse_qs(url.query)
            if url.path != "/callback" or query.get("state", [""])[0] != state or "code" not in query:
                self.reply("<p>Unexpected request.</p>")
                return
            request = urllib.request.Request(
                f"https://api.github.com/app-manifests/{query['code'][0]}/conversions",
                method="POST",
                headers={"Accept": "application/vnd.github+json", "User-Agent": "pr-triage-setup"},
            )
            with urllib.request.urlopen(request, timeout=30) as resp:
                app = json.loads(resp.read())
            put_parameter(f"{PREFIX}/app-id", str(app["id"]), False, aws_args)
            put_parameter(f"{PREFIX}/private-key", app["pem"], True, aws_args)
            put_parameter(f"{PREFIX}/webhook-secret", app["webhook_secret"], True, aws_args)
            result.update(id=app["id"], slug=app["slug"])
            self.reply("<p>Done. The app was created and its secrets are in SSM. You can close this tab.</p>")
            done.set()

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Opening http://127.0.0.1:{PORT}/ - click 'Create GitHub App' on the GitHub page.")
    webbrowser.open(f"http://127.0.0.1:{PORT}/")
    if not done.wait(timeout=600):
        raise SystemExit("Timed out after 10 minutes; nothing was stored.")
    server.shutdown()
    print(f"Created app {result['slug']} (id {result['id']}). Secrets are in SSM under {PREFIX}/.")
    print(f"Install it on the two demo repos: https://github.com/apps/{result['slug']}/installations/new")


if __name__ == "__main__":
    main()
