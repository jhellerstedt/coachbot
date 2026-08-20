"""Deterministic erg session vs prescription checks and weekly zone volume progress."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from weekly_plan_schema import (
    RowingSegment,
    WeeklyPlan,
    _estimate_segment_minutes,
    _zone_bucket,
    parse_weekly_plan,
    session_for_date,
    session_tuple_for_date,
)

_SPLIT_RE = re.compile(r"^(\d{1,2}):(\d{2})(?:[.,](\d))?$")

_PART_TO_PHASE = {
    "warmup": "warm_up",
    "warm_up": "warm_up",
    "main": "main_set",
    "main_set": "main_set",
    "steady": "main_set",
    "interval_block": "main_set",
    "interval": "main_set",
    "intervals": "main_set",
    "cooldown": "cool_down",
    "cool_down": "cool_down",
}

_PHASE_LABELS = {
    "warm_up": "Warm-up",
    "main_set": "Main set",
    "cool_down": "Cool-down",
}


def parse_split_seconds(text: str) -> Optional[float]:
    """Parse M:SS or M:SS.s split string to seconds per 500m."""
    raw = str(text or "").strip().replace(",", ".")
    m = _SPLIT_RE.match(raw)
    if not m:
        return None
    mins = int(m.group(1))
    secs = int(m.group(2))
    frac = int(m.group(3)) if m.group(3) else 0
    return mins * 60.0 + secs + frac / 10.0


def prescribed_erg_section_for_log(
    cache_dir,
    athlete_id: int,
    session_date: date,
) -> Optional[str]:
    """Prescribed erg/rowing day section; prefers athlete DM plan over squad."""
    from generate_training_plan import (
        athlete_plan_for_date,
        plan_for_date,
        plan_record_session_for_date,
    )

    athlete_record = athlete_plan_for_date(cache_dir, athlete_id, session_date)
    if athlete_record:
        section = session_tuple_for_date(
            athlete_record.get("plan_json")
            if isinstance(athlete_record.get("plan_json"), dict)
            else None,
            session_date,
        )
        if section is None:
            from generate_training_plan import session_from_plan

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


def erg_plan_context_for_date(
    cache_dir,
    athlete_id: int,
    d: date,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], bool, Any]:
    """Return (plan_text, plan_json, personalised, squad_record_for_metadata)."""
    from generate_training_plan import athlete_plan_for_date, plan_for_date

    athlete_record = athlete_plan_for_date(cache_dir, athlete_id, d)
    if athlete_record:
        text = str(athlete_record.get("plan_text") or "").strip()
        plan_json = athlete_record.get("plan_json")
        if text or (isinstance(plan_json, dict) and plan_json):
            return (
                text,
                plan_json if isinstance(plan_json, dict) else None,
                True,
                None,
            )
    squad = plan_for_date(cache_dir, d)
    if squad:
        return squad.plan_text, squad.plan_json, False, squad
    return None, None, False, None


def _plan_object(plan_json: Optional[Mapping[str, Any]]) -> Optional[WeeklyPlan]:
    if not plan_json:
        return None
    return parse_weekly_plan(plan_json)


def _normalize_part_role(role: Any) -> str:
    return str(role or "other").strip().lower().replace("-", "_")


def _segment_for_role(
    segments: Sequence[RowingSegment], role: str
) -> Optional[RowingSegment]:
    phase = _PART_TO_PHASE.get(_normalize_part_role(role))
    if not phase:
        return None
    for seg in segments:
        if seg.phase == phase:
            return seg
    return None


def _logged_on_erg(metrics: Mapping[str, Any]) -> bool:
    workout = str(metrics.get("workout_type") or "").lower()
    if workout in ("intervals", "interval", "steady", "steady_state", "other"):
        return True
    parts = _session_parts(metrics)
    if parts:
        return True
    if metrics.get("distance_m") or metrics.get("duration_sec"):
        return True
    return False


def _prescribed_rowing_segments_for_session(
    day,
    metrics: Mapping[str, Any],
) -> Tuple[List[RowingSegment], str]:
    """Pick primary vs erg-alternative prescription for a logged session."""
    if day is None or day.rowing is None:
        return [], "primary"
    alt = day.rowing.erg_alternative
    if (
        day.session_type == "on_water"
        and alt is not None
        and alt.segments
        and _logged_on_erg(metrics)
    ):
        return list(alt.segments), "erg alternative"
    return list(day.rowing.segments), "primary"


def prescribed_warmup_cooldown_minutes(
    cache_dir,
    athlete_id: int,
    session_date: date,
    *,
    metrics: Optional[Mapping[str, Any]] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """Prescribed warm-up / cool-down minutes for the day's rowing plan, if available."""
    _, plan_json, _, _ = erg_plan_context_for_date(cache_dir, athlete_id, session_date)
    plan = _plan_object(plan_json)
    if plan is None:
        return None, None
    day = session_for_date(plan, session_date)
    if day is None or day.rowing is None:
        return None, None
    segs, _ = _prescribed_rowing_segments_for_session(
        day, metrics if metrics is not None else {"workout_type": "intervals"}
    )
    warmup_min: Optional[float] = None
    cooldown_min: Optional[float] = None
    for seg in segs:
        mins = _estimate_segment_minutes(seg.duration)
        if mins <= 0:
            continue
        if seg.phase == "warm_up" and warmup_min is None:
            warmup_min = float(mins)
        elif seg.phase == "cool_down" and cooldown_min is None:
            cooldown_min = float(mins)
    return warmup_min, cooldown_min


def _part_split_sec(part: Mapping[str, Any]) -> Optional[float]:
    raw = part.get("avg_split_500_sec")
    if raw is not None:
        try:
            val = float(raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    fmt = part.get("avg_split_500_fmt")
    if fmt:
        return parse_split_seconds(str(fmt))
    return None


def _part_duration_sec(part: Mapping[str, Any]) -> float:
    raw = part.get("duration_sec")
    if raw is not None:
        try:
            val = float(raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    dist = part.get("distance_m")
    split = _part_split_sec(part)
    if dist is not None and split is not None:
        try:
            return float(dist) * split / 500.0
        except (TypeError, ValueError):
            pass
    return 0.0


def _part_hr(part: Mapping[str, Any]) -> Optional[float]:
    raw = part.get("avg_hr")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _split_verdict(
    split_sec: Optional[float],
    seg: RowingSegment,
    *,
    priority: str,
) -> str:
    if split_sec is None:
        return "split not logged"
    lo = parse_split_seconds(seg.split_min)
    hi = parse_split_seconds(seg.split_max)
    if lo is None or hi is None:
        return "split not comparable"
    if lo <= split_sec <= hi:
        return "split on plan"
    if split_sec > hi:
        delta = split_sec - hi
        verdict = f"split {delta:.1f}s slower than max ({seg.split_max})"
    else:
        delta = lo - split_sec
        verdict = f"split {delta:.1f}s faster than min ({seg.split_min})"
    if priority == "hr":
        return f"{verdict} (HR priority — secondary)"
    return verdict


def _hr_verdict(
    hr: Optional[float],
    seg: RowingSegment,
    *,
    priority: str,
) -> str:
    if hr is None:
        return "HR not logged"
    lo = float(seg.hr_bpm_min)
    hi = float(seg.hr_bpm_max)
    if lo <= hr <= hi:
        return "HR on plan"
    if hr > hi:
        verdict = f"HR {hr:.0f} above max ({seg.hr_bpm_max})"
    else:
        verdict = f"HR {hr:.0f} below min ({seg.hr_bpm_min})"
    if priority == "split":
        return f"{verdict} (split priority — secondary)"
    return verdict


def _overall_part_verdict(
    split_sec: Optional[float],
    hr: Optional[float],
    seg: RowingSegment,
) -> str:
    priority = str(seg.priority or "hr").strip().lower()
    split_v = _split_verdict(split_sec, seg, priority=priority)
    hr_v = _hr_verdict(hr, seg, priority=priority)

    split_on = split_v.startswith("split on plan")
    hr_on = hr_v.startswith("HR on plan")

    if priority == "split":
        if split_on:
            return "on plan" if hr_on or hr is None else f"on plan ({hr_v})"
        return split_v.replace(" (split priority — secondary)", "")
    if hr_on:
        if split_on or split_sec is None:
            return "on plan"
        if "secondary" in split_v:
            return "on plan (HR priority)"
        return f"on plan ({split_v})"
    return hr_v.replace(" (HR priority — secondary)", "")


def _format_part_line(
    label: str,
    part: Mapping[str, Any],
    seg: RowingSegment,
) -> str:
    split_sec = _part_split_sec(part)
    hr = _part_hr(part)
    split_fmt = part.get("avg_split_500_fmt")
    if not split_fmt and split_sec is not None:
        from generate_training_plan import _fmt_split

        split_fmt = _fmt_split(split_sec)
    hr_bit = f", HR {hr:.0f}" if hr is not None else ""
    verdict = _overall_part_verdict(split_sec, hr, seg)
    return (
        f"- {label}: {split_fmt or '?'} "
        f"(prescribed {seg.split_min}–{seg.split_max}, "
        f"HR {seg.hr_bpm_min}–{seg.hr_bpm_max}, priority {seg.priority})"
        f"{hr_bit} — **{verdict}**"
    )


def _session_parts(metrics: Mapping[str, Any]) -> List[Dict[str, Any]]:
    parts = metrics.get("session_parts") or []
    if isinstance(parts, list) and parts:
        return [p for p in parts if isinstance(p, dict)]
    return []


def _whole_session_as_part(metrics: Mapping[str, Any]) -> List[Dict[str, Any]]:
    if metrics.get("distance_m") is None and metrics.get("duration_sec") is None:
        return []
    return [
        {
            "role": "main",
            "distance_m": metrics.get("distance_m"),
            "duration_sec": metrics.get("duration_sec"),
            "avg_split_500_sec": metrics.get("avg_split_500_sec"),
            "avg_split_500_fmt": metrics.get("avg_split_500_fmt"),
            "avg_hr": metrics.get("avg_hr"),
        }
    ]


def _select_part_for_segment(
    parts: Sequence[Mapping[str, Any]], seg: RowingSegment
) -> Optional[Dict[str, Any]]:
    """When multiple parts map to one phase, pick the best match."""
    if not parts:
        return None
    if len(parts) == 1:
        return dict(parts[0])
    if seg.phase == "main_set":
        return dict(
            max(parts, key=lambda p: (_part_duration_sec(p), p.get("distance_m") or 0))
        )
    if seg.phase == "warm_up":
        return dict(max(parts, key=lambda p: _part_split_sec(p) or 0.0))
    if seg.phase == "cool_down":
        return dict(
            min(
                parts,
                key=lambda p: _part_split_sec(p) if _part_split_sec(p) else 9999.0,
            )
        )
    return dict(parts[0])


def compare_erg_session_to_prescription(
    record: Mapping[str, Any],
    plan_json: Optional[Mapping[str, Any]],
    session_date: date,
) -> List[str]:
    """Return bullet lines comparing logged parts to prescribed segments."""
    plan = _plan_object(plan_json)
    if plan is None:
        return []
    day = session_for_date(plan, session_date)
    if day is None or day.rowing is None:
        return []
    metrics = record.get("metrics") or {}
    segments, _source = _prescribed_rowing_segments_for_session(day, metrics)
    parts = _session_parts(metrics) or _whole_session_as_part(metrics)
    parts_by_phase: Dict[str, List[Dict[str, Any]]] = {}
    for part in parts:
        phase = _PART_TO_PHASE.get(_normalize_part_role(part.get("role")))
        if phase:
            parts_by_phase.setdefault(phase, []).append(part)

    lines: List[str] = []

    for seg in segments:
        part = _select_part_for_segment(parts_by_phase.get(seg.phase, []), seg)
        if part is None:
            mins = _estimate_segment_minutes(seg.duration)
            label = _PHASE_LABELS.get(seg.phase, seg.label)
            lines.append(f"- {label}: not logged ({mins}m prescribed)")
            continue
        label = _PHASE_LABELS.get(seg.phase, seg.label or seg.phase)
        dur = _part_duration_sec(part)
        if dur >= 60:
            label = f"{label} ({dur / 60:.0f}m)"
        lines.append(_format_part_line(label, part, seg))

    return lines


def format_erg_session_comparison(
    cache_dir,
    athlete_id: int,
    record: Mapping[str, Any],
    session_date: date,
) -> str:
    """Deterministic session vs prescription summary for coach replies."""
    plan_text, plan_json, personalised, _ = erg_plan_context_for_date(
        cache_dir, athlete_id, session_date
    )
    if not plan_json and not plan_text:
        return ""
    if plan_json is None and plan_text:
        from generate_training_plan import session_from_plan

        # Prose-only plan: still show section header but skip numeric checks.
        section = session_from_plan(plan_text, None, session_date)
        if not section:
            return ""
        source = "personalised plan" if personalised else "squad plan"
        return (
            f"**Prescription check** ({source}):\n"
            f"(Structured segment targets unavailable — compare manually to "
            f"prescribed session.)"
        )

    bullets = compare_erg_session_to_prescription(record, plan_json, session_date)
    if not bullets:
        return ""
    plan = _plan_object(plan_json)
    day = session_for_date(plan, session_date) if plan else None
    metrics = record.get("metrics") or {}
    _, prescription_source = _prescribed_rowing_segments_for_session(day, metrics)
    source = "personalised plan" if personalised else "squad plan"
    header = f"**Prescription check** ({source}"
    if prescription_source == "erg alternative":
        header += ", erg alternative"
    header += "):\n"
    return header + "\n".join(bullets)


def _prescribed_segments_for_day(day) -> List[RowingSegment]:
    """Segments used for weekly volume totals on this plan day."""
    if day is None or day.rowing is None:
        return []
    if day.session_type == "on_water" and day.rowing.erg_alternative:
        return list(day.rowing.erg_alternative.segments)
    return list(day.rowing.segments)


def prescribed_rowing_minutes_by_zone(
    plan_json: Optional[Mapping[str, Any]],
) -> Dict[str, int]:
    """Sum prescribed erg/on-water segment minutes per zone bucket (z2, z5, other)."""
    plan = _plan_object(plan_json)
    out = {"z2": 0, "z5": 0, "other": 0, "total": 0}
    if plan is None:
        return out
    for day in plan.days:
        if day.session_type not in ("erg", "on_water") or day.rowing is None:
            continue
        for seg in _prescribed_segments_for_day(day):
            mins = _estimate_segment_minutes(seg.duration)
            bucket = _zone_bucket(seg.zone_z)
            if bucket not in out:
                bucket = "other"
            out[bucket] += mins
            out["total"] += mins
    return out


def _minutes_from_record(
    record: Mapping[str, Any],
    plan_json: Optional[Mapping[str, Any]],
) -> Dict[str, int]:
    """Assign logged erg minutes to zone buckets using prescribed segment zones."""
    out = {"z2": 0, "z5": 0, "other": 0, "total": 0}
    session_date_raw = record.get("session_date")
    if not session_date_raw:
        return out
    try:
        session_date = date.fromisoformat(str(session_date_raw)[:10])
    except ValueError:
        return out

    plan = _plan_object(plan_json)
    day = session_for_date(plan, session_date) if plan else None
    metrics = record.get("metrics") or {}
    segments, _ = (
        _prescribed_rowing_segments_for_session(day, metrics)
        if day
        else ([], "primary")
    )
    parts = _session_parts(metrics) or _whole_session_as_part(metrics)
    parts_by_phase: Dict[str, List[Dict[str, Any]]] = {}
    for part in parts:
        phase = _PART_TO_PHASE.get(_normalize_part_role(part.get("role")))
        if phase:
            parts_by_phase.setdefault(phase, []).append(part)

    for seg in segments:
        part = _select_part_for_segment(parts_by_phase.get(seg.phase, []), seg)
        if part is None:
            continue
        mins = int(round(_part_duration_sec(part) / 60.0))
        if mins <= 0:
            continue
        bucket = _zone_bucket(seg.zone_z)
        if bucket not in out:
            bucket = "other"
        out[bucket] += mins
        out["total"] += mins
    return out


def _record_total_minutes(record: Mapping[str, Any]) -> int:
    metrics = record.get("metrics") or {}
    parts = _session_parts(metrics)
    if parts:
        total_sec = sum(max(0.0, _part_duration_sec(part)) for part in parts)
        mins = int(round(total_sec / 60.0))
        if mins > 0:
            return mins
    whole = _whole_session_as_part(metrics)
    if whole:
        return int(round(_part_duration_sec(whole[0]) / 60.0))
    return 0


def _record_session_date(record: Mapping[str, Any]) -> Optional[date]:
    raw = record.get("session_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _relative_close(a: Optional[float], b: Optional[float], *, tol: float = 0.15) -> bool:
    if a is None or b is None:
        return True
    if a <= 0 or b <= 0:
        return True
    return abs(a - b) / max(a, b) <= tol


def _records_look_like_same_session(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    lmetrics = left.get("metrics") or {}
    rmetrics = right.get("metrics") or {}
    lmins = _record_total_minutes(left)
    rmins = _record_total_minutes(right)
    ldist = lmetrics.get("distance_m")
    rdist = rmetrics.get("distance_m")
    try:
        ldist_f = float(ldist) if ldist is not None else None
    except (TypeError, ValueError):
        ldist_f = None
    try:
        rdist_f = float(rdist) if rdist is not None else None
    except (TypeError, ValueError):
        rdist_f = None
    return _relative_close(float(lmins or 0), float(rmins or 0), tol=0.05) and _relative_close(
        ldist_f, rdist_f, tol=0.05
    )


def _endurance_activity_minutes(act: Mapping[str, Any]) -> int:
    raw = act.get("moving_time") or act.get("elapsed_time") or act.get("duration_sec")
    if raw is None:
        return 0
    try:
        secs = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, int(round(secs / 60.0)))


def _activity_sport_type(act: Mapping[str, Any]) -> str:
    return str(act.get("sport_type") or act.get("type") or "").strip()


def _is_unprescribed_endurance_activity(act: Mapping[str, Any]) -> bool:
    sport_type = _activity_sport_type(act)
    if not sport_type or sport_type in {"WeightTraining", "Workout", "Crossfit"}:
        return sport_type == "Workout"
    if sport_type in {"Ride", "Run", "VirtualRide", "VirtualRun"}:
        return True
    # Indoor erg rows (trainer=True) are counted via erg scores / prescribed path.
    if sport_type == "Rowing":
        return not bool(act.get("trainer"))
    return False


def _pct(actual: int, prescribed: int) -> Optional[float]:
    if prescribed <= 0:
        return None
    return 100.0 * actual / prescribed


def _fmt_pct_achieved(actual: int, prescribed: int) -> str:
    pct = _pct(actual, prescribed)
    if pct is None:
        return "—"
    return f"{pct:.0f}%"


def _season_zone_targets(cache_dir, week) -> Tuple[Optional[float], Optional[float]]:
    try:
        from season_master_plan import (
            WeekVolumeMetrics,
            parse_season_master_plan_md,
            season_master_plan_md_path,
        )

        md_path = season_master_plan_md_path(cache_dir)
        if not md_path.is_file():
            return None, None
        md_data = parse_season_master_plan_md(md_path.read_text(encoding="utf-8"))
        row = (md_data.get("weeks") or {}).get(week.week_id)
        if not row:
            return None, None
        target = WeekVolumeMetrics.from_dict(row.get("target"))
        return target.z2_percent, target.z5_percent
    except Exception:
        return None, None


def format_last_week_volume_for_dm(
    cache_dir,
    athlete_id: int,
    review_week,
) -> str:
    """Full prescribed + unprescribed volume block for the weekly athlete plan DM."""
    progress = format_week_zone_volume_progress(
        cache_dir,
        athlete_id,
        review_week.week_end,
        include_unprescribed=True,
        include_season_goals=True,
    )
    if not progress.strip():
        return ""
    lines = progress.splitlines()
    bullets = [ln for ln in lines[1:] if ln.strip()] if lines else []
    header = (
        f"**Last week volume** ({review_week.week_start} – {review_week.week_end})"
    )
    if not bullets:
        return header
    return header + "\n" + "\n".join(bullets)


def format_week_zone_volume_progress(
    cache_dir,
    athlete_id: int,
    session_date: date,
    *,
    exclude_id: Optional[str] = None,
    include_unprescribed: bool = False,
    include_season_goals: bool = False,
) -> str:
    """Week-to-date zone volume vs prescribed plan.

    Unprescribed / combined endurance lines are omitted unless
    ``include_unprescribed`` is True (weekly DM after sync).

    Season master-plan zone-mix goals are omitted unless
    ``include_season_goals`` is True (weekly summary DM only — not
    per-session coachbot replies).
    """
    from generate_training_plan import (
        dedupe_erg_scores_by_session_date,
        load_erg_scores_for_week,
        week_for_date,
    )
    from erg_session_merge import load_athlete_index_activities

    _, plan_json, personalised, _ = erg_plan_context_for_date(
        cache_dir, athlete_id, session_date
    )
    if not plan_json:
        return ""

    week = week_for_date(session_date)
    prescribed = prescribed_rowing_minutes_by_zone(plan_json)
    if prescribed["total"] <= 0:
        return ""

    actual = {"z2": 0, "z5": 0, "other": 0, "total": 0}
    unprescribed_total = 0
    counted_activity_ids: Set[int] = set()
    raw_records = [
        rec
        for rec in load_erg_scores_for_week(cache_dir, athlete_id, week)
        if not (exclude_id and str(rec.get("id")) == exclude_id)
    ]
    week_records = dedupe_erg_scores_by_session_date(list(raw_records))
    by_day: Dict[date, List[Dict[str, Any]]] = {}
    for rec in raw_records:
        session_day = _record_session_date(rec)
        if session_day is None:
            continue
        by_day.setdefault(session_day, []).append(dict(rec))

    for rec in week_records:
        session_day = _record_session_date(rec)
        if session_day is None:
            continue
        candidates = by_day.get(session_day) or [dict(rec)]
        ranked: List[Tuple[int, str, Dict[str, Any], Dict[str, int]]] = []
        for cand in candidates:
            chunk = _minutes_from_record(cand, plan_json)
            ranked.append(
                (
                    chunk["total"],
                    str(cand.get("recorded_at") or ""),
                    cand,
                    chunk,
                )
            )
        ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
        _matched_total, _recorded_at, primary, primary_chunk = ranked[0]
        for key in actual:
            actual[key] += primary_chunk[key]
        primary_total = _record_total_minutes(primary)
        if primary_total > primary_chunk["total"]:
            unprescribed_total += primary_total - primary_chunk["total"]
        for _score_total, _other_recorded_at, other, _other_chunk in ranked[1:]:
            if _records_look_like_same_session(primary, other):
                continue
            unprescribed_total += _record_total_minutes(other)

    for act in load_athlete_index_activities(
        cache_dir,
        athlete_id,
        week_start=week.week_start,
        week_end=week.week_end,
    ):
        mins = _endurance_activity_minutes(act)
        if mins <= 0:
            continue
        if _is_unprescribed_endurance_activity(act):
            unprescribed_total += mins
            sid = act.get("id")
            if sid is not None:
                try:
                    counted_activity_ids.add(int(sid))
                except (TypeError, ValueError):
                    pass

    try:
        from suunto_sync import (
            SuuntoCfg,
            list_suunto_endurance_workouts,
            suunto_start_dt,
            suunto_workout_as_activity,
        )
        from strava_erg_hr_plot import activity_local_date

        suunto_cfg = SuuntoCfg(
            enabled=True,
            primary=True,
            suuntool_path=None,
            session_file=None,
            indoor_rowing_activity_ids=frozenset({57}),
            gym_activity_ids=frozenset({23}),
        )
        for rec in list_suunto_endurance_workouts(cache_dir, athlete_id, suunto_cfg):
            start = suunto_start_dt(rec)
            if start is None:
                continue
            local_d = activity_local_date(start)
            if local_d < week.week_start or local_d > week.week_end:
                continue
            act = suunto_workout_as_activity(rec, suunto_cfg)
            strava_id = act.get("strava_activity_id")
            if strava_id is not None:
                try:
                    if int(strava_id) in counted_activity_ids:
                        continue
                except (TypeError, ValueError):
                    pass
            try:
                aid = int(act["id"])
            except (TypeError, ValueError, KeyError):
                aid = None
            if aid is not None and aid in counted_activity_ids:
                continue
            mins = _endurance_activity_minutes(act)
            if mins <= 0:
                continue
            if _is_unprescribed_endurance_activity(act):
                unprescribed_total += mins
                if aid is not None:
                    counted_activity_ids.add(aid)
                if strava_id is not None:
                    try:
                        counted_activity_ids.add(int(strava_id))
                    except (TypeError, ValueError):
                        pass
    except Exception:
        pass

    source = "personalised plan" if personalised else "squad plan"
    lines = [
        f"**Week zone volume** ({source}, through {session_date.isoformat()}):",
        (
            f"- Prescribed rowing logged: {actual['total']} / {prescribed['total']} min "
            f"({_fmt_pct_achieved(actual['total'], prescribed['total'])} of prescribed)"
        ),
    ]
    if include_unprescribed:
        lines.extend(
            [
                f"- Unprescribed endurance: {unprescribed_total} min",
                (
                    f"- Total endurance logged incl. unprescribed: "
                    f"{actual['total'] + unprescribed_total} / {prescribed['total']} min"
                ),
            ]
        )
    for bucket, label in (("z2", "Z2/T2"), ("z5", "Z5/T5"), ("other", "Other zones")):
        if prescribed[bucket] <= 0 and actual[bucket] <= 0:
            continue
        lines.append(
            f"- {label}: {actual[bucket]} / {prescribed[bucket]} min "
            f"({_fmt_pct_achieved(actual[bucket], prescribed[bucket])} of prescribed)"
        )

    if actual["total"] > 0:
        mix_z2 = round(100.0 * actual["z2"] / actual["total"], 1)
        mix_z5 = round(100.0 * actual["z5"] / actual["total"], 1)
        mix_line = f"- Logged zone mix: Z2 {mix_z2:.0f}%"
        if actual["z5"] > 0 or prescribed["z5"] > 0:
            mix_line += f", Z5 {mix_z5:.0f}%"
        if include_season_goals:
            season_z2, season_z5 = _season_zone_targets(cache_dir, week)
            goal_bits = []
            if season_z2 is not None:
                goal_bits.append(f"Z2 ~{season_z2:.0f}%")
            if season_z5 is not None and prescribed["z5"] > 0:
                goal_bits.append(f"Z5 ~{season_z5:.0f}%")
            if goal_bits:
                mix_line += (
                    f" (season week goal: {', '.join(goal_bits)} of erg/row time)"
                )
        lines.append(mix_line)

    return "\n".join(lines)
