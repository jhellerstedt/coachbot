"""Decide whether an unmentioned stream message is a coach follow-up."""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

_ACK_ONLY = re.compile(
    r"^(thanks|thank you|thx|ok|okay|nice|great|perfect|sounds good|looks good|"
    r"will do|got it|cheers|yep|yes|cool)[\s!.]*$",
    re.IGNORECASE,
)

TRIAGE_SYSTEM = """You triage follow-up messages in a rowing/gym coaching Zulip topic.
The bot already posted a conversational reply. Decide whether this new message is
directed at the coach or is squad chatter.

Respond with ONLY a JSON object (no markdown fences):
{"should_reply": true or false, "reason": "one short sentence"}

Set should_reply=true when someone asks the coach a question, reports RPE/effort,
corrects a log, adds a set, or otherwise needs a coaching reply.

Set should_reply=false when they are talking to each other (scheduling, banter,
acknowledgements) or the message is not a coach follow-up."""

LlmCall = Callable[[str, str], str]


def _parse_triage_json(raw: str) -> dict:
    text = _JSON_FENCE.sub("", (raw or "").strip()).strip()
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("triage response is not a JSON object")
    return obj


def should_reply_to_followup(
    user_text: str,
    *,
    llm_call: Optional[LlmCall] = None,
    use_llm: bool = True,
) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    if _ACK_ONLY.match(text):
        return False
    if not use_llm or llm_call is None:
        return False
    try:
        raw = llm_call(TRIAGE_SYSTEM, f"User follow-up message:\n{text}")
        parsed = _parse_triage_json(raw)
        return bool(parsed.get("should_reply", False))
    except Exception:
        logger.warning("follow-up triage failed; skipping reply", exc_info=True)
        return False
