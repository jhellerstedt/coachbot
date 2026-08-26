"""Tests for unmentioned follow-up triage."""

from __future__ import annotations

from coach_bot.followup_triage import should_reply_to_followup


def test_ack_only_skips_without_llm():
    calls = {"n": 0}

    def llm_call(_system: str, _user: str) -> str:
        calls["n"] += 1
        return '{"should_reply": true}'

    assert should_reply_to_followup("thanks!", llm_call=llm_call) is False
    assert should_reply_to_followup("ok", llm_call=llm_call) is False
    assert should_reply_to_followup("yep", llm_call=llm_call) is False
    assert calls["n"] == 0


def test_empty_text_skips():
    assert should_reply_to_followup("  ", llm_call=lambda *_a: '{"should_reply": true}') is False


def test_llm_should_reply_true():
    def llm_call(_system: str, _user: str) -> str:
        return '{"should_reply": true, "reason": "asked a question"}'

    assert should_reply_to_followup("how was that split?", llm_call=llm_call) is True


def test_llm_should_reply_false():
    def llm_call(_system: str, _user: str) -> str:
        return '{"should_reply": false, "reason": "squad chatter"}'

    assert should_reply_to_followup("erg tomorrow 7am?", llm_call=llm_call) is False


def test_malformed_json_skips():
    def llm_call(_system: str, _user: str) -> str:
        return "not json"

    assert should_reply_to_followup("how was gym?", llm_call=llm_call) is False


def test_llm_exception_skips():
    def llm_call(_system: str, _user: str) -> str:
        raise RuntimeError("api down")

    assert should_reply_to_followup("how was gym?", llm_call=llm_call) is False


def test_use_llm_false_skips():
    assert (
        should_reply_to_followup(
            "how was gym?", llm_call=lambda *_a: "x", use_llm=False
        )
        is False
    )
