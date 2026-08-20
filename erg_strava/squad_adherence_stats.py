"""Deterministic squad stats for the previous-week adherence section."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from athlete_profile import AthleteProfile, DEFAULT_FIVE_ZONE_PCT, FIVE_ZONE_ORDER
from generate_training_plan import (
    WeekBounds,
    _fmt_split,
    _parse_activity_start,
    is_gym_activity,
    iter_cached_athlete_ids,
    load_gym_logs_for_athlete,
    week_contains,
)

_AEROBIC_ZONES = frozenset({"z1", "z2", "z3"})
_HIGH_ZONES = frozenset({"z4", "z5"})
_DEFAULT_MAX_HR = 190


@dataclass(frozen=True)
class AthleteWeekAdherenceStats:
    athlete_id: int
    label: str
    gym_tonnage_kg: float
    z13_split_median_sec: Optional[float]
    z13_minutes: float
    z4p_split_median_sec: Optional[float]
    z4p_minutes: float


@dataclass(frozen=True)
class SquadAdherenceStats:
    athlete_stats: Tuple[AthleteWeekAdherenceStats, ...]

    def gym_tonnage_values(self) -> List[float]:
        return [s.gym_tonnage_kg for s in self.athlete_stats if s.gym_tonnage_kg > 0]

    def z13_split_values(self) -> List[float]:
        return [
            s.z13_split_median_sec
            for s in self.athlete_stats
            if s.z13_split_median_sec is not None and s.z13_minutes > 0
        ]

    def z13_minute_values(self) -> List[float]:
        return [s.z13_minutes for s in self.athlete_stats if s.z13_minutes > 0]

    def z4p_split_values(self) -> List[float]:
        return [
            s.z4p_split_median_sec
            for s in self.athlete_stats
            if s.z4p_split_median_sec is not None and s.z4p_minutes > 0
        ]

    def z4p_minute_values(self) -> List[float]:
        return [s.z4p_minutes for s in self.athlete_stats if s.z4p_minutes > 0]


def _median_std(values: Sequence[float]) -> Tuple[Optional[float], Optional[float]]:
    if not values:
        return None, None
    arr = np.asarray(values, dtype=float)
    med = float(np.median(arr))
    std = float(np.std(arr, ddof=0)) if len(arr) > 1 else 0.0
    return med, std


def _classify_hr_zone(
    hr_bpm: float,
    profile: Optional[AthleteProfile],
) -> Optional[str]:
    if profile is not None:
        zone = profile.classify_five_zone_hr(hr_bpm)
        if zone is not None:
            return zone
    if profile is not None and profile.max_hr_bpm is not None:
        max_hr = profile.max_hr_bpm
    else:
        max_hr = _DEFAULT_MAX_HR
    for key in reversed(FIVE_ZONE_ORDER):
        lo_pct, hi_pct = DEFAULT_FIVE_ZONE_PCT[key]
        lo = int(round(max_hr * lo_pct))
        hi = int(round(max_hr * hi_pct))
        if lo <= hr_bpm <= hi:
            return key
    return None


def _point_durations_sec(sub: pd.DataFrame) -> pd.Series:
    if sub.empty:
        return pd.Series(dtype=float)
    ordered = sub.sort_values("time")
    dt = ordered["time"].astype(float).diff()
    if len(dt) > 1:
        median_dt = float(dt.iloc[1:].median())
        if not np.isfinite(median_dt) or median_dt <= 0:
            median_dt = 10.0
    else:
        median_dt = 10.0
    dt = dt.fillna(median_dt)
    return dt.clip(lower=1.0, upper=30.0)


def _erg_week_mask(erg_df: pd.DataFrame, week: WeekBounds) -> pd.Series:
    if erg_df.empty or "activity_start" not in erg_df.columns:
        return pd.Series(False, index=erg_df.index)
    starts = pd.to_datetime(erg_df["activity_start"], utc=True, errors="coerce")

    def _in_week(ts: Any) -> bool:
        if ts is None or (isinstance(ts, float) and np.isnan(ts)):
            return False
        if isinstance(ts, pd.Timestamp):
            if pd.isna(ts):
                return False
            ts = ts.to_pydatetime()
        return week_contains(week, ts)

    return starts.map(_in_week).fillna(False)


def _bucket_erg_points(
    sub: pd.DataFrame,
    profile: Optional[AthleteProfile],
) -> Tuple[Optional[float], float, Optional[float], float]:
    if sub.empty or "split_500" not in sub.columns or "hr" not in sub.columns:
        return None, 0.0, None, 0.0

    z13_splits: List[float] = []
    z13_secs = 0.0
    z4p_splits: List[float] = []
    z4p_secs = 0.0

    for activity_id, act_sub in sub.groupby("activity_id", sort=False):
        durations = _point_durations_sec(act_sub)
        for (_, row), dt in zip(act_sub.iterrows(), durations):
            hr = row.get("hr")
            split = row.get("split_500")
            if hr is None or split is None or not np.isfinite(hr) or not np.isfinite(split):
                continue
            zone = _classify_hr_zone(float(hr), profile)
            if zone in _AEROBIC_ZONES:
                z13_splits.append(float(split))
                z13_secs += float(dt)
            elif zone in _HIGH_ZONES:
                z4p_splits.append(float(split))
                z4p_secs += float(dt)

    z13_med = float(np.median(z13_splits)) if z13_splits else None
    z4p_med = float(np.median(z4p_splits)) if z4p_splits else None
    return z13_med, z13_secs / 60.0, z4p_med, z4p_secs / 60.0


def _athlete_week_gym_tonnage(
    athlete_id: int,
    week: WeekBounds,
    week_activities: Sequence[dict],
    activity_metrics: Mapping[int, Dict[str, Any]],
    activity_athlete_ids: Mapping[int, int],
    gym_types: frozenset,
    gym_name_patterns: Sequence[str],
    cache_dir: Path,
) -> float:
    total = 0.0
    seen_dates: set[str] = set()
    for act in week_activities:
        start = _parse_activity_start(act.get("start_date"))
        if start is None or not week_contains(week, start):
            continue
        aid = int(act["id"])
        if activity_athlete_ids.get(aid) != athlete_id:
            continue
        if not is_gym_activity(act, gym_types, gym_name_patterns):
            continue
        gym = (activity_metrics.get(aid) or {}).get("gym") or {}
        try:
            total += float(gym.get("total_tonnage_kg", 0))
        except (TypeError, ValueError):
            pass

    for rec in load_gym_logs_for_athlete(cache_dir, athlete_id, week=week):
        session_day = str(rec.get("session_date") or "")
        if session_day in seen_dates:
            continue
        seen_dates.add(session_day)
        gym = rec.get("gym") or {}
        try:
            total += float(gym.get("total_tonnage_kg", 0))
        except (TypeError, ValueError):
            pass
    return total


def load_athlete_context(
    cache_dir: Path,
    config_path: Optional[Path],
) -> Tuple[Dict[int, AthleteProfile], Dict[int, int]]:
    profiles: Dict[int, AthleteProfile] = {}
    activity_athlete_ids: Dict[int, int] = {}
    if config_path is None or not config_path.is_file():
        return profiles, activity_athlete_ids
    try:
        import yaml
        from athlete_profile import athlete_profiles_by_id, load_athlete_profiles

        raw = yaml.safe_load(config_path.read_text()) or {}
        profiles = athlete_profiles_by_id(load_athlete_profiles(raw))
        for profile in profiles.values():
            idx_path = cache_dir / f"athlete_{profile.id}" / "index.json"
            if not idx_path.is_file():
                continue
            idx_data = json.loads(idx_path.read_text())
            for act in idx_data.get("activities") or []:
                activity_athlete_ids[int(act["id"])] = profile.id
    except Exception:
        pass
    return profiles, activity_athlete_ids


def compute_squad_week_adherence_stats(
    week: WeekBounds,
    week_activities: Sequence[dict],
    activity_metrics: Mapping[int, Dict[str, Any]],
    erg_df: Optional[pd.DataFrame],
    *,
    cache_dir: Path,
    config_path: Optional[Path] = None,
    athlete_profiles: Optional[Mapping[int, AthleteProfile]] = None,
    activity_athlete_ids: Optional[Mapping[int, int]] = None,
    gym_types: frozenset,
    gym_name_patterns: Sequence[str],
) -> SquadAdherenceStats:
    profiles, act_ids = load_athlete_context(cache_dir, config_path)
    if athlete_profiles:
        profiles.update(dict(athlete_profiles))
    if activity_athlete_ids:
        act_ids.update(dict(activity_athlete_ids))

    label_by_id = {pid: p.label for pid, p in profiles.items()}
    athlete_ids = sorted(set(iter_cached_athlete_ids(cache_dir)) | set(profiles.keys()))
    if not athlete_ids:
        athlete_ids = sorted(set(act_ids.values()))

    erg_week = pd.DataFrame()
    if erg_df is not None and not erg_df.empty:
        mask = _erg_week_mask(erg_df, week)
        erg_week = erg_df.loc[mask].copy()

    per_athlete: List[AthleteWeekAdherenceStats] = []
    for athlete_id in athlete_ids:
        label = label_by_id.get(athlete_id, f"athlete_{athlete_id}")
        tonnage = _athlete_week_gym_tonnage(
            athlete_id,
            week,
            week_activities,
            activity_metrics,
            act_ids,
            gym_types,
            gym_name_patterns,
            cache_dir,
        )
        athlete_erg = pd.DataFrame()
        if not erg_week.empty and "athlete" in erg_week.columns:
            athlete_erg = erg_week.loc[erg_week["athlete"] == label]
        profile = profiles.get(athlete_id)
        z13_split, z13_min, z4p_split, z4p_min = _bucket_erg_points(athlete_erg, profile)
        per_athlete.append(
            AthleteWeekAdherenceStats(
                athlete_id=athlete_id,
                label=label,
                gym_tonnage_kg=tonnage,
                z13_split_median_sec=z13_split,
                z13_minutes=z13_min,
                z4p_split_median_sec=z4p_split,
                z4p_minutes=z4p_min,
            )
        )
    return SquadAdherenceStats(athlete_stats=tuple(per_athlete))


def _fmt_median_std(
    med: Optional[float],
    std: Optional[float],
    *,
    unit: str = "",
    as_split: bool = False,
) -> str:
    if med is None:
        return "—"
    if as_split:
        med_s = _fmt_split(med)
        if std is None or std <= 0:
            return med_s
        return f"{med_s} (σ {_fmt_split(std)})"
    med_i = int(round(med))
    if std is None or std <= 0:
        return f"{med_i}{unit}"
    return f"{med_i} (σ {int(round(std))}{unit})"


def format_squad_adherence_stats(
    stats: SquadAdherenceStats,
    week: WeekBounds,
) -> str:
    gym_med, gym_std = _median_std(stats.gym_tonnage_values())
    z13_split_med, z13_split_std = _median_std(stats.z13_split_values())
    z13_min_med, z13_min_std = _median_std(stats.z13_minute_values())
    z4p_split_med, z4p_split_std = _median_std(stats.z4p_split_values())
    z4p_min_med, z4p_min_std = _median_std(stats.z4p_minute_values())

    lines = [
        f"**Logged squad stats** ({week.week_start} – {week.week_end})",
        (
            "- Gym tonnage (weekly per athlete): "
            f"median {_fmt_median_std(gym_med, gym_std, unit=' kg')}"
            f"; n={len(stats.gym_tonnage_values())} athlete(s)"
        ),
        (
            "- Erg Z1–Z3: median split "
            f"{_fmt_median_std(z13_split_med, z13_split_std, as_split=True)}, "
            f"total time median {_fmt_median_std(z13_min_med, z13_min_std, unit=' min')}"
            f"; n={len(stats.z13_minute_values())} athlete(s)"
        ),
        (
            "- Erg Z4+: median split "
            f"{_fmt_median_std(z4p_split_med, z4p_split_std, as_split=True)}, "
            f"total time median {_fmt_median_std(z4p_min_med, z4p_min_std, unit=' min')}"
            f"; n={len(stats.z4p_minute_values())} athlete(s)"
        ),
    ]
    return "\n".join(lines)


def compose_adherence_review(
    stats_block: str,
    narrative: str,
) -> str:
    narrative = (narrative or "").strip()
    stats_block = (stats_block or "").strip()
    if stats_block and narrative:
        return f"{stats_block}\n\n{narrative}"
    return stats_block or narrative
