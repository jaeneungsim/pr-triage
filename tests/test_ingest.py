import hashlib
import hmac
import json

from pr_triage.handlers import ingest

REPOS = {"jaeneungsim/demo-inventory-api", "jaeneungsim/demo-inventory-web"}


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def pr_payload(action="opened", **extra):
    payload = {
        "action": action,
        "repository": {"full_name": "jaeneungsim/demo-inventory-api"},
        "pull_request": {
            "number": 41,
            "title": "INV-123 Add stock",
            "html_url": "https://example/41",
            "head": {"sha": "b" * 40, "ref": "INV-123-stock"},
            "updated_at": "2026-10-11T08:00:00Z",
            "draft": False,
            "merged": False,
        },
    }
    payload.update(extra)
    return payload


def test_signature():
    body = b'{"a":1}'
    assert ingest.verify_signature("s3cret", body, sign("s3cret", body))
    assert not ingest.verify_signature("s3cret", body, sign("other", body))
    assert not ingest.verify_signature("s3cret", body, None)
    assert not ingest.verify_signature("s3cret", body, "sha1=abc")


def test_review_events():
    message = ingest.route("pull_request", pr_payload(), "d1", REPOS)
    assert message["type"] == "review" and message["ticket"] == "INV-123" and message["sha"] == "b" * 40
    assert ingest.route("pull_request", pr_payload("synchronize"), "d2", REPOS)["reason"] == "synchronize"


def test_edits_only_count_when_text_changed():
    assert ingest.route("pull_request", pr_payload("edited", changes={"base": {}}), "d", REPOS) is None
    assert ingest.route("pull_request", pr_payload("edited", changes={"body": {}}), "d", REPOS)["reason"] == "edited"


def test_drafts_wait_until_ready():
    draft = pr_payload()
    draft["pull_request"]["draft"] = True
    assert ingest.route("pull_request", draft, "d", REPOS) is None
    ready = pr_payload("ready_for_review")
    ready["pull_request"]["draft"] = False
    assert ingest.route("pull_request", ready, "d", REPOS)["reason"] == "ready_for_review"


def test_fork_label_and_close():
    labeled = pr_payload("labeled", label={"name": "ai-review-ok"})
    assert ingest.route("pull_request", labeled, "d", REPOS)["reason"] == "ai-review-ok"
    assert ingest.route("pull_request", pr_payload("labeled", label={"name": "bug"}), "d", REPOS) is None
    closed = ingest.route("pull_request", pr_payload("closed"), "d", REPOS)
    assert closed == {
        "type": "closed",
        "repo": "jaeneungsim/demo-inventory-api",
        "pr": 41,
        "ticket": "INV-123",
        "merged": False,
        "delivery": "d",
    }


def test_workflow_run_and_unknown_repos():
    payload = {
        "action": "completed",
        "repository": {"full_name": "jaeneungsim/demo-inventory-api"},
        "workflow_run": {
            "head_sha": "c" * 40,
            "name": "ci",
            "conclusion": "success",
            "id": 5,
            "event": "pull_request",
            "head_branch": "INV-1-x",
        },
    }
    assert ingest.route("workflow_run", payload, "d", REPOS)["workflow"] == "ci"
    payload["repository"]["full_name"] = "someone/else"
    assert ingest.route("workflow_run", payload, "d", REPOS) is None
    assert ingest.route("ping", {"repository": {"full_name": "jaeneungsim/demo-inventory-api"}}, "d", REPOS) is None


class FakeSqs:
    def __init__(self):
        self.sent = []

    def send_message(self, QueueUrl, MessageBody):
        self.sent.append(json.loads(MessageBody))


def test_handler_end_to_end(monkeypatch):
    body = json.dumps(pr_payload()).encode()
    sqs = FakeSqs()
    claimed = set()
    monkeypatch.setenv("QUEUE_URL", "https://sqs.example/q")
    monkeypatch.setattr(ingest.params, "secret", lambda name: "s3cret")
    monkeypatch.setattr(ingest.aws, "client", lambda name: sqs)
    monkeypatch.setattr(ingest.store, "claim_delivery", lambda d: not (d in claimed or claimed.add(d)))
    event = {
        "headers": {
            "X-Hub-Signature-256": sign("s3cret", body),
            "X-GitHub-Delivery": "d1",
            "X-GitHub-Event": "pull_request",
        },
        "body": body.decode(),
    }
    assert ingest.handler(event, None)["statusCode"] == 202
    assert ingest.handler(event, None)["statusCode"] == 200  # duplicate delivery
    assert len(sqs.sent) == 1
    event["headers"]["X-Hub-Signature-256"] = sign("wrong", body)
    assert ingest.handler(event, None)["statusCode"] == 401
