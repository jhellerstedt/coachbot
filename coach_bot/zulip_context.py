"""Fetch Zulip topic context for coach bot Kagi Q&A (delegates to lighties)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import zulip

from coach_bot.zulip_uploads import extract_image_upload_urls, strip_upload_markdown
from zulip_topic_context import fetch_topic_context_client

_MAX_MESSAGES_DEFAULT = 50
_MAX_CHARS_DEFAULT = 12_000
_MENTION_RE = re.compile(r"@\*\*[^*]+\*\*")
_DEFAULT_SESSION_MAX_AGE_SECONDS = 900
_DEFAULT_SESSION_MAX_IMAGES = 3


def fetch_topic_context(
    client: zulip.Client,
    stream: str,
    topic: str,
    *,
    exclude_message_id: Optional[int] = None,
    max_messages: int = _MAX_MESSAGES_DEFAULT,
    max_chars: int = _MAX_CHARS_DEFAULT,
) -> str:
    """Recent stream/topic messages (no time filter), oldest first."""
    return fetch_topic_context_client(
        client,
        stream,
        topic,
        since=None,
        exclude_message_id=exclude_message_id,
        max_messages=max_messages,
        max_chars=max_chars,
    )


def _message_mentions_user(message: Dict[str, Any], user_id: int) -> bool:
    for uid in message.get("mentions") or []:
        try:
            if int(uid) == user_id:
                return True
        except (TypeError, ValueError):
            continue
    content = str(message.get("content") or "")
    return bool(re.search(rf"@\*\*[^*|]+\|{user_id}\*\*", content))


def _strip_athlete_note(content: str) -> str:
    text = _MENTION_RE.sub("", strip_upload_markdown(content or ""))
    return " ".join(text.split()).strip()


def _fetch_topic_messages_around(
    client: zulip.Client,
    stream: str,
    topic: str,
    *,
    around_message_id: int,
    max_scan: int,
) -> List[Dict[str, Any]]:
    narrow = [
        {"operator": "stream", "operand": stream},
        {"operator": "topic", "operand": topic},
    ]
    result = client.get_messages(
        {
            "narrow": json.dumps(narrow),
            "anchor": str(around_message_id),
            "num_before": max(1, max_scan),
            "num_after": max(1, max_scan),
            "apply_markdown": False,
        }
    )
    if result.get("result") != "success":
        return []
    raw_messages = result.get("messages") or []
    if not isinstance(raw_messages, list):
        return []
    return [msg for msg in raw_messages if isinstance(msg, dict)]


@dataclass(frozen=True)
class SameSenderSessionImages:
    """Image URLs (and optional notes) gathered from one uploader's nearby messages."""

    image_urls: List[str]
    adjacent_text: str = ""


def collect_same_sender_session_images(
    client: zulip.Client,
    stream: str,
    topic: str,
    *,
    sender_id: int,
    around_message_id: int,
    around_timestamp: float,
    current_image_urls: Sequence[str],
    bot_user_id: Optional[int] = None,
    skip_message: Optional[Callable[[Dict[str, Any]], bool]] = None,
    max_age_seconds: int = _DEFAULT_SESSION_MAX_AGE_SECONDS,
    max_images: int = _DEFAULT_SESSION_MAX_IMAGES,
    max_scan: int = 30,
) -> SameSenderSessionImages:
    """Merge image uploads from the same sender's nearby topic messages.

    Looks before *and* after ``around_message_id`` within ``max_age_seconds`` so a
    multi-part erg (warm-up / main / cool-down) split across messages still parses
    as one session. Skips adjacent messages that @-mention the bot (those log on
    their own). Caps at ``max_images`` (default 3).
    """
    current_urls = [u for u in current_image_urls if u]
    if len(current_urls) >= max_images or max_images <= 0:
        return SameSenderSessionImages(image_urls=list(current_urls[:max_images]))

    messages = _fetch_topic_messages_around(
        client,
        stream,
        topic,
        around_message_id=around_message_id,
        max_scan=max_scan,
    )
    if not messages:
        return SameSenderSessionImages(image_urls=list(current_urls))

    lower = around_timestamp - max_age_seconds
    upper = around_timestamp + max_age_seconds
    # message_id -> (urls, note); current message seeded from caller
    by_id: Dict[int, Tuple[List[str], str]] = {
        around_message_id: (list(current_urls), ""),
    }
    adjacent_notes: List[Tuple[int, str]] = []

    for msg in messages:
        mid_raw = msg.get("id")
        if mid_raw is None:
            continue
        mid = int(mid_raw)
        if mid == around_message_id:
            continue
        if int(msg.get("sender_id") or 0) != sender_id:
            continue
        ts = msg.get("timestamp")
        if ts is not None:
            ts_f = float(ts)
            if ts_f < lower or ts_f > upper:
                continue
        if skip_message is not None and skip_message(msg):
            continue
        if bot_user_id is not None and _message_mentions_user(msg, bot_user_id):
            continue
        urls = extract_image_upload_urls(str(msg.get("content") or ""))
        if not urls:
            continue
        note = _strip_athlete_note(str(msg.get("content") or ""))
        by_id[mid] = (urls, note)
        if note:
            adjacent_notes.append((mid, note))

    merged: List[str] = []
    seen: set[str] = set()
    for mid in sorted(by_id):
        urls, _ = by_id[mid]
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            merged.append(url)
            if len(merged) >= max_images:
                break
        if len(merged) >= max_images:
            break

    adjacent_text = " ".join(note for _, note in sorted(adjacent_notes))
    return SameSenderSessionImages(
        image_urls=merged or list(current_urls),
        adjacent_text=adjacent_text,
    )


def find_recent_sender_image_message(
    client: zulip.Client,
    stream: str,
    topic: str,
    *,
    sender_id: int,
    before_message_id: int,
    before_timestamp: float,
    max_age_seconds: int = 900,
    max_scan: int = 30,
) -> Optional[Tuple[Dict[str, Any], List[str]]]:
    """Most recent prior message from ``sender_id`` in topic with image uploads."""
    messages = _fetch_topic_messages_around(
        client,
        stream,
        topic,
        around_message_id=before_message_id,
        max_scan=max_scan,
    )
    if not messages:
        return None

    cutoff = before_timestamp - max_age_seconds
    candidates: List[Tuple[int, Dict[str, Any], List[str]]] = []
    for msg in messages:
        mid = msg.get("id")
        if mid is None or int(mid) >= before_message_id:
            continue
        if int(msg.get("sender_id") or 0) != sender_id:
            continue
        ts = msg.get("timestamp")
        if ts is not None and float(ts) < cutoff:
            continue
        urls = extract_image_upload_urls(str(msg.get("content") or ""))
        if not urls:
            continue
        candidates.append((int(mid), msg, urls))

    if not candidates:
        return None
    _, best_msg, best_urls = max(candidates, key=lambda row: row[0])
    return best_msg, best_urls
