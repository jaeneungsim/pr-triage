import copy

from pr_triage import validate
from tests.conftest import OPENAPI

ROUTES = validate.openapi_routes(OPENAPI)
KW = {
    "changed_files": {"src/inventory/handler.py", "tests/test_items.py"},
    "line_counts": {"src/inventory/handler.py": 60},
    "expected_ids": {"B1", "B2"},
    "requirement_ids": {"R1", "R2"},
    "routes": ROUTES,
}


def finding(file="src/inventory/handler.py", line=12, severity="medium"):
    return {"severity": severity, "category": "bug", "file": file, "line": line, "title": "t", "evidence": "e"}


def test_routes():
    assert validate.route_exists(ROUTES, "GET", "/items/42")
    assert validate.route_exists(ROUTES, "GET", "/items/42?x=1")
    assert validate.route_exists(ROUTES, "POST", "/items")
    assert not validate.route_exists(ROUTES, "DELETE", "/items/42")
    assert not validate.route_exists(ROUTES, "GET", "/stock/42")
    assert validate.openapi_routes("not: [valid") is None
    assert validate.openapi_routes(None) is None


def test_good_review_passes(good_review):
    result = validate.validate_review(good_review, **KW)
    assert result["ok"] and not result["critical"]
    assert result["review"]["verification_steps"] == good_review["verification_steps"]


def test_schema_errors_are_critical(good_review):
    for broken in (
        {**good_review, "suggestion": "merge"},
        {**good_review, "confidence": 2},
        {k: v for k, v in good_review.items() if k != "findings"},
        "text",
    ):
        assert validate.validate_review(broken, **KW)["critical"]


def test_invented_locations_are_removed(good_review):
    review = copy.deepcopy(good_review)
    review["findings"] = [finding(), finding(file="src/made_up.py"), finding(line=500)]
    result = validate.validate_review(review, **KW)
    assert len(result["review"]["findings"]) == 1
    assert any("not part of this PR" in p for p in result["problems"])
    assert any("past the end" in p for p in result["problems"])
    assert not result["critical"]


def test_dropping_a_high_finding_is_critical(good_review):
    review = {**good_review, "findings": [finding(file="nope.py", severity="high")]}
    assert validate.validate_review(review, **KW)["critical"]


def test_changes_requested_with_only_invented_findings_is_critical(good_review):
    review = {**good_review, "suggestion": "changes_requested", "findings": [finding(file="nope.py")]}
    assert validate.validate_review(review, **KW)["critical"]


def test_unknown_ids_are_removed(good_review):
    review = copy.deepcopy(good_review)
    review["behavior_coverage"].append({"id": "B7", "status": "missing"})
    review["ticket_alignment"].append({"requirement": "R5", "status": "met"})
    result = validate.validate_review(review, **KW)
    assert [c["id"] for c in result["review"]["behavior_coverage"]] == ["B1", "B2"]
    assert [a["requirement"] for a in result["review"]["ticket_alignment"]] == ["R1", "R2"]


def test_verification_steps(good_review):
    review = copy.deepcopy(good_review)
    review["verification_steps"] += [
        {"kind": "api", "action": "Call DELETE /items/42", "expected": "204"},
        {"kind": "log", "action": "Search /aws/lambda/inventory-prod", "expected": "nothing"},
        {"kind": "ui", "action": "Open the item page", "expected": "stock shown"},
    ]
    result = validate.validate_review(review, **KW)
    kept = [s["kind"] for s in result["review"]["verification_steps"]]
    assert kept == ["api", "log", "ui"]
    assert not result["critical"]  # 3 of 5 kept


def test_mostly_invented_steps_are_critical(good_review):
    review = {
        **good_review,
        "verification_steps": [
            {"kind": "api", "action": "Call GET /orders", "expected": "200"},
            {"kind": "api", "action": "Call GET /carts", "expected": "200"},
            {"kind": "ui", "action": "Open the page", "expected": "ok"},
        ],
    }
    assert validate.validate_review(review, **KW)["critical"]
