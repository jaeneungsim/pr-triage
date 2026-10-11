"""The expected-behavior block in a PR description, and how results are checked against it.

The block is untrusted input. Requests built from it only ever go to our own preview or dev
API, and only the path comes from the block.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import markdown

BLOCK = re.compile(
    r"^(?P<fence>`{3,}|~{3,})[ \t]*yaml[ \t]+expected-behavior[ \t]*\n(?P<body>.*?)^(?P=fence)[ \t]*$",
    re.M | re.S,
)
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
PR_TYPES = ("feature", "bugfix", "refactor", "tests", "docs")
LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
SAFE_HEADERS = {"content-type", "accept", "accept-language"}

_ID = re.compile(r"^B\d+$")
_REQ = re.compile(r"^R\d+$")
_PATH = re.compile(r"^/(?!/)[A-Za-z0-9\-._~%!$&'()*+,;=:@/?]*$")
MAX_ITEMS = 30


class ExpectedBehaviorError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    id: str
    type: str
    covers: tuple[str, ...]
    method: str | None = None
    path: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    expect: dict[str, Any] = field(default_factory=dict)
    after: str | None = None
    step: str | None = None
    page: str | None = None

    @property
    def request(self) -> str | None:
        return f"{self.method} {self.path}" if self.method else None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "type": self.type, "covers": list(self.covers)}
        if self.type == "api":
            out.update(request=self.request, expect=self.expect)
            if self.headers:
                out["headers"] = self.headers
            if self.body is not None:
                out["body"] = self.body
        elif self.type == "log":
            out.update(after=self.after, expect=self.expect)
        else:
            out.update(step=self.step)
            if self.page:
                out["page"] = self.page
        return out


def extract_block(body: str | None) -> str | None:
    match = BLOCK.search(markdown.normalize(body))
    return match.group("body") if match else None


def pr_type(body: str | None) -> str | None:
    line = markdown.first_line(markdown.section(body, "Type"))
    if not line:
        return None
    word = line.strip("`*_ ").split()[0].lower()
    return word if word in PR_TYPES else None


def parse_items(text: str) -> list[Item]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ExpectedBehaviorError(f"the block is not valid YAML ({err})") from err
    if data is None:
        return []
    if not isinstance(data, list):
        raise ExpectedBehaviorError("the block must be a YAML list")
    if len(data) > MAX_ITEMS:
        raise ExpectedBehaviorError(f"the block has more than {MAX_ITEMS} items")
    items: list[Item] = []
    for index, raw in enumerate(data, 1):
        if not isinstance(raw, dict):
            raise ExpectedBehaviorError(f"item {index} is not a mapping")
        item = _parse_item(raw, index)
        if any(existing.id == item.id for existing in items):
            raise ExpectedBehaviorError(f"{item.id} appears twice")
        items.append(item)
    api_ids = {item.id for item in items if item.type == "api"}
    for item in items:
        if item.type == "log" and item.after not in api_ids:
            raise ExpectedBehaviorError(f"{item.id}: `after` must name an api item")
    return items


def _parse_item(raw: dict[str, Any], index: int) -> Item:
    item_id = str(raw.get("id", ""))
    if not _ID.match(item_id):
        raise ExpectedBehaviorError(f"item {index}: id must look like B1")
    covers = raw.get("covers")
    if not isinstance(covers, list) or not covers or not all(_REQ.match(str(c)) for c in covers):
        raise ExpectedBehaviorError(f"{item_id}: covers must list requirement ids such as [R1]")
    kind = raw.get("type")
    expect = raw.get("expect") or {}
    if not isinstance(expect, dict):
        raise ExpectedBehaviorError(f"{item_id}: expect must be a mapping")
    base = {"id": item_id, "type": kind, "covers": tuple(str(c) for c in covers)}

    if kind == "api":
        method, path = _parse_request(item_id, raw.get("request"))
        headers = raw.get("headers") or {}
        if not isinstance(headers, dict):
            raise ExpectedBehaviorError(f"{item_id}: headers must be a mapping")
        headers = {str(k): str(v) for k, v in headers.items() if str(k).lower() in SAFE_HEADERS}
        if not isinstance(expect.get("status"), int):
            raise ExpectedBehaviorError(f"{item_id}: expect.status must be a number")
        for key in ("body_has", "body_not_has"):
            value = expect.get(key, [])
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ExpectedBehaviorError(f"{item_id}: expect.{key} must be a list of names")
        if "body_includes" in expect and not isinstance(expect["body_includes"], dict):
            raise ExpectedBehaviorError(f"{item_id}: expect.body_includes must be a mapping")
        return Item(**base, method=method, path=path, headers=headers, body=raw.get("body"), expect=expect)

    if kind == "log":
        level = expect.get("no_level")
        message = expect.get("has_message")
        if level is None and message is None:
            raise ExpectedBehaviorError(f"{item_id}: expect needs no_level or has_message")
        if level is not None and str(level).upper() not in LOG_LEVELS:
            raise ExpectedBehaviorError(f"{item_id}: unknown log level {level}")
        return Item(**base, after=str(raw.get("after", "")), expect=expect)

    if kind == "ui":
        step = raw.get("step")
        if not isinstance(step, str) or not step.strip():
            raise ExpectedBehaviorError(f"{item_id}: ui items need a step")
        page = raw.get("page")
        if page is not None and not (isinstance(page, str) and _PATH.match(page)):
            raise ExpectedBehaviorError(f"{item_id}: page must be a path such as /item.html?id=1")
        return Item(**base, step=step.strip(), page=page)

    raise ExpectedBehaviorError(f"{item_id}: type must be api, log or ui")


def _parse_request(item_id: str, request: Any) -> tuple[str, str]:
    parts = str(request or "").split()
    if len(parts) != 2:
        raise ExpectedBehaviorError(f"{item_id}: request must look like 'GET /items/42'")
    method, path = parts[0].upper(), parts[1]
    if method not in METHODS:
        raise ExpectedBehaviorError(f"{item_id}: method must be one of {', '.join(METHODS)}")
    if len(path) > 200 or not _PATH.match(path):
        raise ExpectedBehaviorError(f"{item_id}: the path must start with / and have no host")
    return method, path


def parse_body(body: str | None) -> dict[str, Any]:
    """Everything the review needs from a PR description, without raising."""
    block = extract_block(body)
    result: dict[str, Any] = {"missing": block is None, "error": None, "items": []}
    if block is not None:
        try:
            result["items"] = [item.to_dict() for item in parse_items(block)]
        except ExpectedBehaviorError as err:
            result["error"] = str(err)
    return result


def item_from_dict(data: dict[str, Any]) -> Item:
    """Rebuild an Item from `Item.to_dict()` output."""
    method = path = None
    if data["type"] == "api":
        method, path = data["request"].split(" ", 1)
    return Item(
        id=data["id"],
        type=data["type"],
        covers=tuple(data.get("covers", [])),
        method=method,
        path=path,
        headers=data.get("headers") or {},
        body=data.get("body"),
        expect=data.get("expect") or {},
        after=data.get("after"),
        step=data.get("step"),
        page=data.get("page"),
    )


# --- checking results -----------------------------------------------------------------


def has_path(obj: Any, dotted: str) -> bool:
    current = obj
    for part in dotted.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False
    return True


def includes(actual: Any, expected: Any) -> bool:
    """Deep subset match. Booleans never equal numbers here (True is not 1)."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and includes(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and all(any(includes(a, e) for a in actual) for e in expected)
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(actual) is type(expected) and actual == expected
    return actual == expected


def check_api(item: Item, status: int, body: Any) -> tuple[bool, str]:
    problems: list[str] = []
    expect = item.expect
    if status != expect.get("status"):
        problems.append(f"status {status}, expected {expect.get('status')}")
    problems += [f"`{key}` is missing" for key in expect.get("body_has", []) if not has_path(body, key)]
    problems += [f"`{key}` should not be there" for key in expect.get("body_not_has", []) if has_path(body, key)]
    if "body_includes" in expect and not includes(body, expect["body_includes"]):
        problems.append(f"body does not include {json.dumps(expect['body_includes'], ensure_ascii=False)}")
    if problems:
        return False, "; ".join(problems)
    checked = [f"status {status}"]
    if expect.get("body_has"):
        checked.append("has " + ", ".join(expect["body_has"]))
    if "body_includes" in expect:
        checked.append("body matches")
    return True, ", ".join(checked)


def check_log(item: Item, lines: list[dict[str, Any]]) -> tuple[bool | None, str]:
    """`None` means the logs could not be read, which is neither a pass nor a failure."""
    if not lines:
        return None, "no log lines found for the request"
    expect = item.expect
    if "no_level" in expect:
        threshold = LOG_LEVELS[str(expect["no_level"]).upper()]
        bad = [line for line in lines if LOG_LEVELS.get(str(line.get("level", "")).upper(), 0) >= threshold]
        if bad:
            return False, f"{len(bad)} line(s) at {expect['no_level']} or above: {bad[0].get('message')}"
    if "has_message" in expect and not any(
        str(expect["has_message"]) in str(line.get("message", "")) for line in lines
    ):
        return False, f"no log line contains {expect['has_message']!r}"
    return True, f"{len(lines)} log line(s) checked"
