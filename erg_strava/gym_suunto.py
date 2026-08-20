"""Parse Suunto gym workout notes (reps/weight lists synced from the Suunto app)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, List, Mapping, Optional

from generate_training_plan import (
    GymExerciseMetrics,
    GymSessionMetrics,
    GymSetMetrics,
    normalize_gym_exercise_header,
)

# Suunto app activityId for Gym / weight training (see Suunto Activities.pdf).
DEFAULT_SUUNTO_GYM_ACTIVITY_IDS = frozenset({23})

_SET_WITH_REPS_RE = re.compile(r"^\s*(\d+)\s*r\s*([\d.]+)\s*$", re.IGNORECASE)
_WEIGHT_ONLY_RE = re.compile(r"^\s*([\d.]+)\s*$")
_SET_LINE_RE = re.compile(
    r"\d+\s*r\b|\d+\s*[x×]\s*\d|\d+(?:\.\d+)?\s*(?:kg\b|,|$)",
    re.IGNORECASE,
)
_DURATION_HOLD_RE = re.compile(r"(\d+)\s*s(?:ec(?:onds?)?)?\b", re.IGNORECASE)
_SKIP_HEADER_LINE_RE = re.compile(
    r"^(?:gym\b|goal\s*:|strength\b.*\(|logged\b|total\b)",
    re.IGNORECASE,
)
_BOLD_EXERCISE_RE = re.compile(r"\*\*[^*]+:\*\*")


def extract_suunto_workout_description(detail: Mapping[str, Any]) -> str:
    """Pull free-text workout notes from a suuntool workouts get JSON payload."""
    for key in (
        "description",
        "notes",
        "workoutNotes",
        "richDescription",
        "comment",
        "workoutDescription",
    ):
        value = detail.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    extensions = detail.get("extensions")
    if isinstance(extensions, list):
        for item in extensions:
            if not isinstance(item, dict):
                continue
            for key in ("description", "notes", "value", "text"):
                value = item.get(key)
                if value is not None and str(value).strip():
                    return str(value).strip()
    return ""


def _parse_suunto_set_tokens(sets_text: str) -> List[GymSetMetrics]:
    """Parse '8r 40, 5r 50, …' or '15r 10, 10, 10' Suunto gym note tokens."""
    sets: List[GymSetMetrics] = []
    pending_reps: Optional[int] = None
    for raw_part in sets_text.split(","):
        part = raw_part.strip()
        if not part:
            continue
        m = _SET_WITH_REPS_RE.match(part)
        if m:
            pending_reps = int(m.group(1))
            sets.append(
                GymSetMetrics(reps=pending_reps, weight_kg=float(m.group(2)))
            )
            continue
        w = _WEIGHT_ONLY_RE.match(part)
        if w and pending_reps is not None:
            sets.append(
                GymSetMetrics(reps=pending_reps, weight_kg=float(w.group(1)))
            )
    return sets


def _parse_duration_hold_tokens(sets_text: str) -> List[GymSetMetrics]:
    """Parse timed holds like '75s, 75s' (reps=1, weight=0)."""
    sets: List[GymSetMetrics] = []
    for raw_part in sets_text.split(","):
        part = raw_part.strip()
        if not part:
            continue
        m = _DURATION_HOLD_RE.search(part)
        if m:
            sets.append(GymSetMetrics(reps=1, weight_kg=0.0))
    return sets


def _looks_like_set_line(line: str) -> bool:
    text = (line or "").strip()
    if not text:
        return False
    if _DURATION_HOLD_RE.search(text):
        return True
    return bool(_SET_LINE_RE.search(text))


def _is_skippable_header_line(line: str) -> bool:
    text = (line or "").strip()
    if not text:
        return True
    return bool(_SKIP_HEADER_LINE_RE.match(text))


def _exercise_metrics(name: str, sets: List[GymSetMetrics]) -> GymExerciseMetrics:
    max_w = max(s.weight_kg for s in sets)
    tonnage = sum(s.reps * s.weight_kg for s in sets)
    return GymExerciseMetrics(
        name=name,
        max_weight_kg=max_w,
        tonnage_kg=tonnage,
        sets=sets,
    )


def _canonical_exercise_key(name: str) -> str:
    from generate_training_plan import normalize_gym_exercise_header

    canonical = normalize_gym_exercise_header(name) or (name or "").strip()
    return canonical.lower()


def _exercise_set_signature(ex: GymExerciseMetrics) -> tuple[tuple[int, float], ...]:
    return tuple((s.reps, s.weight_kg) for s in ex.sets)


def _transcript_sets_are_prefix_of_llm(
    transcript: GymExerciseMetrics,
    llm: GymExerciseMetrics,
) -> bool:
    alt = _exercise_set_signature(transcript)
    llm_sig = _exercise_set_signature(llm)
    if len(alt) >= len(llm_sig):
        return False
    return alt == llm_sig[: len(alt)]


def _prefer_transcript_exercise(
    llm_ex: GymExerciseMetrics,
    transcript_ex: GymExerciseMetrics,
) -> bool:
    """True when deterministic transcript sets should replace LLM sets."""
    if _exercise_set_signature(llm_ex) == _exercise_set_signature(transcript_ex):
        return False
    alt_sig = _exercise_set_signature(transcript_ex)
    llm_sig = _exercise_set_signature(llm_ex)
    if len(alt_sig) >= len(llm_sig):
        return True
    if _transcript_sets_are_prefix_of_llm(transcript_ex, llm_ex):
        return False
    return True


def reconcile_gym_metrics_with_transcript(
    metrics: GymSessionMetrics,
    description: str,
) -> GymSessionMetrics:
    """Prefer deterministic transcript sets when they disagree with the LLM."""
    transcript = parse_coach_gym_transcript(description)
    if transcript is None:
        return metrics

    by_name = {_canonical_exercise_key(ex.name): ex for ex in transcript.exercises}
    merged: List[GymExerciseMetrics] = []
    seen: set[str] = set()

    for ex in metrics.exercises:
        key = _canonical_exercise_key(ex.name)
        seen.add(key)
        alt = by_name.get(key)
        if alt is not None and _prefer_transcript_exercise(ex, alt):
            merged.append(
                GymExerciseMetrics(
                    name=alt.name,
                    max_weight_kg=alt.max_weight_kg,
                    tonnage_kg=alt.tonnage_kg,
                    sets=list(alt.sets),
                )
            )
        else:
            merged.append(ex)

    for ex in transcript.exercises:
        key = _canonical_exercise_key(ex.name)
        if key not in seen:
            merged.append(ex)

    if not merged:
        return metrics

    total = sum(ex.tonnage_kg for ex in merged)
    assumptions = metrics.assumptions
    if transcript.assumptions and assumptions != transcript.assumptions:
        assumptions = (
            f"{assumptions}; reconciled with deterministic transcript parse."
            if assumptions
            else transcript.assumptions
        )
    return GymSessionMetrics(
        activity_id=metrics.activity_id,
        activity_name=metrics.activity_name,
        total_tonnage_kg=total,
        unit=metrics.unit,
        exercises=merged,
        assumptions=assumptions,
    )


def parse_coach_gym_transcript(text: str) -> Optional[GymSessionMetrics]:
    """
    Deterministic fallback for free-form coach-bot gym logs, e.g.::

        back squat
        8r 70, 90, 6r 100, 8r 70
        plank
        75s, 75s
    """
    text = (text or "").strip()
    if not text:
        return None
    if _BOLD_EXERCISE_RE.search(text):
        return None

    exercises: List[GymExerciseMetrics] = []
    lines = [line.strip() for line in text.splitlines()]
    i = 0
    while i < len(lines):
        while i < len(lines) and _is_skippable_header_line(lines[i]):
            i += 1
        if i >= len(lines):
            break

        line = lines[i]
        header = normalize_gym_exercise_header(line)
        sets_text = ""

        inline = re.match(r"^(.+?):\s*(.+)$", line)
        if inline and normalize_gym_exercise_header(inline.group(1)):
            header = normalize_gym_exercise_header(inline.group(1))
            sets_text = inline.group(2).strip()
            i += 1
        elif header is not None:
            i += 1
            set_lines: List[str] = []
            while i < len(lines):
                next_line = lines[i]
                if not next_line or _is_skippable_header_line(next_line):
                    i += 1
                    continue
                if normalize_gym_exercise_header(next_line) is not None:
                    break
                if not _looks_like_set_line(next_line):
                    break
                set_lines.append(next_line)
                i += 1
            sets_text = ", ".join(set_lines)
        else:
            i += 1
            continue

        sets = _parse_suunto_set_tokens(sets_text)
        if not sets and header and str(header).lower() == "plank":
            sets = _parse_duration_hold_tokens(sets_text)
        if header and sets:
            exercises.append(_exercise_metrics(header, sets))

    if not exercises:
        return None

    total = sum(ex.tonnage_kg for ex in exercises)
    return GymSessionMetrics(
        activity_id=0,
        activity_name="Gym (coach transcript)",
        total_tonnage_kg=total,
        unit="kg",
        exercises=exercises,
        assumptions=(
            "Parsed from coach-bot gym transcript (reps×kg, weights in kg; "
            "comma-separated weights inherit the previous rep count)."
        ),
    )


def parse_suunto_gym_description(text: str) -> Optional[GymSessionMetrics]:
    """
    Parse Suunto gym move notes, e.g.::

        * **Bench Press:** 8r 40, 5r 50, 5r 50
        * **Kettlebell Swings:** 15r 10, 10, 10

    Weights are treated as kilograms (Suunto app default for this crew).
    """
    text = (text or "").strip()
    if not text:
        return None

    exercises: List[GymExerciseMetrics] = []
    for match in re.finditer(
        r"\*\*([^*]+?):\*\*\s*(.+?)(?=\*\*[^*]+:\*\*|$)",
        text,
        flags=re.DOTALL,
    ):
        name = match.group(1).strip()
        sets = _parse_suunto_set_tokens(match.group(2))
        if not name or not sets:
            continue
        exercises.append(_exercise_metrics(name, sets))

    if not exercises:
        return None

    total = sum(ex.tonnage_kg for ex in exercises)
    return GymSessionMetrics(
        activity_id=0,
        activity_name="Suunto gym",
        total_tonnage_kg=total,
        unit="kg",
        exercises=exercises,
        assumptions="Parsed from Suunto gym notes (reps×kg, weights in kg).",
    )


def suunto_gym_description(
    cache_dir: Path,
    athlete_id: int,
    suunto_key: str,
) -> Optional[str]:
    """Load Suunto gym notes by workout key."""
    from suunto_sync import suunto_paths

    meta_path = suunto_paths(cache_dir, athlete_id)["workouts"] / f"{suunto_key}.json"
    if not meta_path.is_file():
        return None
    try:
        detail = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    text = extract_suunto_workout_description(detail)
    return text or None


def suunto_gym_description_for_strava(
    cache_dir: Path,
    athlete_id: int,
    strava_activity_id: int,
) -> Optional[str]:
    """Load Suunto workout notes linked to a Strava activity, if cached."""
    from suunto_sync import suunto_key_for_strava

    key = suunto_key_for_strava(cache_dir, athlete_id, strava_activity_id)
    if not key:
        return None
    return suunto_gym_description(cache_dir, athlete_id, key)
