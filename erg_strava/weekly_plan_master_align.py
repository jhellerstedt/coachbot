"""Validate and auto-correct weekly plans against season_master_plan.md targets."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from season_master_plan import (
    WeekVolumeMetrics,
    apply_deload_gym_targets_to_season_data,
    load_season_plan_merged,
    parse_season_master_plan_md,
    season_master_plan_md_path,
)
from gym_deload import (
    apply_deload_gym_session,
    gym_for_weekday,
    gym_session_tonnage,
)
from weekly_plan_schema import (
    DayPlan,
    ErgAlternative,
    GymExercise,
    GymSession,
    GymSet,
    RowingSegment,
    RowingSession,
    SESSION_CAP_MINUTES,
    WARMUP_COOLDOWN_CAP_MINUTES,
    WARMUP_COOLDOWN_DEFAULT_MINUTES,
    WARMUP_COOLDOWN_FLOOR_MINUTES,
    WeeklyPlan,
    _estimate_segment_minutes,
    _replace_trailing_simple_duration,
    _strip_rowing_phase_prefix,
    estimate_day_session_minutes,
    estimate_rowing_session_minutes,
    parse_weekly_plan,
    planned_metrics_from_plan_json,
    render_plan_text,
    weekly_plan_to_dict,
)

DEFAULT_MAX_HR = 183
T3_HR_CAP = int(DEFAULT_MAX_HR * 0.80)  # ≤80% MHR

_PHASE_ALIASES = {
    "raceprep": "peak",
    "race-prep": "peak",
    "race_prep": "peak",
}

_ON_WATER_PHASES = frozenset({"base", "build", "deload", "recovery"})
_LONG_Z2_MINUTES = 45


@dataclass
class WeeklyTarget:
    week: str
    phase: str
    tgt_priority: str
    tgt_z2_percent: Optional[float] = None
    tgt_z5_percent: Optional[float] = None
    tgt_km: Optional[float] = None
    tgt_min: Optional[int] = None
    tgt_gym_kg: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Violation:
    field: str
    severity: str  # critical | warning | info
    master_plan_target: str
    coach_bot_actual: str
    description: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    week: str
    phase: str
    violations: List[Violation] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "week": self.week,
            "phase": self.phase,
            "violations": [v.to_dict() for v in self.violations],
        }


@dataclass
class AlignmentResult:
    plan_json: Dict[str, Any]
    plan_markdown: str
    plan_text: str
    validation_before: ValidationResult
    validation_after: ValidationResult
    corrected: bool
    aligned: bool = False


def _normalize_phase(phase: str) -> str:
    raw = (phase or "").strip().lower()
    return _PHASE_ALIASES.get(raw, raw)


def _week_target_from_row(week_start: str, row: Mapping[str, Any]) -> WeeklyTarget:
    target = WeekVolumeMetrics.from_dict(row.get("target"))
    phase = str(row.get("phase") or "").strip()
    priority_raw = row.get("execution_priority")
    priority = str(priority_raw).strip().lower() if priority_raw else ""
    if priority not in ("hr", "split"):
        from season_master_plan import _phase_execution_priority_default

        priority = _phase_execution_priority_default(phase) or "hr"
    km = (
        round(target.rowing_meters / 1000.0, 1)
        if target.rowing_meters is not None
        else None
    )
    return WeeklyTarget(
        week=week_start,
        phase=phase,
        tgt_priority=priority,
        tgt_z2_percent=target.z2_percent,
        tgt_z5_percent=target.z5_percent,
        tgt_km=km,
        tgt_min=target.rowing_minutes,
        tgt_gym_kg=target.gym_tonnage_kg,
    )


def extract_weekly_targets(season_md_text: str) -> Dict[str, WeeklyTarget]:
    """Step 1: parse season_master_plan.md into ``weeklyTargets`` keyed by week start."""
    parsed = parse_season_master_plan_md(season_md_text)
    apply_deload_gym_targets_to_season_data(parsed)
    out: Dict[str, WeeklyTarget] = {}
    for _week_id, row in (parsed.get("weeks") or {}).items():
        week_start = str(row.get("week_start") or _week_id[:10])
        out[week_start] = _week_target_from_row(week_start, row)
    return out


def weekly_targets_to_json(season_md_text: str) -> Dict[str, Any]:
    targets = extract_weekly_targets(season_md_text)
    return {"weeklyTargets": {k: v.to_dict() for k, v in sorted(targets.items())}}


def load_weekly_targets(cache_dir: Path, config: Any) -> Dict[str, WeeklyTarget]:
    """Load weeklyTargets from cache (markdown authoritative for macro columns)."""
    merged = load_season_plan_merged(cache_dir, config)
    apply_deload_gym_targets_to_season_data(merged)
    out: Dict[str, WeeklyTarget] = {}
    for _week_id, row in (merged.get("weeks") or {}).items():
        week_start = str(row.get("week_start") or _week_id[:10])
        out[week_start] = _week_target_from_row(week_start, row)
    return out


def _resolve_target(
    week: str, weekly_targets: Mapping[str, WeeklyTarget]
) -> Optional[WeeklyTarget]:
    if week in weekly_targets:
        return weekly_targets[week]
    for key, target in weekly_targets.items():
        if week.startswith(key) or key.startswith(week[:10]):
            return target
    return None


def _zone_t_index(zone_t: str) -> int:
    m = re.match(r"^T(\d+)$", (zone_t or "").strip(), re.I)
    return int(m.group(1)) if m else 0


def _segment_minutes(seg: RowingSegment) -> int:
    return _estimate_segment_minutes(seg.duration)


def _session_hi_intensity_fraction(segments: Sequence[RowingSegment]) -> float:
    total = sum(_segment_minutes(s) for s in segments) or 1
    hi = sum(
        _segment_minutes(s)
        for s in segments
        if s.zone_z in ("Z4", "Z5") or _zone_t_index(s.zone_t) >= 4
    )
    return hi / total


def _session_z5_fraction(segments: Sequence[RowingSegment]) -> float:
    total = sum(_segment_minutes(s) for s in segments) or 1
    z5 = sum(_segment_minutes(s) for s in segments if s.zone_z == "Z5")
    return z5 / total


def _plan_rowing_days(plan: WeeklyPlan) -> List[DayPlan]:
    return [d for d in plan.days if d.session_type in ("erg", "on_water")]


def _plan_has_on_water(plan: WeeklyPlan) -> bool:
    return any(d.session_type == "on_water" for d in plan.days)


def _plan_has_long_z2(plan: WeeklyPlan) -> bool:
    for day in _plan_rowing_days(plan):
        if day.rowing is None:
            continue
        for seg in day.rowing.segments:
            if seg.zone_z in ("Z1", "Z2", "Z3") and _segment_minutes(seg) >= _LONG_Z2_MINUTES:
                return True
        alt = day.rowing.erg_alternative
        if alt:
            for seg in alt.segments:
                if seg.zone_z in ("Z1", "Z2", "Z3") and _segment_minutes(seg) >= _LONG_Z2_MINUTES:
                    return True
    return False


def _total_prescribed_minutes(plan: WeeklyPlan) -> int:
    return sum(estimate_day_session_minutes(d) for d in plan.days)


def validate_weekly_plan(
    week: str,
    coach_bot_plan: Union[WeeklyPlan, Mapping[str, Any]],
    weekly_targets: Mapping[str, WeeklyTarget],
) -> ValidationResult:
    """Step 2: flag CoachBot plan violations against master-plan targets."""
    target = _resolve_target(week, weekly_targets)
    phase = _normalize_phase(target.phase if target else "")
    result = ValidationResult(week=week, phase=target.phase if target else "")

    if target is None:
        result.violations.append(
            Violation(
                field="week",
                severity="warning",
                master_plan_target="known week",
                coach_bot_actual=week,
                description="No master-plan row found for this week.",
            )
        )
        return result

    if isinstance(coach_bot_plan, Mapping):
        parsed = parse_weekly_plan(coach_bot_plan)
        if parsed is None:
            result.violations.append(
                Violation(
                    field="plan",
                    severity="critical",
                    master_plan_target="valid weekly plan JSON",
                    coach_bot_actual="unparseable",
                    description="CoachBot plan could not be parsed.",
                )
            )
            return result
        plan = parsed
    else:
        plan = coach_bot_plan

    metrics = planned_metrics_from_plan_json(plan)

    if phase == "deload":
        for day in _plan_rowing_days(plan):
            if day.rowing is None:
                continue
            for label, segments in (
                ("primary", day.rowing.segments),
                (
                    "erg alternative",
                    day.rowing.erg_alternative.segments
                    if day.rowing.erg_alternative
                    else [],
                ),
            ):
                if not segments:
                    continue
                frac = _session_hi_intensity_fraction(segments)
                if frac > 0.10:
                    result.violations.append(
                        Violation(
                            field="zone_distribution",
                            severity="critical",
                            master_plan_target=f"≤10% Z4/Z5 per session ({phase})",
                            coach_bot_actual=f"{frac * 100:.0f}% on {day.weekday} ({label})",
                            description=(
                                f"{day.weekday} {label} prescribes high-intensity work "
                                f"exceeding 10% of session time during {phase} phase."
                            ),
                        )
                    )

    if phase == "base":
        for day in _plan_rowing_days(plan):
            if day.rowing is None:
                continue
            for label, segments in (
                ("primary", day.rowing.segments),
                (
                    "erg alternative",
                    day.rowing.erg_alternative.segments
                    if day.rowing.erg_alternative
                    else [],
                ),
            ):
                if not segments:
                    continue
                z5_frac = _session_z5_fraction(segments)
                if z5_frac > 0.12:
                    result.violations.append(
                        Violation(
                            field="zone_distribution",
                            severity="warning",
                            master_plan_target="≤12% Z5 per session (base; weekly cap applies)",
                            coach_bot_actual=f"{z5_frac * 100:.0f}% Z5 on {day.weekday} ({label})",
                            description=(
                                f"{day.weekday} {label} Z5 volume exceeds base-phase "
                                "per-session limit — use T4 threshold (Z4) instead."
                            ),
                        )
                    )

    if target.tgt_priority == "hr":
        for day in _plan_rowing_days(plan):
            if day.rowing is None:
                continue
            for seg in day.rowing.segments:
                if seg.phase in ("main_set", "work", "build") and seg.priority == "split":
                    result.violations.append(
                        Violation(
                            field="split_priority",
                            severity="warning",
                            master_plan_target="HR primary (Tgt priority = hr)",
                            coach_bot_actual=f"split priority on {day.weekday} {seg.label}",
                            description=(
                                f"{day.weekday} main work uses split as primary metric "
                                "but master plan specifies HR priority."
                            ),
                        )
                    )

    gym_tonnage = metrics.get("gym_tonnage_kg")
    if target.tgt_gym_kg and gym_tonnage is not None:
        if gym_tonnage > target.tgt_gym_kg * 1.20:
            result.violations.append(
                Violation(
                    field="gym_tonnage",
                    severity="warning",
                    master_plan_target=f"≤{target.tgt_gym_kg * 1.20:.0f} kg",
                    coach_bot_actual=f"{gym_tonnage:.0f} kg",
                    description="Estimated gym tonnage exceeds target by more than 20%.",
                )
            )

    if phase == "deload":
        for day in plan.days:
            if day.rowing is None:
                continue
            for seg in day.rowing.segments:
                if _zone_t_index(seg.zone_t) > 3 or seg.zone_z in ("Z4", "Z5"):
                    result.violations.append(
                        Violation(
                            field="phase_intensity",
                            severity="critical",
                            master_plan_target="≤T3 / no Z4–Z5 (deload)",
                            coach_bot_actual=f"{seg.zone_z}/{seg.zone_t} on {day.weekday}",
                            description=(
                                f"{day.weekday} segment exceeds deload intensity cap."
                            ),
                        )
                    )

    if phase in _ON_WATER_PHASES and not _plan_has_on_water(plan):
        result.violations.append(
            Violation(
                field="missing_modality",
                severity="warning",
                master_plan_target="on-water rowing session",
                coach_bot_actual="erg-only rowing",
                description=(
                    f"{phase} phase expects on-water work; plan contains only erg sessions."
                ),
            )
        )

    total_min = _total_prescribed_minutes(plan)
    if target.tgt_min:
        if total_min > target.tgt_min * 1.15:
            result.violations.append(
                Violation(
                    field="volume_overshoot",
                    severity="warning",
                    master_plan_target=f"≤{int(target.tgt_min * 1.15)} min",
                    coach_bot_actual=f"{total_min} min",
                    description="Total prescribed minutes exceed target by more than 15%.",
                )
            )
        if total_min < target.tgt_min * 0.70:
            result.violations.append(
                Violation(
                    field="volume_undershoot",
                    severity="warning",
                    master_plan_target=f"≥{int(target.tgt_min * 0.70)} min",
                    coach_bot_actual=f"{total_min} min",
                    description="Total prescribed minutes are below 70% of target.",
                )
            )

    return result


def _threshold_segment_from(seg: RowingSegment, *, priority: str) -> RowingSegment:
    """Downgrade race-pace (Z5) to threshold (Z4/T6) for base-phase quality work."""
    return RowingSegment(
        phase=seg.phase,
        label=re.sub(
            r"race[\s-]?pace|Z5|VO2",
            "threshold",
            seg.label,
            flags=re.I,
        ),
        duration=seg.duration,
        split_min="1:58",
        split_max="2:05",
        zone_z="Z4",
        zone_t="T6",
        hr_bpm_min=seg.hr_bpm_min if seg.hr_bpm_min >= 150 else 152,
        hr_bpm_max=min(seg.hr_bpm_max, 172) if seg.hr_bpm_max else 172,
        priority=priority,
        notes=(seg.notes or "") + " base-phase threshold (Z5→Z4)",
    )


def _z2_segment_from(seg: RowingSegment, *, priority: str) -> RowingSegment:
    phase = seg.phase
    zone_t = "T2" if phase in ("warm_up", "cool_down") else "T3"
    hr_max = T3_HR_CAP if phase not in ("warm_up", "cool_down") else 148
    label = seg.label
    if seg.zone_z in ("Z4", "Z5") or _zone_t_index(seg.zone_t) >= 4:
        dur = seg.duration or "steady"
        label = re.sub(
            r"\d+\s*[x×]\s*\d+.*",
            f"Aerobic steady — {dur}",
            label,
            flags=re.I,
        )
        if label == seg.label and "Main" in label:
            label = f"Main Set: aerobic steady — {dur}"
    return RowingSegment(
        phase=phase,
        label=label,
        duration=seg.duration,
        split_min="2:05",
        split_max="2:15",
        zone_z="Z2",
        zone_t=zone_t,
        hr_bpm_min=128 if phase in ("warm_up", "cool_down") else 130,
        hr_bpm_max=hr_max,
        priority=priority,
        notes=seg.notes,
    )


def _cap_segment_t3(seg: RowingSegment, *, priority: str) -> RowingSegment:
    downgraded = _z2_segment_from(seg, priority=priority)
    if _zone_t_index(seg.zone_t) <= 3 and seg.zone_z not in ("Z4", "Z5"):
        return RowingSegment(
            phase=seg.phase,
            label=seg.label,
            duration=seg.duration,
            split_min=seg.split_min,
            split_max=seg.split_max,
            zone_z=seg.zone_z,
            zone_t=seg.zone_t,
            hr_bpm_min=seg.hr_bpm_min,
            hr_bpm_max=min(seg.hr_bpm_max, T3_HR_CAP),
            priority=priority if seg.phase in ("main_set", "work", "build") else seg.priority,
            notes=seg.notes,
        )
    return downgraded


def _rewrite_rowing_session(
    rowing: RowingSession,
    *,
    phase: str,
    priority: str,
    max_z5_fraction: Optional[float],
) -> RowingSession:
    def transform(segments: Sequence[RowingSegment]) -> List[RowingSegment]:
        out: List[RowingSegment] = []
        for seg in segments:
            if phase == "deload":
                out.append(_cap_segment_t3(seg, priority=priority))
            elif phase == "base" and seg.zone_z == "Z5":
                out.append(_threshold_segment_from(seg, priority=priority))
            elif phase in ("recovery",) and (
                seg.zone_z in ("Z4", "Z5") or _zone_t_index(seg.zone_t) >= 4
            ):
                out.append(_z2_segment_from(seg, priority=priority))
            elif phase in ("peak", "taper", "race") and priority == "hr":
                if seg.phase in ("main_set", "work", "build") and seg.priority == "split":
                    out.append(
                        RowingSegment(
                            phase=seg.phase,
                            label=seg.label,
                            duration=seg.duration,
                            split_min=seg.split_min,
                            split_max=seg.split_max,
                            zone_z=seg.zone_z,
                            zone_t=seg.zone_t,
                            hr_bpm_min=seg.hr_bpm_min,
                            hr_bpm_max=seg.hr_bpm_max,
                            priority="hr",
                            notes=(seg.notes or "") + " split reference only",
                        )
                    )
                else:
                    out.append(seg)
            else:
                if (
                    priority == "hr"
                    and seg.phase in ("main_set", "work", "build")
                    and seg.priority == "split"
                ):
                    out.append(
                        RowingSegment(
                            phase=seg.phase,
                            label=seg.label,
                            duration=seg.duration,
                            split_min=seg.split_min,
                            split_max=seg.split_max,
                            zone_z=seg.zone_z,
                            zone_t=seg.zone_t,
                            hr_bpm_min=seg.hr_bpm_min,
                            hr_bpm_max=seg.hr_bpm_max,
                            priority="hr",
                            notes=(seg.notes or "") + " split reference only",
                        )
                    )
                else:
                    out.append(seg)
        if max_z5_fraction is not None:
            total = sum(_segment_minutes(s) for s in out) or 1
            z5 = sum(_segment_minutes(s) for s in out if s.zone_z == "Z5")
            if z5 / total > max_z5_fraction:
                out = [
                    _z2_segment_from(s, priority=priority)
                    if s.zone_z == "Z5"
                    else s
                    for s in out
                ]
        return out

    alt = rowing.erg_alternative
    return RowingSession(
        segments=transform(rowing.segments),
        erg_alternative=(
            type(alt)(
                description=alt.description,
                segments=transform(alt.segments),
            )
            if alt
            else None
        ),
    )


def _long_z2_main_segment(minutes: int = 22) -> RowingSegment:
    return RowingSegment(
        phase="main_set",
        label="Aerobic steady-state",
        duration=f"{minutes} min",
        split_min="2:05",
        split_max="2:15",
        zone_z="Z2",
        zone_t="T3",
        hr_bpm_min=130,
        hr_bpm_max=T3_HR_CAP,
        priority="hr",
        notes=None,
    )


def _warmup_segment(priority: str = "hr") -> RowingSegment:
    wu_min = WARMUP_COOLDOWN_DEFAULT_MINUTES[0]
    return RowingSegment(
        phase="warm_up",
        label="Warm-up",
        duration=f"{wu_min} min",
        split_min="2:15",
        split_max="2:25",
        zone_z="Z2",
        zone_t="T2",
        hr_bpm_min=120,
        hr_bpm_max=140,
        priority=priority,
        notes=None,
    )


def _cooldown_segment(priority: str = "hr") -> RowingSegment:
    cd_min = WARMUP_COOLDOWN_DEFAULT_MINUTES[1]
    return RowingSegment(
        phase="cool_down",
        label="Cool-down",
        duration=f"{cd_min} min",
        split_min="2:20",
        split_max="2:30",
        zone_z="Z2",
        zone_t="T2",
        hr_bpm_min=118,
        hr_bpm_max=135,
        priority=priority,
        notes=None,
    )


def _ensure_rowing_warmup_cooldown(
    rowing: RowingSession,
    *,
    priority: str,
) -> RowingSession:
    """Inject warm-up and cool-down when the LLM omitted them (≤15 min each)."""

    def _cap_wucd_segment(seg: RowingSegment, *, phase: str) -> RowingSegment:
        mins = _segment_minutes(seg)
        floor = WARMUP_COOLDOWN_FLOOR_MINUTES
        cap = WARMUP_COOLDOWN_CAP_MINUTES
        if mins < floor or mins > cap:
            return _warmup_segment(priority) if phase == "warm_up" else _cooldown_segment(priority)
        return seg

    def fix(segments: Sequence[RowingSegment]) -> List[RowingSegment]:
        segs = list(segments)
        phases = {s.phase for s in segs}
        if "warm_up" not in phases:
            segs.insert(0, _warmup_segment(priority))
        if "cool_down" not in phases:
            segs.append(_cooldown_segment(priority))
        upgraded: List[RowingSegment] = []
        for s in segs:
            if s.phase == "warm_up":
                upgraded.append(_cap_wucd_segment(s, phase="warm_up"))
            elif s.phase == "cool_down":
                upgraded.append(_cap_wucd_segment(s, phase="cool_down"))
            else:
                upgraded.append(s)
        return upgraded

    alt = rowing.erg_alternative
    return RowingSession(
        segments=fix(rowing.segments),
        erg_alternative=(
            type(alt)(
                description=alt.description,
                segments=fix(alt.segments),
            )
            if alt
            else None
        ),
    )


def _trim_segments_to_cap(
    segments: Sequence[RowingSegment],
    *,
    cap: int,
) -> Tuple[List[RowingSegment], int]:
    """Shorten minute-based main sets until segments fit ``cap``; return overflow minutes."""
    segs = list(segments)
    total = sum(_segment_minutes(s) for s in segs)
    if total <= cap:
        return segs, 0
    to_remove = total - cap
    removed = 0
    main_indices = [
        i
        for i, seg in enumerate(segs)
        if seg.phase in ("main_set", "work", "build")
    ]
    for idx in sorted(main_indices, key=lambda i: _segment_minutes(segs[i]), reverse=True):
        seg = segs[idx]
        match = re.match(r"^(\d+)\s*min$", (seg.duration or "").strip(), re.I)
        if not match:
            continue
        current = int(match.group(1))
        if current <= 5:
            continue
        cut = min(to_remove - removed, current - 5)
        if cut <= 0:
            continue
        new_min = current - cut
        segs[idx] = RowingSegment(
            phase=seg.phase,
            label=seg.label,
            duration=f"{new_min} min",
            split_min=seg.split_min,
            split_max=seg.split_max,
            zone_z=seg.zone_z,
            zone_t=seg.zone_t,
            hr_bpm_min=seg.hr_bpm_min,
            hr_bpm_max=seg.hr_bpm_max,
            priority=seg.priority,
            notes=seg.notes,
        )
        removed += cut
        if removed >= to_remove:
            break
    remaining = sum(_segment_minutes(s) for s in segs)
    overflow = max(0, remaining - cap) + max(0, to_remove - removed)
    return segs, overflow


def _trim_rowing_session_to_cap(
    rowing: RowingSession,
    *,
    cap: int = SESSION_CAP_MINUTES,
) -> Tuple[RowingSession, int]:
    """Fit one rowing session under the cap; keep erg alternative in sync when present."""
    segments, overflow = _trim_segments_to_cap(rowing.segments, cap=cap)
    alt = rowing.erg_alternative
    alt_overflow = 0
    if alt is not None:
        alt_segments, alt_overflow = _trim_segments_to_cap(alt.segments, cap=cap)
        alt = ErgAlternative(description=alt.description, segments=alt_segments)
    return (
        RowingSession(segments=segments, erg_alternative=alt),
        max(overflow, alt_overflow),
    )


def _spill_rowing_minutes_to_weekend(
    days: List[DayPlan],
    minutes: int,
    *,
    priority: str,
    min_spill: int = 5,
) -> List[DayPlan]:
    """Move aerobic volume trimmed from Tue/Thu to Friday or Saturday erg sessions."""
    if minutes < min_spill:
        return days
    remaining = minutes
    for weekday in ("Friday", "Saturday"):
        if remaining < min_spill:
            break
        idx = next((i for i, d in enumerate(days) if d.weekday == weekday), None)
        if idx is None:
            continue
        day = days[idx]
        if day.session_type not in ("rest", "recovery"):
            continue
        spill_main = min(remaining, 50)
        days[idx] = DayPlan(
            weekday=day.weekday,
            date=day.date,
            session_type="erg",
            session_subtype="steady-state",
            gym=None,
            rowing=RowingSession(
                segments=[
                    _warmup_segment(priority),
                    _long_z2_main_segment(spill_main),
                    _cooldown_segment(priority),
                ],
                erg_alternative=None,
            ),
            notes=(day.notes or "")
            + f" [session-cap spill: +{spill_main} min aerobic from Tue/Thu]",
        )
        remaining -= spill_main
    return days


def _enforce_session_duration_caps(
    plan: WeeklyPlan,
    *,
    priority: str,
) -> WeeklyPlan:
    """Ensure each gym/erg/on-water session is within SESSION_CAP_MINUTES."""
    days = list(plan.days)
    spill_total = 0
    for idx, day in enumerate(days):
        if day.session_type not in ("erg", "on_water") or day.rowing is None:
            continue
        mins = estimate_rowing_session_minutes(day.rowing)
        if mins <= SESSION_CAP_MINUTES:
            continue
        trimmed, overflow = _trim_rowing_session_to_cap(
            day.rowing, cap=SESSION_CAP_MINUTES
        )
        spill_total += overflow
        note = (day.notes or "") + " [trimmed to session cap]"
        days[idx] = DayPlan(
            weekday=day.weekday,
            date=day.date,
            session_type=day.session_type,
            session_subtype=day.session_subtype,
            gym=day.gym,
            rowing=trimmed,
            notes=note.strip(),
        )
    if spill_total > 0:
        days = _spill_rowing_minutes_to_weekend(days, spill_total, priority=priority)
    return WeeklyPlan(
        version=plan.version,
        personalised=plan.personalised,
        days=days,
        greeting=plan.greeting,
    )


def _ensure_gym_working_sets(gym: GymSession, phase: str) -> GymSession:
    """Base/build: 3–4 sets per exercise with phase-appropriate load pyramids."""
    from gym_pyramid import apply_phase_gym_pyramid

    return apply_phase_gym_pyramid(gym, phase)


def _clone_gym(gym: GymSession) -> GymSession:
    return GymSession(
        category=gym.category,
        goal=gym.goal,
        exercises=[
            GymExercise(
                name=ex.name,
                sets=[
                    GymSet(reps=s.reps, weight_kg=s.weight_kg, duration_sec=s.duration_sec)
                    for s in ex.sets
                ],
            )
            for ex in gym.exercises
        ],
    )


def _clone_rowing(rowing: RowingSession) -> RowingSession:
    def clone_segs(segs: Sequence[RowingSegment]) -> List[RowingSegment]:
        return [
            RowingSegment(
                phase=s.phase,
                label=s.label,
                duration=s.duration,
                split_min=s.split_min,
                split_max=s.split_max,
                zone_z=s.zone_z,
                zone_t=s.zone_t,
                hr_bpm_min=s.hr_bpm_min,
                hr_bpm_max=s.hr_bpm_max,
                priority=s.priority,
                notes=s.notes,
            )
            for s in segs
        ]

    alt = rowing.erg_alternative
    return RowingSession(
        segments=clone_segs(rowing.segments),
        erg_alternative=(
            ErgAlternative(
                description=alt.description,
                segments=clone_segs(alt.segments),
            )
            if alt
            else None
        ),
    )


def _template_leg_gym(*, phase: str) -> GymSession:
    from weekly_plan_schema import GYM_CATEGORY_LEG

    exercises = [
        GymExercise(name="Back squat", sets=[GymSet(8, 70.0, None)] * 3),
        GymExercise(name="Romanian deadlift", sets=[GymSet(8, 60.0, None)] * 3),
        GymExercise(name="Bulgarian split squat", sets=[GymSet(8, 30.0, None)] * 3),
        GymExercise(name="Plank", sets=[GymSet(1, None, 30)] * 2),
    ]
    gym = GymSession(category=GYM_CATEGORY_LEG, goal="strength", exercises=exercises)
    if phase == "deload":
        from gym_deload import apply_deload_modifier, DeloadConfig

        return apply_deload_modifier(gym, DeloadConfig(min_sets=2, max_sets=2))
    return _ensure_gym_working_sets(gym, phase) if phase in ("base", "build") else gym


def _template_upper_gym(*, phase: str) -> GymSession:
    from weekly_plan_schema import GYM_CATEGORY_UPPER_CORE

    exercises = [
        GymExercise(name="Incline bench press", sets=[GymSet(8, 40.0, None)] * 3),
        GymExercise(name="Barbell row", sets=[GymSet(8, 45.0, None)] * 3),
        GymExercise(name="Lat pull-down", sets=[GymSet(8, 50.0, None)] * 3),
        GymExercise(name="Russian twists", sets=[GymSet(20, 8.0, None)] * 2),
    ]
    gym = GymSession(category=GYM_CATEGORY_UPPER_CORE, goal="strength", exercises=exercises)
    if phase == "deload":
        from gym_deload import apply_deload_modifier, DeloadConfig

        return apply_deload_modifier(gym, DeloadConfig(min_sets=2, max_sets=2))
    return _ensure_gym_working_sets(gym, phase) if phase in ("base", "build") else gym


def _template_aerobic_rowing(*, priority: str, on_water: bool) -> RowingSession:
    segments = [
        _warmup_segment(priority),
        _long_z2_main_segment(50),
        _cooldown_segment(priority),
    ]
    if on_water:
        return RowingSession(
            segments=segments,
            erg_alternative=ErgAlternative(
                description="Group erg fallback",
                segments=segments,
            ),
        )
    return RowingSession(segments=segments, erg_alternative=None)


def _find_gym_in_plan(plan: WeeklyPlan, category: str) -> Optional[GymSession]:
    for day in plan.days:
        if day.gym and day.gym.category == category:
            return day.gym
    return None


def _find_rowing_in_plan(plan: WeeklyPlan, session_type: str) -> Optional[RowingSession]:
    for day in plan.days:
        if day.session_type == session_type and day.rowing:
            return day.rowing
    return None


def _rowing_for_weekday(plan: Optional[WeeklyPlan], weekday: str) -> Optional[RowingSession]:
    if plan is None:
        return None
    for day in plan.days:
        if day.weekday == weekday and day.rowing:
            return day.rowing
    return None


def repair_fixed_weekly_schedule(
    plan: WeeklyPlan,
    *,
    include_lifting: bool = True,
    phase: str = "",
    prev_plan: Optional[WeeklyPlan] = None,
    reference_plan: Optional[WeeklyPlan] = None,
    priority: str = "hr",
) -> WeeklyPlan:
    """Restore Mon/Wed gym and Tue/Thu rowing when the LLM misplaced sessions."""
    phase = _normalize_phase(phase)
    thursday_on_water = phase in _ON_WATER_PHASES
    repaired: List[DayPlan] = []

    for day in plan.days:
        gym = day.gym
        rowing = day.rowing
        session_type = day.session_type
        subtype = day.session_subtype
        notes = day.notes

        if include_lifting and day.weekday == "Monday" and session_type != "gym":
            gym = (
                (reference_plan and gym_for_weekday(reference_plan, "Monday"))
                or (prev_plan and gym_for_weekday(prev_plan, "Monday"))
                or _find_gym_in_plan(plan, "leg")
                or _template_leg_gym(phase=phase)
            )
            gym = _clone_gym(gym)
            session_type = "gym"
            rowing = None
            notes = (notes or "") + " [schedule repair: Mon gym restored]".strip()

        elif include_lifting and day.weekday == "Wednesday" and session_type != "gym":
            gym = (
                (reference_plan and gym_for_weekday(reference_plan, "Wednesday"))
                or (prev_plan and gym_for_weekday(prev_plan, "Wednesday"))
                or _find_gym_in_plan(plan, "upper_core")
                or _template_upper_gym(phase=phase)
            )
            gym = _clone_gym(gym)
            session_type = "gym"
            rowing = None
            notes = (notes or "") + " [schedule repair: Wed gym restored]".strip()

        elif day.weekday == "Tuesday" and session_type != "erg":
            rowing_src = (
                _rowing_for_weekday(reference_plan, "Tuesday")
                or _rowing_for_weekday(prev_plan, "Tuesday")
                or _find_rowing_in_plan(plan, "erg")
                or _find_rowing_in_plan(plan, "on_water")
                or _template_aerobic_rowing(priority=priority, on_water=False)
            )
            rowing = _clone_rowing(rowing_src)
            session_type = "erg"
            gym = None
            subtype = subtype or "steady-state"
            notes = (notes or "") + " [schedule repair: Tue erg restored]".strip()

        elif day.weekday == "Thursday" and session_type not in ("erg", "on_water"):
            rowing_src = (
                _rowing_for_weekday(reference_plan, "Thursday")
                or _rowing_for_weekday(prev_plan, "Thursday")
                or _find_rowing_in_plan(plan, "on_water")
                or _find_rowing_in_plan(plan, "erg")
                or _template_aerobic_rowing(
                    priority=priority, on_water=thursday_on_water
                )
            )
            rowing = _clone_rowing(rowing_src)
            session_type = "on_water" if thursday_on_water else "erg"
            gym = None
            subtype = subtype or "steady-state"
            notes = (notes or "") + " [schedule repair: Thu rowing restored]".strip()

        elif day.weekday == "Thursday" and thursday_on_water and session_type == "erg":
            rowing = _clone_rowing(rowing or _template_aerobic_rowing(priority=priority, on_water=True))
            session_type = "on_water"
            if rowing.erg_alternative is None:
                rowing = RowingSession(
                    segments=rowing.segments,
                    erg_alternative=ErgAlternative(
                        description="Group erg fallback",
                        segments=list(rowing.segments),
                    ),
                )

        if rowing is not None:
            rowing = _ensure_rowing_warmup_cooldown(rowing, priority=priority)

        repaired.append(
            DayPlan(
                weekday=day.weekday,
                date=day.date,
                session_type=session_type,
                session_subtype=subtype,
                gym=gym,
                rowing=rowing,
                notes=notes,
            )
        )

    return _enforce_session_duration_caps(
        WeeklyPlan(
            version=plan.version,
            personalised=plan.personalised,
            days=repaired,
            greeting=plan.greeting,
        ),
        priority=priority,
    )


def _ensure_modality(plan: WeeklyPlan, *, phase: str, priority: str) -> WeeklyPlan:
    if _plan_has_on_water(plan) or _plan_has_long_z2(plan):
        return plan
    days = list(plan.days)
    erg_alt_segments = [
        _warmup_segment(priority),
        _long_z2_main_segment(50),
        _cooldown_segment(priority),
    ]
    thursday_idx = next(
        (i for i, d in enumerate(days) if d.weekday == "Thursday"),
        None,
    )
    if thursday_idx is not None and days[thursday_idx].session_type == "erg":
        day = days[thursday_idx]
        days[thursday_idx] = DayPlan(
            weekday=day.weekday,
            date=day.date,
            session_type="on_water",
            session_subtype="steady-state",
            gym=None,
            rowing=RowingSession(
                segments=erg_alt_segments,
                erg_alternative=ErgAlternative(
                    description="Group erg fallback — same aerobic structure",
                    segments=erg_alt_segments,
                ),
            ),
            notes=day.notes,
        )
    else:
        friday_idx = next(
            (i for i, d in enumerate(days) if d.weekday == "Friday"),
            None,
        )
        if friday_idx is not None and days[friday_idx].session_type == "rest":
            day = days[friday_idx]
            days[friday_idx] = DayPlan(
                weekday=day.weekday,
                date=day.date,
                session_type="erg",
                session_subtype="steady-state",
                gym=None,
                rowing=RowingSession(
                    segments=[
                        _warmup_segment(priority),
                        _long_z2_main_segment(55),
                        _cooldown_segment(priority),
                    ],
                    erg_alternative=None,
                ),
                notes="Added to meet base/deload aerobic volume",
            )
    return WeeklyPlan(
        version=plan.version,
        personalised=plan.personalised,
        days=days,
        greeting=plan.greeting,
    )


def _scale_rowing_volume(
    plan: WeeklyPlan, target_min: int, *, priority: str = "hr"
) -> WeeklyPlan:
    current = _total_prescribed_minutes(plan)
    if current <= 0 or not target_min:
        return plan
    lo, hi = target_min * 0.90, target_min * 1.10
    if lo <= current <= hi:
        return plan
    factor = target_min / current
    days: List[DayPlan] = []
    spill_total = 0
    for day in plan.days:
        if day.rowing is None:
            days.append(day)
            continue

        def scale_seg(seg: RowingSegment) -> RowingSegment:
            dur = seg.duration or ""
            m = re.search(r"^(\d+)\s*min$", dur.strip(), re.I)
            if m:
                new_min = max(5, int(round(int(m.group(1)) * factor)))
                return RowingSegment(
                    phase=seg.phase,
                    label=seg.label,
                    duration=f"{new_min} min",
                    split_min=seg.split_min,
                    split_max=seg.split_max,
                    zone_z=seg.zone_z,
                    zone_t=seg.zone_t,
                    hr_bpm_min=seg.hr_bpm_min,
                    hr_bpm_max=seg.hr_bpm_max,
                    priority=seg.priority,
                    notes=seg.notes,
                )
            clock = re.match(r"^(\d{1,2}):(\d{2})$", dur.strip())
            if clock:
                total = int(clock.group(1)) + (1 if int(clock.group(2)) >= 30 else 0)
                new_min = max(5, int(round(total * factor)))
                return RowingSegment(
                    phase=seg.phase,
                    label=seg.label,
                    duration=f"{new_min} min",
                    split_min=seg.split_min,
                    split_max=seg.split_max,
                    zone_z=seg.zone_z,
                    zone_t=seg.zone_t,
                    hr_bpm_min=seg.hr_bpm_min,
                    hr_bpm_max=seg.hr_bpm_max,
                    priority=seg.priority,
                    notes=seg.notes,
                )
            return seg

        def scale_rowing(r: RowingSession) -> RowingSession:
            alt = r.erg_alternative
            return RowingSession(
                segments=[scale_seg(s) for s in r.segments],
                erg_alternative=(
                    type(alt)(
                        description=alt.description,
                        segments=[scale_seg(s) for s in alt.segments],
                    )
                    if alt
                    else None
                ),
            )

        scaled_rowing = scale_rowing(day.rowing)
        if estimate_rowing_session_minutes(scaled_rowing) > SESSION_CAP_MINUTES:
            trimmed, overflow = _trim_rowing_session_to_cap(
                scaled_rowing, cap=SESSION_CAP_MINUTES
            )
            spill_total += overflow
            scaled_rowing = trimmed
        days.append(
            DayPlan(
                weekday=day.weekday,
                date=day.date,
                session_type=day.session_type,
                session_subtype=day.session_subtype,
                gym=day.gym,
                rowing=scaled_rowing,
                notes=day.notes,
            )
        )
    if spill_total > 0:
        days = _spill_rowing_minutes_to_weekend(days, spill_total, priority=priority)
    return WeeklyPlan(
        version=plan.version,
        personalised=plan.personalised,
        days=days,
        greeting=plan.greeting,
    )


def _apply_taper_volume(
    plan: WeeklyPlan,
    prev_plan: Optional[WeeklyPlan],
) -> WeeklyPlan:
    if prev_plan is None:
        return plan
    prev_min = _total_prescribed_minutes(prev_plan)
    if prev_min <= 0:
        return plan
    target = int(prev_min * 0.55)
    return _scale_rowing_volume(plan, target)


def _phase_z5_cap(phase: str, target: WeeklyTarget) -> Optional[float]:
    phase = _normalize_phase(phase)
    if target.tgt_z5_percent is not None:
        return target.tgt_z5_percent / 100.0
    caps = {
        "base": 0.10,
        "deload": 0.05,
        "recovery": 0.05,
        "build": 0.18,
        "peak": 0.22,
        "taper": 0.22,
        "race": 0.22,
    }
    return caps.get(phase)


def correct_weekly_plan(
    week: str,
    coach_bot_plan: Union[WeeklyPlan, Mapping[str, Any]],
    weekly_targets: Mapping[str, WeeklyTarget],
    *,
    previous_week_plan: Optional[Union[WeeklyPlan, Mapping[str, Any]]] = None,
    reference_plan: Optional[Union[WeeklyPlan, Mapping[str, Any]]] = None,
) -> Tuple[WeeklyPlan, ValidationResult]:
    """Step 3: rewrite CoachBot plan to conform to master-plan constraints."""
    target = _resolve_target(week, weekly_targets)
    if isinstance(coach_bot_plan, Mapping):
        parsed = parse_weekly_plan(coach_bot_plan)
        if parsed is None:
            raise ValueError("coach_bot_plan is not valid weekly plan JSON")
        plan = parsed
    else:
        plan = coach_bot_plan

    if target is None:
        return plan, validate_weekly_plan(week, plan, weekly_targets)

    phase = _normalize_phase(target.phase)
    priority = target.tgt_priority
    max_z5 = _phase_z5_cap(phase, target)

    prev_parsed: Optional[WeeklyPlan] = None
    if previous_week_plan is not None:
        if isinstance(previous_week_plan, Mapping):
            prev_parsed = parse_weekly_plan(previous_week_plan)
        else:
            prev_parsed = previous_week_plan

    ref_parsed: Optional[WeeklyPlan] = None
    if reference_plan is not None:
        if isinstance(reference_plan, Mapping):
            ref_parsed = parse_weekly_plan(reference_plan)
        else:
            ref_parsed = reference_plan

    include_lifting = bool(target.tgt_gym_kg) or any(
        d.session_type == "gym" for d in plan.days
    ) or (
        prev_parsed is not None and any(d.session_type == "gym" for d in prev_parsed.days)
    )
    from weekly_plan_schema import validate_fixed_weekly_schedule

    if validate_fixed_weekly_schedule(plan, include_lifting=include_lifting):
        plan = repair_fixed_weekly_schedule(
            plan,
            include_lifting=include_lifting,
            phase=phase,
            prev_plan=prev_parsed,
            reference_plan=ref_parsed,
            priority=priority,
        )

    days: List[DayPlan] = []
    gym_day_budget: Optional[float] = None
    if target.tgt_gym_kg:
        gym_days = sum(1 for d in plan.days if d.session_type == "gym")
        if gym_days:
            gym_day_budget = target.tgt_gym_kg / gym_days

    for day in plan.days:
        gym = day.gym
        rowing = day.rowing
        if gym and phase == "deload":
            ref_gym = (
                gym_for_weekday(prev_parsed, day.weekday)
                if prev_parsed is not None
                else None
            )
            session_target = (
                gym_day_budget
                if gym_day_budget is not None
                else (target.tgt_gym_kg or 3000) / 2.0
            )
            gym, _adj_notes, _check = apply_deload_gym_session(
                gym,
                reference_gym=ref_gym,
                session_tonnage_target=session_target,
            )
        elif gym and phase in ("base", "build"):
            gym = _ensure_gym_working_sets(gym, phase)
        if (
            gym
            and phase not in ("deload",)
            and target.tgt_gym_kg
            and gym_day_budget is not None
            and gym_session_tonnage(gym) > gym_day_budget * 1.20
        ):
            from gym_deload import apply_deload_modifier, DeloadConfig
            from weekly_plan_schema import gym_working_set_bounds

            min_s, max_s = gym_working_set_bounds(phase)
            gym = apply_deload_modifier(
                gym,
                DeloadConfig(
                    min_sets=min_s,
                    max_sets=max_s,
                    load_reduction_factor=0.90,
                ),
            )
            if phase in ("base", "build"):
                gym = _ensure_gym_working_sets(gym, phase)
        if rowing:
            rowing = _rewrite_rowing_session(
                rowing,
                phase=phase,
                priority=priority,
                max_z5_fraction=max_z5,
            )
            rowing = _ensure_rowing_warmup_cooldown(rowing, priority=priority)
        days.append(
            DayPlan(
                weekday=day.weekday,
                date=day.date,
                session_type=day.session_type,
                session_subtype=day.session_subtype,
                gym=gym,
                rowing=rowing,
                notes=day.notes,
            )
        )

    corrected = WeeklyPlan(
        version=plan.version,
        personalised=plan.personalised,
        days=days,
        greeting=plan.greeting,
    )

    if phase in _ON_WATER_PHASES:
        corrected = _ensure_modality(corrected, phase=phase, priority=priority)

    if phase == "taper":
        corrected = _apply_taper_volume(corrected, prev_parsed)
    elif target.tgt_min:
        corrected = _scale_rowing_volume(
            corrected, target.tgt_min, priority=priority
        )

    corrected = _enforce_session_duration_caps(corrected, priority=priority)

    if target.tgt_min:
        current = _total_prescribed_minutes(corrected)
        lo = target.tgt_min * 0.90
        if current < lo:
            need = int(round(lo - current))
            days = _spill_rowing_minutes_to_weekend(
                list(corrected.days), need, priority=priority, min_spill=10
            )
            corrected = WeeklyPlan(
                version=corrected.version,
                personalised=corrected.personalised,
                days=days,
                greeting=corrected.greeting,
            )
            corrected = _enforce_session_duration_caps(corrected, priority=priority)

    validation = validate_weekly_plan(week, corrected, weekly_targets)
    return corrected, validation


def _split_reference_suffix(priority: str, segment_priority: str) -> str:
    if priority == "hr" or segment_priority == "hr":
        return " (reference only)"
    return ""


def _format_segment_markdown(
    heading: str,
    seg: RowingSegment,
    *,
    week_priority: str,
) -> List[str]:
    ref = _split_reference_suffix(week_priority, seg.priority)
    pri_label = "HR" if seg.priority == "hr" or week_priority == "hr" else "Split"
    lines = [
        f"### {heading}",
        f"- Duration: {seg.duration or '—'}",
        f"- Zone: {seg.zone_t}",
        f"- Target HR: {seg.hr_bpm_min}–{seg.hr_bpm_max} bpm (MHR {DEFAULT_MAX_HR})",
        f"- Target Split: {seg.split_min}–{seg.split_max}{ref}",
    ]
    if heading == "Main Set":
        structure = _strip_rowing_phase_prefix(seg.label)
        duration = (seg.duration or "").strip()
        if duration and duration not in structure:
            replaced = _replace_trailing_simple_duration(structure, duration)
            if replaced is not None:
                structure = replaced
            elif not (
                re.fullmatch(r"\d+\s*min", duration, re.I)
                and re.search(r"\d+\s*[x×]\s*\d+", structure, re.I)
            ):
                structure = f"{structure} — {duration}" if structure else duration
        lines = [
            f"### {heading}",
            f"- Structure: {structure}",
            f"- Zone: {seg.zone_t}",
            f"- Target HR: {seg.hr_bpm_min}–{seg.hr_bpm_max} bpm",
            f"- Target Split: {seg.split_min}–{seg.split_max}{ref}",
            f"- Priority metric: {pri_label}",
        ]
    return lines


def render_corrected_plan_markdown(
    plan: WeeklyPlan,
    *,
    phase: str,
    priority: str,
) -> str:
    """Step 4: session markdown with HR, split, zone, and priority metric."""
    parts: List[str] = []
    if plan.greeting:
        parts.append(plan.greeting.strip())
        parts.append("")

    session_labels = {
        "gym": "Gym",
        "erg": "Erg",
        "on_water": "On-water",
        "rest": "Rest",
        "recovery": "Recovery",
    }

    for day in plan.days:
        label = session_labels.get(day.session_type, day.session_type)
        subtype = f" — {day.session_subtype}" if day.session_subtype else ""
        parts.append(f"## {day.weekday}: {label}{subtype}")
        parts.append(
            f"**Phase:** {phase} | **Priority:** {priority} | "
            f"**Zone Target:** varies by segment"
        )
        parts.append("")

        if day.gym:
            parts.append("### Gym session")
            for i, ex in enumerate(day.gym.exercises, start=1):
                parts.append(f"{i}. **{ex.name}**")
                for j, s in enumerate(ex.sets, start=1):
                    if s.duration_sec:
                        parts.append(f"   - Set {j}: {s.duration_sec}s hold")
                    elif s.weight_kg is not None:
                        w = int(s.weight_kg) if s.weight_kg == int(s.weight_kg) else s.weight_kg
                        parts.append(f"   - Set {j}: {s.reps}×{w} kg")
            parts.append("")

        if day.rowing:
            phase_map = {
                "warm_up": "Warm-up",
                "main_set": "Main Set",
                "work": "Main Set",
                "build": "Main Set",
                "cool_down": "Cool-down",
            }
            seen_main = False
            for seg in day.rowing.segments:
                heading = phase_map.get(seg.phase, seg.phase.replace("_", " ").title())
                if heading == "Main Set":
                    if seen_main:
                        heading = "Main Set (continued)"
                    seen_main = True
                parts.extend(
                    _format_segment_markdown(heading, seg, week_priority=priority)
                )
                parts.append("")
            if day.rowing.erg_alternative:
                parts.append("### Erg alternative")
                parts.append(f"- {day.rowing.erg_alternative.description}")
                for seg in day.rowing.erg_alternative.segments:
                    parts.extend(
                        _format_segment_markdown(
                            seg.phase.replace("_", " ").title(),
                            seg,
                            week_priority=priority,
                        )
                    )
                    parts.append("")

        if day.session_type in ("rest", "recovery") and not day.gym and not day.rowing:
            parts.append("_No session prescribed._")
            parts.append("")

        if day.notes:
            parts.append(f"_Notes: {day.notes}_")
            parts.append("")

    return "\n".join(parts).strip()


def _alignment_result_from_plan(
    plan: WeeklyPlan,
    *,
    plan_json: Dict[str, Any],
    target: Optional[WeeklyTarget],
    validation_before: ValidationResult,
    validation_after: ValidationResult,
    corrected: bool,
    aligned: bool,
) -> AlignmentResult:
    phase = target.phase if target else ""
    priority = target.tgt_priority if target else "hr"
    return AlignmentResult(
        plan_json=plan_json,
        plan_markdown=render_corrected_plan_markdown(
            plan, phase=phase, priority=priority
        ),
        plan_text=render_plan_text(plan),
        validation_before=validation_before,
        validation_after=validation_after,
        corrected=corrected,
        aligned=aligned,
    )


def enforce_weekly_plan_alignment(
    week: str,
    coach_bot_plan: Mapping[str, Any],
    weekly_targets: Mapping[str, WeeklyTarget],
    *,
    previous_week_plan: Optional[Mapping[str, Any]] = None,
    reference_plan: Optional[Mapping[str, Any]] = None,
) -> AlignmentResult:
    """
    Mandatory alignment pass for every generated plan.

    When master-plan targets exist for ``week``, always run ``correct_weekly_plan``
    and re-render display text from the corrected JSON.
    """
    before = validate_weekly_plan(week, coach_bot_plan, weekly_targets)
    parsed = parse_weekly_plan(coach_bot_plan)
    if parsed is None:
        return AlignmentResult(
            plan_json=dict(coach_bot_plan),
            plan_markdown="",
            plan_text="",
            validation_before=before,
            validation_after=before,
            corrected=False,
            aligned=False,
        )

    target = _resolve_target(week, weekly_targets)
    if target is None:
        return _alignment_result_from_plan(
            parsed,
            plan_json=dict(coach_bot_plan),
            target=None,
            validation_before=before,
            validation_after=before,
            corrected=False,
            aligned=False,
        )

    corrected_plan, after = correct_weekly_plan(
        week,
        coach_bot_plan,
        weekly_targets,
        previous_week_plan=previous_week_plan,
        reference_plan=reference_plan,
    )
    plan_json = weekly_plan_to_dict(corrected_plan)
    had_violations = bool(before.violations)
    still_has_violations = bool(after.violations)
    return _alignment_result_from_plan(
        corrected_plan,
        plan_json=plan_json,
        target=target,
        validation_before=before,
        validation_after=after,
        corrected=had_violations or still_has_violations,
        aligned=True,
    )


def align_weekly_plan_with_master(
    week: str,
    coach_bot_plan: Mapping[str, Any],
    weekly_targets: Mapping[str, WeeklyTarget],
    *,
    previous_week_plan: Optional[Mapping[str, Any]] = None,
    reference_plan: Optional[Mapping[str, Any]] = None,
    force: bool = False,
) -> AlignmentResult:
    """Validate and align plan against master targets (always aligns when targets exist)."""
    _ = force  # legacy parameter; alignment is always enforced when targets exist
    return enforce_weekly_plan_alignment(
        week,
        coach_bot_plan,
        weekly_targets,
        previous_week_plan=previous_week_plan,
        reference_plan=reference_plan,
    )


def regression_checklist_deload_2026_06_29(
    plan: Union[WeeklyPlan, Mapping[str, Any]],
    *,
    weekly_targets: Optional[Mapping[str, WeeklyTarget]] = None,
) -> Dict[str, bool]:
    """Step 5 pass/fail checklist for the 2026-06-29 deload week."""
    week = "2026-06-29"
    if weekly_targets is None:
        weekly_targets = {
            week: WeeklyTarget(
                week=week,
                phase="deload",
                tgt_priority="hr",
                tgt_z2_percent=85.0,
                tgt_z5_percent=5.0,
                tgt_km=42.0,
                tgt_min=250,
                tgt_gym_kg=3200.0,
            )
        }

    target = _resolve_target(week, weekly_targets)
    gym_target_kg = (target.tgt_gym_kg if target else None) or 3200.0

    if isinstance(plan, Mapping):
        parsed = parse_weekly_plan(plan)
        assert parsed is not None
        plan_obj = parsed
    else:
        plan_obj = plan

    metrics = planned_metrics_from_plan_json(plan_obj)
    total_min = _total_prescribed_minutes(plan_obj)

    no_exceeds_t3 = True
    for day in plan_obj.days:
        if day.rowing is None:
            continue
        for seg in day.rowing.segments:
            if _zone_t_index(seg.zone_t) > 3 or seg.zone_z in ("Z4", "Z5"):
                no_exceeds_t3 = False
        alt = day.rowing.erg_alternative
        if alt:
            for seg in alt.segments:
                if _zone_t_index(seg.zone_t) > 3 or seg.zone_z in ("Z4", "Z5"):
                    no_exceeds_t3 = False

    volume_ok = 225 <= total_min <= 275
    gym_ok = (
        gym_target_kg * 0.8
        <= (metrics.get("gym_tonnage_kg") or 0)
        <= gym_target_kg * 1.2
    )
    gym_sets_ok = all(
        all(len(ex.sets) == 2 for ex in day.gym.exercises)
        for day in plan_obj.days
        if day.session_type == "gym" and day.gym
    )
    modality_ok = _plan_has_on_water(plan_obj) or _plan_has_long_z2(plan_obj)

    split_ref_ok = True
    md = render_corrected_plan_markdown(
        plan_obj, phase="deload", priority="hr"
    )
    if "Priority metric: Split" in md:
        split_ref_ok = False
    if re.search(r"Target Split:.*(?<!reference only)\n", md):
        # All split lines in hr-priority week should say reference only
        for line in md.splitlines():
            if line.startswith("- Target Split:") and "reference only" not in line:
                split_ref_ok = False

    z2_pct = metrics.get("z2_percent") or 0.0
    z2_ok = z2_pct >= 80.0

    return {
        "No session exceeds T3 intensity": no_exceeds_t3,
        "Total prescribed minutes within 10% of 250 min target": volume_ok,
        f"Gym tonnage is within 20% of deload target ({gym_target_kg:.0f} kg)": gym_ok,
        "Every deload gym exercise has exactly 2 sets": gym_sets_ok,
        "At least one on-water or long Z2 erg session is included": modality_ok,
        'All split targets labelled "(reference only)" since Tgt priority = hr': split_ref_ok,
        "Z2 distribution across all sessions is ≥80%": z2_ok,
    }


def format_regression_checklist(results: Mapping[str, bool]) -> str:
    lines = ["Regression test results (2026-06-29 deload week):", ""]
    for item, passed in results.items():
        mark = "PASS" if passed else "FAIL"
        lines.append(f"- [{'x' if passed else ' '}] {item} — **{mark}**")
    all_pass = all(results.values())
    lines.append("")
    lines.append(f"Overall: {'PASS' if all_pass else 'FAIL'}")
    return "\n".join(lines)


def load_and_export_weekly_targets_json(cache_dir: Path, config: Any) -> Dict[str, Any]:
    md_path = season_master_plan_md_path(cache_dir)
    if md_path.is_file():
        return weekly_targets_to_json(md_path.read_text(encoding="utf-8"))
    targets = load_weekly_targets(cache_dir, config)
    return {"weeklyTargets": {k: v.to_dict() for k, v in sorted(targets.items())}}
