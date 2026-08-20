"""Erg score text detection and coaching follow-up helpers."""

from __future__ import annotations

import re

_SPLIT_RE = re.compile(r"\b[12]:\d{2}(?:[.,]\d)?\b")
_DISTANCE_RE = re.compile(
    r"\b\d{3,5}\s*(?:m|met(?:er|re)s?)\b|\b\d{1,2}(?:\.\d+)?\s*km\b", re.I
)
_DURATION_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\.\d)?\b")
_INTERVAL_RE = re.compile(r"\b\d+\s*[x×]\s*\d", re.I)

_ELABORATION_RE = re.compile(
    r"\b("
    r"more detail|more details|elaborate|expand|tell me more|go deeper|"
    r"full (?:analysis|breakdown|feedback)|break(?: it)? down|"
    r"explain (?:more|further)|what do you think|coach me on (?:that|this|it)|"
    r"how did i do|how'd i do|rate (?:that|this|it|my)|"
    r"prescription vs|vs prescription|compare to plan"
    r")\b",
    re.I,
)
_ELABORATION_SHORT_RE = re.compile(r"^(?:more|details?|elaborate|expand)\??$", re.I)


def looks_like_erg_score_text(text: str) -> bool:
    """Heuristic: typed erg transcript rather than a coaching question."""
    body = (text or "").strip()
    if len(body) < 24:
        return False
    if wants_erg_coaching_elaboration(body):
        return False
    signals = 0
    if _SPLIT_RE.search(body):
        signals += 1
    if _DISTANCE_RE.search(body):
        signals += 1
    if _DURATION_RE.search(body):
        signals += 1
    if _INTERVAL_RE.search(body):
        signals += 1
    if re.search(r"\b(erg|concept2|pm5|view detail|steady|interval)\b", body, re.I):
        signals += 1
    if re.search(r"\b\d{3,5}\b", body) and _SPLIT_RE.search(body):
        signals += 1
    return signals >= 2


_NEARBY_ERG_IMAGE_RE = re.compile(
    r"\b("
    r"image above|screenshot above|pic above|photo above|attachment above|"
    r"in the image|in that image|in my (?:screenshot|image|pic)|"
    r"see my erg|log my erg|my erg(?: session)? in"
    r")\b",
    re.I,
)


def references_nearby_erg_screenshot(text: str) -> bool:
    """True when the athlete points at a screenshot in a recent prior message."""
    body = (text or "").strip()
    if not body:
        return False
    if _NEARBY_ERG_IMAGE_RE.search(body):
        return True
    if re.search(r"\berg\b", body, re.I) and re.search(
        r"\b(above|previous message|last message|just (?:sent|posted))\b", body, re.I
    ):
        return True
    return False


def wants_erg_coaching_elaboration(text: str) -> bool:
    """True when the athlete is asking for fuller erg-session coaching."""
    body = (text or "").strip()
    if not body:
        return False
    if _ELABORATION_SHORT_RE.match(body):
        return True
    return bool(_ELABORATION_RE.search(body))
