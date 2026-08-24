"""Shared gym-session credit when stream logs tag other athletes."""

from __future__ import annotations

from coach_bot.config import CoachAthleteCfg, resolve_gym_log_recipients


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
