"""Merge Zulip erg screenshots with Strava erg activities for longitudinal tracking."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from generate_training_plan import (
    _fmt_split,
    _parse_activity_start,
    _parse_erg_score_session_date,
    _plan_tz,
    activity_local_date,
    activity_metrics_path,
    erg_score_path,
    erg_scores_dir,
    load_activity_metrics,
    load_erg_scores_for_week,
    week_for_date,
)

DEFAULT_ERG_SPORT_TYPES = frozenset({"VirtualRow", "Rowing"})
MERGE_MAX_HOURS = 6.0
MERGE_DISTANCE_TOLERANCE = 0.15  # max relative diff when both distances known
SEGMENT_DISTANCE_TOLERANCE = 0.25
SEGMENT_DURATION_TOLERANCE = 0.28
SEGMENT_DURATION_ABS_SEC = 120


def merged_erg_sessions_dir(cache_dir: Path, athlete_id: int) -> Path:
    return cache_dir / f"athlete_{athlete_id}" / "merged_erg_sessions"


def _is_strava_erg_activity(
    act: dict,
    erg_types: frozenset,
    require_trainer_for_rowing: bool = True,
) -> bool:
    st = act.get("sport_type") or act.get("type") or ""
    if st not in erg_types:
        return False
    if st == "Rowing" and require_trainer_for_rowing and not act.get("trainer"):
        return False
    return True


def _score_session_datetime(rec: Dict[str, Any]) -> Optional[datetime]:
    recorded = _parse_activity_start(rec.get("recorded_at"))
    session_date = _parse_erg_score_session_date(rec)
    if session_date and recorded:
        local_rec = recorded.astimezone(_plan_tz)
        return datetime.combine(
            session_date,
            local_rec.timetz().replace(tzinfo=None),
            tzinfo=_plan_tz,
        )
    if recorded:
        return recorded.astimezone(_plan_tz)
    if session_date:
        return datetime.combine(session_date, datetime.min.time(), tzinfo=_plan_tz)
    return None


def _strava_distance_m(act: dict, detail: dict, metrics: Optional[dict]) -> Optional[float]:
    for src in (act, detail):
        for key in ("distance", "total_distance"):
            val = src.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
    return None


def _score_distance_m(rec: Dict[str, Any]) -> Optional[float]:
    metrics = rec.get("metrics") or {}
    val = metrics.get("distance_m")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _distances_compatible(
    a: Optional[float],
    b: Optional[float],
    *,
    tolerance: float = MERGE_DISTANCE_TOLERANCE,
) -> bool:
    if a is None or b is None:
        return True
    if a <= 0 or b <= 0:
        return True
    rel = abs(a - b) / max(a, b)
    return rel <= tolerance


def _durations_compatible(
    a: Optional[float],
    b: Optional[float],
    *,
    tolerance: float = SEGMENT_DURATION_TOLERANCE,
    abs_sec: float = SEGMENT_DURATION_ABS_SEC,
) -> bool:
    if a is None or b is None:
        return True
    if a <= 0 or b <= 0:
        return True
    rel = abs(a - b) / max(a, b)
    return rel <= tolerance or abs(a - b) <= abs_sec


def _hours_apart(a: datetime, b: datetime) -> float:
    return abs((a.astimezone(timezone.utc) - b.astimezone(timezone.utc)).total_seconds()) / 3600.0


def sessions_match(
    score_rec: Dict[str, Any],
    *,
    strava_start: datetime,
    strava_distance_m: Optional[float],
) -> bool:
    score_dt = _score_session_datetime(score_rec)
    if score_dt is None:
        return False
    if _hours_apart(score_dt, strava_start) <= MERGE_MAX_HOURS:
        return _distances_compatible(_score_distance_m(score_rec), strava_distance_m)
    score_day = _parse_erg_score_session_date(score_rec) or activity_local_date(score_dt)
    strava_day = activity_local_date(strava_start)
    if score_day == strava_day:
        return _distances_compatible(_score_distance_m(score_rec), strava_distance_m)
    return False


def _score_match_days(score_rec: Dict[str, Any]) -> Set[date]:
    days: Set[date] = set()
    session_date = _parse_erg_score_session_date(score_rec)
    if session_date is not None:
        days.add(session_date)
    recorded = _parse_activity_start(score_rec.get("recorded_at"))
    if recorded is not None:
        local_d = activity_local_date(recorded)
        days.add(local_d)
        days.add(local_d - timedelta(days=1))
    return days


def _metrics_to_segment(
    metrics: Mapping[str, Any],
    *,
    screenshot_index: Optional[int] = None,
    label: Optional[str] = None,
) -> Dict[str, Any]:
    seg: Dict[str, Any] = {
        "distance_m": metrics.get("distance_m"),
        "duration_sec": metrics.get("duration_sec"),
        "avg_split_500_sec": metrics.get("avg_split_500_sec"),
        "avg_hr": metrics.get("avg_hr"),
        "label": label,
        "screenshot_index": screenshot_index,
    }
    if seg["duration_sec"] is None and metrics.get("intervals"):
        total = 0.0
        for iv in metrics.get("intervals") or []:
            try:
                total += float(iv.get("time_sec") or 0)
            except (TypeError, ValueError):
                pass
        if total > 0:
            seg["duration_sec"] = total
    return seg


def _score_match_segments(score_rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per-screenshot or per-interval pieces used for multi-Suunto matching."""
    segments: List[Dict[str, Any]] = []
    for ex in score_rec.get("screenshot_extractions") or []:
        metrics = ex.get("metrics")
        if not isinstance(metrics, dict):
            continue
        if metrics.get("distance_m") is None and metrics.get("duration_sec") is None:
            continue
        segments.append(
            _metrics_to_segment(
                metrics,
                screenshot_index=ex.get("index"),
                label=str(ex.get("summary") or "")[:80] or None,
            )
        )
    if len(segments) >= 2:
        return segments

    metrics = score_rec.get("metrics") or {}
    for part in metrics.get("session_parts") or []:
        if not isinstance(part, dict):
            continue
        if part.get("distance_m") is None and part.get("duration_sec") is None:
            continue
        segments.append(
            _metrics_to_segment(
                part,
                label=str(part.get("label") or part.get("role") or "") or None,
            )
        )
    if len(segments) >= 2:
        return segments

    for iv in metrics.get("intervals") or []:
        if not isinstance(iv, dict):
            continue
        segments.append(
            {
                "distance_m": iv.get("distance_m"),
                "duration_sec": iv.get("time_sec"),
                "avg_split_500_sec": iv.get("split_500_sec"),
                "label": iv.get("label"),
                "screenshot_index": None,
            }
        )
    return segments if len(segments) >= 2 else []


def _row_distance_m(row: Mapping[str, Any]) -> Optional[float]:
    act = row.get("act") or {}
    detail = row.get("detail") or {}
    metrics = row.get("metrics") or {}
    dist = _strava_distance_m(act, detail, metrics)
    if dist is not None:
        return dist
    raw = act.get("distance")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return None


def _row_duration_sec(row: Mapping[str, Any]) -> Optional[float]:
    act = row.get("act") or {}
    detail = row.get("detail") or {}
    for src in (act, detail):
        for key in ("moving_time", "elapsed_time", "duration", "totalTime"):
            val = src.get(key)
            if val is None:
                continue
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


def _segment_matches_row(
    segment: Mapping[str, Any],
    row: Mapping[str, Any],
    match_days: Set[date],
) -> bool:
    start = row.get("start_dt")
    if not isinstance(start, datetime):
        return False
    if activity_local_date(start) not in match_days:
        return False
    seg_dist = segment.get("distance_m")
    row_dist = _row_distance_m(row)
    if seg_dist is not None and row_dist is not None:
        try:
            if not _distances_compatible(
                float(seg_dist),
                float(row_dist),
                tolerance=SEGMENT_DISTANCE_TOLERANCE,
            ):
                return False
        except (TypeError, ValueError):
            return False
    seg_dur = segment.get("duration_sec")
    row_dur = _row_duration_sec(row)
    if seg_dur is not None and row_dur is not None:
        try:
            if not _durations_compatible(float(seg_dur), float(row_dur)):
                return False
        except (TypeError, ValueError):
            return False
    if seg_dist is not None and row_dist is not None:
        return True
    if seg_dur is not None and row_dur is not None:
        return True
    return False


def _match_score_segments_to_rows(
    score_rec: Dict[str, Any],
    strava_rows: Sequence[Dict[str, Any]],
    used_strava: Set[int],
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Match a multi-screenshot score to several Suunto/Strava erg rows."""
    segments = _score_match_segments(score_rec)
    if len(segments) < 2:
        return []

    match_days = _score_match_days(score_rec)
    candidates = [
        row
        for row in strava_rows
        if int(row["activity_id"]) not in used_strava
        and row.get("suunto_key")
        and isinstance(row.get("start_dt"), datetime)
        and activity_local_date(row["start_dt"]) in match_days
    ]
    if len(candidates) < len(segments):
        return []

    def _segment_sort_key(seg: Mapping[str, Any]) -> Tuple[int, float]:
        idx = seg.get("screenshot_index")
        idx_val = int(idx) if idx is not None else 999
        dur = seg.get("duration_sec")
        try:
            dur_val = float(dur) if dur is not None else 0.0
        except (TypeError, ValueError):
            dur_val = 0.0
        return (idx_val, -dur_val)

    ordered_segments = sorted(segments, key=_segment_sort_key)
    ordered_rows = sorted(candidates, key=lambda r: r["start_dt"])
    if len(ordered_segments) == len(ordered_rows):
        pairs = list(zip(ordered_segments, ordered_rows))
        strict = sum(
            1 for seg, row in pairs if _segment_matches_row(seg, row, match_days)
        )
        if strict >= max(2, len(pairs) - 1):
            return pairs

    matches: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    used_local: Set[int] = set()
    for seg in ordered_segments:
        best: Optional[Dict[str, Any]] = None
        best_rank = -1e18
        for row in candidates:
            aid = int(row["activity_id"])
            if aid in used_local:
                continue
            if not _segment_matches_row(seg, row, match_days):
                continue
            seg_dist = float(seg.get("distance_m") or 0)
            row_dist = float(_row_distance_m(row) or 0)
            seg_dur = float(seg.get("duration_sec") or 0)
            row_dur = float(_row_duration_sec(row) or 0)
            rank = -(abs(seg_dist - row_dist) + 0.25 * abs(seg_dur - row_dur))
            if rank > best_rank:
                best_rank = rank
                best = row
        if best is None:
            continue
        used_local.add(int(best["activity_id"]))
        matches.append((seg, best))

    return matches if len(matches) >= 2 else []


def _load_strava_index_erg_rows(
    cache_dir: Path,
    athlete_id: int,
    erg_types: frozenset,
    require_trainer_for_rowing: bool,
    *,
    week_start: Optional[date] = None,
    week_end: Optional[date] = None,
) -> List[Dict[str, Any]]:
    index_path = cache_dir / f"athlete_{athlete_id}" / "index.json"
    if not index_path.is_file():
        return []
    try:
        idx = json.loads(index_path.read_text())
    except json.JSONDecodeError:
        return []
    metrics_dir = cache_dir / f"athlete_{athlete_id}" / "metrics"
    details_dir = cache_dir / f"athlete_{athlete_id}" / "activity_details"
    rows: List[Dict[str, Any]] = []
    for act in idx.get("activities", []):
        if not _is_strava_erg_activity(act, erg_types, require_trainer_for_rowing):
            continue
        start = _parse_activity_start(act.get("start_date"))
        if start is None:
            continue
        if week_start is not None and week_end is not None:
            local_d = activity_local_date(start)
            if local_d < week_start or local_d > week_end:
                continue
        aid = int(act["id"])
        detail: dict = {}
        detail_path = details_dir / f"{aid}.json"
        if detail_path.is_file():
            try:
                detail = json.loads(detail_path.read_text())
            except json.JSONDecodeError:
                detail = {}
        metrics = load_activity_metrics(metrics_dir / f"{aid}.json")
        rows.append(
            {
                "activity_id": aid,
                "act": act,
                "detail": detail,
                "metrics": metrics,
                "start_dt": start,
            }
        )
    return rows


def _load_erg_session_rows(
    cache_dir: Path,
    athlete_id: int,
    erg_types: frozenset,
    require_trainer_for_rowing: bool,
    *,
    week_start: Optional[date] = None,
    week_end: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Suunto-first erg rows for merge; Strava-only legacy rows appended when unmatched."""
    from suunto_sync import (
        SuuntoCfg,
        list_suunto_erg_workouts,
        stable_activity_id,
        suunto_start_dt,
        suunto_workout_as_activity,
        summarize_suunto_rowing_metrics_by_key,
    )

    suunto_cfg = SuuntoCfg(
        enabled=True,
        primary=True,
        suuntool_path=None,
        session_file=None,
        indoor_rowing_activity_ids=frozenset({57}),
        gym_activity_ids=frozenset({23}),
    )
    metrics_dir = cache_dir / f"athlete_{athlete_id}" / "metrics"
    rows: List[Dict[str, Any]] = []
    seen_aids: Set[int] = set()
    matched_strava: Set[int] = set()

    for rec in list_suunto_erg_workouts(cache_dir, athlete_id, suunto_cfg):
        start = suunto_start_dt(rec)
        if start is None:
            continue
        if week_start is not None and week_end is not None:
            local_d = activity_local_date(start)
            if local_d < week_start or local_d > week_end:
                continue
        key = str(rec.get("key") or "")
        act = suunto_workout_as_activity(rec, suunto_cfg)
        aid = int(act["id"])
        if aid in seen_aids:
            continue
        seen_aids.add(aid)
        strava_id = rec.get("strava_activity_id")
        if strava_id is not None:
            matched_strava.add(int(strava_id))
        metrics: dict = {}
        rowing = summarize_suunto_rowing_metrics_by_key(cache_dir, athlete_id, key)
        if rowing:
            metrics["rowing"] = rowing
        elif strava_id is not None:
            metrics = load_activity_metrics(metrics_dir / f"{int(strava_id)}.json")
        rows.append(
            {
                "activity_id": aid,
                "suunto_key": key,
                "act": act,
                "detail": {},
                "metrics": metrics,
                "start_dt": start,
            }
        )

    for row in _load_strava_index_erg_rows(
        cache_dir,
        athlete_id,
        erg_types,
        require_trainer_for_rowing,
        week_start=week_start,
        week_end=week_end,
    ):
        aid = int(row["activity_id"])
        if aid in seen_aids or aid in matched_strava:
            continue
        rows.append(row)
    return rows


def merge_erg_sources(
    score_rec: Optional[Dict[str, Any]],
    strava_row: Optional[Dict[str, Any]],
    *,
    athlete_id: int,
    athlete_label: str = "",
    cache_dir: Optional[Path] = None,
    segment_metrics: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Combine screenshot metrics (splits/intervals) with Suunto/Strava rowing/HR."""
    sm: Dict[str, Any] = dict(segment_metrics or (score_rec or {}).get("metrics") or {})
    act = (strava_row or {}).get("act") or {}
    detail = (strava_row or {}).get("detail") or {}
    strava_metrics = (strava_row or {}).get("metrics") or {}
    rowing = dict(strava_metrics.get("rowing") or {})
    suunto_key: Optional[str] = (strava_row or {}).get("suunto_key")
    if cache_dir is not None and strava_row:
        try:
            from suunto_sync import (
                summarize_suunto_rowing_metrics,
                summarize_suunto_rowing_metrics_by_key,
                suunto_workout_record,
            )

            aid = int(strava_row["activity_id"])
            if suunto_key:
                suunto_rowing = summarize_suunto_rowing_metrics_by_key(
                    cache_dir, athlete_id, suunto_key
                )
            else:
                suunto_rowing = summarize_suunto_rowing_metrics(
                    cache_dir, athlete_id, aid
                )
            if suunto_rowing:
                rowing = {**rowing, **suunto_rowing}
                suunto_key = suunto_rowing.get("suunto_key") or suunto_key
            elif not suunto_key:
                rec = suunto_workout_record(cache_dir, athlete_id, aid)
                if rec:
                    suunto_key = rec.get("key")
        except Exception:
            pass

    strava_start = (strava_row or {}).get("start_dt")
    session_date_raw = sm.get("session_date")
    if session_date_raw:
        session_date = str(session_date_raw)
    elif strava_start is not None:
        session_date = activity_local_date(strava_start).isoformat()
    else:
        session_date = None

    distance = sm.get("distance_m")
    if distance is None and strava_row:
        distance = _strava_distance_m(act, detail, strava_metrics)

    duration = sm.get("duration_sec")
    if duration is None and strava_row:
        for key in ("moving_time", "elapsed_time"):
            val = act.get(key) or detail.get(key)
            if val is not None:
                try:
                    duration = int(val)
                    break
                except (TypeError, ValueError):
                    pass

    avg_split_sec = sm.get("avg_split_500_sec")
    avg_split_fmt = sm.get("avg_split_500_fmt")
    if avg_split_sec is None and rowing.get("median_split_500") is not None:
        avg_split_sec = rowing.get("median_split_500")
    if not avg_split_fmt and avg_split_sec is not None:
        try:
            avg_split_fmt = _fmt_split(float(avg_split_sec))
        except (TypeError, ValueError):
            pass
    if not avg_split_fmt and rowing.get("median_split_500_fmt"):
        avg_split_fmt = rowing.get("median_split_500_fmt")

    avg_hr = sm.get("avg_hr")
    if avg_hr is None and rowing.get("median_hr") is not None:
        avg_hr = rowing.get("median_hr")

    sources: List[str] = []
    if score_rec:
        sources.append("zulip_screenshot")
    if suunto_key:
        sources.append("suunto")
    if strava_row:
        sources.append("strava")

    merged_id = str(uuid.uuid4())
    if score_rec and strava_row:
        merged_id = f"{score_rec.get('id', 'z')}-{strava_row['activity_id']}"
    elif score_rec:
        merged_id = str(score_rec.get("id"))
    elif strava_row:
        sk = strava_row.get("suunto_key")
        if sk:
            merged_id = f"suunto-{sk}"
        else:
            merged_id = f"strava-{strava_row['activity_id']}"

    return {
        "id": merged_id,
        "athlete_id": athlete_id,
        "athlete_label": athlete_label or (score_rec or {}).get("athlete_label", ""),
        "session_date": session_date,
        "session_start": (
            strava_start.astimezone(timezone.utc).isoformat()
            if strava_start is not None
            else (score_rec or {}).get("recorded_at")
        ),
        "sources": sources,
        "zulip_score_id": (score_rec or {}).get("id"),
        "strava_activity_id": (strava_row or {}).get("activity_id"),
        "suunto_workout_key": suunto_key or (strava_row or {}).get("suunto_key"),
        "activity_name": act.get("name") or detail.get("name"),
        "metrics": {
            "distance_m": distance,
            "duration_sec": duration,
            "avg_split_500_sec": avg_split_sec,
            "avg_split_500_fmt": avg_split_fmt,
            "avg_hr": avg_hr,
            "stroke_rate": sm.get("stroke_rate"),
            "workout_type": sm.get("workout_type") or "erg",
            "intervals": sm.get("intervals") or [],
        },
        "rowing": rowing,
        "zulip_summary": (score_rec or {}).get("summary"),
        "merged_at": datetime.now(timezone.utc).isoformat(),
        "merge_version": "2",
    }


def _build_multi_suunto_merged_sessions(
    score_rec: Dict[str, Any],
    matches: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]],
    *,
    athlete_id: int,
    athlete_label: str,
    cache_dir: Path,
) -> List[Dict[str, Any]]:
    segment_links: List[Dict[str, Any]] = []
    suunto_keys: List[str] = []
    for seg, row in matches:
        key = str(row.get("suunto_key") or "")
        if key and key not in suunto_keys:
            suunto_keys.append(key)
        segment_links.append(
            {
                "label": seg.get("label"),
                "screenshot_index": seg.get("screenshot_index"),
                "suunto_workout_key": key or None,
                "strava_activity_id": row.get("activity_id"),
                "distance_m": seg.get("distance_m"),
                "duration_sec": seg.get("duration_sec"),
            }
        )

    composite = merge_erg_sources(
        score_rec,
        None,
        athlete_id=athlete_id,
        athlete_label=athlete_label,
        cache_dir=cache_dir,
    )
    composite["id"] = str(score_rec.get("id"))
    composite["suunto_workout_keys"] = suunto_keys
    composite["segment_links"] = segment_links
    if suunto_keys and "suunto" not in (composite.get("sources") or []):
        composite["sources"] = list(dict.fromkeys([*(composite.get("sources") or []), "suunto"]))

    out = [composite]
    score_metrics = score_rec.get("metrics") or {}
    for seg, row in matches:
        key = str(row.get("suunto_key") or "")
        seg_metrics = {
            "session_date": score_metrics.get("session_date"),
            "distance_m": seg.get("distance_m"),
            "duration_sec": seg.get("duration_sec"),
            "avg_split_500_sec": seg.get("avg_split_500_sec"),
            "avg_hr": seg.get("avg_hr") or score_metrics.get("avg_hr"),
            "workout_type": score_metrics.get("workout_type") or "erg",
            "intervals": [],
        }
        child = merge_erg_sources(
            score_rec,
            row,
            athlete_id=athlete_id,
            athlete_label=athlete_label,
            cache_dir=cache_dir,
            segment_metrics=seg_metrics,
        )
        child["id"] = f"{score_rec.get('id')}-{key or row.get('activity_id')}"
        child["parent_zulip_score_id"] = score_rec.get("id")
        child["segment_label"] = seg.get("label")
        child["screenshot_index"] = seg.get("screenshot_index")
        out.append(child)
    return out


def _load_all_erg_scores_for_athlete(
    cache_dir: Path,
    athlete_id: int,
) -> List[Dict[str, Any]]:
    scores_dir = erg_scores_dir(cache_dir, athlete_id)
    if not scores_dir.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for path in scores_dir.glob("*.json"):
        try:
            out.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            continue
    out.sort(
        key=lambda r: (
            str(r.get("session_date") or ""),
            str(r.get("recorded_at") or ""),
        )
    )
    return out


def _best_strava_match(
    score_rec: Dict[str, Any],
    strava_rows: Sequence[Dict[str, Any]],
    used_strava: Set[int],
) -> Optional[Dict[str, Any]]:
    score_dt = _score_session_datetime(score_rec)
    if score_dt is None:
        return None
    best: Optional[Dict[str, Any]] = None
    best_hours = MERGE_MAX_HOURS + 1.0
    for row in strava_rows:
        aid = int(row["activity_id"])
        if aid in used_strava:
            continue
        dist = _strava_distance_m(row["act"], row["detail"], row.get("metrics"))
        if not sessions_match(
            score_rec,
            strava_start=row["start_dt"],
            strava_distance_m=dist,
        ):
            continue
        hours = _hours_apart(score_dt, row["start_dt"])
        if hours < best_hours:
            best_hours = hours
            best = row
    return best


def build_merged_erg_sessions_for_athlete(
    cache_dir: Path,
    athlete_id: int,
    *,
    athlete_label: str = "",
    erg_types: frozenset = DEFAULT_ERG_SPORT_TYPES,
    require_trainer_for_rowing: bool = True,
    week_start: Optional[date] = None,
    week_end: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Match and merge screenshot + Strava erg sessions for one athlete."""
    if week_start is not None and week_end is not None:
        scores = load_erg_scores_for_week(
            cache_dir, athlete_id, week_for_date(week_start)
        )
    else:
        scores = _load_all_erg_scores_for_athlete(cache_dir, athlete_id)
    strava_rows = _load_erg_session_rows(
        cache_dir,
        athlete_id,
        erg_types,
        require_trainer_for_rowing,
        week_start=week_start,
        week_end=week_end,
    )

    used_strava: Set[int] = set()
    merged: List[Dict[str, Any]] = []

    for score_rec in scores:
        multi = _match_score_segments_to_rows(score_rec, strava_rows, used_strava)
        if multi:
            label = athlete_label or str(score_rec.get("athlete_label") or "")
            for _, row in multi:
                used_strava.add(int(row["activity_id"]))
            merged.extend(
                _build_multi_suunto_merged_sessions(
                    score_rec,
                    multi,
                    athlete_id=athlete_id,
                    athlete_label=label,
                    cache_dir=cache_dir,
                )
            )
            continue

        best = _best_strava_match(score_rec, strava_rows, used_strava)
        if best is not None:
            used_strava.add(int(best["activity_id"]))
        merged.append(
            merge_erg_sources(
                score_rec,
                best,
                athlete_id=athlete_id,
                athlete_label=athlete_label or str(score_rec.get("athlete_label") or ""),
                cache_dir=cache_dir,
            )
        )

    for row in strava_rows:
        aid = int(row["activity_id"])
        if aid in used_strava:
            continue
        merged.append(
            merge_erg_sources(
                None,
                row,
                athlete_id=athlete_id,
                athlete_label=athlete_label,
                cache_dir=cache_dir,
            )
        )

    merged.sort(
        key=lambda m: str(m.get("session_start") or m.get("session_date") or ""),
        reverse=True,
    )
    return merged


def save_merged_erg_sessions(
    cache_dir: Path,
    athlete_id: int,
    sessions: Sequence[Dict[str, Any]],
) -> None:
    out_dir = merged_erg_sessions_dir(cache_dir, athlete_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    for path in out_dir.glob("*.json"):
        path.unlink()
    for session in sessions:
        sid = str(session.get("id") or uuid.uuid4())
        session["id"] = sid
        (out_dir / f"{sid}.json").write_text(json.dumps(session, indent=2))


def _write_link_back(
    path: Path,
    updates: Dict[str, Any],
    *,
    label: str,
) -> None:
    """Patch a source JSON with merge backlinks; skip unreadable/unwritable files."""
    if not path.is_file():
        return
    try:
        rec = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Merged erg link: could not read {label} ({path}): {e}", file=sys.stderr)
        return
    rec.update(updates)
    try:
        path.write_text(json.dumps(rec, indent=2))
    except OSError as e:
        print(
            f"Merged erg link: could not update {label} ({path}): {e}. "
            "If coach_bot runs in Docker as root, fix cache ownership: "
            f"sudo chown -R $USER {path.parent.parent.parent}",
            file=sys.stderr,
        )


def _link_sources(
    cache_dir: Path,
    athlete_id: int,
    session: Dict[str, Any],
) -> None:
    zid = session.get("zulip_score_id")
    aid = session.get("strava_activity_id")
    merged_at = session.get("merged_at")
    if zid:
        updates: Dict[str, Any] = {"merged_at": merged_at}
        keys = session.get("suunto_workout_keys") or []
        if keys:
            updates["merged_suunto_workout_keys"] = keys
        sk = session.get("suunto_workout_key")
        if sk:
            updates["merged_suunto_workout_key"] = sk
            updates["merged_strava_activity_id"] = aid
        _write_link_back(
            erg_score_path(cache_dir, athlete_id, str(zid)),
            updates,
            label="zulip erg score",
        )
    if aid is not None:
        _write_link_back(
            activity_metrics_path(
                cache_dir / f"athlete_{athlete_id}" / "metrics", int(aid)
            ),
            {"merged_zulip_score_id": zid, "merged_at": merged_at},
            label="activity metrics",
        )


def rebuild_merged_erg_sessions_for_athlete(
    cache_dir: Path,
    athlete_id: int,
    *,
    athlete_label: str = "",
    erg_types: frozenset = DEFAULT_ERG_SPORT_TYPES,
    require_trainer_for_rowing: bool = True,
) -> List[Dict[str, Any]]:
    """Rebuild merged session cache and cross-link source records."""
    sessions = build_merged_erg_sessions_for_athlete(
        cache_dir,
        athlete_id,
        athlete_label=athlete_label,
        erg_types=erg_types,
        require_trainer_for_rowing=require_trainer_for_rowing,
    )
    save_merged_erg_sessions(cache_dir, athlete_id, sessions)
    for session in sessions:
        _link_sources(cache_dir, athlete_id, session)
    return sessions


def load_merged_erg_sessions_for_athlete(
    cache_dir: Path,
    athlete_id: int,
    *,
    limit: int = 24,
    week_start: Optional[date] = None,
    week_end: Optional[date] = None,
) -> List[Dict[str, Any]]:
    out_dir = merged_erg_sessions_dir(cache_dir, athlete_id)
    if not out_dir.is_dir():
        return []
    sessions: List[Dict[str, Any]] = []
    for path in out_dir.glob("*.json"):
        try:
            sessions.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            continue
    if week_start and week_end:
        filtered: List[Dict[str, Any]] = []
        for s in sessions:
            raw = s.get("session_date")
            if not raw:
                continue
            try:
                d = date.fromisoformat(str(raw))
            except ValueError:
                continue
            if week_start <= d <= week_end:
                filtered.append(s)
        sessions = filtered
    sessions.sort(
        key=lambda m: str(m.get("session_start") or m.get("session_date") or ""),
        reverse=True,
    )
    return sessions[:limit]


def format_merged_erg_session_line(session: Dict[str, Any]) -> str:
    metrics = session.get("metrics") or {}
    label = session.get("athlete_label") or f"athlete_{session.get('athlete_id')}"
    session_day = session.get("session_date") or "?"
    sources = "+".join(session.get("sources") or [])
    parts = [str(session_day), str(metrics.get("workout_type") or "erg")]
    if metrics.get("distance_m") is not None:
        parts.append(f"{metrics['distance_m']:g} m")
    split_fmt = metrics.get("avg_split_500_fmt")
    if split_fmt:
        parts.append(f"avg {split_fmt}")
    if metrics.get("avg_hr") is not None:
        parts.append(f"HR {metrics['avg_hr']} bpm")
    intervals = metrics.get("intervals") or []
    if intervals:
        parts.append(f"{len(intervals)} intervals (screenshot)")
    rowing = session.get("rowing") or {}
    if rowing.get("n_points"):
        src = rowing.get("source") or "strava"
        parts.append(f"{src} HR curve ({rowing['n_points']} pts)")
    line = f"- {session_day} ({label}) [{sources}]: {', '.join(parts[1:])}"
    summary = str(session.get("zulip_summary") or "").strip()
    if summary:
        line += f" — {summary[:200]}"
    return line


def format_merged_erg_scores_summary(
    cache_dir: Path,
    week: Any,
    *,
    erg_types: frozenset = DEFAULT_ERG_SPORT_TYPES,
    require_trainer_for_rowing: bool = True,
) -> str:
    """Merged erg sessions for adherence (no duplicate Strava erg lines)."""
    from generate_training_plan import iter_cached_athlete_ids

    lines: List[str] = []
    for athlete_id in iter_cached_athlete_ids(cache_dir):
        sessions = build_merged_erg_sessions_for_athlete(
            cache_dir,
            athlete_id,
            erg_types=erg_types,
            require_trainer_for_rowing=require_trainer_for_rowing,
            week_start=week.week_start,
            week_end=week.week_end,
        )
        for session in sessions:
            lines.append(format_merged_erg_session_line(session))
    if not lines:
        return ""
    return "\n".join(lines)


def load_athlete_index_activities(
    cache_dir: Path,
    athlete_id: int,
    *,
    week_start: Optional[date] = None,
    week_end: Optional[date] = None,
) -> List[dict]:
    """Activities from one athlete's Strava index, optionally filtered to a week."""
    index_path = cache_dir / f"athlete_{athlete_id}" / "index.json"
    if not index_path.is_file():
        return []
    try:
        idx = json.loads(index_path.read_text())
    except json.JSONDecodeError:
        return []
    out: List[dict] = []
    for act in idx.get("activities", []):
        start = _parse_activity_start(act.get("start_date"))
        if start is None:
            continue
        if week_start is not None and week_end is not None:
            local_d = activity_local_date(start)
            if local_d < week_start or local_d > week_end:
                continue
        out.append(act)
    return out


def athlete_has_week_training_log(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    week: Any,
    *,
    erg_types: frozenset = DEFAULT_ERG_SPORT_TYPES,
    require_trainer_for_rowing: bool = True,
) -> bool:
    """True if the athlete logged any Strava or erg (merged/screenshot) sessions in week."""
    if load_athlete_index_activities(
        cache_dir,
        athlete_id,
        week_start=week.week_start,
        week_end=week.week_end,
    ):
        return True
    sessions = build_merged_erg_sessions_for_athlete(
        cache_dir,
        athlete_id,
        athlete_label=athlete_label,
        erg_types=erg_types,
        require_trainer_for_rowing=require_trainer_for_rowing,
        week_start=week.week_start,
        week_end=week.week_end,
    )
    return bool(sessions)


def format_athlete_week_training_log(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    week: Any,
    activity_details: Mapping[int, dict],
    activity_metrics: Mapping[int, Dict[str, Any]],
    *,
    erg_types: frozenset = DEFAULT_ERG_SPORT_TYPES,
    require_trainer_for_rowing: bool = True,
) -> str:
    """Per-athlete adherence log for one review week."""
    from generate_training_plan import format_week_activities_summary

    acts = load_athlete_index_activities(
        cache_dir,
        athlete_id,
        week_start=week.week_start,
        week_end=week.week_end,
    )
    merged_strava_ids: Set[int] = set()
    erg_lines: List[str] = []
    sessions = build_merged_erg_sessions_for_athlete(
        cache_dir,
        athlete_id,
        athlete_label=athlete_label,
        erg_types=erg_types,
        require_trainer_for_rowing=require_trainer_for_rowing,
        week_start=week.week_start,
        week_end=week.week_end,
    )
    for session in sessions:
        erg_lines.append(format_merged_erg_session_line(session))
        aid = session.get("strava_activity_id")
        if aid is not None:
            merged_strava_ids.add(int(aid))
    non_erg_acts = [
        a
        for a in acts
        if int(a["id"]) not in merged_strava_ids
        and not _is_strava_erg_activity(a, erg_types, require_trainer_for_rowing)
    ]
    blocks: List[str] = []
    strava_block = format_week_activities_summary(
        non_erg_acts, dict(activity_details), dict(activity_metrics)
    )
    if strava_block.strip() and "(no Strava" not in strava_block:
        blocks.append(strava_block)
    if erg_lines:
        blocks.append("--- Erg sessions (merged Zulip + Strava) ---")
        blocks.extend(erg_lines)
    if not blocks:
        return f"(no training activities logged for {athlete_label} this week)"
    return "\n\n".join(blocks)


def format_week_training_log(
    activities: Sequence[dict],
    details_by_id: Mapping[int, dict],
    metrics_by_id: Mapping[int, Dict[str, Any]],
    cache_dir: Path,
    week: Any,
    *,
    erg_types: frozenset = DEFAULT_ERG_SPORT_TYPES,
    require_trainer_for_rowing: bool = True,
) -> str:
    """
    Adherence log: non-erg Strava activities + merged erg sessions (deduped).
    """
    from generate_training_plan import (
        format_week_activities_summary,
        iter_cached_athlete_ids,
    )

    merged_strava_ids: Set[int] = set()
    erg_lines: List[str] = []
    for athlete_id in iter_cached_athlete_ids(cache_dir):
        sessions = build_merged_erg_sessions_for_athlete(
            cache_dir,
            athlete_id,
            erg_types=erg_types,
            require_trainer_for_rowing=require_trainer_for_rowing,
            week_start=week.week_start,
            week_end=week.week_end,
        )
        for session in sessions:
            erg_lines.append(format_merged_erg_session_line(session))
            aid = session.get("strava_activity_id")
            if aid is not None:
                merged_strava_ids.add(int(aid))

    non_erg_acts = [
        a
        for a in activities
        if int(a["id"]) not in merged_strava_ids
        and not _is_strava_erg_activity(a, erg_types, require_trainer_for_rowing)
    ]
    blocks: List[str] = []
    strava_block = format_week_activities_summary(
        non_erg_acts, dict(details_by_id), metrics_by_id
    )
    if strava_block.strip() and "(no Strava" not in strava_block:
        blocks.append(strava_block)
    if erg_lines:
        blocks.append("--- Merged erg sessions (Zulip screenshot + Strava) ---")
        blocks.extend(erg_lines)
    if not blocks:
        return "(no training activities logged in this week)"
    return "\n\n".join(blocks)


def format_merged_session_detail(session: Dict[str, Any]) -> str:
    """Structured text for coaching (screenshot intervals + Strava HR/streams)."""
    metrics = session.get("metrics") or {}
    lines = [
        f"Session date: {session.get('session_date') or '?'}",
        f"Sources: {', '.join(session.get('sources') or [])}",
        f"Workout type: {metrics.get('workout_type') or 'erg'}",
    ]
    if session.get("strava_activity_id"):
        lines.append(f"Strava activity: {session['strava_activity_id']}")
    if metrics.get("distance_m") is not None:
        lines.append(f"Distance: {metrics['distance_m']} m")
    if metrics.get("duration_sec") is not None:
        lines.append(f"Work duration: {metrics['duration_sec']} s")
    if metrics.get("avg_split_500_fmt"):
        lines.append(f"Avg split: {metrics['avg_split_500_fmt']}")
    if metrics.get("avg_hr") is not None:
        lines.append(f"Avg HR: {metrics['avg_hr']} bpm")
    rowing = session.get("rowing") or {}
    if rowing.get("median_hr") is not None:
        lines.append(f"Strava median HR: {rowing['median_hr']} bpm")
    if rowing.get("median_split_500_fmt"):
        lines.append(f"Strava median split: {rowing['median_split_500_fmt']}")
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
        lines.append("  " + ", ".join(bits))
    return "\n".join(lines)


def find_merged_session_for_zulip_score(
    cache_dir: Path,
    athlete_id: int,
    zulip_score_id: str,
) -> Optional[Dict[str, Any]]:
    for session in load_merged_erg_sessions_for_athlete(cache_dir, athlete_id, limit=200):
        if str(session.get("zulip_score_id")) == zulip_score_id:
            return session
    return None


def format_merged_erg_history_context(
    cache_dir: Path,
    athlete_id: int,
    *,
    exclude_merged_id: Optional[str] = None,
    limit: int = 12,
    heading: str = "Prior merged erg sessions (newest first)",
) -> str:
    sessions = load_merged_erg_sessions_for_athlete(cache_dir, athlete_id, limit=limit + 5)
    if exclude_merged_id:
        sessions = [s for s in sessions if str(s.get("id")) != exclude_merged_id]
    sessions = sessions[:limit]
    if not sessions:
        return f"--- {heading} ---\n(no prior merged erg sessions)"
    lines = [f"--- {heading} ---"]
    for session in sessions:
        lines.append(format_merged_erg_session_line(session))
    return "\n".join(lines)
