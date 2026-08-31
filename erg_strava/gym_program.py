"""Mesocycle gym programs: exercise selection and per-lift progression."""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from gym_deload import round_plate_kg
from gym_pyramid import infer_peak_weight_kg
from weekly_plan_schema import (
    GYM_CATEGORIES,
    GYM_CATEGORY_LEG,
    GYM_CATEGORY_UPPER_CORE,
    GYM_EXERCISE_NAMES,
    GYM_GOALS,
    GymExercise,
    GymSession,
    GymSet,
    classify_gym_exercise,
    is_low_intensity_plan_phase,
    parse_weekly_plan,
    weekly_plan_to_dict,
)

DATA_DIR = Path(__file__).resolve().parent / "data" / "gym_programs"

_DEFAULT_PEAK_KG: Dict[str, float] = {
    "Arnold press": 20.0,
    "Back squat": 70.0,
    "Back squat to box": 70.0,
    "Barbell row": 45.0,
    "Bench press": 50.0,
    "Bulgarian split squat": 30.0,
    "Hex-bar deadlift": 80.0,
    "Incline bench press": 40.0,
    "Kettlebell swings": 16.0,
    "Lat pull-down": 50.0,
    "Lat pulls": 50.0,
    "Pull-ups": 0.0,
    "Romanian deadlift": 60.0,
    "Russian twists": 8.0,
}

_POOR_RECOVERY_RE = re.compile(
    r"\b("
    r"wrecked|exhausted|destroyed|spent|"
    r"slept\s+(terribly|badly|poorly|awful)|didn't\s+sleep|insomnia|"
    r"hrv|"
    r"go\s+easy|poor\s+recovery|feeling\s+flat|run\s+down"
    r")\b",
    re.I,
)

RPE_PROGRESS_MAX = 7.0
RPE_HOLD_MIN = 9.0


@dataclass(frozen=True)
class ProgressionRule:
    kind: str = "double_progression"
    increment_kg: float = 2.5
    hit_sessions_required: int = 2
    rpe_progress_max: float = RPE_PROGRESS_MAX
    rpe_hold_min: float = RPE_HOLD_MIN


@dataclass(frozen=True)
class ProgramExercise:
    name: str
    progression: ProgressionRule = field(default_factory=ProgressionRule)
    default_peak_kg: Optional[float] = None
    default_duration_sec: Optional[int] = None
    default_reps: int = 8
    default_sets: int = 3


@dataclass(frozen=True)
class Rotation:
    after_weeks: int
    replace: str
    with_name: str


@dataclass(frozen=True)
class ProgramDay:
    category: str
    goal: str
    exercises: Tuple[ProgramExercise, ...]


@dataclass(frozen=True)
class GymProgram:
    id: str
    phases: Tuple[str, ...]
    monday: ProgramDay
    wednesday: ProgramDay
    rotations: Tuple[Rotation, ...] = ()


@dataclass(frozen=True)
class LiftLog:
    peak_kg: float
    hit: bool
    rpe: Optional[float] = None


def _parse_rule(raw: Mapping[str, Any]) -> ProgressionRule:
    kind = str(raw.get("kind") or "double_progression").strip() or "double_progression"
    increment = float(raw.get("increment_kg") or 2.5)
    hits = int(raw.get("hit_sessions_required") or 2)
    rpe_max = float(raw.get("rpe_progress_max") or RPE_PROGRESS_MAX)
    rpe_hold = float(raw.get("rpe_hold_min") or RPE_HOLD_MIN)
    return ProgressionRule(
        kind=kind,
        increment_kg=increment,
        hit_sessions_required=max(1, hits),
        rpe_progress_max=rpe_max,
        rpe_hold_min=rpe_hold,
    )


def _parse_exercise(raw: Mapping[str, Any]) -> Optional[ProgramExercise]:
    name = str(raw.get("name") or "").strip()
    if name not in GYM_EXERCISE_NAMES:
        return None
    peak_raw = raw.get("default_peak_kg")
    duration_raw = raw.get("default_duration_sec")
    reps = int(raw.get("default_reps") or (1 if duration_raw else 8))
    sets = int(raw.get("default_sets") or (2 if duration_raw else 3))
    rule_raw = raw.get("progression") if isinstance(raw.get("progression"), dict) else {}
    return ProgramExercise(
        name=name,
        progression=_parse_rule(rule_raw or {}),
        default_peak_kg=float(peak_raw) if peak_raw is not None else None,
        default_duration_sec=int(duration_raw) if duration_raw is not None else None,
        default_reps=max(1, reps),
        default_sets=max(1, sets),
    )


def _parse_day(raw: Mapping[str, Any]) -> Optional[ProgramDay]:
    category = str(raw.get("category") or "")
    goal = str(raw.get("goal") or "")
    if category not in GYM_CATEGORIES or goal not in GYM_GOALS:
        return None
    exercises_raw = raw.get("exercises")
    if not isinstance(exercises_raw, list):
        return None
    exercises: List[ProgramExercise] = []
    for item in exercises_raw:
        if not isinstance(item, dict):
            return None
        parsed = _parse_exercise(item)
        if parsed is None:
            return None
        exercises.append(parsed)
    return ProgramDay(category=category, goal=goal, exercises=tuple(exercises))


def program_from_dict(data: Mapping[str, Any]) -> Optional[GymProgram]:
    monday = _parse_day(data.get("monday") or {})
    wednesday = _parse_day(data.get("wednesday") or {})
    if monday is None or wednesday is None:
        return None
    rotations: List[Rotation] = []
    for item in data.get("rotations") or []:
        if not isinstance(item, dict):
            continue
        replace_name = str(item.get("replace") or "").strip()
        with_name = str(item.get("with") or "").strip()
        try:
            after = int(item.get("after_weeks"))
        except (TypeError, ValueError):
            continue
        if replace_name and with_name:
            rotations.append(
                Rotation(after_weeks=after, replace=replace_name, with_name=with_name)
            )
    phases_raw = data.get("phases") or []
    phases = tuple(str(p) for p in phases_raw) if isinstance(phases_raw, list) else ()
    program_id = str(data.get("id") or "").strip()
    if not program_id:
        return None
    return GymProgram(
        id=program_id,
        phases=phases,
        monday=monday,
        wednesday=wednesday,
        rotations=tuple(rotations),
    )


def validate_program(program: GymProgram) -> Optional[str]:
    for label, day in (("monday", program.monday), ("wednesday", program.wednesday)):
        if len(day.exercises) != 4:
            return f"{label}: expected 4 exercises, got {len(day.exercises)}"
        expected = GYM_CATEGORY_LEG if label == "monday" else GYM_CATEGORY_UPPER_CORE
        if day.category != expected:
            return f"{label}: category must be {expected}"
        names = [ex.name for ex in day.exercises]
        if len(set(names)) != 4:
            return f"{label}: duplicate exercise names"
        for ex in day.exercises:
            if ex.name not in GYM_EXERCISE_NAMES:
                return f"{label}: unknown exercise {ex.name!r}"
            if ex.name == "Plank":
                continue
            cat = classify_gym_exercise(ex.name)
            if cat is not None and cat != day.category:
                return f"{label}: {ex.name} does not belong in {day.category}"
    for rotation in program.rotations:
        if rotation.replace not in GYM_EXERCISE_NAMES:
            return f"rotation replace unknown: {rotation.replace!r}"
        if rotation.with_name not in GYM_EXERCISE_NAMES:
            return f"rotation with unknown: {rotation.with_name!r}"
        if rotation.after_weeks < 1:
            return "rotation after_weeks must be >= 1"
    return None


def _program_path(phase: str, data_dir: Path) -> Path:
    key = (phase or "base").strip().lower()
    if is_low_intensity_plan_phase(key):
        key = "build"
    if key not in ("base", "build"):
        key = "base"
    return data_dir / f"{key}.json"


def load_program_from_plan(
    plan_json: Optional[Mapping[str, Any]], data_dir: Optional[Path] = None
) -> GymProgram:
    meta = (plan_json or {}).get("gym_program") if isinstance(plan_json, Mapping) else None
    program_id = ""
    if isinstance(meta, Mapping):
        program_id = str(meta.get("id") or "")
    phase = "build" if program_id.startswith("build") else "base"
    return load_program(phase, data_dir=data_dir)


def load_program(
    phase: Optional[str] = None, data_dir: Optional[Path] = None
) -> GymProgram:
    directory = data_dir or DATA_DIR
    path = _program_path(phase or "base", directory)
    data = json.loads(path.read_text(encoding="utf-8"))
    program = program_from_dict(data)
    if program is None:
        raise ValueError(f"invalid gym program: {path}")
    err = validate_program(program)
    if err:
        raise ValueError(f"{path.name}: {err}")
    return program


def _replacement_exercise(name: str, template: ProgramExercise) -> ProgramExercise:
    peak = _DEFAULT_PEAK_KG.get(name, template.default_peak_kg)
    duration = 30 if name.lower() == "plank" else None
    return replace(
        template,
        name=name,
        default_peak_kg=None if duration else peak,
        default_duration_sec=duration,
        default_reps=1 if duration else template.default_reps,
        default_sets=2 if duration else template.default_sets,
    )


def exercises_for_week(
    day: ProgramDay, week_index: int, rotations: Sequence[Rotation]
) -> Tuple[ProgramExercise, ...]:
    mapping: Dict[str, str] = {}
    for rotation in rotations:
        if week_index >= rotation.after_weeks:
            mapping[rotation.replace] = rotation.with_name
    out: List[ProgramExercise] = []
    for ex in day.exercises:
        new_name = mapping.get(ex.name)
        if new_name and new_name != ex.name:
            out.append(_replacement_exercise(new_name, ex))
        else:
            out.append(ex)
    return tuple(out)


def _peak_for(ex: ProgramExercise, peak_kg_by_exercise: Mapping[str, float]) -> float:
    if ex.name in peak_kg_by_exercise:
        return float(peak_kg_by_exercise[ex.name])
    if ex.default_peak_kg is not None:
        return float(ex.default_peak_kg)
    return float(_DEFAULT_PEAK_KG.get(ex.name, 40.0))


def _materialize_exercise(
    ex: ProgramExercise, peak_kg_by_exercise: Mapping[str, float]
) -> GymExercise:
    if ex.default_duration_sec:
        duration = int(ex.default_duration_sec)
        return GymExercise(
            name=ex.name,
            sets=[
                GymSet(reps=ex.default_reps, weight_kg=None, duration_sec=duration)
                for _ in range(ex.default_sets)
            ],
        )
    peak = _peak_for(ex, peak_kg_by_exercise)
    if peak <= 0:
        return GymExercise(
            name=ex.name,
            sets=[
                GymSet(reps=ex.default_reps, weight_kg=0.0, duration_sec=None)
                for _ in range(ex.default_sets)
            ],
        )
    load = round_plate_kg(max(2.5, peak))
    return GymExercise(
        name=ex.name,
        sets=[
            GymSet(reps=ex.default_reps, weight_kg=load, duration_sec=None)
            for _ in range(ex.default_sets)
        ],
    )


def _materialize_day(
    day: ProgramDay,
    exercises: Sequence[ProgramExercise],
    peak_kg_by_exercise: Mapping[str, float],
) -> GymSession:
    return GymSession(
        category=day.category,
        goal=day.goal,
        exercises=[_materialize_exercise(ex, peak_kg_by_exercise) for ex in exercises],
    )


def _gym_from_plan_day(
    plan_json: Mapping[str, Any], weekday: str
) -> Optional[GymSession]:
    parsed = parse_weekly_plan(plan_json)
    if parsed is None:
        return None
    for day in parsed.days:
        if day.weekday == weekday and day.gym:
            return day.gym
    return None


def _reuse_prev_session(
    prev: GymSession,
    peak_kg_by_exercise: Mapping[str, float],
    day: ProgramDay,
) -> GymSession:
    exercises: List[GymExercise] = []
    for prev_ex in prev.exercises:
        duration = next((s.duration_sec for s in prev_ex.sets if s.duration_sec), None)
        if duration:
            count = len(prev_ex.sets) or 2
            exercises.append(
                GymExercise(
                    name=prev_ex.name,
                    sets=[
                        GymSet(reps=1, weight_kg=None, duration_sec=duration)
                        for _ in range(count)
                    ],
                )
            )
            continue
        peak = peak_kg_by_exercise.get(prev_ex.name)
        if peak is None:
            peak = infer_peak_weight_kg(prev_ex.sets)
        load = round_plate_kg(max(0.0, float(peak)))
        count = max(1, len(prev_ex.sets))
        reps = prev_ex.sets[0].reps if prev_ex.sets else 8
        exercises.append(
            GymExercise(
                name=prev_ex.name,
                sets=[
                    GymSet(
                        reps=reps,
                        weight_kg=load if load > 0 else 0.0,
                        duration_sec=None,
                    )
                    for _ in range(count)
                ],
            )
        )
    return GymSession(category=day.category, goal=day.goal, exercises=exercises)


def materialize_week(
    program: GymProgram,
    *,
    week_index: int = 0,
    peak_kg_by_exercise: Optional[Mapping[str, float]] = None,
    prev_plan_json: Optional[Mapping[str, Any]] = None,
    phase: Optional[str] = None,
) -> Tuple[GymSession, GymSession]:
    peaks = dict(peak_kg_by_exercise or {})
    if is_low_intensity_plan_phase(phase) and prev_plan_json:
        prev_mon = _gym_from_plan_day(prev_plan_json, "Monday")
        prev_wed = _gym_from_plan_day(prev_plan_json, "Wednesday")
        if prev_mon and prev_wed:
            return (
                _reuse_prev_session(prev_mon, peaks, program.monday),
                _reuse_prev_session(prev_wed, peaks, program.wednesday),
            )
    monday_ex = exercises_for_week(program.monday, week_index, program.rotations)
    wednesday_ex = exercises_for_week(
        program.wednesday, week_index, program.rotations
    )
    return (
        _materialize_day(program.monday, monday_ex, peaks),
        _materialize_day(program.wednesday, wednesday_ex, peaks),
    )


def gym_session_to_dict(gym: GymSession) -> Dict[str, Any]:
    return {
        "category": gym.category,
        "goal": gym.goal,
        "exercises": [
            {
                "name": ex.name,
                "sets": [
                    {
                        "reps": s.reps,
                        "weight_kg": s.weight_kg,
                        "duration_sec": s.duration_sec,
                    }
                    for s in ex.sets
                ],
            }
            for ex in gym.exercises
        ],
    }


def apply_gym_program_to_plan(
    plan_json: Mapping[str, Any],
    monday: GymSession,
    wednesday: GymSession,
    *,
    week_index: int = 0,
    program_id: Optional[str] = None,
) -> Dict[str, Any]:
    out = json.loads(json.dumps(plan_json))
    for day in out.get("days") or []:
        weekday = day.get("weekday")
        if weekday == "Monday":
            day["session_type"] = "gym"
            day["gym"] = gym_session_to_dict(monday)
            day["rowing"] = None
        elif weekday == "Wednesday":
            day["session_type"] = "gym"
            day["gym"] = gym_session_to_dict(wednesday)
            day["rowing"] = None
    out["gym_program"] = {
        "id": program_id or monday.category,
        "week_index": int(week_index),
    }
    return out


def apply_program_gym_to_plan(
    plan_json: Mapping[str, Any],
    *,
    phase: Optional[str] = None,
    week_index: int = 0,
    peak_kg_by_exercise: Optional[Mapping[str, float]] = None,
    prev_plan_json: Optional[Mapping[str, Any]] = None,
    data_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    program = load_program(phase, data_dir=data_dir)
    monday, wednesday = materialize_week(
        program,
        week_index=week_index,
        peak_kg_by_exercise=peak_kg_by_exercise,
        prev_plan_json=prev_plan_json,
        phase=phase,
    )
    return apply_gym_program_to_plan(
        plan_json,
        monday,
        wednesday,
        week_index=week_index,
        program_id=program.id,
    )


def next_gym_week_index(prev_plan_json: Optional[Mapping[str, Any]]) -> int:
    if not prev_plan_json:
        return 0
    meta = prev_plan_json.get("gym_program")
    if not isinstance(meta, dict):
        return 0
    try:
        return int(meta.get("week_index", 0)) + 1
    except (TypeError, ValueError):
        return 0


def next_gym_week_index_from_plans(records: Sequence[Mapping[str, Any]]) -> int:
    """Return the week after the newest record carrying gym program metadata."""
    for record in records:
        plan_json = record.get("plan_json")
        if not isinstance(plan_json, Mapping):
            continue
        meta = plan_json.get("gym_program")
        if not isinstance(meta, Mapping) or "week_index" not in meta:
            continue
        try:
            return int(meta["week_index"]) + 1
        except (TypeError, ValueError):
            continue
    return 0


def _parse_start(raw: Any) -> datetime:
    text = str(raw or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _athlete_key(rec: Mapping[str, Any], fallback: Any) -> Any:
    if rec.get("athlete_id") is not None:
        return rec.get("athlete_id")
    return fallback


def median_latest_peak_kg(
    metrics_by_id: Mapping[Any, Mapping[str, Any]],
) -> Dict[str, float]:
    """Median of each athlete's latest logged peak per exercise."""
    latest: Dict[Any, Dict[str, Tuple[datetime, float]]] = {}
    for rec_id, rec in metrics_by_id.items():
        if not isinstance(rec, Mapping):
            continue
        gym = rec.get("gym")
        if not isinstance(gym, Mapping):
            continue
        start = _parse_start(rec.get("start_date"))
        athlete = _athlete_key(rec, rec_id)
        by_ex = latest.setdefault(athlete, {})
        for ex in gym.get("exercises") or []:
            if not isinstance(ex, Mapping):
                continue
            name = str(ex.get("name") or "").strip()
            if not name:
                continue
            try:
                peak = float(ex.get("max_weight_kg", 0) or 0)
            except (TypeError, ValueError):
                continue
            if peak <= 0:
                continue
            prev = by_ex.get(name)
            if prev is None or start >= prev[0]:
                by_ex[name] = (start, peak)
    by_name: Dict[str, List[float]] = {}
    for athlete_peaks in latest.values():
        for name, (_when, peak) in athlete_peaks.items():
            by_name.setdefault(name, []).append(peak)
    return {name: float(statistics.median(vals)) for name, vals in by_name.items()}


def _set_rpe(raw: Mapping[str, Any]) -> Optional[float]:
    value = raw.get("rpe")
    if value is None or value == "":
        return None
    try:
        rpe = float(value)
    except (TypeError, ValueError):
        return None
    if rpe < 1 or rpe > 10:
        return None
    return rpe


def lift_logs_from_metrics(
    metrics_by_id: Mapping[Any, Mapping[str, Any]],
) -> Dict[str, List[LiftLog]]:
    rows: List[Tuple[datetime, str, LiftLog]] = []
    for rec in metrics_by_id.values():
        if not isinstance(rec, Mapping):
            continue
        gym = rec.get("gym")
        if not isinstance(gym, Mapping):
            continue
        start = _parse_start(rec.get("start_date"))
        for ex in gym.get("exercises") or []:
            if not isinstance(ex, Mapping):
                continue
            name = str(ex.get("name") or "").strip()
            if not name:
                continue
            sets = ex.get("sets") or []
            peak = 0.0
            rpes: List[float] = []
            peak_reps = 0
            if isinstance(sets, list) and sets:
                for s in sets:
                    if not isinstance(s, Mapping):
                        continue
                    try:
                        weight = float(s.get("weight_kg") or 0)
                    except (TypeError, ValueError):
                        continue
                    rpe = _set_rpe(s)
                    if rpe is not None:
                        rpes.append(rpe)
                    if weight >= peak:
                        peak = weight
                        try:
                            peak_reps = int(s.get("reps") or 0)
                        except (TypeError, ValueError):
                            peak_reps = 0
            else:
                try:
                    peak = float(ex.get("max_weight_kg") or 0)
                except (TypeError, ValueError):
                    peak = 0.0
            session_rpe = rpes[-1] if rpes else None
            hit = peak > 0 and peak_reps >= 5
            if not sets:
                hit = peak > 0
            rows.append((start, name, LiftLog(peak_kg=peak, hit=hit, rpe=session_rpe)))
    rows.sort(key=lambda item: item[0])
    out: Dict[str, List[LiftLog]] = {}
    for _start, name, log in rows:
        out.setdefault(name, []).append(log)
    return out


def _rpe_blocks_progress(rpe: Optional[float], rule: ProgressionRule) -> bool:
    return rpe is not None and rpe >= rule.rpe_hold_min


def _rpe_allows_progress(rpe: Optional[float], rule: ProgressionRule) -> bool:
    if rpe is None:
        return True
    return rpe <= rule.rpe_progress_max


def progression_decision(logs: Sequence[LiftLog], rule: ProgressionRule) -> str:
    if rule.kind == "none" or not logs:
        return "hold"
    last = logs[-1]
    if not last.hit:
        return "regress"
    if _rpe_blocks_progress(last.rpe, rule):
        return "hold"
    needed = rule.hit_sessions_required
    if len(logs) < needed:
        return "hold"
    window = logs[-needed:]
    if all(item.hit and _rpe_allows_progress(item.rpe, rule) for item in window):
        return "progress"
    return "hold"


def apply_progression(
    logs: Sequence[LiftLog],
    rule: ProgressionRule,
    *,
    fallback: Optional[float] = None,
) -> float:
    if not logs:
        return float(fallback or 0.0)
    last_peak = float(logs[-1].peak_kg)
    decision = progression_decision(logs, rule)
    if decision == "progress":
        return round_plate_kg(last_peak + rule.increment_kg)
    if decision == "regress":
        return round_plate_kg(max(2.5, last_peak - rule.increment_kg))
    return round_plate_kg(last_peak)


def _rule_for_name(program: GymProgram, name: str) -> ProgressionRule:
    for day in (program.monday, program.wednesday):
        for ex in day.exercises:
            if ex.name == name:
                return ex.progression
    for rotation in program.rotations:
        if rotation.with_name == name:
            for day in (program.monday, program.wednesday):
                for ex in day.exercises:
                    if ex.name == rotation.replace:
                        return ex.progression
    return ProgressionRule()


def _scale_sets_to_peak(sets: Sequence[GymSet], new_peak: float) -> List[GymSet]:
    old_peak = infer_peak_weight_kg(sets)
    out: List[GymSet] = []
    for s in sets:
        if s.duration_sec:
            out.append(
                GymSet(reps=s.reps, weight_kg=s.weight_kg, duration_sec=s.duration_sec)
            )
            continue
        if s.weight_kg is None:
            out.append(GymSet(reps=s.reps, weight_kg=None, duration_sec=s.duration_sec))
            continue
        if old_peak <= 0:
            load = round_plate_kg(new_peak) if new_peak > 0 else s.weight_kg
        else:
            load = round_plate_kg(max(0.0, float(s.weight_kg) * (new_peak / old_peak)))
        out.append(GymSet(reps=s.reps, weight_kg=load, duration_sec=None))
    return out


def personalize_plan_gym_loads(
    athlete_plan_json: Mapping[str, Any],
    squad_plan_json: Mapping[str, Any],
    *,
    lift_logs_by_exercise: Optional[Mapping[str, Sequence[LiftLog]]] = None,
    program: Optional[GymProgram] = None,
) -> Dict[str, Any]:
    """Keep squad gym names/order; set kg from this athlete's logs + progression."""
    squad = parse_weekly_plan(squad_plan_json)
    athlete = parse_weekly_plan(athlete_plan_json)
    if squad is None:
        return json.loads(json.dumps(athlete_plan_json))
    if athlete is None:
        athlete = squad
    logs = lift_logs_by_exercise or {}
    new_days = []
    for a_day, s_day in zip(athlete.days, squad.days):
        if s_day.gym is None:
            new_days.append(a_day)
            continue
        exercises: List[GymExercise] = []
        for s_ex in s_day.gym.exercises:
            rule = (
                _rule_for_name(program, s_ex.name) if program else ProgressionRule()
            )
            fallback = infer_peak_weight_kg(s_ex.sets)
            if s_ex.sets and any(s.duration_sec for s in s_ex.sets):
                exercises.append(GymExercise(name=s_ex.name, sets=list(s_ex.sets)))
                continue
            peak = apply_progression(
                list(logs.get(s_ex.name) or []),
                rule,
                fallback=fallback,
            )
            if not logs.get(s_ex.name):
                peak = fallback
            exercises.append(
                GymExercise(
                    name=s_ex.name,
                    sets=_scale_sets_to_peak(s_ex.sets, peak),
                )
            )
        gym = GymSession(
            category=s_day.gym.category,
            goal=s_day.gym.goal,
            exercises=exercises,
        )
        new_days.append(replace(a_day, gym=gym, session_type="gym", rowing=None))
    personalised = replace(athlete, days=new_days, personalised=True)
    out = weekly_plan_to_dict(personalised)
    for key in ("gym_program", "session_library"):
        if key in athlete_plan_json:
            out[key] = athlete_plan_json[key]
        elif key in squad_plan_json:
            out[key] = squad_plan_json[key]
    return out


def template_gym_session(weekday: str, *, phase: str = "base") -> GymSession:
    program = load_program(phase)
    monday, wednesday = materialize_week(program, week_index=0, peak_kg_by_exercise={})
    gym = monday if weekday == "Monday" else wednesday
    if is_low_intensity_plan_phase(phase):
        from gym_deload import DeloadConfig, apply_deload_modifier

        return apply_deload_modifier(gym, DeloadConfig(min_sets=2, max_sets=2))
    if (phase or "").strip().lower() in ("base", "build"):
        from gym_pyramid import apply_phase_gym_pyramid

        return apply_phase_gym_pyramid(gym, phase)
    return gym


def gym_log_missing_rpe(record: Mapping[str, Any]) -> bool:
    gym = record.get("gym") if isinstance(record, Mapping) else None
    if not isinstance(gym, Mapping):
        return False
    last: Optional[Mapping[str, Any]] = None
    for ex in gym.get("exercises") or []:
        if not isinstance(ex, Mapping):
            continue
        for s in ex.get("sets") or []:
            if not isinstance(s, Mapping):
                continue
            try:
                weight = float(s.get("weight_kg") or 0)
            except (TypeError, ValueError):
                weight = 0.0
            duration = s.get("duration_sec")
            if duration or weight <= 0:
                continue
            last = s
    if last is None:
        return False
    return _set_rpe(last) is None


def format_rpe_follow_up() -> str:
    return (
        "How hard did the last set feel? Reply with RPE 1–10, or "
        "easy / moderate / hard / max effort."
    )


_RPE_WORD_VALUES = {
    "easy": 5.0,
    "moderate": 7.0,
    "hard": 8.5,
    "max": 10.0,
    "max effort": 10.0,
    "maximum effort": 10.0,
}
_RPE_NUMBER_RE = re.compile(
    r"^(?:rpe\s*)?(\d+(?:\.\d+)?)(?:\s*/\s*10)?$",
    re.I,
)


def parse_rpe_follow_up_reply(text: str) -> Optional[float]:
    """Parse a short RPE follow-up such as 'RPE 6' or 'hard'. Else None."""
    body = " ".join((text or "").strip().split())
    if not body:
        return None
    lower = body.lower().rstrip(".!")
    if lower in _RPE_WORD_VALUES:
        return _RPE_WORD_VALUES[lower]
    match = _RPE_NUMBER_RE.fullmatch(lower)
    if not match:
        return None
    try:
        rpe = float(match.group(1))
    except (TypeError, ValueError):
        return None
    if rpe < 1 or rpe > 10:
        return None
    return rpe


def _is_weighted_working_set(raw: Mapping[str, Any]) -> bool:
    if raw.get("duration_sec"):
        return False
    try:
        return float(raw.get("weight_kg") or 0) > 0
    except (TypeError, ValueError):
        return False


def apply_rpe_to_last_working_set(record: Dict[str, Any], rpe: float) -> bool:
    """Set rpe on the last weighted working set. Returns True if a set was updated."""
    gym = record.get("gym")
    if not isinstance(gym, Mapping):
        return False
    last: Optional[Dict[str, Any]] = None
    for ex in gym.get("exercises") or []:
        if not isinstance(ex, Mapping):
            continue
        for s in ex.get("sets") or []:
            if isinstance(s, dict) and _is_weighted_working_set(s):
                last = s
    if last is None:
        return False
    last["rpe"] = float(rpe)
    return True


def extract_session_rpe_from_transcript(text: str) -> Optional[float]:
    """Last session-level RPE line (``RPE 4`` / ``easy``). Bare numbers are ignored."""
    last: Optional[float] = None
    for raw in (text or "").splitlines():
        line = " ".join(raw.strip().split())
        if not line:
            continue
        lower = line.lower().rstrip(".!")
        if not (lower.startswith("rpe") or lower in _RPE_WORD_VALUES):
            continue
        parsed = parse_rpe_follow_up_reply(line)
        if parsed is not None:
            last = parsed
    return last


def overlay_session_rpe_on_record(record: Dict[str, Any], transcript: str) -> bool:
    """Apply transcript session RPE to the last weighted set if it has none."""
    rpe = extract_session_rpe_from_transcript(transcript)
    if rpe is None or not gym_log_missing_rpe(record):
        return False
    return apply_rpe_to_last_working_set(record, rpe)


def _canonical_gym_exercise_name(name: str) -> str:
    try:
        from generate_training_plan import normalize_gym_exercise_header
    except ImportError:
        normalize_gym_exercise_header = None  # type: ignore[assignment]
    text = (name or "").strip()
    if normalize_gym_exercise_header is not None:
        canonical = normalize_gym_exercise_header(text)
        if canonical:
            return canonical.lower()
    stripped = re.sub(r"^\d+\.\s*", "", text)
    stripped = re.sub(r"^\*+|\*+$", "", stripped).strip().rstrip(":")
    return stripped.lower()


def _recorded_exercise_for_header(
    record: Mapping[str, Any], line: str
) -> Optional[Mapping[str, Any]]:
    gym = record.get("gym") if isinstance(record, Mapping) else None
    if not isinstance(gym, Mapping):
        return None
    header_key = _canonical_gym_exercise_name(line)
    stripped = re.sub(r"^\d+\.\s*", "", (line or "").strip())
    stripped = re.sub(r"^\*+|\*+$", "", stripped).strip()
    stripped_lower = stripped.lower()
    for ex in gym.get("exercises") or []:
        if not isinstance(ex, Mapping):
            continue
        name = str(ex.get("name") or "")
        name_key = _canonical_gym_exercise_name(name)
        if header_key and name_key and header_key == name_key:
            return ex
        lower_name = name.lower()
        if stripped_lower == lower_name or stripped_lower.startswith(lower_name + ":"):
            return ex
    return None


def _is_duration_set(raw: Mapping[str, Any]) -> bool:
    try:
        return float(raw.get("duration_sec") or 0) > 0
    except (TypeError, ValueError):
        return False


def apply_rpe_to_exercise_last_working_set(
    record: Dict[str, Any], exercise_name: str, rpe: float
) -> bool:
    """Set rpe on an exercise's last working set. Does not overwrite existing rpe."""
    gym = record.get("gym")
    if not isinstance(gym, Mapping):
        return False
    want = _canonical_gym_exercise_name(exercise_name)
    target_ex: Optional[Mapping[str, Any]] = None
    for ex in gym.get("exercises") or []:
        if not isinstance(ex, Mapping):
            continue
        if _canonical_gym_exercise_name(str(ex.get("name") or "")) == want:
            target_ex = ex
            break
    if target_ex is None:
        return False
    last_weighted: Optional[Dict[str, Any]] = None
    last_duration: Optional[Dict[str, Any]] = None
    last_set: Optional[Dict[str, Any]] = None
    for s in target_ex.get("sets") or []:
        if not isinstance(s, dict):
            continue
        last_set = s
        if _is_weighted_working_set(s):
            last_weighted = s
        elif _is_duration_set(s):
            last_duration = s
    last = last_weighted or last_duration or last_set
    if last is None or _set_rpe(last) is not None:
        return False
    last["rpe"] = float(rpe)
    return True


def overlay_exercise_rpe_on_record(record: Dict[str, Any], transcript: str) -> bool:
    """Apply per-exercise ``RPE N`` lines to each exercise's last working set."""
    current_name: Optional[str] = None
    updated = False
    for raw in (transcript or "").splitlines():
        line = " ".join(raw.strip().split())
        if not line:
            continue
        matched = _recorded_exercise_for_header(record, line)
        if matched is not None:
            current_name = str(matched.get("name") or "") or current_name
            continue
        lower = line.lower().rstrip(".!")
        if not (lower.startswith("rpe") or lower in _RPE_WORD_VALUES):
            continue
        parsed = parse_rpe_follow_up_reply(line)
        if parsed is None or not current_name:
            continue
        if apply_rpe_to_exercise_last_working_set(record, current_name, parsed):
            updated = True
    return updated


def overlay_transcript_rpe_on_record(record: Dict[str, Any], transcript: str) -> bool:
    """Per-exercise RPE first, then session-level last-set RPE if still missing."""
    updated = overlay_exercise_rpe_on_record(record, transcript)
    if overlay_session_rpe_on_record(record, transcript):
        updated = True
    return updated


def format_rpe_recorded_confirmation(
    records: Sequence[Mapping[str, Any]], rpe: float
) -> str:
    labels = [
        str(rec.get("athlete_label") or rec.get("athlete_id") or "athlete")
        for rec in records
    ]
    ids = [str(rec.get("id") or "") for rec in records]
    rpe_txt = f"{rpe:g}"
    if len(records) == 1:
        suffix = f" (`{ids[0]}`)" if ids[0] else ""
        return (
            f"Recorded RPE {rpe_txt} on {labels[0]}'s last working set{suffix}."
        )
    named = ", ".join(labels)
    return f"Recorded RPE {rpe_txt} on the last working set for {named}."


def format_lift_review(decisions: Mapping[str, str]) -> str:
    if not decisions:
        return "Gym lift review: no logged lifts to progress."
    lines = [
        "Gym lift review (performance → next week, not a rewrite of the program):"
    ]
    for name, decision in decisions.items():
        lines.append(f"- {name}: {decision}")
    return "\n".join(lines)


def review_lifts(
    lift_logs_by_exercise: Mapping[str, Sequence[LiftLog]],
    program: GymProgram,
) -> Dict[str, str]:
    names = [ex.name for ex in program.monday.exercises]
    names.extend(ex.name for ex in program.wednesday.exercises)
    names.extend(rotation.with_name for rotation in program.rotations)
    out: Dict[str, str] = {}
    for name in names:
        logs = lift_logs_by_exercise.get(name)
        if not logs:
            continue
        out[name] = progression_decision(logs, _rule_for_name(program, name))
    return out


def infer_poor_recovery(message: str) -> bool:
    return bool(_POOR_RECOVERY_RE.search(message or ""))


def gate_gym_session_for_recovery(
    gym: GymSession, *, poor_recovery: bool
) -> GymSession:
    if not poor_recovery:
        return gym
    exercises: List[GymExercise] = []
    for ex in gym.exercises:
        sets = list(ex.sets)
        if len(sets) >= 3:
            sets = sets[:-1]
        else:
            scaled: List[GymSet] = []
            for s in sets:
                if s.weight_kg is None or s.duration_sec:
                    scaled.append(s)
                    continue
                scaled.append(
                    GymSet(
                        reps=s.reps,
                        weight_kg=round_plate_kg(max(2.5, float(s.weight_kg) * 0.9)),
                        duration_sec=None,
                    )
                )
            sets = scaled
        exercises.append(GymExercise(name=ex.name, sets=sets))
    return GymSession(category=gym.category, goal=gym.goal, exercises=exercises)


def recovery_gate_note(poor_recovery: bool) -> Optional[str]:
    if not poor_recovery:
        return None
    return (
        "Recovery looks poor — today's gym is gated (dropped a set / ~10% load). "
        "This does not change the weekly program."
    )
