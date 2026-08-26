"""Listen-window state: 10-minute rolling follow-up after a stream reply."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from coach_bot.listen_window import (
    LISTEN_WINDOW_SECONDS,
    activate_listen_window,
    listen_state_path,
    listen_window_open,
    load_listen_state,
    save_listen_state,
)


def test_listen_window_seconds_is_ten_minutes():
    assert LISTEN_WINDOW_SECONDS == 600


def test_activate_and_open_within_window():
    state: dict = {}
    now = datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)
    activate_listen_window(state, "general", "project-640", now=now)
    assert listen_window_open(state, "general", "project-640", now=now) is True
    still = now + timedelta(minutes=9, seconds=59)
    assert listen_window_open(state, "general", "project-640", now=still) is True


def test_listen_window_expires_after_ten_minutes():
    state: dict = {}
    now = datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)
    activate_listen_window(state, "general", "project-640", now=now)
    expired = now + timedelta(minutes=10, seconds=1)
    assert listen_window_open(state, "general", "project-640", now=expired) is False


def test_refresh_extends_window():
    state: dict = {}
    now = datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)
    activate_listen_window(state, "general", "project-640", now=now)
    later = now + timedelta(minutes=8)
    activate_listen_window(state, "general", "project-640", now=later)
    assert listen_window_open(
        state, "general", "project-640", now=later + timedelta(minutes=9)
    ) is True
    assert listen_window_open(
        state, "general", "project-640", now=later + timedelta(minutes=11)
    ) is False


def test_other_topic_is_closed():
    state: dict = {}
    now = datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)
    activate_listen_window(state, "general", "project-640", now=now)
    assert listen_window_open(state, "general", "other", now=now) is False


def test_missing_state_file_is_empty(tmp_path: Path):
    path = listen_state_path(tmp_path)
    assert load_listen_state(path) == {"threads": {}}


def test_corrupt_state_file_is_empty(tmp_path: Path):
    path = listen_state_path(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    assert load_listen_state(path) == {"threads": {}}


def test_save_round_trip(tmp_path: Path):
    path = listen_state_path(tmp_path)
    now = datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc)
    state = load_listen_state(path)
    activate_listen_window(state, "general", "project-640", now=now)
    save_listen_state(path, state)
    loaded = load_listen_state(path)
    assert listen_window_open(loaded, "general", "project-640", now=now) is True
