from datetime import UTC, datetime, timedelta

from pr_triage import expected, params, runner
from pr_triage.handlers import cleanup, digest
from tests.conftest import PR_BODY

ITEMS = expected.parse_items(expected.extract_block(PR_BODY))


def test_run_api_and_log_items():
    calls = []

    def http(base, item):
        calls.append(base + item.path)
        return 200, {"id": "42", "stock": 5}, "req-1", None

    def logs(group, request_id, since):
        assert (group, request_id) == ("/aws/lambda/inv-api-preview-inv-123-handler", "req-1")
        return [{"level": "INFO", "request_id": "req-1", "message": "GET /items/{id}"}]

    result = runner.run(
        ITEMS, "https://api.example/", "/aws/lambda/inv-api-preview-inv-123-handler", http=http, logs=logs
    )
    assert result["passed"] and [i["ok"] for i in result["items"]] == [True, True]
    assert calls == ["https://api.example//items/42"]  # the fake gets the base URL as given


def test_run_reports_failures_and_unreadable_logs():
    result = runner.run(
        ITEMS, "https://api", "/g", http=lambda b, i: (200, {"id": "42"}, "r", None), logs=lambda g, r, s: []
    )
    assert result["passed"] is False
    assert result["items"][0]["ok"] is False and result["items"][1]["ok"] is None


def test_read_only_skips_writes():
    [post] = expected.parse_items(
        "- id: B1\n  covers: [R1]\n  type: api\n  request: POST /items\n  expect: {status: 201}"
    )
    result = runner.run(
        [post], "https://api", None, read_only=True, http=lambda b, i: (_ for _ in ()).throw(AssertionError)
    )
    assert result["items"][0]["ok"] is None and result["passed"]


def test_network_errors_fail_the_item():
    result = runner.run(ITEMS[:1], "https://api", None, http=lambda b, i: (None, None, None, "timed out"))
    assert result["items"][0]["detail"] == "request failed: timed out" and not result["passed"]


def test_config_parsing_fails_safe():
    good = params.parse_config(
        {
            "enabled": "on",
            "level": "v3",
            "mode": "advisory",
            "limits": '{"daily_reviews": 3}',
            "model": '{"id": "m", "input_usd_per_mtok": 1, "output_usd_per_mtok": 5}',
            "allowed-authors": '["me"]',
        }
    )
    assert good.enabled and good.limits.daily_reviews == 3 and good.allowed_authors == ("me",)
    for broken in ({"level": "v9"}, {"limits": '{"unknown": 1}'}, {"model": "not json"}, {"model": "{}"}):
        cfg = params.parse_config(
            {"enabled": "on", "level": "v3", "mode": "advisory", "model": '{"id": "m"}', **broken}
        )
        assert cfg.enabled is False and cfg.level == "v1"


def test_digest_lists_people_work_first():
    now = datetime(2026, 10, 12, 8, tzinfo=UTC)
    runs = [
        {
            "repo": "o/demo-inventory-api",
            "pr": 1,
            "ticket": "INV-1",
            "decision": "approvable",
            "finished_at": "a",
            "cost_usd": 0.02,
        },
        {
            "repo": "o/demo-inventory-api",
            "pr": 2,
            "ticket": "INV-2",
            "decision": "changes_requested",
            "finished_at": "a",
        },
        {
            "repo": "o/demo-inventory-api",
            "pr": 2,
            "ticket": "INV-2",
            "decision": "needs_human",
            "finished_at": "b",
            "cost_usd": 0.01,
        },
    ]
    body = digest.build(runs, now)
    lines = [line for line in body.splitlines() if line.startswith("| demo")]
    assert lines[0].startswith("| demo-inventory-api#2") and "needs human review" in lines[0]
    assert "1 PR(s) wait for a person" in body and "$0.03" in body
    assert digest.build([], now) is None


def test_cleanup_rule():
    now = datetime(2026, 10, 12, tzinfo=UTC)
    assert cleanup.expired(now - timedelta(hours=25), "INV-1", {"INV-1"}, now, timedelta(hours=24))
    assert cleanup.expired(now - timedelta(hours=1), "INV-1", set(), now, timedelta(hours=24))
    assert not cleanup.expired(now - timedelta(hours=1), "INV-1", {"INV-1"}, now, timedelta(hours=24))
