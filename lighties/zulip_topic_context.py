"""Fetch and format Zulip stream/topic messages for training-plan context."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import requests

from send_to_zulip import default_zuliprc_path, load_zuliprc

_MENTION_RE = re.compile(r"@\*{2}[^*]+\*\*")
_MAX_MESSAGES_DEFAULT = 200
_MAX_CHARS_DEFAULT = 12_000


def _strip_message_content(content: str) -> str:
    text = _MENTION_RE.sub("", content or "")
    return " ".join(text.split())


def _format_timestamp(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def format_messages_as_context(
    messages: Sequence[Dict[str, Any]],
    *,
    since: Optional[datetime] = None,
    exclude_message_id: Optional[int] = None,
    max_chars: int = _MAX_CHARS_DEFAULT,
) -> str:
    """Turn Zulip API message dicts into plain text (oldest first)."""
    since_ts: Optional[float] = None
    if since is not None:
        s = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        since_ts = s.astimezone(timezone.utc).timestamp()

    sorted_msgs = sorted(messages, key=lambda m: int(m.get("id", 0)))
    lines: List[str] = []
    total = 0
    for msg in sorted_msgs:
        mid = msg.get("id")
        if exclude_message_id is not None and mid == exclude_message_id:
            continue
        ts = msg.get("timestamp")
        if since_ts is not None and ts is not None and float(ts) <= since_ts:
            continue
        body = _strip_message_content(str(msg.get("content", "")))
        if not body:
            continue
        sender = str(msg.get("sender_full_name") or msg.get("sender_email") or "?")
        stamp = _format_timestamp(float(ts)) if ts is not None else ""
        line = f"[{stamp}] {sender}: {body}" if stamp else f"{sender}: {body}"
        if total + len(line) + 1 > max_chars and lines:
            break
        lines.append(line)
        total += len(line) + 1
    while len(lines) > 1 and total > max_chars:
        dropped = lines.pop(0)
        total -= len(dropped) + 1
    return "\n".join(lines)


def _get_messages_requests(
    creds: dict,
    stream: str,
    topic: str,
    *,
    max_messages: int,
) -> List[Dict[str, Any]]:
    narrow = [
        {"operator": "stream", "operand": stream},
        {"operator": "topic", "operand": topic},
    ]
    site = creds["site"].rstrip("/")
    resp = requests.get(
        f"{site}/api/v1/messages",
        auth=(creds["email"], creds["key"]),
        params={
            "narrow": json.dumps(narrow),
            "anchor": "newest",
            "num_before": max(1, max_messages),
            "num_after": 0,
            "apply_markdown": "false",
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("result") != "success":
        raise RuntimeError(f"Zulip get_messages failed: {data}")
    raw = data.get("messages") or []
    return raw if isinstance(raw, list) else []


def fetch_topic_context_since(
    stream: str,
    topic: str,
    since: datetime,
    *,
    zuliprc_path: Optional[Path] = None,
    max_messages: int = _MAX_MESSAGES_DEFAULT,
    max_chars: int = _MAX_CHARS_DEFAULT,
    exclude_message_id: Optional[int] = None,
) -> str:
    """Messages from stream/topic after ``since`` (UTC), via Zulip REST API."""
    creds = load_zuliprc(zuliprc_path or default_zuliprc_path())
    messages = _get_messages_requests(
        creds, stream, topic, max_messages=max_messages
    )
    return format_messages_as_context(
        messages,
        since=since,
        exclude_message_id=exclude_message_id,
        max_chars=max_chars,
    )


def fetch_topic_context_client(
    client: Any,
    stream: str,
    topic: str,
    *,
    since: Optional[datetime] = None,
    exclude_message_id: Optional[int] = None,
    max_messages: int = 50,
    max_chars: int = _MAX_CHARS_DEFAULT,
) -> str:
    """Same as :func:`fetch_topic_context_since` using a ``zulip.Client``."""
    narrow = [
        {"operator": "stream", "operand": stream},
        {"operator": "topic", "operand": topic},
    ]
    result = client.get_messages(
        {
            "narrow": json.dumps(narrow),
            "anchor": "newest",
            "num_before": max(1, max_messages),
            "num_after": 0,
            "apply_markdown": False,
        }
    )
    if result.get("result") != "success":
        return ""
    raw_messages = result.get("messages") or []
    if not isinstance(raw_messages, list):
        return ""
    return format_messages_as_context(
        raw_messages,
        since=since,
        exclude_message_id=exclude_message_id,
        max_chars=max_chars,
    )
