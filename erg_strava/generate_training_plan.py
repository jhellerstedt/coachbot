# Requires: OPENROUTER_API_KEY env var — https://openrouter.ai/keys
# Optional: OPENROUTER_MODEL (default openrouter/auto)
# LLM calls use OpenRouter chat completions: POST https://openrouter.ai/api/v1/chat/completions
#   Auth:  Authorization: Bearer <OPENROUTER_API_KEY>
#   Body:  {"model": "openrouter/auto", "messages": [{"role": "system"|"user"|"assistant", "content": "..."}]}
#   Resp:  response.json()["choices"][0]["message"]["content"]

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import copy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from athlete_profile import AthleteProfile
from openrouter_client import (
    ChatMessage,
    call_openrouter,
    is_openrouter_error,
    topic_context_to_history,
)
from weekly_plan_schema import (
    GYM_SESSION_HARNESS_INSTRUCTIONS,
    WeeklyPlan,
    format_plan_prescribed_summary,
    format_previous_week_gym_exercises_from_json,
    openrouter_gym_session_response_format,
    openrouter_response_format,
    parse_gym_session_harness_json,
    parse_weekly_plan,
    parse_weekly_plan_json,
    render_plan_text,
    session_tuple_for_date,
    validate_weekly_plan,
    validate_athlete_plan_against_squad,
    weekly_plan_to_dict,
)
from document_router import InputData, process_input

DEFAULT_PLAN_TIMEZONE = "Australia/Melbourne"
_plan_tz = ZoneInfo(DEFAULT_PLAN_TIMEZONE)

# Week boundaries: Monday–Sunday in plan_timezone (default Australia/Melbourne).
# Plan week: Mon–Thu local: this calendar Mon–Sun; Fri–Sun local: next Mon–Sun.
_WEEKDAY_MON = 0
_WEEKDAY_THU = 3

_WINDOW_LABELS = {
    "historical": "12 months vs 6–18 mo ago",
    "six_months": "6 months vs 3–9 mo ago",
    "one_month": "~30 days vs 2–6 wk ago",
}

DEFAULT_GYM_SPORT_TYPES = frozenset(
    {"WeightTraining", "Workout", "Crossfit", "HighIntensityIntervalTraining"}
)
DEFAULT_GYM_NAME_PATTERNS = (
    r"\bgym\b",
    r"\bweight\b",
    r"\blifting\b",
    r"\bstrength\b",
    r"\bsquat\b",
    r"\bdeadlift\b",
)

GYM_METRICS_PARSER_VERSION = "2"
ERG_SCORE_PARSER_VERSION = "2"
LB_TO_KG = 0.453592

_BODYWEIGHT_EXERCISE_RE = re.compile(
    r"\b(pull[- ]?ups?|chin[- ]?ups?|dips?)\b",
    re.I,
)


def is_bodyweight_gym_exercise(name: str) -> bool:
    """True for lifts where load is the athlete's body mass when no weight is given."""
    return bool(_BODYWEIGHT_EXERCISE_RE.search(name or ""))


_PER_LEG_EXERCISE_RE = re.compile(
    r"\b("
    r"bulgarian\s+split\s+squats?|"
    r"single[- ]leg\s+(?:rdl|deadlifts?|squats?|lunges?)|"
    r"split\s+squats?"
    r")\b",
    re.I,
)


def is_per_leg_gym_exercise(name: str) -> bool:
    """True when logged reps are per leg (both legs performed each set)."""
    return bool(_PER_LEG_EXERCISE_RE.search(name or ""))


def _recompute_gym_metrics_from_sets(
    metrics: GymSessionMetrics,
) -> GymSessionMetrics:
    """Derive per-exercise and session tonnage from parsed sets (ignore LLM totals)."""
    exercises: List[GymExerciseMetrics] = []
    for ex in metrics.exercises:
        if ex.sets:
            max_w = max(s.weight_kg for s in ex.sets)
            tonnage = sum(s.reps * s.weight_kg for s in ex.sets)
        else:
            max_w = ex.max_weight_kg
            tonnage = ex.tonnage_kg
        exercises.append(
            GymExerciseMetrics(
                name=ex.name,
                max_weight_kg=max_w,
                tonnage_kg=tonnage,
                sets=ex.sets,
            )
        )
    return GymSessionMetrics(
        activity_id=metrics.activity_id,
        activity_name=metrics.activity_name,
        total_tonnage_kg=sum(e.tonnage_kg for e in exercises),
        exercises=exercises,
        unit=metrics.unit,
        assumptions=metrics.assumptions,
    )


def apply_per_leg_reps_to_gym_session(metrics: GymSessionMetrics) -> GymSessionMetrics:
    """Double reps on unilateral exercises — logged reps are per leg, both legs done."""
    updated: List[GymExerciseMetrics] = []
    per_leg_used = False
    for ex in metrics.exercises:
        if not is_per_leg_gym_exercise(ex.name):
            updated.append(ex)
            continue
        new_sets = [
            GymSetMetrics(reps=s.reps * 2, weight_kg=s.weight_kg) for s in ex.sets
        ]
        if not new_sets:
            updated.append(ex)
            continue
        per_leg_used = True
        max_w = max(s.weight_kg for s in new_sets)
        tonnage = sum(s.reps * s.weight_kg for s in new_sets)
        updated.append(
            GymExerciseMetrics(
                name=ex.name,
                max_weight_kg=max_w,
                tonnage_kg=tonnage,
                sets=new_sets,
            )
        )

    assumptions = metrics.assumptions or ""
    if per_leg_used:
        note = (
            "Unilateral exercises (e.g. Bulgarian split squat): logged reps were per leg; "
            "tonnage counts both legs (reps × 2)."
        )
        assumptions = f"{assumptions} {note}".strip() if assumptions else note

    return GymSessionMetrics(
        activity_id=metrics.activity_id,
        activity_name=metrics.activity_name,
        total_tonnage_kg=sum(e.tonnage_kg for e in updated),
        exercises=updated,
        unit=metrics.unit,
        assumptions=assumptions or None,
    )


def finalize_gym_session_metrics(
    metrics: GymSessionMetrics,
    body_weight_kg: Optional[float] = None,
) -> GymSessionMetrics:
    """Post-parse adjustments: per-leg rep doubling, then bodyweight load fill-in."""
    return apply_athlete_bodyweight_to_gym_session(
        apply_per_leg_reps_to_gym_session(
            _recompute_gym_metrics_from_sets(metrics)
        ),
        body_weight_kg,
    )


def apply_athlete_bodyweight_to_gym_session(
    metrics: GymSessionMetrics,
    body_weight_kg: Optional[float],
) -> GymSessionMetrics:
    """Fill missing set loads on bodyweight exercises using the athlete profile."""
    if body_weight_kg is None or body_weight_kg <= 0:
        return metrics

    updated: List[GymExerciseMetrics] = []
    bodyweight_used = False
    for ex in metrics.exercises:
        if not is_bodyweight_gym_exercise(ex.name):
            updated.append(ex)
            continue
        new_sets: List[GymSetMetrics] = []
        for s in ex.sets:
            w = s.weight_kg if s.weight_kg > 0 else body_weight_kg
            if s.weight_kg <= 0:
                bodyweight_used = True
            new_sets.append(GymSetMetrics(reps=s.reps, weight_kg=w))
        if not new_sets:
            updated.append(ex)
            continue
        max_w = max(s.weight_kg for s in new_sets)
        tonnage = sum(s.reps * s.weight_kg for s in new_sets)
        updated.append(
            GymExerciseMetrics(
                name=ex.name,
                max_weight_kg=max_w,
                tonnage_kg=tonnage,
                sets=new_sets,
            )
        )

    assumptions = metrics.assumptions or ""
    if bodyweight_used:
        note = (
            f"Bodyweight exercises (e.g. pull-ups) counted at athlete body mass "
            f"{body_weight_kg:g} kg."
        )
        assumptions = f"{assumptions} {note}".strip() if assumptions else note

    return GymSessionMetrics(
        activity_id=metrics.activity_id,
        activity_name=metrics.activity_name,
        total_tonnage_kg=sum(e.tonnage_kg for e in updated),
        exercises=updated,
        unit=metrics.unit,
        assumptions=assumptions or None,
    )


@dataclass
class GymSetMetrics:
    reps: int
    weight_kg: float
    rpe: Optional[float] = None


@dataclass
class GymExerciseMetrics:
    name: str
    max_weight_kg: float
    tonnage_kg: float
    sets: List[GymSetMetrics] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        sets_out: List[Dict[str, Any]] = []
        for s in self.sets:
            row: Dict[str, Any] = {"reps": s.reps, "weight_kg": s.weight_kg}
            if s.rpe is not None:
                row["rpe"] = s.rpe
            sets_out.append(row)
        return {
            "name": self.name,
            "max_weight_kg": self.max_weight_kg,
            "tonnage_kg": self.tonnage_kg,
            "sets": sets_out,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GymExerciseMetrics":
        sets: List[GymSetMetrics] = []
        for s in data.get("sets") or []:
            if s.get("reps") is None or s.get("weight_kg") is None:
                continue
            rpe_raw = s.get("rpe")
            rpe = None
            if rpe_raw is not None and rpe_raw != "":
                try:
                    rpe = float(rpe_raw)
                except (TypeError, ValueError):
                    rpe = None
            sets.append(
                GymSetMetrics(
                    reps=int(s["reps"]),
                    weight_kg=float(s["weight_kg"]),
                    rpe=rpe,
                )
            )
        return cls(
            name=str(data.get("name", "")),
            max_weight_kg=float(data.get("max_weight_kg", 0)),
            tonnage_kg=float(data.get("tonnage_kg", 0)),
            sets=sets,
        )


@dataclass
class GymSessionMetrics:
    activity_id: int
    activity_name: str
    total_tonnage_kg: float
    exercises: List[GymExerciseMetrics]
    unit: str = "kg"
    assumptions: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "activity_id": self.activity_id,
            "activity_name": self.activity_name,
            "total_tonnage_kg": self.total_tonnage_kg,
            "unit": self.unit,
            "exercises": [e.to_dict() for e in self.exercises],
            "assumptions": self.assumptions,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GymSessionMetrics":
        return cls(
            activity_id=int(data["activity_id"]),
            activity_name=str(data.get("activity_name", "")),
            total_tonnage_kg=float(data.get("total_tonnage_kg", 0)),
            unit=str(data.get("unit", "kg")),
            exercises=[
                GymExerciseMetrics.from_dict(e) for e in data.get("exercises") or []
            ],
            assumptions=data.get("assumptions"),
        )


def set_plan_timezone(tz_name: str) -> None:
    """Set IANA timezone for plan weeks and activity-to-week matching."""
    global _plan_tz
    _plan_tz = ZoneInfo(tz_name)


def plan_timezone_name() -> str:
    return str(_plan_tz)


def activity_local_date(dt: datetime) -> date:
    """Calendar date of a Strava UTC start in the plan timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_plan_tz).date()


def local_datetime_from_timestamp(ts: float) -> datetime:
    """Convert a Unix epoch (e.g. Zulip message timestamp) to plan-local time."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_plan_tz)


def _local_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(_plan_tz)


def _local_today(now: Optional[datetime] = None) -> date:
    now = now or datetime.now(timezone.utc)
    return activity_local_date(now)


@dataclass(frozen=True)
class WeekBounds:
    """Monday–Sunday local week used for plan cache keys and adherence windows."""

    week_start: date  # Monday
    week_end: date  # Sunday
    week_id: str  # YYYY-MM-DD_YYYY-MM-DD


@dataclass(frozen=True)
class WeeklyPlanRecord:
    week_id: str
    week_start: str
    week_end: str
    plan_text: str
    generated_at: str
    training_summary: str
    include_lifting: bool
    plan_json: Optional[Dict[str, Any]] = None
    adherence_review: Optional[str] = None
    goal_tracking: Optional[str] = None
    gym_tonnage_summary: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "week_id": self.week_id,
            "week_start": self.week_start,
            "week_end": self.week_end,
            "plan_text": self.plan_text,
            "generated_at": self.generated_at,
            "training_summary": self.training_summary,
            "include_lifting": self.include_lifting,
            "adherence_review": self.adherence_review,
            "goal_tracking": self.goal_tracking,
            "gym_tonnage_summary": self.gym_tonnage_summary,
        }
        if self.plan_json is not None:
            out["plan_json"] = self.plan_json
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WeeklyPlanRecord":
        plan_json = data.get("plan_json")
        return cls(
            week_id=str(data["week_id"]),
            week_start=str(data["week_start"]),
            week_end=str(data["week_end"]),
            plan_text=str(data.get("plan_text", "")),
            generated_at=str(data.get("generated_at", "")),
            training_summary=str(data.get("training_summary", "")),
            include_lifting=bool(data.get("include_lifting", True)),
            plan_json=plan_json if isinstance(plan_json, dict) else None,
            adherence_review=data.get("adherence_review"),
            goal_tracking=data.get("goal_tracking"),
            gym_tonnage_summary=data.get("gym_tonnage_summary"),
        )



def _monday_on_or_before(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_bounds_from_monday(monday: date) -> WeekBounds:
    sunday = monday + timedelta(days=6)
    return WeekBounds(
        week_start=monday,
        week_end=sunday,
        week_id=f"{monday.isoformat()}_{sunday.isoformat()}",
    )


def plan_week_bounds(now: Optional[datetime] = None) -> WeekBounds:
    """
    Target week for a new plan. Mon–Thu (local): this calendar Mon–Sun; Fri–Sun: next Mon–Sun.
    """
    today = _local_today(now)
    this_monday = _monday_on_or_before(today)
    if today.weekday() <= _WEEKDAY_THU:
        return week_bounds_from_monday(this_monday)
    return week_bounds_from_monday(this_monday + timedelta(days=7))


def previous_week_bounds(plan_week: WeekBounds) -> WeekBounds:
    prev_monday = plan_week.week_start - timedelta(days=7)
    return week_bounds_from_monday(prev_monday)


def week_contains(bounds: WeekBounds, activity_start: datetime) -> bool:
    d = activity_local_date(activity_start)
    return bounds.week_start <= d <= bounds.week_end


def weekly_plans_dir(cache_dir: Path) -> Path:
    return cache_dir / "weekly_plans"


def weekly_plan_path(cache_dir: Path, week_id: str) -> Path:
    return weekly_plans_dir(cache_dir) / f"{week_id}.json"


def save_weekly_plan(cache_dir: Path, record: WeeklyPlanRecord) -> Path:
    root = weekly_plans_dir(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = weekly_plan_path(cache_dir, record.week_id)
    path.write_text(json.dumps(record.to_dict(), indent=2))
    return path


def load_weekly_plan(cache_dir: Path, week_id: str) -> Optional[WeeklyPlanRecord]:
    path = weekly_plan_path(cache_dir, week_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "week_id" not in data:
        return None
    return WeeklyPlanRecord.from_dict(data)


def list_weekly_plan_records(cache_dir: Path) -> List[Dict[str, Any]]:
    """Load cached squad plan records newest week first, skipping invalid files."""
    root = weekly_plans_dir(cache_dir)
    if not root.is_dir():
        return []
    records: List[Dict[str, Any]] = []
    for path in root.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            records.append(data)
    records.sort(key=lambda record: str(record.get("week_id") or ""), reverse=True)
    return records


def athlete_weekly_plans_dir(cache_dir: Path, athlete_id: int) -> Path:
    return cache_dir / f"athlete_{athlete_id}" / "weekly_plans"


def athlete_weekly_plan_path(
    cache_dir: Path, athlete_id: int, week_id: str
) -> Path:
    return athlete_weekly_plans_dir(cache_dir, athlete_id) / f"{week_id}.json"


def save_athlete_weekly_plan(
    cache_dir: Path,
    athlete_id: int,
    week: "WeekBounds",
    plan_text: str,
    *,
    squad_week_id: Optional[str] = None,
    plan_json: Optional[Dict[str, Any]] = None,
) -> Path:
    """Persist a personalised athlete DM plan so it can benchmark later gym logs."""
    root = athlete_weekly_plans_dir(cache_dir, athlete_id)
    root.mkdir(parents=True, exist_ok=True)
    path = athlete_weekly_plan_path(cache_dir, athlete_id, week.week_id)
    record: Dict[str, Any] = {
        "athlete_id": athlete_id,
        "week_id": week.week_id,
        "week_start": week.week_start.isoformat(),
        "week_end": week.week_end.isoformat(),
        "plan_text": plan_text,
        "squad_week_id": squad_week_id or week.week_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if plan_json is not None:
        record["plan_json"] = plan_json
    path.write_text(json.dumps(record, indent=2))
    return path


def load_athlete_weekly_plan(
    cache_dir: Path, athlete_id: int, week_id: str
) -> Optional[Dict[str, Any]]:
    path = athlete_weekly_plan_path(cache_dir, athlete_id, week_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("plan_text"):
        return None
    if not isinstance(data.get("plan_json"), dict):
        data = ensure_athlete_plan_structured(cache_dir, athlete_id, data)
    return data


def athlete_plan_for_date(
    cache_dir: Path, athlete_id: int, d: date
) -> Optional[Dict[str, Any]]:
    """Personalised athlete plan record for the week containing d, if cached."""
    return load_athlete_weekly_plan(
        cache_dir, athlete_id, week_for_date(d).week_id
    )


def athlete_plan_text_for_date(
    cache_dir: Path, athlete_id: int, d: date
) -> Optional[str]:
    """Personalised athlete plan text for the week containing d, if cached."""
    record = athlete_plan_for_date(cache_dir, athlete_id, d)
    if record is None:
        return None
    text = str(record.get("plan_text") or "").strip()
    return text or None


_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def week_for_date(d: date) -> WeekBounds:
    """Calendar Mon–Sun week containing date d (plan timezone semantics via d itself)."""
    return week_bounds_from_monday(_monday_on_or_before(d))


def plan_for_date(cache_dir: Path, d: date) -> Optional[WeeklyPlanRecord]:
    """Load cached weekly plan for the calendar week containing d."""
    return load_weekly_plan(cache_dir, week_for_date(d).week_id)


def extract_session_for_date(
    plan_text: str, d: date
) -> Optional[Tuple[str, str]]:
    """
    Extract one day's section from plan_text (e.g. 'Monday: …' through next weekday).
    Returns (weekday_name, section_text) or None if not found.
    """
    day_name = _WEEKDAY_NAMES[d.weekday()]
    if not plan_text or not plan_text.strip():
        return None
    others = [n for n in _WEEKDAY_NAMES if n != day_name]
    next_days = "|".join(re.escape(n) for n in others)
    md3_pattern = (
        rf"(?ms)^\s*#{{1,3}}\s*{re.escape(day_name)}\s*,\s*{re.escape(d.isoformat())}"
        rf"\s*\n(.*?)(?=^\s*#{{1,3}}\s*(?:{next_days})\b|\Z)"
    )
    m = re.search(md3_pattern, plan_text)
    if m:
        section = m.group(1).strip()
        if section:
            return day_name, section
    pattern = (
        rf"(?mi)^\s*{re.escape(day_name)}\b[:\s\-–—]*(.*?)"
        rf"(?=^\s*(?:{next_days})\b|\Z)"
    )
    m = re.search(pattern, plan_text, re.DOTALL)
    if not m:
        return None
    section = m.group(1).strip()
    if not section:
        return None
    return day_name, section


def session_from_plan(
    plan_text: str,
    plan_json: Optional[Dict[str, Any]],
    d: date,
) -> Optional[Tuple[str, str]]:
    """Return (weekday_name, session_text) from plan_json first, else plan_text."""
    if plan_json:
        from_json = session_tuple_for_date(plan_json, d)
        if from_json is not None:
            return from_json
    return extract_session_for_date(plan_text, d)


def ensure_athlete_plan_structured(
    cache_dir: Path,
    athlete_id: int,
    record: Dict[str, Any],
    *,
    persist: bool = True,
) -> Dict[str, Any]:
    """Import plan_json from cached plan_text when missing; optionally persist."""
    from weekly_plan_harness import finalize_imported_plan_json, import_prose_plan_json
    from weekly_plan_schema import parse_weekly_plan

    existing = record.get("plan_json")
    if isinstance(existing, dict) and parse_weekly_plan(existing) is not None:
        return record

    plan_text = str(record.get("plan_text") or "").strip()
    week_start = str(record.get("week_start") or record.get("week_id", ""))[:10]
    if not plan_text or len(week_start) < 10:
        return record

    greeting = None
    first = plan_text.splitlines()[0].strip() if plan_text else ""
    if first and not re.match(
        r"^(?:#{1,3}\s*)?(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|\*\*)",
        first,
        re.I,
    ):
        greeting = first

    imported = import_prose_plan_json(
        plan_text,
        week_start=week_start,
        personalised=True,
        greeting=greeting,
    )
    if imported is None:
        return record

    finalized, _err = finalize_imported_plan_json(
        imported,
        include_lifting=True,
        squad_plan_json=None,
        cached_import=True,
    )
    if finalized is None:
        return record

    updated = dict(record)
    updated["plan_json"] = finalized
    if persist:
        week_id = str(record.get("week_id") or "").strip()
        if week_id:
            path = athlete_weekly_plan_path(cache_dir, athlete_id, week_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(updated, indent=2))
    return updated


def plan_record_session_for_date(
    record: WeeklyPlanRecord, d: date
) -> Optional[Tuple[str, str]]:
    return session_from_plan(record.plan_text, record.plan_json, d)


def _today_session_extra_blocks(
    plan_record: WeeklyPlanRecord,
    local_date: date,
    athlete_message: str,
) -> List[str]:
    """Today's session for coach prompts; recovery-gated gym does not rewrite the plan."""
    from dataclasses import replace as dc_replace

    from gym_program import (
        gate_gym_session_for_recovery,
        infer_poor_recovery,
        recovery_gate_note,
    )
    from weekly_plan_schema import render_day_text, session_for_date

    poor = infer_poor_recovery(athlete_message)
    if poor and plan_record.plan_json:
        day = session_for_date(plan_record.plan_json, local_date)
        if day is not None and day.gym is not None:
            gated_day = dc_replace(
                day, gym=gate_gym_session_for_recovery(day.gym, poor_recovery=True)
            )
            blocks = [
                f"--- Today's prescribed session ({day.weekday}) [recovery-gated] ---\n"
                f"{render_day_text(gated_day)}"
            ]
            note = recovery_gate_note(True)
            if note:
                blocks.append(f"--- Recovery gate ---\n{note}")
            return blocks
    today_section = plan_record_session_for_date(plan_record, local_date)
    if not today_section:
        return []
    day_name, section = today_section
    return [f"--- Today's prescribed session ({day_name}) ---\n{section}"]


def plan_adjustments_dir(cache_dir: Path) -> Path:
    return cache_dir / "plan_adjustments"


def _plan_adjustments_pending_path(cache_dir: Path) -> Path:
    return plan_adjustments_dir(cache_dir) / "pending.jsonl"


def _plan_adjustments_consumed_path(cache_dir: Path) -> Path:
    return plan_adjustments_dir(cache_dir) / "consumed.jsonl"


def enqueue_plan_adjustment(
    cache_dir: Path,
    text: str,
    *,
    zulip_message_id: Optional[int] = None,
) -> str:
    """Append an athlete adjustment paragraph; return queue entry id."""
    text = text.strip()
    if not text:
        raise ValueError("adjustment text is empty")
    root = plan_adjustments_dir(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    entry_id = str(uuid.uuid4())
    row = {
        "id": entry_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "text": text,
    }
    if zulip_message_id is not None:
        row["zulip_message_id"] = zulip_message_id
    path = _plan_adjustments_pending_path(cache_dir)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return entry_id


def pending_plan_adjustment_rows(cache_dir: Path) -> List[Dict[str, Any]]:
    """Return pending adjustment records in enqueue order."""
    path = _plan_adjustments_pending_path(cache_dir)
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("text"):
            rows.append(row)
    return rows


def pending_plan_adjustments(cache_dir: Path) -> List[str]:
    """Return pending adjustment texts in enqueue order."""
    return [
        str(row.get("text") or "").strip()
        for row in pending_plan_adjustment_rows(cache_dir)
        if str(row.get("text") or "").strip()
    ]


def format_pending_adjustments_context(cache_dir: Path) -> str:
    """Text block listing queued plan adjustments for coach-bot prompts."""
    rows = pending_plan_adjustment_rows(cache_dir)
    if not rows:
        return (
            "--- Pending plan adjustments (next weekly plan generation) ---\n"
            "(none queued)"
        )
    lines = [
        "--- Pending plan adjustments (next weekly plan generation) ---",
        "These will be applied when strava_erg_hr_plot.py generates the next plan.",
    ]
    for row in rows:
        entry_id = str(row.get("id") or "?")
        created = str(row.get("created_at") or "?")
        text = str(row.get("text") or "").strip()
        lines.append(f"- `{entry_id}` ({created}): {text}")
    return "\n".join(lines)


def consume_plan_adjustments(cache_dir: Path) -> List[str]:
    """
    Move all pending adjustments to consumed.jsonl and clear pending.
    Returns the texts that were consumed.
    """
    pending_path = _plan_adjustments_pending_path(cache_dir)
    if not pending_path.is_file():
        return []
    raw = pending_path.read_text(encoding="utf-8")
    if not raw.strip():
        pending_path.unlink(missing_ok=True)
        return []
    texts: List[str] = []
    consumed_rows: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc).isoformat()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or not row.get("text"):
            continue
        texts.append(str(row["text"]).strip())
        row = dict(row)
        row["consumed_at"] = now
        consumed_rows.append(row)
    if not texts:
        pending_path.unlink(missing_ok=True)
        return []
    root = plan_adjustments_dir(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    consumed_path = _plan_adjustments_consumed_path(cache_dir)
    with consumed_path.open("a", encoding="utf-8") as fh:
        for row in consumed_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp = pending_path.with_suffix(".tmp")
    tmp.write_text("", encoding="utf-8")
    tmp.replace(pending_path)
    return texts


def missing_plan_reply(week_id: str) -> str:
    return (
        f"No cached weekly plan for week `{week_id}`. "
        "Run `python erg_strava/strava_erg_hr_plot.py` (with `OPENROUTER_API_KEY` set, "
        "without `--no-kagi`) to generate one."
    )


def strategic_goals_context() -> str:
    """Season goals and constraints used when coaching via Kagi."""
    return _STRATEGIC_GOALS_CONTEXT


def _resolve_coach_local_datetime(
    *,
    local_date: Optional[date] = None,
    local_datetime: Optional[datetime] = None,
) -> datetime:
    if local_datetime is not None:
        if local_datetime.tzinfo is None:
            local_datetime = local_datetime.replace(tzinfo=timezone.utc)
        return local_datetime.astimezone(_plan_tz)
    local_date = local_date or _local_today()
    return datetime.combine(local_date, datetime.min.time(), tzinfo=_plan_tz)


def build_athlete_training_context_for_coach(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    ref_date: date,
    *,
    recent_limit: int = 8,
) -> str:
    """Per-athlete erg history for coach Q&A (merged sessions when available)."""
    blocks: List[str] = []
    week = week_for_date(ref_date)
    try:
        from erg_session_merge import (
            build_merged_erg_sessions_for_athlete,
            format_merged_erg_session_line,
            load_merged_erg_sessions_for_athlete,
        )

        week_sessions = build_merged_erg_sessions_for_athlete(
            cache_dir,
            athlete_id,
            athlete_label=athlete_label,
            week_start=week.week_start,
            week_end=week.week_end,
        )
        if week_sessions:
            lines = [format_merged_erg_session_line(s) for s in week_sessions]
            blocks.append(
                f"--- {athlete_label}: erg sessions this plan week ({week.week_id}) ---\n"
                + "\n".join(lines)
            )
        recent = load_merged_erg_sessions_for_athlete(
            cache_dir, athlete_id, limit=recent_limit
        )
        if recent and not week_sessions:
            lines = [format_merged_erg_session_line(s) for s in recent]
            blocks.append(
                f"--- {athlete_label}: recent merged erg sessions ---\n"
                + "\n".join(lines)
            )
    except Exception:
        week_scores = load_erg_scores_for_week(cache_dir, athlete_id, week)
        if week_scores:
            lines = [format_erg_score_line(r) for r in week_scores]
            blocks.append(
                f"--- {athlete_label}: erg scores this plan week ({week.week_id}) ---\n"
                + "\n".join(f"- {line}" for line in lines)
            )
        else:
            scores = load_erg_scores_for_athlete(cache_dir, athlete_id, limit=recent_limit)
            if scores:
                lines = [format_erg_score_line(r) for r in scores]
                blocks.append(
                    f"--- {athlete_label}: recent logged erg scores ---\n"
                    + "\n".join(f"- {line}" for line in lines)
                )
    if not blocks:
        return f"--- {athlete_label}: logged erg sessions ---\n(none cached for this athlete)"
    return "\n\n".join(blocks)


def build_coach_message_prompt(
    athlete_message: str,
    plan_record: WeeklyPlanRecord,
    *,
    local_date: Optional[date] = None,
    local_datetime: Optional[datetime] = None,
    topic_context: Optional[str] = None,
    sender_label: Optional[str] = None,
    subject_label: Optional[str] = None,
    subject_training_context: Optional[str] = None,
    subject_hr_context: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    subject_athlete_id: Optional[int] = None,
) -> Tuple[str, str, List[ChatMessage]]:
    """Assemble OpenRouter messages for coach-bot Q&A and general topic messages."""
    athlete_message = athlete_message.strip()
    local_datetime = _resolve_coach_local_datetime(
        local_date=local_date, local_datetime=local_datetime
    )
    local_date = local_datetime.date()
    weekday = _WEEKDAY_NAMES[local_date.weekday()]
    extra_blocks: List[str] = []
    if subject_label:
        if sender_label and sender_label.strip().lower() != subject_label.strip().lower():
            extra_blocks.append(
                f"--- Who this message is about ---\n"
                f"Asker (Zulip sender): {sender_label}\n"
                f"Subject athlete (answer about this person): {subject_label}\n"
                f"Address your reply to {sender_label} but base training facts on "
                f"{subject_label}'s logged sessions—not the asker's unless the question "
                f"is explicitly about the asker."
            )
        else:
            extra_blocks.append(
                f"--- Athlete ---\n"
                f"Subject: {subject_label}\n"
                f"Address your reply to {subject_label} (use their name)."
            )
    if subject_training_context and subject_training_context.strip():
        extra_blocks.append(subject_training_context.strip())
    if subject_hr_context and subject_hr_context.strip():
        extra_blocks.append(subject_hr_context.strip())
    if plan_record.training_summary.strip():
        extra_blocks.append(
            f"--- Training summary (recent erg performance) ---\n"
            f"{plan_record.training_summary.strip()}"
        )
    extra_blocks.extend(
        _today_session_extra_blocks(plan_record, local_date, athlete_message)
    )
    if plan_record.goal_tracking and str(plan_record.goal_tracking).strip():
        extra_blocks.append(
            f"--- Season goal tracking ---\n{str(plan_record.goal_tracking).strip()}"
        )
    history = topic_context_to_history(topic_context or "")
    user_blocks: List[str] = [
        f"Current local time ({plan_timezone_name()}): "
        f"{local_datetime.strftime('%Y-%m-%d %H:%M')} ({weekday}).\n"
        f"Plan week: {plan_record.week_start} to {plan_record.week_end}.",
        f"--- Strategic context ---\n{strategic_goals_context()}",
    ]
    user_blocks.extend(extra_blocks)
    user_blocks.append(
        f"--- Strength programming guidelines ---\n{programming_guidelines_context()}"
    )
    if cache_dir is not None and subject_athlete_id is not None:
        tonnage_summary = get_tonnage_summary(cache_dir, subject_athlete_id)
        if tonnage_summary:
            user_blocks.append(
                f"--- Recent gym tonnage ({subject_label or 'athlete'}) ---\n"
                f"{tonnage_summary}"
            )
    user_blocks.append(
        f"--- Weekly training plan ---\n{plan_record.plan_text.strip()}"
    )
    user_blocks.append(f"--- Athlete message ---\n{athlete_message}")
    system = (
        "You are an expert rowing coach and strength coach. "
        "Respond to the athlete's message using the context in this conversation.\n\n"
        "RULES:\n"
        "- Interpret 'today', 'tomorrow', 'this morning', and similar phrases "
        "relative to the current local time in the user message—not your own clock.\n"
        "- Base answers on the weekly plan and training summary where possible; "
        "do not invent prescribed sessions not in the plan unless the message is "
        "general technique or programming advice clearly separate from scheduling.\n"
        "- Use strategic goals to shape advice but do not recite them verbatim.\n"
        "- When athlete profile HR bpm ranges are present, use those Z1–Z5 and T1–T7 "
        "tables (from Max HR) for every rowing segment—include split range, Z/T zone "
        "codes, explicit bpm, and priority (split or HR) on each component; not "
        "generic zone labels alone.\n"
        "- Use recent Zulip topic messages in the conversation history for extra "
        "context (adjustments, reschedules). If the thread reschedules a session "
        "(e.g. Thursday moved to Friday), honour that schedule.\n"
        "- When a subject athlete is named in context, do not confuse them with "
        "other athletes. Use that athlete's logged sessions for adherence facts.\n"
        "- When advising on gym work, follow the strength programming guidelines and "
        "emphasize 'intent'—explosive concentric speed and load quality rather than "
        "just volume. Reference the athlete's recent tonnage history (provided above) "
        "to ensure progressive overload.\n"
        "- If a recovery-gated today's session is present, use that intensity for "
        "today only; do not treat it as a change to the stored weekly plan.\n"
        "- If the context does not specify something the athlete asks for, say so clearly.\n"
        "- Be concise and actionable. Do not rewrite the full weekly plan."
    )
    return system, "\n\n".join(user_blocks), history


@dataclass
class CoachInterpretation:
    """First-pass LLM routing for coach-bot messages."""

    intent: str  # plan_adjustment | coaching_reply | list_adjustments | gym_session_log | profile_update
    reply: str
    pending_adjustment: Optional[str] = None
    workout_text: Optional[str] = None
    session_date: Optional[str] = None
    body_weight_kg: Optional[float] = None
    max_hr_bpm: Optional[int] = None


def _optional_json_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, str) and value.strip().lower() == "null":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_json_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, str) and value.strip().lower() == "null":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


_SETS_REPS_RE = re.compile(r"\b\d{1,3}\s*r(?:eps?)?\b", re.I)
_SETSxREPS_RE = re.compile(r"\b\d{1,2}\s*[x×]\s*\d{1,3}\b", re.I)
_WORKOUT_CATEGORY_RE = re.compile(
    r"\((?:strength|power|core|hypertrophy|endurance|accessor(?:y|ies))\)", re.I
)
_FUTURE_PLAN_RE = re.compile(
    r"\b(?:next week|next session|upcoming|tomorrow|change(?: the)? plan|"
    r"reschedul\w*|swap|skip|reduce|increase|instead of|move .* to)\b",
    re.I,
)
_ERG_REFERRAL_RE = re.compile(
    r"\b("
    r"erg|concept2|pm5|row(?:ing)?|screenshot|image above|pic above|"
    r"photo above|attachment above|in the image|see my erg|my erg"
    r")\b",
    re.I,
)


def looks_like_completed_workout_text(text: str) -> bool:
    """Heuristic: a logged strength/gym session (sets/reps/weights), not a plan change.

    Conservative — used as a safety net to stop completed-workout transcripts being
    misrouted to plan_adjustment. Requires multiple set/rep signals and no explicit
    request to change future training.
    """
    body = (text or "").strip()
    if len(body) < 12:
        return False
    if _FUTURE_PLAN_RE.search(body):
        return False
    signals = len(_SETS_REPS_RE.findall(body))
    signals += len(_SETSxREPS_RE.findall(body))
    if _WORKOUT_CATEGORY_RE.search(body):
        signals += 1
    return signals >= 2


def looks_like_erg_referral_not_gym_log(text: str) -> bool:
    """Heuristic: athlete is pointing at an erg screenshot, not logging gym work."""
    body = (text or "").strip()
    if not body or not _ERG_REFERRAL_RE.search(body):
        return False
    return not looks_like_completed_workout_text(body)


def build_coach_interpret_prompt(
    athlete_message: str,
    plan_record: WeeklyPlanRecord,
    cache_dir: Path,
    *,
    local_date: Optional[date] = None,
    local_datetime: Optional[datetime] = None,
    topic_context: Optional[str] = None,
    sender_label: Optional[str] = None,
    subject_label: Optional[str] = None,
    subject_training_context: Optional[str] = None,
    subject_hr_context: Optional[str] = None,
    subject_athlete_id: Optional[int] = None,
    private_dm: bool = False,
) -> Tuple[str, str, List[ChatMessage]]:
    """OpenRouter messages: classify intent, answer, and format pending plan adjustments."""
    athlete_message = athlete_message.strip()
    local_datetime = _resolve_coach_local_datetime(
        local_date=local_date, local_datetime=local_datetime
    )
    local_date = local_datetime.date()
    weekday = _WEEKDAY_NAMES[local_date.weekday()]
    extra_blocks: List[str] = [format_pending_adjustments_context(cache_dir)]
    if private_dm:
        extra_blocks.append(
            "--- Channel ---\n"
            "Private DM to the coach bot. The sender is the subject athlete; "
            "use only their logged sessions and address them by name."
        )
    if subject_label:
        if sender_label and sender_label.strip().lower() != subject_label.strip().lower():
            extra_blocks.append(
                f"--- Who this message is about ---\n"
                f"Asker (Zulip sender): {sender_label}\n"
                f"Subject athlete: {subject_label}\n"
                f"If queuing a plan adjustment, write pending_adjustment for "
                f"{subject_label}'s future training unless the asker is changing "
                f"their own plan."
            )
        else:
            extra_blocks.append(
                f"--- Athlete ---\n"
                f"Subject: {subject_label}\n"
                f"Address reply to {subject_label}."
            )
    if subject_training_context and subject_training_context.strip():
        extra_blocks.append(subject_training_context.strip())
    if subject_hr_context and subject_hr_context.strip():
        extra_blocks.append(subject_hr_context.strip())
    if plan_record.training_summary.strip():
        extra_blocks.append(
            f"--- Training summary (recent erg performance) ---\n"
            f"{plan_record.training_summary.strip()}"
        )
    extra_blocks.extend(
        _today_session_extra_blocks(plan_record, local_date, athlete_message)
    )
    if plan_record.goal_tracking and str(plan_record.goal_tracking).strip():
        extra_blocks.append(
            f"--- Season goal tracking ---\n{str(plan_record.goal_tracking).strip()}"
        )
    extra_blocks.append(
        f"--- Strength programming guidelines ---\n{programming_guidelines_context()}"
    )
    if cache_dir is not None and subject_athlete_id is not None:
        tonnage_summary = get_tonnage_summary(cache_dir, subject_athlete_id)
        if tonnage_summary:
            extra_blocks.append(
                f"--- Recent gym tonnage ({subject_label or 'athlete'}) ---\n"
                f"{tonnage_summary}"
            )
    history = topic_context_to_history(topic_context or "")
    user_blocks: List[str] = [
        f"Current local time ({plan_timezone_name()}): "
        f"{local_datetime.strftime('%Y-%m-%d %H:%M')} ({weekday}).\n"
        f"Plan week: {plan_record.week_start} to {plan_record.week_end}.",
        f"--- Strategic context ---\n{strategic_goals_context()}",
    ]
    user_blocks.extend(extra_blocks)
    user_blocks.append(
        f"--- Weekly training plan ---\n{plan_record.plan_text.strip()}"
    )
    user_blocks.append(f"--- Athlete message ---\n{athlete_message}")
    gym_log_rules = (
        "- gym_session_log: athlete reports a COMPLETED gym/strength session with "
        "exercises, sets, reps, and/or weights (including Suunto-style notes like "
        "'**Bench Press:** 8r 40, 5r 50' or pasted workout transcripts). "
        "NOT Concept2 erg scores, NOT questions about future workouts.\n"
        "- coaching_reply: erg screenshot follow-ups (e.g. 'see my erg in the image "
        "above') when no gym set/rep transcript is present.\n"
    )
    gym_log_json = (
        '  "workout_text": "When intent is gym_session_log: the full workout log text '
        'to parse (preserve exercise names and set lines). Otherwise null.",\n'
        '  "session_date": "YYYY-MM-DD when the workout occurred if stated or '
        'inferable; null if today (local time in user message)",\n'
    )
    profile_update_rules = ""
    profile_update_json = ""
    if private_dm:
        profile_update_rules = (
            "- profile_update: athlete reports their body weight and/or max heart rate "
            "to save in config (e.g. 'body weight 82 kg', 'max HR 185', '75kg', "
            "'I'm 178 bpm max'). NOT gym workouts, NOT erg scores, NOT plan changes.\n"
        )
        profile_update_json = (
            '  "body_weight_kg": number or null — body mass in kg if updating weight,\n'
            '  "max_hr_bpm": integer or null — max heart rate in bpm if updating max HR,\n'
        )
    system = (
        "You are an expert rowing coach bot interpreting an athlete's Zulip message. "
        "Decide intent, answer the message, and if they request a FUTURE weekly plan "
        "change, produce text for the plan-adjustment queue.\n\n"
        "INTENT RULES:\n"
        "- plan_adjustment: athlete asks to change an UPCOMING weekly plan (next week, "
        "future schedule)—e.g. reduce volume, swap sessions, skip/add a prescribed day, "
        "reschedule a plan day for a future week. NOT questions about today's workout "
        "unless they explicitly want that day changed on the next generated plan.\n"
        "- list_adjustments: athlete asks what plan adjustments are already queued "
        "pending for the next weekly plan run.\n"
        f"{gym_log_rules}"
        f"{profile_update_rules}"
        "- coaching_reply: everything else (today's session, recovery, technique, "
        "adherence, banter, questions about past sessions).\n\n"
        "OUTPUT: Return ONLY a single JSON object (no markdown fence, no commentary):\n"
        "{\n"
        '  "intent": "plan_adjustment" | "coaching_reply" | "list_adjustments"'
        " | gym_session_log"
        + (" | profile_update" if private_dm else "")
        + ",\n"
        '  "reply": "Concise Zulip reply to the athlete. For plan_adjustment, confirm '
        "what you understood and that it will be applied at the next weekly plan "
        'generation. For list_adjustments, list each queued item from the pending '
        'block in context. For gym_session_log, one short sentence confirming the '
        "session was logged. Do not include log ids, tonnage tables, athlete "
        "headings, prescription checks, or an RPE question — those are appended "
        'separately. For profile_update, confirm the saved '
        'values.",\n'
        f"{gym_log_json}"
        f"{profile_update_json}"
        '  "pending_adjustment": "string or null"\n'
        "}\n\n"
        "pending_adjustment (only when intent is plan_adjustment):\n"
        "- One standalone paragraph for the weekly plan generator (third person, "
        "imperative, self-contained).\n"
        "- Name the athlete(s) affected and the specific plan change (days, sessions, "
        "volume, intensity).\n"
        "- Plain text only—no markdown, bullets, or JSON.\n"
        "- Example: \"Reduce Thursday erg threshold volume by 20% for James Merrett "
        "next plan week due to missed Thursday session; keep Friday as scheduled.\"\n"
        "- null for coaching_reply and list_adjustments.\n\n"
        "workout_text / session_date:\n"
        "- Only populate when intent is gym_session_log.\n"
        "- workout_text must include every exercise and set line from the message.\n"
        "- Keep any RPE lines (e.g. 'RPE 5' or easy/moderate/hard) with the exercise "
        "they follow.\n"
        "- null for other intents.\n\n"
        "body_weight_kg / max_hr_bpm:\n"
        "- Only populate when intent is profile_update.\n"
        "- Set each field the athlete is updating; null for fields not mentioned.\n"
        "- At least one must be a number.\n"
        "- null for other intents.\n\n"
        "Other rules:\n"
        "- Interpret today/tomorrow relative to current local time in the user message.\n"
        "- Use conversation history for reschedules already discussed.\n"
        "- Do not invent queued adjustments; list_adjustments uses only the pending "
        "block in context.\n"
        "- GUARDRAIL: A message reporting a COMPLETED workout (past-tense, with "
        "exercise names and sets/reps/weights like '8r 40' or '3x10') is a log, never "
        "a plan_adjustment. Only choose plan_adjustment when the athlete explicitly "
        "asks to CHANGE future/upcoming training. Never invent reschedule reasons that "
        "the athlete did not state.\n"
        "- When advising on gym work, follow the strength programming guidelines and "
        "emphasize 'intent'—explosive concentric speed and load quality rather than "
        "just volume—and reference the recent gym tonnage history (when provided) for "
        "progressive overload.\n"
        "- Be concise in reply.\n"
        "- For gym_session_log, never ask how the last set felt and never invent a "
        "log id or tonnage table."
    )
    return system, "\n\n".join(user_blocks), history


def interpret_coach_message_with_kagi(
    athlete_message: str,
    plan_record: WeeklyPlanRecord,
    cache_dir: Path,
    token: str,
    *,
    local_date: Optional[date] = None,
    local_datetime: Optional[datetime] = None,
    topic_context: Optional[str] = None,
    sender_label: Optional[str] = None,
    subject_label: Optional[str] = None,
    subject_training_context: Optional[str] = None,
    subject_hr_context: Optional[str] = None,
    subject_athlete_id: Optional[int] = None,
    private_dm: bool = False,
) -> CoachInterpretation:
    """First-pass Kagi: classify message and format pending plan adjustments."""
    athlete_message = athlete_message.strip()
    if not athlete_message:
        return CoachInterpretation(
            intent="coaching_reply",
            reply="Please send a training question or coaching request.",
        )
    system, user, history = build_coach_interpret_prompt(
        athlete_message,
        plan_record,
        cache_dir,
        local_date=local_date,
        local_datetime=local_datetime,
        topic_context=topic_context,
        sender_label=sender_label,
        subject_label=subject_label,
        subject_training_context=subject_training_context,
        subject_hr_context=subject_hr_context,
        subject_athlete_id=subject_athlete_id,
        private_dm=private_dm,
    )
    raw = _call_llm(system, user, token, history=history).strip()
    if raw.startswith("OpenRouter API"):
        return CoachInterpretation(intent="coaching_reply", reply=raw)
    data = _extract_json_object(raw)
    if not data:
        return CoachInterpretation(intent="coaching_reply", reply=raw)
    intent = str(data.get("intent") or "coaching_reply").strip().lower()
    allowed = (
        "plan_adjustment",
        "coaching_reply",
        "list_adjustments",
        "gym_session_log",
    )
    if private_dm:
        allowed = (*allowed, "profile_update")
    if intent not in allowed:
        intent = "coaching_reply"
    reply = str(data.get("reply") or "").strip()
    pending_raw = data.get("pending_adjustment")
    pending_adjustment = (
        str(pending_raw).strip() if pending_raw is not None and str(pending_raw).strip() else None
    )
    workout_raw = data.get("workout_text")
    workout_text = (
        str(workout_raw).strip()
        if workout_raw is not None and str(workout_raw).strip()
        else None
    )
    session_raw = data.get("session_date")
    session_date = (
        str(session_raw).strip()[:10]
        if session_raw is not None and str(session_raw).strip()
        else None
    )
    body_weight_kg = _optional_json_float(data.get("body_weight_kg"))
    max_hr_bpm = _optional_json_int(data.get("max_hr_bpm"))
    if intent == "plan_adjustment" and looks_like_completed_workout_text(athlete_message):
        # Safety net: a logged workout transcript is never a future-plan change.
        # The LLM's reply describes a plan change, so drop it for a neutral
        # acknowledgement; the handler appends the parsed tonnage confirmation.
        intent = "gym_session_log"
        pending_adjustment = None
        reply = "Got it — logging your gym session."
    if intent == "plan_adjustment" and not pending_adjustment:
        intent = "coaching_reply"
    if intent == "gym_session_log":
        pending_adjustment = None
        body_weight_kg = None
        max_hr_bpm = None
        if not workout_text:
            workout_text = athlete_message
    if intent == "gym_session_log" and looks_like_erg_referral_not_gym_log(
        athlete_message
    ):
        intent = "coaching_reply"
        workout_text = None
        session_date = None
        reply = (
            "I need the erg screenshot attached in the same message as @coach, "
            "or wait a moment if you just posted one — I'm still reading it."
        )
    if intent == "profile_update":
        pending_adjustment = None
        workout_text = None
        session_date = None
        if body_weight_kg is None and max_hr_bpm is None:
            intent = "coaching_reply"
    if not reply:
        reply = pending_adjustment or "I could not interpret that request."
    return CoachInterpretation(
        intent=intent,
        reply=reply,
        pending_adjustment=pending_adjustment,
        workout_text=workout_text,
        session_date=session_date,
        body_weight_kg=body_weight_kg,
        max_hr_bpm=max_hr_bpm,
    )


def answer_coach_message(
    athlete_message: str,
    plan_record: WeeklyPlanRecord,
    token: str,
    *,
    local_date: Optional[date] = None,
    local_datetime: Optional[datetime] = None,
    topic_context: Optional[str] = None,
    sender_label: Optional[str] = None,
    subject_label: Optional[str] = None,
    subject_training_context: Optional[str] = None,
    subject_hr_context: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    subject_athlete_id: Optional[int] = None,
) -> str:
    """Answer an athlete message via Kagi (interpret first; queue plan adjustments)."""
    athlete_message = athlete_message.strip()
    if not athlete_message:
        return "Please send a training question or coaching request."
    if cache_dir is not None:
        interpretation = interpret_coach_message_with_kagi(
            athlete_message,
            plan_record,
            cache_dir,
            token,
            local_date=local_date,
            local_datetime=local_datetime,
            topic_context=topic_context,
            sender_label=sender_label,
            subject_label=subject_label,
            subject_training_context=subject_training_context,
            subject_hr_context=subject_hr_context,
        )
        return interpretation.reply
    system, user, history = build_coach_message_prompt(
        athlete_message,
        plan_record,
        local_date=local_date,
        local_datetime=local_datetime,
        topic_context=topic_context,
        sender_label=sender_label,
        subject_label=subject_label,
        subject_training_context=subject_training_context,
        subject_hr_context=subject_hr_context,
        cache_dir=cache_dir,
        subject_athlete_id=subject_athlete_id,
    )
    return _call_llm(system, user, token, history=history)


def answer_training_question(
    question: str,
    plan_record: WeeklyPlanRecord,
    token: str,
    *,
    local_date: Optional[date] = None,
    local_datetime: Optional[datetime] = None,
    topic_context: Optional[str] = None,
) -> str:
    """Answer a training question using Kagi with the cached weekly plan as context."""
    return answer_coach_message(
        question,
        plan_record,
        token,
        local_date=local_date,
        local_datetime=local_datetime,
        topic_context=topic_context,
    )


def _fmt_split(sec: float) -> str:
    """Convert seconds to M:SS string."""
    if not pd.notna(sec):
        return "—"
    m = int(sec // 60)
    s = int(round(sec - 60 * m))
    if s >= 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}"


def _window_mask(df: pd.DataFrame, which: str, now_ts: pd.Timestamp) -> pd.Series:
    """Same boundaries as ``subplot_mask()`` in strava_erg_hr_plot.py."""
    from strava_erg_hr_plot import subplot_mask

    now = now_ts.to_pydatetime()
    return subplot_mask(df, which, now)


def _median_iqr_split(series: pd.Series) -> Tuple[str, str]:
    med = float(series.median())
    q1, q3 = float(series.quantile(0.25)), float(series.quantile(0.75))
    return _fmt_split(med), _fmt_split(q3 - q1)


def _median_iqr_hr(series: pd.Series) -> Tuple[int, int]:
    med = int(round(float(series.median())))
    q1, q3 = series.quantile(0.25), series.quantile(0.75)
    return med, int(round(float(q3 - q1)))


def _intensity_mix(split: pd.Series) -> Tuple[float, float, float]:
    n = len(split)
    if n == 0:
        return 0.0, 0.0, 0.0
    high = (split < 110).sum() / n * 100
    moderate = ((split >= 110) & (split < 130)).sum() / n * 100
    easy = (split >= 130).sum() / n * 100
    return high, moderate, easy


def _summarize_window(sub: pd.DataFrame, label: str) -> str:
    if sub.empty:
        return f"{label}\n  (no erg data in this window)"
    sessions = sub["activity_id"].nunique()
    split_med, split_iqr = _median_iqr_split(sub["split_500"])
    hr_med, hr_iqr = _median_iqr_hr(sub["hr"])
    hi, mod, easy = _intensity_mix(sub["split_500"].astype(float))
    return (
        f"{label}\n"
        f"  Sessions: {sessions}\n"
        f"  500m split — median {split_med}, IQR {split_iqr}\n"
        f"  HR (bpm) — median {hr_med}, IQR {hr_iqr}\n"
        f"  Intensity mix: high (<1:50) {hi:.0f}%, "
        f"moderate (1:50–2:10) {mod:.0f}%, easy (>2:10) {easy:.0f}%"
    )


def build_training_summary(df: pd.DataFrame, now: Optional[datetime] = None) -> str:
    """
    Derive a compact text summary from the erg DataFrame for the same three
    time windows used by subplot_mask() (historical / six_months / one_month).
    """
    return _build_training_summary_from_frame(df, now=now)


def build_athlete_training_summary(
    df: pd.DataFrame,
    athlete_label: str,
    now: Optional[datetime] = None,
) -> str:
    """Per-athlete erg summary (same windows as ``build_training_summary``)."""
    if df.empty or "athlete" not in df.columns:
        return f"{athlete_label}\n  (no erg stream data cached for this athlete)"
    sub = df.loc[df["athlete"] == athlete_label]
    if sub.empty:
        return f"{athlete_label}\n  (no erg stream data cached for this athlete)"
    return _build_training_summary_from_frame(sub, now=now, header=athlete_label)


def _build_training_summary_from_frame(
    df: pd.DataFrame,
    now: Optional[datetime] = None,
    header: Optional[str] = None,
) -> str:
    now = now or datetime.now(timezone.utc)

    sections = []
    from strava_erg_hr_plot import panel_period_labels, panel_period_mask

    for key in ("historical", "six_months", "one_month"):
        label = _WINDOW_LABELS[key]
        if header:
            label = f"{header} — {label}"
        period_sections = []
        for period, period_label in enumerate(panel_period_labels(key, now), start=1):
            sub = df.loc[panel_period_mask(df, key, period, now)]
            block = _summarize_window(sub, period_label)
            period_sections.append("\n".join(f"  {line}" for line in block.split("\n")))
        sections.append(label + "\n" + "\n\n".join(period_sections))
    return "\n\n".join(sections)


def is_gym_activity(
    act: dict,
    gym_types: frozenset,
    name_patterns: Sequence[str],
) -> bool:
    st = act.get("sport_type") or act.get("type") or ""
    if st in gym_types:
        return True
    name = (act.get("name") or "").lower()
    for pat in name_patterns:
        if re.search(pat, name, flags=re.IGNORECASE):
            return True
    return False


DEFAULT_ZULIP_STREAM = "general"
DEFAULT_ZULIP_TOPIC = "project-640"


def latest_session_end_utc(
    activities: Sequence[dict],
    activity_details: Optional[Dict[int, dict]] = None,
) -> Optional[datetime]:
    """UTC end time of the most recent Strava activity (start + moving/elapsed time)."""
    activity_details = activity_details or {}
    best: Optional[datetime] = None
    for act in activities:
        start = _parse_activity_start(act.get("start_date"))
        if start is None:
            continue
        detail = activity_details.get(int(act["id"]), {})
        secs = act.get("moving_time") or act.get("elapsed_time")
        if secs is None:
            secs = detail.get("moving_time") or detail.get("elapsed_time")
        try:
            duration = int(secs) if secs is not None else 0
        except (TypeError, ValueError):
            duration = 0
        end = start + timedelta(seconds=max(0, duration))
        if best is None or end > best:
            best = end
    return best


def _zulip_feedback_since_timestamp(
    week_activities: Sequence[dict],
    activity_details: Dict[int, dict],
    prev_record: Optional[WeeklyPlanRecord],
    now: datetime,
) -> datetime:
    """Anchor for Zulip context: after last logged session, else last plan generation, else 7d."""
    session_end = latest_session_end_utc(week_activities, activity_details)
    if session_end is not None:
        return session_end
    if prev_record and prev_record.generated_at:
        try:
            dt = datetime.fromisoformat(
                prev_record.generated_at.replace("Z", "+00:00")
            )
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass
    return now - timedelta(days=7)


def fetch_zulip_topic_feedback_since_last_session(
    since: datetime,
    *,
    stream: str = DEFAULT_ZULIP_STREAM,
    topic: str = DEFAULT_ZULIP_TOPIC,
    zuliprc_path: Optional[Path] = None,
) -> Optional[str]:
    """
    Zulip messages from stream/topic after ``since``. Returns None if unavailable.
    """
    try:
        import sys

        lighties_dir = Path(__file__).resolve().parent.parent / "lighties"
        if str(lighties_dir) not in sys.path:
            sys.path.insert(0, str(lighties_dir))
        from zulip_topic_context import fetch_topic_context_since
    except ImportError:
        return None

    repo_root = Path(__file__).resolve().parent.parent
    rc = zuliprc_path or (repo_root / "zuliprc")
    if not rc.is_file():
        return None
    try:
        text = fetch_topic_context_since(
            stream,
            topic,
            since,
            zuliprc_path=rc,
        )
    except (OSError, RuntimeError, requests.RequestException) as exc:
        print(f"Zulip topic feedback fetch skipped: {exc}", flush=True)
        return None
    return text.strip() if text and text.strip() else None


def _parse_activity_start(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_duration(act: dict, detail: Optional[dict] = None) -> str:
    detail = detail or {}
    secs = act.get("moving_time") or act.get("elapsed_time")
    if secs is None:
        secs = detail.get("moving_time") or detail.get("elapsed_time")
    if secs is None:
        return "—"
    try:
        s = int(secs)
    except (TypeError, ValueError):
        return "—"
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 60}m"


def format_week_activities_summary(
    activities: Sequence[dict],
    details_by_id: Optional[Dict[int, dict]] = None,
    metrics_by_id: Optional[Mapping[int, Dict[str, Any]]] = None,
) -> str:
    """Human-readable list of Strava activities for adherence review."""
    details_by_id = details_by_id or {}
    metrics_by_id = metrics_by_id or {}
    rows: List[Tuple[datetime, str]] = []
    for act in activities:
        start = _parse_activity_start(act.get("start_date"))
        if start is None:
            continue
        aid = int(act["id"])
        detail = details_by_id.get(aid, {})
        st = act.get("sport_type") or act.get("type") or "?"
        name = act.get("name") or detail.get("name") or "(unnamed)"
        dur = _format_duration(act, detail)
        day = start.astimezone(_plan_tz).strftime("%A %Y-%m-%d")
        line = f"- {day}: {name} ({st}), {dur}"
        desc = (detail.get("description") or "").strip()
        if desc:
            excerpt = desc[:500].replace("\n", " ")
            if len(desc) > 500:
                excerpt += "…"
            line += f"\n  Description: {excerpt}"
        gym = metrics_by_id.get(aid, {}).get("gym")
        if gym:
            line += (
                f"\n  Parsed gym: {gym.get('total_tonnage_kg', 0):.0f} kg total; "
                + ", ".join(
                    f"{ex.get('name')} max {ex.get('max_weight_kg', 0):.0f} kg"
                    for ex in (gym.get("exercises") or [])[:8]
                )
            )
        rowing = metrics_by_id.get(aid, {}).get("rowing")
        if rowing:
            line += (
                f"\n  Erg: median split {rowing.get('median_split_500_fmt', '?')}, "
                f"HR {rowing.get('median_hr', '?')} bpm"
            )
        rows.append((start, line))
    if not rows:
        return "(no Strava activities logged in this week)"
    rows.sort(key=lambda x: x[0])
    return "\n".join(line for _, line in rows)


def collect_gym_descriptions(
    activities: Sequence[dict],
    details_by_id: Dict[int, dict],
    gym_types: frozenset,
    name_patterns: Sequence[str],
    *,
    cache_dir: Optional[Path] = None,
    athlete_id: Optional[int] = None,
) -> List[Tuple[int, str, str]]:
    """Return (activity_id, name, description) for gym sessions with workout text."""
    out: List[Tuple[int, str, str]] = []
    for act in activities:
        if not is_gym_activity(act, gym_types, name_patterns):
            continue
        aid = int(act["id"])
        detail = details_by_id.get(aid, {})
        desc = (detail.get("description") or "").strip()
        if not desc and cache_dir is not None and athlete_id is not None:
            try:
                from gym_suunto import (
                    suunto_gym_description,
                    suunto_gym_description_for_strava,
                )

                suunto_key = act.get("suunto_key")
                if suunto_key:
                    desc = suunto_gym_description(cache_dir, athlete_id, str(suunto_key)) or ""
                if not desc and aid > 0:
                    desc = suunto_gym_description_for_strava(cache_dir, athlete_id, aid) or ""
            except ImportError:
                pass
        if not desc:
            continue
        name = act.get("name") or detail.get("name") or f"activity {aid}"
        out.append((aid, str(name), desc))
    return out


def _apply_session_rpe_from_transcript(
    metrics: GymSessionMetrics, transcript: str
) -> GymSessionMetrics:
    from gym_program import overlay_transcript_rpe_on_record

    rec = {"gym": metrics.to_dict()}
    if overlay_transcript_rpe_on_record(rec, transcript):
        return GymSessionMetrics.from_dict(rec["gym"])
    return metrics


def parse_gym_session_metrics(
    activity_id: int,
    activity_name: str,
    description: str,
    token: str,
    *,
    body_weight_kg: Optional[float] = None,
    parse_errors: Optional[List[str]] = None,
) -> Optional[GymSessionMetrics]:
    """Parse gym sets/reps/tonnage — Suunto, LLM harness, then transcript fallback."""
    desc = (description or "").strip()
    if not desc:
        if parse_errors is not None:
            parse_errors.append("workout text is empty")
        return None
    try:
        from gym_suunto import (
            parse_coach_gym_transcript,
            parse_suunto_gym_description,
            reconcile_gym_metrics_with_transcript,
        )

        parsed = parse_suunto_gym_description(desc)
        if parsed:
            metrics = GymSessionMetrics(
                activity_id=activity_id,
                activity_name=activity_name,
                total_tonnage_kg=parsed.total_tonnage_kg,
                unit=parsed.unit,
                exercises=parsed.exercises,
                assumptions=parsed.assumptions,
            )
            return _apply_session_rpe_from_transcript(
                finalize_gym_session_metrics(metrics, body_weight_kg), desc
            )

        parsed = parse_gym_session_with_llm_harness(
            activity_id, activity_name, desc, token, parse_errors=parse_errors
        )
        if parsed is not None:
            parsed = reconcile_gym_metrics_with_transcript(parsed, desc)
            return _apply_session_rpe_from_transcript(
                finalize_gym_session_metrics(parsed, body_weight_kg), desc
            )

        parsed = parse_coach_gym_transcript(desc)
        if parsed:
            metrics = GymSessionMetrics(
                activity_id=activity_id,
                activity_name="Gym (coach transcript)",
                total_tonnage_kg=parsed.total_tonnage_kg,
                unit=parsed.unit,
                exercises=parsed.exercises,
                assumptions=parsed.assumptions,
            )
            return _apply_session_rpe_from_transcript(
                finalize_gym_session_metrics(metrics, body_weight_kg), desc
            )
    except ImportError as exc:
        if parse_errors is not None:
            parse_errors.append(str(exc))
        return None
    if parse_errors is not None and not parse_errors:
        parse_errors.append("structured parse returned no exercises")
    return None


def activity_metrics_path(metrics_dir: Path, activity_id: int) -> Path:
    return metrics_dir / f"{activity_id}.json"


def description_fingerprint(description: str) -> str:
    return hashlib.sha256(description.encode("utf-8")).hexdigest()[:16]


def load_activity_metrics(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def save_activity_metrics(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2))


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return None


def _normalize_weight_kg(value: float, unit: str) -> float:
    if unit.lower() in ("lb", "lbs", "pound", "pounds"):
        return value * LB_TO_KG
    return value


def gym_session_metrics_from_harness(
    data: Mapping[str, Any],
    activity_id: int,
    activity_name: str,
) -> Optional[GymSessionMetrics]:
    """Build gym metrics from validated LLM harness JSON (tonnage derived from sets)."""
    unit = str(data.get("unit") or "kg")
    exercises: List[GymExerciseMetrics] = []
    for ex in data.get("exercises") or []:
        if not isinstance(ex, dict):
            continue
        sets: List[GymSetMetrics] = []
        for s in ex.get("sets") or []:
            if not isinstance(s, dict):
                continue
            try:
                reps = int(s["reps"])
                w = _normalize_weight_kg(float(s["weight_kg"]), unit)
            except (KeyError, TypeError, ValueError):
                continue
            if reps < 1 or w < 0:
                continue
            rpe_raw = s.get("rpe")
            rpe = None
            if rpe_raw is not None and rpe_raw != "":
                try:
                    rpe = float(rpe_raw)
                except (TypeError, ValueError):
                    rpe = None
            sets.append(GymSetMetrics(reps=reps, weight_kg=w, rpe=rpe))
        if not sets:
            continue
        max_w = max(s.weight_kg for s in sets)
        tonnage = sum(s.reps * s.weight_kg for s in sets)
        exercises.append(
            GymExerciseMetrics(
                name=str(ex.get("name", "unknown")),
                max_weight_kg=max_w,
                tonnage_kg=tonnage,
                sets=sets,
            )
        )
    if not exercises:
        return None
    return GymSessionMetrics(
        activity_id=activity_id,
        activity_name=activity_name,
        total_tonnage_kg=sum(e.tonnage_kg for e in exercises),
        exercises=exercises,
        unit="kg",
        assumptions=data.get("assumptions"),
    )


def parse_gym_session_with_llm_harness(
    activity_id: int,
    activity_name: str,
    description: str,
    token: str,
    *,
    parse_errors: Optional[List[str]] = None,
) -> Optional[GymSessionMetrics]:
    """Tabulate a gym transcript via OpenRouter json_schema harness."""
    if not (token or "").strip():
        if parse_errors is not None:
            parse_errors.append("OPENROUTER_API_KEY is not set")
        return None
    user = (
        f"Activity: {activity_name} (id {activity_id}).\n\n"
        f"--- Workout transcript ---\n{description.strip()}"
    )
    raw = _call_llm(
        GYM_SESSION_HARNESS_INSTRUCTIONS,
        user,
        token,
        response_format=openrouter_gym_session_response_format(),
        timeout=90,
    )
    if is_openrouter_error(raw):
        if parse_errors is not None:
            parse_errors.append(raw.strip())
        return None
    data = parse_gym_session_harness_json(raw)
    if data is None:
        if parse_errors is not None:
            snippet = raw.strip().replace("\n", " ")[:240]
            parse_errors.append(
                "LLM response was not valid gym_session_log JSON"
                + (f" ({snippet})" if snippet else "")
            )
        return None
    metrics = gym_session_metrics_from_harness(data, activity_id, activity_name)
    if metrics is None and parse_errors is not None:
        parse_errors.append("LLM JSON had no usable weighted sets")
    return metrics


def parse_gym_session_with_kagi(
    activity_id: int,
    activity_name: str,
    description: str,
    token: str,
) -> Optional[GymSessionMetrics]:
    """Backward-compatible alias for :func:`parse_gym_session_with_llm_harness`."""
    return parse_gym_session_with_llm_harness(
        activity_id, activity_name, description, token
    )


def erg_scores_dir(cache_dir: Path, athlete_id: int) -> Path:
    return cache_dir / f"athlete_{athlete_id}" / "erg_scores"


def erg_score_path(cache_dir: Path, athlete_id: int, score_id: str) -> Path:
    return erg_scores_dir(cache_dir, athlete_id) / f"{score_id}.json"


def find_erg_score_by_zulip_message(
    cache_dir: Path, athlete_id: int, zulip_message_id: int
) -> Optional[Dict[str, Any]]:
    scores_dir = erg_scores_dir(cache_dir, athlete_id)
    if not scores_dir.is_dir():
        return None
    for path in scores_dir.glob("*.json"):
        try:
            rec = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if int(rec.get("zulip_message_id") or 0) == zulip_message_id:
            return rec
    return None


def find_erg_score_by_id(
    cache_dir: Path, athlete_id: int, score_id: str
) -> Optional[Dict[str, Any]]:
    path = erg_score_path(cache_dir, athlete_id, score_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def find_erg_score_for_reaction_message(
    cache_dir: Path, message_id: int
) -> Optional[Tuple[int, Dict[str, Any]]]:
    """Find erg log by coach confirmation id or the athlete's upload message id."""
    found = find_erg_score_by_coach_reply_message(cache_dir, message_id)
    if found is not None:
        return found
    for athlete_id in iter_cached_athlete_ids(cache_dir):
        rec = find_erg_score_by_zulip_message(cache_dir, athlete_id, message_id)
        if rec is not None:
            return athlete_id, rec
    return None


def find_erg_score_by_coach_reply_message(
    cache_dir: Path, coach_reply_message_id: int
) -> Optional[Tuple[int, Dict[str, Any]]]:
    """Return (athlete_id, record) for a coach-bot log confirmation message."""
    for athlete_id in iter_cached_athlete_ids(cache_dir):
        scores_dir = erg_scores_dir(cache_dir, athlete_id)
        if not scores_dir.is_dir():
            continue
        for path in scores_dir.glob("*.json"):
            try:
                rec = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            if (
                int(rec.get("coach_reply_zulip_message_id") or 0)
                == coach_reply_message_id
            ):
                return athlete_id, rec
    return None


def set_erg_score_coach_reply_message_id(
    cache_dir: Path,
    athlete_id: int,
    score_id: str,
    coach_reply_message_id: int,
) -> None:
    path = erg_score_path(cache_dir, athlete_id, score_id)
    if not path.is_file():
        return
    try:
        rec = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    rec["coach_reply_zulip_message_id"] = int(coach_reply_message_id)
    path.write_text(json.dumps(rec, indent=2))


def _clear_erg_score_merge_linkbacks(
    cache_dir: Path, athlete_id: int, record: Dict[str, Any]
) -> None:
    """Remove merge backlinks left on Strava activity metrics."""
    aid = record.get("merged_strava_activity_id")
    score_id = str(record.get("id") or "")
    if aid is None or not score_id:
        return
    metrics_path = activity_metrics_path(
        cache_dir / f"athlete_{athlete_id}" / "metrics", int(aid)
    )
    metrics = load_activity_metrics(metrics_path)
    if not metrics:
        return
    if str(metrics.get("merged_zulip_score_id") or "") != score_id:
        return
    metrics.pop("merged_zulip_score_id", None)
    metrics.pop("merged_at", None)
    save_activity_metrics(metrics_path, metrics)


def delete_erg_score_record(
    cache_dir: Path,
    athlete_id: int,
    score_id: str,
    *,
    athlete_label: str = "",
) -> bool:
    """Delete a logged erg score and rebuild merged sessions. Returns True if removed."""
    path = erg_score_path(cache_dir, athlete_id, score_id)
    if not path.is_file():
        return False
    try:
        record = json.loads(path.read_text())
    except json.JSONDecodeError:
        record = {}
    _clear_erg_score_merge_linkbacks(cache_dir, athlete_id, record)
    path.unlink()
    try:
        from erg_session_merge import rebuild_merged_erg_sessions_for_athlete

        rebuild_merged_erg_sessions_for_athlete(
            cache_dir,
            athlete_id,
            athlete_label=athlete_label or str(record.get("athlete_label") or ""),
        )
    except Exception:
        pass
    return True


def _erg_score_recency_key(record: Dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("recorded_at") or ""),
        str(record.get("zulip_message_id") or ""),
        str(record.get("id") or ""),
    )


def dedupe_erg_scores_by_session_date(
    records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Keep only the latest erg log per session_date (re-logs supersede earlier attempts)."""
    by_date: Dict[str, Dict[str, Any]] = {}
    undated: List[Dict[str, Any]] = []
    for rec in records:
        session_date = str(rec.get("session_date") or "").strip()
        if not session_date:
            undated.append(rec)
            continue
        prev = by_date.get(session_date)
        if prev is None or _erg_score_recency_key(rec) > _erg_score_recency_key(prev):
            by_date[session_date] = rec
    out = list(by_date.values()) + undated
    out.sort(
        key=lambda r: (
            str(r.get("session_date") or ""),
            *_erg_score_recency_key(r),
        )
    )
    return out


def supersede_older_erg_scores_for_date(
    cache_dir: Path,
    athlete_id: int,
    session_date: str,
    keep_score_id: str,
    *,
    athlete_label: str = "",
) -> int:
    """Delete other erg logs for the same calendar day when a session is re-logged."""
    session_date = str(session_date or "").strip()
    keep_score_id = str(keep_score_id or "").strip()
    if not session_date or not keep_score_id:
        return 0
    removed = 0
    scores_dir = erg_scores_dir(cache_dir, athlete_id)
    if not scores_dir.is_dir():
        return 0
    for path in scores_dir.glob("*.json"):
        try:
            rec = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        score_id = str(rec.get("id") or path.stem)
        if score_id == keep_score_id:
            continue
        if str(rec.get("session_date") or "").strip() != session_date:
            continue
        label = athlete_label or str(rec.get("athlete_label") or "")
        if delete_erg_score_record(
            cache_dir, athlete_id, score_id, athlete_label=label
        ):
            removed += 1
    return removed


def save_erg_score_record(
    cache_dir: Path,
    athlete_id: int,
    record: Dict[str, Any],
) -> Path:
    score_id = str(record.get("id") or uuid.uuid4())
    record["id"] = score_id
    path = erg_score_path(cache_dir, athlete_id, score_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2))
    session_date = str(record.get("session_date") or "").strip()
    if session_date:
        supersede_older_erg_scores_for_date(
            cache_dir,
            athlete_id,
            session_date,
            score_id,
            athlete_label=str(record.get("athlete_label") or ""),
        )
    return path


def load_erg_scores_for_week(
    cache_dir: Path,
    athlete_id: int,
    week: WeekBounds,
) -> List[Dict[str, Any]]:
    scores_dir = erg_scores_dir(cache_dir, athlete_id)
    if not scores_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in scores_dir.glob("*.json"):
        try:
            rec = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        session_date_raw = rec.get("session_date")
        session_date: Optional[date] = None
        if session_date_raw:
            try:
                session_date = date.fromisoformat(str(session_date_raw))
            except ValueError:
                session_date = None
        if session_date is not None:
            if week.week_start <= session_date <= week.week_end:
                out.append(rec)
            continue
        recorded = _parse_activity_start(rec.get("recorded_at"))
        if recorded is not None and week_contains(week, recorded):
            out.append(rec)
    out.sort(
        key=lambda r: (
            str(r.get("session_date") or ""),
            str(r.get("recorded_at") or ""),
        )
    )
    return out


def iter_cached_athlete_ids(cache_dir: Path) -> List[int]:
    ids: List[int] = []
    for path in cache_dir.glob("athlete_*"):
        if not path.is_dir():
            continue
        suffix = path.name.split("_", 1)[-1]
        if suffix.isdigit():
            ids.append(int(suffix))
    return sorted(ids)


def format_manual_erg_scores_summary(
    cache_dir: Path,
    week: WeekBounds,
) -> str:
    """Manual erg scores logged via Zulip screenshots (for adherence review)."""
    lines: List[str] = []
    for athlete_id in iter_cached_athlete_ids(cache_dir):
        for rec in load_erg_scores_for_week(cache_dir, athlete_id, week):
            metrics = rec.get("metrics") or {}
            label = rec.get("athlete_label") or f"athlete_{athlete_id}"
            session_day = rec.get("session_date") or "?"
            distance = metrics.get("distance_m")
            split_fmt = metrics.get("avg_split_500_fmt") or metrics.get("avg_split_500_sec")
            hr = metrics.get("avg_hr")
            workout = metrics.get("workout_type") or "erg"
            detail = f"{workout}"
            if distance:
                detail += f", {distance:g} m"
            if split_fmt:
                detail += f", avg split {split_fmt}"
            if hr:
                detail += f", avg HR {hr} bpm"
            summary = str(rec.get("summary") or "").strip()
            line = f"- {session_day} ({label}): {detail}"
            if summary:
                line += f" — {summary[:240]}"
            lines.append(line)
    if not lines:
        return ""
    return "\n".join(lines)


def _extract_erg_ocr_hints(ocr_text: str) -> str:
    """Pull high-confidence numbers/patterns from noisy OCR for the Kagi prompt."""
    text = ocr_text or ""
    hints: List[str] = []

    if re.search(r"4\s*[x×]\s*6[:\.]?\s*0{2}", text, re.I) or re.search(
        r"4\s*[rx]\s*6\s*0{2}\s*/\s*3", text, re.I
    ):
        hints.append(
            "Workout prescription looks like 4×6:00 time intervals (with rest), "
            "NOT 600 m or 5×600 m distance reps."
        )

    if re.search(r"24\s*:\s*0{2}", text):
        hints.append(
            "Summary/work total time ~24:00.0 → duration_sec should be 1440 "
            "(work intervals only, not rest)."
        )
    if re.search(r"36\s*:\s*0{2}", text):
        hints.append(
            "Total elapsed ~36:00.0 includes rest; still use 24:00.0 work total "
            "for duration_sec when both appear."
        )

    if re.search(r"\b6177\b", text):
        hints.append("Total work distance 6177 m appears in OCR — use for distance_m.")

    total_distances = sorted(
        {
            int(m)
            for m in re.findall(r"\b([56]\d{3})\b", text)
            if 5000 <= int(m) <= 6999
        }
    )
    if total_distances and 6177 not in total_distances:
        hints.append(
            f"Possible total work distances (m): {total_distances} — prefer the "
            "summary-row total over summing intervals."
        )

    interval_m = sorted(
        {
            int(m)
            for m in re.findall(r"\b(1[45]\d{2})\b", text)
            if 1500 <= int(m) <= 1600
        }
    )
    if interval_m:
        hints.append(
            f"Per-interval distances (m) visible in OCR: {interval_m} — use these "
            "for interval distance_m; do NOT substitute 600 m from '6:00.0' times."
        )

    splits = re.findall(r"1\s*:\s*5[0-9](?:[.,]\d)?", text)
    if splits:
        hints.append(
            f"Pace tokens resembling /500m splits: {splits[:8]} — parse as M:SS.s."
        )

    if not hints:
        return ""
    return "--- OCR hints (deterministic pre-parse) ---\n" + "\n".join(
        f"- {h}" for h in hints
    )


def parse_erg_score_with_kagi(
    ocr_text: str,
    token: str,
    *,
    athlete_label: str = "",
    session_hint_date: Optional[date] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Parse Concept2 / erg monitor OCR text via Kagi FastGPT.
    Returns (metrics dict, athlete-facing summary).
    """
    hint = ""
    if session_hint_date is not None:
        hint = f"Message sent on local date {session_hint_date.isoformat()}.\n"
    ocr_hints = _extract_erg_ocr_hints(ocr_text)
    ocr_hints_block = f"{ocr_hints}\n\n" if ocr_hints else ""
    system = (
        "You are an expert rowing coach parsing OCR text from a Concept2 erg monitor "
        "screenshot (PM3/PM4/PM5 View Detail or ErgData-style display).\n\n"
        "PARSING RULES (strict):\n"
        "- On Concept2 View Detail screens, the FIRST summary row after the column "
        "headers (time / meter / /500m / s/m) is TOTAL WORK for all intervals — "
        "prefer its meter and time values for distance_m and duration_sec.\n"
        "- Do NOT infer distance reps from '6:00.0' interval times or '600' substrings. "
        "6:00.0 means six minutes, not 600 metres.\n"
        "- Prescriptions like '4x6:00/3:00r' are TIME intervals with rest, not "
        "4×600 m or 5×600 m.\n"
        "- Per-interval distance_m must come from the meter column (typically "
        "1500–1600 m for 6:00 pieces), not rounded to 600.\n"
        "- duration_sec is total WORK time (e.g. 24:00.0 → 1440), not elapsed "
        "including rest (e.g. 36:00.0) unless work total is missing.\n"
        "- Only include intervals explicitly supported by OCR rows; do not invent "
        "extra reps to force a round count.\n"
        "- On View Detail screens, the rightmost column (heart icon) shows heart rate "
        "in bpm when a HR monitor is connected. Read avg_hr from the summary row and "
        "per-interval avg_hr from each interval row when visible.\n"
        "- Use null for fields not visible; never fabricate totals by multiplying "
        "guessed rep count × guessed distance.\n\n"
        "Extract workout metrics from the OCR text. Return ONLY a single JSON object "
        "(no markdown, no commentary):\n"
        "{\n"
        '  "session_date": "YYYY-MM-DD or null",\n'
        '  "distance_m": number or null,\n'
        '  "duration_sec": number or null,\n'
        '  "avg_split_500_sec": number or null,\n'
        '  "avg_split_500_fmt": "M:SS.s string",\n'
        '  "avg_hr": number or null,\n'
        '  "stroke_rate": number or null,\n'
        '  "workout_type": "steady|intervals|2k_test|race_pace|other",\n'
        '  "intervals": [{"label": string, "distance_m": number or null, '
        '"time_sec": number or null, "split_500_sec": number or null, '
        '"stroke_rate": number or null, "avg_hr": number or null}],\n'
        '  "summary": "1-3 concise sentences for the athlete on what they did '
        'and how it compares to typical training",\n'
        '  "assumptions": string or null\n'
        "}\n"
        "Use null for fields not visible in the OCR. Prefer session_date from the "
        "display if shown; otherwise infer from the hint date."
    )
    user_parts = [f"Athlete: {athlete_label or 'unknown'}."]
    if hint:
        user_parts.append(hint.strip())
    if ocr_hints_block:
        user_parts.append(ocr_hints_block.strip())
    user_parts.append(f"--- OCR text ---\n{ocr_text.strip()}")
    raw = _call_llm(system, "\n\n".join(user_parts), token)
    data = _extract_json_object(raw)
    if not data:
        return None, raw.strip() or "Could not parse erg score from screenshot."
    summary = str(data.get("summary") or "").strip()
    if not summary:
        summary = "Logged erg session from screenshot."
    metrics = {
        "session_date": data.get("session_date"),
        "distance_m": data.get("distance_m"),
        "duration_sec": data.get("duration_sec"),
        "avg_split_500_sec": data.get("avg_split_500_sec"),
        "avg_split_500_fmt": data.get("avg_split_500_fmt"),
        "avg_hr": data.get("avg_hr"),
        "stroke_rate": data.get("stroke_rate"),
        "workout_type": data.get("workout_type"),
        "intervals": data.get("intervals") or [],
        "assumptions": data.get("assumptions"),
    }
    if metrics.get("avg_split_500_sec") and not metrics.get("avg_split_500_fmt"):
        try:
            metrics["avg_split_500_fmt"] = _fmt_split(float(metrics["avg_split_500_sec"]))
        except (TypeError, ValueError):
            pass
    return metrics, summary


_ERG_VISION_SYSTEM_PROMPT = (
    "You are an expert sports data analyst reading a Concept2 ergometer screenshot "
    "(PM3/PM4/PM5 View Detail or ErgData-style display) directly from the image — "
    "there is no OCR text, so rely entirely on your own visual reading.\n\n"
    "PARSING RULES (strict):\n"
    "- On Concept2 View Detail screens, the FIRST summary row after the column "
    "headers (time / meter / /500m / s/m) is TOTAL WORK for all intervals — "
    "prefer its meter and time values for distance_m and duration_sec.\n"
    "- '6:00.0' interval times mean six minutes, not 600 metres; do not infer "
    "distance reps from interval times or '600' substrings.\n"
    "- Prescriptions like '4x6:00/3:00r' are TIME intervals with rest, not "
    "4×600 m or 5×600 m.\n"
    "- Per-interval distance_m must come from the meter column (typically "
    "1500–1600 m for 6:00 pieces), not rounded to 600.\n"
    "- duration_sec is total WORK time (e.g. 24:00.0 → 1440), not elapsed time "
    "including rest unless the work total is missing.\n"
    "- Only include intervals you can actually read; never invent extra reps to "
    "force a round count, and never fabricate totals by multiplying guessed reps "
    "× guessed distance.\n"
    "- On View Detail screens, the rightmost column (heart icon) shows heart rate "
    "in bpm when a HR monitor is connected. Read avg_hr from the summary row and "
    "per-interval avg_hr from each interval row when visible.\n"
    "- If the image is blurry, cropped, or the core data (distance/duration/split) "
    "is not legible, DO NOT guess. Return {\"error\": \"<short reason>\"} only.\n\n"
    "Return ONLY a single JSON object (no markdown, no commentary). On success:\n"
    "{\n"
    '  "session_date": "YYYY-MM-DD or null",\n'
    '  "distance_m": number or null,\n'
    '  "duration_sec": number or null,\n'
    '  "avg_split_500_sec": number or null,\n'
    '  "avg_split_500_fmt": "M:SS.s string",\n'
    '  "avg_hr": number or null,\n'
    '  "stroke_rate": number or null,\n'
    '  "workout_type": "steady|intervals|2k_test|race_pace|other",\n'
    '  "intervals": [{"label": string, "distance_m": number or null, '
    '"time_sec": number or null, "split_500_sec": number or null, '
    '"stroke_rate": number or null, "avg_hr": number or null}],\n'
    '  "summary": "1-3 concise sentences for the athlete on what they did and '
    'how it compares to typical training",\n'
    '  "assumptions": string or null\n'
    "}\n"
    "On failure, return ONLY: {\"error\": \"<short reason the screenshot is "
    "unreadable>\"}.\n"
    "Use null for fields not visible. Prefer session_date shown on the display; "
    "otherwise infer from the hint date."
)

_ERG_VISION_PART_SCREEN_SUFFIX = (
    "\n\nMULTI-SCREENSHOT CONTEXT:\n"
    "- This image is ONE of several screenshots from the same erg session upload.\n"
    "- Extract ONLY metrics visible on THIS screen (one piece, one interval block, "
    "or one View Detail table).\n"
    "- Do NOT infer or fabricate data from other parts of the session.\n"
    "- If this screen shows only part of the workout, that is expected — still "
    "return what you can read."
)

_ERG_SYNTHESIS_SYSTEM_PROMPT = (
    "You combine multiple Concept2 erg screenshot extractions into ONE training "
    "session record.\n\n"
    "Each extraction came from a separate screenshot in upload order (index 0 = "
    "first attachment). Infer how they fit together — e.g. warmup + main piece + "
    "cooldown, or several interval screens from one workout — using volume, "
    "intensity, structure, athlete message text, and any prescribed session.\n\n"
    "RULES:\n"
    "- session-level distance_m and duration_sec are total WORK across distinct "
    "parts (sum when each screen is a separate piece).\n"
    "- Do NOT double-count if two screens show the same cumulative View Detail "
    "total for the full session — keep the most complete extraction and note "
    "duplication in assumptions.\n"
    "- avg_split_500_sec / avg_split_500_fmt: session average weighted by distance "
    "or time when combining parts; null if not meaningful.\n"
    "- avg_hr: session average weighted by work time when combining parts; derive "
    "from per-interval avg_hr when the summary row HR is blank.\n"
    "- session_parts: one entry per logical part with role "
    "(warmup|main|cooldown|interval_block|test|steady|other), screenshot_index "
    "(0-based), and that part's distance_m, duration_sec, splits, hr, spm.\n"
    "- intervals: interval rows when visible; label with part context when helpful.\n"
    "- Align part roles to the prescribed session when it clearly lists warmup / "
    "main / cooldown or similar blocks.\n"
    "- When the athlete caption labels screenshots (warmup / main set / cooldown), "
    "map session_parts roles to upload order and those labels.\n"
    "- Preserve session_date from the most reliable extraction.\n\n"
    "Return ONLY a single JSON object (no markdown). On success:\n"
    "{\n"
    '  "session_date": "YYYY-MM-DD or null",\n'
    '  "distance_m": number or null,\n'
    '  "duration_sec": number or null,\n'
    '  "avg_split_500_sec": number or null,\n'
    '  "avg_split_500_fmt": "M:SS.s string",\n'
    '  "avg_hr": number or null,\n'
    '  "stroke_rate": number or null,\n'
    '  "workout_type": "steady|intervals|2k_test|race_pace|other",\n'
    '  "session_parts": [{"role": string, "screenshot_index": int, "label": string '
    "or null, \"distance_m\": number or null, \"duration_sec\": number or null, "
    '"avg_split_500_sec": number or null, "avg_split_500_fmt": string or null, '
    '"avg_hr": number or null, "stroke_rate": number or null}],\n'
    '  "intervals": [{"label": string, "distance_m": number or null, '
    '"time_sec": number or null, "split_500_sec": number or null, '
    '"stroke_rate": number or null, "avg_hr": number or null}],\n'
    '  "summary": "1-3 concise sentences describing the full session",\n'
    '  "assumptions": string or null\n'
    "}\n"
    "On failure return ONLY: {\"error\": \"<short reason>\"}."
)


def _metrics_from_erg_parse_data(data: Dict[str, Any]) -> Dict[str, Any]:
    from erg_hr_enrich import enrich_erg_metrics_hr

    metrics = {
        "session_date": data.get("session_date"),
        "distance_m": data.get("distance_m"),
        "duration_sec": data.get("duration_sec"),
        "avg_split_500_sec": data.get("avg_split_500_sec"),
        "avg_split_500_fmt": data.get("avg_split_500_fmt"),
        "avg_hr": data.get("avg_hr"),
        "stroke_rate": data.get("stroke_rate"),
        "workout_type": data.get("workout_type"),
        "intervals": data.get("intervals") or [],
        "assumptions": data.get("assumptions"),
    }
    if data.get("session_parts") is not None:
        metrics["session_parts"] = data.get("session_parts") or []
    if metrics.get("avg_split_500_sec") and not metrics.get("avg_split_500_fmt"):
        try:
            metrics["avg_split_500_fmt"] = _fmt_split(float(metrics["avg_split_500_sec"]))
        except (TypeError, ValueError):
            pass
    for part in metrics.get("session_parts") or []:
        if not isinstance(part, dict):
            continue
        if part.get("avg_split_500_sec") and not part.get("avg_split_500_fmt"):
            try:
                part["avg_split_500_fmt"] = _fmt_split(float(part["avg_split_500_sec"]))
            except (TypeError, ValueError):
                pass
    return enrich_erg_metrics_hr(metrics)


def _parse_vision_erg_response(raw: str) -> Tuple[Optional[Dict[str, Any]], str]:
    data = _extract_json_object(raw)
    if not data:
        return None, raw.strip() or "Could not read erg score from screenshot."
    error = data.get("error")
    if error:
        return None, str(error).strip() or "Screenshot was not legible."
    summary = str(data.get("summary") or "").strip()
    if not summary:
        summary = "Logged erg session from screenshot."
    return _metrics_from_erg_parse_data(data), summary


def parse_erg_score_from_image(
    image_bytes: bytes,
    token: str,
    *,
    image_mime: str = "image/png",
    athlete_label: str = "",
    session_hint_date: Optional[date] = None,
    part_of_multi: bool = False,
    part_index: int = 0,
    part_total: int = 1,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Parse a Concept2 erg screenshot by sending the image straight to a vision MLLM
    via OpenRouter (no local OCR). Returns (metrics dict, athlete-facing summary),
    using the same JSON schema as :func:`parse_erg_score_with_kagi`.
    """
    user_parts = [f"Athlete: {athlete_label or 'unknown'}."]
    if session_hint_date is not None:
        user_parts.append(
            f"Message sent on local date {session_hint_date.isoformat()}."
        )
    if part_of_multi:
        user_parts.append(
            f"This is screenshot {part_index + 1} of {part_total} attached to "
            "the same message."
        )
        user_parts.append(
            "Extract the erg workout metrics visible on THIS screenshot only."
        )
    else:
        user_parts.append("Extract the erg workout metrics from the attached screenshot.")
    vision_prompt = _ERG_VISION_SYSTEM_PROMPT
    if part_of_multi:
        vision_prompt = _ERG_VISION_SYSTEM_PROMPT + _ERG_VISION_PART_SCREEN_SUFFIX
    result = process_input(
        InputData(
            text="\n\n".join(user_parts),
            image_bytes=image_bytes,
            image_mime=image_mime,
        ),
        api_key=token,
        vision_system_prompt=vision_prompt,
    )
    return _parse_vision_erg_response(result.response)


def synthesize_erg_score_from_extractions(
    extractions: Sequence[Dict[str, Any]],
    token: str,
    *,
    athlete_label: str = "",
    session_hint_date: Optional[date] = None,
    athlete_message: str = "",
    prescribed_session_text: str = "",
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Second-pass text model call: merge per-screenshot metrics into one session."""
    usable = [
        ex
        for ex in extractions
        if ex.get("metrics") and isinstance(ex.get("metrics"), dict)
    ]
    if len(usable) < 2:
        raise ValueError("synthesize_erg_score_from_extractions requires >=2 usable extractions")

    payload = []
    for ex in extractions:
        row: Dict[str, Any] = {
            "screenshot_index": ex.get("index"),
            "summary": ex.get("summary") or "",
        }
        if ex.get("error"):
            row["error"] = ex["error"]
        elif ex.get("metrics"):
            row["metrics"] = ex["metrics"]
        else:
            row["error"] = ex.get("error") or "no metrics extracted"
        payload.append(row)

    user_parts = [f"Athlete: {athlete_label or 'unknown'}."]
    if session_hint_date is not None:
        user_parts.append(
            f"Message sent on local date {session_hint_date.isoformat()}."
        )
    if athlete_message.strip():
        user_parts.append(f"Athlete message text:\n{athlete_message.strip()}")
        caption = athlete_message.strip().lower()
        if any(
            token in caption
            for token in (
                "warmup",
                "warm up",
                "warm-up",
                "cooldown",
                "cool down",
                "cool-down",
                "main set",
                "mainset",
            )
        ):
            user_parts.append(
                "IMPORTANT: The athlete caption labels screenshot parts "
                "(warmup / main / cooldown). Align session_parts roles with upload "
                "order and those labels."
            )
    if prescribed_session_text.strip():
        user_parts.append(
            f"--- Prescribed session for this day ---\n{prescribed_session_text.strip()}"
        )
    user_parts.append(
        "--- Per-screenshot extractions (upload order) ---\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    raw = _call_llm(_ERG_SYNTHESIS_SYSTEM_PROMPT, "\n\n".join(user_parts), token)
    metrics, summary = _parse_vision_erg_response(raw)
    if metrics is None:
        return None, summary
    from erg_hr_enrich import enrich_erg_metrics_hr

    metrics = enrich_erg_metrics_hr(metrics, extractions)
    if not metrics.get("session_parts"):
        metrics["session_parts"] = [
            {
                "role": "other",
                "screenshot_index": ex.get("index"),
                "label": None,
                **{
                    k: ex["metrics"].get(k)
                    for k in (
                        "distance_m",
                        "duration_sec",
                        "avg_split_500_sec",
                        "avg_split_500_fmt",
                        "avg_hr",
                        "stroke_rate",
                    )
                    if ex.get("metrics")
                },
            }
            for ex in usable
        ]
    return metrics, summary


def parse_erg_scores_from_images(
    images: Sequence[Tuple[bytes, str]],
    token: str,
    *,
    athlete_label: str = "",
    session_hint_date: Optional[date] = None,
    athlete_message: str = "",
    prescribed_session_text: str = "",
) -> Tuple[Optional[Dict[str, Any]], str, List[Dict[str, Any]]]:
    """
    Parse one or more erg screenshots. Multiple images get per-screen vision
    extraction then a synthesis pass to infer session structure (warmup / main /
    cooldown, etc.). Returns (metrics, summary, per_screenshot_extractions).
    """
    if not images:
        return None, "No screenshots provided.", []

    total = len(images)
    extractions: List[Dict[str, Any]] = []
    for idx, (image_bytes, image_mime) in enumerate(images):
        metrics, summary = parse_erg_score_from_image(
            image_bytes,
            token,
            image_mime=image_mime,
            athlete_label=athlete_label,
            session_hint_date=session_hint_date,
            part_of_multi=total > 1,
            part_index=idx,
            part_total=total,
        )
        extraction: Dict[str, Any] = {
            "index": idx,
            "summary": summary,
            "raw_summary": summary,
        }
        if metrics is None:
            extraction["error"] = summary
        else:
            from erg_hr_enrich import enrich_erg_metrics_hr

            metrics = enrich_erg_metrics_hr(metrics)
            extraction["metrics"] = metrics
        extractions.append(extraction)

    usable = [ex for ex in extractions if ex.get("metrics")]
    if not usable:
        errors = [str(ex.get("error") or "unreadable") for ex in extractions]
        return None, "; ".join(errors) or "Could not read any screenshot.", extractions

    if len(usable) == 1:
        ex = usable[0]
        return ex["metrics"], str(ex.get("summary") or ""), extractions

    metrics, summary = synthesize_erg_score_from_extractions(
        extractions,
        token,
        athlete_label=athlete_label,
        session_hint_date=session_hint_date,
        athlete_message=athlete_message,
        prescribed_session_text=prescribed_session_text,
    )
    if metrics is None:
        # Fall back to concatenating usable parts without synthesis.
        combined_summary = " ".join(
            str(ex.get("summary") or "").strip() for ex in usable if ex.get("summary")
        ).strip()
        return (
            usable[0]["metrics"],
            summary or combined_summary or "Logged erg session from screenshots.",
            extractions,
        )
    from erg_hr_enrich import enrich_erg_metrics_hr

    metrics = enrich_erg_metrics_hr(metrics, extractions)
    return metrics, summary, extractions


def _persist_parsed_erg_score(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    metrics: Dict[str, Any],
    summary: str,
    raw_text: str,
    *,
    source: str,
    zulip_message_id: Optional[int] = None,
    zulip_sender_email: str = "",
    recorded_at: Optional[datetime] = None,
    session_hint_date: Optional[date] = None,
    screenshot_extractions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    recorded_at = recorded_at or datetime.now(timezone.utc)
    session_date = metrics.get("session_date") or (
        session_hint_date.isoformat() if session_hint_date else None
    )
    record: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "athlete_id": athlete_id,
        "athlete_label": athlete_label,
        "recorded_at": recorded_at.astimezone(timezone.utc).isoformat(),
        "session_date": session_date,
        "source": source,
        "zulip_message_id": zulip_message_id,
        "zulip_sender_email": zulip_sender_email,
        "ocr_text": raw_text[:8000],
        "metrics": metrics,
        "summary": summary,
        "parser_version": ERG_SCORE_PARSER_VERSION,
        "coaching_elaboration_pending": True,
    }
    if screenshot_extractions:
        record["screenshot_extractions"] = screenshot_extractions
    save_erg_score_record(cache_dir, athlete_id, record)
    try:
        from erg_session_merge import rebuild_merged_erg_sessions_for_athlete

        rebuild_merged_erg_sessions_for_athlete(
            cache_dir,
            athlete_id,
            athlete_label=athlete_label,
        )
    except Exception:
        pass
    return record


def record_erg_score_from_screenshot(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    ocr_text: str,
    token: str,
    *,
    zulip_message_id: Optional[int] = None,
    zulip_sender_email: str = "",
    recorded_at: Optional[datetime] = None,
    session_hint_date: Optional[date] = None,
) -> Tuple[Dict[str, Any], str]:
    """Parse OCR via Kagi, persist under athlete cache, return (record, summary)."""
    if zulip_message_id is not None:
        existing = find_erg_score_by_zulip_message(
            cache_dir, athlete_id, zulip_message_id
        )
        if existing:
            return existing, str(existing.get("summary") or "Already logged this score.")

    metrics, summary = parse_erg_score_with_kagi(
        ocr_text,
        token,
        athlete_label=athlete_label,
        session_hint_date=session_hint_date,
    )
    if metrics is None:
        raise ValueError(summary or "Kagi could not parse erg score.")

    from erg_score_validate import erg_score_metrics_usable

    if not erg_score_metrics_usable(metrics):
        from erg_score_validate import format_unusable_parse_reply

        raise ValueError(format_unusable_parse_reply())

    record = _persist_parsed_erg_score(
        cache_dir,
        athlete_id,
        athlete_label,
        metrics,
        summary,
        ocr_text,
        source="zulip_screenshot",
        zulip_message_id=zulip_message_id,
        zulip_sender_email=zulip_sender_email,
        recorded_at=recorded_at,
        session_hint_date=session_hint_date,
    )
    return record, summary


def record_erg_score_from_image(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    image_bytes: bytes,
    token: str,
    *,
    image_mime: str = "image/png",
    zulip_message_id: Optional[int] = None,
    zulip_sender_email: str = "",
    recorded_at: Optional[datetime] = None,
    session_hint_date: Optional[date] = None,
) -> Tuple[Dict[str, Any], str]:
    """Parse an erg screenshot via the vision MLLM and persist it like a logged score.

    Mirrors :func:`record_erg_score_from_screenshot` but reads the image directly
    (no local OCR). The persisted record keeps the same JSON schema.
    """
    if zulip_message_id is not None:
        existing = find_erg_score_by_zulip_message(
            cache_dir, athlete_id, zulip_message_id
        )
        if existing:
            return existing, str(existing.get("summary") or "Already logged this score.")

    metrics, summary = parse_erg_score_from_image(
        image_bytes,
        token,
        image_mime=image_mime,
        athlete_label=athlete_label,
        session_hint_date=session_hint_date,
    )
    if metrics is None:
        raise ValueError(summary or "Vision model could not read erg score.")

    from erg_score_validate import erg_score_metrics_usable

    if not erg_score_metrics_usable(metrics):
        from erg_score_validate import format_unusable_parse_reply

        raise ValueError(format_unusable_parse_reply())

    record = _persist_parsed_erg_score(
        cache_dir,
        athlete_id,
        athlete_label,
        metrics,
        summary,
        # No OCR text for vision; store the model summary as the source artifact.
        f"[vision MLLM screenshot read]\n{summary}",
        source="zulip_screenshot_vision",
        zulip_message_id=zulip_message_id,
        zulip_sender_email=zulip_sender_email,
        recorded_at=recorded_at,
        session_hint_date=session_hint_date,
    )
    return record, summary


def record_erg_score_from_images(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    images: Sequence[Tuple[bytes, str]],
    token: str,
    *,
    zulip_message_id: Optional[int] = None,
    zulip_sender_email: str = "",
    recorded_at: Optional[datetime] = None,
    session_hint_date: Optional[date] = None,
    athlete_message: str = "",
    prescribed_session_text: str = "",
) -> Tuple[Dict[str, Any], str]:
    """Parse one or more erg screenshots (vision + optional synthesis) and persist."""
    if zulip_message_id is not None:
        existing = find_erg_score_by_zulip_message(
            cache_dir, athlete_id, zulip_message_id
        )
        if existing:
            return existing, str(existing.get("summary") or "Already logged this score.")

    if len(images) == 1:
        image_bytes, image_mime = images[0]
        return record_erg_score_from_image(
            cache_dir,
            athlete_id,
            athlete_label,
            image_bytes,
            token,
            image_mime=image_mime,
            zulip_message_id=zulip_message_id,
            zulip_sender_email=zulip_sender_email,
            recorded_at=recorded_at,
            session_hint_date=session_hint_date,
        )

    metrics, summary, extractions = parse_erg_scores_from_images(
        images,
        token,
        athlete_label=athlete_label,
        session_hint_date=session_hint_date,
        athlete_message=athlete_message,
        prescribed_session_text=prescribed_session_text,
    )
    if metrics is None:
        raise ValueError(summary or "Vision model could not read erg screenshots.")

    from erg_score_validate import erg_score_metrics_usable, format_unusable_parse_reply

    if not erg_score_metrics_usable(metrics):
        raise ValueError(format_unusable_parse_reply())

    from erg_session_parts import normalize_multi_screenshot_session
    from erg_hr_enrich import enrich_erg_metrics_hr
    from erg_prescription_compare import prescribed_warmup_cooldown_minutes

    role_session_date = session_hint_date
    if role_session_date is None:
        raw_sd = metrics.get("session_date")
        if isinstance(raw_sd, str) and raw_sd.strip():
            try:
                role_session_date = date.fromisoformat(raw_sd.strip()[:10])
            except ValueError:
                role_session_date = None
    wu_min, cd_min = prescribed_warmup_cooldown_minutes(
        cache_dir,
        athlete_id,
        role_session_date or date.today(),
        metrics=metrics,
    )
    metrics = normalize_multi_screenshot_session(
        metrics,
        extractions,
        prescribed_warmup_min=wu_min,
        prescribed_cooldown_min=cd_min,
    )
    metrics = enrich_erg_metrics_hr(metrics, extractions)

    raw_lines = [
        f"[vision MLLM — {len(images)} screenshots, synthesis pass]",
        summary,
    ]
    for ex in extractions:
        idx = ex.get("index")
        if ex.get("error"):
            raw_lines.append(f"Screenshot {idx}: error — {ex['error']}")
        elif ex.get("summary"):
            raw_lines.append(f"Screenshot {idx}: {ex['summary']}")

    record = _persist_parsed_erg_score(
        cache_dir,
        athlete_id,
        athlete_label,
        metrics,
        summary,
        "\n".join(raw_lines),
        source="zulip_screenshot_vision_multi",
        zulip_message_id=zulip_message_id,
        zulip_sender_email=zulip_sender_email,
        recorded_at=recorded_at,
        session_hint_date=session_hint_date,
        screenshot_extractions=extractions,
    )
    return record, summary


def record_erg_score_from_text(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    transcript: str,
    token: str,
    *,
    zulip_message_id: Optional[int] = None,
    zulip_sender_email: str = "",
    recorded_at: Optional[datetime] = None,
    session_hint_date: Optional[date] = None,
) -> Tuple[Dict[str, Any], str]:
    """Parse athlete-typed erg summary via Kagi and persist like a screenshot log."""
    if zulip_message_id is not None:
        existing = find_erg_score_by_zulip_message(
            cache_dir, athlete_id, zulip_message_id
        )
        if existing:
            return existing, str(existing.get("summary") or "Already logged this score.")

    wrapped = (
        "--- Athlete-provided erg session transcript (typed summary) ---\n"
        f"{transcript.strip()}"
    )
    metrics, summary = parse_erg_score_with_kagi(
        wrapped,
        token,
        athlete_label=athlete_label,
        session_hint_date=session_hint_date,
    )
    if metrics is None:
        raise ValueError(summary or "Could not parse erg score from text.")

    from erg_score_validate import erg_score_metrics_usable

    if not erg_score_metrics_usable(metrics):
        raise ValueError(
            "Could not extract reliable erg metrics from your message. "
            "Include date, distance, duration, avg split, and intervals if any."
        )

    record = _persist_parsed_erg_score(
        cache_dir,
        athlete_id,
        athlete_label,
        metrics,
        summary,
        wrapped,
        source="zulip_text",
        zulip_message_id=zulip_message_id,
        zulip_sender_email=zulip_sender_email,
        recorded_at=recorded_at,
        session_hint_date=session_hint_date,
    )
    return record, summary


def find_latest_elaboration_pending_erg_score(
    cache_dir: Path,
    athlete_id: int,
    *,
    within_hours: float = 72,
) -> Optional[Dict[str, Any]]:
    """Most recent logged erg score still awaiting a detailed coaching follow-up."""
    now = datetime.now(timezone.utc)
    for rec in load_erg_scores_for_athlete(cache_dir, athlete_id, limit=8):
        if not rec.get("coaching_elaboration_pending"):
            continue
        recorded = _parse_activity_start(rec.get("recorded_at"))
        if recorded is not None:
            age_h = (now - recorded.astimezone(timezone.utc)).total_seconds() / 3600.0
            if age_h > within_hours:
                continue
        return rec
    return None


def mark_erg_score_elaboration_sent(
    cache_dir: Path,
    athlete_id: int,
    score_id: str,
) -> None:
    path = erg_score_path(cache_dir, athlete_id, score_id)
    if not path.is_file():
        return
    try:
        rec = json.loads(path.read_text())
    except json.JSONDecodeError:
        return
    rec["coaching_elaboration_pending"] = False
    path.write_text(json.dumps(rec, indent=2))


GYM_LOG_PARSER_VERSION = "2"


def gym_logs_dir(cache_dir: Path, athlete_id: int) -> Path:
    return cache_dir / f"athlete_{athlete_id}" / "gym_logs"


def gym_log_path(cache_dir: Path, athlete_id: int, log_id: str) -> Path:
    return gym_logs_dir(cache_dir, athlete_id) / f"{log_id}.json"


def find_gym_log_by_zulip_message(
    cache_dir: Path, athlete_id: int, zulip_message_id: int
) -> Optional[Dict[str, Any]]:
    root = gym_logs_dir(cache_dir, athlete_id)
    if not root.is_dir():
        return None
    for path in root.glob("*.json"):
        try:
            rec = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if int(rec.get("zulip_message_id") or 0) == zulip_message_id:
            return rec
    return None


def list_gym_logs_sharing_zulip_message(
    cache_dir: Path, zulip_message_id: int
) -> List[Tuple[int, Dict[str, Any]]]:
    """Return every athlete copy logged from the same Zulip message."""
    out: List[Tuple[int, Dict[str, Any]]] = []
    for athlete_id in iter_cached_athlete_ids(cache_dir):
        rec = find_gym_log_by_zulip_message(cache_dir, athlete_id, zulip_message_id)
        if rec is not None:
            out.append((athlete_id, rec))
    return out


def find_gym_log_by_id(
    cache_dir: Path, athlete_id: int, log_id: str
) -> Optional[Dict[str, Any]]:
    path = gym_log_path(cache_dir, athlete_id, log_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def find_gym_log_by_coach_reply_message(
    cache_dir: Path, coach_reply_message_id: int
) -> Optional[Tuple[int, Dict[str, Any]]]:
    """Return (athlete_id, record) for a coach-bot gym log confirmation message."""
    for athlete_id in iter_cached_athlete_ids(cache_dir):
        rec = _find_gym_log_by_coach_reply_for_athlete(
            cache_dir, athlete_id, coach_reply_message_id
        )
        if rec is not None:
            return athlete_id, rec
    return None


def _find_gym_log_by_coach_reply_for_athlete(
    cache_dir: Path, athlete_id: int, coach_reply_message_id: int
) -> Optional[Dict[str, Any]]:
    root = gym_logs_dir(cache_dir, athlete_id)
    if not root.is_dir():
        return None
    for path in root.glob("*.json"):
        try:
            rec = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if int(rec.get("coach_reply_zulip_message_id") or 0) == coach_reply_message_id:
            return rec
    return None


def find_gym_log_for_reaction_message(
    cache_dir: Path,
    message_id: int,
    *,
    athlete_id: Optional[int] = None,
) -> Optional[Tuple[int, Dict[str, Any]]]:
    """Find gym log by coach confirmation id or the athlete's upload message id."""
    athlete_ids = (
        [athlete_id]
        if athlete_id is not None
        else list(iter_cached_athlete_ids(cache_dir))
    )
    for aid in athlete_ids:
        rec = _find_gym_log_by_coach_reply_for_athlete(cache_dir, aid, message_id)
        if rec is not None:
            return aid, rec
    for aid in athlete_ids:
        rec = find_gym_log_by_zulip_message(cache_dir, aid, message_id)
        if rec is not None:
            return aid, rec
    return None


def set_gym_log_coach_reply_message_id(
    cache_dir: Path,
    athlete_id: int,
    log_id: str,
    coach_reply_message_id: int,
) -> None:
    path = gym_log_path(cache_dir, athlete_id, log_id)
    if not path.is_file():
        return
    try:
        rec = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    rec["coach_reply_zulip_message_id"] = int(coach_reply_message_id)
    path.write_text(json.dumps(rec, indent=2))


def delete_gym_log_record(
    cache_dir: Path,
    athlete_id: int,
    log_id: str,
) -> bool:
    """Delete a logged gym session. Returns True if removed."""
    path = gym_log_path(cache_dir, athlete_id, log_id)
    if not path.is_file():
        return False
    path.unlink()
    return True


def save_gym_log_record(
    cache_dir: Path,
    athlete_id: int,
    record: Dict[str, Any],
) -> Path:
    log_id = str(record.get("id") or uuid.uuid4())
    record["id"] = log_id
    path = gym_log_path(cache_dir, athlete_id, log_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2))
    return path


def apply_rpe_follow_up_from_zulip(
    cache_dir: Path,
    athlete_id: int,
    rpe: float,
    *,
    sender_email: str = "",
    now: Optional[datetime] = None,
    within_hours: float = 72,
) -> List[Dict[str, Any]]:
    """Apply an RPE follow-up to the athlete's latest Zulip gym log missing RPE.

    If the sender logged the original message, also copy that RPE onto other
    athletes' logs that share the same ``zulip_message_id``.
    """
    from gym_program import apply_rpe_to_last_working_set, gym_log_missing_rpe

    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    target: Optional[Dict[str, Any]] = None
    for rec in reversed(load_gym_logs_for_athlete(cache_dir, athlete_id)):
        if not gym_log_missing_rpe(rec):
            continue
        recorded = _parse_activity_start(rec.get("recorded_at"))
        if recorded is not None:
            age_h = (now_utc - recorded).total_seconds() / 3600.0
            if age_h > within_hours:
                continue
        target = rec
        break
    if target is None:
        return []

    to_update: List[Tuple[int, Dict[str, Any]]] = [(athlete_id, target)]
    sender = sender_email.strip().lower()
    logged_by = str(target.get("zulip_sender_email") or "").strip().lower()
    msg_id = target.get("zulip_message_id")
    if sender and logged_by and sender == logged_by and msg_id is not None:
        try:
            zulip_message_id = int(msg_id)
        except (TypeError, ValueError):
            zulip_message_id = None
        if zulip_message_id is not None:
            for other_id in iter_cached_athlete_ids(cache_dir):
                if other_id == athlete_id:
                    continue
                other = find_gym_log_by_zulip_message(
                    cache_dir, other_id, zulip_message_id
                )
                if other is not None and gym_log_missing_rpe(other):
                    to_update.append((other_id, other))

    updated: List[Dict[str, Any]] = []
    for aid, rec in to_update:
        if apply_rpe_to_last_working_set(rec, rpe):
            save_gym_log_record(cache_dir, aid, rec)
            updated.append(rec)
    return updated


def load_gym_logs_for_athlete(
    cache_dir: Path,
    athlete_id: int,
    *,
    week: Optional[WeekBounds] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    root = gym_logs_dir(cache_dir, athlete_id)
    if not root.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in root.glob("*.json"):
        try:
            rec = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        session_date = _parse_gym_log_session_date(rec)
        if week is not None:
            if session_date is None or not (
                week.week_start <= session_date <= week.week_end
            ):
                continue
        if start_date is not None and (
            session_date is None or session_date < start_date
        ):
            continue
        if end_date is not None and (
            session_date is None or session_date > end_date
        ):
            continue
        out.append(rec)
    out.sort(
        key=lambda r: (
            str(r.get("session_date") or ""),
            str(r.get("recorded_at") or ""),
        )
    )
    return out


def _gym_record_tonnage(rec: Dict[str, Any]) -> Optional[float]:
    """Total tonnage (kg) stored on a gym-log record, or None when absent."""
    gym = rec.get("gym") or {}
    tonnage = gym.get("total_tonnage_kg")
    if tonnage is None:
        return None
    try:
        return float(tonnage)
    except (TypeError, ValueError):
        return None


def get_tonnage_summary(
    cache_dir: Path,
    athlete_id: int,
    *,
    limit: int = 3,
) -> str:
    """Summarise recent gym tonnage so the LLM can drive progressive overload.

    Returns a string like ``"Last 3 sessions tonnage: 1200kg, 1250kg, 1300kg"``
    (oldest to newest), or ``""`` when no gym sessions are logged.
    """
    logs = load_gym_logs_for_athlete(cache_dir, athlete_id)
    tonnages = [t for rec in logs if (t := _gym_record_tonnage(rec)) is not None]
    recent = tonnages[-limit:]
    if not recent:
        return ""
    parts = ", ".join(f"{t:.0f}kg" for t in recent)
    return f"Last {len(recent)} sessions tonnage: {parts}"


def gym_record_category(rec: Dict[str, Any]) -> str:
    """A/B category ('leg'/'upper_core'/'mixed') of a logged gym session."""
    gym = rec.get("gym") or {}
    names = [str(ex.get("name") or "") for ex in gym.get("exercises") or []]
    return classify_gym_exercise_list(names)


def last_comparable_gym_session(
    cache_dir: Path,
    athlete_id: int,
    category: str,
    *,
    exclude_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Most recent prior gym log of the SAME A/B category (leg vs upper/core)."""
    logs = load_gym_logs_for_athlete(cache_dir, athlete_id)
    for rec in reversed(logs):
        if exclude_id is not None and str(rec.get("id")) == str(exclude_id):
            continue
        if _gym_record_tonnage(rec) is None:
            continue
        if gym_record_category(rec) == category:
            return rec
    return None


# Prescribed gym-set patterns from weekly plans, e.g. "Set 1: 6×60 kg" / "8 reps".
_PRESCRIBED_WEIGHTED_SET_RE = re.compile(
    r"(\d+)\s*(?:reps?\s*)?[×x*]\s*(\d+(?:\.\d+)?)\s*kg", re.I
)
_PRESCRIBED_WEIGHT_FIRST_SET_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*kg\s*[×x*]\s*(\d+)\s*(?:reps?)?", re.I
)
_PRESCRIBED_BODYWEIGHT_SET_RE = re.compile(r"(\d+)\s*reps?\b", re.I)


def _gym_exercise_from_sets(
    name: str, sets: List["GymSetMetrics"]
) -> "GymExerciseMetrics":
    max_w = max((s.weight_kg for s in sets), default=0.0)
    tonnage = sum(s.reps * s.weight_kg for s in sets)
    return GymExerciseMetrics(
        name=name, max_weight_kg=max_w, tonnage_kg=tonnage, sets=sets
    )


def parse_prescribed_gym_session(
    section_text: str,
    *,
    body_weight_kg: Optional[float] = None,
) -> Optional["GymSessionMetrics"]:
    """Deterministically parse prescribed gym sets from one plan-day section.

    Applies the same per-leg and bodyweight finalisation as logged sessions so
    prescribed tonnage is comparable to actual tonnage. Returns None when no
    weighted/bodyweight sets can be parsed.
    """
    if not section_text or not section_text.strip():
        return None
    current_name: Optional[str] = None
    order: List[str] = []
    sets_by_name: Dict[str, List[GymSetMetrics]] = {}

    for raw_line in section_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        header = None
        for match in _GYM_EXERCISE_MATCH_NAMES:
            if re.search(re.escape(match), line, re.I):
                header = _GYM_EXERCISE_DISPLAY[match]
                break
        if header is not None:
            current_name = header
            if header not in sets_by_name:
                sets_by_name[header] = []
                order.append(header)

        if current_name is None:
            continue

        weighted = _PRESCRIBED_WEIGHTED_SET_RE.findall(line)
        if not weighted:
            for w, reps in _PRESCRIBED_WEIGHT_FIRST_SET_RE.findall(line):
                weighted.append((reps, w))
        if weighted:
            for reps, weight in weighted:
                sets_by_name[current_name].append(
                    GymSetMetrics(reps=int(reps), weight_kg=float(weight))
                )
            continue
        if is_bodyweight_gym_exercise(current_name):
            bw_matches = _PRESCRIBED_BODYWEIGHT_SET_RE.findall(line)
            for reps in bw_matches:
                sets_by_name[current_name].append(
                    GymSetMetrics(reps=int(reps), weight_kg=0.0)
                )

    exercises = [
        _gym_exercise_from_sets(name, sets_by_name[name])
        for name in order
        if sets_by_name[name]
    ]
    if not exercises:
        return None
    metrics = GymSessionMetrics(
        activity_id=0,
        activity_name="Prescribed gym session",
        total_tonnage_kg=sum(e.tonnage_kg for e in exercises),
        exercises=exercises,
    )
    return finalize_gym_session_metrics(metrics, body_weight_kg)


def prescribed_gym_section_for_log(
    cache_dir: Path,
    athlete_id: int,
    session_date: date,
) -> Optional[str]:
    """Prescribed gym-day section for a logged session.

    Prefers the athlete's personalised DM plan when one is cached for that week;
    falls back to the squad plan. Returns the day-section text or None.
    """
    athlete_record = athlete_plan_for_date(cache_dir, athlete_id, session_date)
    if athlete_record:
        section = session_from_plan(
            str(athlete_record.get("plan_text") or ""),
            athlete_record.get("plan_json")
            if isinstance(athlete_record.get("plan_json"), dict)
            else None,
            session_date,
        )
        if section and section[1].strip():
            return section[1]
    squad = plan_for_date(cache_dir, session_date)
    if squad:
        section = plan_record_session_for_date(squad, session_date)
        if section and section[1].strip():
            return section[1]
    return None


def format_gym_session_comparison(
    cache_dir: Path,
    athlete_id: int,
    record: Dict[str, Any],
    *,
    prescribed_section: Optional[str] = None,
    body_weight_kg: Optional[float] = None,
) -> str:
    """Prescription-based comparison: actual vs prescribed and vs last comparable day.

    No deload/fatigue questions — deloads are coach-prescribed, not inferred.
    """
    actual = _gym_record_tonnage(record)
    if actual is None or actual <= 0:
        return ""
    category = gym_record_category(record)
    label = _GYM_CATEGORY_LABEL.get(category, "comparable")
    lines: List[str] = []

    if prescribed_section:
        prescribed = parse_prescribed_gym_session(
            prescribed_section, body_weight_kg=body_weight_kg
        )
        prescribed_tonnage = (
            prescribed.total_tonnage_kg if prescribed is not None else None
        )
        if prescribed_tonnage and prescribed_tonnage > 0:
            diff_pct = (actual - prescribed_tonnage) / prescribed_tonnage * 100
            if abs(diff_pct) < 5:
                verdict = "on plan"
            elif diff_pct > 0:
                verdict = f"{diff_pct:.0f}% above plan"
            else:
                verdict = f"{abs(diff_pct):.0f}% below plan"
            lines.append(
                f"Actual {actual:.0f} kg vs prescribed ~{prescribed_tonnage:.0f} kg "
                f"({verdict})."
            )

    comparable = last_comparable_gym_session(
        cache_dir, athlete_id, category, exclude_id=str(record.get("id"))
    )
    if comparable is not None:
        prev = _gym_record_tonnage(comparable)
        if prev and prev > 0:
            delta_pct = (actual - prev) / prev * 100
            sign = "+" if delta_pct >= 0 else "−"
            prev_date = comparable.get("session_date", "?")
            lines.append(
                f"vs last {label} day ({prev_date}, {prev:.0f} kg): "
                f"{sign}{abs(delta_pct):.0f}%."
            )

    return "\n".join(lines)


def _parse_gym_log_session_date(rec: Dict[str, Any]) -> Optional[date]:
    raw = rec.get("session_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _synthetic_gym_log_activity_id(
    zulip_message_id: Optional[int] = None,
    *,
    log_id: Optional[str] = None,
    session_date: Optional[str] = None,
) -> int:
    if zulip_message_id is not None:
        base = int(zulip_message_id) % 50_000_000
    elif log_id:
        base = int(hashlib.sha256(log_id.encode()).hexdigest()[:8], 16) % 50_000_000
    elif session_date:
        base = int(str(session_date).replace("-", "")) % 50_000_000
    else:
        base = 0
    return 900_000_000 + base


def find_gym_log_by_session_date(
    cache_dir: Path,
    athlete_id: int,
    session_date: date,
    *,
    source: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    for rec in load_gym_logs_for_athlete(cache_dir, athlete_id):
        rec_date = _parse_gym_log_session_date(rec)
        if rec_date != session_date:
            continue
        if source is not None and rec.get("source") != source:
            continue
        return rec
    return None


def build_gym_session_from_set_weights(
    exercises: Sequence[Tuple[str, Sequence[float]]],
    *,
    default_reps: int = 5,
    arms_reps: int = 10,
    arms_sets: int = 3,
) -> GymSessionMetrics:
    """Build gym metrics from per-set weights (kg) when reps were not recorded."""
    parsed_exercises: List[GymExerciseMetrics] = []
    for name, weights in exercises:
        wlist = [float(w) for w in weights if w is not None]
        if not wlist:
            continue
        if len(wlist) == 1 and name.strip().lower() == "arms":
            sets = [
                GymSetMetrics(reps=arms_reps, weight_kg=wlist[0])
                for _ in range(arms_sets)
            ]
        else:
            sets = [GymSetMetrics(reps=default_reps, weight_kg=w) for w in wlist]
        max_w = max(s.weight_kg for s in sets)
        tonnage = sum(s.reps * s.weight_kg for s in sets)
        parsed_exercises.append(
            GymExerciseMetrics(
                name=name,
                max_weight_kg=max_w,
                tonnage_kg=tonnage,
                sets=sets,
            )
        )
    total = sum(ex.tonnage_kg for ex in parsed_exercises)
    return GymSessionMetrics(
        activity_id=0,
        activity_name="Gym (historical import)",
        total_tonnage_kg=total,
        exercises=parsed_exercises,
        unit="kg",
        assumptions=(
            f"Historical import; {default_reps} reps/set assumed for recorded "
            f"weights (arms: {arms_sets}×{arms_reps} when only one weight listed)."
        ),
    )


def build_gym_session_from_tonnage(
    exercises: Sequence[Tuple[str, float, Optional[float]]],
    *,
    default_sets: int = 5,
    default_reps: int = 5,
) -> GymSessionMetrics:
    """Build gym metrics from per-exercise tonnage (kg) when sets were not recorded."""
    parsed_exercises: List[GymExerciseMetrics] = []
    for name, tonnage_kg, max_weight_kg in exercises:
        tonnage_kg = float(tonnage_kg)
        if tonnage_kg <= 0:
            continue
        work = default_sets * default_reps
        avg_weight = tonnage_kg / work
        max_w = float(max_weight_kg) if max_weight_kg is not None else avg_weight
        sets = [
            GymSetMetrics(reps=default_reps, weight_kg=avg_weight)
            for _ in range(default_sets)
        ]
        parsed_exercises.append(
            GymExerciseMetrics(
                name=name,
                max_weight_kg=max_w,
                tonnage_kg=tonnage_kg,
                sets=sets,
            )
        )
    total = sum(ex.tonnage_kg for ex in parsed_exercises)
    return GymSessionMetrics(
        activity_id=0,
        activity_name="Gym (historical import)",
        total_tonnage_kg=total,
        exercises=parsed_exercises,
        unit="kg",
        assumptions=(
            f"Historical tonnage import; per-exercise tonnage spread evenly as "
            f"{default_sets}×{default_reps} at average load (max weight used when given)."
        ),
    )


def _save_historical_gym_record(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    session_date: date,
    parsed: GymSessionMetrics,
    *,
    import_format: str,
    body_weight_kg: Optional[float] = None,
) -> Dict[str, Any]:
    log_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"gym-hist:{athlete_id}:{session_date}"))
    gym_dict = parsed.to_dict()
    gym_dict["activity_id"] = _synthetic_gym_log_activity_id(
        log_id=log_id, session_date=session_date.isoformat()
    )
    record: Dict[str, Any] = {
        "id": log_id,
        "athlete_id": athlete_id,
        "athlete_label": athlete_label,
        "recorded_at": datetime.combine(
            session_date, datetime.min.time(), tzinfo=timezone.utc
        ).isoformat(),
        "session_date": session_date.isoformat(),
        "source": "historical_import",
        "import_format": import_format,
        "zulip_message_id": None,
        "zulip_sender_email": "",
        "raw_text": "",
        "gym": gym_dict,
        "parser_version": GYM_LOG_PARSER_VERSION,
    }
    if body_weight_kg is not None:
        record["body_weight_kg"] = body_weight_kg
    save_gym_log_record(cache_dir, athlete_id, record)
    return record


def record_historical_gym_session(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    session_date: date,
    exercises: Sequence[Tuple[str, Sequence[float]]],
    *,
    default_reps: int = 5,
    arms_reps: int = 10,
    arms_sets: int = 3,
    skip_existing: bool = True,
) -> Dict[str, Any]:
    """Import a pre-digital gym session into athlete gym_logs/."""
    if skip_existing and find_gym_log_by_session_date(
        cache_dir, athlete_id, session_date, source="historical_import"
    ):
        return find_gym_log_by_session_date(
            cache_dir, athlete_id, session_date, source="historical_import"
        )  # type: ignore[return-value]

    parsed = build_gym_session_from_set_weights(
        exercises,
        default_reps=default_reps,
        arms_reps=arms_reps,
        arms_sets=arms_sets,
    )
    return _save_historical_gym_record(
        cache_dir,
        athlete_id,
        athlete_label,
        session_date,
        parsed,
        import_format="set_weights",
    )


def record_historical_gym_tonnage_session(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    session_date: date,
    exercises: Sequence[Tuple[str, float, Optional[float]]],
    *,
    default_sets: int = 5,
    default_reps: int = 5,
    body_weight_kg: Optional[float] = None,
    skip_existing: bool = True,
) -> Dict[str, Any]:
    """Import a spreadsheet tonnage row into athlete gym_logs/."""
    if skip_existing and find_gym_log_by_session_date(
        cache_dir, athlete_id, session_date, source="historical_import"
    ):
        return find_gym_log_by_session_date(
            cache_dir, athlete_id, session_date, source="historical_import"
        )  # type: ignore[return-value]

    parsed = build_gym_session_from_tonnage(
        exercises,
        default_sets=default_sets,
        default_reps=default_reps,
    )
    return _save_historical_gym_record(
        cache_dir,
        athlete_id,
        athlete_label,
        session_date,
        parsed,
        import_format="tonnage_xlsx",
        body_weight_kg=body_weight_kg,
    )


def zulip_gym_logs_as_metrics_records(
    cache_dir: Path,
    athlete_id: int,
    *,
    week: Optional[WeekBounds] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> Dict[int, Dict[str, Any]]:
    """Map DM-logged gym sessions into activity-metrics shape for plan summaries."""
    out: Dict[int, Dict[str, Any]] = {}
    for rec in load_gym_logs_for_athlete(
        cache_dir,
        athlete_id,
        week=week,
        start_date=start_date,
        end_date=end_date,
    ):
        gym = rec.get("gym")
        if not gym:
            continue
        zmid = rec.get("zulip_message_id")
        log_id = rec.get("id")
        session_date = rec.get("session_date")
        aid = _synthetic_gym_log_activity_id(
            int(zmid) if zmid is not None else None,
            log_id=str(log_id) if log_id else None,
            session_date=str(session_date) if session_date else None,
        )
        start = f"{session_date}T12:00:00+00:00" if session_date else None
        source = str(rec.get("source") or "zulip_dm")
        default_name = (
            "Gym (historical import)"
            if source == "historical_import"
            else "Gym (Zulip DM)"
        )
        out[aid] = {
            "activity_id": aid,
            "activity_name": gym.get("activity_name") or default_name,
            "start_date": start,
            "gym": gym,
            "source": source,
            "gym_log_id": log_id,
            "athlete_id": athlete_id,
        }
    return out


def merge_zulip_gym_logs_into_metrics(
    metrics_by_id: Dict[int, Dict[str, Any]],
    cache_dir: Path,
    *,
    week: Optional[WeekBounds] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> Dict[int, Dict[str, Any]]:
    merged = dict(metrics_by_id)
    for athlete_id in iter_cached_athlete_ids(cache_dir):
        merged.update(
            zulip_gym_logs_as_metrics_records(
                cache_dir,
                athlete_id,
                week=week,
                start_date=start_date,
                end_date=end_date,
            )
        )
    return merged


def format_zulip_gym_logs_summary(
    cache_dir: Path,
    week: WeekBounds,
) -> str:
    """One-line-per-athlete summary of gym sessions logged via coach-bot DM."""
    lines: List[str] = []
    for athlete_id in iter_cached_athlete_ids(cache_dir):
        for rec in load_gym_logs_for_athlete(cache_dir, athlete_id, week=week):
            gym = rec.get("gym") or {}
            label = rec.get("athlete_label") or f"athlete_{athlete_id}"
            session_day = rec.get("session_date") or "?"
            tonnage = gym.get("total_tonnage_kg", 0)
            exercises = gym.get("exercises") or []
            names = ", ".join(str(ex.get("name", "?")) for ex in exercises[:6])
            if len(exercises) > 6:
                names += ", …"
            lines.append(
                f"- {session_day} ({label}): {tonnage:.0f} kg tonnage"
                + (f" ({names})" if names else "")
            )
    if not lines:
        return ""
    return "--- Gym sessions logged via Zulip DM ---\n" + "\n".join(lines)


def record_gym_session_from_zulip(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    workout_text: str,
    token: str,
    *,
    zulip_message_id: Optional[int] = None,
    zulip_sender_email: str = "",
    recorded_at: Optional[datetime] = None,
    session_hint_date: Optional[date] = None,
    body_weight_kg: Optional[float] = None,
    rpe_transcript: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse a DM workout transcript and persist under athlete gym_logs/."""
    records = record_gym_sessions_from_zulip_for_athletes(
        cache_dir,
        [(athlete_id, athlete_label, body_weight_kg)],
        workout_text,
        token,
        zulip_message_id=zulip_message_id,
        zulip_sender_email=zulip_sender_email,
        recorded_at=recorded_at,
        session_hint_date=session_hint_date,
        rpe_transcript=rpe_transcript,
    )
    return records[0]


def record_gym_sessions_from_zulip_for_athletes(
    cache_dir: Path,
    recipients: Sequence[Tuple[int, str, Optional[float]]],
    workout_text: str,
    token: str,
    *,
    zulip_message_id: Optional[int] = None,
    recorded_at: Optional[datetime] = None,
    zulip_sender_email: str = "",
    session_hint_date: Optional[date] = None,
    rpe_transcript: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Parse a gym log once and persist an identical copy per recipient."""
    if not recipients:
        return []

    recorded_at = recorded_at or datetime.now(timezone.utc)
    session_date = session_hint_date or activity_local_date(recorded_at)
    gym_dict: Optional[Dict[str, Any]] = None
    if zulip_message_id is not None:
        for athlete_id, _, _ in recipients:
            existing = find_gym_log_by_zulip_message(
                cache_dir, athlete_id, zulip_message_id
            )
            if existing and existing.get("gym"):
                gym_dict = copy.deepcopy(existing["gym"])
                break

    if gym_dict is None:
        parse_errors: List[str] = []
        parsed = parse_gym_session_metrics(
            0,
            "Gym (Zulip DM)",
            workout_text,
            token,
            body_weight_kg=recipients[0][2],
            parse_errors=parse_errors,
        )
        if parsed is None:
            detail = parse_errors[0] if parse_errors else "no exercises parsed"
            raise ValueError(
                f"Could not parse exercises, sets, or tonnage from message ({detail})."
            )
        gym_dict = parsed.to_dict()
        gym_dict["activity_id"] = 0
        gym_dict["activity_name"] = "Gym (Zulip DM)"
        from gym_program import overlay_transcript_rpe_on_record

        overlay_transcript_rpe_on_record(
            {"gym": gym_dict}, rpe_transcript or workout_text
        )

    persist_text = (rpe_transcript or workout_text)[:8000]
    return [
        _persist_zulip_gym_log(
            cache_dir,
            athlete_id,
            athlete_label,
            gym_dict,
            zulip_message_id=zulip_message_id,
            zulip_sender_email=zulip_sender_email,
            recorded_at=recorded_at,
            session_date=session_date,
            raw_text=persist_text,
            body_weight_kg=body_weight_kg,
        )
        for athlete_id, athlete_label, body_weight_kg in recipients
    ]


def _persist_zulip_gym_log(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    gym_dict: Dict[str, Any],
    *,
    zulip_message_id: Optional[int],
    zulip_sender_email: str,
    recorded_at: datetime,
    session_date: date,
    raw_text: str,
    body_weight_kg: Optional[float],
) -> Dict[str, Any]:
    if zulip_message_id is not None:
        existing = find_gym_log_by_zulip_message(
            cache_dir, athlete_id, zulip_message_id
        )
        if existing:
            return existing

    gym_copy = copy.deepcopy(gym_dict)
    record: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "athlete_id": athlete_id,
        "athlete_label": athlete_label,
        "recorded_at": recorded_at.astimezone(timezone.utc).isoformat(),
        "session_date": session_date.isoformat(),
        "source": "zulip_dm",
        "zulip_message_id": zulip_message_id,
        "zulip_sender_email": zulip_sender_email,
        "raw_text": raw_text[:8000],
        "gym": gym_copy,
        "parser_version": GYM_LOG_PARSER_VERSION,
    }
    if body_weight_kg is not None:
        record["body_weight_kg"] = body_weight_kg
    elif any(
        is_bodyweight_gym_exercise(str(ex.get("name") or ""))
        and float(ex.get("tonnage_kg") or 0) <= 0
        for ex in gym_copy.get("exercises") or []
    ):
        record["bodyweight_note"] = (
            "Bodyweight exercises need your body weight in config for tonnage "
            "(ask coach to set body_weight_kg in your athlete profile)."
        )
    save_gym_log_record(cache_dir, athlete_id, record)
    return record


def format_gym_log_confirmation(record: Dict[str, Any]) -> str:
    gym = record.get("gym") or {}
    exercises = gym.get("exercises") or []
    log_id = str(record.get("id") or "")
    session_date = record.get("session_date", "?")
    if log_id:
        header = (
            f"**Logged gym session** (`{log_id}`, {session_date}) — "
            f"{float(gym.get('total_tonnage_kg', 0)):.0f} kg total tonnage, "
            f"{len(exercises)} exercise(s)."
        )
    else:
        header = (
            f"**Logged gym session** ({session_date}) — "
            f"{float(gym.get('total_tonnage_kg', 0)):.0f} kg total tonnage, "
            f"{len(exercises)} exercise(s)."
        )
    lines = [header]
    for ex in exercises[:8]:
        name = str(ex.get("name") or "")
        max_w = float(ex.get("max_weight_kg", 0))
        tonnage = float(ex.get("tonnage_kg", 0))
        bw = record.get("body_weight_kg")
        max_label = f"{max_w:.1f} kg"
        if (
            bw is not None
            and is_bodyweight_gym_exercise(name)
            and max_w > 0
            and abs(max_w - float(bw)) < 0.05
        ):
            max_label = f"{max_w:.1f} kg (bodyweight)"
        lines.append(f"- {name}: max {max_label}, {tonnage:.0f} kg tonnage")
    if len(exercises) > 8:
        lines.append("- …")
    bodyweight_note = record.get("bodyweight_note")
    if bodyweight_note:
        lines.append("")
        lines.append(str(bodyweight_note))
    return "\n".join(lines)


def _parse_erg_score_session_date(rec: Dict[str, Any]) -> Optional[date]:
    raw = rec.get("session_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def load_erg_scores_for_athlete(
    cache_dir: Path,
    athlete_id: int,
    *,
    exclude_id: Optional[str] = None,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    """Recent erg score records for one athlete, newest session first."""
    scores_dir = erg_scores_dir(cache_dir, athlete_id)
    if not scores_dir.is_dir():
        return []
    records: List[Dict[str, Any]] = []
    for path in scores_dir.glob("*.json"):
        try:
            rec = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if exclude_id and str(rec.get("id")) == exclude_id:
            continue
        records.append(rec)

    def sort_key(rec: Dict[str, Any]) -> Tuple[str, str]:
        session = str(rec.get("session_date") or "")
        recorded = str(rec.get("recorded_at") or "")
        return (session, recorded)

    records.sort(key=sort_key, reverse=True)
    return records[:limit]


def format_erg_score_line(rec: Dict[str, Any]) -> str:
    """One-line summary of a logged erg score."""
    metrics = rec.get("metrics") or {}
    session_day = rec.get("session_date") or "?"
    parts = [str(session_day), str(metrics.get("workout_type") or "erg")]
    if metrics.get("distance_m") is not None:
        parts.append(f"{metrics['distance_m']:g} m")
    split_fmt = metrics.get("avg_split_500_fmt")
    if split_fmt:
        parts.append(f"avg {split_fmt}")
    elif metrics.get("avg_split_500_sec") is not None:
        parts.append(f"avg {_fmt_split(float(metrics['avg_split_500_sec']))}")
    if metrics.get("avg_hr") is not None:
        parts.append(f"HR {metrics['avg_hr']} bpm")
    intervals = metrics.get("intervals") or []
    if intervals:
        parts.append(f"{len(intervals)} intervals")
    return " — ".join([parts[0], ", ".join(parts[1:])])


def format_erg_score_history_context(
    records: Sequence[Dict[str, Any]],
    *,
    heading: str = "Prior logged erg scores (newest first)",
) -> str:
    if not records:
        return f"--- {heading} ---\n(no prior erg scores logged via Zulip for this athlete)"
    lines = [f"--- {heading} ---"]
    for rec in records:
        line = format_erg_score_line(rec)
        note = str(rec.get("summary") or "").strip()
        if note:
            line += f" — {note[:160]}"
        lines.append(f"- {line}")
    return "\n".join(lines)


def format_logged_erg_score_detail(rec: Dict[str, Any]) -> str:
    """Structured text for the session just logged."""
    metrics = rec.get("metrics") or {}
    lines = [
        f"Session date: {rec.get('session_date') or '?'}",
        f"Workout type: {metrics.get('workout_type') or '?'}",
    ]
    if metrics.get("distance_m") is not None:
        lines.append(f"Distance: {metrics['distance_m']} m")
    if metrics.get("duration_sec") is not None:
        lines.append(f"Work duration: {metrics['duration_sec']} s")
    if metrics.get("avg_split_500_fmt"):
        lines.append(f"Avg split: {metrics['avg_split_500_fmt']}")
    if metrics.get("avg_hr") is not None:
        lines.append(f"Avg HR: {metrics['avg_hr']} bpm")
    if metrics.get("stroke_rate") is not None:
        lines.append(f"Stroke rate: {metrics['stroke_rate']} spm")
    for part in metrics.get("session_parts") or []:
        if not isinstance(part, dict):
            continue
        role = part.get("role") or part.get("label") or "part"
        bits = [f"part {role}"]
        if part.get("screenshot_index") is not None:
            bits[0] += f" (screenshot {int(part['screenshot_index']) + 1})"
        if part.get("distance_m") is not None:
            bits.append(f"{part['distance_m']} m")
        if part.get("duration_sec") is not None:
            bits.append(f"{part['duration_sec']} s")
        if part.get("avg_split_500_fmt"):
            bits.append(part["avg_split_500_fmt"])
        elif part.get("avg_split_500_sec") is not None:
            try:
                bits.append(_fmt_split(float(part["avg_split_500_sec"])))
            except (TypeError, ValueError):
                pass
        if part.get("avg_hr") is not None:
            bits.append(f"HR {part['avg_hr']} bpm")
        if part.get("stroke_rate") is not None:
            bits.append(f"{part['stroke_rate']} spm")
        lines.append("  " + ", ".join(bits))
    for interval in metrics.get("intervals") or []:
        if not isinstance(interval, dict):
            continue
        label = interval.get("label") or "?"
        bits = [f"interval {label}"]
        if interval.get("distance_m") is not None:
            bits.append(f"{interval['distance_m']} m")
        if interval.get("time_sec") is not None:
            bits.append(f"{interval['time_sec']} s")
        if interval.get("split_500_sec") is not None:
            try:
                bits.append(_fmt_split(float(interval["split_500_sec"])))
            except (TypeError, ValueError):
                pass
        if interval.get("stroke_rate") is not None:
            bits.append(f"{interval['stroke_rate']} spm")
        if interval.get("avg_hr") is not None:
            bits.append(f"HR {interval['avg_hr']} bpm")
        lines.append("  " + ", ".join(bits))
    if metrics.get("assumptions"):
        lines.append(f"Parse notes: {metrics['assumptions']}")
    return "\n".join(lines)


def _weekday_name_in_text(text: str) -> Optional[str]:
    for name in _WEEKDAY_NAMES:
        if re.search(rf"\b{re.escape(name)}\b", text, re.I):
            return name
    return None


_MAKEUP_PRESCRIPTION_RE = re.compile(
    r"\b(made?\s*up|make\s*up|makeup|missed|catch\s*up|backfill|substitut\w*)\b",
    re.I,
)


def infer_makeup_prescribed_date(text: str, logged_on: date) -> Optional[date]:
    """When the athlete did another plan day's workout (e.g. Tuesday erg on Thursday).

    Returns the prescribed plan date to compare against. ``logged_on`` stays the
    session date; only the prescription target moves.
    """
    body = (text or "").strip()
    if not body or not _MAKEUP_PRESCRIPTION_RE.search(body):
        return None
    weekday = _weekday_name_in_text(body)
    if weekday is None:
        return None
    return _plan_date_for_weekday(logged_on, weekday)


def _plan_date_for_weekday(session_date: date, weekday_name: str) -> date:
    week = week_for_date(session_date)
    idx = _WEEKDAY_NAMES.index(weekday_name)
    return week.week_start + timedelta(days=idx)


def build_erg_score_coaching_prompt(
    erg_record: Dict[str, Any],
    plan_record: Optional[WeeklyPlanRecord],
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    *,
    local_datetime: Optional[datetime] = None,
    topic_context: Optional[str] = None,
    athlete_message: Optional[str] = None,
    brief: bool = False,
) -> Tuple[str, str, List[ChatMessage]]:
    """OpenRouter messages for post-log coaching: prescribed session vs logged vs history."""
    local_datetime = _resolve_coach_local_datetime(local_datetime=local_datetime)
    session_date = _parse_erg_score_session_date(erg_record) or local_datetime.date()
    weekday = _WEEKDAY_NAMES[session_date.weekday()]
    prescribed_date = infer_makeup_prescribed_date(
        athlete_message or "", session_date
    ) or session_date
    prescribed_weekday = _WEEKDAY_NAMES[prescribed_date.weekday()]
    exclude_id = str(erg_record.get("id") or "")
    history = load_erg_scores_for_athlete(
        cache_dir, athlete_id, exclude_id=exclude_id or None, limit=12
    )
    week = week_for_date(session_date)
    week_scores = [
        r
        for r in load_erg_scores_for_week(cache_dir, athlete_id, week)
        if str(r.get("id")) != exclude_id
    ]

    from erg_prescription_compare import (
        erg_plan_context_for_date,
        format_erg_session_comparison,
        format_week_zone_volume_progress,
    )

    plan_text, plan_json, personalised, _ = erg_plan_context_for_date(
        cache_dir, athlete_id, session_date
    )
    meta_record = plan_record if plan_record and plan_record.plan_text.strip() else None
    if meta_record is None:
        meta_record = plan_for_date(cache_dir, session_date)

    blocks: List[str] = []
    prescription_check = format_erg_session_comparison(
        cache_dir, athlete_id, erg_record, session_date,
        prescribed_session_date=(
            prescribed_date if prescribed_date != session_date else None
        ),
    )
    if prescription_check:
        blocks.append(
            "--- Deterministic prescription check (authoritative) ---\n"
            + prescription_check
        )
    week_zone_progress = format_week_zone_volume_progress(
        cache_dir,
        athlete_id,
        session_date,
    )
    if week_zone_progress:
        blocks.append(
            "--- Deterministic week zone volume (authoritative) ---\n"
            + week_zone_progress
        )

    plan_source = "personalised athlete DM plan" if personalised else "squad plan"
    if plan_text or plan_json:
        prescribed = session_from_plan(plan_text or "", plan_json, session_date)
        if prescribed:
            day_name, section = prescribed
            blocks.append(
                f"--- Prescribed session ({day_name} {session_date.isoformat()}, "
                f"{plan_source}) ---\n"
                f"{section.strip()}"
            )
        elif not brief:
            blocks.append(
                f"--- Prescribed session ({weekday} {session_date.isoformat()}) ---\n"
                f"(Could not extract this day from {plan_source}; "
                "use the full weekly plan below.)"
            )
        if not brief and plan_text:
            blocks.append(
                f"--- Weekly plan ({week.week_start.isoformat()} to "
                f"{week.week_end.isoformat()}, {plan_source}) ---\n"
                f"{plan_text.strip()}"
            )
            if meta_record and meta_record.training_summary.strip():
                blocks.append(
                    "--- Tracked erg performance (Strava/training summary at plan generation) ---\n"
                    f"{meta_record.training_summary.strip()}"
                )
            if meta_record and meta_record.goal_tracking and str(
                meta_record.goal_tracking
            ).strip():
                blocks.append(
                    f"--- Season goal tracking ---\n{str(meta_record.goal_tracking).strip()}"
                )
    else:
        blocks.append(
            f"--- Prescribed session ({weekday} {session_date.isoformat()}) ---\n"
            f"(No cached weekly plan for week `{week.week_id}`.)"
        )

    try:
        from erg_session_merge import (
            find_merged_session_for_zulip_score,
            format_merged_erg_history_context,
            format_merged_session_detail,
            rebuild_merged_erg_sessions_for_athlete,
        )

        rebuild_merged_erg_sessions_for_athlete(
            cache_dir, athlete_id, athlete_label=athlete_label
        )
        merged_current = find_merged_session_for_zulip_score(
            cache_dir, athlete_id, exclude_id
        )
        if merged_current:
            blocks.append(
                "--- Logged session (merged Zulip screenshot + Strava when matched) ---\n"
                f"{format_merged_session_detail(merged_current)}"
            )
        else:
            blocks.append(
                "--- Logged session (from screenshot just now) ---\n"
                f"{format_logged_erg_score_detail(erg_record)}"
            )
        if not brief:
            blocks.append(
                format_merged_erg_history_context(
                    cache_dir,
                    athlete_id,
                    exclude_merged_id=merged_current.get("id") if merged_current else exclude_id,
                    limit=12,
                )
            )
    except Exception:
        blocks.append(
            "--- Logged session (from screenshot just now) ---\n"
            f"{format_logged_erg_score_detail(erg_record)}"
        )
        if not brief:
            if week_scores:
                blocks.append(
                    format_erg_score_history_context(
                        week_scores,
                        heading=f"Other erg scores logged this plan week ({week.week_id})",
                    )
                )
            blocks.append(format_erg_score_history_context(history))
    if athlete_message and athlete_message.strip():
        blocks.append(f"--- Athlete message ---\n{athlete_message.strip()}")
        asked_day = _weekday_name_in_text(athlete_message)
        if asked_day and (plan_json or (plan_text or "").strip()):
            asked_date = _plan_date_for_weekday(session_date, asked_day)
            asked_prescribed = session_from_plan(
                plan_text or "", plan_json, asked_date
            )
            if asked_prescribed:
                _, asked_section = asked_prescribed
                blocks.append(
                    f"--- Prescribed session athlete asked about "
                    f"({asked_day} {asked_date.isoformat()}) ---\n"
                    f"{asked_section.strip()}"
                )
    history = [] if brief else topic_context_to_history(topic_context or "")
    user_blocks = [
        f"Athlete: {athlete_label}.",
        f"Session date ({plan_timezone_name()}): {session_date.isoformat()} ({weekday}).",
        *blocks,
    ]
    if brief:
        prescription_phrase = (
            f"the {prescribed_weekday} prescription (makeup session logged {weekday})"
            if prescribed_date != session_date
            else "today's prescription"
        )
        system = (
            "You are an expert rowing coach. The athlete just logged an erg session.\n\n"
            "Write a **very short** reply — at most **2 sentences** total:\n"
            f"1. One sentence: what they did (key metrics) vs {prescription_phrase} "
            "(matched / over / under / substituted).\n"
            "2. One sentence: the single most important takeaway, including week zone "
            "volume progress when provided.\n\n"
            "RULES:\n"
            "- The **Deterministic prescription check** and **Deterministic week zone "
            "volume** blocks are authoritative — do NOT contradict them.\n"
            "- Compare each logged segment to its matching prescribed segment (warm-up, "
            "main set, cool-down). Respect `priority: hr` vs `priority: split`.\n"
            "- Week volume context is prescribed minutes only (Z2/T2, other zones, "
            "logged mix). Do NOT mention season goals, season week targets, or compare "
            "logged mix percentages to a season Z2% goal.\n"
            "- Do not mention Z5 or high-intensity targets unless Z5 volume is "
            "prescribed in the week zone volume block.\n"
            "- No bullets, no history essay, no pipeline summaries.\n"
            "- Do not quote or repeat Zulip topic history lines or timestamps.\n"
            "- Lead with concrete logged metrics only.\n"
            "- End the reply with exactly: "
            "\"@coach for more detail on this session.\""
        )
    else:
        system = (
            "You are an expert rowing coach reviewing an athlete's erg session they just "
            "logged via a Concept2 screenshot.\n\n"
            "Write a concise coaching reply (3–6 short paragraphs or bullets) that:\n"
            "1. States what was PRESCRIBED for this session (or the closest plan day if "
            "rescheduled in topic conversation history).\n"
            "2. Compares what they ACTUALLY did (logged metrics) to that prescription — "
            "volume, intensity, intervals, splits, HR if present. Say clearly if they "
            "matched, exceeded, under-ran, or substituted.\n"
            "3. Places the session in context of their TRACKED HISTORY — prior logged erg "
            "scores and the training summary — noting trends (faster/slower, more/less "
            "volume, consistency).\n"
            "4. Gives 1–2 specific takeaways for the rest of this week or next session.\n"
            "5. If an athlete message is provided, answer it directly.\n\n"
            "RULES:\n"
            "- The **Deterministic prescription check** and **Deterministic week zone "
            "volume** blocks are authoritative — do NOT contradict them.\n"
            "- Compare each logged segment to its matching prescribed segment. Respect "
            "`priority: hr` vs `priority: split`.\n"
            "- Be direct and actionable; no generic praise.\n"
            "- Lead with concrete logged metrics and comparison to prescription.\n"
            "- If the session was rescheduled (see conversation history), compare to the "
            "original and/or rescheduled prescription explicitly.\n"
            "- If prescription is missing, coach from history and logged data only.\n"
            "- Do not repeat raw JSON or re-list every interval unless needed for a point.\n"
            "- Do not quote or echo prior Zulip topic messages or timestamps.\n"
            "- Do not mention OCR or screenshot parsing."
        )
    return system, "\n\n".join(user_blocks), history


def answer_erg_score_coaching(
    erg_record: Dict[str, Any],
    plan_record: Optional[WeeklyPlanRecord],
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    token: str,
    *,
    local_datetime: Optional[datetime] = None,
    topic_context: Optional[str] = None,
    athlete_message: Optional[str] = None,
    brief: bool = True,
) -> str:
    """Coach feedback after logging an erg screenshot or typed transcript."""
    system, user, history = build_erg_score_coaching_prompt(
        erg_record,
        plan_record,
        cache_dir,
        athlete_id,
        athlete_label,
        local_datetime=local_datetime,
        topic_context=topic_context,
        athlete_message=athlete_message,
        brief=brief,
    )
    return _call_llm(system, user, token, history=history).strip()


def summarize_rowing_metrics_from_df(
    erg_df: pd.DataFrame, activity_id: int
) -> Optional[Dict[str, Any]]:
    """Aggregate erg stream/photo points for one activity (stored with gym metrics)."""
    sub = erg_df.loc[erg_df["activity_id"] == activity_id]
    if sub.empty:
        return None
    split = sub["split_500"].astype(float)
    hr = sub["hr"].astype(float)
    n = len(sub)
    high = (split < 110).sum() / n * 100 if n else 0.0
    return {
        "median_split_500": float(split.median()),
        "median_split_500_fmt": _fmt_split(float(split.median())),
        "iqr_split_500": float(split.quantile(0.75) - split.quantile(0.25)),
        "median_hr": int(round(float(hr.median()))),
        "iqr_hr": int(round(float(hr.quantile(0.75) - hr.quantile(0.25)))),
        "n_points": n,
        "intensity_high_pct": round(high, 1),
    }


def ensure_activity_metrics_cached(
    metrics_path: Path,
    activity_id: int,
    activity_name: str,
    start_date: Optional[str],
    sport_type: str,
    description: Optional[str],
    token: str,
    rowing_metrics: Optional[Dict[str, Any]] = None,
    refresh: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Load or compute per-activity metrics JSON (gym tonnage/max per lift + rowing aggregates).
    """
    existing = load_activity_metrics(metrics_path)
    desc = (description or "").strip()
    fp = description_fingerprint(desc) if desc else ""
    if (
        existing
        and not refresh
        and existing.get("description_fingerprint") == fp
        and existing.get("gym_metrics_parser_version") == GYM_METRICS_PARSER_VERSION
        and (not rowing_metrics or existing.get("rowing"))
    ):
        if rowing_metrics and not existing.get("rowing"):
            existing["rowing"] = rowing_metrics
            existing["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_activity_metrics(metrics_path, existing)
        return existing

    record: Dict[str, Any] = {
        "activity_id": activity_id,
        "activity_name": activity_name,
        "start_date": start_date,
        "sport_type": sport_type,
        "description_fingerprint": fp,
        "gym_metrics_parser_version": GYM_METRICS_PARSER_VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if rowing_metrics:
        record["rowing"] = rowing_metrics
    elif existing and existing.get("rowing"):
        record["rowing"] = existing["rowing"]

    if desc:
        parsed = parse_gym_session_metrics(activity_id, activity_name, desc, token)
        if parsed:
            record["gym"] = parsed.to_dict()
        elif existing and existing.get("gym"):
            record["gym"] = existing["gym"]
            record["gym_parse_error"] = "Kagi parse failed; kept previous gym metrics."
    elif existing and existing.get("gym"):
        record["gym"] = existing["gym"]

    save_activity_metrics(metrics_path, record)
    return record


def sync_activity_metrics_cache(
    athletes: Sequence[Any],
    cache_dir: Path,
    athlete_paths_fn: Any,
    activities: Sequence[dict],
    activity_details: Dict[int, dict],
    gym_types: frozenset,
    name_patterns: Sequence[str],
    token: str,
    erg_df: Optional[pd.DataFrame] = None,
    refresh: bool = False,
) -> Dict[int, Dict[str, Any]]:
    erg_df = erg_df if erg_df is not None else pd.DataFrame()
    by_id: Dict[int, Dict[str, Any]] = {}
    athlete_by_act: Dict[int, Any] = {}
    for acfg in athletes:
        paths = athlete_paths_fn(cache_dir, acfg.id)
        idx_path = paths["index"]
        if idx_path.is_file():
            idx = json.loads(idx_path.read_text())
            for act in idx.get("activities", []):
                athlete_by_act[int(act["id"])] = (acfg, paths)

    for act in activities:
        aid = int(act["id"])
        if aid not in athlete_by_act:
            continue
        acfg, paths = athlete_by_act[aid]
        metrics_dir = paths.get("metrics") or (paths["root"] / "metrics")
        detail = activity_details.get(aid, {})
        name = act.get("name") or detail.get("name") or f"activity {aid}"
        st = act.get("sport_type") or act.get("type") or ""
        start = act.get("start_date") or detail.get("start_date")
        rowing = None
        try:
            from suunto_sync import (
                summarize_suunto_rowing_metrics,
                summarize_suunto_rowing_metrics_by_key,
            )

            suunto_key = act.get("suunto_key")
            if suunto_key:
                rowing = summarize_suunto_rowing_metrics_by_key(
                    cache_dir, acfg.id, str(suunto_key)
                )
            elif aid > 0:
                rowing = summarize_suunto_rowing_metrics(cache_dir, acfg.id, aid)
        except Exception:
            rowing = None
        if not rowing:
            rowing = summarize_rowing_metrics_from_df(erg_df, aid)
        desc: Optional[str] = None
        if is_gym_activity(act, gym_types, name_patterns):
            desc = (detail.get("description") or "").strip() or None
            if not desc:
                try:
                    from gym_suunto import (
                        suunto_gym_description,
                        suunto_gym_description_for_strava,
                    )

                    suunto_key = act.get("suunto_key")
                    if suunto_key:
                        desc = suunto_gym_description(
                            cache_dir, acfg.id, str(suunto_key)
                        )
                    elif aid > 0:
                        desc = suunto_gym_description_for_strava(
                            cache_dir, acfg.id, aid
                        )
                except ImportError:
                    pass
        mpath = activity_metrics_path(metrics_dir, aid)
        if not desc and not rowing:
            cached = load_activity_metrics(mpath)
            if cached:
                by_id[aid] = cached
            continue
        record = ensure_activity_metrics_cached(
            mpath,
            aid,
            str(name),
            str(start) if start else None,
            str(st),
            desc,
            token,
            rowing_metrics=rowing,
            refresh=refresh,
        )
        if record:
            by_id[aid] = record
    return by_id


def format_gym_metrics_summary(
    metrics_by_id: Mapping[int, Dict[str, Any]],
    week_label: str,
) -> str:
    """Human-readable gym tonnage + max-weight context from cached activity metrics."""
    sessions: List[Tuple[datetime, str]] = []
    for aid, rec in metrics_by_id.items():
        gym = rec.get("gym")
        if not gym:
            continue
        start = _parse_activity_start(rec.get("start_date"))
        lines = [
            f"Activity {aid} — {rec.get('activity_name', gym.get('activity_name', '?'))}:",
            f"  Total tonnage: {gym.get('total_tonnage_kg', 0):.0f} kg",
        ]
        for ex in gym.get("exercises") or []:
            lines.append(
                f"  - {ex.get('name')}: max {ex.get('max_weight_kg', 0):.1f} kg, "
                f"tonnage {ex.get('tonnage_kg', 0):.0f} kg"
            )
            sets = ex.get("sets") or []
            if sets:
                set_strs = [
                    f"{s.get('reps')}×{s.get('weight_kg', 0):.0f}kg"
                    for s in sets[:6]
                ]
                if len(sets) > 6:
                    set_strs.append("…")
                lines.append(f"    Sets: {', '.join(set_strs)}")
        if gym.get("assumptions"):
            lines.append(f"  Notes: {gym['assumptions']}")
        sessions.append((start or datetime.min.replace(tzinfo=timezone.utc), "\n".join(lines)))

    if not sessions:
        return "No gym sessions with parsed metrics in the review window."

    sessions.sort(key=lambda x: x[0])
    total = sum(
        float(rec.get("gym", {}).get("total_tonnage_kg", 0))
        for rec in metrics_by_id.values()
        if rec.get("gym")
    )
    header = f"Gym metrics ({week_label}). Aggregate tonnage: {total:.0f} kg\n"
    return header + "\n\n".join(block for _, block in sessions)


def format_exercise_history_for_plan(
    metrics_by_id: Mapping[int, Dict[str, Any]],
) -> str:
    """Per-exercise recent max and tonnage for target-weight prescriptions."""
    by_exercise: Dict[str, List[Tuple[datetime, float, float]]] = {}
    for rec in metrics_by_id.values():
        gym = rec.get("gym")
        if not gym:
            continue
        start = _parse_activity_start(rec.get("start_date")) or datetime.min.replace(
            tzinfo=timezone.utc
        )
        for ex in gym.get("exercises") or []:
            name = str(ex.get("name", "")).strip()
            if not name:
                continue
            by_exercise.setdefault(name, []).append(
                (
                    start,
                    float(ex.get("max_weight_kg", 0)),
                    float(ex.get("tonnage_kg", 0)),
                )
            )
    if not by_exercise:
        return "(no parsed gym history — prescribe conservatively from athlete level)"

    lines = ["Recent lift history (use for target weights per set):"]
    for name in sorted(by_exercise.keys()):
        entries = sorted(by_exercise[name], key=lambda x: x[0])
        latest = entries[-1]
        best_max = max(e[1] for e in entries)
        lines.append(
            f"- {name}: latest max {latest[1]:.1f} kg (session tonnage {latest[2]:.0f} kg); "
            f"window best max {best_max:.1f} kg ({len(entries)} session(s))"
        )
    return "\n".join(lines)


def _call_llm(
    system: str,
    user: str,
    api_key: str,
    *,
    history: Optional[Sequence[ChatMessage]] = None,
    timeout: int = 60,
    response_format: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
) -> str:
    from openrouter_client import openrouter_structured_model

    if model is None and response_format is not None:
        model = openrouter_structured_model()
    return call_openrouter(
        system=system,
        user=user,
        api_key=api_key,
        conversation_history=history,
        timeout=timeout,
        response_format=response_format,
        model=model,
    )


@dataclass(frozen=True)
class GeneratedWeeklyPlan:
    plan_text: str
    plan_json: Optional[Dict[str, Any]] = None


_PLAN_WEEKLY_TARGETS_TAIL_RE = re.compile(
    r"\n+---?\s*\n+\s*\*?\*?Weekly\s+Targets\*?\*?.*\Z",
    re.I | re.DOTALL,
)
_PLAN_ESSAY_HEADING_RE = re.compile(
    r"\A\s*\*?\*?Squad\s+Weekly\s+Training\s+Plan.*?\*?\*?\s*\n+---+\s*\n+",
    re.I | re.DOTALL,
)


def sanitize_plan_prose(plan_text: str) -> str:
    """Strip LLM essay wrappers and hallucinated weekly-target summaries."""
    text = (plan_text or "").strip()
    if not text:
        return text
    text = _PLAN_WEEKLY_TARGETS_TAIL_RE.sub("", text).strip()
    text = re.sub(
        r"\n+\*?\*?Weekly\s+Targets\*?\*?.*\Z",
        "",
        text,
        flags=re.I | re.DOTALL,
    ).strip()
    text = _PLAN_ESSAY_HEADING_RE.sub("", text).strip()
    text = re.sub(
        r"\n+Ensure all athletes adhere.*\Z",
        "",
        text,
        flags=re.I | re.DOTALL,
    ).strip()
    return text


def finalize_plan_text_for_display(
    plan_text: str,
    plan_json: Optional[Dict[str, Any]] = None,
) -> str:
    """Sanitize LLM prose and append computed volume summary when JSON is available."""
    body = sanitize_plan_prose(plan_text)
    if plan_json:
        summary = format_plan_prescribed_summary(plan_json)
        if summary and summary not in body:
            body = f"{body}\n\n{summary}"
    return body.strip()


_ROWING_ZONE_SESSION_TEMPLATES = (
    "CANONICAL ROWING MAIN SETS (adapt duration to weekly load; keep Z/T coherent; "
    "total session ≤45 min including WU/CD ≤15 min each):\n"
    "- Z2/T3–T4 aerobic: continuous 20–25 min OR 2×12 min / 2 min rest; priority hr\n"
    "- Z3/T5 UT1-style: 3×8 min / 2 min rest OR 2×10 min / 2 min rest; priority hr\n"
    "- Z4/T6 threshold: 4×5 min / 2 min rest OR 3×6 min / 2 min rest; "
    "HR must sit in athlete Z4 and T6 bpm tables when personalised\n"
    "- Z5/T7 VO2: short reps (e.g. 6×500 m / ≥3 min rest); only when phase/cap allows\n"
    "- Never pair easy Z1–Z2 with T6–T7; never label threshold work with HR below the "
    "athlete T6/Z4 floor on personalised plans\n"
)


_INTERVAL_SESSION_REPAIR_SYSTEM = (
    "You are an elite rowing coach. Repair one erg/on-water interval session so it is "
    "a realistic multi-piece workout an athlete can execute on a Concept2 erg or on water.\n"
    "Rules:\n"
    "- Keep warm_up and cool_down segments (≤15 min each; typically ~8 min).\n"
    "- Main work MUST use explicit reps×distance or reps×duration WITH rest between "
    "pieces in the main_set duration field, e.g. '5×8 min / 2 min rest' or "
    "'6×500 m / 2:00 rest' or '4×1000 m / 3 min rest'.\n"
    "- Do NOT use flat continuous labels like 'Threshold intervals — 40 min' or "
    "'48 min @ Z4' when the session is interval work.\n"
    "- Total session (WU + main + rest + CD) must be ≤45 minutes.\n"
    "- Preserve zones (zone_z/zone_t), HR bands, splits, and priority when sensible.\n"
    "- session_subtype should be 'intervals' (or 'vo2' when appropriate).\n"
    "- Return JSON only matching the schema: "
    '{"session_subtype": "...", "segments": [ ... rowing segments ... ]}.'
    + _ROWING_ZONE_SESSION_TEMPLATES
)


def _interval_days_needing_repair(
    plan: WeeklyPlan,
) -> List[Tuple[Any, str]]:
    from weekly_plan_schema import validate_rowing_interval_rest

    bad: List[Tuple[Any, str]] = []
    for day in plan.days:
        if day.session_type not in ("erg", "on_water") or day.rowing is None:
            continue
        err = validate_rowing_interval_rest(day)
        if err:
            bad.append((day, err))
    return bad


def _apply_repaired_day_segments(
    plan_json: Dict[str, Any],
    weekday: str,
    *,
    session_subtype: Optional[str],
    segments: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return a shallow-copied plan with one day's rowing segments replaced."""
    out = dict(plan_json)
    days = list(out.get("days") or [])
    new_days: List[Any] = []
    for raw_day in days:
        if not isinstance(raw_day, dict):
            new_days.append(raw_day)
            continue
        if str(raw_day.get("weekday") or "") != weekday:
            new_days.append(raw_day)
            continue
        day = dict(raw_day)
        if session_subtype is not None:
            day["session_subtype"] = session_subtype
        rowing = dict(day.get("rowing") or {})
        rowing["segments"] = [dict(s) for s in segments]
        day["rowing"] = rowing
        new_days.append(day)
    out["days"] = new_days
    return out


def _parse_interval_repair_response(
    raw: str,
) -> Tuple[Optional[str], Optional[List[Dict[str, Any]]]]:
    """Extract session_subtype + segments from an LLM repair reply."""
    text = (raw or "").strip()
    if not text or is_openrouter_error(text):
        return None, None
    # Tolerate fenced JSON
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
    if fence:
        text = fence.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None, None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None, None
    if not isinstance(payload, dict):
        return None, None
    subtype = payload.get("session_subtype")
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        return None, None
    cleaned: List[Dict[str, Any]] = []
    for seg in segments:
        if isinstance(seg, dict):
            cleaned.append(dict(seg))
    if not cleaned:
        return None, None
    subtype_str = str(subtype).strip() if subtype is not None else None
    return subtype_str, cleaned


def ensure_realistic_interval_sessions(
    plan_json: Dict[str, Any],
    api_key: str,
    *,
    max_attempts: int = 3,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Iterate LLM repairs until interval/threshold days have realistic reps×rest.

    If the plan already validates, returns it unchanged. Without an API key,
    returns the input unchanged (caller must validate).
    """
    from weekly_plan_schema import validate_rowing_interval_rest, weekly_plan_to_dict

    if not isinstance(plan_json, dict):
        return plan_json

    current = dict(plan_json)
    parsed = parse_weekly_plan(current)
    if parsed is None:
        return current

    bad = _interval_days_needing_repair(parsed)
    if not bad:
        return current
    if not (api_key or "").strip():
        print(
            "Interval repair skipped: OPENROUTER_API_KEY missing; "
            f"invalid days: {[d.weekday for d, _ in bad]}",
            flush=True,
        )
        return current

    # Prefer a capable structured model for interval repair when not overridden.
    repair_model = model
    if repair_model is None:
        from openrouter_client import openrouter_structured_model

        repair_model = openrouter_structured_model()

    for day, err in bad:
        weekday = day.weekday
        day_dict = next(
            (
                d
                for d in (current.get("days") or [])
                if isinstance(d, dict) and d.get("weekday") == weekday
            ),
            None,
        )
        if day_dict is None or not isinstance(day_dict.get("rowing"), dict):
            continue

        last_err = err
        for attempt in range(1, max_attempts + 1):
            user = (
                f"Repair the {weekday} rowing session.\n"
                f"Validation error: {last_err}\n\n"
                f"Current day JSON:\n{json.dumps(day_dict, indent=2)}\n\n"
                "Return JSON only with keys session_subtype and segments "
                "(full warm_up + main_set + cool_down). "
                "Main set duration MUST look like '5×8 min / 2 min rest'."
            )
            if attempt > 1:
                user += (
                    f"\n\nPrevious attempt #{attempt - 1} was still invalid: {last_err}. "
                    "Use explicit multi-piece structure with rest; do not emit a single "
                    "continuous block labeled intervals."
                )
            print(
                f"Interval repair: {weekday} attempt {attempt}/{max_attempts}…",
                flush=True,
            )
            raw = _call_llm(
                _INTERVAL_SESSION_REPAIR_SYSTEM,
                user,
                api_key,
                timeout=90,
                model=repair_model,
            )
            subtype, segments = _parse_interval_repair_response(raw)
            if segments is None:
                print(
                    f"Interval repair: {weekday} attempt {attempt} returned unusable JSON",
                    flush=True,
                )
                continue
            candidate = _apply_repaired_day_segments(
                current,
                weekday,
                session_subtype=subtype or day.session_subtype,
                segments=segments,
            )
            cand_plan = parse_weekly_plan(candidate)
            if cand_plan is None:
                last_err = "repaired day did not parse as a weekly plan"
                continue
            cand_day = next(
                (d for d in cand_plan.days if d.weekday == weekday),
                None,
            )
            if cand_day is None:
                last_err = f"{weekday} missing after repair"
                continue
            rest_err = validate_rowing_interval_rest(cand_day)
            if rest_err:
                last_err = rest_err
                # Keep candidate as base for next attempt so the model can iterate
                current = candidate
                day_dict = next(
                    (
                        d
                        for d in (current.get("days") or [])
                        if isinstance(d, dict) and d.get("weekday") == weekday
                    ),
                    day_dict,
                )
                print(
                    f"Interval repair: {weekday} still invalid ({rest_err})",
                    flush=True,
                )
                continue
            current = candidate
            print(f"Interval repair: {weekday} accepted on attempt {attempt}", flush=True)
            break
        else:
            print(
                f"Interval repair: {weekday} still invalid after {max_attempts} attempts "
                f"({last_err})",
                flush=True,
            )

    # Re-serialize through schema when possible so downstream sees clean shapes.
    final_plan = parse_weekly_plan(current)
    if final_plan is not None:
        return weekly_plan_to_dict(final_plan)
    return current


def apply_season_master_plan_alignment(
    cache_dir: Path,
    season_cfg_obj: Any,
    week: WeekBounds,
    plan_json: Optional[Dict[str, Any]],
    *,
    plan_text: Optional[str] = None,
    personalised: bool = False,
    greeting: Optional[str] = None,
    previous_week_plan: Optional[Dict[str, Any]] = None,
    reference_plan: Optional[Dict[str, Any]] = None,
    plan_label: str = "weekly plan",
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Mandatory pass: align plan JSON with season_master_plan.md targets.

    When ``plan_json`` is missing, imports structured JSON from ``plan_text`` first.
    Returns (aligned plan_json or original, log message).
    """
    if plan_json is None and plan_text and plan_text.strip():
        from weekly_plan_harness import (
            finalize_imported_plan_json,
            import_prose_plan_json,
        )

        imported = import_prose_plan_json(
            plan_text.strip(),
            week_start=week.week_start.isoformat(),
            personalised=personalised,
            greeting=greeting if personalised else None,
        )
        if imported is None:
            return None, (
                f"{plan_label}: alignment skipped (could not import plan_text as JSON)"
            )
        finalized, import_err = finalize_imported_plan_json(
            imported,
            include_lifting=True,
            squad_plan_json=reference_plan,
        )
        if finalized is None:
            return None, (
                f"{plan_label}: alignment skipped (imported plan_text failed "
                f"validation: {import_err or 'invalid interval/session structure'})"
            )
        plan_json = finalized

    if plan_json is None:
        return None, f"{plan_label}: alignment skipped (no structured plan JSON)"
    if season_cfg_obj is None:
        return plan_json, f"{plan_label}: alignment skipped (season config unavailable)"

    try:
        from weekly_plan_master_align import (
            enforce_weekly_plan_alignment,
            load_weekly_targets,
        )
    except ImportError as exc:
        return plan_json, f"{plan_label}: alignment skipped ({exc})"

    try:
        weekly_targets = load_weekly_targets(cache_dir, season_cfg_obj)
        if not weekly_targets:
            return plan_json, f"{plan_label}: alignment skipped (no weekly targets)"

        aligned = enforce_weekly_plan_alignment(
            week.week_start.isoformat(),
            plan_json,
            weekly_targets,
            previous_week_plan=previous_week_plan,
            reference_plan=reference_plan,
        )
        if not aligned.aligned:
            return plan_json, f"{plan_label}: alignment skipped (no target for week)"

        lines = [
            f"{plan_label}: season master alignment applied for "
            f"{week.week_start.isoformat()}"
        ]
        if aligned.validation_before.violations:
            lines.append(
                f"  pre-alignment violations: {len(aligned.validation_before.violations)}"
            )
            for v in aligned.validation_before.violations:
                lines.append(f"    [{v.severity}] {v.field}: {v.description}")
        if aligned.validation_after.violations:
            lines.append(
                f"  post-alignment violations: {len(aligned.validation_after.violations)}"
            )
            for v in aligned.validation_after.violations:
                lines.append(f"    [{v.severity}] {v.field}: {v.description}")
        elif aligned.validation_before.violations:
            lines.append("  post-alignment violations: 0")

        # Alignment rewrites can reintroduce flat interval labels — reject those.
        aligned_plan = aligned.plan_json
        if isinstance(aligned_plan, dict):
            aligned_plan = ensure_realistic_interval_sessions(
                aligned_plan,
                api_key=os.environ.get("OPENROUTER_API_KEY", ""),
                max_attempts=3,
            )
            parsed_aligned = parse_weekly_plan(aligned_plan)
            if parsed_aligned is not None:
                from weekly_plan_schema import validate_plan_session_constraints

                constraint_err = validate_plan_session_constraints(parsed_aligned)
                if constraint_err:
                    lines.append(f"  post-alignment interval repair needed: {constraint_err}")

        return aligned_plan, "\n".join(lines)
    except Exception as exc:
        return plan_json, f"{plan_label}: alignment failed ({exc})"


_STRUCTURED_PLAN_JSON_RULES = (
    "\n\nSTRUCTURED OUTPUT (strict — replaces prose day-by-day output):\n"
    "- Return ONLY a JSON object matching the weekly training plan schema.\n"
    "- version must be 1; days must contain exactly 7 entries (Monday through Sunday).\n"
    "- Gym days (Mon/Wed when lifting enabled): session_type=gym, gym object with "
    "category leg or upper_core (different on each gym day), goal, exactly 4 exercises "
    "from the approved pool. Each exercise is a separate object with name + sets array "
    "(reps, weight_kg, duration_sec) — NEVER flatten all sets into one list.\n"
    "- Base-phase gym: ascending pyramids per exercise (Set 1 lightest ~60% 8RM, "
    "each set heavier; 3–4 sets). Build-phase gym: reverse pyramids (Set 1 heaviest "
    "~100% 8RM, each set lighter; 3–4 sets). Deload: exactly 2 flat sets at 80–85%.\n"
    "- Erg days: session_type=erg, rowing object with warm_up, main_set, cool_down "
    "segments; all multi-rep work (distance or time) needs rest between pieces "
    "(rest segment or '/ N min rest' in duration). Use 'm'/'km' for distance and "
    "'min' for time in duration fields.\n"
    "- On-water days: session_type=on_water, rowing with indicative splits and "
    "erg_alternative time-interval fallback for group erg sessions.\n"
    "- Each erg/on-water session must total ≤45 min (warm-up through cool-down); "
    "gym sessions may be up to 90 min.\n"
    "- Rest/recovery days: session_type rest or recovery; gym and rowing must be null.\n"
    "- Set null for unused gym/rowing fields on non-applicable days.\n"
    + _ROWING_ZONE_SESSION_TEMPLATES
)


def _parse_structured_plan_or_error(
    raw: str,
    *,
    include_lifting: bool,
    squad_plan: Optional[Dict[str, Any]] = None,
    phase: Optional[str] = None,
    priority: str = "hr",
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    from weekly_plan_harness import parse_structured_plan_or_error

    return parse_structured_plan_or_error(
        raw,
        include_lifting=include_lifting,
        squad_plan=squad_plan,
        phase=phase,
        priority=priority,
    )


def _try_parse_structured_plan(
    raw: str,
    *,
    include_lifting: bool,
    squad_plan: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    plan_dict, _err = _parse_structured_plan_or_error(
        raw, include_lifting=include_lifting, squad_plan=squad_plan
    )
    return plan_dict


def _validate_parsed_weekly_plan(
    plan: WeeklyPlan,
    *,
    include_lifting: bool,
    squad_plan_json: Optional[Dict[str, Any]] = None,
    goal_tracking: Optional[str] = None,
    phase: Optional[str] = None,
    athlete_profile: Optional[AthleteProfile] = None,
) -> Optional[str]:
    from weekly_plan_harness import validate_parsed_weekly_plan as _validate

    return _validate(
        plan,
        include_lifting=include_lifting,
        squad_plan_json=squad_plan_json,
        goal_tracking=goal_tracking,
        phase=phase,
        athlete_profile=athlete_profile,
    )


def _generate_structured_plan_with_fallback(
    system: str,
    user: str,
    api_key: str,
    *,
    history: Optional[Sequence[ChatMessage]] = None,
    include_lifting: bool = True,
    squad_plan_json: Optional[Dict[str, Any]] = None,
    goal_tracking: Optional[str] = None,
    phase: Optional[str] = None,
    plan_week: Optional[WeekBounds] = None,
    personalised: bool = False,
    greeting: Optional[str] = None,
    athlete_profile: Optional[AthleteProfile] = None,
    prose_fallback: Callable[[], str],
    timeout: int = 120,
) -> GeneratedWeeklyPlan:
    from weekly_plan_harness import (
        MAX_STRUCTURED_ATTEMPTS,
        SCHEDULE_RETRY_HINT,
        STRUCTURED_JSON_SKELETON,
        build_retry_feedback,
        build_validation_retry_feedback,
        finalize_imported_plan_json,
        import_prose_plan_json,
    )

    structured_system = (
        system.strip()
        + _STRUCTURED_PLAN_JSON_RULES
        + STRUCTURED_JSON_SKELETON
        + f"\n{SCHEDULE_RETRY_HINT}\n"
        + "\nIgnore any earlier instruction to return prose; output JSON only."
    )
    is_athlete = squad_plan_json is not None or personalised
    if is_athlete:
        structured_system += (
            "\n- personalised must be true; include a one-line greeting with the "
            "athlete's first name."
        )
    else:
        structured_system += "\n- personalised must be false; greeting must be null."
    response_format = openrouter_response_format(
        name="athlete_weekly_plan" if is_athlete else "squad_weekly_plan"
    )
    retry_feedback = ""
    for _attempt in range(MAX_STRUCTURED_ATTEMPTS):
        user_msg = user + retry_feedback
        raw = _call_llm(
            structured_system,
            user_msg,
            api_key,
            history=history,
            timeout=timeout,
            response_format=response_format,
        )
        if is_openrouter_error(raw):
            continue
        plan_dict, parse_err = _parse_structured_plan_or_error(
            raw,
            include_lifting=include_lifting,
            squad_plan=squad_plan_json,
            phase=phase,
        )
        if plan_dict is None:
            retry_feedback = build_retry_feedback(parse_err)
            continue
        parsed = parse_weekly_plan(plan_dict)
        if parsed is not None:
            validation_err = _validate_parsed_weekly_plan(
                parsed,
                include_lifting=include_lifting,
                squad_plan_json=squad_plan_json,
                goal_tracking=goal_tracking if squad_plan_json is None else None,
                phase=phase,
                athlete_profile=athlete_profile,
            )
            if validation_err:
                retry_feedback = build_validation_retry_feedback(validation_err)
                continue
            plan_dict = ensure_realistic_interval_sessions(
                plan_dict,
                api_key=api_key,
                max_attempts=3,
            )
            parsed = parse_weekly_plan(plan_dict)
            if parsed is None:
                retry_feedback = build_retry_feedback(
                    "interval repair produced unparseable plan JSON"
                )
                continue
            validation_err = _validate_parsed_weekly_plan(
                parsed,
                include_lifting=include_lifting,
                squad_plan_json=squad_plan_json,
                goal_tracking=goal_tracking if squad_plan_json is None else None,
                phase=phase,
                athlete_profile=athlete_profile,
            )
            if validation_err:
                retry_feedback = build_validation_retry_feedback(validation_err)
                continue
            return GeneratedWeeklyPlan(
                plan_json=plan_dict,
                plan_text=render_plan_text(parsed),
            )
    plan_text = sanitize_plan_prose(prose_fallback())
    if is_openrouter_error(plan_text):
        plan_text = "Weekly plan unavailable (LLM error)."
        return GeneratedWeeklyPlan(plan_text=plan_text, plan_json=None)

    week_start = (plan_week or plan_week_bounds()).week_start.isoformat()
    imported = import_prose_plan_json(
        plan_text,
        week_start=week_start,
        personalised=is_athlete,
        greeting=greeting,
    )
    if imported is not None:
        finalized, import_err = finalize_imported_plan_json(
            imported,
            include_lifting=include_lifting,
            squad_plan_json=squad_plan_json,
            goal_tracking=goal_tracking if squad_plan_json is None else None,
            phase=phase,
            athlete_profile=athlete_profile,
        )
        if finalized is None and import_err:
            # Flat interval prose often imports but fails session constraints —
            # iterate LLM repair before giving up on structured JSON.
            print(
                f"Prose import failed validation: {import_err}; "
                "attempting interval LLM repair…",
                flush=True,
            )
            repaired = ensure_realistic_interval_sessions(
                imported,
                api_key=api_key,
                max_attempts=4,
            )
            finalized, import_err = finalize_imported_plan_json(
                repaired,
                include_lifting=include_lifting,
                squad_plan_json=squad_plan_json,
                goal_tracking=goal_tracking if squad_plan_json is None else None,
                phase=phase,
                athlete_profile=athlete_profile,
            )
        if finalized is not None:
            parsed = parse_weekly_plan(finalized)
            if parsed is not None:
                print(
                    "Structured plan: recovered JSON from prose fallback via import.",
                    flush=True,
                )
                return GeneratedWeeklyPlan(
                    plan_json=finalized,
                    plan_text=render_plan_text(parsed),
                )
        if import_err:
            print(f"Prose import failed validation: {import_err}", flush=True)

    return GeneratedWeeklyPlan(plan_text=plan_text, plan_json=None)


_STRATEGIC_GOALS_CONTEXT = (
    "Strategic goals (context only — do NOT repeat or explain these in your output; "
    "use them only to shape session choices):\n"
    "1. Head of the Yarra (November): 8.5 km eights race — aerobic endurance, "
    "race-pace pieces over distance, crew rhythm for a long head race.\n"
    "2. Victoria State Championships: club 4x− over 2 km — target crew time 6:40; "
    "2k-specific power, starts, rate builds, lactate-tolerance work.\n"
)

_PROGRAMMING_GUIDELINES_CONTEXT = (
    "Programming Guidelines (use these to structure the weekly load):\n"
    "1. Focus on 'Intent': Prioritize maximal velocity (explosive concentric phase) "
    "and precise load management.\n"
    "   - For lightweight rowers (60-75kg), focus on neural adaptations: low volume "
    "(3-5 reps), high intensity (>85% 1RM), and long rest periods.\n"
    "   - Avoid training to failure; prioritize quality of movement and speed of the "
    "bar.\n"
    "2. Periodization: Align gym intensity with water intensity.\n"
    "   - Early season: Hypertrophy/durability (moderate volume).\n"
    "   - Pre-championships (2k focus): Maximal strength/power (low volume, high load, "
    "explosive intent).\n"
    "3. Load Management: Avoid heavy leg sessions (Back Squat/Hex-bar) within 24 hours "
    "of high-intensity erg sessions.\n"
    "4. Session Structure: Prioritize compound lifts first, followed by core/accessory "
    "work.\n"
    "5. A/B split: Keep one leg/posterior-chain dominant day and one "
    "upper-body/core dominant day (Mon/Wed). Exercise names come from the squad "
    "gym program — do not invent or rotate the menu week to week.\n"
    "6. Set pyramids (base/build only): Base phase uses ascending pyramids "
    "(light→heavy: ~60%/75%/87%/100% of 8RM with 12/10/8/7 reps). Build phase uses "
    "reverse pyramids (heavy→light: ~100%/85%/78%/70% of 8RM with 5/6/8/10 reps). "
    "Set 1 must never be the heaviest working weight in base; build leads with the "
    "heaviest set. Deload: flat 2 sets at 80–85% only."
)


def programming_guidelines_context() -> str:
    """Strength programming guidelines used to structure weekly gym load."""
    return _PROGRAMMING_GUIDELINES_CONTEXT

_GYM_EXERCISE_DISPLAY: Dict[str, str] = {
    "incline bench press": "Incline bench press",
    "incline bench": "Incline bench press",
    "back squat to box": "Back squat to box",
    "bulgarian split squat": "Bulgarian split squat",
    "bulgarian": "Bulgarian split squat",
    "hex-bar deadlift": "Hex-bar deadlift",
    "romanian deadlift": "Romanian deadlift",
    "rdl": "Romanian deadlift",
    "rdls": "Romanian deadlift",
    "kettlebell swings": "Kettlebell swings",
    "russian twists": "Russian twists",
    "russian twist": "Russian twists",
    "lat pull-down": "Lat pull-down",
    "barbell row": "Barbell row",
    "bench press": "Bench press",
    "back squat": "Back squat",
    "arnold press": "Arnold press",
    "lat pulls": "Lat pulls",
    "pull-ups": "Pull-ups",
    "pull ups": "Pull-ups",
    "plank": "Plank",
}

_GYM_EXERCISE_MATCH_NAMES: Tuple[str, ...] = tuple(
    sorted(_GYM_EXERCISE_DISPLAY.keys(), key=len, reverse=True)
)

# A/B session split: leg/posterior-chain dominant vs upper-body/core dominant.
GYM_CATEGORY_LEG = "leg"
GYM_CATEGORY_UPPER_CORE = "upper_core"

_GYM_EXERCISE_CATEGORY: Dict[str, str] = {
    "back squat": GYM_CATEGORY_LEG,
    "back squat to box": GYM_CATEGORY_LEG,
    "hex-bar deadlift": GYM_CATEGORY_LEG,
    "romanian deadlift": GYM_CATEGORY_LEG,
    "rdl": GYM_CATEGORY_LEG,
    "rdls": GYM_CATEGORY_LEG,
    "bulgarian split squat": GYM_CATEGORY_LEG,
    "bulgarian": GYM_CATEGORY_LEG,
    "kettlebell swings": GYM_CATEGORY_LEG,
    "bench press": GYM_CATEGORY_UPPER_CORE,
    "incline bench press": GYM_CATEGORY_UPPER_CORE,
    "incline bench": GYM_CATEGORY_UPPER_CORE,
    "barbell row": GYM_CATEGORY_UPPER_CORE,
    "lat pull-down": GYM_CATEGORY_UPPER_CORE,
    "lat pulls": GYM_CATEGORY_UPPER_CORE,
    "pull-ups": GYM_CATEGORY_UPPER_CORE,
    "pull ups": GYM_CATEGORY_UPPER_CORE,
    "arnold press": GYM_CATEGORY_UPPER_CORE,
    "russian twists": GYM_CATEGORY_UPPER_CORE,
    "russian twist": GYM_CATEGORY_UPPER_CORE,
    "plank": GYM_CATEGORY_UPPER_CORE,
}

_GYM_CATEGORY_LABEL = {
    GYM_CATEGORY_LEG: "leg/posterior-chain",
    GYM_CATEGORY_UPPER_CORE: "upper-body/core",
}

_REFERENCE_MONDAY = date(2025, 1, 6)
_REFERENCE_WEDNESDAY = _REFERENCE_MONDAY + timedelta(days=2)


def normalize_gym_exercise_header(line: str) -> Optional[str]:
    """Map a free-text exercise header line to a canonical exercise name."""
    text = re.sub(r"^\d+\.\s*", "", (line or "").strip())
    text = re.sub(r"^\*+|\*+$", "", text).strip().rstrip(":")
    if not text:
        return None
    lower = text.lower()
    for match in _GYM_EXERCISE_MATCH_NAMES:
        if re.search(re.escape(match), lower, re.I):
            return _GYM_EXERCISE_DISPLAY[match]
    return _GYM_EXERCISE_DISPLAY.get(lower)


def classify_gym_exercise(name: str) -> Optional[str]:
    """Return the A/B category (leg vs upper/core) for a known exercise name."""
    text = (name or "").lower()
    for match in _GYM_EXERCISE_MATCH_NAMES:
        if re.search(re.escape(match), text):
            return _GYM_EXERCISE_CATEGORY.get(match)
    return None


def classify_gym_exercise_list(names: Sequence[str]) -> str:
    """Dominant A/B category for a set of exercise names ('leg', 'upper_core', 'mixed')."""
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


def _gym_exercises_in_text(text: str) -> List[str]:
    found: List[str] = []
    for name in _GYM_EXERCISE_MATCH_NAMES:
        if re.search(re.escape(name), text, re.I):
            display = _GYM_EXERCISE_DISPLAY[name]
            if display not in found:
                found.append(display)
    return found


def extract_gym_exercises_by_day(
    plan_text: str,
    plan_json: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[str]]:
    """Map each gym weekday (Monday/Wednesday) to its prescribed exercise names."""
    if plan_json:
        from weekly_plan_schema import extract_gym_exercises_by_day_from_json

        by_day = extract_gym_exercises_by_day_from_json(plan_json)
        if by_day:
            return by_day
    out: Dict[str, List[str]] = {}
    if not plan_text or not plan_text.strip():
        return out
    for ref_date in (_REFERENCE_MONDAY, _REFERENCE_WEDNESDAY):
        section = extract_session_for_date(plan_text, ref_date)
        if not section:
            continue
        day_name, body = section
        names = _gym_exercises_in_text(body)
        if names:
            out[day_name] = names
    return out


def extract_gym_exercises_from_plan_text(plan_text: str) -> List[str]:
    """Return gym exercise names prescribed on Mon/Wed in a weekly plan."""
    found: List[str] = []
    for names in extract_gym_exercises_by_day(plan_text).values():
        for name in names:
            if name not in found:
                found.append(name)
    return found


def format_previous_week_gym_exercises(
    plan_text: str,
    plan_json: Optional[Dict[str, Any]] = None,
) -> str:
    """Context block listing last week's gym exercises, split by A/B day type."""
    if plan_json:
        text = format_previous_week_gym_exercises_from_json(plan_json)
        if text:
            return text
    by_day = extract_gym_exercises_by_day(plan_text)
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


_GYM_EXERCISE_OPTIONS_CONTEXT = (
    "Gym structure comes from the squad gym program. Monday is a 4-exercise "
    "leg/posterior-chain day; Wednesday is a 4-exercise upper-body/core day. "
    "Do NOT change exercise names, order, or categories; code overwrites gym days "
    "from the program. Only leave plausible kg placeholders if the schema requires them.\n"
)


def _gym_output_format_guide() -> str:
    return (
        "GYM OUTPUT FORMAT (strict — Monday and Wednesday):\n"
        "- Exactly 4 exercises per gym day from the approved pool for that day's "
        "category (leg/posterior-chain vs upper-body/core).\n"
        "- Group sets under each exercise — NEVER one flat numbered list of sets "
        "across multiple exercises.\n"
        "- Example:\n"
        "  Goal: strength (leg/posterior-chain)\n"
        "  1. Back squat\n"
        "  Set 1: 8×70 kg\n"
        "  Set 2: 8×80 kg\n"
        "  2. Romanian deadlift\n"
        "  Set 1: 8×75 kg\n"
        "  ... (exercises 3 and 4)\n"
        "  4. Plank\n"
        "  Set 1: 30s hold\n"
        "- Timed holds use duration_sec in JSON; render as 'Set N: 30s hold'."
    )


def _session_volume_and_units_guide() -> str:
    return (
        "SESSION DURATION AND UNITS (strict — all gym/erg/rowing sessions):\n"
        "- Each session (warm-up through cool-down) must total ≤45 minutes.\n"
        "- If weekly rowing volume cannot fit within 45 min on Tue/Thu, add an extra "
        "erg or on-water session on Friday OR Saturday (not both) — each session still "
        "≤45 min.\n"
        "- Distances: always suffix with 'm' or 'km' (e.g. '500 m', '5×1 km', '6 km').\n"
        "- Durations: always suffix with 'min' (e.g. '12 min', '45 min', "
        "'4×6 min / 3 min rest') — avoids confusion with metres.\n"
        "- Interval prescriptions MUST specify rest between work pieces (distance AND "
        "time): either a separate Rest segment (phase=rest) after each work segment, "
        "or rest in the duration string (e.g. '5×1 km / 2 min rest', "
        "'4×6 min / 3 min rest', '3×8 min / 2 min rest')."
    )


def _weekly_plan_lifting_clause(
    include_lifting: bool,
    *,
    fixed_exercises: bool = False,
    phase: Optional[str] = None,
) -> str:
    if not include_lifting:
        return (
            "Do not schedule gym or weightlifting; use Monday and Wednesday mornings "
            "for rest or light recovery only."
        )
    if fixed_exercises:
        # Athlete DM plan: exercises already chosen by the squad plan; tailor only loads.
        return (
            f"{_gym_output_format_guide()}\n\n"
            "Monday and Wednesday mornings are gym sessions. Keep the EXACT gym "
            "exercises, days, categories (Monday=leg, Wednesday=upper/core), and order "
            "from the squad plan — do not add, drop, swap, or substitute any exercise. "
            "Never copy Monday leg exercises onto Wednesday. Tailor per-set reps and "
            "target weight (kg) to THIS athlete's lift history; keep the same set count "
            "per exercise as the squad plan unless history clearly warrants a change."
        )
    from weekly_plan_schema import is_low_intensity_plan_phase

    if is_low_intensity_plan_phase(phase):
        return (
            f"{_gym_output_format_guide()}\n\n"
            "Monday and Wednesday mornings are gym sessions (deload/recovery/taper).\n"
            "- Use EXACTLY the same exercises as the preceding build week when "
            "previous week gym exercises are listed in context. Do NOT add, drop, "
            "swap, or rotate exercises.\n"
            "- Exactly 2 working sets per exercise (not 1, not 3).\n"
            "- Reduce each exercise's load to 80–85% of the recent working weight, "
            "rounded to the nearest 2.5 kg; keep the same rep range as the build week.\n"
            "- A/B split: keep Monday leg/posterior-chain and Wednesday upper-body/core.\n"
        )
    return (
        f"{_gym_output_format_guide()}\n\n"
        "Monday and Wednesday mornings are gym sessions (base/build load weeks).\n"
        "- A/B session split (strict): Monday = leg/posterior-chain dominant; "
        "Wednesday = upper-body/core dominant. Never duplicate Monday's exercise list "
        "on Wednesday.\n"
        "- Exactly 3–4 working sets per exercise (not 1–2, not 5+).\n"
        "- Target weights must progress logically from the lift history below "
        "(latest max_weight_kg and tonnage per exercise); differentiate warm-up vs "
        "top sets when appropriate.\n"
        "- Exercise names come from the gym program. Do NOT rotate, add, drop, or "
        "substitute exercises."
    )


def _squad_rowing_alignment_clause(
    *,
    phase: Optional[str] = None,
    goal_tracking: Optional[str] = None,
) -> str:
    from weekly_plan_schema import is_low_intensity_plan_phase

    if is_low_intensity_plan_phase(phase):
        return (
            "ROWING SESSION ALIGNMENT (strict — deload/recovery/taper):\n"
            "- Season master plan phase OVERRIDES season goal tracking for rowing "
            "intensity this week.\n"
            "- Tuesday erg and Thursday on-water/erg: all main work MUST stay Z2/T3 "
            "(T1–T3); default priority: HR on every main segment.\n"
            "- Do NOT prescribe Z4/Z5, threshold, VO2max, or race-pace intervals.\n"
            "- Thursday on-water MUST include erg_alternative with matching aerobic "
            "structure for group erg fallback.\n\n"
        )
    phase_norm = (phase or "").strip().lower()
    if phase_norm == "base":
        return (
            "ROWING SESSION ALIGNMENT (strict — base phase):\n"
            "- Season master plan phase OVERRIDES goal-tracking race-pace language.\n"
            "- Primary quality work: T3–T4 threshold (Z3/Z4) on ONE rowing day; the other "
            "day is longer aerobic T2–T3.\n"
            "- Weekly Z5/T5 cap (~8%) is for brief VO2max touches only — do NOT prescribe "
            "full Z5 interval sessions on Tuesday.\n"
            "- Tuesday erg and Thursday on-water MUST each include warm-up (≤15 min) "
            "and cool-down (≤15 min) segments in addition to the main set.\n"
            "- Thursday on-water MUST include erg_alternative with parallel structure.\n\n"
        )
    if phase_norm == "build":
        return (
            "ROWING SESSION ALIGNMENT (strict — build phase):\n"
            "- Mix T3 aerobic volume with structured T4 threshold and capped T5/Z5 work "
            "within the weekly intensity cap from season master plan.\n"
            "- Tuesday erg and Thursday on-water MUST each include warm-up (≤15 min) "
            "and cool-down (≤15 min) segments in addition to the main set.\n"
            "- Thursday on-water MUST include erg_alternative with parallel structure.\n\n"
        )
    if not goal_tracking:
        return ""
    return (
        "ROWING SESSION ALIGNMENT (strict):\n"
        "- When season goal tracking appears in the user message, implement the "
        "'This week's rowing prescriptions' section exactly for Tuesday erg and "
        "Thursday on-water (session_subtype, main set structure, splits, zones).\n"
        "- Do NOT prescribe both rowing days as identical Z2 steady-state at the same "
        "split range if goal tracking calls for race-pace work (~1:45–1:55), "
        "threshold intervals, or tighter aerobic pacing (~2:00).\n"
        "- Labeling a session 'intervals' while keeping all main work in Z2/T2 at "
        "2:10+ splits is invalid when Next Steps cite sub-1:50 or race-pace work.\n"
        "- Macro Z2% targets are shares of total erg/row time — include Z4/Z5 work "
        "pieces within the stated Z5 intensity cap rather than diluting all sessions "
        "to Z2.\n"
        "- Thursday on-water MUST include erg_alternative with parallel structure.\n\n"
    )


_SESSION_SEGMENT_ZONE_GUIDE = (
    "Map each segment to BOTH a 5-zone label (Z1–Z5) and a 7-zone T-level (T1–T7), "
    "using the athlete's zone tables (derived from Max HR) for the HR bpm range:\n"
    "- Warm-up / Cool-down → Z1–Z2 / T1–T2\n"
    "- Steady state / aerobic base → Z2–Z3 / T3–T4\n"
    "- Extensive aerobic / long distance → Z3 / T4–T5\n"
    "- Threshold / AT pieces → Z4 / T6\n"
    "- VO2max / race-pace / high-rate intervals → Z5 / T7\n"
    "Base/recovery phases bias the Main Set toward Z2–Z3 / T3–T4; build/peak phases "
    "earn Z4–Z5 / T6–T7 work, but only within the weekly intensity caps from the "
    "season master plan."
)

_SESSION_SEGMENT_PRIORITY_GUIDE = (
    "Every segment must end with exactly one priority — either `priority: split` or "
    "`priority: HR` — telling the athlete which target governs execution when split "
    "and HR conflict:\n"
    "- `priority: split` — hold the target split (or split range); ease rate/pressure "
    "or accept HR above the range rather than slowing to fit HR.\n"
    "- `priority: HR` — stay inside the HR bpm range; let split drift slower (or "
    "faster on recovery) rather than breach HR limits.\n"
    "When the season master plan block in the user message sets a WEEKLY EXECUTION "
    "PRIORITY (`split` or `HR`), use that as the default for Main Set segments this "
    "week (warm-up, cool-down, and active recovery still default to `priority: HR` "
    "unless notes say otherwise).\n"
    "Otherwise default by segment intent:\n"
    "- Warm-up, Cool-down, active recovery, steady-state, and aerobic-base work → "
    "priority: HR\n"
    "- Threshold, race-pace, VO2max, and pace-trial intervals → priority: split\n"
    "- Mixed build pieces: choose the priority that matches the session goal and "
    "state it explicitly."
)


def _session_segment_context(*, personalised: bool) -> str:
    """Granularity rules: break every erg/row session into labelled segments."""
    if personalised:
        hr_clause = (
            "the bpm range for that segment's Z/T zone, taken from this athlete's "
            "5-zone (Z1–Z5) and 7-zone (T1–T7) tables above (both derived from Max HR) "
            "— never a bare zone label without bpm numbers"
        )
        split_clause = (
            "a target 500m split RANGE (e.g. '1:58–2:05'), derived from THIS athlete's "
            "recent erg medians for that zone over several weeks of data when available"
        )
    else:
        hr_clause = (
            "the squad-average bpm range for that Z/T zone (label it as squad average)"
        )
        split_clause = (
            "a squad-average target 500m split RANGE (e.g. '1:58–2:05'), derived from "
            "the medians/spread in the training summary"
        )
    return (
        "SESSION GRANULARITY (strict — applies to EVERY erg and on-water rowing "
        "session):\n"
        "- Break each session into explicit, separately-listed segments: Warm-up, "
        "Main Set, Cool-down. Split the Main Set into named sub-blocks (build, work "
        "intervals, active recovery, etc.) whenever it is not a single continuous "
        "piece.\n"
        "- EVERY Tuesday erg and Thursday on-water session MUST include warm-up "
        "(≤15 min) and cool-down (≤15 min) segments listed separately from the "
        "main set — never prescribe main-set-only rowing sessions.\n"
        "- EVERY segment line must specify: (1) target split range, (2) target HR "
        "zone as explicit bpm, (3) zone codes on both the 5-zone (Z) and 7-zone "
        "(T) scales, and (4) whether split or HR is the governing priority.\n"
        "- Output every segment on its own line in EXACTLY this shape:\n"
        "    Warm-up: [duration with min] @ Z[n]/T[n], split [range], HR [range] bpm, "
        "priority: [split|HR]\n"
        "    Main Set: [reps×distance with m/km, or duration with min] @ Z[n]/T[n], "
        "split [range], HR [range] bpm, priority: [split|HR]\n"
        "    Main Set (intervals): [e.g. 5×1 km / 2 min rest] @ Z[n]/T[n], ...\n"
        "    Rest: [duration with min] @ Z[n]/T[n], ... (between work pieces)\n"
        "    Cool-down: [duration with min] @ Z[n]/T[n], split [range], HR [range] bpm, "
        "priority: [split|HR]\n"
        "- [Z[n]/T[n]] lists BOTH the 5-zone level (e.g. Z2, Z3) and the matching "
        "7-zone T-level (e.g. T3, T4) for that segment.\n"
        f"- split [range] is {split_clause}.\n"
        f"- HR [range] bpm is {hr_clause}.\n"
        f"{_SESSION_SEGMENT_ZONE_GUIDE}\n"
        f"{_SESSION_SEGMENT_PRIORITY_GUIDE}\n"
        f"{_session_volume_and_units_guide()}\n"
    )


def generate_squad_weekly_plan(
    training_summary: str,
    token: str,
    include_lifting: bool = True,
    plan_week: Optional[WeekBounds] = None,
    adherence_review: Optional[str] = None,
    goal_tracking: Optional[str] = None,
    gym_tonnage_summary: Optional[str] = None,
    gym_exercise_history: Optional[str] = None,
    previous_week_gym_exercises: Optional[str] = None,
    coach_adjustments: Optional[str] = None,
    zulip_topic_feedback: Optional[str] = None,
    season_week_context: Optional[str] = None,
    phase: Optional[str] = None,
    recent_session_ids: Optional[Sequence[str]] = None,
    peak_kg_by_exercise: Optional[Mapping[str, float]] = None,
    prev_plan_json: Optional[Dict[str, Any]] = None,
    gym_lift_review: Optional[str] = None,
    gym_week_index: Optional[int] = None,
) -> GeneratedWeeklyPlan:
    """
    Squad weekly plan for the public channel: session structure with squad-average
    targets (splits, HR, gym loads). Execution-only day-by-day output.
    """
    from weekly_plan_schema import parse_weekly_plan, render_plan_text
    from session_library import (
        apply_library_sessions_to_plan,
        format_session_library_prompt,
        select_sessions_for_week,
    )

    plan_week = plan_week or plan_week_bounds()
    session_selections = select_sessions_for_week(
        phase=phase,
        recent_session_ids=recent_session_ids,
    )
    session_library_block = format_session_library_prompt(session_selections)
    lifting_clause = _weekly_plan_lifting_clause(include_lifting, phase=phase)
    extra_sections: List[str] = []
    if adherence_review:
        extra_sections.append(
            f"--- Previous week adherence review ---\n{adherence_review}"
        )
    if goal_tracking:
        extra_sections.append(f"--- Season goal tracking ---\n{goal_tracking}")
    if gym_tonnage_summary:
        extra_sections.append(f"--- Gym tonnage (recent) ---\n{gym_tonnage_summary}")
    if gym_exercise_history and include_lifting:
        extra_sections.append(
            f"--- Gym lift history (for target weights) ---\n{gym_exercise_history}"
        )
    if gym_lift_review and include_lifting:
        extra_sections.append(gym_lift_review.strip())
    if previous_week_gym_exercises and include_lifting:
        extra_sections.append(
            "--- Previous week gym exercises (program reuse / deload names) ---\n"
            f"{previous_week_gym_exercises}"
        )
    if coach_adjustments and coach_adjustments.strip():
        extra_sections.append(
            "--- Athlete-requested plan adjustments (apply to this week's plan) ---\n"
            f"{coach_adjustments.strip()}"
        )
    if season_week_context and season_week_context.strip():
        extra_sections.append(season_week_context.strip())
    extra_block = ("\n\n".join(extra_sections) + "\n\n") if extra_sections else ""
    gym_options_block = (
        f"{_GYM_EXERCISE_OPTIONS_CONTEXT}\n\n" if include_lifting else ""
    )

    history = topic_context_to_history(zulip_topic_feedback or "")
    system = (
        "You are an expert rowing coach and strength coach writing the SQUAD weekly "
        "plan for a group channel. Using the squad erg training summary and context "
        "in the user message, write a training plan for execution during the target week.\n\n"
        "TARGET RULES (strict):\n"
        "- Every numeric target (500m splits, HR zones, gym kg) must be a SQUAD AVERAGE "
        "or typical expectation — derived from medians and spread in the training summary, "
        "not tuned to one athlete.\n"
        "- Label expectations as squad averages where helpful (e.g. 'target split ~1:52 "
        "(squad avg)').\n"
        "- Athletes will receive personalised numbers by private message; this post is "
        "the shared schedule and benchmark targets only.\n\n"
        "OUTPUT RULES (strict):\n"
        "- Return EXECUTION ONLY: day-by-day sessions with gym and rowing detail.\n"
        "- Gym days: use the numbered 4-exercise format from the gym output guide.\n"
        "- Rowing days: Warm-up / Main Set / Cool-down segments with Z/T, splits, HR, "
        "priority; intervals include rest between pieces; use m/km/min units.\n"
        "- Do NOT re-state season strategy, goal rationale, or long-term periodisation "
        "narrative. No introductory essay.\n"
        "- Do NOT append 'Weekly Targets', volume totals, intensity-mix percentages, "
        "or closing coach notes — prescribed volume is computed from sessions automatically.\n"
        "- No markdown essay formatting (no **bold**, ## headings, or --- dividers).\n"
        "- When season master plan targets appear in the user message, align session "
        "volume, intensity mix, gym tonnage, and weekly execution priority (split vs HR) "
        "with those macro targets, and keep "
        "T5/Z5 (VO2max/race-pace) volume at or below the weekly intensity cap stated "
        "there. Macro targets are planning inputs only — never restate them as a "
        "summary block at the end.\n"
        "- Start directly with the weekly schedule (e.g. Monday: …).\n\n"
        f"{_STRATEGIC_GOALS_CONTEXT}\n"
        "Use this fixed morning schedule (do not move sessions to other days):\n"
        "- Monday morning: gym (session_type=gym — never rest or erg)\n"
        "- Tuesday morning: erg\n"
        "- Wednesday morning: gym (session_type=gym — never rest or erg)\n"
        "- Thursday morning: rowing on water OR erg (pick one and state which)\n"
        "- Friday and Saturday: rest by default; schedule an extra erg or on-water "
        "session here only when Tue/Thu volume cannot fit within 45 min each.\n"
        "- Sunday: rest or optional recovery only unless clearly justified.\n\n"
        f"{lifting_clause}\n\n"
        f"{gym_options_block}"
        f"{_session_segment_context(personalised=False)}\n"
        f"{_squad_rowing_alignment_clause(phase=phase, goal_tracking=goal_tracking)}"
        "For each erg or rowing session also state the session type (steady-state / "
        "threshold / intervals / race-pace). Reference recent volume and intensity "
        "from the summary. Be concrete and actionable.\n\n"
        "On-water distance intervals: when Thursday (or any session) prescribes "
        "on-water intervals by distance (e.g. 500 m, 1 km pieces), also give an "
        "erg group-session alternative using TIME intervals (not distance)—work "
        "duration, rest duration, rep count, target 500m split range, Z/T HR zone "
        "with bpm, and split-vs-HR priority—so the "
        "same session can be done together on ergs when not on water.\n"
        "When Zulip topic feedback appears in conversation history, treat it as "
        "athlete context and requests since the last logged session; fold into the plan."
        f"{session_library_block}"
    )
    user = (
        f"Target week (Mon–Sun {plan_timezone_name()}): "
        f"{plan_week.week_start.isoformat()} to {plan_week.week_end.isoformat()}.\n\n"
        f"{extra_block}"
        f"--- Training summary ---\n{training_summary}"
    )
    generated = _generate_structured_plan_with_fallback(
        system,
        user,
        token,
        history=history,
        include_lifting=include_lifting,
        goal_tracking=goal_tracking,
        phase=phase,
        plan_week=plan_week,
        prose_fallback=lambda: _call_llm(system, user, token, history=history),
    )
    from gym_program import apply_program_gym_to_plan, next_gym_week_index
    from squad_plan_fallback import build_library_squad_plan_json

    week_index = (
        next_gym_week_index(prev_plan_json)
        if gym_week_index is None
        else gym_week_index
    )
    plan_json = generated.plan_json
    if plan_json is None:
        print(
            "Weekly plan: structured JSON failed; using library/gym fallback.",
            flush=True,
        )
        plan_json = build_library_squad_plan_json(
            plan_week=plan_week,
            phase=phase or "base",
            include_lifting=include_lifting,
            peak_kg_by_exercise=peak_kg_by_exercise or {},
            prev_plan_json=prev_plan_json,
            session_selections=session_selections,
            week_index=week_index,
        )
    else:
        plan_json = apply_library_sessions_to_plan(plan_json, session_selections)
        if include_lifting:
            plan_json = apply_program_gym_to_plan(
                plan_json,
                phase=phase,
                week_index=week_index,
                peak_kg_by_exercise=peak_kg_by_exercise,
                prev_plan_json=prev_plan_json,
            )
    parsed = parse_weekly_plan(plan_json)
    if parsed is None:
        plan_json = build_library_squad_plan_json(
            plan_week=plan_week,
            phase=phase or "base",
            include_lifting=include_lifting,
            peak_kg_by_exercise=peak_kg_by_exercise or {},
            prev_plan_json=prev_plan_json,
            session_selections=session_selections,
            week_index=week_index,
        )
        parsed = parse_weekly_plan(plan_json)
    if parsed is None:
        raise ValueError("library/gym squad plan fallback failed validation")
    return GeneratedWeeklyPlan(
        plan_json=plan_json,
        plan_text=render_plan_text(parsed),
    )


def get_kagi_weekly_plan(
    training_summary: str,
    token: str,
    include_lifting: bool = True,
    plan_week: Optional[WeekBounds] = None,
    adherence_review: Optional[str] = None,
    goal_tracking: Optional[str] = None,
    gym_tonnage_summary: Optional[str] = None,
    gym_exercise_history: Optional[str] = None,
    previous_week_gym_exercises: Optional[str] = None,
    coach_adjustments: Optional[str] = None,
    zulip_topic_feedback: Optional[str] = None,
    season_week_context: Optional[str] = None,
) -> str:
    """Squad weekly plan prose (legacy wrapper; prefer generate_squad_weekly_plan)."""
    return generate_squad_weekly_plan(
        training_summary,
        token,
        include_lifting=include_lifting,
        plan_week=plan_week,
        adherence_review=adherence_review,
        goal_tracking=goal_tracking,
        gym_tonnage_summary=gym_tonnage_summary,
        gym_exercise_history=gym_exercise_history,
        previous_week_gym_exercises=previous_week_gym_exercises,
        coach_adjustments=coach_adjustments,
        zulip_topic_feedback=zulip_topic_feedback,
        season_week_context=season_week_context,
    ).plan_text


def generate_athlete_weekly_plan(
    athlete_label: str,
    athlete_training_summary: str,
    token: str,
    *,
    squad_plan_json: Optional[Dict[str, Any]] = None,
    squad_plan_text: str = "",
    include_lifting: bool = True,
    plan_week: Optional[WeekBounds] = None,
    gym_exercise_history: Optional[str] = None,
    recent_sessions_summary: Optional[str] = None,
    coach_adjustments: Optional[str] = None,
    athlete_hr_context: Optional[str] = None,
    athlete_profile: Optional[AthleteProfile] = None,
    season_week_context: Optional[str] = None,
    lift_logs_by_exercise: Optional[Mapping[str, Sequence[Any]]] = None,
) -> GeneratedWeeklyPlan:
    """Personalised weekly plan for one athlete (DM): tailored targets per session.

    Gym exercises are fixed by the squad plan; only per-set reps/weights are tailored.
    """
    plan_week = plan_week or plan_week_bounds()
    lifting_clause = _weekly_plan_lifting_clause(
        include_lifting, fixed_exercises=True
    )
    # Athlete plan must reuse the squad plan's exercises, so no exercise pool or
    # variety/rotation context here — those drive exercise SELECTION (squad concern).
    gym_options_block = ""
    extra_sections: List[str] = []
    if season_week_context and season_week_context.strip():
        extra_sections.append(season_week_context.strip())
    if recent_sessions_summary and recent_sessions_summary.strip():
        extra_sections.append(
            f"--- Recent logged sessions ---\n{recent_sessions_summary.strip()}"
        )
    if gym_exercise_history and include_lifting:
        extra_sections.append(
            f"--- Gym lift history (for target weights) ---\n{gym_exercise_history}"
        )
    if coach_adjustments and coach_adjustments.strip():
        extra_sections.append(
            "--- Athlete-requested plan adjustments (apply to this week's plan) ---\n"
            f"{coach_adjustments.strip()}"
        )
    extra_block = ("\n\n".join(extra_sections) + "\n\n") if extra_sections else ""
    hr_block = ""
    if athlete_hr_context and athlete_hr_context.strip():
        hr_block = (
            f"{athlete_hr_context.strip()}\n\n"
            "Use these athlete-specific Z1–Z5 and T1–T7 HR bpm ranges (from Max HR) "
            "for every erg/row session segment—not squad-average zones.\n\n"
        )

    system = (
        f"You are an expert rowing coach and strength coach writing a PRIVATE weekly "
        f"plan for {athlete_label}.\n\n"
        "Use the squad weekly plan as the session STRUCTURE (which days, session types, "
        "interval format) but replace every target with numbers tailored to THIS athlete "
        "from their erg summary and logged sessions.\n\n"
        "TARGET RULES (strict):\n"
        "- Every 500m split range, HR zone (Z and T codes plus bpm), distance/duration, "
        "and gym set weight must be "
        "specific to this athlete's recent performance—not squad averages.\n"
        "- Prescribe HR targets as explicit bpm ranges from the athlete's 5-zone "
        "(Z1–Z5) and 7-zone T-level (T1–T7) tables (both derived from Max HR), "
        "e.g. '@ Z2/T3, split 1:58–2:05, HR 130–148 bpm, priority: HR' on every "
        "segment line.\n"
        "- GYM EXERCISES ARE FIXED BY THE SQUAD PLAN: use the EXACT same gym exercises, "
        "on the same days (Mon/Wed), in the same order as the squad plan. Do NOT add, "
        "drop, swap, or substitute any gym exercise. Only the per-set reps and target "
        "weight (kg) may be tailored to this athlete.\n"
        "- Gym weights/reps must progress from this athlete's lift history; keep the "
        "same set count per exercise as the squad plan unless the athlete's history "
        "clearly warrants a different rep target.\n"
        "- Keep the same fixed morning schedule as the squad plan.\n\n"
        "OUTPUT RULES (strict):\n"
        "- Return EXECUTION ONLY: day-by-day sessions with gym and rowing detail.\n"
        "- Gym days: numbered 4-exercise format; exercises fixed from squad plan.\n"
        "- Rowing days: segmented with Z/T, splits, HR, priority; intervals include "
        "rest; use m/km/min units; each session ≤45 min.\n"
        "- When season master plan targets appear, keep this athlete's T5/Z5 "
        "(VO2max/race-pace) volume at or below the stated weekly intensity cap and "
        "honour the weekly execution priority (split vs HR) for Main Set segments.\n"
        "- No introductory essay. Start directly with the weekly schedule.\n"
        "- Do NOT append weekly volume summaries or intensity-mix percentages.\n"
        "- Address the athlete by first name once at the top (one short line), then "
        "day-by-day sessions.\n\n"
        f"{hr_block}"
        f"{_STRATEGIC_GOALS_CONTEXT}\n"
        "Use this fixed morning schedule (do not move sessions to other days):\n"
        "- Monday morning: gym\n"
        "- Tuesday morning: erg\n"
        "- Wednesday morning: gym\n"
        "- Thursday morning: rowing on water OR erg (match squad plan modality)\n"
        "- Friday and Saturday: rest by default; match any extra rowing session the "
        "squad plan adds when Tue/Thu exceed 45 min.\n"
        "- Sunday: rest or optional recovery only unless clearly justified.\n\n"
        f"{lifting_clause}\n\n"
        f"{gym_options_block}"
        f"{_session_segment_context(personalised=True)}\n"
        "For each erg or rowing session also state the session type. On-water "
        "distance intervals must include the erg time-interval alternative from the "
        "squad plan, segmented the same way with this athlete's splits, HR, and priority."
    )
    if squad_plan_json:
        squad_block = (
            "--- Squad plan JSON (structure + gym exercises; tailor targets only) ---\n"
            f"{json.dumps(squad_plan_json, indent=2)}"
        )
    else:
        squad_block = (
            "--- Squad plan (structure + session types; do not copy squad-average numbers) ---\n"
            f"{squad_plan_text.strip()}"
        )
    user = (
        f"Target week (Mon–Sun {plan_timezone_name()}): "
        f"{plan_week.week_start.isoformat()} to {plan_week.week_end.isoformat()}.\n\n"
        f"{squad_block}\n\n"
        f"{extra_block}"
        f"--- {athlete_label}: erg training summary ---\n{athlete_training_summary}"
    )
    generated = _generate_structured_plan_with_fallback(
        system,
        user,
        token,
        include_lifting=include_lifting,
        squad_plan_json=squad_plan_json,
        plan_week=plan_week,
        personalised=True,
        greeting=athlete_label.split()[0] + ",",
        athlete_profile=athlete_profile,
        prose_fallback=lambda: _call_llm(system, user, token),
    )
    greeting = athlete_label.split()[0] + ","
    proposal = generated.plan_json
    if proposal is None and squad_plan_json and generated.plan_text.strip():
        from weekly_plan_harness import import_prose_plan_json

        proposal = import_prose_plan_json(
            generated.plan_text,
            week_start=plan_week.week_start.isoformat(),
            personalised=True,
            greeting=greeting,
        )
    if squad_plan_json:
        from athlete_plan_lock import lock_athlete_plan_to_squad
        from weekly_plan_schema import (
            parse_weekly_plan,
            render_plan_text,
            validate_athlete_plan_against_squad,
        )

        locked = lock_athlete_plan_to_squad(
            squad_plan_json,
            proposal_json=proposal,
            athlete_profile=athlete_profile,
            lift_logs_by_exercise=lift_logs_by_exercise,
            greeting=greeting,
            include_lifting=include_lifting,
        )
        parsed = parse_weekly_plan(locked)
        squad_parsed = parse_weekly_plan(squad_plan_json)
        if parsed is not None:
            if squad_parsed is not None:
                err = validate_athlete_plan_against_squad(parsed, squad_parsed)
                if err:
                    print(f"Athlete plan lock validator: {err}", flush=True)
            return GeneratedWeeklyPlan(
                plan_json=locked,
                plan_text=render_plan_text(parsed, absolute_hr_bpm=False),
            )
    return generated


def get_kagi_athlete_weekly_plan(
    athlete_label: str,
    athlete_training_summary: str,
    squad_plan_text: str,
    token: str,
    *,
    squad_plan_json: Optional[Dict[str, Any]] = None,
    include_lifting: bool = True,
    plan_week: Optional[WeekBounds] = None,
    gym_exercise_history: Optional[str] = None,
    recent_sessions_summary: Optional[str] = None,
    coach_adjustments: Optional[str] = None,
    athlete_hr_context: Optional[str] = None,
    athlete_profile: Optional[AthleteProfile] = None,
    season_week_context: Optional[str] = None,
) -> str:
    """Personalised weekly plan prose (legacy wrapper; prefer generate_athlete_weekly_plan)."""
    return generate_athlete_weekly_plan(
        athlete_label,
        athlete_training_summary,
        token,
        squad_plan_json=squad_plan_json,
        squad_plan_text=squad_plan_text,
        include_lifting=include_lifting,
        plan_week=plan_week,
        gym_exercise_history=gym_exercise_history,
        recent_sessions_summary=recent_sessions_summary,
        coach_adjustments=coach_adjustments,
        athlete_hr_context=athlete_hr_context,
        athlete_profile=athlete_profile,
        season_week_context=season_week_context,
    ).plan_text


def filter_metrics_for_athlete(
    cache_dir: Path,
    athlete_id: int,
    metrics_by_id: Mapping[int, Dict[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    """Activity metrics belonging to one athlete's Strava index."""
    try:
        from erg_session_merge import load_athlete_index_activities
    except ImportError:
        return dict(metrics_by_id)
    act_ids = {int(a["id"]) for a in load_athlete_index_activities(cache_dir, athlete_id)}
    return {aid: rec for aid, rec in metrics_by_id.items() if aid in act_ids}


def get_kagi_adherence_review(
    previous_plan_text: str,
    week_activities_summary: str,
    token: str,
    plan_week: WeekBounds,
) -> str:
    """Compare logged activities to the cached plan for the prior week (two sentences)."""
    system = (
        "You are an expert rowing coach reviewing squad training adherence.\n\n"
        "Compare the prescribed plan to what was actually logged.\n\n"
        "OUTPUT RULES (strict):\n"
        "- Write EXACTLY TWO sentences total.\n"
        "- No bullets, lists, headings, or session-by-session breakdown.\n"
        "- First sentence: overall compliance (what was completed vs missed/substituted).\n"
        "- Second sentence: the single most important adjustment for the coming week."
    )
    user = (
        f"Review week (Mon–Sun {plan_timezone_name()}): "
        f"{plan_week.week_start.isoformat()} to {plan_week.week_end.isoformat()}.\n\n"
        f"--- Prescribed plan ---\n{previous_plan_text}\n\n"
        f"--- Logged activities ---\n{week_activities_summary}"
    )
    result = _call_llm(system, user, token)
    if is_openrouter_error(result):
        print(f"Adherence review LLM failed: {result}", flush=True)
        return "Adherence review unavailable (LLM error)."
    return result


@dataclass(frozen=True)
class PipelineAthleteCfg:
    """Athlete row from config.yaml for weekly DM delivery."""

    id: int
    label: str
    zulip_email: Optional[str] = None
    zulip_user_id: Optional[int] = None
    body_weight_kg: Optional[float] = None
    max_hr_bpm: Optional[int] = None
    hr_z2_pct: Tuple[float, float] = (0.60, 0.75)
    hr_z5_pct: Tuple[float, float] = (0.90, 1.00)
    five_zone_pct: Optional[Mapping[str, Tuple[float, float]]] = None
    seven_zone_pct: Optional[Mapping[str, Tuple[float, float]]] = None
    training_zone_pct: Optional[Mapping[str, Tuple[float, float]]] = None

    def _athlete_profile(self) -> Any:
        from athlete_profile import (
            DEFAULT_FIVE_ZONE_PCT,
            DEFAULT_SEVEN_ZONE_PCT,
            AthleteProfile,
        )

        seven = (
            dict(self.seven_zone_pct)
            if self.seven_zone_pct
            else (
                dict(self.training_zone_pct)
                if self.training_zone_pct
                else dict(DEFAULT_SEVEN_ZONE_PCT)
            )
        )
        return AthleteProfile(
            id=self.id,
            label=self.label,
            body_weight_kg=self.body_weight_kg,
            max_hr_bpm=self.max_hr_bpm,
            zulip_email=self.zulip_email,
            zulip_user_id=self.zulip_user_id,
            hr_z2_pct=self.hr_z2_pct,
            hr_z5_pct=self.hr_z5_pct,
            five_zone_pct=(
                dict(self.five_zone_pct)
                if self.five_zone_pct
                else dict(DEFAULT_FIVE_ZONE_PCT)
            ),
            seven_zone_pct=seven,
        )

    def hr_zone_context_text(self) -> str:
        return self._athlete_profile().hr_zone_context_text()


def load_pipeline_athletes(config_path: Optional[Path]) -> List["PipelineAthleteCfg"]:
    """Load athlete id/label/Zulip mapping from erg_strava config.yaml."""
    if config_path is None or not config_path.is_file():
        return []
    try:
        import yaml
    except ImportError:
        return []
    raw = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(raw, dict):
        return []
    try:
        from athlete_profile import load_athlete_profiles

        return [
            PipelineAthleteCfg(
                id=p.id,
                label=p.label,
                zulip_email=p.zulip_email,
                zulip_user_id=p.zulip_user_id,
                body_weight_kg=p.body_weight_kg,
                max_hr_bpm=p.max_hr_bpm,
                hr_z2_pct=p.hr_z2_pct,
                hr_z5_pct=p.hr_z5_pct,
                five_zone_pct=dict(p.five_zone_pct),
                seven_zone_pct=dict(p.seven_zone_pct),
                training_zone_pct=dict(p.seven_zone_pct),
            )
            for p in load_athlete_profiles(raw)
        ]
    except ImportError:
        pass
    out: List[PipelineAthleteCfg] = []
    for entry in raw.get("athletes") or []:
        if not isinstance(entry, dict) or "id" not in entry:
            continue
        out.append(
            PipelineAthleteCfg(
                id=int(entry["id"]),
                label=str(entry.get("label", f"athlete_{entry['id']}")),
                zulip_email=(
                    str(entry["zulip_email"]).strip().lower()
                    if entry.get("zulip_email")
                    else None
                ),
                zulip_user_id=(
                    int(entry["zulip_user_id"])
                    if entry.get("zulip_user_id") is not None
                    else None
                ),
            )
        )
    return out


def get_kagi_athlete_weekly_compliance_dm(
    athlete_label: str,
    previous_plan_text: str,
    athlete_activities_summary: str,
    token: str,
    plan_week: WeekBounds,
) -> str:
    """Private weekly check-in: two-sentence compliance plus coaching interpretation."""
    system = (
        f"You are an expert rowing coach writing a private weekly check-in to "
        f"{athlete_label}.\n\n"
        "Compare the prescribed plan to this athlete's logged sessions only.\n\n"
        "OUTPUT FORMAT (strict):\n"
        "1. Exactly TWO sentences on plan compliance for this athlete "
        "(done / partial / missed / substituted—be specific but brief).\n"
        "2. Then TWO or THREE sentences of coaching interpretation: what went well, "
        "what to fix, and one focus for the coming week.\n"
        "- Address the athlete by first name.\n"
        "- No bullets, headings, or JSON.\n"
        "- Under 120 words total."
    )
    user = (
        f"Review week (Mon–Sun {plan_timezone_name()}): "
        f"{plan_week.week_start.isoformat()} to {plan_week.week_end.isoformat()}.\n\n"
        f"--- Prescribed plan ---\n{previous_plan_text}\n\n"
        f"--- {athlete_label}: logged sessions ---\n{athlete_activities_summary}"
    )
    return _call_llm(system, user, token).strip()


def compose_weekly_athlete_plan_dm(
    volume_block: str,
    target_week: WeekBounds,
    plan_body: str,
) -> str:
    """Assemble weekly athlete DM: optional last-week volume, then next-week plan."""
    header = (
        f"**Your weekly plan** ({target_week.week_start} – {target_week.week_end})\n"
        "_Personalised session targets — squad averages are in the public topic._\n\n"
    )
    plan_section = header + plan_body.strip()
    if volume_block.strip():
        return volume_block.strip() + "\n\n" + plan_section
    return plan_section


def send_weekly_athlete_plan_dms(
    cache_dir: Path,
    athletes: Sequence[PipelineAthleteCfg],
    squad_plan_text: str,
    target_week: WeekBounds,
    review_week: WeekBounds,
    activity_details: Dict[int, dict],
    activity_metrics: Optional[Dict[int, Dict[str, Any]]],
    token: str,
    *,
    squad_plan_json: Optional[Dict[str, Any]] = None,
    erg_df: Optional[pd.DataFrame] = None,
    include_lifting: bool = True,
    coach_adjustments: Optional[str] = None,
    erg_types: Optional[frozenset] = None,
    require_trainer_for_rowing: bool = True,
    zuliprc_path: Optional[Path] = None,
    season_week_context: Optional[str] = None,
    season_cfg_obj: Any = None,
) -> int:
    """DM each athlete with logged sessions their personalised weekly plan."""
    if not squad_plan_text.strip() or not athletes:
        return 0
    try:
        from erg_session_merge import (
            DEFAULT_ERG_SPORT_TYPES,
            athlete_has_week_training_log,
            format_athlete_week_training_log,
        )
        from erg_prescription_compare import format_last_week_volume_for_dm
        from send_to_zulip import send_private_message_to_zulip
    except ImportError as exc:
        print(f"Weekly athlete plan DMs skipped: {exc}", flush=True)
        return 0

    erg_types = erg_types or DEFAULT_ERG_SPORT_TYPES
    metrics = activity_metrics or {}
    sent = 0
    for athlete in athletes:
        recipient: Optional[Union[int, str]] = None
        if athlete.zulip_user_id is not None:
            recipient = athlete.zulip_user_id
        elif athlete.zulip_email:
            recipient = athlete.zulip_email
        if recipient is None:
            print(
                f"Weekly plan DM skipped for {athlete.label}: "
                "no zulip_user_id or zulip_email",
                flush=True,
            )
            continue
        if not athlete_has_week_training_log(
            cache_dir,
            athlete.id,
            athlete.label,
            review_week,
            erg_types=erg_types,
            require_trainer_for_rowing=require_trainer_for_rowing,
        ):
            print(
                f"Weekly plan DM skipped for {athlete.label}: no logged sessions in "
                f"{review_week.week_start}–{review_week.week_end}",
                flush=True,
            )
            continue
        athlete_summary = (
            build_athlete_training_summary(erg_df, athlete.label)
            if erg_df is not None and not erg_df.empty
            else f"{athlete.label}\n  (no erg stream data cached for this athlete)"
        )
        recent_sessions = format_athlete_week_training_log(
            cache_dir,
            athlete.id,
            athlete.label,
            review_week,
            activity_details,
            metrics,
            erg_types=erg_types,
            require_trainer_for_rowing=require_trainer_for_rowing,
        )
        athlete_metrics = filter_metrics_for_athlete(cache_dir, athlete.id, metrics)
        gym_history = (
            format_exercise_history_for_plan(athlete_metrics)
            if include_lifting
            else None
        )
        athlete_lift_logs = None
        if include_lifting:
            from gym_program import lift_logs_from_metrics

            athlete_lift_logs = lift_logs_from_metrics(athlete_metrics)
        try:
            generated = generate_athlete_weekly_plan(
                athlete.label,
                athlete_summary,
                token,
                squad_plan_json=squad_plan_json,
                squad_plan_text=squad_plan_text,
                include_lifting=include_lifting,
                plan_week=target_week,
                gym_exercise_history=gym_history,
                recent_sessions_summary=recent_sessions,
                coach_adjustments=coach_adjustments,
                athlete_hr_context=athlete.hr_zone_context_text(),
                athlete_profile=athlete._athlete_profile(),
                season_week_context=season_week_context,
                lift_logs_by_exercise=athlete_lift_logs,
            )
            athlete_plan_json = generated.plan_json
            prev_athlete_record = load_athlete_weekly_plan(
                cache_dir, athlete.id, review_week.week_id
            )
            prev_athlete_json = (
                prev_athlete_record.get("plan_json")
                if isinstance(prev_athlete_record, dict)
                and isinstance(prev_athlete_record.get("plan_json"), dict)
                else None
            )
            athlete_plan_json, align_log = apply_season_master_plan_alignment(
                cache_dir,
                season_cfg_obj,
                target_week,
                athlete_plan_json,
                plan_text=generated.plan_text,
                personalised=True,
                greeting=(
                    (generated.plan_json or {}).get("greeting")
                    if isinstance(generated.plan_json, dict)
                    else None
                ),
                previous_week_plan=prev_athlete_json,
                reference_plan=squad_plan_json,
                plan_label=f"Athlete plan ({athlete.label})",
            )
            if align_log.strip():
                print(align_log, flush=True)
            if athlete_plan_json is None and squad_plan_json:
                from athlete_plan_lock import lock_athlete_plan_to_squad

                try:
                    athlete_plan_json = lock_athlete_plan_to_squad(
                        squad_plan_json,
                        proposal_json=generated.plan_json,
                        athlete_profile=athlete._athlete_profile(),
                        lift_logs_by_exercise=athlete_lift_logs,
                        greeting=athlete.label.split()[0] + ",",
                        include_lifting=include_lifting,
                    )
                except ValueError as exc:
                    print(
                        f"Weekly plan DM skipped for {athlete.label}: {exc}",
                        flush=True,
                    )
                    continue
            if athlete_plan_json is not None:
                plan_body = finalize_plan_text_for_display(
                    generated.plan_text, athlete_plan_json
                )
                from weekly_plan_schema import parse_weekly_plan, render_plan_text

                parsed_athlete = parse_weekly_plan(athlete_plan_json)
                if parsed_athlete is not None:
                    profile = athlete._athlete_profile()
                    use_bpm = (
                        profile is not None and profile.max_hr_bpm is not None
                    )
                    plan_body = finalize_plan_text_for_display(
                        render_plan_text(
                            parsed_athlete, absolute_hr_bpm=use_bpm
                        ),
                        athlete_plan_json,
                    )
            else:
                print(
                    f"Weekly plan DM skipped for {athlete.label}: "
                    "no structured plan JSON",
                    flush=True,
                )
                continue
            volume_block = format_last_week_volume_for_dm(
                cache_dir, athlete.id, review_week
            )
            dm_body = compose_weekly_athlete_plan_dm(
                volume_block, target_week, plan_body
            )
            send_private_message_to_zulip(
                dm_body,
                [recipient],
                zuliprc_path=zuliprc_path,
            )
            save_athlete_weekly_plan(
                cache_dir,
                athlete.id,
                target_week,
                plan_body.strip(),
                squad_week_id=target_week.week_id,
                plan_json=athlete_plan_json,
            )
            sent += 1
            print(f"Weekly plan DM sent to {athlete.label}.", flush=True)
        except Exception as exc:
            print(f"Weekly plan DM failed for {athlete.label}: {exc}", flush=True)
    return sent


def get_kagi_gym_tonnage(
    gym_sessions: Sequence[Tuple[int, str, str]],
    token: str,
    week_label: str,
    metrics_by_id: Optional[Mapping[int, Dict[str, Any]]] = None,
) -> str:
    """
    Gym tonnage summary from cached structured metrics, or parse via Kagi if missing.
    """
    if metrics_by_id:
        summary = format_gym_metrics_summary(metrics_by_id, week_label)
        if "No gym sessions" not in summary:
            return summary

    if not gym_sessions:
        return "No gym sessions with workout descriptions in the review window."

    parsed_records: Dict[int, Dict[str, Any]] = {}
    for aid, name, desc in gym_sessions:
        metrics = parse_gym_session_metrics(aid, name, desc, token)
        if metrics:
            parsed_records[aid] = {
                "activity_id": aid,
                "activity_name": name,
                "gym": metrics.to_dict(),
            }
    if parsed_records:
        return format_gym_metrics_summary(parsed_records, week_label)

    return "Gym sessions present but tonnage parsing failed for all descriptions."


_LOW_INTENSITY_ROWING_BODY = """- Tuesday erg: steady-state or short aerobic intervals — all main work Z2/T3 (split ~2:05–2:15), priority: HR. No Z4/Z5, threshold, or race-pace work (recovery/deload week).
- Thursday on-water or erg: long Z2 steady-state (45–60 min continuous or tempo blocks), Z2/T3 only, priority: HR. Include erg_alternative for group fallback. No threshold or race-pace pieces."""

_BASE_ROWING_BODY = """- Tuesday erg: threshold intervals or tempo — main work T3–T4 (Z3/Z4), split ~1:58–2:08, priority: HR. No full Z5/VO2max sets (weekly Z5 cap ~8% is for brief touches only).
- Thursday on-water or erg: long aerobic T2–T3 steady-state (45–60 min main work), split ~2:00–2:10, priority: HR. Include erg_alternative. Complements Tuesday threshold — not a second threshold day."""

_BUILD_ROWING_BODY = """- Tuesday erg: threshold or VO2max intervals — include T4–T5 work within the weekly Z5 cap; main set structure with reps×distance, priority HR unless race-pace primer noted.
- Thursday on-water or erg: aerobic–threshold blend or sustained T3 pieces; differ from Tuesday stimulus. Include erg_alternative with parallel structure."""

_PEAK_ROWING_BODY = """- Tuesday erg: race-pace or VO2max primers within the weekly Z5 cap; short, high-quality main sets, priority split where noted.
- Thursday on-water or erg: aerobic maintenance with optional short race-pace touches; differ from Tuesday. Include erg_alternative."""

_RACE_ROWING_BODY = _PEAK_ROWING_BODY


def rowing_prescriptions_for_phase(phase: Optional[str]) -> str:
    """Heading plus bullet prescriptions for the current season phase."""
    from weekly_plan_schema import is_low_intensity_plan_phase

    phase_norm = (phase or "unspecified").strip().lower()
    if is_low_intensity_plan_phase(phase_norm):
        body = _LOW_INTENSITY_ROWING_BODY
    elif phase_norm == "base":
        body = _BASE_ROWING_BODY
    elif phase_norm == "build":
        body = _BUILD_ROWING_BODY
    elif phase_norm in ("peak", "race"):
        body = _PEAK_ROWING_BODY if phase_norm == "peak" else _RACE_ROWING_BODY
    else:
        body = (
            "- Tuesday erg: session_subtype, main set structure, target 500m split range, "
            "and primary Z/T zone for main work.\n"
            "- Thursday on-water or erg: same fields; differ in primary stimulus from Tuesday."
        )
    return f"### This week's rowing prescriptions ({phase_norm} week)\n{body}"


# Backward-compatible aliases used in LLM prompts.
_LOW_INTENSITY_ROWING_PRESCRIPTIONS = rowing_prescriptions_for_phase("deload")
_BASE_ROWING_PRESCRIPTIONS = rowing_prescriptions_for_phase("base")
_BUILD_ROWING_PRESCRIPTIONS = rowing_prescriptions_for_phase("build")


def adapt_goal_tracking_for_phase(
    goal_tracking: str,
    phase: Optional[str],
) -> str:
    """Replace rowing prescriptions so they match season phase (not generic race-pace)."""
    phase_norm = (phase or "").strip().lower()
    replacement = rowing_prescriptions_for_phase(phase_norm)
    body = goal_tracking.strip()
    import re

    patched = re.sub(
        r"(?ms)^### This week's rowing prescriptions(?: \([^)]+\))?.*?(?=^### |\Z)",
        replacement + "\n\n",
        body,
    )
    if patched == body:
        patched = f"{body}\n\n{replacement}"
    return patched.strip()


def plan_phase_for_week(
    cache_dir: Path,
    season_cfg_obj: Any,
    week: WeekBounds,
) -> Optional[str]:
    """Return season master plan phase for one calendar week."""
    if season_cfg_obj is None:
        return None
    try:
        from weekly_plan_master_align import load_weekly_targets

        targets = load_weekly_targets(cache_dir, season_cfg_obj)
        target = targets.get(week.week_start.isoformat())
        return target.phase if target else None
    except Exception:
        return None


def get_kagi_goal_tracking(
    training_summary: str,
    token: str,
    adherence_review: Optional[str] = None,
    gym_tonnage_summary: Optional[str] = None,
    *,
    phase: Optional[str] = None,
) -> str:
    """Summarise progress toward Head of the Yarra and Vic 2k 6:40 goals."""
    from weekly_plan_schema import is_low_intensity_plan_phase

    sections = [f"--- Erg training summary ---\n{training_summary}"]
    if adherence_review:
        sections.append(f"--- Last week adherence ---\n{adherence_review}")
    if gym_tonnage_summary:
        sections.append(f"--- Gym tonnage ---\n{gym_tonnage_summary}")
    context = "\n\n".join(sections)

    phase_norm = (phase or "").strip().lower()

    if is_low_intensity_plan_phase(phase):
        system = (
            "You are an expert rowing coach. Based on the erg summary, adherence, and "
            "gym data in the user message, write a concise progress snapshot toward two "
            "season goals:\n"
            "1. Head of the Yarra (November): 8.5 km eights — aerobic capacity and "
            "sustained race pace.\n"
            "2. Victoria State Championships: club 4x− 2k — target 6:40.\n\n"
            f"This is a {phase_norm} week — do NOT prescribe high-intensity "
            "rowing or race-pace work in Next Steps.\n\n"
            "STRUCTURE (strict):\n"
            "For EACH season goal, use: Current Fitness Indicators, Gaps, and "
            "Next Steps (1-2 weeks). Next Steps must focus on recovery, aerobic "
            "maintenance, and technique — not VO2max or race-pace intervals.\n\n"
            "End with this EXACT section (copy verbatim):\n"
            f"{rowing_prescriptions_for_phase(phase_norm)}\n\n"
            "Reference specific splits/HR from the summary where possible. "
            "No generic motivation."
        )
    elif phase_norm == "base":
        system = (
            "You are an expert rowing coach. Based on the erg summary, adherence, and "
            "gym data in the user message, write a concise progress snapshot toward two "
            "season goals:\n"
            "1. Head of the Yarra (November): 8.5 km eights.\n"
            "2. Victoria State Championships: club 4x− 2k — target 6:40.\n\n"
            "This is a BASE week — Next Steps should emphasise aerobic volume and "
            "T4 threshold (Z4) development, NOT full Z5/VO2max interval blocks.\n\n"
            "STRUCTURE (strict):\n"
            "For EACH season goal: Current Fitness Indicators, Gaps, Next Steps (1-2 weeks).\n\n"
            "End with this EXACT section (copy verbatim):\n"
            f"{rowing_prescriptions_for_phase('base')}\n\n"
            "Reference specific splits/HR from the summary where possible."
        )
    elif phase_norm == "build":
        system = (
            "You are an expert rowing coach. Based on the erg summary, adherence, and "
            "gym data in the user message, write a concise progress snapshot toward two "
            "season goals:\n"
            "1. Head of the Yarra (November): 8.5 km eights.\n"
            "2. Victoria State Championships: club 4x− 2k — target 6:40.\n\n"
            "This is a BUILD week — include threshold and capped VO2max work in Next Steps "
            "consistent with the weekly Z5 cap.\n\n"
            "STRUCTURE (strict):\n"
            "For EACH season goal: Current Fitness Indicators, Gaps, Next Steps (1-2 weeks).\n\n"
            "End with this EXACT section (copy verbatim):\n"
            f"{rowing_prescriptions_for_phase('build')}\n\n"
            "Reference specific splits/HR from the summary where possible."
        )
    else:
        system = (
            "You are an expert rowing coach. Based on the erg summary, adherence, and "
            "gym data in the user message, write a concise progress snapshot toward two season goals:\n"
            "1. Head of the Yarra (November): 8.5 km eights — aerobic capacity and "
            "sustained race pace.\n"
            "2. Victoria State Championships: club 4x− 2k — target 6:40.\n\n"
            "STRUCTURE (strict):\n"
            "For EACH season goal, use a subsection with: Current Fitness Indicators, "
            "Gaps, and Next Steps (1-2 weeks).\n\n"
            "End with a mandatory section:\n"
            f"{rowing_prescriptions_for_phase(phase_norm)}\n\n"
            "Fill in concrete session_subtype, structure, split ranges, and zones "
            "consistent with the gaps/next steps above.\n\n"
            "Reference specific splits/HR/intensity from the summary where possible. "
            "No generic motivation."
        )
    result = _call_llm(system, context, token)
    if is_openrouter_error(result):
        print(f"Goal tracking LLM failed: {result}", flush=True)
        return "Season goal tracking unavailable (LLM error)."
    return adapt_goal_tracking_for_phase(result, phase)


def format_public_weekly_plan_post(record: WeeklyPlanRecord) -> str:
    """Zulip public post: adherence plus the squad-average weekly plan."""
    parts: List[str] = []
    if record.adherence_review and "skipping adherence" not in record.adherence_review:
        parts.append(
            "=== Previous week adherence ===\n\n" + record.adherence_review.strip()
        )
    plan_body = finalize_plan_text_for_display(record.plan_text, record.plan_json)
    parts.append(
        f"=== Squad weekly plan ({record.week_start} – {record.week_end}) ===\n"
        "_Squad-average session targets — athletes with logged sessions receive "
        "personalised targets by DM._\n\n"
        + plan_body
    )
    return "\n\n".join(parts)


def run_weekly_training_pipeline(
    training_summary: str,
    token: str,
    cache_dir: Path,
    week_activities: Sequence[dict],
    activity_details: Dict[int, dict],
    gym_types: frozenset,
    gym_name_patterns: Sequence[str],
    include_lifting: bool = True,
    now: Optional[datetime] = None,
    activity_metrics: Optional[Dict[int, Dict[str, Any]]] = None,
    erg_df: Optional[pd.DataFrame] = None,
    zulip_stream: str = DEFAULT_ZULIP_STREAM,
    zulip_topic: str = DEFAULT_ZULIP_TOPIC,
    zuliprc_path: Optional[Path] = None,
    erg_types: Optional[frozenset] = None,
    require_trainer_for_rowing: bool = True,
    config_path: Optional[Path] = None,
    send_athlete_plan_dms: bool = True,
    season_config: Optional[Any] = None,
    refresh_season_plan: bool = False,
) -> Tuple[str, WeeklyPlanRecord]:
    """
    Full weekly flow: adherence (if prior plan), gym tonnage, goal tracking, new plan, cache.
    Returns (full_report_text, saved_record).
    """
    now = now or datetime.now(timezone.utc)
    target_week = plan_week_bounds(now)
    prev_week = previous_week_bounds(target_week)

    prev_record = load_weekly_plan(cache_dir, prev_week.week_id)
    adherence_review: Optional[str] = None
    if prev_record and prev_record.plan_text.strip():
        prev_acts = [
            a
            for a in week_activities
            if _parse_activity_start(a.get("start_date"))
            and week_contains(prev_week, _parse_activity_start(a.get("start_date")))  # type: ignore[arg-type]
        ]
        try:
            from erg_session_merge import (
                DEFAULT_ERG_SPORT_TYPES,
                format_week_training_log,
            )

            acts_summary = format_week_training_log(
                prev_acts,
                activity_details,
                activity_metrics or {},
                cache_dir,
                prev_week,
                erg_types=erg_types or DEFAULT_ERG_SPORT_TYPES,
                require_trainer_for_rowing=require_trainer_for_rowing,
            )
            manual_erg = format_manual_erg_scores_summary(cache_dir, prev_week)
            if manual_erg.strip():
                acts_summary += (
                    "\n\n--- Manual erg scores (Zulip screenshots) ---\n"
                    + manual_erg.strip()
                )
            manual_gym = format_zulip_gym_logs_summary(cache_dir, prev_week)
            if manual_gym.strip():
                acts_summary += "\n\n" + manual_gym.strip()
        except Exception:
            acts_summary = format_week_activities_summary(
                prev_acts, activity_details, metrics_by_id=activity_metrics or {}
            )
            manual_erg = format_manual_erg_scores_summary(cache_dir, prev_week)
            if manual_erg.strip():
                acts_summary += (
                    "\n\n--- Manual erg scores (Zulip screenshots) ---\n"
                    + manual_erg.strip()
                )
            manual_gym = format_zulip_gym_logs_summary(cache_dir, prev_week)
            if manual_gym.strip():
                acts_summary += "\n\n" + manual_gym.strip()
        adherence_review = get_kagi_adherence_review(
            prev_record.plan_text,
            acts_summary,
            token,
            prev_week,
        )
        try:
            from squad_adherence_stats import (
                compose_adherence_review,
                compute_squad_week_adherence_stats,
                format_squad_adherence_stats,
            )

            prev_week_metrics = dict(activity_metrics or {})
            prev_week_metrics.update(
                merge_zulip_gym_logs_into_metrics(
                    prev_week_metrics,
                    cache_dir,
                    week=prev_week,
                )
            )
            squad_stats = compute_squad_week_adherence_stats(
                prev_week,
                week_activities,
                prev_week_metrics,
                erg_df,
                cache_dir=cache_dir,
                config_path=config_path,
                gym_types=gym_types,
                gym_name_patterns=gym_name_patterns,
            )
            stats_block = format_squad_adherence_stats(squad_stats, prev_week)
            adherence_review = compose_adherence_review(stats_block, adherence_review)
        except Exception as exc:
            print(f"Squad adherence stats skipped: {exc}", flush=True)
    else:
        adherence_review = (
            f"No cached plan for previous week ({prev_week.week_id}); "
            "skipping adherence review."
        )

    tonnage_window_start = prev_week.week_start
    tonnage_acts = [
        a
        for a in week_activities
        if _parse_activity_start(a.get("start_date"))
        and tonnage_window_start
        <= activity_local_date(_parse_activity_start(a.get("start_date")))  # type: ignore[arg-type]
        <= target_week.week_end
    ]
    gym_sessions = collect_gym_descriptions(
        tonnage_acts, activity_details, gym_types, gym_name_patterns
    )
    week_label = (
        f"{tonnage_window_start.isoformat()} to {target_week.week_end.isoformat()}"
    )
    metrics_subset: Dict[int, Dict[str, Any]] = {}
    if activity_metrics:
        for act in tonnage_acts:
            aid = int(act["id"])
            if aid in activity_metrics:
                metrics_subset[aid] = activity_metrics[aid]
    metrics_with_zulip = merge_zulip_gym_logs_into_metrics(
        metrics_subset,
        cache_dir,
        start_date=tonnage_window_start,
        end_date=target_week.week_end,
    )
    gym_tonnage_summary = get_kagi_gym_tonnage(
        gym_sessions,
        token,
        week_label,
        metrics_by_id=metrics_with_zulip or None,
    )
    gym_exercise_history = format_exercise_history_for_plan(
        metrics_with_zulip or activity_metrics or {}
    )
    peak_kg_by_exercise = None
    gym_lift_review = None
    if include_lifting:
        from gym_program import median_latest_peak_kg

        peak_kg_by_exercise = median_latest_peak_kg(
            metrics_with_zulip or activity_metrics or {}
        )
    previous_week_gym_exercises: Optional[str] = None
    if include_lifting and prev_record and (
        prev_record.plan_json or prev_record.plan_text.strip()
    ):
        previous_week_gym_exercises = format_previous_week_gym_exercises(
            prev_record.plan_text,
            prev_record.plan_json,
        )

    season_week_context: Optional[str] = None
    season_cfg_obj: Any = None
    if season_config is not None:
        try:
            from season_master_plan import (
                ensure_macro_season_plan,
                load_season_config,
                load_season_week_macro_context,
            )

            season_cfg_obj = (
                season_config
                if hasattr(season_config, "races")
                else load_season_config(season_config)
            )
            if refresh_season_plan:
                ensure_macro_season_plan(
                    cache_dir,
                    season_cfg_obj,
                    training_summary,
                    token,
                    refresh=True,
                    include_lifting=include_lifting,
                )
                print(
                    "Season master plan macro regenerated from LLM "
                    f"({cache_dir / 'season_master_plan.md'}).",
                    flush=True,
                )
            season_week_context = load_season_week_macro_context(
                cache_dir, target_week, season_cfg_obj
            )
            if season_week_context:
                print(
                    f"Using season master plan macro targets for week "
                    f"{target_week.week_start.isoformat()}.",
                    flush=True,
                )
        except Exception as exc:
            print(f"Season master plan macro load skipped: {exc}", flush=True)

    phase = plan_phase_for_week(cache_dir, season_cfg_obj, target_week)
    if phase:
        print(f"Season phase for {target_week.week_start.isoformat()}: {phase}.", flush=True)

    if include_lifting:
        from gym_program import (
            format_lift_review,
            lift_logs_from_metrics,
            load_program,
            review_lifts,
        )

        try:
            gym_lift_review = format_lift_review(
                review_lifts(
                    lift_logs_from_metrics(
                        metrics_with_zulip or activity_metrics or {}
                    ),
                    load_program(phase),
                )
            )
        except Exception:
            gym_lift_review = None

    goal_tracking = get_kagi_goal_tracking(
        training_summary,
        token,
        adherence_review=adherence_review,
        gym_tonnage_summary=gym_tonnage_summary,
        phase=phase,
    )

    pending_adjustments = pending_plan_adjustments(cache_dir)
    coach_adjustments = (
        "\n\n".join(pending_adjustments) if pending_adjustments else None
    )

    since_zulip = _zulip_feedback_since_timestamp(
        week_activities, activity_details, prev_record, now
    )
    zulip_topic_feedback = fetch_zulip_topic_feedback_since_last_session(
        since_zulip,
        stream=zulip_stream,
        topic=zulip_topic,
        zuliprc_path=zuliprc_path,
    )
    if zulip_topic_feedback:
        print(
            f"Included Zulip topic feedback since {since_zulip.isoformat()} "
            f"({zulip_stream}/{zulip_topic}).",
            flush=True,
        )

    from session_library import recent_session_ids_from_plan
    from gym_program import next_gym_week_index_from_plans

    gym_week_index = next_gym_week_index_from_plans(
        list_weekly_plan_records(cache_dir)
    )
    try:
        generated_plan = generate_squad_weekly_plan(
            training_summary,
            token,
            include_lifting=include_lifting,
            plan_week=target_week,
            adherence_review=adherence_review,
            goal_tracking=goal_tracking,
            gym_tonnage_summary=gym_tonnage_summary,
            gym_exercise_history=gym_exercise_history,
            previous_week_gym_exercises=previous_week_gym_exercises,
            coach_adjustments=coach_adjustments,
            zulip_topic_feedback=zulip_topic_feedback,
            season_week_context=season_week_context,
            phase=phase,
            recent_session_ids=recent_session_ids_from_plan(
                prev_record.plan_json if prev_record else None
            ),
            peak_kg_by_exercise=peak_kg_by_exercise,
            prev_plan_json=prev_record.plan_json if prev_record else None,
            gym_lift_review=gym_lift_review,
            gym_week_index=gym_week_index,
        )
    except Exception as exc:
        print(
            f"CRITICAL: squad weekly plan generation failed: {exc}. "
            "Trying deterministic library/gym fallback.",
            flush=True,
        )
        generated_plan = GeneratedWeeklyPlan(plan_text="", plan_json=None)
    plan_json = generated_plan.plan_json
    prev_json = prev_record.plan_json if prev_record else None
    pre_alignment_plan_json = plan_json
    aligned_plan_json, align_log = apply_season_master_plan_alignment(
        cache_dir,
        season_cfg_obj,
        target_week,
        plan_json,
        plan_text=generated_plan.plan_text,
        previous_week_plan=prev_json,
        plan_label="Squad weekly plan",
    )
    if align_log.strip():
        print(align_log, flush=True)
    if parse_weekly_plan(aligned_plan_json) is not None:
        plan_json = aligned_plan_json
    elif parse_weekly_plan(pre_alignment_plan_json) is not None:
        plan_json = pre_alignment_plan_json
    else:
        plan_json = None
    if plan_json is None:
        from squad_plan_fallback import build_library_squad_plan_json

        try:
            plan_json = build_library_squad_plan_json(
                plan_week=target_week,
                phase=phase or "base",
                include_lifting=include_lifting,
                peak_kg_by_exercise=peak_kg_by_exercise or {},
                prev_plan_json=prev_json,
                week_index=gym_week_index,
            )
        except Exception as exc:
            print(
                f"CRITICAL: deterministic squad plan fallback failed: {exc}",
                flush=True,
            )
            plan_json = None
    parsed_squad = parse_weekly_plan(plan_json)
    if parsed_squad is None:
        print(
            "CRITICAL: no usable structured squad plan JSON; continuing the "
            "weekly cron without saving or posting a plan.",
            flush=True,
        )
        plan_json = None
        plan_text = generated_plan.plan_text.strip()
    else:
        plan_text = finalize_plan_text_for_display(
            render_plan_text(parsed_squad), plan_json
        )

    record = WeeklyPlanRecord(
        week_id=target_week.week_id,
        week_start=target_week.week_start.isoformat(),
        week_end=target_week.week_end.isoformat(),
        plan_text=plan_text,
        plan_json=plan_json,
        generated_at=now.astimezone(timezone.utc).isoformat(),
        training_summary=training_summary,
        include_lifting=include_lifting,
        adherence_review=adherence_review,
        goal_tracking=goal_tracking,
        gym_tonnage_summary=gym_tonnage_summary,
    )
    if record.plan_json is not None:
        save_weekly_plan(cache_dir, record)
        if pending_adjustments:
            consume_plan_adjustments(cache_dir)

    intensity_cap_report: Optional[str] = None
    if season_config is not None and season_cfg_obj is not None:
        try:
            from season_master_plan import update_season_master_plan_hybrid

            profiles_by_id: Dict[int, Any] = {}
            activity_athlete_ids: Dict[int, int] = {}
            if config_path and config_path.is_file():
                try:
                    import yaml
                    from athlete_profile import athlete_profiles_by_id, load_athlete_profiles

                    raw_cfg = yaml.safe_load(config_path.read_text()) or {}
                    profiles_by_id = athlete_profiles_by_id(
                        load_athlete_profiles(raw_cfg)
                    )
                    for profile in profiles_by_id.values():
                        idx_path = cache_dir / f"athlete_{profile.id}" / "index.json"
                        if not idx_path.is_file():
                            continue
                        idx_data = json.loads(idx_path.read_text())
                        for act in idx_data.get("activities") or []:
                            activity_athlete_ids[int(act["id"])] = profile.id
                except Exception:
                    pass
            md_path = update_season_master_plan_hybrid(
                cache_dir,
                season_cfg_obj,
                training_summary,
                token,
                target_week=target_week,
                prev_week=prev_week,
                plan_text=plan_text,
                plan_json=plan_json,
                week_activities=week_activities,
                activity_details=activity_details,
                activity_metrics=activity_metrics or {},
                erg_df=erg_df,
                erg_types=erg_types,
                require_trainer_for_rowing=require_trainer_for_rowing,
                gym_types=gym_types,
                gym_name_patterns=gym_name_patterns,
                athlete_profiles=profiles_by_id,
                activity_athlete_ids=activity_athlete_ids,
                refresh_macro=False,
                include_lifting=include_lifting,
            )
            print(f"Season master plan updated: {md_path}", flush=True)
            try:
                from season_master_plan import verify_week_intensity_against_target

                intensity_cap_report = verify_week_intensity_against_target(
                    cache_dir, season_cfg_obj, target_week
                )
                if intensity_cap_report:
                    print(intensity_cap_report, flush=True)
            except Exception as exc:
                print(f"Intensity cap verification skipped: {exc}", flush=True)
        except Exception as exc:
            print(f"Season master plan update skipped: {exc}", flush=True)

    if send_athlete_plan_dms and plan_json is not None and plan_text.strip():
        pipeline_athletes = load_pipeline_athletes(config_path)
        send_weekly_athlete_plan_dms(
            cache_dir,
            pipeline_athletes,
            plan_text,
            target_week,
            prev_week,
            activity_details,
            activity_metrics,
            token,
            squad_plan_json=plan_json,
            erg_df=erg_df,
            include_lifting=include_lifting,
            coach_adjustments=coach_adjustments,
            erg_types=erg_types,
            require_trainer_for_rowing=require_trainer_for_rowing,
            zuliprc_path=zuliprc_path,
            season_week_context=season_week_context,
            season_cfg_obj=season_cfg_obj,
        )

    report_parts = []
    if adherence_review:
        report_parts.append(
            "=== Previous week adherence ===\n\n" + adherence_review.strip()
        )
    if gym_tonnage_summary:
        report_parts.append("=== Gym tonnage ===\n\n" + gym_tonnage_summary.strip())
    report_parts.append(
        f"=== Squad weekly plan ({target_week.week_start} – {target_week.week_end}) ===\n\n"
        + plan_text.strip()
    )
    if intensity_cap_report:
        report_parts.append(
            "=== Intensity cap check ===\n\n" + intensity_cap_report.strip()
        )
    full_report = "\n\n".join(report_parts)
    return full_report, record


def post_plan_to_zulip(
    plan_text: str,
    stream: str,
    topic: str,
    zuliprc_path: Optional[Path] = None,
) -> None:
    """Post plan_text to a Zulip stream/topic; print result, do not raise."""
    try:
        from send_to_zulip import send_text_to_zulip
    except ImportError:
        print(
            "Zulip upload unavailable: send_to_zulip not on PYTHONPATH "
            "(expected lighties/ next to erg_strava/).",
            flush=True,
        )
        return
    send_text_to_zulip(
        plan_text,
        stream=stream,
        topic=topic,
        filename="weekly_training_plan.txt",
        initial_comment="Squad weekly training plan (average targets)",
        title="Squad weekly training plan",
        zuliprc_path=zuliprc_path,
    )
