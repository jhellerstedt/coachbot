"""Sync indoor rowing workouts from Suunto via suuntool CLI (primary activity catalog)."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

import pandas as pd

from erg_parse import fit_has_heartrate, records_from_fit
from suunto_client import (
    DEFAULT_GYM_ACTIVITY_IDS,
    DEFAULT_INDOOR_ROWING_ACTIVITY_IDS,
    SuuntoCfg,
    SuuntoClient,
    load_suunto_cfg,
    suunto_sync_enabled_for_athlete,
)

MERGE_MAX_HOURS = 6.0
MERGE_DISTANCE_TOLERANCE = 0.15

__all__ = [
    "DEFAULT_GYM_ACTIVITY_IDS",
    "DEFAULT_INDOOR_ROWING_ACTIVITY_IDS",
    "MERGE_DISTANCE_TOLERANCE",
    "MERGE_MAX_HOURS",
    "SuuntoCfg",
    "link_suunto_workouts_to_strava",
    "list_suunto_erg_workouts",
    "list_suunto_endurance_workouts",
    "list_suunto_gym_workouts",
    "list_tracked_suunto_workouts",
    "load_suunto_cfg",
    "suunto_sync_enabled_for_athlete",
    "load_suunto_index",
    "match_suunto_to_strava",
    "save_suunto_index",
    "stable_activity_id",
    "suunto_fit_path",
    "suunto_fit_path_for_strava",
    "suunto_key_for_strava",
    "suunto_paths",
    "suunto_start_dt",
    "suunto_workout_as_activity",
    "suunto_workout_record",
    "suunto_workout_record_by_key",
    "summarize_suunto_rowing_metrics",
    "summarize_suunto_rowing_metrics_by_key",
    "sync_suunto_erg_for_athlete",
    "sync_suunto_workouts_for_athlete",
    "sync_suunto_workouts_for_athlete_detailed",
]


def suunto_paths(cache_dir: Path, athlete_id: int) -> Dict[str, Path]:
    root = cache_dir / f"athlete_{athlete_id}" / "suunto"
    return {
        "root": root,
        "index": root / "index.json",
        "fits": root / "fits",
        "workouts": root / "workouts",
    }


def load_suunto_index(path: Path) -> dict:
    if not path.is_file():
        return {"workouts": {}, "by_strava_id": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_suunto_index(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def stable_activity_id(
    suunto_key: str, strava_activity_id: Optional[int] = None
) -> int:
    if strava_activity_id is not None:
        return int(strava_activity_id)
    digest = hashlib.sha256(suunto_key.encode("utf-8")).digest()
    n = int.from_bytes(digest[:8], "big") % (10**15 - 1)
    return -n if n else -1


def suunto_start_dt(workout: Mapping[str, Any]) -> Optional[datetime]:
    raw = workout.get("startTime")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(float(raw) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _parse_strava_start(act: Mapping[str, Any]) -> Optional[datetime]:
    raw = act.get("start_date")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _distances_compatible(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return True
    if a <= 0 or b <= 0:
        return True
    rel = abs(a - b) / max(a, b)
    return rel <= MERGE_DISTANCE_TOLERANCE


def _hours_apart(a: datetime, b: datetime) -> float:
    return abs((a.astimezone(timezone.utc) - b.astimezone(timezone.utc)).total_seconds()) / 3600.0


def match_suunto_to_strava(
    suunto_wk: Mapping[str, Any],
    strava_act: Mapping[str, Any],
) -> bool:
    s_start = suunto_start_dt(suunto_wk)
    s_start_strava = _parse_strava_start(strava_act)
    if s_start is None or s_start_strava is None:
        return False
    if _hours_apart(s_start, s_start_strava) > MERGE_MAX_HOURS:
        return False
    s_dist = suunto_wk.get("totalDistance")
    try:
        s_dist_f = float(s_dist) if s_dist is not None else None
    except (TypeError, ValueError):
        s_dist_f = None
    st_dist = strava_act.get("distance")
    try:
        st_dist_f = float(st_dist) if st_dist is not None else None
    except (TypeError, ValueError):
        st_dist_f = None
    return _distances_compatible(s_dist_f, st_dist_f)


def suunto_key_for_strava(
    cache_dir: Path,
    athlete_id: int,
    strava_activity_id: int,
) -> Optional[str]:
    idx = load_suunto_index(suunto_paths(cache_dir, athlete_id)["index"])
    return (idx.get("by_strava_id") or {}).get(str(strava_activity_id))


def suunto_fit_path(
    cache_dir: Path, athlete_id: int, suunto_key: str
) -> Optional[Path]:
    fit = suunto_paths(cache_dir, athlete_id)["fits"] / f"{suunto_key}.fit"
    return fit if fit.is_file() else None


def suunto_fit_path_for_strava(
    cache_dir: Path,
    athlete_id: int,
    strava_activity_id: int,
) -> Optional[Path]:
    key = suunto_key_for_strava(cache_dir, athlete_id, strava_activity_id)
    if not key:
        return None
    return suunto_fit_path(cache_dir, athlete_id, key)


def suunto_workout_record_by_key(
    cache_dir: Path, athlete_id: int, suunto_key: str
) -> Optional[Dict[str, Any]]:
    idx = load_suunto_index(suunto_paths(cache_dir, athlete_id)["index"])
    rec = (idx.get("workouts") or {}).get(suunto_key)
    return dict(rec) if rec else None


def suunto_workout_record(
    cache_dir: Path,
    athlete_id: int,
    strava_activity_id: int,
) -> Optional[Dict[str, Any]]:
    key = suunto_key_for_strava(cache_dir, athlete_id, strava_activity_id)
    if not key:
        return None
    return suunto_workout_record_by_key(cache_dir, athlete_id, key)


def list_tracked_suunto_workouts(
    cache_dir: Path,
    athlete_id: int,
    cfg: SuuntoCfg,
    *,
    activity_ids: Optional[frozenset] = None,
) -> List[Dict[str, Any]]:
    if not suunto_sync_enabled_for_athlete(cfg, athlete_id):
        return []
    idx = load_suunto_index(suunto_paths(cache_dir, athlete_id)["index"])
    allowed = activity_ids or (cfg.indoor_rowing_activity_ids | cfg.gym_activity_ids)
    out: List[Dict[str, Any]] = []
    for key, rec in (idx.get("workouts") or {}).items():
        if int(rec.get("activityId") or 0) not in allowed:
            continue
        row = dict(rec)
        row["key"] = row.get("key") or key
        out.append(row)
    out.sort(
        key=lambda r: float(r.get("startTime") or 0),
        reverse=True,
    )
    return out


# Suunto / Sports Tracker activityId → Strava-like sport_type.
# Verified via `suuntool workouts get` pretty labels (CYCLING act=2, RUNNING act=1).
SUUNTO_ACTIVITY_ID_SPORT_TYPE: Dict[int, str] = {
    1: "Run",
    2: "Ride",
}


def _suunto_sport_type(rec: Mapping[str, Any], cfg: SuuntoCfg) -> str:
    activity_id = int(rec.get("activityId") or 0)
    if activity_id in cfg.indoor_rowing_activity_ids:
        return "VirtualRow"
    if activity_id in cfg.gym_activity_ids:
        return "WeightTraining"
    mapped = SUUNTO_ACTIVITY_ID_SPORT_TYPE.get(activity_id)
    if mapped:
        return mapped
    stored = str(rec.get("sport_type") or rec.get("sportType") or "").strip()
    if stored and stored not in {"Workout", "INDOOR_ROWING"}:
        return stored
    name = str(rec.get("activityName") or "").strip().lower()
    if any(tok in name for tok in ("run", "running", "trail")):
        return "Run"
    if any(tok in name for tok in ("ride", "cycling", "bike", "biking", "cycle")):
        return "Ride"
    return "Workout"


def list_suunto_erg_workouts(
    cache_dir: Path, athlete_id: int, cfg: SuuntoCfg
) -> List[Dict[str, Any]]:
    return list_tracked_suunto_workouts(
        cache_dir, athlete_id, cfg, activity_ids=cfg.indoor_rowing_activity_ids
    )


def list_suunto_gym_workouts(
    cache_dir: Path, athlete_id: int, cfg: SuuntoCfg
) -> List[Dict[str, Any]]:
    return list_tracked_suunto_workouts(
        cache_dir, athlete_id, cfg, activity_ids=cfg.gym_activity_ids
    )


def list_suunto_endurance_workouts(
    cache_dir: Path, athlete_id: int, cfg: SuuntoCfg
) -> List[Dict[str, Any]]:
    if not suunto_sync_enabled_for_athlete(cfg, athlete_id):
        return []
    idx = load_suunto_index(suunto_paths(cache_dir, athlete_id)["index"])
    out: List[Dict[str, Any]] = []
    for key, rec in (idx.get("workouts") or {}).items():
        row = dict(rec)
        row["key"] = row.get("key") or key
        sport_type = _suunto_sport_type(row, cfg)
        if sport_type in {"VirtualRow", "WeightTraining"}:
            continue
        if sport_type not in {"Ride", "Run", "VirtualRide", "VirtualRun", "Rowing", "Workout"}:
            continue
        out.append(row)
    out.sort(
        key=lambda r: float(r.get("startTime") or 0),
        reverse=True,
    )
    return out


def suunto_workout_as_activity(rec: Mapping[str, Any], cfg: SuuntoCfg) -> dict:
    """Normalize a Suunto index row into an activity-like dict for week filters."""
    key = str(rec.get("key") or "")
    strava_id = rec.get("strava_activity_id")
    strava_id_int = int(strava_id) if strava_id is not None else None
    start = suunto_start_dt(rec)
    sport_type = _suunto_sport_type(rec, cfg)
    return {
        "id": stable_activity_id(key, strava_id_int),
        "suunto_key": key,
        "strava_activity_id": strava_id_int,
        "name": rec.get("activityName") or "Suunto workout",
        "sport_type": sport_type,
        "start_date": start.isoformat().replace("+00:00", "Z") if start else None,
        "distance": rec.get("totalDistance"),
        "moving_time": rec.get("totalTime"),
        "source": "suunto",
        "has_hr": rec.get("has_hr_fit"),
    }


def _records_from_fit_path(fit_path: Path) -> Optional[pd.DataFrame]:
    if not fit_path.is_file():
        return None
    return records_from_fit(fit_path.read_bytes())


def summarize_suunto_rowing_metrics_by_key(
    cache_dir: Path,
    athlete_id: int,
    suunto_key: str,
) -> Optional[Dict[str, Any]]:
    fit_path = suunto_fit_path(cache_dir, athlete_id, suunto_key)
    if fit_path is None:
        return None
    raw = _records_from_fit_path(fit_path)
    if raw is None or raw.empty or not raw["hr"].notna().any():
        return None
    from erg_parse import samples_to_split_hr_frame
    from generate_training_plan import _fmt_split

    inst = samples_to_split_hr_frame(raw)
    if inst.empty:
        hr = raw["hr"].dropna().astype(float)
        if hr.empty:
            return None
        return {
            "median_split_500": None,
            "median_split_500_fmt": None,
            "median_hr": int(round(float(hr.median()))),
            "iqr_hr": int(round(float(hr.quantile(0.75) - hr.quantile(0.25)))),
            "n_points": int(len(hr)),
            "source": "suunto_fit",
            "suunto_key": suunto_key,
        }
    split = inst["split_500"].astype(float)
    hr = inst["hr"].astype(float)
    n = len(inst)
    high = (split < 110).sum() / n * 100 if n else 0.0
    return {
        "median_split_500": float(split.median()),
        "median_split_500_fmt": _fmt_split(float(split.median())),
        "iqr_split_500": float(split.quantile(0.75) - split.quantile(0.25)),
        "median_hr": int(round(float(hr.median()))),
        "iqr_hr": int(round(float(hr.quantile(0.75) - hr.quantile(0.25)))),
        "n_points": n,
        "intensity_high_pct": round(high, 1),
        "source": "suunto_fit",
        "suunto_key": suunto_key,
    }


def summarize_suunto_rowing_metrics(
    cache_dir: Path,
    athlete_id: int,
    strava_activity_id: int,
) -> Optional[Dict[str, Any]]:
    key = suunto_key_for_strava(cache_dir, athlete_id, strava_activity_id)
    if not key:
        return None
    return summarize_suunto_rowing_metrics_by_key(cache_dir, athlete_id, key)


def _best_strava_match(
    suunto_wk: Mapping[str, Any],
    erg_acts: Sequence[Mapping[str, Any]],
    used: Set[int],
) -> Optional[int]:
    best_id: Optional[int] = None
    best_hours = MERGE_MAX_HOURS + 1.0
    s_start = suunto_start_dt(suunto_wk)
    if s_start is None:
        return None
    for act in erg_acts:
        aid = int(act["id"])
        if aid in used:
            continue
        if not match_suunto_to_strava(suunto_wk, act):
            continue
        st = _parse_strava_start(act)
        if st is None:
            continue
        hours = _hours_apart(s_start, st)
        if hours < best_hours:
            best_hours = hours
            best_id = aid
    return best_id


def sync_suunto_workouts_for_athlete(
    *,
    athlete_id: int,
    athlete_label: str,
    cache_dir: Path,
    cfg: SuuntoCfg,
    config_base: Path,
    refresh: bool = False,
) -> int:
    """Sync Suunto workouts while preserving the legacy integer return."""
    synced, _ = sync_suunto_workouts_for_athlete_detailed(
        athlete_id=athlete_id,
        athlete_label=athlete_label,
        cache_dir=cache_dir,
        cfg=cfg,
        config_base=config_base,
        refresh=refresh,
    )
    return synced


def sync_suunto_workouts_for_athlete_detailed(
    *,
    athlete_id: int,
    athlete_label: str,
    cache_dir: Path,
    cfg: SuuntoCfg,
    config_base: Path,
    refresh: bool = False,
) -> tuple[int, Optional[str]]:
    """Sync workouts and return an error from client setup or workout listing."""
    if not cfg.enabled:
        return 0, None

    paths = suunto_paths(cache_dir, athlete_id)
    paths["fits"].mkdir(parents=True, exist_ok=True)
    paths["workouts"].mkdir(parents=True, exist_ok=True)
    idx = load_suunto_index(paths["index"])
    known = dict(idx.get("workouts") or {})

    try:
        client = SuuntoClient(cfg, config_base)
        who = client.whoami()
    except (FileNotFoundError, RuntimeError) as e:
        print(f"[{athlete_label}] Suunto sync skipped: {e}", file=sys.stderr)
        return 0, str(e)

    username = str(who.get("username") or "")
    try:
        items = client.list_workouts(cfg.list_since_days)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"[{athlete_label}] Suunto workouts list failed: {e}", file=sys.stderr)
        return 0, str(e)

    tracked_ids = cfg.indoor_rowing_activity_ids | cfg.gym_activity_ids
    n_synced = 0
    n_erg = 0
    n_gym = 0

    for wk in items:
        key = str(wk.get("key") or "")
        if not key:
            continue
        activity_id = int(wk.get("activityId") or 0)
        is_erg = activity_id in cfg.indoor_rowing_activity_ids
        is_gym = activity_id in cfg.gym_activity_ids
        if is_erg:
            n_erg += 1
        if is_gym:
            n_gym += 1
        prev = known.get(key) or {}
        fit_path = paths["fits"] / f"{key}.fit"
        meta_path = paths["workouts"] / f"{key}.json"

        need_meta = (refresh or not meta_path.is_file()) and (is_erg or is_gym)
        need_fit = is_erg and (
            refresh or not fit_path.is_file() or not fit_has_heartrate(fit_path)
        )

        detail: Dict[str, Any] = {}
        if need_meta:
            try:
                detail = client.get_workout(key)
                meta_path.write_text(json.dumps(detail, indent=2), encoding="utf-8")
                wk = {**wk, **detail}
            except RuntimeError as e:
                print(f"[{athlete_label}] Suunto get {key}: {e}", file=sys.stderr)
                continue
        elif meta_path.is_file():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    detail = loaded
                    wk = {**wk, **detail}
            except (OSError, json.JSONDecodeError):
                detail = {}

        if need_fit:
            try:
                client.download_fit(key, fit_path)
                n_synced += 1
            except RuntimeError as e:
                print(f"[{athlete_label}] Suunto FIT {key}: {e}", file=sys.stderr)

        description = ""
        try:
            from gym_suunto import extract_suunto_workout_description

            description = extract_suunto_workout_description(detail or wk)
        except ImportError:
            description = str((detail or wk).get("description") or "").strip()

        sport_type = _suunto_sport_type(wk, cfg)
        activity_name = wk.get("activityName")
        if not activity_name or str(activity_name).strip().upper() == "INDOOR_ROWING":
            if sport_type == "Ride":
                activity_name = "Cycling"
            elif sport_type == "Run":
                activity_name = "Running"
            elif sport_type == "VirtualRow":
                activity_name = "INDOOR_ROWING"
            elif sport_type == "WeightTraining":
                activity_name = "Gym"
            else:
                activity_name = activity_name or "Suunto workout"

        rec = {
            "key": key,
            "activityId": wk.get("activityId"),
            "startTime": wk.get("startTime"),
            "totalTime": wk.get("totalTime"),
            "totalDistance": wk.get("totalDistance"),
            "activityName": activity_name,
            "sport_type": sport_type,
            "strava_activity_id": prev.get("strava_activity_id"),
            "has_hr_fit": fit_has_heartrate(fit_path),
            "description": description or None,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
        known[key] = rec

    idx["username"] = username
    idx["workouts"] = known
    idx.setdefault("by_strava_id", dict(idx.get("by_strava_id") or {}))
    idx["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_suunto_index(paths["index"], idx)

    hr_ok = sum(1 for r in known.values() if r.get("has_hr_fit"))
    print(
        f"[{athlete_label}] Suunto: {n_erg} erg + {n_gym} gym workout(s), "
        f"{hr_ok} with HR in FIT"
        + (f", {n_synced} FIT downloaded" if n_synced else "")
    )
    return n_synced, None


def link_suunto_workouts_to_strava(
    *,
    athlete_id: int,
    athlete_label: str,
    cache_dir: Path,
    cfg: SuuntoCfg,
    erg_acts: Sequence[Mapping[str, Any]],
    gym_acts: Optional[Sequence[Mapping[str, Any]]] = None,
) -> int:
    """Match cached Suunto workouts to Strava listing rows; returns match count."""
    gym_acts = gym_acts or []
    paths = suunto_paths(cache_dir, athlete_id)
    idx = load_suunto_index(paths["index"])
    known = dict(idx.get("workouts") or {})
    by_strava: Dict[str, str] = dict(idx.get("by_strava_id") or {})
    used_strava: Set[int] = {int(k) for k in by_strava.keys()}
    n_new = 0

    for key, rec in known.items():
        strava_id = rec.get("strava_activity_id")
        if strava_id is not None:
            by_strava[str(int(strava_id))] = key
            continue
        activity_id = int(rec.get("activityId") or 0)
        is_erg = activity_id in cfg.indoor_rowing_activity_ids
        is_gym = activity_id in cfg.gym_activity_ids
        candidates = erg_acts if is_erg else gym_acts if is_gym else []
        meta_path = paths["workouts"] / f"{key}.json"
        wk = rec
        if meta_path.is_file():
            try:
                wk = {**rec, **json.loads(meta_path.read_text(encoding="utf-8"))}
            except (OSError, json.JSONDecodeError):
                pass
        matched = _best_strava_match(wk, candidates, used_strava)
        if matched is not None:
            rec["strava_activity_id"] = matched
            used_strava.add(matched)
            by_strava[str(matched)] = key
            known[key] = rec
            n_new += 1

    idx["workouts"] = known
    idx["by_strava_id"] = by_strava
    idx["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_suunto_index(paths["index"], idx)
    matched = sum(1 for r in known.values() if r.get("strava_activity_id") is not None)
    print(
        f"[{athlete_label}] Suunto↔Strava: {matched} linked"
        + (f" ({n_new} new this run)" if n_new else "")
    )
    return n_new


def sync_suunto_erg_for_athlete(
    *,
    athlete_id: int,
    athlete_label: str,
    cache_dir: Path,
    erg_acts: Sequence[Mapping[str, Any]],
    cfg: SuuntoCfg,
    config_base: Path,
    refresh: bool = False,
    gym_acts: Optional[Sequence[Mapping[str, Any]]] = None,
) -> int:
    """Download Suunto workouts then link to Strava (legacy combined entry point)."""
    n = sync_suunto_workouts_for_athlete(
        athlete_id=athlete_id,
        athlete_label=athlete_label,
        cache_dir=cache_dir,
        cfg=cfg,
        config_base=config_base,
        refresh=refresh,
    )
    link_suunto_workouts_to_strava(
        athlete_id=athlete_id,
        athlete_label=athlete_label,
        cache_dir=cache_dir,
        cfg=cfg,
        erg_acts=erg_acts,
        gym_acts=gym_acts,
    )
    return n
