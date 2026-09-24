"""Tests for unmentioned follow-up triage."""

from __future__ import annotations

from coach_bot.followup_triage import should_reply_to_followup


def test_ack_only_skips_without_llm():
    def decide(_state, _questions):
        raise AssertionError("decisions should not run for acknowledgements")

    assert should_reply_to_followup("thanks!", decide=decide) is False
    assert should_reply_to_followup("ok", decide=decide) is False
    assert should_reply_to_followup("yep", decide=decide) is False


def test_empty_text_skips():
    assert (
        should_reply_to_followup(
            "  ", decide=lambda *_a: {"should_reply": {"noul": 1}}
        )
        is False
    )


def test_noul_at_threshold_replies():
    def decide(state, questions):
        assert state["message"] == "how was that split?"
        assert "should_reply" in questions
        assert "why" in questions
        return {
            "should_reply": {"type": "noul", "noul": 0.8},
            "why": {"type": "choice", "choice": "question", "confidence": 0.7},
        }

    assert should_reply_to_followup("how was that split?", decide=decide) is True


def test_noul_below_threshold_skips():
    def decide(_state, _questions):
        return {"should_reply": {"type": "noul", "noul": 0.79}}

    assert should_reply_to_followup("erg tomorrow 7am?", decide=decide) is False


def test_missing_answers_skips():
    assert should_reply_to_followup("how was gym?", decide=lambda *_a: None) is False


def test_decide_exception_skips():
    def decide(_state, _questions):
        raise RuntimeError("api down")

    assert should_reply_to_followup("how was gym?", decide=decide) is False


def test_use_llm_false_skips():
    assert (
        should_reply_to_followup(
            "how was gym?",
            decide=lambda *_a: {"should_reply": {"noul": 1}},
            use_llm=False,
        )
        is False
    )


def test_missing_key_skips(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert should_reply_to_followup("how was gym?") is False
