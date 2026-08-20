"""Curated and approved erg session templates for weekly plan generation."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from weekly_plan_schema import (
    DayPlan,
    RowingSegment,
    RowingSession,
    SESSION_CAP_MINUTES,
    WARMUP_COOLDOWN_CAP_MINUTES,
    _parse_rowing_session,
    estimate_rowing_session_minutes,
    parse_weekly_plan,
    validate_plan_session_constraints,
    validate_rowing_warmup_cooldown_caps,
    weekly_plan_to_dict,
)

DATA_DIR = Path(__file__).resolve().parent / "data" / "erg_sessions"
CURATED_DIR = DATA_DIR / "curated"
APPROVED_DIR = DATA_DIR / "approved"

DAY_ROLE_TUESDAY = "tuesday"
DAY_ROLE_THURSDAY = "thursday"


@dataclass(frozen=True)
class ErgSessionTemplate:
    id: str
    name: str
    source: str
    tags: Dict[str, Any]
    rowing: Dict[str, Any]
    approved_at: Optional[str] = None

    @property
    def session_subtype(self) -> str:
        return str(self.tags.get("session_subtype") or "steady-state")

    @property
    def total_minutes(self) -> int:
        session = _parse_rowing_session(self.rowing)
        if session is None:
            return 0
        return estimate_rowing_session_minutes(session)

    def rowing_session(self) -> Optional[RowingSession]:
        return _parse_rowing_session(self.rowing)


def _seg(
    phase: str,
    label: str,
    duration: str,
    *,
    zone_z: str,
    zone_t: str,
    split_min: str,
    split_max: str,
    hr_min: int,
    hr_max: int,
    priority: str = "hr",
) -> Dict[str, Any]:
    return {
        "phase": phase,
        "label": label,
        "duration": duration,
        "split_min": split_min,
        "split_max": split_max,
        "zone_z": zone_z,
        "zone_t": zone_t,
        "hr_bpm_min": hr_min,
        "hr_bpm_max": hr_max,
        "priority": priority,
        "notes": None,
    }


def _session(
    session_id: str,
    name: str,
    *,
    session_subtype: str,
    phases: Sequence[str],
    day_roles: Sequence[str],
    segments: List[Dict[str, Any]],
    zones: Sequence[str],
) -> Dict[str, Any]:
    total = 0
    parsed = _parse_rowing_session({"segments": segments, "erg_alternative": None})
    if parsed:
        total = estimate_rowing_session_minutes(parsed)
    return {
        "id": session_id,
        "name": name,
        "source": "curated",
        "approved_at": None,
        "tags": {
            "phase": list(phases),
            "zones": list(zones),
            "session_subtype": session_subtype,
            "day_role": list(day_roles),
            "total_minutes": total,
        },
        "rowing": {"segments": segments, "erg_alternative": None},
    }


CURATED_SEED: List[Dict[str, Any]] = [
    _session(
        "z2-steady-25",
        "25 min aerobic steady-state",
        session_subtype="steady-state",
        phases=["base", "build", "deload", "recovery"],
        day_roles=[DAY_ROLE_TUESDAY, DAY_ROLE_THURSDAY],
        zones=["Z2", "T3"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:15", split_max="2:25", hr_min=120, hr_max=140),
            _seg("main_set", "Aerobic steady-state", "25 min", zone_z="Z2", zone_t="T3", split_min="2:05", split_max="2:15", hr_min=130, hr_max=148),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=118, hr_max=135),
        ],
    ),
    _session(
        "z2-2x12",
        "2×12 min aerobic intervals",
        session_subtype="steady-state",
        phases=["base", "build", "deload"],
        day_roles=[DAY_ROLE_THURSDAY],
        zones=["Z2", "T3"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:15", split_max="2:25", hr_min=120, hr_max=140),
            _seg("main_set", "2×12 min / 2 min rest", "2×12 min / 2 min rest", zone_z="Z2", zone_t="T3", split_min="2:05", split_max="2:12", hr_min=132, hr_max=150),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=118, hr_max=135),
        ],
    ),
    _session(
        "z2-20-continuous",
        "20 min continuous aerobic",
        session_subtype="steady-state",
        phases=["deload", "recovery", "base"],
        day_roles=[DAY_ROLE_TUESDAY, DAY_ROLE_THURSDAY],
        zones=["Z2", "T3"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:15", split_max="2:25", hr_min=120, hr_max=140),
            _seg("main_set", "Aerobic steady-state", "20 min", zone_z="Z2", zone_t="T3", split_min="2:08", split_max="2:18", hr_min=128, hr_max=145),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=118, hr_max=135),
        ],
    ),
    _session(
        "z3-3x8",
        "3×8 min UT1",
        session_subtype="intervals",
        phases=["base", "build"],
        day_roles=[DAY_ROLE_TUESDAY, DAY_ROLE_THURSDAY],
        zones=["Z3", "T5"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:15", split_max="2:25", hr_min=120, hr_max=140),
            _seg("main_set", "3×8 min / 2 min rest", "3×8 min / 2 min rest", zone_z="Z3", zone_t="T5", split_min="2:00", split_max="2:08", hr_min=145, hr_max=162),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=118, hr_max=135),
        ],
    ),
    _session(
        "z3-2x10",
        "2×10 min UT1",
        session_subtype="intervals",
        phases=["base", "build"],
        day_roles=[DAY_ROLE_THURSDAY],
        zones=["Z3", "T5"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:15", split_max="2:25", hr_min=120, hr_max=140),
            _seg("main_set", "2×10 min / 2 min rest", "2×10 min / 2 min rest", zone_z="Z3", zone_t="T5", split_min="2:02", split_max="2:10", hr_min=148, hr_max=165),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=118, hr_max=135),
        ],
    ),
    _session(
        "threshold-4x5",
        "4×5 min threshold",
        session_subtype="threshold",
        phases=["base", "build", "peak"],
        day_roles=[DAY_ROLE_TUESDAY],
        zones=["Z4", "T6"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:15", split_max="2:25", hr_min=120, hr_max=140),
            _seg("main_set", "4×5 min / 2 min rest", "4×5 min / 2 min rest", zone_z="Z4", zone_t="T6", split_min="1:55", split_max="2:02", hr_min=165, hr_max=178),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=118, hr_max=135),
        ],
    ),
    _session(
        "threshold-3x6",
        "3×6 min threshold",
        session_subtype="threshold",
        phases=["base", "build"],
        day_roles=[DAY_ROLE_TUESDAY],
        zones=["Z4", "T6"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:15", split_max="2:25", hr_min=120, hr_max=140),
            _seg("main_set", "3×6 min / 2 min rest", "3×6 min / 2 min rest", zone_z="Z4", zone_t="T6", split_min="1:56", split_max="2:04", hr_min=163, hr_max=176),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=118, hr_max=135),
        ],
    ),
    _session(
        "threshold-4x4",
        "4×4 min threshold",
        session_subtype="threshold",
        phases=["build", "peak"],
        day_roles=[DAY_ROLE_TUESDAY],
        zones=["Z4", "T6"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:15", split_max="2:25", hr_min=120, hr_max=140),
            _seg("main_set", "4×4 min / 2 min rest", "4×4 min / 2 min rest", zone_z="Z4", zone_t="T6", split_min="1:54", split_max="2:00", hr_min=167, hr_max=180),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=118, hr_max=135),
        ],
    ),
    _session(
        "vo2-6x500",
        "6×500 m VO2",
        session_subtype="vo2",
        phases=["build", "peak"],
        day_roles=[DAY_ROLE_TUESDAY],
        zones=["Z5", "T7"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:15", split_max="2:25", hr_min=120, hr_max=140),
            _seg("main_set", "6×500 m / 3 min rest", "6×500 m / 3 min rest", zone_z="Z5", zone_t="T7", split_min="1:42", split_max="1:48", hr_min=175, hr_max=190, priority="split"),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=118, hr_max=135),
        ],
    ),
    _session(
        "vo2-5x500",
        "5×500 m VO2",
        session_subtype="vo2",
        phases=["build", "peak"],
        day_roles=[DAY_ROLE_TUESDAY],
        zones=["Z5", "T7"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:15", split_max="2:25", hr_min=120, hr_max=140),
            _seg("main_set", "5×500 m / 3 min rest", "5×500 m / 3 min rest", zone_z="Z5", zone_t="T7", split_min="1:43", split_max="1:49", hr_min=174, hr_max=188, priority="split"),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=118, hr_max=135),
        ],
    ),
    _session(
        "recovery-18",
        "18 min recovery aerobic",
        session_subtype="steady-state",
        phases=["deload", "recovery"],
        day_roles=[DAY_ROLE_TUESDAY, DAY_ROLE_THURSDAY],
        zones=["Z2", "T2"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=115, hr_max=132),
            _seg("main_set", "Easy aerobic", "18 min", zone_z="Z2", zone_t="T2", split_min="2:12", split_max="2:22", hr_min=120, hr_max=138),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:25", split_max="2:35", hr_min=115, hr_max=130),
        ],
    ),
    _session(
        "z2-3x8",
        "3×8 min aerobic pieces",
        session_subtype="intervals",
        phases=["base", "build", "deload"],
        day_roles=[DAY_ROLE_THURSDAY],
        zones=["Z2", "T3"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:15", split_max="2:25", hr_min=120, hr_max=140),
            _seg("main_set", "3×8 min / 2 min rest", "3×8 min / 2 min rest", zone_z="Z2", zone_t="T3", split_min="2:06", split_max="2:14", hr_min=135, hr_max=152),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=118, hr_max=135),
        ],
    ),
    _session(
        "tempo-2x12",
        "2×12 min tempo",
        session_subtype="threshold",
        phases=["base", "build"],
        day_roles=[DAY_ROLE_THURSDAY],
        zones=["Z3", "T5"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:15", split_max="2:25", hr_min=120, hr_max=140),
            _seg("main_set", "2×12 min / 2 min rest", "2×12 min / 2 min rest", zone_z="Z3", zone_t="T5", split_min="2:00", split_max="2:08", hr_min=150, hr_max=165),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=118, hr_max=135),
        ],
    ),
    _session(
        "race-pace-4x3",
        "4×3 min race-pace",
        session_subtype="race-pace",
        phases=["peak", "build"],
        day_roles=[DAY_ROLE_TUESDAY],
        zones=["Z5", "T7"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:15", split_max="2:25", hr_min=120, hr_max=140),
            _seg("main_set", "4×3 min / 3 min rest", "4×3 min / 3 min rest", zone_z="Z5", zone_t="T7", split_min="1:48", split_max="1:54", hr_min=178, hr_max=192, priority="split"),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=118, hr_max=135),
        ],
    ),
    _session(
        "z4-5x4",
        "5×4 min threshold",
        session_subtype="threshold",
        phases=["build", "peak"],
        day_roles=[DAY_ROLE_TUESDAY],
        zones=["Z4", "T6"],
        segments=[
            _seg("warm_up", "Warm-up", "8 min", zone_z="Z2", zone_t="T2", split_min="2:15", split_max="2:25", hr_min=120, hr_max=140),
            _seg("main_set", "5×4 min / 2 min rest", "5×4 min / 2 min rest", zone_z="Z4", zone_t="T6", split_min="1:55", split_max="2:02", hr_min=166, hr_max=179),
            _seg("cool_down", "Cool-down", "8 min", zone_z="Z2", zone_t="T2", split_min="2:20", split_max="2:30", hr_min=118, hr_max=135),
        ],
    ),
]

_TUESDAY_SUBTYPE_PREF: Dict[str, List[str]] = {
    "deload": ["steady-state"],
    "recovery": ["steady-state"],
    "base": ["threshold", "intervals", "steady-state"],
    "build": ["threshold", "vo2", "race-pace", "intervals"],
    "peak": ["vo2", "race-pace", "threshold"],
}

_THURSDAY_SUBTYPE_PREF: Dict[str, List[str]] = {
    "deload": ["steady-state"],
    "recovery": ["steady-state"],
    "base": ["steady-state", "intervals"],
    "build": ["steady-state", "intervals", "threshold"],
    "peak": ["steady-state", "threshold"],
}


def ensure_curated_seed_files() -> None:
    CURATED_DIR.mkdir(parents=True, exist_ok=True)
    APPROVED_DIR.mkdir(parents=True, exist_ok=True)
    for raw in CURATED_SEED:
        path = CURATED_DIR / f"{raw['id']}.json"
        if not path.is_file():
            path.write_text(json.dumps(raw, indent=2) + "\n")


def template_from_dict(raw: Mapping[str, Any]) -> ErgSessionTemplate:
    return ErgSessionTemplate(
        id=str(raw["id"]),
        name=str(raw.get("name") or raw["id"]),
        source=str(raw.get("source") or "curated"),
        tags=dict(raw.get("tags") or {}),
        rowing=dict(raw.get("rowing") or {}),
        approved_at=raw.get("approved_at"),
    )


def load_session_file(path: Path) -> ErgSessionTemplate:
    return template_from_dict(json.loads(path.read_text()))


def load_all_sessions(*, data_dir: Optional[Path] = None) -> List[ErgSessionTemplate]:
    ensure_curated_seed_files()
    root = data_dir or DATA_DIR
    sessions: List[ErgSessionTemplate] = []
    for sub in (root / "curated", root / "approved"):
        if not sub.is_dir():
            continue
        for path in sorted(sub.glob("*.json")):
            try:
                sessions.append(load_session_file(path))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
    return sessions


def _validation_plan_dict(template: ErgSessionTemplate, *, weekday: str = "Tuesday") -> Dict[str, Any]:
    dates = ["2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19", "2026-06-20", "2026-06-21"]
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    days: List[Dict[str, Any]] = []
    for wd, dt in zip(weekdays, dates):
        if wd == weekday:
            days.append(
                {
                    "weekday": wd,
                    "date": dt,
                    "session_type": "erg" if wd == "Tuesday" else "on_water",
                    "session_subtype": template.session_subtype,
                    "gym": None,
                    "rowing": template.rowing,
                    "notes": None,
                }
            )
        elif wd in ("Monday", "Wednesday"):
            days.append(
                {
                    "weekday": wd,
                    "date": dt,
                    "session_type": "rest",
                    "session_subtype": None,
                    "gym": None,
                    "rowing": None,
                    "notes": None,
                }
            )
        else:
            days.append(
                {
                    "weekday": wd,
                    "date": dt,
                    "session_type": "rest",
                    "session_subtype": None,
                    "gym": None,
                    "rowing": None,
                    "notes": None,
                }
            )
    return {
        "version": 1,
        "personalised": False,
        "greeting": None,
        "days": days,
    }


def validate_session_template(template: ErgSessionTemplate) -> Optional[str]:
    if template.total_minutes > SESSION_CAP_MINUTES:
        return f"session ~{template.total_minutes} min exceeds {SESSION_CAP_MINUTES} min cap"
    session = template.rowing_session()
    if session is None:
        return "unparseable rowing session"
    for seg in session.segments:
        if seg.phase in ("warm_up", "cool_down"):
            from weekly_plan_schema import _estimate_segment_minutes

            mins = _estimate_segment_minutes(seg.duration)
            if mins > WARMUP_COOLDOWN_CAP_MINUTES:
                return f"{seg.phase} ~{mins} min exceeds {WARMUP_COOLDOWN_CAP_MINUTES} min cap"
    plan = parse_weekly_plan(_validation_plan_dict(template))
    if plan is None:
        return "plan parse failed"
    return validate_plan_session_constraints(plan)


def _matches_phase(template: ErgSessionTemplate, phase: str) -> bool:
    phases = [str(p).lower() for p in template.tags.get("phase") or []]
    return not phases or phase.lower() in phases


def _matches_day_role(template: ErgSessionTemplate, day_role: str) -> bool:
    roles = [str(r).lower() for r in template.tags.get("day_role") or []]
    return not roles or day_role.lower() in roles


def _subtype_rank(template: ErgSessionTemplate, preferences: Sequence[str]) -> int:
    subtype = template.session_subtype.lower()
    try:
        return preferences.index(subtype)
    except ValueError:
        return len(preferences) + 1


def _pick_session(
    candidates: Sequence[ErgSessionTemplate],
    *,
    preferences: Sequence[str],
    exclude_ids: Sequence[str],
) -> Optional[ErgSessionTemplate]:
    filtered = [
        t
        for t in candidates
        if t.id not in exclude_ids and validate_session_template(t) is None
    ]
    if not filtered:
        return None
    ranked = sorted(
        filtered,
        key=lambda t: (_subtype_rank(t, preferences), t.total_minutes, t.id),
    )
    return ranked[0]


def select_sessions_for_week(
    *,
    phase: Optional[str] = None,
    recent_session_ids: Optional[Sequence[str]] = None,
    data_dir: Optional[Path] = None,
) -> Dict[str, ErgSessionTemplate]:
    phase_key = (phase or "base").strip().lower()
    recent = set(recent_session_ids or [])
    all_sessions = load_all_sessions(data_dir=data_dir)
    eligible = [
        t for t in all_sessions if _matches_phase(t, phase_key) and validate_session_template(t) is None
    ]
    if not eligible:
        ensure_curated_seed_files()
        eligible = load_all_sessions(data_dir=data_dir)

    tuesday = _pick_session(
        [t for t in eligible if _matches_day_role(t, DAY_ROLE_TUESDAY)],
        preferences=_TUESDAY_SUBTYPE_PREF.get(phase_key, _TUESDAY_SUBTYPE_PREF["base"]),
        exclude_ids=recent,
    )
    thursday_exclude = set(recent) | ({tuesday.id} if tuesday else set())
    thursday = _pick_session(
        [t for t in eligible if _matches_day_role(t, DAY_ROLE_THURSDAY)],
        preferences=_THURSDAY_SUBTYPE_PREF.get(phase_key, _THURSDAY_SUBTYPE_PREF["base"]),
        exclude_ids=thursday_exclude,
    )
    if tuesday is None:
        tuesday = _pick_session(eligible, preferences=["steady-state"], exclude_ids=recent)
    if thursday is None:
        thursday = _pick_session(
            eligible,
            preferences=["steady-state", "intervals"],
            exclude_ids=thursday_exclude,
        )
    if tuesday is None or thursday is None:
        fallback = eligible[0] if eligible else template_from_dict(CURATED_SEED[0])
        tuesday = tuesday or fallback
        thursday = thursday or fallback
    return {"tuesday": tuesday, "thursday": thursday}


def recent_session_ids_from_plan(plan_json: Optional[Mapping[str, Any]]) -> List[str]:
    if not plan_json:
        return []
    lib = plan_json.get("session_library")
    if not isinstance(lib, dict):
        return []
    out: List[str] = []
    for key in ("tuesday", "thursday"):
        val = lib.get(key)
        if val:
            out.append(str(val))
    return out


def main_structure_key(segments: Sequence[RowingSegment]) -> str:
    parts: List[str] = []
    for seg in segments:
        if seg.phase in ("main_set", "work", "build"):
            parts.append(f"{seg.phase}:{(seg.duration or '').strip().lower()}")
    return "|".join(parts) or "none"


def format_session_library_prompt(selections: Mapping[str, ErgSessionTemplate]) -> str:
    lines = [
        "\n\nSESSION LIBRARY (mandatory — use these exact session structures):\n",
        "Personalize splits, HR bpm, and zone labels only; do NOT change rep×duration/rest structure.\n",
    ]
    for day_name, template in selections.items():
        weekday = "Tuesday" if day_name == "tuesday" else "Thursday"
        lines.append(f"- {weekday} MUST use library session `{template.id}` ({template.name}).")
        lines.append(f"  session_subtype: {template.session_subtype}")
        for seg in template.rowing.get("segments") or []:
            lines.append(
                f"  - {seg.get('phase')}: {seg.get('label')} — {seg.get('duration')} "
                f"({seg.get('zone_z')}/{seg.get('zone_t')})"
            )
    return "\n".join(lines) + "\n"


def _merge_personalized_segments(
    template_segments: List[Dict[str, Any]],
    existing_segments: Sequence[RowingSegment],
) -> List[Dict[str, Any]]:
    by_phase: Dict[str, RowingSegment] = {}
    for seg in existing_segments:
        if seg.phase not in by_phase:
            by_phase[seg.phase] = seg
    merged: List[Dict[str, Any]] = []
    for raw in template_segments:
        phase = str(raw.get("phase") or "")
        out = dict(raw)
        existing = by_phase.get(phase)
        if existing is not None:
            for field in (
                "split_min",
                "split_max",
                "hr_bpm_min",
                "hr_bpm_max",
                "zone_z",
                "zone_t",
                "priority",
            ):
                val = getattr(existing, field)
                if val is not None and val != "":
                    out[field] = val
        merged.append(out)
    return merged


def apply_library_sessions_to_plan(
    plan_json: Dict[str, Any],
    selections: Mapping[str, ErgSessionTemplate],
) -> Dict[str, Any]:
    out = json.loads(json.dumps(plan_json))
    day_map = {"tuesday": "Tuesday", "thursday": "Thursday"}
    for key, weekday in day_map.items():
        template = selections.get(key)
        if template is None:
            continue
        for day in out.get("days") or []:
            if day.get("weekday") != weekday:
                continue
            if day.get("session_type") not in ("erg", "on_water"):
                continue
            rowing = day.get("rowing")
            if not isinstance(rowing, dict):
                continue
            existing = None
            parsed_day = parse_weekly_plan(out)
            if parsed_day:
                d = next((d for d in parsed_day.days if d.weekday == weekday), None)
                if d and d.rowing:
                    existing = d.rowing.segments
            template_segments = list(template.rowing.get("segments") or [])
            rowing["segments"] = _merge_personalized_segments(
                template_segments,
                existing or [],
            )
            day["session_subtype"] = template.session_subtype
    out["session_library"] = {k: v.id for k, v in selections.items()}
    return out


def promote_erg_score_to_library(
    *,
    cache_dir: Path,
    athlete_id: int,
    score_id: str,
    session_id: Optional[str] = None,
    name: Optional[str] = None,
    day_roles: Optional[Sequence[str]] = None,
    phases: Optional[Sequence[str]] = None,
    data_dir: Optional[Path] = None,
) -> Path:
    from generate_training_plan import find_erg_score_by_id

    record = find_erg_score_by_id(cache_dir, athlete_id, score_id)
    if record is None:
        raise FileNotFoundError(f"erg score {score_id} not found for athlete {athlete_id}")

    parts = record.get("parts") or record.get("session_parts") or []
    if not parts:
        raise ValueError("erg score has no session parts to promote")

    segments: List[Dict[str, Any]] = []
    phase_map = {
        "warmup": "warm_up",
        "warm_up": "warm_up",
        "main": "main_set",
        "main_set": "main_set",
        "cooldown": "cool_down",
        "cool_down": "cool_down",
    }
    for part in parts:
        role = str(part.get("role") or part.get("phase") or "main_set").lower()
        phase = phase_map.get(role, "main_set")
        dur_sec = part.get("duration_sec")
        duration = part.get("duration")
        if not duration and dur_sec:
            mins = max(1, int(round(float(dur_sec) / 60.0)))
            duration = f"{mins} min"
        duration = duration or "10 min"
        segments.append(
            _seg(
                phase,
                str(part.get("label") or phase.replace("_", " ").title()),
                str(duration),
                zone_z=str(part.get("zone_z") or "Z2"),
                zone_t=str(part.get("zone_t") or "T3"),
                split_min=str(part.get("split_min") or part.get("avg_split_500_fmt") or "2:10"),
                split_max=str(part.get("split_max") or part.get("avg_split_500_fmt") or "2:15"),
                hr_min=int(part.get("hr_bpm_min") or part.get("avg_hr") or 130),
                hr_max=int(part.get("hr_bpm_max") or part.get("avg_hr") or 150),
                priority=str(part.get("priority") or "hr"),
            )
        )

    sid = session_id or f"approved-{score_id}"
    sid = re.sub(r"[^a-zA-Z0-9_-]+", "-", sid).strip("-").lower()
    rowing = {"segments": segments, "erg_alternative": None}
    parsed = _parse_rowing_session(rowing)
    total = estimate_rowing_session_minutes(parsed) if parsed else 0
    subtype = str(record.get("session_subtype") or "intervals")
    template = ErgSessionTemplate(
        id=sid,
        name=name or f"Approved session {score_id}",
        source="approved_log",
        tags={
            "phase": list(phases or ["base", "build"]),
            "zones": [],
            "session_subtype": subtype,
            "day_role": list(day_roles or [DAY_ROLE_TUESDAY, DAY_ROLE_THURSDAY]),
            "total_minutes": total,
        },
        rowing=rowing,
        approved_at=datetime.now(timezone.utc).isoformat(),
    )
    err = validate_session_template(template)
    if err:
        raise ValueError(f"promoted session failed validation: {err}")

    root = data_dir or DATA_DIR
    APPROVED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = root / "approved" / f"{sid}.json"
    out_path.write_text(
        json.dumps(
            {
                "id": template.id,
                "name": template.name,
                "source": template.source,
                "approved_at": template.approved_at,
                "tags": template.tags,
                "rowing": template.rowing,
            },
            indent=2,
        )
        + "\n"
    )
    return out_path


def cmd_list(_: argparse.Namespace) -> int:
    ensure_curated_seed_files()
    for template in load_all_sessions():
        err = validate_session_template(template)
        status = "ok" if err is None else f"INVALID: {err}"
        print(f"{template.id}\t{template.source}\t{template.session_subtype}\t{template.total_minutes} min\t{status}")
    return 0


def cmd_validate(_: argparse.Namespace) -> int:
    ensure_curated_seed_files()
    failed = 0
    for template in load_all_sessions():
        err = validate_session_template(template)
        if err:
            failed += 1
            print(f"FAIL {template.id}: {err}")
        else:
            print(f"OK   {template.id}")
    return 1 if failed else 0


def cmd_promote(args: argparse.Namespace) -> int:
    path = promote_erg_score_to_library(
        cache_dir=Path(args.cache_dir),
        athlete_id=int(args.athlete_id),
        score_id=str(args.score_id),
        session_id=args.session_id,
        name=args.name,
    )
    print(f"Promoted to {path}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Erg session library tools")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List library sessions").set_defaults(func=cmd_list)
    sub.add_parser("validate", help="Validate all library sessions").set_defaults(func=cmd_validate)
    promote = sub.add_parser("promote", help="Promote erg score to approved library")
    promote.add_argument("--score-id", required=True)
    promote.add_argument("--athlete-id", type=int, required=True)
    promote.add_argument("--cache-dir", required=True)
    promote.add_argument("--session-id", default=None)
    promote.add_argument("--name", default=None)
    promote.set_defaults(func=cmd_promote)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
