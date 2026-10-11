#!/usr/bin/env python3
"""Register the pr-triage GitHub App and store its credentials in SSM Parameter Store.

GitHub's manifest flow posts the settings from a local page to github.com. Browsers no longer
send the GitHub login cookie with such a cross-site POST, so the flow ends in "We didn't find
an App Manifest". This script uses GitHub's pre-filled registration link instead:

1. `open`: creates a webhook secret, stores it in SSM, copies it to the clipboard, and opens the
   "Register new GitHub App" page with every other setting filled in. Paste the secret into
   "Webhook secret", create the app, then "Generate a private key" (GitHub downloads a .pem).
2. `store`: stores the App ID and the private key in SSM and clears the clipboard. Delete the
   downloaded .pem afterwards.

    python3 scripts/register_github_app.py open --webhook-url https://…/webhook --profile jae-dev
    python3 scripts/register_github_app.py store --app-id 1234567 --pem ~/Downloads/….pem --profile jae-dev

Secret values are never printed.
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import tempfile
import urllib.parse
import webbrowser
from pathlib import Path

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


def put_parameter(name: str, value: str, secure: bool, aws_args: list[str]) -> None:
    """Pass the value through a private temp file so it never appears in a process list."""
    fd, path = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(value)
        kind = "SecureString" if secure else "String"
        command = ["aws", "ssm", "put-parameter", "--name", name, "--type", kind, "--overwrite"]
        subprocess.run([*command, "--value", f"file://{path}", *aws_args], check=True, stdout=subprocess.DEVNULL)
    finally:
        os.remove(path)


def registration_url(owner: str, name: str, webhook_url: str) -> str:
    query = [
        ("name", name),
        ("description", "AI-first PR triage: cheap checks first, the AI suggests, code decides."),
        ("url", f"https://github.com/{owner}/pr-triage"),
        ("public", "false"),
        ("webhook_active", "true"),
        ("webhook_url", webhook_url),
        *PERMISSIONS.items(),
        *(("events[]", event) for event in EVENTS),
    ]
    return "https://github.com/settings/apps/new?" + urllib.parse.urlencode(query)


def open_registration(args: argparse.Namespace, aws_args: list[str]) -> None:
    secret = secrets.token_hex(32)
    put_parameter(f"{PREFIX}/webhook-secret", secret, True, aws_args)
    subprocess.run(["pbcopy"], input=secret.encode(), check=True)
    url = registration_url(args.owner, args.name or f"pr-triage-{args.owner}", args.webhook_url)
    webbrowser.open(url)
    print("A new webhook secret is in SSM and on your clipboard.")
    print("On the GitHub page: paste it into 'Webhook secret', click 'Create GitHub App',")
    print("note the App ID, then click 'Generate a private key'.")


def store_credentials(args: argparse.Namespace, aws_args: list[str]) -> None:
    pem_path = Path(args.pem).expanduser()
    pem = pem_path.read_text()
    if "PRIVATE KEY-----" not in pem:
        raise SystemExit(f"{pem_path} does not look like a private key")
    if not args.app_id.isdigit():
        raise SystemExit("--app-id must be the numeric App ID from the app's settings page")
    put_parameter(f"{PREFIX}/app-id", args.app_id, False, aws_args)
    put_parameter(f"{PREFIX}/private-key", pem, True, aws_args)
    subprocess.run(["pbcopy"], input=b"", check=False)
    print(f"Stored the App ID and the private key under {PREFIX}/ and cleared the clipboard.")
    print(f"Delete the downloaded key now: {pem_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile")
    parser.add_argument("--region", default="ap-southeast-2")
    commands = parser.add_subparsers(dest="command", required=True)
    opener = commands.add_parser("open", help="store a webhook secret and open the registration page")
    opener.add_argument("--webhook-url", required=True, help="WebhookUrl output of the pr-triage-core stack")
    opener.add_argument("--owner", default="jaeneungsim")
    opener.add_argument("--name", help="app name; must be unique on GitHub (default: pr-triage-<owner>)")
    storer = commands.add_parser("store", help="store the App ID and the private key")
    storer.add_argument("--app-id", required=True)
    storer.add_argument("--pem", required=True, help="the private key file GitHub downloaded")
    args = parser.parse_args()
    aws_args = ["--region", args.region] + (["--profile", args.profile] if args.profile else [])
    if args.command == "open":
        open_registration(args, aws_args)
    else:
        store_credentials(args, aws_args)


if __name__ == "__main__":
    main()
