"""Message intent detection for the coach bot."""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Mapping, Optional

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


CHOICE_CONFIDENCE_FLOOR = 0.6

INTENT_QUESTIONS: dict[str, dict[str, Any]] = {
    "intent": {
        "type": "choice",
        "instructions": "What is this athlete message asking for?",
        "criteria": {
            "question": "A question for the coach.",
            "plan_adjustment": "A request to change a future weekly plan, even without the words plan, reduce, or next week.",
            "other": "A log, acknowledgement, or squad chatter.",
        },
    }
}

DecideFn = Callable[[Mapping[str, Any], Mapping[str, Any]], Optional[Mapping[str, Any]]]


def classify_message_intent(
    text: str,
    *,
    api_key: Optional[str] = None,
    decide: Optional[DecideFn] = None,
) -> str:
    """Return question, plan_adjustment, or other.

    Regex is the certain path. Jev runs only when neither pattern matches.
    """
    body = (text or "").strip()
    if looks_like_plan_adjustment(body):
        return "plan_adjustment"
    if looks_like_question(body):
        return "question"
    key = (api_key or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if decide is None:
        if not key:
            return "other"

        def decide(state: Mapping[str, Any], questions: Mapping[str, Any]):
            from openrouter_client import call_openrouter_decisions

            return call_openrouter_decisions(
                state=dict(state),
                questions=dict(questions),
                api_key=key,
            )

    try:
        answers = decide({"message": body}, INTENT_QUESTIONS)
    except Exception:
        return "other"
    if not isinstance(answers, Mapping):
        return "other"
    raw = answers.get("intent")
    if not isinstance(raw, Mapping):
        return "other"
    choice = raw.get("choice")
    if choice not in ("question", "plan_adjustment", "other"):
        return "other"
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        return "other"
    if confidence < CHOICE_CONFIDENCE_FLOOR:
        return "other"
    return str(choice)


def truncate_for_zulip(text: str, limit: int = 9500) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n\n… (truncated)"
