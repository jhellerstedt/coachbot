"""Coach bot behaviour while maintenance mode is on."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from coach_bot.config import CoachAthleteCfg
from coach_bot.handler import CoachMessageHandler
from generate_training_plan import CoachInterpretation
from maintenance_mode import PAUSED_WEEKLY_PLANS, SYNCING_NOTICE


def _athlete() -> CoachAthleteCfg:
    return CoachAthleteCfg(
        id=7,
        label="Ada",
        zulip_email="ada@example.com",
        zulip_user_id=5,
        max_hr_bpm=185,
    )


def _handler(tmp_path: Path, client=None) -> CoachMessageHandler:
    return CoachMessageHandler(
        cache_dir=tmp_path,
        bot_user_id=99,
        zulip_client=client,
        kagi_token="test-key",
        athletes=[_athlete()],
    )


def _config(handler: CoachMessageHandler):
    return patch(
        "coach_bot.handler.load_bot_config",
        return_value=(None, None, None, None, None, handler.athletes),
    )


def _message(content: str) -> dict:
    return {
        "type": "private",
        "id": 42,
        "sender_id": 5,
        "sender_email": "ada@example.com",
        "sender_full_name": "Ada",
        "content": content,
        "timestamp": datetime(2026, 10, 10, tzinfo=timezone.utc).timestamp(),
    }


def test_plan_adjustment_is_not_queued(tmp_path: Path):
    handler = _handler(tmp_path)
    with _config(handler), patch(
        "coach_bot.handler.config_maintenance_enabled", return_value=True
    ):
        reply = handler.handle(_message("reduce next week's volume"))
    assert reply == PAUSED_WEEKLY_PLANS
    pending = tmp_path / "plan_adjustments" / "pending.jsonl"
    assert not pending.exists()


def test_question_does_not_require_a_squad_plan(tmp_path: Path):
    handler = _handler(tmp_path)
    seen = {}

    def interpret(*_args, **_kwargs):
        seen["called"] = True
        return CoachInterpretation(intent="coaching_reply", reply="Rest was fine.")

    ref = datetime(2026, 10, 10, tzinfo=timezone.utc)
    with _config(handler), patch(
        "coach_bot.handler.config_maintenance_enabled", return_value=True
    ), patch(
        "coach_bot.handler.plan_for_date", return_value=None
    ), patch(
        "coach_bot.handler.interpret_coach_message_with_kagi", interpret
    ):
        reply = handler._reply_kagi(
            "why was my heart rate high yesterday?",
            ref,
            _message("why was my heart rate high yesterday?"),
            private_dm=True,
        )
    assert seen.get("called") is True
    assert "No cached weekly plan" not in reply
    assert "Rest was fine." in reply


def test_complete_request_posts_syncing_notice(tmp_path: Path):
    client = MagicMock()
    handler = _handler(tmp_path, client)
    with _config(handler), patch(
        "coach_bot.handler.config_maintenance_enabled", return_value=True
    ), patch(
        "coach_bot.handler.fulfill_session_request", return_value="the workout"
    ):
        reply = handler.handle(_message("gym session, leg day, 45min"))
    assert reply == "the workout"
    payload = client.send_message.call_args[0][0]
    assert payload["content"] == SYNCING_NOTICE
    assert payload["type"] == "private"
