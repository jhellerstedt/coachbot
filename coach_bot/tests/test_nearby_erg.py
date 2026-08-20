"""Tests for nearby-screenshot erg routing and gym-intent guardrails."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from coach_bot.config import CoachAthleteCfg
from coach_bot.erg_score import references_nearby_erg_screenshot
from coach_bot.handler import CoachMessageHandler
from generate_training_plan import (
    looks_like_erg_referral_not_gym_log,
    interpret_coach_message_with_kagi,
)


def _athletes() -> list[CoachAthleteCfg]:
    return [
        CoachAthleteCfg(
            id=53603359,
            label="James Merrett",
            zulip_user_id=12345,
        )
    ]


def _handler(tmp_path: Path) -> CoachMessageHandler:
    return CoachMessageHandler(
        cache_dir=tmp_path,
        bot_user_id=99,
        zulip_client=MagicMock(),
        kagi_token="test-token",
        zulip_stream="general",
        zulip_topic="project-640",
        athletes=_athletes(),
    )


def test_references_nearby_erg_screenshot():
    assert references_nearby_erg_screenshot("@coach see my erg in the image above")
    assert references_nearby_erg_screenshot("my erg today — screenshot above")
    assert not references_nearby_erg_screenshot("@coach gym today: 8r 40 back squat")


def test_looks_like_erg_referral_not_gym_log():
    assert looks_like_erg_referral_not_gym_log("see my erg in the image above")
    assert not looks_like_erg_referral_not_gym_log(
        "gym today: Back squat 8r 70, 8r 90, 6r 100"
    )


def test_handler_routes_image_above_to_prior_screenshot(tmp_path):
    handler = _handler(tmp_path)
    prior = {
        "id": 102864,
        "sender_id": 12345,
        "sender_email": "james@example.com",
        "sender_full_name": "James Merrett",
        "content": (
            "[image.png](/user_uploads/10/x/y/image.png)\n\nMy erg today @**coach**"
        ),
        "timestamp": 1000.0,
        "type": "stream",
        "display_recipient": "general",
        "subject": "project-640",
    }
    follow_up = {
        "id": 102865,
        "sender_id": 12345,
        "sender_email": "james@example.com",
        "sender_full_name": "James Merrett",
        "content": "@**coach** see my erg in the image above",
        "timestamp": 1240.0,
        "type": "stream",
        "display_recipient": "general",
        "subject": "project-640",
        "mentions": [99],
    }

    with patch(
        "coach_bot.handler.find_recent_sender_image_message",
        return_value=(prior, ["/user_uploads/10/x/y/image.png"]),
    ), patch.object(
        handler,
        "_handle_erg_score_screenshot",
        return_value="Logged erg session.",
    ) as mock_erg:
        reply = handler.handle(follow_up)

    assert reply == "Logged erg session."
    mock_erg.assert_called_once()
    args = mock_erg.call_args[0]
    assert args[0]["id"] == 102864
    assert "see my erg" in args[3]


def test_interpret_downgrades_erg_image_above_from_gym_log(tmp_path, monkeypatch):
    from generate_training_plan import WeeklyPlanRecord

    plan = WeeklyPlanRecord(
        week_id="2026-07-06_2026-07-12",
        week_start="2026-07-06",
        week_end="2026-07-12",
        plan_text="Tuesday erg threshold intervals.",
        generated_at="2026-07-06T00:00:00Z",
        training_summary="",
        include_lifting=True,
        plan_json=None,
    )

    def fake_call(*_args, **_kwargs):
        return (
            '{"intent":"gym_session_log","reply":"Nice plank.",'
            '"workout_text":"Plank 30s","pending_adjustment":null}'
        )

    monkeypatch.setattr("generate_training_plan._call_llm", fake_call)

    result = interpret_coach_message_with_kagi(
        "see my erg in the image above",
        plan,
        tmp_path,
        "token",
    )
    assert result.intent == "coaching_reply"
    assert result.workout_text is None
    assert "screenshot attached" in result.reply
