"""Persist a rolling 10-minute listen window per Zulip stream/topic."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

LISTEN_WINDOW_SECONDS = 600
LISTEN_STATE_FILENAME = "coach_listen_state.json"


def listen_state_path(cache_dir: Path) -> Path:
    return Path(cache_dir) / LISTEN_STATE_FILENAME


def _empty_state() -> dict[str, Any]:
    return {"threads": {}}


def load_listen_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read listen state %s: %s; starting empty", path, exc)
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    threads = data.get("threads")
    if not isinstance(threads, dict):
        threads = {}
    return {"threads": threads}


def save_listen_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {"threads": state.get("threads") if isinstance(state.get("threads"), dict) else {}}
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def thread_key(stream: str, topic: str) -> str:
    return f"{stream}|{topic}"


def _as_utc(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _parse_listen_until(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def activate_listen_window(
    state: dict[str, Any],
    stream: str,
    topic: str,
    *,
    now: datetime,
    duration_seconds: int = LISTEN_WINDOW_SECONDS,
) -> None:
    threads = state.setdefault("threads", {})
    if not isinstance(threads, dict):
        threads = {}
        state["threads"] = threads
    until = _as_utc(now) + timedelta(seconds=int(duration_seconds))
    threads[thread_key(stream, topic)] = {"listen_until": until.isoformat()}


def listen_window_open(
    state: dict[str, Any],
    stream: str,
    topic: str,
    *,
    now: datetime,
) -> bool:
    threads = state.get("threads")
    if not isinstance(threads, dict):
        return False
    entry = threads.get(thread_key(stream, topic))
    if not isinstance(entry, dict):
        return False
    until = _parse_listen_until(entry.get("listen_until"))
    if until is None:
        return False
    return _as_utc(now) <= until
