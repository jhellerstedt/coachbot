"""Guardrails for Zulip erg score OCR and parsed metrics."""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional


_MIN_OCR_CHARS = 120
_WORK_DISTANCE_RE = re.compile(r"\b([1-2]\d{3,4})\b")
_SPLIT_RE = re.compile(r"\b[12]:\d{2}(?:[.,]\d)?\b")
_DISTANCE_RE = re.compile(r"\b\d{3,5}\s*(?:m|met(?:er|re)s?)?\b", re.I)
_DURATION_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\.\d)?\b")


def ocr_text_likely_readable(ocr_text: str) -> bool:
    """False when Tesseract output is too short or lacks erg-like tokens."""
    text = (ocr_text or "").strip()
    if len(text) < _MIN_OCR_CHARS:
        return False
    digit_chars = sum(ch.isdigit() for ch in text)
    if digit_chars < 24:
        return False
    work_distances = [
        int(m.group(1))
        for m in _WORK_DISTANCE_RE.finditer(text)
        if 1000 <= int(m.group(1)) <= 25000
    ]
    if work_distances and (_SPLIT_RE.search(text) or _DURATION_RE.search(text)):
        return True
    signals = 0
    if _SPLIT_RE.search(text):
        signals += 1
    if _DISTANCE_RE.search(text):
        signals += 1
    if _DURATION_RE.search(text):
        signals += 1
    if re.search(r"\b(interval|steady|/500|spm|s/m|view detail|concept)\b", text, re.I):
        signals += 1
    return signals >= 3


def erg_score_metrics_usable(metrics: Optional[Mapping[str, Any]]) -> bool:
    """True when at least one core session field was parsed."""
    if not metrics:
        return False
    if metrics.get("distance_m") is not None:
        try:
            if float(metrics["distance_m"]) > 0:
                return True
        except (TypeError, ValueError):
            pass
    if metrics.get("duration_sec") is not None:
        try:
            if float(metrics["duration_sec"]) > 0:
                return True
        except (TypeError, ValueError):
            pass
    if metrics.get("avg_split_500_sec") is not None:
        try:
            if float(metrics["avg_split_500_sec"]) > 0:
                return True
        except (TypeError, ValueError):
            pass
    if metrics.get("avg_split_500_fmt"):
        return True
    intervals = metrics.get("intervals") or []
    if isinstance(intervals, list) and intervals:
        return True
    parts = metrics.get("session_parts") or []
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            if part.get("distance_m") is not None:
                try:
                    if float(part["distance_m"]) > 0:
                        return True
                except (TypeError, ValueError):
                    pass
            if part.get("duration_sec") is not None:
                try:
                    if float(part["duration_sec"]) > 0:
                        return True
                except (TypeError, ValueError):
                    pass
            if part.get("avg_split_500_fmt") or part.get("avg_split_500_sec"):
                return True
    return False


def format_unreadable_screenshot_reply() -> str:
    return (
        "Could not read that erg screenshot — the OCR text was too garbled.\n\n"
        "Please repost using one of:\n"
        "- A **tight crop** of the PM5 View Detail table (numbers only, no bezel)\n"
        "- A **typed summary** with `@coach`, e.g. date, total distance, avg split, "
        "duration, and interval rows\n\n"
        "Nothing was logged."
    )


def format_unusable_parse_reply() -> str:
    return (
        "I could read some text from the screenshot but could not extract reliable "
        "erg metrics (distance, duration, or split).\n\n"
        "Please repost a clearer crop, or paste a typed summary with `@coach`:\n"
        "`2026-06-09 · 12993 m · 1:00:00 · avg 2:18.5 · 5×12:00 intervals …`\n\n"
        "Nothing was logged."
    )
