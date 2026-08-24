"""Shared gym-session credit when stream logs tag other athletes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from coach_bot.config import CoachAthleteCfg, resolve_gym_log_recipients
from coach_bot.handler import CoachMessageHandler
from generate_training_plan import (
    CoachInterpretation,
    GymExerciseMetrics,
    GymSessionMetrics,
    GymSetMetrics,
    WeeklyPlanRecord,
    find_gym_log_by_id,
    find_gym_log_by_zulip_message,
    load_gym_logs_for_athlete,
    record_gym_sessions_from_zulip_for_athletes,
    save_gym_log_record,
    apply_rpe_follow_up_from_zulip,
)


def _parsed_gym() -> GymSessionMetrics:
    return GymSessionMetrics(
        activity_id=0,
        activity_name="Gym (Zulip DM)",
        total_tonnage_kg=1500.0,
        exercises=[
            GymExerciseMetrics(
                name="Back squat",
                max_weight_kg=100.0,
                tonnage_kg=1500.0,
                sets=[GymSetMetrics(reps=5, weight_kg=100.0)],
            )
        ],
    )


def _athletes() -> list[CoachAthleteCfg]:
    return [
        CoachAthleteCfg(
            id=1,
            label="Jack H",
            zulip_email="jack@example.com",
            zulip_full_name="Jack H",
            zulip_user_id=101,
            body_weight_kg=82.0,
        ),
        CoachAthleteCfg(
            id=2,
            label="Sarah T",
            zulip_email="sarah@example.com",
            zulip_full_name="Sarah T",
            zulip_user_id=202,
            body_weight_kg=64.0,
        ),
        CoachAthleteCfg(
            id=3,
            label="Tom B",
            zulip_email="tom@example.com",
            zulip_full_name="Tom B",
            zulip_user_id=303,
        ),
    ]


def test_stream_gym_no_mentions_is_sender_only():
    jack, *_ = _athletes()
    recipients = resolve_gym_log_recipients(
        _athletes(),
        sender_email="jack@example.com",
        sender_id=101,
        message_content="@**coach|99** gym today: squat 5x5 100",
        bot_user_id=99,
    )
    assert [a.id for a in recipients] == [jack.id]


def test_stream_gym_mentions_include_sender_then_tagged():
    jack, sarah, tom = _athletes()
    recipients = resolve_gym_log_recipients(
        _athletes(),
        sender_email="jack@example.com",
        sender_id=101,
        message_content=(
            "@**coach|99** gym with @**Sarah T|202** and @**Tom B|303**: "
            "bench 3x5 60"
        ),
        bot_user_id=99,
    )
    assert [a.id for a in recipients] == [jack.id, sarah.id, tom.id]


def test_stream_self_mention_is_not_duplicated():
    jack, sarah, _ = _athletes()
    recipients = resolve_gym_log_recipients(
        _athletes(),
        sender_email="jack@example.com",
        sender_id=101,
        message_content="@**coach|99** @**Jack H|101** @**Sarah T|202** gym: 5x5",
        bot_user_id=99,
    )
    assert [a.id for a in recipients] == [jack.id, sarah.id]


def test_unmapped_sender_credits_mapped_mention_only():
    _, sarah, _ = _athletes()
    recipients = resolve_gym_log_recipients(
        _athletes(),
        sender_email="coach@example.com",
        sender_full_name="Head Coach",
        sender_id=999,
        message_content="@**coach|99** logging for @**Sarah T|202** gym: 3x8",
        bot_user_id=99,
    )
    assert [a.id for a in recipients] == [sarah.id]


def test_dm_ignores_mentions():
    jack, *_ = _athletes()
    recipients = resolve_gym_log_recipients(
        _athletes(),
        sender_email="jack@example.com",
        sender_id=101,
        message_content="gym with @**Sarah T|202**: 5x5",
        bot_user_id=99,
        private_dm=True,
    )
    assert [a.id for a in recipients] == [jack.id]


def test_unresolved_mention_is_skipped():
    jack, sarah, _ = _athletes()
    recipients = resolve_gym_log_recipients(
        _athletes(),
        sender_email="jack@example.com",
        sender_id=101,
        message_content="@**coach|99** @**Unknown|404** @**Sarah T|202** gym: 5x5",
        bot_user_id=99,
    )
    assert [a.id for a in recipients] == [jack.id, sarah.id]


def test_multi_athlete_zulip_gym_logs_share_payload(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_parse(*_args, **_kwargs):
        calls["n"] += 1
        return _parsed_gym()

    monkeypatch.setattr(
        "generate_training_plan.parse_gym_session_metrics", fake_parse
    )
    recorded = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    records = record_gym_sessions_from_zulip_for_athletes(
        tmp_path,
        [(1, "Jack H", 82.0), (2, "Sarah T", 64.0)],
        "Back squat 5x5 100",
        "token",
        zulip_message_id=44001,
        zulip_sender_email="jack@example.com",
        recorded_at=recorded,
        session_hint_date=recorded.date(),
    )
    assert calls["n"] == 1
    assert len(records) == 2
    assert records[0]["id"] != records[1]["id"]
    assert records[0]["gym"] == records[1]["gym"]
    assert records[0]["athlete_id"] == 1
    assert records[1]["athlete_id"] == 2
    assert records[0]["body_weight_kg"] == 82.0
    assert records[1]["body_weight_kg"] == 64.0
    assert len(load_gym_logs_for_athlete(tmp_path, 1)) == 1
    assert len(load_gym_logs_for_athlete(tmp_path, 2)) == 1


def test_multi_athlete_zulip_gym_logs_are_idempotent(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_parse(*_args, **_kwargs):
        calls["n"] += 1
        return _parsed_gym()

    monkeypatch.setattr(
        "generate_training_plan.parse_gym_session_metrics", fake_parse
    )
    kwargs = dict(
        cache_dir=tmp_path,
        recipients=[(1, "Jack H", 82.0), (2, "Sarah T", 64.0)],
        workout_text="Back squat 5x5 100",
        token="token",
        zulip_message_id=44001,
        zulip_sender_email="jack@example.com",
    )
    first = record_gym_sessions_from_zulip_for_athletes(**kwargs)
    second = record_gym_sessions_from_zulip_for_athletes(**kwargs)
    assert calls["n"] == 1
    assert [r["id"] for r in first] == [r["id"] for r in second]
    assert len(load_gym_logs_for_athlete(tmp_path, 1)) == 1
    assert find_gym_log_by_zulip_message(tmp_path, 1, 44001)["id"] == first[0]["id"]


def _handler(tmp_path) -> CoachMessageHandler:
    return CoachMessageHandler(
        cache_dir=tmp_path,
        bot_user_id=99,
        zulip_client=MagicMock(),
        kagi_token="token",
        zulip_stream="general",
        athletes=_athletes(),
    )


def _plan() -> WeeklyPlanRecord:
    return WeeklyPlanRecord(
        week_id="2026-08-24_2026-08-30",
        week_start="2026-08-24",
        week_end="2026-08-30",
        plan_text="Monday gym.",
        generated_at="2026-08-24T00:00:00Z",
        training_summary="",
        include_lifting=True,
    )


def _patch_gym_log_handler(monkeypatch, handler: CoachMessageHandler):
    monkeypatch.setattr(
        "coach_bot.handler.plan_for_date", lambda *_args, **_kwargs: _plan()
    )
    monkeypatch.setattr(
        "coach_bot.handler.load_bot_config",
        lambda: (None, None, None, None, None, handler.athletes),
    )
    monkeypatch.setattr(
        "coach_bot.handler.interpret_coach_message_with_kagi",
        lambda *_args, **_kwargs: CoachInterpretation(
            intent="gym_session_log",
            reply="Nice work.",
            workout_text="Back squat 5x5 100",
        ),
    )
    monkeypatch.setattr(
        "generate_training_plan.parse_gym_session_metrics",
        lambda *_args, **_kwargs: _parsed_gym(),
    )


def test_stream_gym_log_credits_sender_and_mentions(tmp_path, monkeypatch):
    handler = _handler(tmp_path)
    _patch_gym_log_handler(monkeypatch, handler)
    ref = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    message = {
        "id": 9001,
        "sender_id": 101,
        "sender_email": "jack@example.com",
        "sender_full_name": "Jack H",
        "content": (
            "@**coach|99** gym with @**Sarah T|202** and @**Tom B|303**: "
            "Back squat 5x5 100"
        ),
        "type": "stream",
        "timestamp": ref.timestamp(),
    }
    reply = handler._reply_kagi(
        "gym with Sarah and Tom: Back squat 5x5 100", ref, message
    )
    assert "Jack H" in reply
    assert "Sarah T" in reply
    assert "Tom B" in reply
    assert len(load_gym_logs_for_athlete(tmp_path, 1)) == 1
    assert len(load_gym_logs_for_athlete(tmp_path, 2)) == 1
    assert len(load_gym_logs_for_athlete(tmp_path, 3)) == 1
    pending = handler.consume_pending_gym_log()
    assert pending is not None
    assert {athlete_id for athlete_id, _log_id in pending} == {1, 2, 3}


def test_dm_gym_log_ignores_mentions(tmp_path, monkeypatch):
    handler = _handler(tmp_path)
    _patch_gym_log_handler(monkeypatch, handler)
    ref = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    message = {
        "id": 9002,
        "sender_id": 101,
        "sender_email": "jack@example.com",
        "sender_full_name": "Jack H",
        "content": "gym with @**Sarah T|202**: Back squat 5x5 100",
        "type": "private",
        "timestamp": ref.timestamp(),
    }
    reply = handler._reply_kagi(
        "gym with Sarah: Back squat 5x5 100",
        ref,
        message,
        private_dm=True,
    )
    assert "Sarah T" not in reply
    assert len(load_gym_logs_for_athlete(tmp_path, 1)) == 1
    assert load_gym_logs_for_athlete(tmp_path, 2) == []


def test_unmapped_sender_stream_gym_log_credits_mention(tmp_path, monkeypatch):
    handler = _handler(tmp_path)
    _patch_gym_log_handler(monkeypatch, handler)
    ref = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
    message = {
        "id": 9003,
        "sender_id": 999,
        "sender_email": "coach@example.com",
        "sender_full_name": "Head Coach",
        "content": "@**coach|99** logging for @**Sarah T|202** gym: Back squat 5x5 100",
        "type": "stream",
        "timestamp": ref.timestamp(),
    }
    reply = handler._reply_kagi(
        "logging for Sarah gym: Back squat 5x5 100", ref, message
    )
    assert "Sarah T" in reply
    assert load_gym_logs_for_athlete(tmp_path, 1) == []
    assert len(load_gym_logs_for_athlete(tmp_path, 2)) == 1


def _bot_config_patch(handler: CoachMessageHandler):
    return patch(
        "coach_bot.handler.load_bot_config",
        return_value=(None, None, None, None, None, handler.athletes),
    )


def _save_shared_gym_copies(tmp_path: Path, **fields) -> None:
    gym = {
        "total_tonnage_kg": 1500,
        "exercises": [{"name": "Back squat", "max_weight_kg": 100, "tonnage_kg": 1500}],
    }
    base = {
        "session_date": "2026-08-24",
        "zulip_message_id": 44001,
        "gym": gym,
    }
    base.update(fields)
    save_gym_log_record(
        tmp_path,
        1,
        {
            **base,
            "id": "gym-jack",
            "athlete_id": 1,
            "athlete_label": "Jack H",
        },
    )
    save_gym_log_record(
        tmp_path,
        2,
        {
            **base,
            "id": "gym-sarah",
            "athlete_id": 2,
            "athlete_label": "Sarah T",
        },
    )


def test_thumbs_down_deletes_only_reactor_copy(tmp_path):
    _save_shared_gym_copies(tmp_path, coach_reply_zulip_message_id=555010)
    handler = _handler(tmp_path)
    with _bot_config_patch(handler):
        reply = handler.handle_reaction(
            {
                "type": "reaction",
                "op": "add",
                "emoji_name": "-1",
                "user_id": 202,
                "message_id": 555010,
            }
        )
    assert reply is not None
    assert "gym-sarah" in reply
    assert find_gym_log_by_id(tmp_path, 2, "gym-sarah") is None
    assert find_gym_log_by_id(tmp_path, 1, "gym-jack") is not None


def test_thumbs_down_by_non_recipient_is_refused(tmp_path):
    _save_shared_gym_copies(tmp_path, coach_reply_zulip_message_id=555010)
    handler = _handler(tmp_path)
    handler.zulip_client.get_raw_message.return_value = {
        "result": "success",
        "message": {
            "sender_id": 99,
            "content": (
                "**Logged gym session** (`gym-jack`, 2026-08-24)\n\n"
                "**Logged gym session** (`gym-sarah`, 2026-08-24)\n"
            ),
        },
    }
    with _bot_config_patch(handler):
        reply = handler.handle_reaction(
            {
                "type": "reaction",
                "op": "add",
                "emoji_name": "-1",
                "user_id": 303,
                "message_id": 555010,
            }
        )
    assert reply is not None
    assert "Only the athlete who logged that session" in reply
    assert find_gym_log_by_id(tmp_path, 1, "gym-jack") is not None
    assert find_gym_log_by_id(tmp_path, 2, "gym-sarah") is not None


def _save_rpe_pending_copies(tmp_path: Path) -> None:
    recorded = datetime.now(timezone.utc).isoformat()
    gym = {
        "total_tonnage_kg": 1500,
        "exercises": [
            {
                "name": "Back squat",
                "max_weight_kg": 80,
                "tonnage_kg": 1500,
                "sets": [
                    {"reps": 8, "weight_kg": 40.0},
                    {"reps": 5, "weight_kg": 80.0},
                ],
            },
            {
                "name": "Plank",
                "sets": [{"reps": 1, "weight_kg": 0.0, "duration_sec": 60}],
            },
        ],
    }
    for athlete_id, log_id, label in (
        (1, "gym-jack", "Jack H"),
        (2, "gym-sarah", "Sarah T"),
    ):
        save_gym_log_record(
            tmp_path,
            athlete_id,
            {
                "id": log_id,
                "athlete_id": athlete_id,
                "athlete_label": label,
                "session_date": "2026-08-24",
                "recorded_at": recorded,
                "zulip_message_id": 105823,
                "zulip_sender_email": "jack@example.com",
                "gym": gym,
            },
        )


def test_rpe_follow_up_from_sender_updates_tagged_copies(tmp_path):
    _save_rpe_pending_copies(tmp_path)
    updated = apply_rpe_follow_up_from_zulip(
        tmp_path,
        1,
        6.0,
        sender_email="jack@example.com",
    )
    assert {r["id"] for r in updated} == {"gym-jack", "gym-sarah"}
    jack = find_gym_log_by_id(tmp_path, 1, "gym-jack")
    sarah = find_gym_log_by_id(tmp_path, 2, "gym-sarah")
    assert jack["gym"]["exercises"][0]["sets"][1]["rpe"] == 6.0
    assert sarah["gym"]["exercises"][0]["sets"][1]["rpe"] == 6.0


def test_rpe_follow_up_from_tagged_athlete_updates_only_their_copy(tmp_path):
    _save_rpe_pending_copies(tmp_path)
    updated = apply_rpe_follow_up_from_zulip(
        tmp_path,
        2,
        6.0,
        sender_email="sarah@example.com",
    )
    assert [r["id"] for r in updated] == ["gym-sarah"]
    jack = find_gym_log_by_id(tmp_path, 1, "gym-jack")
    sarah = find_gym_log_by_id(tmp_path, 2, "gym-sarah")
    assert jack["gym"]["exercises"][0]["sets"][1].get("rpe") is None
    assert sarah["gym"]["exercises"][0]["sets"][1]["rpe"] == 6.0


def test_handler_rpe_reply_does_not_call_llm(tmp_path):
    _save_rpe_pending_copies(tmp_path)
    handler = _handler(tmp_path)
    handler.kagi_token = ""
    with _bot_config_patch(handler), patch(
        "coach_bot.handler.interpret_coach_message_with_kagi"
    ) as interpret:
        reply = handler.handle(
            {
                "id": 105825,
                "sender_id": 101,
                "sender_email": "jack@example.com",
                "sender_full_name": "Jack H",
                "content": "@**coach|99** RPE 6",
                "mentions": [99],
                "type": "stream",
                "display_recipient": "general",
                "subject": "project-640",
                "timestamp": datetime.now(timezone.utc).timestamp(),
            }
        )
    interpret.assert_not_called()
    assert reply is not None
    assert "RPE 6" in reply
    assert "Jack H" in reply
    assert "Sarah T" in reply
    jack = find_gym_log_by_id(tmp_path, 1, "gym-jack")
    assert jack["gym"]["exercises"][0]["sets"][1]["rpe"] == 6.0
