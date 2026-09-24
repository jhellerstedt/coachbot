"""Tests for regex-then-Jev message intent classification."""

from __future__ import annotations

from coach_bot.intents import classify_message_intent


def test_plan_adjustment_regex_skips_jev():
    def decide(_state, _questions):
        raise AssertionError("regex plan adjustments must not call Jev")

    assert (
        classify_message_intent("reduce a bit next week", decide=decide)
        == "plan_adjustment"
    )


def test_question_regex_skips_jev():
    def decide(_state, _questions):
        raise AssertionError("regex questions must not call Jev")

    assert classify_message_intent("what should I do?", decide=decide) == "question"


def test_uncertain_text_uses_jev_choice():
    def decide(state, questions):
        assert state == {"message": "ease off a little"}
        assert questions["intent"]["type"] == "choice"
        return {
            "intent": {
                "type": "choice",
                "choice": "plan_adjustment",
                "confidence": 0.72,
            }
        }

    assert (
        classify_message_intent("ease off a little", decide=decide)
        == "plan_adjustment"
    )


def test_low_confidence_choice_is_other():
    def decide(_state, _questions):
        return {
            "intent": {
                "choice": "plan_adjustment",
                "confidence": 0.59,
            }
        }

    assert classify_message_intent("ease off a little", decide=decide) == "other"


def test_missing_key_is_other(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert classify_message_intent("ease off a little") == "other"
