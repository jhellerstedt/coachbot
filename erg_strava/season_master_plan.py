"""Season master plan: macro targets, weekly planned metrics, and logged actuals."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from generate_training_plan import (
    DEFAULT_GYM_NAME_PATTERNS,
    DEFAULT_GYM_SPORT_TYPES,
    WeekBounds,
    _extract_json_object,
    _local_today,
    _monday_on_or_before,
    _parse_activity_start,
    activity_local_date,
    is_gym_activity,
    plan_timezone_name,
    plan_week_bounds,
    strategic_goals_context,
    week_bounds_from_monday,
    week_contains,
)
from openrouter_client import call_openrouter

SEASON_PLAN_VERSION = 1


@dataclass(frozen=True)
class RaceTarget:
    name: str
    date: date
    description: str = ""


@dataclass(frozen=True)
class SeasonConfig:
    races: Tuple[RaceTarget, ...]
    start_date: Optional[date]
    hr_z2_max: int = 145
    hr_z5_min: int = 175


@dataclass
class WeekVolumeMetrics:
    rowing_meters: Optional[int] = None
    rowing_minutes: Optional[int] = None
    z2_percent: Optional[float] = None
    z5_percent: Optional[float] = None
    gym_tonnage_kg: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rowing_meters": self.rowing_meters,
            "rowing_minutes": self.rowing_minutes,
            "z2_percent": self.z2_percent,
            "z5_percent": self.z5_percent,
            "gym_tonnage_kg": self.gym_tonnage_kg,
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "WeekVolumeMetrics":
        if not data:
            return cls()
        return cls(
            rowing_meters=_optional_int(data.get("rowing_meters")),
            rowing_minutes=_optional_int(data.get("rowing_minutes")),
            z2_percent=_optional_float(data.get("z2_percent")),
            z5_percent=_optional_float(data.get("z5_percent")),
            gym_tonnage_kg=_optional_float(data.get("gym_tonnage_kg")),
        )


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iso_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def load_season_config(raw: Optional[Mapping[str, Any]], ref: Optional[date] = None) -> SeasonConfig:
    """Parse season block from config YAML; use defaults when omitted."""
    ref = ref or _local_today()
    block = (raw or {}).get("season") or {}
    hr = block.get("hr_zones") or {}
    hr_z2_max = int(hr.get("z2_max", 145))
    hr_z5_min = int(hr.get("z5_min", 175))
    start_date = _parse_iso_date(block.get("start_date"))

    races_raw = block.get("races")
    races: List[RaceTarget] = []
    if races_raw:
        for item in races_raw:
            if not isinstance(item, dict):
                continue
            race_date = _parse_iso_date(item.get("date"))
            name = str(item.get("name", "")).strip()
            if not name or race_date is None:
                continue
            races.append(
                RaceTarget(
                    name=name,
                    date=race_date,
                    description=str(item.get("description", "")).strip(),
                )
            )
    if not races:
        year = ref.year
        hoty = date(year, 11, 30)
        if hoty < ref:
            hoty = date(year + 1, 11, 30)
        vic = date(year, 3, 15)
        if vic < ref:
            vic = date(year + 1, 3, 15)
        races = [
            RaceTarget(
                name="Victoria State Championships",
                date=vic,
                description="club 4x− over 2 km — target crew time 6:40",
            ),
            RaceTarget(
                name="Head of the Yarra",
                date=hoty,
                description="8.5 km eights race — aerobic endurance and race pace",
            ),
        ]
    races = sorted(races, key=lambda r: r.date)
    return SeasonConfig(
        races=tuple(races),
        start_date=start_date,
        hr_z2_max=hr_z2_max,
        hr_z5_min=hr_z5_min,
    )


def season_bounds(config: SeasonConfig, ref: Optional[date] = None) -> Tuple[date, date]:
    ref = ref or _local_today()
    start = config.start_date or plan_week_bounds().week_start
    end = max(r.date for r in config.races)
    if end < start:
        end = start + timedelta(days=7 * 12)
    return start, end


def iter_season_weeks(config: SeasonConfig, ref: Optional[date] = None) -> List[WeekBounds]:
    start, end = season_bounds(config, ref)
    monday = _monday_on_or_before(start)
    weeks: List[WeekBounds] = []
    while monday <= end:
        weeks.append(week_bounds_from_monday(monday))
        monday += timedelta(days=7)
    return weeks


def season_master_plan_json_path(cache_dir: Path) -> Path:
    return cache_dir / "season_master_plan.json"


def season_master_plan_md_path(cache_dir: Path) -> Path:
    return cache_dir / "season_master_plan.md"


def load_season_master_plan(cache_dir: Path) -> Optional[Dict[str, Any]]:
    path = season_master_plan_json_path(cache_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save_season_master_plan(cache_dir: Path, data: Dict[str, Any]) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = season_master_plan_json_path(cache_dir)
    path.write_text(json.dumps(data, indent=2))
    return path


def _is_rowing_activity(
    act: dict,
    erg_types: frozenset,
    require_trainer_for_rowing: bool,
) -> bool:
    st = act.get("sport_type") or act.get("type") or ""
    if st not in erg_types:
        return False
    if st == "Rowing" and require_trainer_for_rowing:
        return bool(act.get("trainer"))
    return True


def _metrics_from_llm_dict(data: Mapping[str, Any]) -> WeekVolumeMetrics:
    return WeekVolumeMetrics(
        rowing_meters=_optional_int(
            data.get("rowing_meters") or data.get("target_rowing_meters")
        ),
        rowing_minutes=_optional_int(
            data.get("rowing_minutes") or data.get("target_rowing_minutes")
        ),
        z2_percent=_optional_float(
            data.get("z2_percent") or data.get("target_z2_percent")
        ),
        z5_percent=_optional_float(
            data.get("z5_percent") or data.get("target_z5_percent")
        ),
        gym_tonnage_kg=_optional_float(
            data.get("gym_tonnage_kg") or data.get("target_gym_tonnage_kg")
        ),
    )


def generate_macro_season_plan(
    config: SeasonConfig,
    weeks: Sequence[WeekBounds],
    training_summary: str,
    token: str,
    *,
    include_lifting: bool = True,
) -> Dict[str, Any]:
    """LLM-generated macro targets and periodisation narrative for every season week."""
    start, end = season_bounds(config)
    race_lines = "\n".join(
        f"- {r.name} ({r.date.isoformat()}): {r.description or '—'}"
        for r in config.races
    )
    week_lines = "\n".join(
        f"- {w.week_id}: {w.week_start.isoformat()} to {w.week_end.isoformat()}"
        for w in weeks
    )
    lifting_note = (
        "Include Mon/Wed gym sessions with progressive weekly tonnage targets."
        if include_lifting
        else "No gym sessions this season (lifting disabled)."
    )
    system = (
        "You are an expert rowing coach and strength coach designing a full-season "
        "macro periodisation plan for squad review.\n\n"
        "Return ONLY a single JSON object (no markdown fences):\n"
        "{\n"
        '  "periodisation_overview": "2-4 paragraphs: blocks, rationale, taper/race weeks",\n'
        '  "weeks": [\n'
        "    {\n"
        '      "week_id": "YYYY-MM-DD_YYYY-MM-DD",\n'
        '      "phase": "base|build|peak|taper|race|recovery",\n'
        '      "target_rowing_meters": integer,\n'
        '      "target_rowing_minutes": integer,\n'
        '      "target_z2_percent": number (0-100, HR zone 2 share of erg/row time),\n'
        '      "target_z5_percent": number (0-100, HR zone 5 share of erg/row time),\n'
        '      "target_gym_tonnage_kg": integer or 0,\n'
        '      "execution_priority": "split" or "hr" — default governing target for Main '
        "Set erg/row segments when split and HR conflict (warm-up/cool-down still HR "
        'priority),\n'
        '      "notes": "one line for reviewers"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Provide exactly one entry per week_id listed in the user message.\n"
        "- Progress volume and intensity logically toward the target races.\n"
        "- target_z2_percent + target_z5_percent should not exceed 100; leave headroom "
        "for Z3–Z4.\n"
        "- execution_priority: use `hr` for base/recovery aerobic weeks; shift toward "
        "`split` in peak/taper/race-prep weeks where holding pace matters more than "
        "strict HR ceilings on work pieces.\n"
        "- Use realistic squad weekly totals (typical masters/club crew).\n"
        f"- {lifting_note}\n"
        f"{strategic_goals_context()}"
    )
    user = (
        f"Season window ({plan_timezone_name()}): {start.isoformat()} to {end.isoformat()}.\n\n"
        f"Target races:\n{race_lines}\n\n"
        f"Weeks to plan (use these week_id values exactly):\n{week_lines}\n\n"
        f"--- Recent squad erg training summary ---\n{training_summary}"
    )
    raw = call_openrouter(system=system, user=user, api_key=token, timeout=120)
    data = _extract_json_object(raw) or {}
    week_rows: Dict[str, Dict[str, Any]] = {}
    for w in weeks:
        week_rows[w.week_id] = {
            "week_start": w.week_start.isoformat(),
            "week_end": w.week_end.isoformat(),
            "phase": "",
            "execution_priority": None,
            "notes": "",
            "target": WeekVolumeMetrics().to_dict(),
            "planned": None,
            "actual": None,
        }
    for item in data.get("weeks") or []:
        if not isinstance(item, dict):
            continue
        week_id = str(item.get("week_id", "")).strip()
        if week_id not in week_rows:
            continue
        phase = str(item.get("phase", "")).strip()
        execution_priority = _normalize_execution_priority(item.get("execution_priority"))
        if execution_priority is None:
            execution_priority = _phase_execution_priority_default(phase)
        week_rows[week_id].update(
            {
                "phase": phase,
                "execution_priority": execution_priority,
                "notes": str(item.get("notes", "")).strip(),
                "target": _metrics_from_llm_dict(item).to_dict(),
            }
        )
    return {
        "version": SEASON_PLAN_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season_start": start.isoformat(),
        "season_end": end.isoformat(),
        "races": [
            {"name": r.name, "date": r.date.isoformat(), "description": r.description}
            for r in config.races
        ],
        "periodisation_overview": str(data.get("periodisation_overview", "")).strip(),
        "weeks": week_rows,
    }


def extract_planned_metrics_from_plan(
    plan_text: str,
    token: str,
    *,
    plan_json: Optional[Dict[str, Any]] = None,
) -> WeekVolumeMetrics:
    """Parse a squad weekly plan into volume / intensity / gym totals."""
    if plan_json:
        from weekly_plan_schema import planned_metrics_from_plan_json

        metrics = planned_metrics_from_plan_json(plan_json)
        if metrics:
            return WeekVolumeMetrics.from_dict(metrics)
    return extract_planned_metrics_from_plan_text(plan_text, token)


def extract_planned_metrics_from_plan_text(plan_text: str, token: str) -> WeekVolumeMetrics:
    """Parse a squad weekly plan into volume / intensity / gym totals."""
    if not plan_text or not plan_text.strip():
        return WeekVolumeMetrics()
    system = (
        "You parse a rowing squad weekly training plan into weekly totals for review.\n\n"
        "Estimate totals for prescribed erg and on-water rowing sessions only "
        "(not gym work duration as rowing time).\n"
        "Map aerobic/steady-state/threshold sessions to Z2; VO2max/race-pace/high-rate "
        "intervals to Z5. Percentages are shares of total prescribed rowing time.\n\n"
        "Return ONLY JSON:\n"
        "{\n"
        '  "rowing_meters": integer,\n'
        '  "rowing_minutes": integer,\n'
        '  "z2_percent": number,\n'
        '  "z5_percent": number,\n'
        '  "gym_tonnage_kg": integer (sum of reps×kg for all prescribed gym sets)\n'
        "}"
    )
    user = f"--- Weekly plan ---\n{plan_text.strip()}"
    raw = call_openrouter(system=system, user=user, api_key=token, timeout=60)
    data = _extract_json_object(raw)
    if not data:
        return WeekVolumeMetrics()
    return _metrics_from_llm_dict(data)


def compute_actual_week_metrics(
    week: WeekBounds,
    week_activities: Sequence[dict],
    activity_details: Mapping[int, dict],
    activity_metrics: Mapping[int, Dict[str, Any]],
    erg_df: Optional[pd.DataFrame],
    *,
    erg_types: frozenset,
    require_trainer_for_rowing: bool,
    gym_types: frozenset,
    gym_name_patterns: Sequence[str],
    hr_z2_max: int,
    hr_z5_min: int,
    activity_athlete_ids: Optional[Mapping[int, int]] = None,
    athlete_profiles: Optional[Mapping[int, Any]] = None,
) -> WeekVolumeMetrics:
    """Aggregate logged Strava/Suunto sessions for one calendar week."""
    rowing_meters = 0
    rowing_minutes = 0
    z2_samples = 0
    z5_samples = 0
    hr_samples = 0
    gym_tonnage = 0.0

    for act in week_activities:
        start = _parse_activity_start(act.get("start_date"))
        if start is None or not week_contains(week, start):
            continue
        aid = int(act["id"])
        detail = activity_details.get(aid, {})
        metrics = activity_metrics.get(aid, {})

        if _is_rowing_activity(act, erg_types, require_trainer_for_rowing):
            distance = act.get("distance") or detail.get("distance") or 0
            if (act.get("source") == "suunto" or act.get("suunto_key")) and act.get(
                "distance"
            ):
                distance = act.get("distance")
            rowing = metrics.get("rowing") or {}
            if rowing.get("distance_m"):
                distance = rowing["distance_m"]
            try:
                rowing_meters += int(round(float(distance)))
            except (TypeError, ValueError):
                pass
            secs = act.get("moving_time") or act.get("elapsed_time")
            if secs is None:
                secs = detail.get("moving_time") or detail.get("elapsed_time")
            try:
                rowing_minutes += int(round(float(secs) / 60.0))
            except (TypeError, ValueError):
                pass
            if erg_df is not None and not erg_df.empty and "activity_id" in erg_df.columns:
                sub = erg_df.loc[erg_df["activity_id"] == aid]
                if not sub.empty and "hr" in sub.columns:
                    hr = sub["hr"].astype(float)
                    hr = hr[hr.notna()]
                    profile = None
                    if activity_athlete_ids and athlete_profiles:
                        athlete_id = activity_athlete_ids.get(aid)
                        if athlete_id is not None:
                            profile = athlete_profiles.get(athlete_id)
                    if len(hr):
                        for value in hr:
                            zone = None
                            if profile is not None and hasattr(profile, "classify_hr"):
                                zone = profile.classify_hr(float(value))
                            elif float(value) <= hr_z2_max:
                                zone = "z2"
                            elif float(value) >= hr_z5_min:
                                zone = "z5"
                            if zone == "z2":
                                z2_samples += 1
                            elif zone == "z5":
                                z5_samples += 1
                            hr_samples += 1

        if is_gym_activity(act, gym_types, gym_name_patterns):
            gym = metrics.get("gym") or {}
            try:
                gym_tonnage += float(gym.get("total_tonnage_kg", 0))
            except (TypeError, ValueError):
                pass

    z2_percent = round(z2_samples / hr_samples * 100.0, 1) if hr_samples else None
    z5_percent = round(z5_samples / hr_samples * 100.0, 1) if hr_samples else None
    return WeekVolumeMetrics(
        rowing_meters=rowing_meters or None,
        rowing_minutes=rowing_minutes or None,
        z2_percent=z2_percent,
        z5_percent=z5_percent,
        gym_tonnage_kg=round(gym_tonnage, 0) if gym_tonnage else None,
    )


def _fmt_km(meters: Optional[int]) -> str:
    if meters is None:
        return "—"
    return f"{meters / 1000:.1f}"


def _fmt_min(minutes: Optional[int]) -> str:
    if minutes is None:
        return "—"
    return str(minutes)


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.0f}%"


def _fmt_kg(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.0f}"


def _normalize_execution_priority(value: Any) -> Optional[str]:
    """Return ``split`` or ``hr`` when recognised; otherwise ``None``."""
    if value is None:
        return None
    raw = str(value).strip().lower()
    if raw in ("split", "pace", "split priority", "priority: split"):
        return "split"
    if raw in ("hr", "heart rate", "hr priority", "priority: hr", "heart-rate"):
        return "hr"
    return None


def _fmt_execution_priority(value: Any) -> str:
    normalized = _normalize_execution_priority(value)
    if normalized == "split":
        return "split"
    if normalized == "hr":
        return "hr"
    return "—"


_PHASE_EXECUTION_PRIORITY: Dict[str, str] = {
    "base": "hr",
    "deload": "hr",
    "recovery": "hr",
    "build": "hr",
    "peak": "split",
    "taper": "split",
    "race": "split",
}


def _phase_execution_priority_default(phase: str) -> Optional[str]:
    return _PHASE_EXECUTION_PRIORITY.get(phase.strip().lower())


def _weekly_table_column_offsets(header_cells: Sequence[str]) -> Tuple[int, int, int, int, bool]:
    """
    Return (target_offset, planned_offset, actual_offset, priority_col, has_priority_col).

    Supports legacy tables without a priority column and new tables with
    ``Tgt priority`` after ``Phase``.
    """
    lowered = [c.strip().lower() for c in header_cells]
    has_priority = any("priority" in cell for cell in lowered[2:4])
    if has_priority:
        return 3, 8, 13, 2, True
    return 2, 7, 12, -1, False


def _parse_weekly_progression_table(table_section: str) -> Dict[str, Dict[str, Any]]:
    weeks: Dict[str, Dict[str, Any]] = {}
    target_offset = 2
    planned_offset = 7
    actual_offset = 12
    priority_col = -1
    min_planned_cells = 12
    min_actual_cells = 17

    for line in table_section.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells:
            continue
        if cells[0].lower() == "week":
            target_offset, planned_offset, actual_offset, priority_col, has_priority = (
                _weekly_table_column_offsets(cells)
            )
            if has_priority:
                min_planned_cells = 13
                min_actual_cells = 18
            continue
        week_start = _parse_iso_date(cells[0])
        if week_start is None:
            continue
        week_id = _week_id_from_start(week_start)
        bounds = week_bounds_from_monday(week_start)
        phase = cells[1] if len(cells) > 1 and not _empty_cell(cells[1]) else ""
        execution_priority = None
        if priority_col >= 0 and len(cells) > priority_col:
            execution_priority = _normalize_execution_priority(cells[priority_col])
        if execution_priority is None and phase:
            execution_priority = _phase_execution_priority_default(phase)
        target = _metrics_from_table_cells(cells, target_offset)
        planned = (
            _metrics_from_table_cells(cells, planned_offset)
            if len(cells) >= min_planned_cells
            else WeekVolumeMetrics()
        )
        actual = (
            _metrics_from_table_cells(cells, actual_offset)
            if len(cells) >= min_actual_cells
            else WeekVolumeMetrics()
        )
        weeks[week_id] = {
            "week_start": bounds.week_start.isoformat(),
            "week_end": bounds.week_end.isoformat(),
            "phase": phase,
            "execution_priority": execution_priority,
            "notes": "",
            "target": target.to_dict(),
            "planned": planned.to_dict() if _metrics_has_values(planned) else None,
            "actual": actual.to_dict() if _metrics_has_values(actual) else None,
        }
    return weeks


def _empty_cell(value: str) -> bool:
    return value.strip() in ("", "—", "-", "?")


def _parse_table_km(cell: str) -> Optional[int]:
    if _empty_cell(cell):
        return None
    try:
        return int(round(float(cell.strip()) * 1000))
    except ValueError:
        return None


def _parse_table_minutes(cell: str) -> Optional[int]:
    if _empty_cell(cell):
        return None
    try:
        return int(round(float(cell.strip())))
    except ValueError:
        return None


def _parse_table_percent(cell: str) -> Optional[float]:
    if _empty_cell(cell):
        return None
    raw = cell.strip().rstrip("%")
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_table_kg(cell: str) -> Optional[float]:
    if _empty_cell(cell):
        return None
    try:
        return float(cell.strip())
    except ValueError:
        return None


def _metrics_from_table_cells(cells: Sequence[str], offset: int) -> WeekVolumeMetrics:
    if len(cells) < offset + 5:
        return WeekVolumeMetrics()
    return WeekVolumeMetrics(
        rowing_meters=_parse_table_km(cells[offset]),
        rowing_minutes=_parse_table_minutes(cells[offset + 1]),
        z2_percent=_parse_table_percent(cells[offset + 2]),
        z5_percent=_parse_table_percent(cells[offset + 3]),
        gym_tonnage_kg=_parse_table_kg(cells[offset + 4]),
    )


def _week_id_from_start(week_start: date) -> str:
    return week_bounds_from_monday(week_start).week_id


DELOAD_TONNAGE_FACTOR = 0.50
DELOAD_REFERENCE_WEEKS = 3
_NON_LOAD_PHASES = frozenset({"deload", "recovery", "taper", "race"})


@dataclass(frozen=True)
class DeloadGymTargetComputation:
    target_kg: float
    source: str  # "actual" | "planned fallback"
    reference_week_ids: Tuple[str, ...]
    reference_values: Tuple[float, ...]
    raw_mean_kg: float


def _week_row_sort_key(week_id: str, row: Mapping[str, Any]) -> date:
    parsed = _parse_iso_date(str(row.get("week_start") or week_id[:10]))
    return parsed or date.min


def _reference_gym_kg_for_week(row: Mapping[str, Any]) -> Tuple[Optional[float], bool]:
    """Return (kg, from_actual) for one reference week."""
    actual = WeekVolumeMetrics.from_dict(row.get("actual")).gym_tonnage_kg
    if actual is not None and actual > 0:
        return float(actual), True
    target = WeekVolumeMetrics.from_dict(row.get("target")).gym_tonnage_kg
    if target is not None and target > 0:
        return float(target), False
    return None, False


def _resolve_season_week_key(
    weeks: Mapping[str, Mapping[str, Any]],
    week_ref: str,
) -> Optional[str]:
    """Resolve ``week_ref`` (week_start or week_id) to a key in ``weeks``."""
    if week_ref in weeks:
        return week_ref
    prefix = week_ref[:10]
    for week_id, row in weeks.items():
        if week_id.startswith(prefix):
            return week_id
        if str(row.get("week_start") or "")[:10] == prefix:
            return week_id
    return None


def compute_deload_gym_target(
    weeks: Mapping[str, Mapping[str, Any]],
    deload_week_ref: str,
    *,
    n_reference: int = DELOAD_REFERENCE_WEEKS,
) -> Optional[DeloadGymTargetComputation]:
    """
    Deload gym tonnage = mean(recent load-week gym kg) × 0.50.

    Reference weeks: the ``n_reference`` most recent non-deload weeks before
    ``deload_week_ref``. Prefer ``Act gym kg``; per-week fallback to ``Tgt gym kg``.
    Source is ``actual`` only when every reference week used Act gym kg; otherwise
    ``planned fallback``.
    """
    deload_week_id = _resolve_season_week_key(weeks, deload_week_ref)
    if deload_week_id is None:
        return None
    deload_row = weeks.get(deload_week_id)
    if not deload_row:
        return None
    if str(deload_row.get("phase") or "").strip().lower() != "deload":
        return None

    sorted_weeks = sorted(
        weeks.items(),
        key=lambda item: _week_row_sort_key(item[0], item[1]),
    )
    prior_load_weeks: List[Tuple[str, Mapping[str, Any]]] = []
    for week_id, row in sorted_weeks:
        if week_id == deload_week_id:
            break
        phase = str(row.get("phase") or "").strip().lower()
        if phase and phase not in _NON_LOAD_PHASES:
            prior_load_weeks.append((week_id, row))

    reference = prior_load_weeks[-n_reference:]
    if not reference:
        return None

    values: List[float] = []
    week_ids: List[str] = []
    all_actual = True
    for week_id, row in reference:
        kg, from_actual = _reference_gym_kg_for_week(row)
        if kg is None:
            continue
        values.append(kg)
        week_ids.append(str(row.get("week_start") or week_id[:10]))
        if not from_actual:
            all_actual = False

    if not values:
        return None

    raw_mean = sum(values) / len(values)
    target_kg = round(raw_mean * DELOAD_TONNAGE_FACTOR)
    return DeloadGymTargetComputation(
        target_kg=float(target_kg),
        source="actual" if all_actual else "planned fallback",
        reference_week_ids=tuple(week_ids),
        reference_values=tuple(values),
        raw_mean_kg=raw_mean,
    )


def apply_deload_gym_target_to_week(
    weeks: Dict[str, Any],
    deload_week_ref: str,
) -> Optional[DeloadGymTargetComputation]:
    """Override deload week ``Tgt gym kg`` with computed value; mutates ``weeks``."""
    deload_week_id = _resolve_season_week_key(weeks, deload_week_ref)
    if deload_week_id is None:
        return None
    result = compute_deload_gym_target(weeks, deload_week_id)
    if result is None:
        return None
    row = weeks[deload_week_id]
    target = dict(row.get("target") or WeekVolumeMetrics().to_dict())
    target["gym_tonnage_kg"] = result.target_kg
    row["target"] = target
    row["deload_gym_target_source"] = result.source
    row["deload_gym_target_reference_weeks"] = list(result.reference_week_ids)
    return result


def apply_deload_gym_targets_to_season_data(
    data: Dict[str, Any],
) -> List[DeloadGymTargetComputation]:
    """Apply computed deload gym targets to all deload weeks in season data."""
    weeks: Dict[str, Any] = data.setdefault("weeks", {})
    results: List[DeloadGymTargetComputation] = []
    for week_id, row in weeks.items():
        if str(row.get("phase") or "").strip().lower() != "deload":
            continue
        applied = apply_deload_gym_target_to_week(weeks, week_id)
        if applied is not None:
            results.append(applied)
    return results


def format_deload_gym_target_log(
    deload_week_id: str,
    result: DeloadGymTargetComputation,
) -> str:
    refs = ", ".join(result.reference_week_ids)
    return (
        f"Deload gym target for {deload_week_id}: {result.target_kg:.0f} kg "
        f"(50% × mean {result.raw_mean_kg:.0f} kg from [{refs}]; "
        f"source: {result.source})"
    )


def ensure_deload_gym_target_for_week(
    cache_dir: Path,
    config: SeasonConfig,
    week_ref: str,
    *,
    persist: bool = True,
) -> Optional[str]:
    """
    Compute and apply deload gym target for one week before plan generation.

    Updates ``Tgt gym kg`` in season data (and markdown when ``persist``).
    Returns a log line describing the computation source.
    """
    data = load_season_plan_merged(cache_dir, config)
    weeks: Dict[str, Any] = data.get("weeks") or {}
    week_id = _resolve_season_week_key(weeks, week_ref)
    if week_id is None:
        return None
    row = weeks.get(week_id)
    if not row or str(row.get("phase") or "").strip().lower() != "deload":
        return None

    before = WeekVolumeMetrics.from_dict(row.get("target")).gym_tonnage_kg
    result = apply_deload_gym_target_to_week(weeks, week_id)
    if result is None:
        return None

    week_start = str(row.get("week_start") or week_ref[:10])
    log_line = format_deload_gym_target_log(week_start, result)
    if persist and before != result.target_kg:
        write_season_master_plan_md(cache_dir, data)
    return log_line


def _metrics_has_values(metrics: WeekVolumeMetrics) -> bool:
    return any(
        (
            metrics.rowing_meters,
            metrics.rowing_minutes,
            metrics.z2_percent is not None,
            metrics.z5_percent is not None,
            metrics.gym_tonnage_kg,
        )
    )


def _extract_md_section(text: str, heading: str, *, until: Optional[str] = None) -> str:
    pattern = rf"^## {re.escape(heading)}\s*$"
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        return ""
    start = match.end()
    if until:
        end_match = re.search(rf"^## {re.escape(until)}\s*$", text[start:], flags=re.MULTILINE)
        end = start + end_match.start() if end_match else len(text)
    else:
        end = len(text)
    return text[start:end].strip()


def parse_season_master_plan_md(text: str) -> Dict[str, Any]:
    """
    Parse season_master_plan.md into structured data.

    Macro columns (Tgt, phase, notes) are authoritative when edited by hand.
    Pln/Act columns are also parsed when present.
    """
    season_match = re.search(
        r"\*\*Season:\*\*\s*(\d{4}-\d{2}-\d{2})\s*[–-]\s*(\d{4}-\d{2}-\d{2})",
        text,
    )
    generated_match = re.search(
        r"\*\*Macro generated:\*\*\s*(\S+)",
        text,
    )
    races: List[Dict[str, str]] = []
    races_match = re.search(r"\*\*Races:\*\*\s*(.+)$", text, flags=re.MULTILINE)
    if races_match:
        for part in races_match.group(1).split(","):
            part = part.strip()
            m = re.match(r"(.+?)\s*\((\d{4}-\d{2}-\d{2})\)", part)
            if m:
                races.append({"name": m.group(1).strip(), "date": m.group(2)})

    periodisation_overview = _extract_md_section(
        text, "Periodisation overview", until="Weekly progression"
    )
    weeks: Dict[str, Dict[str, Any]] = {}
    table_section = _extract_md_section(text, "Weekly progression", until="Week notes")
    if not table_section:
        table_section = _extract_md_section(text, "Weekly progression", until="Legend")

    weeks = _parse_weekly_progression_table(table_section)

    notes_section = _extract_md_section(text, "Week notes", until="Legend")
    if notes_section:
        for line in notes_section.splitlines():
            m = re.match(
                r"^- \*\*(\d{4}-\d{2}-\d{2})\*\* \(([^)]*)\):\s*(.+)$",
                line.strip(),
            )
            if not m:
                continue
            note_start = _parse_iso_date(m.group(1))
            if note_start is None:
                continue
            week_id = _week_id_from_start(note_start)
            if week_id not in weeks:
                bounds = week_bounds_from_monday(note_start)
                weeks[week_id] = {
                    "week_start": bounds.week_start.isoformat(),
                    "week_end": bounds.week_end.isoformat(),
                    "phase": m.group(2).strip() if m.group(2).strip() != "—" else "",
                    "execution_priority": _phase_execution_priority_default(
                        m.group(2).strip()
                    ),
                    "notes": "",
                    "target": WeekVolumeMetrics().to_dict(),
                    "planned": None,
                    "actual": None,
                }
            weeks[week_id]["notes"] = m.group(3).strip()
            if m.group(2).strip() and m.group(2).strip() != "—":
                weeks[week_id]["phase"] = m.group(2).strip()

    return {
        "season_start": season_match.group(1) if season_match else None,
        "season_end": season_match.group(2) if season_match else None,
        "generated_at": generated_match.group(1) if generated_match else None,
        "races": races,
        "periodisation_overview": periodisation_overview,
        "weeks": weeks,
    }


def _empty_week_row(bounds: WeekBounds) -> Dict[str, Any]:
    return {
        "week_start": bounds.week_start.isoformat(),
        "week_end": bounds.week_end.isoformat(),
        "phase": "",
        "execution_priority": None,
        "notes": "",
        "target": WeekVolumeMetrics().to_dict(),
        "planned": None,
        "actual": None,
    }


def _merge_week_rows(
    md_row: Optional[Mapping[str, Any]],
    json_row: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    md_row = md_row or {}
    json_row = json_row or {}
    merged: Dict[str, Any] = {
        "week_start": md_row.get("week_start") or json_row.get("week_start"),
        "week_end": md_row.get("week_end") or json_row.get("week_end"),
        "phase": md_row.get("phase") if md_row.get("phase") else json_row.get("phase", ""),
        "execution_priority": (
            _normalize_execution_priority(md_row.get("execution_priority"))
            or _normalize_execution_priority(json_row.get("execution_priority"))
            or _phase_execution_priority_default(
                str(md_row.get("phase") or json_row.get("phase") or "")
            )
        ),
        "notes": md_row.get("notes") if md_row.get("notes") else json_row.get("notes", ""),
        "target": md_row.get("target") or json_row.get("target") or WeekVolumeMetrics().to_dict(),
    }
    for field in ("planned", "actual", "planned_updated_at", "actual_updated_at"):
        json_val = json_row.get(field)
        md_val = md_row.get(field)
        if json_val is not None:
            merged[field] = json_val
        elif md_val is not None:
            merged[field] = md_val
    return merged


def load_season_plan_merged(
    cache_dir: Path,
    config: SeasonConfig,
) -> Dict[str, Any]:
    """
    Merge season plan state: macro targets from markdown (if present), metrics
    history from JSON, with markdown taking precedence for Tgt/phase/notes.
    """
    json_data = load_season_master_plan(cache_dir) or {}
    md_path = season_master_plan_md_path(cache_dir)
    md_data: Dict[str, Any] = {}
    if md_path.is_file():
        try:
            md_data = parse_season_master_plan_md(md_path.read_text(encoding="utf-8"))
        except OSError:
            md_data = {}

    start, end = season_bounds(config)
    races = json_data.get("races") or md_data.get("races")
    if not races:
        races = [
            {"name": r.name, "date": r.date.isoformat(), "description": r.description}
            for r in config.races
        ]

    weeks: Dict[str, Any] = {}
    for bounds in iter_season_weeks(config):
        md_row = (md_data.get("weeks") or {}).get(bounds.week_id)
        json_row = (json_data.get("weeks") or {}).get(bounds.week_id)
        if md_row or json_row:
            weeks[bounds.week_id] = _merge_week_rows(md_row, json_row)
        else:
            weeks[bounds.week_id] = _empty_week_row(bounds)

    for week_id, md_row in (md_data.get("weeks") or {}).items():
        if week_id not in weeks:
            weeks[week_id] = _merge_week_rows(md_row, None)

    return {
        "version": json_data.get("version", SEASON_PLAN_VERSION),
        "generated_at": md_data.get("generated_at") or json_data.get("generated_at"),
        "season_start": md_data.get("season_start") or json_data.get("season_start") or start.isoformat(),
        "season_end": md_data.get("season_end") or json_data.get("season_end") or end.isoformat(),
        "races": races,
        "periodisation_overview": (
            md_data.get("periodisation_overview")
            or json_data.get("periodisation_overview")
            or ""
        ),
        "hr_z2_max": json_data.get("hr_z2_max", config.hr_z2_max),
        "hr_z5_min": json_data.get("hr_z5_min", config.hr_z5_min),
        "weeks": weeks,
    }


# Season phase → which training zones the Main Set should bias toward.
_PHASE_ZONE_GUIDANCE: Dict[str, str] = {
    "base": "Main Set in T2–T3 (UT2/UT1) with structured T4 threshold (Z4) on one rowing day; weekly Z5 cap is for brief VO2max touches only — not full interval sessions.",
    "deload": "Keep everything T1–T3; replace intensity with Z2 volume at HR priority.",
    "build": "Main Set mixes T3 with structured T4 threshold work; introduce T5 in caps.",
    "peak": "Race-pace focus: deliberate T4–T5 work within the weekly Z5 cap.",
    "taper": "Cut volume, keep short T5 race-pace touches; most work T1–T2.",
    "race": "Race-pace primers only (brief T5), everything else T1–T2 recovery.",
    "recovery": "Keep everything T1–T2; no T4–T5 intensity.",
}


def _phase_zone_guidance(phase: str) -> Optional[str]:
    return _PHASE_ZONE_GUIDANCE.get(phase.strip().lower())


def _execution_priority_context(priority: Optional[str]) -> str:
    if priority == "split":
        return (
            "WEEKLY EXECUTION PRIORITY: split — default Main Set erg/row segments to "
            "`priority: split` (hold target split; accept HR above range on work pieces "
            "rather than slowing). Warm-up, cool-down, and active recovery still use "
            "`priority: HR`."
        )
    if priority == "hr":
        return (
            "WEEKLY EXECUTION PRIORITY: HR — default Main Set erg/row segments to "
            "`priority: HR` (stay inside the HR bpm range; let split drift rather than "
            "breach HR limits). Use `priority: split` only for brief race-pace primers "
            "explicitly called out in notes."
        )
    return ""


def format_season_week_macro_context(
    week: WeekBounds,
    row: Mapping[str, Any],
) -> str:
    """Prompt block: this week's macro targets from the season master plan."""
    target = WeekVolumeMetrics.from_dict(row.get("target"))
    phase = str(row.get("phase") or "").strip()
    notes = str(row.get("notes") or "").strip()
    execution_priority = _normalize_execution_priority(row.get("execution_priority"))
    if execution_priority is None and phase:
        execution_priority = _phase_execution_priority_default(phase)
    lines = [
        f"--- Season master plan targets ({week.week_start.isoformat()} – "
        f"{week.week_end.isoformat()}) ---",
    ]
    if phase:
        lines.append(f"Phase: {phase}")
        guidance = _phase_zone_guidance(phase)
        if guidance:
            lines.append(f"Phase zone focus: {guidance}")
    priority_line = _execution_priority_context(execution_priority)
    if priority_line:
        lines.append(priority_line)
    parts: List[str] = []
    if target.rowing_meters is not None:
        parts.append(f"{target.rowing_meters / 1000:.1f} km rowing")
    if target.rowing_minutes is not None:
        parts.append(f"{target.rowing_minutes} min rowing")
    if target.z2_percent is not None:
        parts.append(f"Z2/T2 ~{target.z2_percent:.0f}% of erg/row time")
    if target.z5_percent is not None:
        parts.append(f"Z5/T5 ~{target.z5_percent:.0f}% of erg/row time")
    if target.gym_tonnage_kg is not None:
        deload_source = str(row.get("deload_gym_target_source") or "").strip()
        if phase.strip().lower() == "deload" and deload_source:
            parts.append(
                f"{target.gym_tonnage_kg:.0f} kg gym tonnage "
                f"(deload: 50% of recent load-week tonnage; source: {deload_source})"
            )
        else:
            parts.append(f"{target.gym_tonnage_kg:.0f} kg gym tonnage")
    if parts:
        lines.append("Macro targets: " + "; ".join(parts) + ".")
    if target.z5_percent is not None:
        if phase.strip().lower() == "base":
            lines.append(
                f"INTENSITY CAP: T5/Z5 (VO2max/race-pace) volume must not exceed "
                f"{target.z5_percent:.0f}% of total prescribed erg/row time this week. "
                "Base-phase quality work is T4 threshold (Z4) — not Z5 intervals. "
                "Use the cap only for optional brief VO2max touches."
            )
        else:
            lines.append(
                f"HARD INTENSITY CAP: T5/Z5 (VO2max/race-pace) volume must not exceed "
                f"{target.z5_percent:.0f}% of total prescribed erg/row time this week. "
                "Cross-reference every session's Main-Set zone against this cap before "
                "finalising the plan."
            )
    if notes:
        lines.append(f"Reviewer note: {notes}")
    if phase.strip().lower() == "deload":
        from gym_deload import DELOAD_GYM_PROTOCOL_PROMPT

        lines.append("")
        lines.append(DELOAD_GYM_PROTOCOL_PROMPT.strip())
    elif phase.strip().lower() in ("base", "build"):
        from gym_pyramid import GYM_PYRAMID_PROTOCOL_PROMPT

        lines.append("")
        lines.append(GYM_PYRAMID_PROTOCOL_PROMPT.strip())
    lines.append(
        "Prescribe this week's squad sessions to hit these macro targets unless "
        "adherence, injury, or queued athlete adjustments clearly require a deviation."
    )
    return "\n".join(lines)


def load_season_week_macro_context(
    cache_dir: Path,
    week: WeekBounds,
    config: SeasonConfig,
) -> Optional[str]:
    """Load macro targets for one plan week from season_master_plan.md."""
    data = load_season_plan_merged(cache_dir, config)
    if str((data.get("weeks") or {}).get(week.week_id, {}).get("phase") or "").strip().lower() == "deload":
        log = ensure_deload_gym_target_for_week(
            cache_dir, config, week.week_id, persist=True
        )
        if log:
            print(log, flush=True)
        data = load_season_plan_merged(cache_dir, config)

    row = (data.get("weeks") or {}).get(week.week_id)
    if not row:
        return None
    target = WeekVolumeMetrics.from_dict(row.get("target"))
    if not any(
        (
            target.rowing_meters,
            target.rowing_minutes,
            target.z2_percent,
            target.z5_percent,
            target.gym_tonnage_kg,
            row.get("phase"),
            row.get("notes"),
            _normalize_execution_priority(row.get("execution_priority")),
        )
    ):
        return None
    return format_season_week_macro_context(week, row)


def intensity_cap_status(
    target_z5_percent: Optional[float],
    planned_z5_percent: Optional[float],
    *,
    tolerance: float = 1.0,
) -> Optional[Tuple[bool, str]]:
    """
    Compare a generated plan's Z5/T5 share against the season target cap.

    Returns ``(within_cap, message)`` or ``None`` when either value is missing.
    """
    if target_z5_percent is None or planned_z5_percent is None:
        return None
    within = planned_z5_percent <= target_z5_percent + tolerance
    if within:
        msg = (
            f"OK — planned T5/Z5 {planned_z5_percent:.0f}% within cap "
            f"{target_z5_percent:.0f}% (+{tolerance:g} tol)."
        )
    else:
        msg = (
            f"OVER CAP — planned T5/Z5 {planned_z5_percent:.0f}% exceeds season cap "
            f"{target_z5_percent:.0f}%; reduce high-intensity volume."
        )
    return within, msg


def verify_week_intensity_against_target(
    cache_dir: Path,
    config: SeasonConfig,
    week: WeekBounds,
) -> Optional[str]:
    """Check this week's parsed plan intensity against the season Z5 cap."""
    data = load_season_plan_merged(cache_dir, config)
    row = (data.get("weeks") or {}).get(week.week_id)
    if not row:
        return None
    target = WeekVolumeMetrics.from_dict(row.get("target"))
    planned = WeekVolumeMetrics.from_dict(row.get("planned"))
    status = intensity_cap_status(target.z5_percent, planned.z5_percent)
    if status is None:
        return None
    return (
        f"Intensity cap check {week.week_start.isoformat()}–"
        f"{week.week_end.isoformat()}: {status[1]}"
    )


def _preserve_planned_actual(new_data: Dict[str, Any], old_data: Mapping[str, Any]) -> None:
    old_weeks = old_data.get("weeks") or {}
    new_weeks = new_data.setdefault("weeks", {})
    for week_id, old_row in old_weeks.items():
        if week_id not in new_weeks:
            continue
        for field in ("planned", "actual", "planned_updated_at", "actual_updated_at"):
            if old_row.get(field) is not None:
                new_weeks[week_id][field] = old_row[field]


def render_season_master_plan_md(data: Mapping[str, Any]) -> str:
    """Human-readable season document for expert review."""
    races = data.get("races") or []
    race_lines = ", ".join(
        f"{r.get('name')} ({str(r.get('date', ''))[:10]})" for r in races
    )
    lines = [
        "# Season Master Plan",
        "",
        "> Macro targets (LLM periodisation) vs weekly **Planned** (squad plan) vs "
        "**Actual** (logged Strava/Suunto). Review progression before adjusting "
        "weekly generation.",
        "",
        f"**Season:** {data.get('season_start', '?')} – {data.get('season_end', '?')} "
        f"({plan_timezone_name()})  ",
        f"**Macro generated:** {str(data.get('generated_at', '—'))[:19]}  ",
        f"**Races:** {race_lines or '—'}",
        "",
        "## Strategic goals",
        "",
        strategic_goals_context().strip(),
        "",
        "## Periodisation overview",
        "",
        (str(data.get("periodisation_overview", "")).strip() or "_(not generated)_"),
        "",
        "## Weekly progression",
        "",
        "| Week | Phase | Tgt priority | Tgt km | Tgt min | Tgt Z2 | Tgt Z5 | Tgt gym kg | "
        "Pln km | Pln min | Pln Z2 | Pln Z5 | Pln gym kg | "
        "Act km | Act min | Act Z2 | Act Z5 | Act gym kg |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | "
        "---: | ---: | ---: | ---: | ---: | "
        "---: | ---: | ---: | ---: | ---: |",
    ]
    weeks: Dict[str, Any] = data.get("weeks") or {}
    for week_id in sorted(weeks.keys()):
        row = weeks[week_id]
        target = WeekVolumeMetrics.from_dict(row.get("target"))
        planned = WeekVolumeMetrics.from_dict(row.get("planned"))
        actual = WeekVolumeMetrics.from_dict(row.get("actual"))
        week_start = str(row.get("week_start", week_id[:10]))
        execution_priority = (
            _normalize_execution_priority(row.get("execution_priority"))
            or _phase_execution_priority_default(str(row.get("phase") or ""))
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    week_start,
                    str(row.get("phase") or "—"),
                    _fmt_execution_priority(execution_priority),
                    _fmt_km(target.rowing_meters),
                    _fmt_min(target.rowing_minutes),
                    _fmt_pct(target.z2_percent),
                    _fmt_pct(target.z5_percent),
                    _fmt_kg(target.gym_tonnage_kg),
                    _fmt_km(planned.rowing_meters),
                    _fmt_min(planned.rowing_minutes),
                    _fmt_pct(planned.z2_percent),
                    _fmt_pct(planned.z5_percent),
                    _fmt_kg(planned.gym_tonnage_kg),
                    _fmt_km(actual.rowing_meters),
                    _fmt_min(actual.rowing_minutes),
                    _fmt_pct(actual.z2_percent),
                    _fmt_pct(actual.z5_percent),
                    _fmt_kg(actual.gym_tonnage_kg),
                ]
            )
            + " |"
        )
    notes_blocks: List[str] = []
    for week_id in sorted(weeks.keys()):
        row = weeks[week_id]
        note = str(row.get("notes", "")).strip()
        if note:
            notes_blocks.append(f"- **{row.get('week_start', week_id[:10])}** ({row.get('phase') or '—'}): {note}")
    if notes_blocks:
        lines.extend(["", "## Week notes", ""] + notes_blocks)
    lines.extend(
        [
            "",
            "## Legend",
            "",
            "- **Tgt**: macro season target — edit in this markdown file; weekly plans read these targets",
            "- **Tgt priority**: `split` or `hr` — weekly default for Main Set segment "
            "execution when split and HR conflict (warm-up/cool-down still HR priority)",
            "- **Pln**: parsed from generated squad weekly plan (`weekly_plans/*.json`)",
            "- **Act**: summed from logged erg/rowing and gym sessions",
            "- Regenerate macro targets with `strava_erg_hr_plot.py --refresh-season-plan`",
            f"- HR zones for Actual: Z2 ≤ {data.get('hr_z2_max', 145)} bpm, "
            f"Z5 ≥ {data.get('hr_z5_min', 175)} bpm (configurable in `config.yaml`)",
            "",
        ]
    )
    return "\n".join(lines)


def write_season_master_plan_md(cache_dir: Path, data: Mapping[str, Any]) -> Path:
    path = season_master_plan_md_path(cache_dir)
    path.write_text(render_season_master_plan_md(data))
    return path


def ensure_macro_season_plan(
    cache_dir: Path,
    config: SeasonConfig,
    training_summary: str,
    token: str,
    *,
    refresh: bool = False,
    include_lifting: bool = True,
) -> Dict[str, Any]:
    """
    Ensure season plan exists.

    ``refresh=True``: LLM-regenerate macro targets and rewrite markdown.
    Otherwise: load merged state from markdown + JSON without overwriting macro edits.
    """
    if refresh:
        old = load_season_plan_merged(cache_dir, config)
        weeks = iter_season_weeks(config)
        data = generate_macro_season_plan(
            config,
            weeks,
            training_summary,
            token,
            include_lifting=include_lifting,
        )
        _preserve_planned_actual(data, old)
        data["hr_z2_max"] = config.hr_z2_max
        data["hr_z5_min"] = config.hr_z5_min
        save_season_master_plan(cache_dir, data)
        write_season_master_plan_md(cache_dir, data)
        return data

    md_path = season_master_plan_md_path(cache_dir)
    if md_path.is_file() or load_season_master_plan(cache_dir):
        data = load_season_plan_merged(cache_dir, config)
        data["hr_z2_max"] = config.hr_z2_max
        data["hr_z5_min"] = config.hr_z5_min
        return data

    weeks = iter_season_weeks(config)
    start, end = season_bounds(config)
    data = {
        "version": SEASON_PLAN_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "season_start": start.isoformat(),
        "season_end": end.isoformat(),
        "races": [
            {"name": r.name, "date": r.date.isoformat(), "description": r.description}
            for r in config.races
        ],
        "periodisation_overview": "",
        "hr_z2_max": config.hr_z2_max,
        "hr_z5_min": config.hr_z5_min,
        "weeks": {w.week_id: _empty_week_row(w) for w in weeks},
    }
    save_season_master_plan(cache_dir, data)
    write_season_master_plan_md(cache_dir, data)
    return data


def update_season_master_plan_hybrid(
    cache_dir: Path,
    config: SeasonConfig,
    training_summary: str,
    token: str,
    *,
    target_week: WeekBounds,
    prev_week: WeekBounds,
    plan_text: str,
    week_activities: Sequence[dict],
    activity_details: Mapping[int, dict],
    activity_metrics: Mapping[int, Dict[str, Any]],
    erg_df: Optional[pd.DataFrame] = None,
    plan_json: Optional[Dict[str, Any]] = None,
    erg_types: Optional[frozenset] = None,
    require_trainer_for_rowing: bool = True,
    gym_types: Optional[frozenset] = None,
    gym_name_patterns: Optional[Sequence[str]] = None,
    athlete_profiles: Optional[Mapping[int, Any]] = None,
    activity_athlete_ids: Optional[Mapping[int, int]] = None,
    refresh_macro: bool = False,
    include_lifting: bool = True,
) -> Path:
    """
    Hybrid update: macro from markdown (or LLM when refresh_macro), set planned
    metrics for target week and actual for previous week, sync JSON + markdown.
    """
    erg_types = erg_types or frozenset({"VirtualRow", "Rowing"})
    gym_types = gym_types or DEFAULT_GYM_SPORT_TYPES
    gym_name_patterns = gym_name_patterns or DEFAULT_GYM_NAME_PATTERNS

    data = ensure_macro_season_plan(
        cache_dir,
        config,
        training_summary,
        token,
        refresh=refresh_macro,
        include_lifting=include_lifting,
    )
    if not refresh_macro:
        data = load_season_plan_merged(cache_dir, config)

    weeks: Dict[str, Any] = data.setdefault("weeks", {})
    if target_week.week_id not in weeks:
        weeks[target_week.week_id] = _empty_week_row(target_week)

    planned = extract_planned_metrics_from_plan(
        plan_text, token, plan_json=plan_json
    )
    weeks[target_week.week_id]["planned"] = planned.to_dict()
    weeks[target_week.week_id]["planned_updated_at"] = datetime.now(timezone.utc).isoformat()

    if prev_week.week_id not in weeks:
        weeks[prev_week.week_id] = _empty_week_row(prev_week)
    actual = compute_actual_week_metrics(
        prev_week,
        week_activities,
        activity_details,
        activity_metrics,
        erg_df,
        erg_types=erg_types,
        require_trainer_for_rowing=require_trainer_for_rowing,
        gym_types=gym_types,
        gym_name_patterns=gym_name_patterns,
        hr_z2_max=config.hr_z2_max,
        hr_z5_min=config.hr_z5_min,
        activity_athlete_ids=activity_athlete_ids,
        athlete_profiles=athlete_profiles,
    )
    weeks[prev_week.week_id]["actual"] = actual.to_dict()
    weeks[prev_week.week_id]["actual_updated_at"] = datetime.now(timezone.utc).isoformat()

    data["hr_z2_max"] = config.hr_z2_max
    data["hr_z5_min"] = config.hr_z5_min
    save_season_master_plan(cache_dir, data)
    return write_season_master_plan_md(cache_dir, data)
