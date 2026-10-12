from pr_triage import decide, params
from pr_triage.handlers import killswitch, review


class FakeSSM:
    def __init__(self):
        self.calls = []

    def put_parameter(self, **kwargs):
        self.calls.append(kwargs)


def test_budget_alert_switches_ai_reviews_off(monkeypatch):
    ssm = FakeSSM()
    monkeypatch.setattr(killswitch.aws, "client", lambda name: ssm)
    event = {"Records": [{"Sns": {"Message": "AWS Budget Notification: actual cost is greater than $8"}}]}
    assert killswitch.handler(event, None) == {"enabled": "off"}
    assert ssm.calls == [{"Name": "/pr-triage/config/enabled", "Value": "off", "Type": "String", "Overwrite": True}]


def test_switched_off_reviews_go_to_a_person(monkeypatch, rules):
    off = params.parse_config({"enabled": "off", "level": "v3", "mode": "auto-approve", "model": '{"id": "m"}'})
    monkeypatch.setattr(review.params, "load_config", lambda: off)
    state = {
        "ctx": {"repo": "jaeneungsim/demo-inventory-api", "fork": False, "base_ref": "dev", "level": "v3"},
        "policy": {"ai_allowed": True, "ai_blocked_reason": None, "hits": [], "category": "code"},
        "checks": {"hits": []},
    }
    result = review.handler({"state": state}, None)
    assert result["status"] == "unavailable" and result["reason"] == "AI reviews are switched off"
    state["review"] = result
    decision = decide.decide(state, rules, "auto-approve")
    assert decision["decision"] == "needs_human" and decision["auto_approve"] is False
