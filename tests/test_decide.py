import copy

from pr_triage import decide

API = "jaeneungsim/demo-inventory-api"
EVIDENCE = {
    "test_relevance": {"verdict": "ok"},
    "behavior_ci": {"passed": True, "items": [{"id": "B1", "type": "api", "ok": True}]},
    "behavior_preview": {"items": [{"id": "B1", "type": "api", "ok": True}]},
}


def state(review=None, policy_hits=(), check_hits=(), category="code", fork=False, base="dev", level="v3"):
    s = {
        "ctx": {"repo": API, "fork": fork, "base_ref": base, "mode": "advisory", "level": level},
        "policy": {
            "hits": list(policy_hits),
            "category": category,
            "ticket_repos": ["demo-inventory-api"],
            "expected_types": ["api"],
        },
    }
    if check_hits is not None:
        s["checks"] = {"hits": list(check_hits), **copy.deepcopy(EVIDENCE)}
    if review is not None:
        s["review"] = {"status": "ok", "result": review}
    return s


def hit(kind, outcome):
    return {"kind": kind, "outcome": outcome, "message": kind, "source": "test"}


def test_approvable_needs_every_gate_and_the_ai(rules, good_review):
    result = decide.decide(state(good_review), rules, "advisory")
    assert result["decision"] == "approvable" and result["hits"] == [] and not result["auto_approve"]


def test_security_beats_fixable_problems(rules):
    s = state(policy_hits=[hit("ticket", "changes_requested"), hit("sensitive", "needs_human")])
    assert decide.decide(s, rules, "advisory")["decision"] == "needs_human"


def test_fixable_problems_beat_other_human_reasons(rules):
    s = state(check_hits=[hit("ci", "changes_requested"), hit("timeout", "needs_human")])
    assert decide.decide(s, rules, "advisory")["decision"] == "changes_requested"


def test_notes_do_not_block(rules, good_review):
    result = decide.decide(state(good_review, policy_hits=[hit("coverage", "note")]), rules, "advisory")
    assert result["decision"] == "approvable" and result["notes"][0]["kind"] == "coverage"


def test_ai_unavailable_goes_to_a_person(rules):
    s = state()
    s["review"] = {"status": "unavailable", "reason": "the daily limit is reached", "kind": "limit"}
    result = decide.decide(s, rules, "advisory")
    assert result["decision"] == "needs_human" and result["hits"][0]["kind"] == "limit"


def test_missing_review_is_never_approved(rules):
    assert decide.decide(state(), rules, "advisory")["decision"] == "needs_human"


def test_ai_suggestions(rules, good_review):
    changes = {
        **good_review,
        "suggestion": "changes_requested",
        "findings": [{"severity": "medium", "title": "stock is not returned", "category": "bug"}],
    }
    assert decide.decide(state(changes), rules, "advisory")["decision"] == "changes_requested"
    assert (
        decide.decide(state({**good_review, "suggestion": "needs_human"}), rules, "advisory")["decision"]
        == "needs_human"
    )
    low = decide.decide(state({**good_review, "confidence": 0.5}), rules, "advisory")
    assert low["decision"] == "needs_human" and low["hits"][0]["kind"] == "ai_low_confidence"


def test_ai_cannot_wave_through_its_own_high_finding(rules, good_review):
    review = {**good_review, "findings": [{"severity": "high", "title": "x", "category": "bug"}]}
    assert decide.decide(state(review), rules, "advisory")["decision"] == "needs_human"


def test_injection_finding_is_a_security_hit(rules, good_review):
    review = {
        **good_review,
        "suggestion": "changes_requested",
        "findings": [{"severity": "high", "title": "x", "category": "prompt_injection"}],
    }
    result = decide.decide(state(review), rules, "advisory")
    assert result["decision"] == "needs_human" and result["hits"][0]["kind"] == "injection"


def test_validation_failure_overrides_the_ai(rules, good_review):
    s = state(good_review)
    s["validation"] = {"critical": True, "problems": ["finding at a.py:9 removed"], "review": None}
    assert decide.decide(s, rules, "advisory")["decision"] == "needs_human"


def test_cleaned_review_is_used(rules, good_review):
    s = state({**good_review, "suggestion": "changes_requested"})
    s["validation"] = {"critical": False, "problems": [], "review": copy.deepcopy(good_review)}
    assert decide.decide(s, rules, "advisory")["decision"] == "approvable"


def auto(s, rules, mode="auto-approve"):
    result = decide.decide(s, rules, mode)
    return result["auto_approve"], result["auto_blocker"]


def test_auto_approve_needs_the_mode_and_a_clean_dev_pr(rules, good_review):
    assert auto(state(good_review, category="docs_only"), rules) == (True, None)
    assert auto(state(good_review), rules) == (True, None)
    assert auto(state(good_review), rules, mode="advisory") == (False, None)
    assert auto(state(good_review, fork=True), rules) == (False, "it comes from a fork")
    assert auto(state(good_review, base="main"), rules)[1].startswith("it targets main")


def test_code_needs_executed_evidence(rules, good_review):
    s = state(good_review, level="v1")
    assert auto(s, rules) == (False, "v1 does not run the code")
    s = state(good_review)
    s["checks"]["test_relevance"] = {"verdict": "undetermined"}
    assert auto(s, rules)[1] == "the new tests were not shown to fail on the old code"
    s = state(good_review)
    s["checks"]["behavior_ci"] = None
    assert auto(s, rules)[1] == "the expected behavior did not run in CI"
    s = state(good_review)
    s["checks"]["behavior_preview"] = None
    assert auto(s, rules)[1] == "the expected behavior did not run on the AWS preview"
    s = state(good_review, level="v2")
    s["checks"]["behavior_preview"] = None
    assert auto(s, rules) == (True, None)


def test_code_that_a_person_has_to_see_waits(rules, good_review):
    s = state(good_review)
    s["policy"]["ticket_repos"] = ["demo-inventory-api", "demo-inventory-web"]
    assert auto(s, rules)[1] == "the ticket spans several repos, so a person merges the parts together"
    s = state(good_review)
    s["policy"]["expected_types"] = ["api", "ui"]
    assert auto(s, rules)[1] == "the expected behavior has UI steps that a person has to look at"


def test_only_approvable_prs_are_auto_approved(rules, good_review):
    s = state(good_review, check_hits=[hit("behavior", "changes_requested")])
    result = decide.decide(s, rules, "auto-approve")
    assert result["decision"] == "changes_requested" and result["auto_approve"] is False
