"""Tests for thumbs-down erg log removal."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from coach_bot.config import CoachAthleteCfg
from coach_bot.handler import CoachMessageHandler, _parse_logged_erg_score_id, _reaction_user_id
from generate_training_plan import find_erg_score_by_id, save_erg_score_record


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


def test_reaction_user_id_from_top_level_event():
    assert _reaction_user_id({"user_id": 12345}) == 12345
    assert _reaction_user_id({"user": {"user_id": 99}}) == 99
    assert _reaction_user_id({}) is None


def test_parse_logged_erg_score_id_strips_trailing_date(tmp_path):
    content = "**Logged for Jack H** (`abc-123`, 2026-06-25)\n\n"
    assert _parse_logged_erg_score_id(content) == "abc-123"


def test_thumbs_down_deletes_by_coach_reply_message(tmp_path):
    athlete_id = 53603359
    record = {
        "id": "old-thursday",
        "coach_reply_zulip_message_id": 555001,
        "session_date": "2026-06-25",
        "metrics": {"distance_m": 1000},
    }
    save_erg_score_record(tmp_path, athlete_id, record)

    handler = _handler(tmp_path)
    with _bot_config_patch(handler):
        reply = handler.handle_reaction(
            {
                "type": "reaction",
                "op": "add",
                "emoji_name": "-1",
                "user_id": 12345,
                "message_id": 555001,
            }
        )
    assert reply is not None
    assert "Removed the logged erg session" in reply
    assert find_erg_score_by_id(tmp_path, athlete_id, "old-thursday") is None


def test_thumbs_down_parses_score_id_from_coach_message(tmp_path):
    athlete_id = 53603359
    record = {
        "id": "parsed-id",
        "session_date": "2026-06-25",
        "metrics": {"distance_m": 1000},
    }
    save_erg_score_record(tmp_path, athlete_id, record)

    handler = _handler(tmp_path)
    handler.zulip_client.get_raw_message.return_value = {
        "result": "success",
        "message": {
            "sender_id": 99,
            "content": "**Logged for Jack H** (`parsed-id`, 2026-06-25)",
        },
    }
    with _bot_config_patch(handler):
        reply = handler.handle_reaction(
            {
                "type": "reaction",
                "op": "add",
                "emoji_name": "thumbs_down",
                "user_id": 12345,
                "message_id": 777888,
            }
        )
    assert reply is not None
    assert "parsed-id" in reply
    assert find_erg_score_by_id(tmp_path, athlete_id, "parsed-id") is None


def test_save_supersedes_older_same_day_logs(tmp_path):
    athlete_id = 53603359
    save_erg_score_record(
        tmp_path,
        athlete_id,
        {
            "id": "first-attempt",
            "session_date": "2026-06-25",
            "recorded_at": "2026-06-25T10:00:00",
            "metrics": {"distance_m": 1000},
        },
    )
    save_erg_score_record(
        tmp_path,
        athlete_id,
        {
            "id": "second-attempt",
            "session_date": "2026-06-25",
            "recorded_at": "2026-06-25T18:00:00",
            "metrics": {"distance_m": 2000},
        },
    )
    assert find_erg_score_by_id(tmp_path, athlete_id, "first-attempt") is None
    assert find_erg_score_by_id(tmp_path, athlete_id, "second-attempt") is not None
