"""Shared gym-session credit when stream logs tag other athletes."""

from __future__ import annotations

from datetime import datetime, timezone

from coach_bot.config import CoachAthleteCfg, resolve_gym_log_recipients
from generate_training_plan import (
    GymExerciseMetrics,
    GymSessionMetrics,
    GymSetMetrics,
    find_gym_log_by_zulip_message,
    load_gym_logs_for_athlete,
    record_gym_sessions_from_zulip_for_athletes,
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
