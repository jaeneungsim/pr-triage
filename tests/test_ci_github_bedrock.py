import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pr_triage import bedrock, ci, github


def run(
    name,
    number,
    status="completed",
    conclusion="success",
    created="2026-10-11T08:00:00Z",
    attempt=1,
    event="pull_request",
):
    return {
        "name": name,
        "run_number": number,
        "run_attempt": attempt,
        "status": status,
        "conclusion": conclusion,
        "created_at": created,
        "event": event,
        "id": number * 10,
        "html_url": f"https://runs/{number}",
    }


def test_latest_run_per_workflow_wins():
    runs = [
        run("ci", 1, conclusion="failure"),
        run("ci", 2),
        run("ci", 2, attempt=2, conclusion="cancelled"),
        run("behavior", 3),
        run("ci", 9, event="push"),
        run("lint", 4),
    ]
    latest = ci.latest_runs(runs, ["ci", "behavior", "preview-deploy"])
    assert latest["ci"]["conclusion"] == "cancelled"
    assert latest["behavior"]["run_number"] == 3
    assert latest["preview-deploy"] is None
    assert ci.summarize(latest)["complete"] is False


def test_not_before_ignores_runs_from_before_an_edit():
    runs = [run("behavior", 1, created="2026-10-11T08:00:00Z")]
    assert ci.latest_runs(runs, ["behavior"], {"behavior": "2026-10-11T09:00:00Z"})["behavior"] is None
    runs.append(run("behavior", 2, status="in_progress", conclusion=None, created="2026-10-11T09:00:05Z"))
    summary = ci.summarize(ci.latest_runs(runs, ["behavior"], {"behavior": "2026-10-11T09:00:00Z"}))
    assert summary["complete"] is False and summary["workflows"]["behavior"]["run_id"] == 20


def test_skipped_counts_as_complete():
    assert ci.summarize(ci.latest_runs([run("preview-deploy", 1, conclusion="skipped")], ["preview-deploy"]))[
        "complete"
    ]


def test_app_jwt_is_rs256_and_short_lived():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()
    now = int(time.time())
    token = github.app_jwt("12345", pem, now=now)
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"])
    assert claims["iss"] == "12345" and claims["exp"] - claims["iat"] <= 600


def test_next_link():
    link = '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=5>; rel="last"'
    assert github._next_link(link) == "https://api.github.com/x?page=2"
    assert github._next_link(None) is None


def test_download_does_not_forward_the_token(monkeypatch):
    calls = []

    def fake_send(req, *, follow=True, timeout=20):
        calls.append((req.full_url, dict(req.header_items()), follow))
        if len(calls) == 1:
            return 302, b"", {"location": "https://blob.example/artifact.zip?sig=x"}
        return 200, b"zipbytes", {}

    monkeypatch.setattr(github, "_send", fake_send)
    data = github.GitHubClient("tok").download("/repos/o/r/actions/artifacts/1/zip")
    assert data == b"zipbytes"
    assert calls[0][2] is False and "Authorization" in calls[0][1]
    assert "Authorization" not in calls[1][1]


def test_client_retries_server_errors_then_raises(monkeypatch):
    answers = [(502, b"{}", {}), (500, b'{"message": "boom"}', {})]
    monkeypatch.setattr(github, "_send", lambda req, **kw: answers.pop(0) if answers else (500, b"{}", {}))
    monkeypatch.setattr(github.time, "sleep", lambda s: None)
    with pytest.raises(github.TransientGitHubError):
        github.GitHubClient("tok").get("/x")


def test_client_404_is_not_transient(monkeypatch):
    monkeypatch.setattr(github, "_send", lambda req, **kw: (404, b'{"message": "Not Found"}', {}))
    with pytest.raises(github.GitHubError) as err:
        github.GitHubClient("tok").get("/x")
    assert err.value.status == 404 and not isinstance(err.value, github.TransientGitHubError)


def converse_response(content, stop="tool_use"):
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop,
        "usage": {"inputTokens": 12000, "outputTokens": 900},
        "metrics": {"latencyMs": 4100},
    }


def test_bedrock_parse_tool_answer(good_review):
    call = bedrock.parse(
        converse_response([{"toolUse": {"toolUseId": "1", "name": "submit_review", "input": good_review}}])
    )
    assert call.review == good_review and call.input_tokens == 12000 and call.latency_ms == 4100


def test_bedrock_without_tool_answer_keeps_usage():
    with pytest.raises(bedrock.ModelOutputError) as err:
        bedrock.parse(converse_response([{"text": "I think it is fine"}], stop="end_turn"))
    assert err.value.output_tokens == 900


def test_bedrock_request_forces_the_tool():
    req = bedrock.request("model", "sys", "user", {"type": "object"}, 3000)
    assert req["toolConfig"]["toolChoice"] == {"tool": {"name": "submit_review"}}
    assert req["inferenceConfig"]["temperature"] == 0
