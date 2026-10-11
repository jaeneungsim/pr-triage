import pytest

from pr_triage import expected
from pr_triage.expected import ExpectedBehaviorError
from tests.conftest import PR_BODY


def items(text):
    return expected.parse_items(text)


def test_extract_block_needs_the_exact_info_string():
    assert expected.extract_block(PR_BODY).startswith("- id: B1")
    assert expected.extract_block("```yaml\n- id: B1\n```") is None
    assert expected.extract_block(PR_BODY.replace("\n", "\r\n")) is not None


def test_parse_body_reports_missing_and_errors():
    assert expected.parse_body("no block")["missing"] is True
    broken = expected.parse_body("```yaml expected-behavior\n- id: X\n```")
    assert broken["missing"] is False and "id must look like B1" in broken["error"]
    good = expected.parse_body(PR_BODY)
    assert good["error"] is None and [i["id"] for i in good["items"]] == ["B1", "B2"]


def test_pr_type():
    assert expected.pr_type(PR_BODY) == "feature"
    assert expected.pr_type("## Type\n<!-- pick one -->\n**bugfix**\n") == "bugfix"
    assert expected.pr_type("## Type\nsomething else") is None
    assert expected.pr_type("no sections") is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("- id: B1\n  covers: [R1]\n  type: api\n  request: GET //evil.example/x\n  expect: {status: 200}", "no host"),
        (
            "- id: B1\n  covers: [R1]\n  type: api\n  request: GET https://evil.example/x\n  expect: {status: 200}",
            "no host",
        ),
        ("- id: B1\n  covers: [R1]\n  type: api\n  request: TRACE /items\n  expect: {status: 200}", "method"),
        ("- id: B1\n  covers: [R1]\n  type: api\n  request: GET /items\n  expect: {}", "status"),
        ("- id: B1\n  covers: []\n  type: ui\n  step: look", "covers"),
        ("- id: B1\n  covers: [R1]\n  type: log\n  after: B9\n  expect: {no_level: ERROR}", "after"),
        ("- id: B1\n  covers: [R1]\n  type: ui\n  step: a\n- id: B1\n  covers: [R1]\n  type: ui\n  step: b", "twice"),
        ("key: value", "list"),
        ("- [unclosed", "valid YAML"),
    ],
)
def test_invalid_items(text, message):
    with pytest.raises(ExpectedBehaviorError, match=message):
        items(text)


def test_only_safe_headers_are_kept():
    [item] = items(
        "- id: B1\n  covers: [R1]\n  type: api\n  request: GET /items\n"
        "  headers: {Authorization: secret, Accept: application/json}\n  expect: {status: 200}"
    )
    assert item.headers == {"Accept": "application/json"}


def test_round_trip():
    parsed = items(expected.extract_block(PR_BODY))
    assert [expected.item_from_dict(i.to_dict()) for i in parsed] == parsed


def api_item(**expect):
    return expected.Item(
        id="B1", type="api", covers=("R1",), method="GET", path="/items/42", expect={"status": 200, **expect}
    )


def test_check_api():
    assert expected.check_api(api_item(body_has=["stock"]), 200, {"stock": 5})[0] is True
    ok, detail = expected.check_api(api_item(body_has=["stock"]), 200, {"id": "42"})
    assert not ok and "`stock` is missing" in detail
    ok, detail = expected.check_api(api_item(), 404, {"error": "not_found"})
    assert not ok and "status 404" in detail
    assert expected.check_api(api_item(body_not_has=["stock"]), 200, {"stock": 1})[0] is False


def test_body_includes_is_a_deep_subset_and_strict_about_booleans():
    item = api_item(body_includes={"sold_out": True, "item": {"id": "43"}})
    assert expected.check_api(item, 200, {"sold_out": True, "item": {"id": "43", "x": 1}})[0]
    assert not expected.check_api(item, 200, {"sold_out": 1, "item": {"id": "43"}})[0]
    assert expected.includes([{"a": 1}, {"b": 2}], [{"b": 2}])
    assert expected.has_path({"a": [{"b": 1}]}, "a.0.b")


def test_check_log():
    item = expected.Item(id="B2", type="log", covers=("R1",), after="B1", expect={"no_level": "ERROR"})
    assert expected.check_log(item, [])[0] is None
    assert expected.check_log(item, [{"level": "INFO", "message": "ok"}])[0] is True
    ok, detail = expected.check_log(item, [{"level": "ERROR", "message": "boom"}])
    assert ok is False and "boom" in detail
    with_message = expected.Item(id="B3", type="log", covers=("R1",), after="B1", expect={"has_message": "stock"})
    assert expected.check_log(with_message, [{"level": "INFO", "message": "read stock"}])[0] is True
