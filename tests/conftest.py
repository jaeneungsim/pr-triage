from __future__ import annotations

import copy
from typing import Any

import pytest

from pr_triage import policy as policy_module

TICKET_TEXT = """---
key: INV-123
title: Show the stock quantity on the item detail page
status: in-progress
repos: [demo-inventory-api, demo-inventory-web]
---
## Requirements
- R1: The item API response includes the stock quantity (`stock`).
- R2: The item detail page shows the stock quantity.
"""

PR_BODY = """## Ticket
INV-123

## Type
<!-- one of: feature, bugfix, refactor, tests, docs -->
feature

## Summary
Return stock from GET /items/{id}.

## Expected behavior
```yaml expected-behavior
- id: B1
  covers: [R1]
  type: api
  request: GET /items/42
  expect:
    status: 200
    body_has: [stock]
- id: B2
  covers: [R1]
  type: log
  after: B1
  expect:
    no_level: ERROR
```
"""

OPENAPI = """openapi: 3.0.3
info: {title: Inventory, version: "1"}
paths:
  /health:
    get: {responses: {"200": {description: ok}}}
  /items:
    get: {responses: {"200": {description: ok}}}
    post: {responses: {"201": {description: created}}}
  /items/{id}:
    get: {responses: {"200": {description: ok}}}
"""

HANDLER = "\n".join(f"line {i}" for i in range(1, 61))


@pytest.fixture
def rules() -> dict[str, Any]:
    return copy.deepcopy(policy_module.load())


@pytest.fixture
def doc() -> dict[str, Any]:
    """A context document as handlers/context.py stores it."""
    from pr_triage import expected

    return {
        "repo": "jaeneungsim/demo-inventory-api",
        "pr": 41,
        "sha": "a" * 40,
        "url": "https://github.com/jaeneungsim/demo-inventory-api/pull/41",
        "title": "INV-123 Add stock to the item response",
        "body": PR_BODY,
        "author": "jaeneungsim",
        "base_ref": "dev",
        "head_ref": "INV-123-stock-in-item",
        "fork": False,
        "labels": [],
        "files": [
            {
                "filename": "src/inventory/handler.py",
                "status": "modified",
                "additions": 3,
                "deletions": 1,
                "patch": "@@ -10,3 +10,5 @@\n-    return item\n+    item['stock'] = row['stock']\n+    return item",
            },
            {
                "filename": "tests/test_items.py",
                "status": "modified",
                "additions": 6,
                "deletions": 0,
                "patch": "@@ -1,1 +1,7 @@\n+def test_item_has_stock():\n+    assert 'stock' in get_item('42')",
            },
        ],
        "contents": {"src/inventory/handler.py": HANDLER, "tests/test_items.py": "x\n" * 20},
        "ticket_key": "INV-123",
        "ticket": {
            "key": "INV-123",
            "title": "Show the stock quantity on the item detail page",
            "status": "in-progress",
            "repos": ["demo-inventory-api", "demo-inventory-web"],
            "requirements": {
                "R1": "The item API response includes the stock quantity (`stock`).",
                "R2": "The item detail page shows the stock quantity.",
            },
        },
        "ticket_error": None,
        "expected": expected.parse_body(PR_BODY),
        "pr_type": "feature",
        "siblings": [],
        "openapi": OPENAPI,
        "ai_ok_sha": None,
    }


@pytest.fixture
def good_review() -> dict[str, Any]:
    return {
        "suggestion": "approve",
        "confidence": 0.9,
        "summary": "Adds stock to the item response as the ticket asks.",
        "findings": [],
        "behavior_coverage": [{"id": "B1", "status": "implemented"}, {"id": "B2", "status": "implemented"}],
        "ticket_alignment": [
            {"requirement": "R1", "status": "met"},
            {"requirement": "R2", "status": "not_applicable"},
        ],
        "cross_repo_issues": [],
        "verification_steps": [
            {"kind": "api", "action": "Call GET /items/42", "expected": "200 with stock"},
            {"kind": "log", "action": "Search /aws/lambda/inv-api-preview-inv-123-handler", "expected": "no ERROR"},
        ],
    }
