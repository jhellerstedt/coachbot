"""Decide whether an unmentioned stream message is a coach follow-up."""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

_ACK_ONLY = re.compile(
    r"^(thanks|thank you|thx|ok|okay|nice|great|perfect|sounds good|looks good|"
    r"will do|got it|cheers|yep|yes|cool)[\s!.]*$",
    re.IGNORECASE,
)

REPLY_NOUL_THRESHOLD = 0.8

FOLLOWUP_QUESTIONS: dict[str, dict[str, Any]] = {
    "should_reply": {
        "type": "noul",
        "instructions": (
            "This message needs a coaching reply. It asks the coach a question, "
            "reports RPE or effort, corrects a log, or adds a set."
        ),
        "criteria": {
            "true": "The message is directed at the coach and needs a reply.",
            "false": "Squad chatter, scheduling talk, or an acknowledgement.",
        },
    },
    "why": {
        "type": "choice",
        "instructions": "Why would the coach reply, if at all?",
        "criteria": {
            "question": "They asked the coach something.",
            "rpe_or_log": "They reported effort, corrected a log, or added a set.",
            "plan_change": "They asked to change an upcoming plan.",
            "chatter": "They are talking to teammates or only acknowledging.",
        },
    },
}

DecideFn = Callable[[Mapping[str, Any], Mapping[str, Any]], Optional[Mapping[str, Any]]]


def should_reply_to_followup(
    user_text: str,
    *,
    api_key: Optional[str] = None,
    decide: Optional[DecideFn] = None,
    use_llm: bool = True,
) -> bool:
    """Reply only when Jev's should_reply Noul is at least 0.8.

    A missing key or a transport error stays fail-closed.
    """
    text = (user_text or "").strip()
    if not text or _ACK_ONLY.match(text):
        return False
    if not use_llm:
        return False
    key = (api_key or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if decide is None:
        if not key:
            return False

        def decide(state: Mapping[str, Any], questions: Mapping[str, Any]):
            from openrouter_client import call_openrouter_decisions

            return call_openrouter_decisions(
                state=dict(state),
                questions=dict(questions),
                api_key=key,
            )

    try:
        answers = decide({"message": text}, FOLLOWUP_QUESTIONS)
    except Exception:
        logger.warning("follow-up triage failed; skipping reply", exc_info=True)
        return False
    if not isinstance(answers, Mapping):
        logger.warning("follow-up triage failed; skipping reply")
        return False
    raw = answers.get("should_reply")
    if not isinstance(raw, Mapping):
        return False
    try:
        return float(raw.get("noul")) >= REPLY_NOUL_THRESHOLD
    except (TypeError, ValueError):
        return False
