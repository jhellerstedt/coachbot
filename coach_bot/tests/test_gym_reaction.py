"""Tests for thumbs-down gym log removal."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from coach_bot.config import CoachAthleteCfg
from coach_bot.handler import (
    CoachMessageHandler,
    _parse_logged_gym_session_id,
)
from generate_training_plan import (
    find_gym_log_by_id,
    format_gym_log_confirmation,
    save_gym_log_record,
)


def _athletes() -> list[CoachAthleteCfg]:
    return [
        CoachAthleteCfg(
            id=53603359,
            label="Jack H",
            zulip_user_id=12345,
        )
    ]


def _handler(tmp_path: Path) -> CoachMessageHandler:
    return CoachMessageHandler(
        cache_dir=tmp_path,
        bot_user_id=99,
        zulip_client=MagicMock(),
        athletes=_athletes(),
    )


def _bot_config_patch(handler: CoachMessageHandler):
    return patch(
        "coach_bot.handler.load_bot_config",
        return_value=(None, None, None, None, None, handler.athletes),
    )


def _sample_gym_record(**overrides) -> dict:
    record = {
        "id": "gym-log-abc",
        "athlete_id": 53603359,
        "athlete_label": "Jack H",
        "session_date": "2026-06-29",
        "zulip_message_id": 444001,
        "gym": {
            "total_tonnage_kg": 5090,
            "exercises": [
                {
                    "name": "Back squat",
                    "max_weight_kg": 100,
                    "tonnage_kg": 1880,
                }
            ],
        },
    }
    record.update(overrides)
    return record


def test_parse_logged_gym_session_id_strips_trailing_date():
    content = "**Logged gym session** (`gym-log-abc`, 2026-06-29)\n\n"
    assert _parse_logged_gym_session_id(content) == "gym-log-abc"


def test_format_gym_log_confirmation_includes_log_id():
    text = format_gym_log_confirmation(_sample_gym_record())
    assert "**Logged gym session** (`gym-log-abc`, 2026-06-29)" in text


def test_gym_log_without_rpe_prompts_follow_up():
    from gym_program import format_rpe_follow_up, gym_log_missing_rpe

    record = _sample_gym_record()
    record["gym"]["exercises"][0]["sets"] = [{"reps": 8, "weight_kg": 70}]
    assert gym_log_missing_rpe(record) is True
    assert "RPE" in format_rpe_follow_up()



def test_thumbs_down_deletes_gym_log_by_coach_reply_message(tmp_path):
    athlete_id = 53603359
    record = _sample_gym_record(coach_reply_zulip_message_id=555002)
    save_gym_log_record(tmp_path, athlete_id, record)

    handler = _handler(tmp_path)
    with _bot_config_patch(handler):
        reply = handler.handle_reaction(
            {
                "type": "reaction",
                "op": "add",
                "emoji_name": "-1",
                "user_id": 12345,
                "message_id": 555002,
            }
        )
    assert reply is not None
    assert "Removed the logged gym session" in reply
    assert find_gym_log_by_id(tmp_path, athlete_id, "gym-log-abc") is None


def test_thumbs_down_deletes_gym_log_by_athlete_message(tmp_path):
    athlete_id = 53603359
    record = _sample_gym_record(zulip_message_id=444001)
    save_gym_log_record(tmp_path, athlete_id, record)

    handler = _handler(tmp_path)
    with _bot_config_patch(handler):
        reply = handler.handle_reaction(
            {
                "type": "reaction",
                "op": "add",
                "emoji_name": "thumbs_down",
                "user_id": 12345,
                "message_id": 444001,
            }
        )
    assert reply is not None
    assert "gym-log-abc" in reply
    assert find_gym_log_by_id(tmp_path, athlete_id, "gym-log-abc") is None


def test_thumbs_down_parses_gym_log_id_from_coach_message(tmp_path):
    athlete_id = 53603359
    record = _sample_gym_record()
    save_gym_log_record(tmp_path, athlete_id, record)

    handler = _handler(tmp_path)
    handler.zulip_client.get_raw_message.return_value = {
        "result": "success",
        "message": {
            "sender_id": 99,
            "content": format_gym_log_confirmation(record),
        },
    }
    with _bot_config_patch(handler):
        reply = handler.handle_reaction(
            {
                "type": "reaction",
                "op": "add",
                "emoji_name": "-1",
                "user_id": 12345,
                "message_id": 777889,
            }
        )
    assert reply is not None
    assert "gym-log-abc" in reply
    assert find_gym_log_by_id(tmp_path, athlete_id, "gym-log-abc") is None
