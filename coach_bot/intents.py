"""Message intent detection for the coach bot."""

from __future__ import annotations

import re
from typing import Optional

_QUESTION_WORD_RE = re.compile(
    r"(?:^|\b)(?:what|why|how|when|should|can|is|are|do|does)\b",
    re.I,
)
_PLAN_ADJUSTMENT_RE = re.compile(
    r"\b(?:"
    r"next week(?:'?s)?(?: plan| program)?|"
    r"adjust(?:ment)?|change(?: the)? plan|update(?: the)? plan|"
    r"reduce(?:\.|\s|$)|increase(?:\.|\s|$)|"
    r"swap(?: out| for|\.|\s|$)|replace(?: with|\.|\s|$)|"
    r"skip(?:\.|\s|$)|add(?: to)?(?: the)? plan|"
    r"remove(?: from)?(?: the)? plan|"
    r"schedule(?:\.|\s|$)"
    r")\b",
    re.I,
)
_MENTION_RE = re.compile(r"@\*\*[^*]+\*\*\s*")
_MENTION_EXTRACT_RE = re.compile(r"@\*\*([^*|]+)(?:\|(\d+))?\*\*", re.I)


def extract_zulip_user_mentions(content: str) -> list[tuple[str, Optional[int]]]:
    """Return (display_name, zulip_user_id) for each @**name|id** mention."""
    out: list[tuple[str, Optional[int]]] = []
    for match in _MENTION_EXTRACT_RE.finditer(content):
        name = match.group(1).strip()
        uid_raw = match.group(2)
        uid = int(uid_raw) if uid_raw is not None else None
        out.append((name, uid))
    return out


def strip_zulip_mentions(content: str) -> str:
    return _MENTION_RE.sub("", content).strip()


def looks_like_question(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    return bool(_QUESTION_WORD_RE.search(t))


def looks_like_plan_adjustment(text: str) -> bool:
    """True when the athlete is requesting a change to a future weekly plan."""
    return bool(_PLAN_ADJUSTMENT_RE.search(text.strip()))


def truncate_for_zulip(text: str, limit: int = 9500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n\n… (truncated)"
