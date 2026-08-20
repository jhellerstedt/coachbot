"""Tests for same-uploader adjacent erg screenshot collection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from coach_bot.config import CoachAthleteCfg
from coach_bot.handler import CoachMessageHandler
from coach_bot.zulip_context import collect_same_sender_session_images


def _mock_client(messages: list[dict]) -> MagicMock:
    client = MagicMock()
    client.get_messages.return_value = {"result": "success", "messages": messages}
    return client


def test_collect_merges_prior_same_sender_image_without_bot_mention():
    """James-style: first message has screenshot, @coach message has another."""
    prior_url = "/user_uploads/10/a/warmup.heic"
    current_url = "/user_uploads/10/b/main.heic"
    messages = [
        {
            "id": 103320,
            "sender_id": 12345,
            "timestamp": 1000.0,
            "content": f"Erg this morning [a.heic]({prior_url})",
        },
        {
            "id": 103321,
            "sender_id": 12345,
            "timestamp": 1001.0,
            "content": f"@**coach** [b.heic]({current_url})",
            "mentions": [99],
        },
    ]
    collected = collect_same_sender_session_images(
        _mock_client(messages),
        "general",
        "project-640",
        sender_id=12345,
        around_message_id=103321,
        around_timestamp=1001.0,
        current_image_urls=[current_url],
        bot_user_id=99,
    )
    assert collected.image_urls == [prior_url, current_url]
    assert "Erg this morning" in collected.adjacent_text


def test_collect_merges_following_same_sender_image():
    current_url = "/user_uploads/10/b/main.jpeg"
    after_url = "/user_uploads/10/c/cooldown.jpeg"
    messages = [
        {
            "id": 10,
            "sender_id": 7,
            "timestamp": 500.0,
            "content": f"@**coach** [main.jpeg]({current_url})",
            "mentions": [99],
        },
        {
            "id": 11,
            "sender_id": 7,
            "timestamp": 502.0,
            "content": f"[cd.jpeg]({after_url})",
        },
    ]
    collected = collect_same_sender_session_images(
        _mock_client(messages),
        "general",
        "project-640",
        sender_id=7,
        around_message_id=10,
        around_timestamp=500.0,
        current_image_urls=[current_url],
        bot_user_id=99,
    )
    assert collected.image_urls == [current_url, after_url]


def test_collect_skips_adjacent_bot_mention_message():
    other_coach_url = "/user_uploads/10/x/other.jpeg"
    current_url = "/user_uploads/10/y/this.jpeg"
    messages = [
        {
            "id": 1,
            "sender_id": 7,
            "timestamp": 100.0,
            "content": f"@**coach|99** earlier [o.jpeg]({other_coach_url})",
            "mentions": [99],
        },
        {
            "id": 2,
            "sender_id": 7,
            "timestamp": 110.0,
            "content": f"@**coach** [t.jpeg]({current_url})",
            "mentions": [99],
        },
    ]
    collected = collect_same_sender_session_images(
        _mock_client(messages),
        "general",
        "project-640",
        sender_id=7,
        around_message_id=2,
        around_timestamp=110.0,
        current_image_urls=[current_url],
        bot_user_id=99,
    )
    assert collected.image_urls == [current_url]


def test_collect_ignores_other_senders_and_stale_messages():
    stale_url = "/user_uploads/10/s/stale.jpeg"
    other_url = "/user_uploads/10/o/other.jpeg"
    current_url = "/user_uploads/10/c/current.jpeg"
    nearby_url = "/user_uploads/10/n/nearby.jpeg"
    messages = [
        {
            "id": 1,
            "sender_id": 7,
            "timestamp": 0.0,
            "content": f"[s.jpeg]({stale_url})",
        },
        {
            "id": 2,
            "sender_id": 999,
            "timestamp": 1000.0,
            "content": f"[o.jpeg]({other_url})",
        },
        {
            "id": 3,
            "sender_id": 7,
            "timestamp": 1000.0,
            "content": f"[n.jpeg]({nearby_url})",
        },
        {
            "id": 4,
            "sender_id": 7,
            "timestamp": 1001.0,
            "content": f"@**coach** [c.jpeg]({current_url})",
            "mentions": [99],
        },
    ]
    collected = collect_same_sender_session_images(
        _mock_client(messages),
        "general",
        "project-640",
        sender_id=7,
        around_message_id=4,
        around_timestamp=1001.0,
        current_image_urls=[current_url],
        bot_user_id=99,
        max_age_seconds=900,
    )
    assert collected.image_urls == [nearby_url, current_url]


def test_collect_caps_at_three_chronologically():
    urls = [f"/user_uploads/10/{i}/img{i}.jpeg" for i in range(4)]
    messages = [
        {
            "id": i + 1,
            "sender_id": 7,
            "timestamp": 1000.0 + i,
            "content": (
                f"@**coach** [x.jpeg]({urls[i]})"
                if i == 2
                else f"[x.jpeg]({urls[i]})"
            ),
            "mentions": [99] if i == 2 else [],
        }
        for i in range(4)
    ]
    collected = collect_same_sender_session_images(
        _mock_client(messages),
        "general",
        "project-640",
        sender_id=7,
        around_message_id=3,
        around_timestamp=1002.0,
        current_image_urls=[urls[2]],
        bot_user_id=99,
    )
    assert collected.image_urls == urls[:3]


def test_handler_expands_adjacent_images_before_download(tmp_path: Path):
    handler = CoachMessageHandler(
        cache_dir=tmp_path,
        bot_user_id=99,
        zulip_client=MagicMock(),
        kagi_token="test-token",
        zulip_stream="general",
        zulip_topic="project-640",
        athletes=[
            CoachAthleteCfg(
                id=53603359,
                label="James Merrett",
                zulip_user_id=12345,
            )
        ],
    )
    prior_url = "/user_uploads/10/a/warmup.heic"
    current_url = "/user_uploads/10/b/main.heic"
    message = {
        "id": 103321,
        "sender_id": 12345,
        "sender_email": "james@example.com",
        "sender_full_name": "James Merrett",
        "content": f"@**coach** [main.heic]({current_url})",
        "timestamp": 1001.0,
        "type": "stream",
        "display_recipient": "general",
        "subject": "project-640",
        "mentions": [99],
    }

    with patch(
        "coach_bot.handler.collect_same_sender_session_images"
    ) as mock_collect, patch(
        "coach_bot.handler.download_zulip_upload",
        side_effect=[b"wu", b"main"],
    ) as mock_dl, patch(
        "coach_bot.handler.record_erg_score_from_images",
        return_value=({"id": "score-1", "session_date": "2026-07-16"}, True),
    ) as mock_record, patch.object(
        handler,
        "_finish_erg_score_log",
        return_value="ok",
    ), patch(
        "coach_bot.handler.prescribed_erg_section_for_log",
        return_value="",
    ), patch(
        "coach_bot.handler.find_erg_score_by_zulip_message",
        return_value=None,
    ):
        from coach_bot.zulip_context import SameSenderSessionImages

        mock_collect.return_value = SameSenderSessionImages(
            image_urls=[prior_url, current_url],
            adjacent_text="Erg this morning",
        )
        reply = handler.handle(message)

    assert reply == "ok"
    mock_collect.assert_called_once()
    assert mock_dl.call_count == 2
    mock_dl.assert_any_call(handler.zulip_client, prior_url)
    mock_dl.assert_any_call(handler.zulip_client, current_url)
    images_arg = mock_record.call_args[0][3]
    assert len(images_arg) == 2
    assert mock_record.call_args.kwargs["athlete_message"] == "Erg this morning"
