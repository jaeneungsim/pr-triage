"""Settings in SSM Parameter Store, cached for a short time per Lambda container."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from . import aws
from .env import PARAM_PREFIX

log = logging.getLogger(__name__)

LEVELS = ("v1", "v2", "v3")
MODES = ("advisory", "auto-approve")

_cache: dict[str, tuple[float, str]] = {}


def get_many(names: list[str], *, decrypt: bool = False, ttl: float = 60.0) -> dict[str, str]:
    now = time.monotonic()
    stale = [n for n in names if n not in _cache or now - _cache[n][0] > ttl]
    for start in range(0, len(stale), 10):
        resp = aws.client("ssm").get_parameters(Names=stale[start : start + 10], WithDecryption=decrypt)
        for param in resp["Parameters"]:
            _cache[param["Name"]] = (now, param["Value"])
    return {n: _cache[n][1] for n in names if n in _cache}


def secret(name: str) -> str:
    """A GitHub App value: app-id, private-key or webhook-secret."""
    full = f"{PARAM_PREFIX}/github/{name}"
    values = get_many([full], decrypt=True, ttl=300)
    if full not in values:
        raise RuntimeError(f"SSM parameter {full} is not set")
    return values[full]


@dataclass(frozen=True)
class Limits:
    daily_reviews: int = 10
    monthly_usd: float = 5.0
    max_input_tokens: int = 30_000
    max_output_tokens: int = 3_000


@dataclass(frozen=True)
class Model:
    id: str = ""
    input_usd_per_mtok: float = 1.0
    output_usd_per_mtok: float = 5.0


@dataclass(frozen=True)
class Config:
    enabled: bool
    level: str
    mode: str
    limits: Limits
    model: Model
    allowed_authors: tuple[str, ...]


CONFIG_KEYS = ("enabled", "level", "mode", "limits", "model", "allowed-authors")


def load_config() -> Config:
    names = [f"{PARAM_PREFIX}/config/{key}" for key in CONFIG_KEYS]
    raw = get_many(names)
    return parse_config({key: raw.get(f"{PARAM_PREFIX}/config/{key}") for key in CONFIG_KEYS})


def parse_config(raw: dict[str, str | None]) -> Config:
    """Parse the SSM values. A broken value turns AI reviews off instead of guessing."""
    try:
        limits = Limits(**json.loads(raw.get("limits") or "{}"))
        model = Model(**json.loads(raw.get("model") or "{}"))
        authors = tuple(json.loads(raw.get("allowed-authors") or "[]"))
        level = raw.get("level") or "v1"
        mode = raw.get("mode") or "advisory"
        if level not in LEVELS or mode not in MODES or not model.id:
            raise ValueError(f"level={level!r} mode={mode!r} model={model.id!r}")
        return Config(
            enabled=(raw.get("enabled") or "off") == "on",
            level=level,
            mode=mode,
            limits=limits,
            model=model,
            allowed_authors=authors,
        )
    except (TypeError, ValueError) as err:
        log.error("invalid configuration, AI reviews are off: %s", err)
        return Config(False, "v1", "advisory", Limits(), Model(), ())
