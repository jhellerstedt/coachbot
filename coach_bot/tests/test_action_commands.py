"""Exact bot commands that produce actions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from coach_bot.commands import KDE_BUSY, KDE_NOTICE, help_text, match_action_command
from coach_bot.config import CoachAthleteCfg, format_unmatched_sender_help
from coach_bot.handler import CoachMessageHandler


def test_help_and_kde_match_exactly():
    assert match_action_command("help") == "help"
    assert match_action_command("  KDE  ") == "kde"
    assert match_action_command("kde please") is None
    assert match_action_command("") is None


def test_help_text_lists_kde_without_a_model():
    text = help_text()
    assert "kde" in text
    assert "project-640" in text
    assert "No weekly plan" in text


def _athlete() -> CoachAthleteCfg:
    return CoachAthleteCfg(
        id=7,
        label="Ada",
        zulip_email="ada@example.com",
        zulip_user_id=5,
    )


def _handler(tmp_path: Path, client=None) -> CoachMessageHandler:
    return CoachMessageHandler(
        cache_dir=tmp_path,
        bot_user_id=99,
        zulip_client=client,
        kagi_token="test-key",
        bot_full_name="coach",
        athletes=[_athlete()],
    )


def _config(handler: CoachMessageHandler):
    return patch(
        "coach_bot.handler.load_bot_config",
        return_value=(None, None, None, None, None, handler.athletes),
    )


def _dm(content: str, *, sender_id: int = 5) -> dict:
    return {
        "type": "private",
        "id": 42,
        "sender_id": sender_id,
        "sender_email": "ada@example.com" if sender_id == 5 else "other@example.com",
        "sender_full_name": "Ada" if sender_id == 5 else "Other",
        "content": content,
        "timestamp": datetime(2026, 10, 28, tzinfo=timezone.utc).timestamp(),
    }


def _stream(content: str, *, mentions: list[int] | None = None, sender_id: int = 5) -> dict:
    return {
        "type": "stream",
        "id": 43,
        "display_recipient": "general",
        "subject": "project-640",
        "sender_id": sender_id,
        "sender_email": "ada@example.com" if sender_id == 5 else "other@example.com",
        "sender_full_name": "Ada" if sender_id == 5 else "Other",
        "content": content,
        "mentions": mentions or [],
        "timestamp": datetime(2026, 10, 28, tzinfo=timezone.utc).timestamp(),
    }


def test_help_does_not_call_the_plot_runner_or_a_model(tmp_path: Path):
    handler = _handler(tmp_path)
    with _config(handler), patch(
        "strava_erg_hr_plot.refresh_kde_plot"
    ) as refresh, patch(
        "coach_bot.handler.interpret_coach_message_with_kagi"
    ) as interpret:
        reply = handler.handle(_dm("help"))
    assert reply == help_text()
    refresh.assert_not_called()
    interpret.assert_not_called()


def test_kde_posts_notice_then_runs_the_plot(tmp_path: Path):
    client = MagicMock()
    handler = _handler(tmp_path, client)
    with _config(handler), patch(
        "strava_erg_hr_plot.refresh_kde_plot", return_value="Posted the KDE plot."
    ) as refresh, patch(
        "coach_bot.handler.get_config_path", return_value=tmp_path / "config.yaml"
    ):
        reply = handler.handle(_dm("KDE"))
    assert reply == "Posted the KDE plot."
    refresh.assert_called_once_with(tmp_path / "config.yaml")
    payload = client.send_message.call_args[0][0]
    assert payload["content"] == KDE_NOTICE
    assert payload["type"] == "private"


def test_kde_please_is_not_a_command(tmp_path: Path):
    handler = _handler(tmp_path)
    with _config(handler), patch(
        "strava_erg_hr_plot.refresh_kde_plot"
    ) as refresh, patch(
        "coach_bot.handler.config_maintenance_enabled", return_value=False
    ), patch(
        "coach_bot.handler.plan_for_date", return_value=None
    ), patch(
        "coach_bot.handler.interpret_coach_message_with_kagi",
        return_value=MagicMock(intent="coaching_reply", reply="ok"),
    ):
        handler.handle(_dm("kde please"))
    refresh.assert_not_called()


def test_stream_mention_runs_kde_and_unmentioned_does_not(tmp_path: Path):
    handler = _handler(tmp_path, MagicMock())
    with _config(handler), patch(
        "strava_erg_hr_plot.refresh_kde_plot", return_value="posted"
    ) as refresh, patch(
        "coach_bot.handler.get_config_path", return_value=tmp_path / "config.yaml"
    ):
        mentioned = handler.handle(_stream("@**coach** kde", mentions=[99]))
        unmentioned = handler.handle(_stream("kde"))
    assert mentioned == "posted"
    assert unmentioned is None
    refresh.assert_called_once()
    payload = handler.zulip_client.send_message.call_args[0][0]
    assert payload["type"] == "stream"
    assert payload["topic"] == "project-640"
    assert payload["content"] == KDE_NOTICE


def test_unmapped_mention_does_not_run_kde(tmp_path: Path):
    handler = _handler(tmp_path)
    with _config(handler), patch(
        "strava_erg_hr_plot.refresh_kde_plot"
    ) as refresh:
        reply = handler.handle(_stream("@**coach** help", mentions=[99], sender_id=8))
    refresh.assert_not_called()
    assert reply == format_unmatched_sender_help(
        sender_email="other@example.com",
        sender_full_name="Other",
        sender_id=8,
    )


def test_second_kde_while_running_is_busy(tmp_path: Path):
    handler = _handler(tmp_path, MagicMock())
    seen: dict[str, str] = {}

    def refresh(_path: Path) -> str:
        seen["second"] = handler.handle(_dm("kde")) or ""
        return "Posted the KDE plot."

    with _config(handler), patch(
        "strava_erg_hr_plot.refresh_kde_plot", side_effect=refresh
    ), patch(
        "coach_bot.handler.get_config_path", return_value=tmp_path / "config.yaml"
    ):
        reply = handler.handle(_dm("kde"))
    assert seen["second"] == KDE_BUSY
    assert reply == "Posted the KDE plot."
