"""Build KDE plot point frames from cached stream data and Zulip erg scores."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from erg_parse import HR_MIN_PLOT_BPM
from erg_session_merge import (
    _load_all_erg_scores_for_athlete,
    _score_session_datetime,
    build_merged_erg_sessions_for_athlete,
)
from suunto_sync import (
    list_suunto_erg_workouts,
    stable_activity_id,
    suunto_key_for_strava,
    suunto_sync_enabled_for_athlete,
    suunto_workout_record_by_key,
)


@dataclass
class CollectStats:
    dense_cached: int = 0
    dense_parsed: int = 0
    dense_sessions: int = 0
    sparse_scores: int = 0
    sparse_points: int = 0


def score_session_start_utc(score_rec: Dict[str, Any]):
    dt = _score_session_datetime(score_rec)
    if dt is None:
        return None
    return dt.astimezone(timezone.utc)


def build_sparse_points_from_score(
    athlete_label: str,
    score_rec: Dict[str, Any],
) -> Optional[pd.DataFrame]:
    """One or more (split_500, hr) points from a logged Zulip erg score."""
    metrics = score_rec.get("metrics") or {}
    avg_hr_raw = metrics.get("avg_hr")
    try:
        avg_hr = float(avg_hr_raw) if avg_hr_raw is not None else None
    except (TypeError, ValueError):
        avg_hr = None
    if avg_hr is None or avg_hr < HR_MIN_PLOT_BPM:
        return None

    rows: List[Dict[str, Any]] = []
    intervals = metrics.get("intervals") or []
    if intervals:
        for iv in intervals:
            split_raw = iv.get("split_500_sec")
            if split_raw is None:
                continue
            try:
                split_sec = float(split_raw)
            except (TypeError, ValueError):
                continue
            if split_sec <= 0:
                continue
            rows.append(
                {
                    "time": float(len(rows)),
                    "split_500": split_sec,
                    "hr": avg_hr,
                    "point_source": "zulip_screenshot",
                }
            )
    else:
        split_raw = metrics.get("avg_split_500_sec")
        if split_raw is None:
            return None
        try:
            split_sec = float(split_raw)
        except (TypeError, ValueError):
            return None
        if split_sec <= 0:
            return None
        rows.append(
            {
                "time": 0.0,
                "split_500": split_sec,
                "hr": avg_hr,
                "point_source": "zulip_screenshot",
            }
        )

    if not rows:
        return None

    start_dt = score_session_start_utc(score_rec)
    if start_dt is None:
        return None

    score_id = str(score_rec.get("id") or "")
    df = pd.DataFrame(rows)
    df["athlete"] = athlete_label
    df["activity_id"] = stable_activity_id(f"zulip-{score_id}", None)
    df["suunto_key"] = None
    df["zulip_score_id"] = score_id or None
    df["activity_start"] = start_dt
    return df


def _try_dense_suunto_rec(
    acfg,
    cache_dir,
    paths,
    suunto_rec: Dict[str, Any],
    photo_cfg,
    photo_hash: str,
    manifest: dict,
    strava_by_id: Dict[int, dict],
) -> Tuple[Optional[pd.DataFrame], bool]:
    from strava_erg_hr_plot import ensure_parsed_activity_cache

    strava_id = suunto_rec.get("strava_activity_id")
    strava_act = (
        strava_by_id.get(int(strava_id)) if strava_id is not None else None
    )
    return ensure_parsed_activity_cache(
        acfg,
        cache_dir,
        paths,
        suunto_rec,
        photo_cfg,
        photo_hash,
        manifest,
        strava_act=strava_act,
    )


def _try_dense_strava_act(
    acfg,
    cache_dir,
    act: dict,
    photo_cfg,
    photo_hash: str,
    manifest: dict,
    paths,
) -> Tuple[Optional[pd.DataFrame], bool]:
    from strava_erg_hr_plot import ensure_parsed_activity_cache, parse_start_date

    aid = int(act["id"])
    start = parse_start_date(act.get("start_date"))
    synthetic: Dict[str, Any] = {
        "key": f"strava-{aid}",
        "strava_activity_id": aid,
        "startTime": int(start.timestamp() * 1000) if start else None,
    }
    linked = suunto_key_for_strava(cache_dir, acfg.id, aid)
    if linked:
        rec = suunto_workout_record_by_key(cache_dir, acfg.id, str(linked))
        if rec:
            return _try_dense_suunto_rec(
                acfg,
                cache_dir,
                paths,
                rec,
                photo_cfg,
                photo_hash,
                manifest,
                {aid: act},
            )
    return ensure_parsed_activity_cache(
        acfg,
        cache_dir,
        paths,
        synthetic,
        photo_cfg,
        photo_hash,
        manifest,
        strava_act=act,
    )


def _session_suunto_keys(session: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    for raw in session.get("suunto_workout_keys") or []:
        key = str(raw or "")
        if key and key not in keys:
            keys.append(key)
    sk = session.get("suunto_workout_key")
    if sk:
        key = str(sk)
        if key not in keys:
            keys.insert(0, key)
    return keys


def _collect_dense_for_suunto_key(
    acfg,
    cache_dir,
    paths,
    suunto_key_str: str,
    photo_cfg,
    photo_hash: str,
    manifest: dict,
    strava_by_id: Dict[int, dict],
) -> Tuple[Optional[pd.DataFrame], bool]:
    rec = suunto_workout_record_by_key(cache_dir, acfg.id, suunto_key_str)
    if not rec:
        return None, False
    return _try_dense_suunto_rec(
        acfg,
        cache_dir,
        paths,
        rec,
        photo_cfg,
        photo_hash,
        manifest,
        strava_by_id,
    )


def _record_dense(
    frames: List[pd.DataFrame],
    stats: CollectStats,
    df: Optional[pd.DataFrame],
    from_cache: bool,
    *,
    plotted_suunto: Set[str],
    plotted_strava: Set[int],
    covered_scores: Set[str],
    score_by_suunto: Dict[str, str],
    score_by_strava: Dict[int, str],
    suunto_key: Optional[str],
    strava_id: Optional[int],
    zulip_score_id: Optional[str],
) -> None:
    if df is None or df.empty:
        return
    frames.append(df)
    stats.dense_sessions += 1
    if from_cache:
        stats.dense_cached += 1
    else:
        stats.dense_parsed += 1
    if suunto_key:
        plotted_suunto.add(suunto_key)
        linked = score_by_suunto.get(suunto_key)
        if linked:
            covered_scores.add(linked)
    if strava_id is not None:
        plotted_strava.add(int(strava_id))
        linked = score_by_strava.get(int(strava_id))
        if linked:
            covered_scores.add(linked)
    if zulip_score_id:
        covered_scores.add(str(zulip_score_id))


def collect_athlete_plot_points(
    acfg,
    cache_dir,
    erg_types,
    require_trainer_for_rowing: bool,
    photo_cfg,
    photo_hash: str,
    paths: Dict[str, Any],
    manifest: dict,
    suunto_cfg,
) -> Tuple[List[pd.DataFrame], CollectStats, Set[str]]:
    """Collect plot frames for one athlete from local cache only."""
    from strava_erg_hr_plot import (
        is_erg_summary,
        load_index,
        prune_parsed_cache,
        save_parsed_manifest,
    )

    stats = CollectStats()
    frames: List[pd.DataFrame] = []
    covered_scores: Set[str] = set()
    plotted_suunto: Set[str] = set()
    plotted_strava: Set[int] = set()
    active_manifest_keys: Set[str] = set()

    idx = load_index(paths["index"])
    strava_by_id = {
        int(a["id"]): a
        for a in idx.get("activities", [])
        if is_erg_summary(a, erg_types, require_trainer_for_rowing)
    }
    use_suunto = suunto_sync_enabled_for_athlete(suunto_cfg, acfg.id)

    merged = build_merged_erg_sessions_for_athlete(
        cache_dir,
        acfg.id,
        athlete_label=acfg.label,
        erg_types=erg_types,
        require_trainer_for_rowing=require_trainer_for_rowing,
    )

    score_by_suunto: Dict[str, str] = {}
    score_by_strava: Dict[int, str] = {}
    for session in merged:
        if session.get("parent_zulip_score_id"):
            continue
        zid = session.get("zulip_score_id")
        if not zid:
            continue
        zid_str = str(zid)
        for key in _session_suunto_keys(session):
            score_by_suunto[key] = zid_str
        sid = session.get("strava_activity_id")
        if sid is not None:
            score_by_strava[int(sid)] = zid_str

    for session in merged:
        if session.get("parent_zulip_score_id"):
            continue
        zid = session.get("zulip_score_id")
        zid_str = str(zid) if zid else None
        suunto_keys = _session_suunto_keys(session) if use_suunto else []
        strava_id_raw = session.get("strava_activity_id")
        strava_id = int(strava_id_raw) if strava_id_raw is not None else None

        if suunto_keys:
            for suunto_key_str in suunto_keys:
                if suunto_key_str in plotted_suunto:
                    if zid_str:
                        covered_scores.add(zid_str)
                    continue
                dense_df, from_cache = _collect_dense_for_suunto_key(
                    acfg,
                    cache_dir,
                    paths,
                    suunto_key_str,
                    photo_cfg,
                    photo_hash,
                    manifest,
                    strava_by_id,
                )
                if dense_df is not None:
                    active_manifest_keys.add(suunto_key_str)
                linked_strava = None
                rec = suunto_workout_record_by_key(cache_dir, acfg.id, suunto_key_str)
                if rec and rec.get("strava_activity_id") is not None:
                    linked_strava = int(rec["strava_activity_id"])
                _record_dense(
                    frames,
                    stats,
                    dense_df,
                    from_cache,
                    plotted_suunto=plotted_suunto,
                    plotted_strava=plotted_strava,
                    covered_scores=covered_scores,
                    score_by_suunto=score_by_suunto,
                    score_by_strava=score_by_strava,
                    suunto_key=suunto_key_str,
                    strava_id=linked_strava,
                    zulip_score_id=zid_str if len(suunto_keys) == 1 else None,
                )
            if zid_str and suunto_keys and all(k in plotted_suunto for k in suunto_keys):
                covered_scores.add(zid_str)
            continue

        suunto_key_str: Optional[str] = None
        dense_df: Optional[pd.DataFrame] = None
        from_cache = False

        if strava_id is not None:
            act = strava_by_id.get(strava_id)
            if act is not None:
                linked_key = (
                    suunto_key_for_strava(cache_dir, acfg.id, strava_id)
                    if use_suunto
                    else None
                )
                if linked_key and not suunto_key_str:
                    rec = suunto_workout_record_by_key(
                        cache_dir, acfg.id, str(linked_key)
                    )
                    if rec:
                        dense_df, from_cache = _try_dense_suunto_rec(
                            acfg,
                            cache_dir,
                            paths,
                            rec,
                            photo_cfg,
                            photo_hash,
                            manifest,
                            strava_by_id,
                        )
                        if dense_df is not None:
                            suunto_key_str = str(linked_key)
                            active_manifest_keys.add(suunto_key_str)
                if dense_df is None or dense_df.empty:
                    dense_df, from_cache = _try_dense_strava_act(
                        acfg,
                        cache_dir,
                        act,
                        photo_cfg,
                        photo_hash,
                        manifest,
                        paths,
                    )
                    if dense_df is not None:
                        active_manifest_keys.add(str(strava_id))

        _record_dense(
            frames,
            stats,
            dense_df,
            from_cache,
            plotted_suunto=plotted_suunto,
            plotted_strava=plotted_strava,
            covered_scores=covered_scores,
            score_by_suunto=score_by_suunto,
            score_by_strava=score_by_strava,
            suunto_key=suunto_key_str,
            strava_id=strava_id,
            zulip_score_id=zid_str,
        )

    for rec in (
        list_suunto_erg_workouts(cache_dir, acfg.id, suunto_cfg) if use_suunto else []
    ):
        key = str(rec.get("key") or "")
        if not key or key in plotted_suunto:
            continue
        strava_id = rec.get("strava_activity_id")
        strava_id_int = int(strava_id) if strava_id is not None else None
        if strava_id_int is not None and strava_id_int in plotted_strava:
            continue
        dense_df, from_cache = _try_dense_suunto_rec(
            acfg,
            cache_dir,
            paths,
            rec,
            photo_cfg,
            photo_hash,
            manifest,
            strava_by_id,
        )
        if dense_df is not None:
            active_manifest_keys.add(key)
        _record_dense(
            frames,
            stats,
            dense_df,
            from_cache,
            plotted_suunto=plotted_suunto,
            plotted_strava=plotted_strava,
            covered_scores=covered_scores,
            score_by_suunto=score_by_suunto,
            score_by_strava=score_by_strava,
            suunto_key=key,
            strava_id=strava_id_int,
            zulip_score_id=None,
        )

    for aid, act in strava_by_id.items():
        if aid in plotted_strava:
            continue
        linked = suunto_key_for_strava(cache_dir, acfg.id, aid) if use_suunto else None
        if linked and str(linked) in plotted_suunto:
            continue
        dense_df, from_cache = _try_dense_strava_act(
            acfg,
            cache_dir,
            act,
            photo_cfg,
            photo_hash,
            manifest,
            paths,
        )
        if dense_df is not None:
            active_manifest_keys.add(str(aid))
        _record_dense(
            frames,
            stats,
            dense_df,
            from_cache,
            plotted_suunto=plotted_suunto,
            plotted_strava=plotted_strava,
            covered_scores=covered_scores,
            score_by_suunto=score_by_suunto,
            score_by_strava=score_by_strava,
            suunto_key=str(linked) if linked else None,
            strava_id=aid,
            zulip_score_id=None,
        )

    for score_rec in _load_all_erg_scores_for_athlete(cache_dir, acfg.id):
        score_id = str(score_rec.get("id") or "")
        if not score_id or score_id in covered_scores:
            continue
        sparse = build_sparse_points_from_score(acfg.label, score_rec)
        if sparse is None or sparse.empty:
            continue
        frames.append(sparse)
        stats.sparse_scores += 1
        stats.sparse_points += len(sparse)

    for k, entry in list(manifest.get("activities", {}).items()):
        sk = entry.get("suunto_key")
        if sk and sk in active_manifest_keys:
            active_manifest_keys.add(k)
    prune_parsed_cache(paths, manifest, active_manifest_keys)
    save_parsed_manifest(paths, manifest)

    if stats.dense_sessions or stats.sparse_scores:
        print(
            f"[{acfg.label}] Collect: {stats.dense_sessions} stream session(s) "
            f"({stats.dense_cached} cached, {stats.dense_parsed} parsed), "
            f"{stats.sparse_scores} screenshot score(s) "
            f"({stats.sparse_points} sparse points)"
        )

    return frames, stats, active_manifest_keys
