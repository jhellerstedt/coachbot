"""Weekly training plan JSON schema, parse/validate, render, and metrics."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from athlete_profile import AthleteProfile

PLAN_VERSION = 1

WEEKDAYS: Tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

SESSION_TYPES = ("gym", "erg", "on_water", "rest", "recovery")
GYM_CATEGORIES = ("leg", "upper_core")
GYM_GOALS = ("strength", "hypertrophy", "power", "recovery")
ROWING_PHASES = (
    "warm_up",
    "main_set",
    "cool_down",
    "work",
    "rest",
    "build",
    "active_recovery",
)
ZONE_Z = ("Z1", "Z2", "Z3", "Z4", "Z5")
ZONE_T = ("T1", "T2", "T3", "T4", "T5", "T6", "T7")
ZONE_ZT_COMPATIBLE: Dict[str, frozenset[str]] = {
    "Z1": frozenset({"T1", "T2"}),
    "Z2": frozenset({"T2", "T3", "T4"}),
    "Z3": frozenset({"T3", "T4", "T5"}),
    "Z4": frozenset({"T5", "T6"}),
    "Z5": frozenset({"T6", "T7"}),
}
PRIORITIES = ("split", "hr")

GYM_EXERCISE_NAMES: Tuple[str, ...] = (
    "Arnold press",
    "Back squat",
    "Back squat to box",
    "Barbell row",
    "Bench press",
    "Bulgarian split squat",
    "Hex-bar deadlift",
    "Incline bench press",
    "Kettlebell swings",
    "Lat pull-down",
    "Lat pulls",
    "Plank",
    "Pull-ups",
    "Romanian deadlift",
    "Russian twists",
)

GYM_CATEGORY_LEG = "leg"
GYM_CATEGORY_UPPER_CORE = "upper_core"

_SPLIT_RE = re.compile(r"^\d{1,2}:\d{2}$")
_DURATION_MIN_RE = re.compile(
    r"(\d+)\s*[x×]\s*(\d+)\s*min|\b(\d+)\s*min\b",
    re.I,
)
# Legacy shorthand still found in cached plans (new prescriptions must use "min").
_LEGACY_SINGLE_MIN_RE = re.compile(r"^(\d+)\s*m\b", re.I)
_LEGACY_REP_MIN_RE = re.compile(r"(\d+)\s*[x×]\s*(\d+)\s*m\b", re.I)
_DURATION_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_DURATION_DIST_M_RE = re.compile(r"(\d+)\s*[x×]\s*(\d+)\s*m\b", re.I)
_DURATION_DIST_KM_RE = re.compile(
    r"(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*km\b",
    re.I,
)
_INTERVAL_REP_RE = re.compile(r"\d+\s*[x×]\s*\d", re.I)
_REST_IN_TEXT_RE = re.compile(
    r"rest|/\s*\d+\s*min|\d+\s*min\s*r\b",
    re.I,
)

_GYM_EXERCISE_CATEGORY: Dict[str, str] = {
    "Back squat": GYM_CATEGORY_LEG,
    "Back squat to box": GYM_CATEGORY_LEG,
    "Hex-bar deadlift": GYM_CATEGORY_LEG,
    "Romanian deadlift": GYM_CATEGORY_LEG,
    "Bulgarian split squat": GYM_CATEGORY_LEG,
    "Kettlebell swings": GYM_CATEGORY_LEG,
    "Bench press": GYM_CATEGORY_UPPER_CORE,
    "Incline bench press": GYM_CATEGORY_UPPER_CORE,
    "Barbell row": GYM_CATEGORY_UPPER_CORE,
    "Lat pull-down": GYM_CATEGORY_UPPER_CORE,
    "Lat pulls": GYM_CATEGORY_UPPER_CORE,
    "Pull-ups": GYM_CATEGORY_UPPER_CORE,
    "Arnold press": GYM_CATEGORY_UPPER_CORE,
    "Russian twists": GYM_CATEGORY_UPPER_CORE,
    "Plank": GYM_CATEGORY_UPPER_CORE,
}

_GYM_CATEGORY_LABEL = {
    GYM_CATEGORY_LEG: "leg/posterior-chain",
    GYM_CATEGORY_UPPER_CORE: "upper-body/core",
}

_WEEKDAY_NAMES = WEEKDAYS


@dataclass
class GymSet:
    reps: int
    weight_kg: Optional[float] = None
    duration_sec: Optional[int] = None


@dataclass
class GymExercise:
    name: str
    sets: List[GymSet]


@dataclass
class GymSession:
    category: str
    goal: str
    exercises: List[GymExercise]


@dataclass
class RowingSegment:
    phase: str
    label: str
    split_min: str
    split_max: str
    zone_z: str
    zone_t: str
    hr_bpm_min: int
    hr_bpm_max: int
    priority: str
    duration: Optional[str] = None
    notes: Optional[str] = None


@dataclass
class ErgAlternative:
    description: str
    segments: List[RowingSegment]


@dataclass
class RowingSession:
    segments: List[RowingSegment]
    erg_alternative: Optional[ErgAlternative] = None


@dataclass
class DayPlan:
    weekday: str
    date: str
    session_type: str
    session_subtype: Optional[str] = None
    gym: Optional[GymSession] = None
    rowing: Optional[RowingSession] = None
    notes: Optional[str] = None


@dataclass
class WeeklyPlan:
    version: int
    personalised: bool
    days: List[DayPlan]
    greeting: Optional[str] = None


def _gym_set_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "reps": {"type": "integer", "minimum": 1},
            "weight_kg": {"type": ["number", "null"]},
            "duration_sec": {"type": ["integer", "null"]},
        },
        "required": ["reps", "weight_kg", "duration_sec"],
        "additionalProperties": False,
    }


def _gym_exercise_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "enum": list(GYM_EXERCISE_NAMES)},
            "sets": {
                "type": "array",
                "items": _gym_set_schema(),
                "minItems": 1,
            },
        },
        "required": ["name", "sets"],
        "additionalProperties": False,
    }


def _gym_session_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": list(GYM_CATEGORIES)},
            "goal": {"type": "string", "enum": list(GYM_GOALS)},
            "exercises": {
                "type": "array",
                "items": _gym_exercise_schema(),
                "minItems": 4,
                "maxItems": 4,
            },
        },
        "required": ["category", "goal", "exercises"],
        "additionalProperties": False,
    }


def _rowing_segment_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "phase": {"type": "string", "enum": list(ROWING_PHASES)},
            "label": {"type": "string"},
            "duration": {"type": ["string", "null"]},
            "split_min": {
                "type": "string",
                "description": "500m split lower bound M:SS",
            },
            "split_max": {
                "type": "string",
                "description": "500m split upper bound M:SS",
            },
            "zone_z": {"type": "string", "enum": list(ZONE_Z)},
            "zone_t": {"type": "string", "enum": list(ZONE_T)},
            "hr_bpm_min": {"type": "integer", "minimum": 40},
            "hr_bpm_max": {"type": "integer", "minimum": 40},
            "priority": {"type": "string", "enum": list(PRIORITIES)},
            "notes": {"type": ["string", "null"]},
        },
        "required": [
            "phase",
            "label",
            "duration",
            "split_min",
            "split_max",
            "zone_z",
            "zone_t",
            "hr_bpm_min",
            "hr_bpm_max",
            "priority",
            "notes",
        ],
        "additionalProperties": False,
    }


def _erg_alternative_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "segments": {
                "type": "array",
                "items": _rowing_segment_schema(),
                "minItems": 1,
            },
        },
        "required": ["description", "segments"],
        "additionalProperties": False,
    }


def _rowing_session_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "items": _rowing_segment_schema(),
                "minItems": 1,
            },
            "erg_alternative": {
                "anyOf": [_erg_alternative_schema(), {"type": "null"}],
            },
        },
        "required": ["segments", "erg_alternative"],
        "additionalProperties": False,
    }


def _day_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "weekday": {"type": "string", "enum": list(WEEKDAYS)},
            "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
            "session_type": {"type": "string", "enum": list(SESSION_TYPES)},
            "session_subtype": {"type": ["string", "null"]},
            "gym": {"anyOf": [_gym_session_schema(), {"type": "null"}]},
            "rowing": {"anyOf": [_rowing_session_schema(), {"type": "null"}]},
            "notes": {"type": ["string", "null"]},
        },
        "required": [
            "weekday",
            "date",
            "session_type",
            "session_subtype",
            "gym",
            "rowing",
            "notes",
        ],
        "additionalProperties": False,
    }


WEEKLY_PLAN_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "version": {"type": "integer", "const": PLAN_VERSION},
        "personalised": {"type": "boolean"},
        "greeting": {"type": ["string", "null"]},
        "days": {
            "type": "array",
            "items": _day_schema(),
            "minItems": 7,
            "maxItems": 7,
        },
    },
    "required": ["version", "personalised", "greeting", "days"],
    "additionalProperties": False,
}


def openrouter_response_format(*, name: str = "weekly_training_plan") -> Dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": WEEKLY_PLAN_JSON_SCHEMA,
        },
    }


# --- Logged gym session JSON harness (coach-bot / Strava workout transcripts) ---

_LOGGED_GYM_EXERCISE_ALIASES: Dict[str, str] = {
    "bulgarian": "Bulgarian split squat",
    "bulgarian split squats": "Bulgarian split squat",
    "incline bench": "Incline bench press",
    "russian twist": "Russian twists",
    "rdl": "Romanian deadlift",
    "rdls": "Romanian deadlift",
    "romanian dl": "Romanian deadlift",
    "back squats": "Back squat",
    "squat": "Back squat",
    "pull ups": "Pull-ups",
    "pullups": "Pull-ups",
    "pull-ups": "Pull-ups",
}

_LOGGED_GYM_SET_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "reps": {"type": "integer", "minimum": 1},
        "weight_kg": {"type": "number", "minimum": 0},
        "duration_sec": {"type": ["integer", "null"]},
        "rpe": {"type": ["number", "null"], "minimum": 1, "maximum": 10},
    },
    "required": ["reps", "weight_kg", "duration_sec", "rpe"],
    "additionalProperties": False,
}

_LOGGED_GYM_EXERCISE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "enum": list(GYM_EXERCISE_NAMES)},
        "sets": {
            "type": "array",
            "items": _LOGGED_GYM_SET_SCHEMA,
            "minItems": 1,
        },
    },
    "required": ["name", "sets"],
    "additionalProperties": False,
}

GYM_SESSION_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "unit": {"type": "string", "enum": ["kg"]},
        "exercises": {
            "type": "array",
            "items": _LOGGED_GYM_EXERCISE_SCHEMA,
            "minItems": 1,
        },
        "assumptions": {"type": ["string", "null"]},
    },
    "required": ["unit", "exercises", "assumptions"],
    "additionalProperties": False,
}

GYM_SESSION_HARNESS_INSTRUCTIONS = (
    "You tabulate a COMPLETED gym/strength session from an athlete transcript.\n\n"
    "Return ONLY JSON matching the gym_session_log schema (no markdown, no commentary).\n\n"
    "Interpretation rules:\n"
    "1. Weights are kilograms unless explicitly labelled lb/lbs/pounds (convert with ×0.453592).\n"
    "2. Set \"unit\" to \"kg\".\n"
    "3. Map every exercise to one of the approved canonical names in the schema enum "
    "(e.g. \"bulgarian\" → \"Bulgarian split squat\", \"rdl\" → \"Romanian deadlift\").\n"
    "4. Expand shorthand set notation into one JSON object per working set:\n"
    "   - \"8r 70, 90, 6r 100\" → three sets: 8×70, 8×90, 6×100 kg.\n"
    "   - Comma-separated bare numbers inherit the most recent rep count "
    "(after \"8r 70, 90\" the second set is 8×90, not 1×90; "
    "\"20r 7.5, 10\" is 20×7.5 kg then 20×10 kg).\n"
    "   - Log only sets the athlete actually reported — never append extra sets "
    "from examples or prior sessions.\n"
    "   - \"3x10 40\" or \"3×10 40 kg\" → three sets of 10 reps at 40 kg.\n"
    "5. Suunto / bold notes use \"**Exercise:** 8r 40, 5r 50\" — same expansion rules.\n"
    "6. Free-form coach logs often put the exercise name on one line and sets on the next.\n"
    "7. Timed holds (e.g. \"75s\", \"30 sec\") → reps=1, weight_kg=0, duration_sec=<seconds>.\n"
    "   For weighted sets, set duration_sec to null.\n"
    "8. Omit boilerplate lines (\"gym this morning\", goals, session labels).\n"
    "9. Include every weighted working set the athlete reported; do not merge or drop sets.\n"
    "10. Do not compute tonnage — only list sets. Use assumptions for any ambiguity.\n"
    "11. If the athlete gave an effort rating, set rpe (1–10) on that set; "
    "easy≈5, moderate≈7, hard≈8.5, max effort≈10. If none was given, set rpe to null.\n"
)


def openrouter_gym_session_response_format(
    *, name: str = "gym_session_log"
) -> Dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": GYM_SESSION_JSON_SCHEMA,
        },
    }


def canonical_logged_gym_exercise_name(name: str) -> Optional[str]:
    """Map free-text or abbreviated exercise names to approved canonical names."""
    text = re.sub(r"^\d+\.\s*", "", (name or "").strip())
    text = re.sub(r"^\*+|\*+$", "", text).strip().rstrip(":")
    if not text:
        return None
    if text in GYM_EXERCISE_NAMES:
        return text
    lower = text.lower()
    alias = _LOGGED_GYM_EXERCISE_ALIASES.get(lower)
    if alias is not None:
        return alias
    for official in GYM_EXERCISE_NAMES:
        if official.lower() == lower:
            return official
    for official in GYM_EXERCISE_NAMES:
        if re.search(re.escape(official.lower()), lower):
            return official
    for key, official in _LOGGED_GYM_EXERCISE_ALIASES.items():
        if re.search(re.escape(key), lower):
            return official
    return None


def _parse_logged_gym_set(raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        reps = int(raw["reps"])
        weight_kg = float(raw["weight_kg"])
    except (KeyError, TypeError, ValueError):
        return None
    if reps < 1 or weight_kg < 0:
        return None
    duration_raw = raw.get("duration_sec")
    duration_sec: Optional[int]
    if duration_raw is None:
        duration_sec = None
    else:
        try:
            duration_sec = int(duration_raw)
        except (TypeError, ValueError):
            return None
        if duration_sec < 1:
            return None
    row: Dict[str, Any] = {"reps": reps, "weight_kg": weight_kg}
    if duration_sec is not None:
        row["duration_sec"] = duration_sec
    rpe_raw = raw.get("rpe")
    if rpe_raw is not None and rpe_raw != "":
        try:
            rpe = float(rpe_raw)
        except (TypeError, ValueError):
            return None
        if rpe < 1 or rpe > 10:
            return None
        row["rpe"] = rpe
    return row


def _parse_logged_gym_exercise(raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    name = canonical_logged_gym_exercise_name(str(raw.get("name") or ""))
    if name is None:
        return None
    sets_raw = raw.get("sets")
    if not isinstance(sets_raw, list) or not sets_raw:
        return None
    sets: List[Dict[str, Any]] = []
    for item in sets_raw:
        if not isinstance(item, dict):
            return None
        parsed = _parse_logged_gym_set(item)
        if parsed is None:
            return None
        sets.append(parsed)
    return {"name": name, "sets": sets}


def parse_gym_session_harness_json(raw: str) -> Optional[Dict[str, Any]]:
    """Parse and validate structured gym-session JSON from the LLM harness."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if not fence:
            return None
        try:
            data = json.loads(fence.group(1))
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    unit = str(data.get("unit") or "").strip().lower()
    if unit != "kg":
        return None
    exercises_raw = data.get("exercises")
    if not isinstance(exercises_raw, list) or not exercises_raw:
        return None
    exercises: List[Dict[str, Any]] = []
    for item in exercises_raw:
        if not isinstance(item, dict):
            return None
        parsed = _parse_logged_gym_exercise(item)
        if parsed is None:
            return None
        exercises.append(parsed)
    assumptions = data.get("assumptions")
    if assumptions is not None and not isinstance(assumptions, str):
        return None
    return {
        "unit": "kg",
        "exercises": exercises,
        "assumptions": assumptions.strip() if isinstance(assumptions, str) else None,
    }


def _parse_gym_set(raw: Mapping[str, Any]) -> Optional[GymSet]:
    try:
        reps = int(raw["reps"])
    except (KeyError, TypeError, ValueError):
        return None
    if reps < 1:
        return None
    weight = raw.get("weight_kg")
    duration = raw.get("duration_sec")
    weight_kg = float(weight) if weight is not None else None
    duration_sec = int(duration) if duration is not None else None
    return GymSet(reps=reps, weight_kg=weight_kg, duration_sec=duration_sec)


def _parse_gym_exercise(raw: Mapping[str, Any]) -> Optional[GymExercise]:
    name = str(raw.get("name") or "").strip()
    if name not in GYM_EXERCISE_NAMES:
        return None
    sets_raw = raw.get("sets")
    if not isinstance(sets_raw, list) or not sets_raw:
        return None
    sets: List[GymSet] = []
    for item in sets_raw:
        if not isinstance(item, dict):
            return None
        parsed = _parse_gym_set(item)
        if parsed is None:
            return None
        sets.append(parsed)
    return GymExercise(name=name, sets=sets)


def _parse_gym_session(raw: Mapping[str, Any]) -> Optional[GymSession]:
    category = str(raw.get("category") or "")
    goal = str(raw.get("goal") or "")
    if category not in GYM_CATEGORIES or goal not in GYM_GOALS:
        return None
    exercises_raw = raw.get("exercises")
    if not isinstance(exercises_raw, list) or len(exercises_raw) != 4:
        return None
    exercises: List[GymExercise] = []
    for item in exercises_raw:
        if not isinstance(item, dict):
            return None
        parsed = _parse_gym_exercise(item)
        if parsed is None:
            return None
        exercises.append(parsed)
    return GymSession(category=category, goal=goal, exercises=exercises)


def _parse_rowing_segment(raw: Mapping[str, Any]) -> Optional[RowingSegment]:
    phase = str(raw.get("phase") or "")
    if phase not in ROWING_PHASES:
        return None
    zone_z = str(raw.get("zone_z") or "")
    zone_t = str(raw.get("zone_t") or "")
    priority = str(raw.get("priority") or "")
    if zone_z not in ZONE_Z or zone_t not in ZONE_T or priority not in PRIORITIES:
        return None
    try:
        hr_min = int(raw["hr_bpm_min"])
        hr_max = int(raw["hr_bpm_max"])
    except (KeyError, TypeError, ValueError):
        return None
    split_min = str(raw.get("split_min") or "").strip()
    split_max = str(raw.get("split_max") or "").strip()
    if not _SPLIT_RE.match(split_min) or not _SPLIT_RE.match(split_max):
        return None
    label = str(raw.get("label") or "").strip()
    if not label:
        return None
    duration_raw = raw.get("duration")
    notes_raw = raw.get("notes")
    return RowingSegment(
        phase=phase,
        label=label,
        split_min=split_min,
        split_max=split_max,
        zone_z=zone_z,
        zone_t=zone_t,
        hr_bpm_min=hr_min,
        hr_bpm_max=hr_max,
        priority=priority,
        duration=str(duration_raw).strip() if duration_raw else None,
        notes=str(notes_raw).strip() if notes_raw else None,
    )


def _parse_erg_alternative(raw: Mapping[str, Any]) -> Optional[ErgAlternative]:
    description = str(raw.get("description") or "").strip()
    if not description:
        return None
    segments_raw = raw.get("segments")
    if not isinstance(segments_raw, list) or not segments_raw:
        return None
    segments: List[RowingSegment] = []
    for item in segments_raw:
        if not isinstance(item, dict):
            return None
        parsed = _parse_rowing_segment(item)
        if parsed is None:
            return None
        segments.append(parsed)
    return ErgAlternative(description=description, segments=segments)


def _parse_rowing_session(raw: Mapping[str, Any]) -> Optional[RowingSession]:
    segments_raw = raw.get("segments")
    if not isinstance(segments_raw, list) or not segments_raw:
        return None
    segments: List[RowingSegment] = []
    for item in segments_raw:
        if not isinstance(item, dict):
            return None
        parsed = _parse_rowing_segment(item)
        if parsed is None:
            return None
        segments.append(parsed)
    erg_alt: Optional[ErgAlternative] = None
    alt_raw = raw.get("erg_alternative")
    if isinstance(alt_raw, dict):
        erg_alt = _parse_erg_alternative(alt_raw)
        if erg_alt is None:
            return None
    return RowingSession(segments=segments, erg_alternative=erg_alt)


def _parse_day(raw: Mapping[str, Any]) -> Optional[DayPlan]:
    weekday = str(raw.get("weekday") or "")
    session_type = str(raw.get("session_type") or "")
    if weekday not in WEEKDAYS or session_type not in SESSION_TYPES:
        return None
    day_date = str(raw.get("date") or "").strip()
    if not day_date:
        return None
    subtype_raw = raw.get("session_subtype")
    notes_raw = raw.get("notes")
    gym: Optional[GymSession] = None
    rowing: Optional[RowingSession] = None
    gym_raw = raw.get("gym")
    if isinstance(gym_raw, dict):
        gym = _parse_gym_session(gym_raw)
        if gym is None:
            return None
    rowing_raw = raw.get("rowing")
    if isinstance(rowing_raw, dict):
        rowing = _parse_rowing_session(rowing_raw)
        if rowing is None:
            return None
    return DayPlan(
        weekday=weekday,
        date=day_date,
        session_type=session_type,
        session_subtype=str(subtype_raw).strip() if subtype_raw else None,
        gym=gym,
        rowing=rowing,
        notes=str(notes_raw).strip() if notes_raw else None,
    )


def parse_weekly_plan(data: Any) -> Optional[WeeklyPlan]:
    if not isinstance(data, dict):
        return None
    try:
        version = int(data.get("version", 0))
    except (TypeError, ValueError):
        return None
    if version != PLAN_VERSION:
        return None
    personalised = bool(data.get("personalised"))
    greeting_raw = data.get("greeting")
    greeting = str(greeting_raw).strip() if greeting_raw else None
    days_raw = data.get("days")
    if not isinstance(days_raw, list) or len(days_raw) != 7:
        return None
    days: List[DayPlan] = []
    for item in days_raw:
        if not isinstance(item, dict):
            return None
        parsed = _parse_day(item)
        if parsed is None:
            return None
        days.append(parsed)
    return WeeklyPlan(
        version=version,
        personalised=personalised,
        days=days,
        greeting=greeting,
    )


def parse_weekly_plan_json(text: str) -> Optional[WeeklyPlan]:
    text = text.strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parse_weekly_plan(data)


def weekly_plan_to_dict(plan: WeeklyPlan) -> Dict[str, Any]:
    def set_to_dict(s: GymSet) -> Dict[str, Any]:
        return {
            "reps": s.reps,
            "weight_kg": s.weight_kg,
            "duration_sec": s.duration_sec,
        }

    def exercise_to_dict(ex: GymExercise) -> Dict[str, Any]:
        return {"name": ex.name, "sets": [set_to_dict(s) for s in ex.sets]}

    def gym_to_dict(g: GymSession) -> Dict[str, Any]:
        return {
            "category": g.category,
            "goal": g.goal,
            "exercises": [exercise_to_dict(e) for e in g.exercises],
        }

    def segment_to_dict(seg: RowingSegment) -> Dict[str, Any]:
        return {
            "phase": seg.phase,
            "label": seg.label,
            "duration": seg.duration,
            "split_min": seg.split_min,
            "split_max": seg.split_max,
            "zone_z": seg.zone_z,
            "zone_t": seg.zone_t,
            "hr_bpm_min": seg.hr_bpm_min,
            "hr_bpm_max": seg.hr_bpm_max,
            "priority": seg.priority,
            "notes": seg.notes,
        }

    def rowing_to_dict(r: RowingSession) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "segments": [segment_to_dict(s) for s in r.segments],
            "erg_alternative": None,
        }
        if r.erg_alternative:
            out["erg_alternative"] = {
                "description": r.erg_alternative.description,
                "segments": [segment_to_dict(s) for s in r.erg_alternative.segments],
            }
        return out

    return {
        "version": plan.version,
        "personalised": plan.personalised,
        "greeting": plan.greeting,
        "days": [
            {
                "weekday": d.weekday,
                "date": d.date,
                "session_type": d.session_type,
                "session_subtype": d.session_subtype,
                "gym": gym_to_dict(d.gym) if d.gym else None,
                "rowing": rowing_to_dict(d.rowing) if d.rowing else None,
                "notes": d.notes,
            }
            for d in plan.days
        ],
    }


_LOW_INTENSITY_PLAN_PHASES = frozenset({"deload", "recovery", "taper"})


def is_low_intensity_plan_phase(phase: Optional[str]) -> bool:
    """True for deload, recovery, and taper — rowing stays aerobic; no goal-tracking HIT."""
    return (phase or "").strip().lower() in _LOW_INTENSITY_PLAN_PHASES


_LOAD_GYM_PHASES = frozenset({"base", "build"})


def is_load_gym_phase(phase: Optional[str]) -> bool:
    """True for base/build — normal 3–4 working sets per gym exercise."""
    return (phase or "").strip().lower() in _LOAD_GYM_PHASES


def gym_working_set_bounds(phase: Optional[str]) -> tuple[int, int]:
    """Min/max working sets per exercise for a season phase."""
    if is_low_intensity_plan_phase(phase):
        return (2, 2)
    if is_load_gym_phase(phase):
        return (3, 4)
    return (2, 4)


def validate_fixed_weekly_schedule(
    plan: WeeklyPlan,
    *,
    include_lifting: bool = True,
) -> Optional[str]:
    """Enforce Mon/Wed gym, Tue erg, Thu erg/on_water fixed morning schedule."""
    by_wd = {d.weekday: d for d in plan.days}
    if include_lifting:
        for wd in ("Monday", "Wednesday"):
            day = by_wd.get(wd)
            if day is None:
                return f"missing {wd}"
            if day.session_type != "gym":
                return (
                    f"{wd}: must be gym (fixed morning schedule); got {day.session_type}"
                )
            if day.gym is None:
                return f"{wd}: gym session missing gym payload"
    tuesday = by_wd.get("Tuesday")
    if tuesday is None:
        return "missing Tuesday"
    if tuesday.session_type != "erg":
        return f"Tuesday: must be erg; got {tuesday.session_type}"
    if tuesday.rowing is None:
        return "Tuesday: erg day missing rowing payload"
    thursday = by_wd.get("Thursday")
    if thursday is None:
        return "missing Thursday"
    if thursday.session_type not in ("erg", "on_water"):
        return f"Thursday: must be erg or on_water; got {thursday.session_type}"
    if thursday.rowing is None:
        return "Thursday: rowing day missing rowing payload"
    return None


def validate_weekly_plan(plan: WeeklyPlan, *, include_lifting: bool = True) -> Optional[str]:
    """Return an error message if invalid, else None."""
    if len(plan.days) != 7:
        return "expected 7 days"
    seen_weekdays = {d.weekday for d in plan.days}
    if seen_weekdays != set(WEEKDAYS):
        return "days must cover Monday–Sunday exactly once"
    gym_days = [d for d in plan.days if d.session_type == "gym"]
    if include_lifting:
        if len(gym_days) != 2:
            return "expected exactly 2 gym days"
        categories = [d.gym.category for d in gym_days if d.gym]
        if len(categories) != 2:
            return "gym days missing gym payload"
        if categories[0] == categories[1]:
            return "gym days must use different A/B categories"
        for day in gym_days:
            if day.gym is None:
                return f"{day.weekday} gym session missing gym payload"
            if len(day.gym.exercises) != 4:
                return f"{day.weekday} must have exactly 4 exercises"
    err = validate_fixed_weekly_schedule(plan, include_lifting=include_lifting)
    if err:
        return err
    for day in plan.days:
        if day.session_type == "gym":
            if day.gym is None:
                return f"{day.weekday}: gym session_type requires gym object"
            if day.rowing is not None:
                return f"{day.weekday}: gym day must not have rowing"
        elif day.session_type in ("erg", "on_water"):
            if day.rowing is None:
                return f"{day.weekday}: {day.session_type} requires rowing object"
            if day.gym is not None:
                return f"{day.weekday}: rowing day must not have gym"
            phases = {s.phase for s in day.rowing.segments}
            if "warm_up" not in phases or "cool_down" not in phases:
                return f"{day.weekday}: rowing session needs warm_up and cool_down"
            if day.session_type == "on_water" and day.rowing.erg_alternative is None:
                return f"{day.weekday}: on_water requires erg_alternative"
        elif day.session_type in ("rest", "recovery"):
            if day.gym is not None or day.rowing is not None:
                return f"{day.weekday}: rest/recovery must not have gym or rowing"
    return None


def _format_set_line(index: int, s: GymSet) -> str:
    if s.duration_sec is not None:
        return f"Set {index}: {s.duration_sec}s hold"
    if s.weight_kg is not None:
        w = int(s.weight_kg) if s.weight_kg == int(s.weight_kg) else s.weight_kg
        return f"Set {index}: {s.reps}×{w} kg"
    return f"Set {index}: {s.reps} reps"


def _format_gym_session(gym: GymSession) -> List[str]:
    cat_label = _GYM_CATEGORY_LABEL.get(gym.category, gym.category)
    lines = [f"  Goal: {gym.goal} ({cat_label})", ""]
    for i, ex in enumerate(gym.exercises, start=1):
        lines.append(f"  {i}. {ex.name}")
        for j, s in enumerate(ex.sets, start=1):
            lines.append(f"  {_format_set_line(j, s)}")
        if i < len(gym.exercises):
            lines.append("")
    return lines


def _strip_rowing_phase_prefix(label: str) -> str:
    """Remove leading 'Warm Up:' / 'Main Set:' style headings from a label."""
    text = (label or "").strip()
    # Require a colon so bare labels like "Warm-up" are handled separately.
    prefix_re = re.compile(
        r"^(?:"
        r"Warm[\s\-]?Ups?"
        r"|Cool[\s\-]?Downs?"
        r"|Main\s+Sets?(?:\s*\(continued\))?"
        r"|Work|Rest|Build|Active\s+Recovery"
        r")\s*:\s+",
        re.I,
    )
    while True:
        stripped = prefix_re.sub("", text, count=1).strip()
        if stripped == text:
            return text
        text = stripped


_PHASE_SYNONYM_LEAD: Dict[str, re.Pattern[str]] = {
    "warm_up": re.compile(r"^warm[\s\-]?ups?(?:\s*[—\-:]\s*|\s+$)", re.I),
    "cool_down": re.compile(r"^cool[\s\-]?downs?(?:\s*[—\-:]\s*|\s+$)", re.I),
    "main_set": re.compile(r"^main\s+sets?(?:\s*[—\-:]\s*|\s+$)", re.I),
    "work": re.compile(r"^work(?:\s*[—\-:]\s*|\s+$)", re.I),
    "rest": re.compile(r"^rest(?:\s*[—\-:]\s*|\s+$)", re.I),
    "build": re.compile(r"^build(?:\s*[—\-:]\s*|\s+$)", re.I),
}

_PHASE_SYNONYM_ONLY: Dict[str, re.Pattern[str]] = {
    "warm_up": re.compile(r"^warm[\s\-]?ups?$", re.I),
    "cool_down": re.compile(r"^cool[\s\-]?downs?$", re.I),
    "main_set": re.compile(r"^main\s+sets?$", re.I),
    "work": re.compile(r"^work$", re.I),
    "rest": re.compile(r"^rest$", re.I),
    "build": re.compile(r"^build$", re.I),
}


def _strip_phase_synonym_label(label: str, phase: str) -> str:
    """Drop label text that only restates the phase (e.g. 'Warm-up' under warm_up)."""
    text = (label or "").strip()
    only = _PHASE_SYNONYM_ONLY.get(phase)
    if only is not None and only.fullmatch(text):
        return ""
    lead = _PHASE_SYNONYM_LEAD.get(phase)
    if lead is not None:
        stripped = lead.sub("", text, count=1).strip(" -:—,")
        return stripped
    return text


def _replace_trailing_simple_duration(label: str, duration: str) -> Optional[str]:
    """If label ends with '— N min' and duration is 'M min', swap in duration."""
    if not re.fullmatch(r"\d+\s*min", duration, re.I):
        return None
    if not re.search(r"—\s*\d+\s*min\s*$", label, re.I):
        return None
    return re.sub(r"—\s*\d+\s*min\s*$", f"— {duration}", label, flags=re.I)


def _hr_clause_for_segment(
    seg: RowingSegment,
    *,
    absolute_hr_bpm: bool,
) -> str:
    """Squad posts use % max from zone_z; athlete DMs with Max HR use bpm."""
    if absolute_hr_bpm:
        return f"HR {seg.hr_bpm_min}–{seg.hr_bpm_max} bpm"
    from athlete_profile import DEFAULT_FIVE_ZONE_PCT

    key = str(seg.zone_z or "").strip().lower()
    pct = DEFAULT_FIVE_ZONE_PCT.get(key)
    if pct is None:
        return f"HR {seg.hr_bpm_min}–{seg.hr_bpm_max} bpm"
    lo_pct = int(round(pct[0] * 100))
    hi_pct = int(round(pct[1] * 100))
    return f"HR {lo_pct}–{hi_pct}% max"


def _format_segment(
    seg: RowingSegment,
    *,
    absolute_hr_bpm: bool = False,
) -> str:
    phase_label = seg.phase.replace("_", " ").title()
    label = _strip_phase_synonym_label(
        _strip_rowing_phase_prefix(seg.label),
        seg.phase,
    )
    duration = (seg.duration or "").strip()

    if duration and duration not in label:
        replaced = _replace_trailing_simple_duration(label, duration)
        if replaced is not None:
            piece = replaced
        elif re.fullmatch(r"\d+\s*min", duration, re.I) and re.search(
            r"\d+\s*[x×]\s*\d+", label, re.I
        ):
            # Interval structure already in the label; skip total-minute tail.
            piece = label
        elif (
            re.fullmatch(r"\d+\s*min", label, re.I)
            and re.fullmatch(r"\d+\s*min", duration, re.I)
        ):
            piece = duration
        elif label:
            piece = f"{label} — {duration}"
        else:
            piece = duration
    else:
        piece = label or duration or phase_label

    hr_clause = _hr_clause_for_segment(seg, absolute_hr_bpm=absolute_hr_bpm)
    line = (
        f"  {phase_label}: {piece} @ {seg.zone_z}/{seg.zone_t}, "
        f"split {seg.split_min}–{seg.split_max}, {hr_clause}, "
        f"priority: {seg.priority}"
    )
    if seg.notes:
        line += f" ({seg.notes})"
    return line


def _format_rowing_session(
    rowing: RowingSession,
    *,
    on_water: bool,
    absolute_hr_bpm: bool = False,
) -> List[str]:
    lines = [
        _format_segment(seg, absolute_hr_bpm=absolute_hr_bpm)
        for seg in rowing.segments
    ]
    if on_water and rowing.erg_alternative:
        lines.append(f"  Erg alternative: {rowing.erg_alternative.description}")
        for seg in rowing.erg_alternative.segments:
            lines.append(
                "    "
                + _format_segment(seg, absolute_hr_bpm=absolute_hr_bpm).strip()
            )
    return lines


def render_day_text(
    day: DayPlan,
    *,
    absolute_hr_bpm: bool = False,
) -> str:
    parts: List[str] = []
    if day.session_type == "gym" and day.gym:
        parts.append("gym")
        parts.extend(_format_gym_session(day.gym))
    elif day.session_type == "erg" and day.rowing:
        subtype = f" — {day.session_subtype}" if day.session_subtype else ""
        parts.append(f"erg{subtype}")
        parts.extend(
            _format_rowing_session(
                day.rowing, on_water=False, absolute_hr_bpm=absolute_hr_bpm
            )
        )
    elif day.session_type == "on_water" and day.rowing:
        subtype = f" — {day.session_subtype}" if day.session_subtype else ""
        parts.append(f"on-water{subtype}")
        parts.extend(
            _format_rowing_session(
                day.rowing, on_water=True, absolute_hr_bpm=absolute_hr_bpm
            )
        )
    elif day.session_type == "rest":
        parts.append("rest")
    elif day.session_type == "recovery":
        parts.append("optional recovery")
    if day.notes:
        parts.append(f"  Notes: {day.notes}")
    return "\n".join(parts)


def render_plan_text(
    plan: WeeklyPlan,
    *,
    absolute_hr_bpm: bool = False,
) -> str:
    lines: List[str] = []
    if plan.greeting:
        lines.append(plan.greeting.strip())
        lines.append("")
    for day in plan.days:
        header = day.weekday + ":"
        body = render_day_text(day, absolute_hr_bpm=absolute_hr_bpm)
        lines.append(f"{header}\n{body}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def session_for_date(
    plan: Union[WeeklyPlan, Mapping[str, Any]], d: date
) -> Optional[DayPlan]:
    if isinstance(plan, Mapping):
        parsed = parse_weekly_plan(plan)
        if parsed is None:
            return None
        plan_obj = parsed
    else:
        plan_obj = plan
    day_name = _WEEKDAY_NAMES[d.weekday()]
    iso = d.isoformat()
    for day in plan_obj.days:
        if day.weekday == day_name or day.date == iso:
            return day
    return None


def session_tuple_for_date(
    plan_json: Optional[Mapping[str, Any]], d: date
) -> Optional[Tuple[str, str]]:
    if not plan_json:
        return None
    day = session_for_date(plan_json, d)
    if day is None:
        return None
    return day.weekday, render_day_text(day)


def classify_gym_exercise(name: str) -> Optional[str]:
    return _GYM_EXERCISE_CATEGORY.get(name)


def classify_gym_exercise_list(names: Sequence[str]) -> str:
    leg = sum(1 for n in names if classify_gym_exercise(n) == GYM_CATEGORY_LEG)
    upper = sum(
        1 for n in names if classify_gym_exercise(n) == GYM_CATEGORY_UPPER_CORE
    )
    if leg == 0 and upper == 0:
        return "mixed"
    if leg > upper:
        return GYM_CATEGORY_LEG
    if upper > leg:
        return GYM_CATEGORY_UPPER_CORE
    return "mixed"


def extract_gym_exercises_by_day_from_json(
    plan_json: Union[WeeklyPlan, Mapping[str, Any]],
) -> Dict[str, List[str]]:
    if isinstance(plan_json, Mapping):
        parsed = parse_weekly_plan(plan_json)
        if parsed is None:
            return {}
        plan_obj = parsed
    else:
        plan_obj = plan_json
    out: Dict[str, List[str]] = {}
    for day in plan_obj.days:
        if day.session_type == "gym" and day.gym:
            out[day.weekday] = [ex.name for ex in day.gym.exercises]
    return out


def format_previous_week_gym_exercises_from_json(
    plan_json: Union[WeeklyPlan, Mapping[str, Any]],
) -> str:
    by_day = extract_gym_exercises_by_day_from_json(plan_json)
    if not by_day:
        return ""
    lines = ["Previous week gym exercises (reuse exactly on deload weeks):"]
    for day_name, names in by_day.items():
        category = classify_gym_exercise_list(names)
        label = _GYM_CATEGORY_LABEL.get(category, "mixed")
        lines.append(f"- {day_name} ({label} day): {', '.join(names)}")
    lines.append(
        "Gym exercise names come from the squad gym program. Do not rotate or "
        "substitute. Deload/recovery/taper weeks reuse these names exactly."
    )
    return "\n".join(lines)


def _estimate_segment_minutes(duration: Optional[str]) -> int:
    if not duration:
        return 10
    text = duration.strip()
    clock = _DURATION_CLOCK_RE.match(text)
    if clock:
        return int(clock.group(1)) + (1 if int(clock.group(2)) >= 30 else 0)
    meters = _estimate_segment_meters(text)
    if meters >= 500:
        # Distance prescription (e.g. 3x1000m) — estimate work time at ~2:10/500m.
        return max(1, int(round(meters * 130.0 / 500.0 / 60.0)))
    m = _DURATION_MIN_RE.search(text)
    if m:
        if m.group(1) and m.group(2):
            return int(m.group(1)) * int(m.group(2))
        if m.group(3):
            return int(m.group(3))
    legacy = _LEGACY_SINGLE_MIN_RE.match(text)
    if legacy:
        return int(legacy.group(1))
    legacy_rep = _LEGACY_REP_MIN_RE.search(text)
    if legacy_rep and int(legacy_rep.group(2)) < 100:
        return int(legacy_rep.group(1)) * int(legacy_rep.group(2))
    return 10


def _estimate_segment_meters(duration: Optional[str]) -> int:
    if not duration:
        return 0
    text = duration.strip()
    total = 0
    for reps, km in _DURATION_DIST_KM_RE.findall(text):
        total += int(round(float(km) * 1000 * int(reps)))
    for reps, meters in _DURATION_DIST_M_RE.findall(text):
        total += int(reps) * int(meters)
    return total


def _segment_specifies_rest(seg: RowingSegment) -> bool:
    text = f"{seg.label} {seg.duration or ''}"
    return _REST_IN_TEXT_RE.search(text) is not None


def _segment_needs_explicit_rest(
    seg: RowingSegment,
    session_subtype: Optional[str],
) -> bool:
    text = f"{seg.label} {seg.duration or ''}"
    rep = re.search(r"(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(?:min|m|km|sec|s)?", text, re.I)
    if not rep:
        if seg.phase not in {"main_set", "work", "build"}:
            return False
        subtype = str(session_subtype or "").strip().lower()
        label = str(seg.label or "").strip().lower()
        duration = str(seg.duration or "").strip().lower()
        # Continuous threshold/tempo is allowed; only multi-piece "intervals" need
        # explicit reps×structure.
        looks_like_interval_block = (
            "interval" in subtype
            or "interval" in label
            or "interval" in duration
            or "vo2" in label
            or "vo2" in subtype
        )
        return looks_like_interval_block
    # Single continuous pieces (e.g. 1×45 min) do not need inter-piece rest.
    return float(rep.group(1)) > 1


def _rowing_segments_need_rest_validation(
    segments: Sequence[RowingSegment],
    session_subtype: Optional[str],
) -> bool:
    for i, seg in enumerate(segments):
        if not _segment_needs_explicit_rest(seg, session_subtype):
            continue
        text = f"{seg.label} {seg.duration or ''}"
        has_reps = bool(
            re.search(r"(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(?:min|m|km|sec|s)?", text, re.I)
        )
        next_is_rest = i + 1 < len(segments) and segments[i + 1].phase in (
            "rest",
            "active_recovery",
        )
        if not has_reps:
            # Allow work + dedicated rest-phase pairs (erg alternative style).
            if next_is_rest:
                continue
            return True
        if _segment_specifies_rest(seg):
            continue
        if next_is_rest:
            continue
        return True
    return False


def validate_rowing_interval_rest(day: DayPlan) -> Optional[str]:
    """Interval / threshold main work must specify reps×pieces and rest."""
    if day.rowing is None:
        return None
    if _rowing_segments_need_rest_validation(day.rowing.segments, day.session_subtype):
        return (
            "interval/threshold main set must use explicit reps×distance or "
            "reps×duration with rest between pieces (e.g. '5×8 min / 2 min rest')"
        )
    if day.rowing.erg_alternative and _rowing_segments_need_rest_validation(
        day.rowing.erg_alternative.segments,
        day.session_subtype,
    ):
        return (
            "erg alternative interval/threshold work must use explicit "
            "reps×distance or reps×duration with rest between pieces"
        )
    return None


def validate_rowing_zone_zt_compatibility(day: DayPlan) -> Optional[str]:
    if day.rowing is None:
        return None

    def check(segments):
        for seg in segments:
            allowed = ZONE_ZT_COMPATIBLE.get(seg.zone_z)
            if allowed is None or seg.zone_t not in allowed:
                return (
                    f"incompatible zones {seg.zone_z}/{seg.zone_t} — "
                    f"{seg.zone_z} allows {', '.join(sorted(allowed or []))}"
                )
        return None

    err = check(day.rowing.segments)
    if err:
        return err
    if day.rowing.erg_alternative:
        return check(day.rowing.erg_alternative.segments)
    return None


def validate_rowing_phase_intensity(
    day: DayPlan,
    *,
    phase: Optional[str],
) -> Optional[str]:
    if day.rowing is None:
        return None

    phase_norm = (phase or "").strip().lower()
    if not is_low_intensity_plan_phase(phase_norm) and phase_norm != "base":
        return None

    def check(segments: Sequence[RowingSegment]) -> Optional[str]:
        for seg in segments:
            if seg.phase not in {"main_set", "work", "build"}:
                continue
            if phase_norm in {"deload", "recovery"} and (
                seg.zone_t in {"T5", "T6", "T7"}
                or seg.zone_z in {"Z4", "Z5"}
            ):
                return (
                    f"{phase_norm} phase main work cannot use high intensity "
                    f"{seg.zone_z}/{seg.zone_t}"
                )
            if phase_norm in {"base", "taper"} and seg.zone_t in {"T6", "T7"}:
                return (
                    f"{phase_norm} phase main work cannot use high intensity "
                    f"{seg.zone_z}/{seg.zone_t}"
                )
        return None

    err = check(day.rowing.segments)
    if err:
        return err
    if day.rowing.erg_alternative:
        return check(day.rowing.erg_alternative.segments)
    return None


def _ranges_overlap(a_lo: int, a_hi: int, b_lo: int, b_hi: int) -> bool:
    return a_lo <= b_hi and a_hi >= b_lo


def validate_athlete_hr_zone_consistency(
    plan: WeeklyPlan,
    profile: AthleteProfile,
) -> Optional[str]:
    """Require each prescribed HR band to overlap its athlete Z and T bands."""
    if not plan.personalised or profile.max_hr_bpm is None:
        return None

    def check(day: DayPlan, segments: Sequence[RowingSegment]) -> Optional[str]:
        for seg in segments:
            prescribed = (seg.hr_bpm_min, seg.hr_bpm_max)
            if prescribed[0] > prescribed[1]:
                return (
                    f"{day.weekday}: HR minimum {prescribed[0]} bpm exceeds "
                    f"maximum {prescribed[1]} bpm for {seg.label} "
                    f"({seg.zone_z}/{seg.zone_t})"
                )
            z_range = profile.zone_bpm_range(seg.zone_z.lower())
            t_range = profile.zone_bpm_range(seg.zone_t.lower())
            if z_range is None or t_range is None:
                continue
            if not _ranges_overlap(*prescribed, *z_range):
                return (
                    f"{day.weekday}: HR {prescribed[0]}–{prescribed[1]} bpm for "
                    f"{seg.label} does not overlap {seg.zone_z} "
                    f"{z_range[0]}–{z_range[1]} bpm"
                )
            if not _ranges_overlap(*prescribed, *t_range):
                return (
                    f"{day.weekday}: HR {prescribed[0]}–{prescribed[1]} bpm for "
                    f"{seg.label} does not overlap {seg.zone_t} "
                    f"{t_range[0]}–{t_range[1]} bpm"
                )
        return None

    for day in plan.days:
        if day.rowing is None:
            continue
        err = check(day, day.rowing.segments)
        if err:
            return err
        if day.rowing.erg_alternative:
            err = check(day, day.rowing.erg_alternative.segments)
            if err:
                return err
    return None


SESSION_CAP_MINUTES = 45
GYM_SESSION_CAP_MINUTES = 90
WARMUP_COOLDOWN_CAP_MINUTES = 15
WARMUP_COOLDOWN_DEFAULT_MINUTES = (8, 8)
WARMUP_COOLDOWN_FLOOR_MINUTES = 5


def estimate_rowing_session_minutes(rowing: RowingSession) -> int:
    """Warm-up through cool-down for one rowing path (primary segments only).

    On-water days may include an erg_alternative with parallel structure; that is
    a fallback for the same session slot, not additional prescribed time.
    """
    return sum(_estimate_segment_minutes(seg.duration) for seg in rowing.segments)


def estimate_day_session_minutes(day: DayPlan) -> int:
    """Rough warm-up-through-cool-down duration for gym or rowing days."""
    total = 0
    if day.gym:
        total += 10
        for ex in day.gym.exercises:
            total += max(len(ex.sets), 1) * 3
    if day.rowing:
        total += estimate_rowing_session_minutes(day.rowing)
    return total


def validate_plan_session_constraints(
    plan: WeeklyPlan,
    *,
    phase: Optional[str] = None,
) -> Optional[str]:
    """Session duration cap and interval rest requirements."""
    for day in plan.days:
        if day.session_type == "gym":
            mins = estimate_day_session_minutes(day)
            if mins > GYM_SESSION_CAP_MINUTES:
                return (
                    f"{day.weekday}: estimated gym session ~{mins} min exceeds "
                    f"{GYM_SESSION_CAP_MINUTES} min cap"
                )
            continue
        if day.session_type not in ("erg", "on_water"):
            continue
        mins = estimate_rowing_session_minutes(day.rowing) if day.rowing else 0
        if mins > SESSION_CAP_MINUTES:
            return (
                f"{day.weekday}: estimated erg/rowing session ~{mins} min exceeds "
                f"{SESSION_CAP_MINUTES} min cap — "
                "shorten the session or move extra volume to Friday or Saturday"
            )
        if day.session_type in ("erg", "on_water"):
            wucd_err = validate_rowing_warmup_cooldown_caps(day)
            if wucd_err:
                return f"{day.weekday}: {wucd_err}"
            rest_err = validate_rowing_interval_rest(day)
            if rest_err:
                return f"{day.weekday}: {rest_err}"
            zt_err = validate_rowing_zone_zt_compatibility(day)
            if zt_err:
                return f"{day.weekday}: {zt_err}"
            phase_err = validate_rowing_phase_intensity(day, phase=phase)
            if phase_err:
                return f"{day.weekday}: {phase_err}"
    return None


def validate_rowing_warmup_cooldown_caps(day: DayPlan) -> Optional[str]:
    """Reject warm-up or cool-down segments longer than WARMUP_COOLDOWN_CAP_MINUTES."""
    if day.rowing is None:
        return None
    for label, segments in (
        ("primary", day.rowing.segments),
        (
            "erg_alternative",
            day.rowing.erg_alternative.segments if day.rowing.erg_alternative else [],
        ),
    ):
        for seg in segments:
            if seg.phase not in ("warm_up", "cool_down"):
                continue
            mins = _estimate_segment_minutes(seg.duration)
            if mins > WARMUP_COOLDOWN_CAP_MINUTES:
                return (
                    f"{seg.phase} segment ~{mins} min exceeds "
                    f"{WARMUP_COOLDOWN_CAP_MINUTES} min cap ({label})"
                )
    return None


def _zone_bucket(zone_z: str) -> str:
    if zone_z in ("Z1", "Z2", "Z3"):
        return "z2"
    if zone_z == "Z4":
        return "z4"
    if zone_z == "Z5":
        return "z5"
    return "other"


def planned_metrics_from_plan_json(
    plan_json: Union[WeeklyPlan, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Compute season review metrics from structured plan (no LLM)."""
    if isinstance(plan_json, Mapping):
        parsed = parse_weekly_plan(plan_json)
        if parsed is None:
            return {}
        plan_obj = parsed
    else:
        plan_obj = plan_json

    rowing_minutes = 0
    rowing_meters = 0
    z2_minutes = 0
    z4_minutes = 0
    z5_minutes = 0
    gym_tonnage = 0.0

    def _accumulate_rowing_segment(seg: RowingSegment) -> None:
        nonlocal rowing_minutes, rowing_meters, z2_minutes, z4_minutes, z5_minutes
        mins = _estimate_segment_minutes(seg.duration)
        rowing_minutes += mins
        rowing_meters += _estimate_segment_meters(seg.duration)
        bucket = _zone_bucket(seg.zone_z)
        if bucket == "z2":
            z2_minutes += mins
        elif bucket == "z4":
            z4_minutes += mins
        elif bucket == "z5":
            z5_minutes += mins

    for day in plan_obj.days:
        if day.gym:
            for ex in day.gym.exercises:
                for s in ex.sets:
                    if s.weight_kg is not None:
                        gym_tonnage += s.reps * s.weight_kg
        if day.rowing:
            for seg in day.rowing.segments:
                _accumulate_rowing_segment(seg)

    total_row = max(rowing_minutes, 1)
    return {
        "rowing_meters": rowing_meters or None,
        "rowing_minutes": rowing_minutes or None,
        "z2_percent": round(100.0 * z2_minutes / total_row, 1) if rowing_minutes else None,
        "z4_percent": round(100.0 * z4_minutes / total_row, 1) if rowing_minutes else None,
        "z5_percent": round(100.0 * z5_minutes / total_row, 1) if rowing_minutes else None,
        "gym_tonnage_kg": round(gym_tonnage, 1) if gym_tonnage else None,
    }


def format_plan_prescribed_summary(
    plan_json: Union[WeeklyPlan, Mapping[str, Any]],
) -> Optional[str]:
    """Human-readable volume/intensity totals derived from prescribed sessions."""
    metrics = planned_metrics_from_plan_json(plan_json)
    if not metrics.get("rowing_minutes") and not metrics.get("gym_tonnage_kg"):
        return None
    lines = ["_Prescribed volume (summed from sessions above):_"]
    row_parts: List[str] = []
    if metrics.get("rowing_meters"):
        row_parts.append(f"{metrics['rowing_meters'] / 1000:.1f} km")
    if metrics.get("rowing_minutes"):
        row_parts.append(f"{metrics['rowing_minutes']} min rowing")
    if row_parts:
        lines.append("- " + "; ".join(row_parts))
    zone_parts: List[str] = []
    if metrics.get("z2_percent") is not None:
        zone_parts.append(f"Z1–Z3 (aerobic) {metrics['z2_percent']:.0f}%")
    if metrics.get("z4_percent") is not None:
        zone_parts.append(f"Z4 (threshold) {metrics['z4_percent']:.0f}%")
    if metrics.get("z5_percent") is not None:
        zone_parts.append(f"Z5 (race-pace) {metrics['z5_percent']:.0f}%")
    if zone_parts:
        lines.append("- Intensity mix: " + "; ".join(zone_parts))
    if metrics.get("gym_tonnage_kg"):
        lines.append(f"- Gym tonnage: {metrics['gym_tonnage_kg']:.0f} kg")
    return "\n".join(lines)


_INTENSITY_GOAL_RE = re.compile(
    r"1:4\d|1:5[0-9]|race[\s-]?pace|high[\s-]?intensity|vo2|"
    r"interval(?:\s+training)?|speed[\s-]?work|sub[\s-]?1:50|6:40|threshold",
    re.I,
)
_AEROBIC_PACE_GOAL_RE = re.compile(
    r"2:0[0-5]|45[\s-]?min(?:ute)?|continuous\s+row",
    re.I,
)
_MAIN_WORK_PHASES = frozenset({"main_set", "work", "build"})


def _split_to_seconds(split: str) -> int:
    m = re.match(r"^(\d{1,2}):(\d{2})$", (split or "").strip())
    if not m:
        return 9999
    return int(m.group(1)) * 60 + int(m.group(2))


def _is_main_work_segment(phase: str, label: str) -> bool:
    if phase in _MAIN_WORK_PHASES:
        return True
    return "main set" in (label or "").lower()


def goal_tracking_needs_intensity_work(goal_tracking: Optional[str]) -> bool:
    """True when goal-tracking text prescribes race-pace or high-intensity rowing."""
    if not goal_tracking or not goal_tracking.strip():
        return False
    return _INTENSITY_GOAL_RE.search(goal_tracking) is not None


def goal_tracking_needs_aerobic_pace_work(goal_tracking: Optional[str]) -> bool:
    """True when goal-tracking text prescribes tighter aerobic pacing (~2:00)."""
    if not goal_tracking or not goal_tracking.strip():
        return False
    return _AEROBIC_PACE_GOAL_RE.search(goal_tracking) is not None


def _rowing_day_main_work_segments(day: "DayPlan") -> List["RowingSegment"]:
    if day.rowing is None:
        return []
    return [
        seg
        for seg in day.rowing.segments
        if _is_main_work_segment(seg.phase, seg.label)
    ]


def rowing_day_has_intensity_work(day: "DayPlan") -> bool:
    """True when a rowing day's main work includes Z4/Z5 segments."""
    return any(
        seg.zone_z in ("Z4", "Z5") for seg in _rowing_day_main_work_segments(day)
    )


def rowing_day_is_z2_steady_only(day: "DayPlan") -> bool:
    """True when all main-work segments are Z2 (regardless of session_subtype label)."""
    main_segs = _rowing_day_main_work_segments(day)
    if not main_segs:
        return False
    return all(seg.zone_z == "Z2" for seg in main_segs)


def _rowing_day_main_split_min_seconds(day: "DayPlan") -> Optional[int]:
    main_segs = _rowing_day_main_work_segments(day)
    if not main_segs:
        return None
    return min(_split_to_seconds(seg.split_min) for seg in main_segs)


def validate_squad_rowing_aligns_with_goals(
    plan: WeeklyPlan,
    goal_tracking: Optional[str],
    *,
    phase: Optional[str] = None,
) -> Optional[str]:
    """
    Reject squad plans where Tuesday/Thursday rowing contradicts goal-tracking
    prescriptions (e.g. both days Z2 steady-state when race-pace work is required).

    Skipped during deload, recovery, and taper — season phase overrides goal tracking.
    Also skipped during base — quality work is T4 threshold within phase guidance, not
    goal-tracking Z5 race-pace prescriptions.
    """
    if is_low_intensity_plan_phase(phase):
        return None
    if (phase or "").strip().lower() == "base":
        return None
    if not goal_tracking or not goal_tracking.strip():
        return None

    rowing_days = [
        d
        for d in plan.days
        if d.weekday in ("Tuesday", "Thursday") and d.session_type in ("erg", "on_water")
    ]
    if len(rowing_days) < 2:
        return None

    needs_intensity = goal_tracking_needs_intensity_work(goal_tracking)
    needs_aerobic_pace = goal_tracking_needs_aerobic_pace_work(goal_tracking)

    if needs_intensity and not any(rowing_day_has_intensity_work(d) for d in rowing_days):
        return (
            "Tuesday and Thursday rowing lack Z4/Z5 or threshold/interval/race-pace "
            "main work, but season goal tracking prescribes race-pace or high-intensity "
            "intervals."
        )

    if needs_intensity and all(rowing_day_is_z2_steady_only(d) for d in rowing_days):
        return (
            "Both Tuesday and Thursday are Z2 steady-state only, but season goal "
            "tracking calls for race-pace or interval work on at least one day."
        )

    if needs_aerobic_pace:
        tuesday = next((d for d in rowing_days if d.weekday == "Tuesday"), None)
        if tuesday is not None:
            split_min = _rowing_day_main_split_min_seconds(tuesday)
            if split_min is not None and split_min > _split_to_seconds("2:05"):
                return (
                    "Tuesday erg main-set splits are slower than 2:05, but season goal "
                    "tracking prescribes aerobic work near 2:00."
                )

    return None


def validate_athlete_plan_against_squad(
    athlete: WeeklyPlan,
    squad: WeeklyPlan,
) -> Optional[str]:
    """Ensure athlete plan mirrors squad session structure and gym exercise selection."""
    if len(athlete.days) != len(squad.days):
        return "athlete plan day count must match squad"
    for a_day, s_day in zip(athlete.days, squad.days):
        if a_day.weekday != s_day.weekday:
            return f"weekday mismatch: {a_day.weekday} vs {s_day.weekday}"
        if a_day.session_type != s_day.session_type:
            return f"{a_day.weekday}: session_type must match squad"
        if a_day.gym and s_day.gym:
            if a_day.gym.category != s_day.gym.category:
                return (
                    f"{a_day.weekday}: gym category must match squad "
                    f"({s_day.gym.category}, not {a_day.gym.category})"
                )
            a_names = [ex.name for ex in a_day.gym.exercises]
            s_names = [ex.name for ex in s_day.gym.exercises]
            if a_names != s_names:
                return f"{a_day.weekday}: gym exercises must match squad order/names"
    return None
