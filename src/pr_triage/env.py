from __future__ import annotations

import os
from functools import cache
from pathlib import Path

PARAM_PREFIX = os.environ.get("PARAM_PREFIX", "/pr-triage")


def required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"environment variable {name} is not set")
    return value


def optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def github_owner() -> str:
    return os.environ.get("GITHUB_OWNER", "jaeneungsim")


def managed_repos() -> set[str]:
    names = os.environ.get("MANAGED_REPOS", "demo-inventory-api,demo-inventory-web")
    return {f"{github_owner()}/{name.strip()}" for name in names.split(",") if name.strip()}


@cache
def package_root() -> Path:
    """The directory that holds policy/ and prompts/: the Lambda task root or the repo root."""
    here = Path(__file__).resolve().parent
    for candidate in (here.parent, here.parent.parent):
        if (candidate / "policy").is_dir():
            return candidate
    raise RuntimeError("policy/ directory not found next to the package")
