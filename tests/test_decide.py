import copy

from pr_triage import decide


def state(review=None, policy_hits=(), check_hits=(), category="code", fork=False, base="dev"):
    s = {
        "ctx": {"fork": fork, "base_ref": base, "mode": "advisory"},
        "policy": {"hits": list(policy_hits), "category": category},
    }
    if check_hits is not None:
        s["checks"] = {"hits": list(check_hits)}
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


def test_auto_approve_only_for_docs_and_tests_into_dev(rules, good_review):
    assert decide.decide(state(good_review, category="docs_only"), rules, "auto-approve")["auto_approve"]
    assert not decide.decide(state(good_review), rules, "auto-approve")["auto_approve"]
    assert not decide.decide(state(good_review, category="tests_only", fork=True), rules, "auto-approve")[
        "auto_approve"
    ]
    assert not decide.decide(state(good_review, category="tests_only", base="main"), rules, "auto-approve")[
        "auto_approve"
    ]
    assert not decide.decide(state(good_review, category="docs_only"), rules, "advisory")["auto_approve"]
