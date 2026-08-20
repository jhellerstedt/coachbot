#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch indoor rowing (erg) activities for configured Strava athletes, cache API
responses, derive 500 m split vs heart rate with 10 s time-based rolling means,
and scatter-plot three date ranges (shared axes).

Authentication: OAuth token files per athlete (same layout as ``lighties/``):

- ``token_dir/strava_token`` (required), optional ``strava_refresh_token`` and
  ``strava_token_expires``, and optional ``token_dir/.env`` with
  ``STRAVA_CLIENT_ID`` / ``STRAVA_CLIENT_SECRET`` for automatic refresh.
  See ``lighties/STRAVA_SETUP.md`` and ``lighties/regenerate_strava_token.py``.

Strava's documented API does not guarantee FIT downloads for activities; this
tool tries export_original first, then falls back to activity streams (same
split/HR pipeline). Cached FIT or stream payloads are reused to respect rate
limits.

For Suunto-recorded indoor rows with heart rate, optional Concept2 display
photos are downloaded, OCR'd for 500 m splits, and aligned to the workout
timeline using each photo's Strava timestamp plus a fuzzy match against the
noisy pace derived from the uploaded file (distance/time).

Dependencies: see erg_strava/requirements.txt (Tesseract for OCR; stravalib for Strava API)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

_ERG_DIR = Path(__file__).resolve().parent
_LIGHTIES_DIR = _ERG_DIR.parent / "lighties"
if str(_ERG_DIR) not in sys.path:
    sys.path.insert(0, str(_ERG_DIR))
if str(_LIGHTIES_DIR) not in sys.path:
    sys.path.insert(0, str(_LIGHTIES_DIR))

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

import numpy as np
import pandas as pd

try:
    from fitparse import FitFile
except ImportError:
    FitFile = None  # type: ignore

from concept2_photo_splits import (
    activity_is_suunto_like,
    build_photo_split_points,
    download_binary,
    fetch_activity_photos,
    load_manifest,
    ocr_concept2_splits_from_bytes,
    pick_largest_photo_url,
    save_manifest,
)
from generate_training_plan import (
    DEFAULT_GYM_NAME_PATTERNS,
    DEFAULT_GYM_SPORT_TYPES,
    DEFAULT_PLAN_TIMEZONE,
    WeekBounds,
    activity_local_date,
    week_bounds_from_monday,
    build_training_summary,
    is_gym_activity,
    plan_week_bounds,
    format_public_weekly_plan_post,
    post_plan_to_zulip,
    previous_week_bounds,
    run_weekly_training_pipeline,
    set_plan_timezone,
    sync_activity_metrics_cache,
    week_contains,
    weekly_plans_dir,
)
from send_to_zulip import send_png_to_zulip
from strava_token_client import get_strava_client
from strava_transport import StravalibStravaTransport, StravaHttpResponse, StravaTransport
from erg_parse import (
    HR_MIN_PLOT_BPM,
    fit_has_heartrate as _fit_has_heartrate,
    records_from_fit as _records_from_fit,
    records_from_streams as _records_from_streams,
    rolling_10s_means,
    samples_to_split_hr_frame,
    streams_have_heartrate as _streams_have_heartrate,
)
from suunto_sync import (
    SuuntoCfg,
    link_suunto_workouts_to_strava,
    list_suunto_endurance_workouts,
    list_suunto_erg_workouts,
    load_suunto_cfg,
    stable_activity_id,
    suunto_fit_path,
    suunto_fit_path_for_strava,
    suunto_key_for_strava,
    suunto_start_dt,
    suunto_workout_as_activity,
    suunto_sync_enabled_for_athlete,
    sync_suunto_workouts_for_athlete,
)

# Zulip destination shared with lighties/lighties-2k-tt.py.
ZULIP_STREAM = "general"
ZULIP_TOPIC = "project-640"
_COACH_BOT_ENV = _ERG_DIR.parent / "coach_bot" / ".env"


def load_coach_bot_dotenv() -> Optional[Path]:
    """Load coach_bot/.env (OPENROUTER_API_KEY, OPENROUTER_MODEL, etc.)."""
    if not _COACH_BOT_ENV.is_file():
        return None
    try:
        from dotenv import load_dotenv

        load_dotenv(_COACH_BOT_ENV, override=False)
    except ImportError:
        for line in _COACH_BOT_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = value
    return _COACH_BOT_ENV


load_coach_bot_dotenv()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# -----------------------------------------------------------------------------
# Strava API helpers
# -----------------------------------------------------------------------------

STRAVA_API = "https://www.strava.com/api/v3"
# Drop sensor glitches / bad streams from split-vs-HR scatter (resting HR is not plotted).
# HR_MIN_PLOT_BPM imported from erg_parse
# Fastest 500 m pace shown on the plot (cap the left side of the x-axis).
SPLIT_500_MIN_DISPLAY_SEC = 1 * 60 + 20  # 1:20
# Slowest 500 m pace shown on the plot (faster = lower seconds; cap the right side of the x-axis).
SPLIT_500_MAX_DISPLAY_SEC = 2 * 60 + 45  # 2:45
# Bump when parse / rolling / photo-align logic changes (invalidates parsed parquet cache).
PARSER_VERSION = "1"
# Subsample KDE input above this many points per athlete per panel.
KDE_MAX_POINTS = 8000
ERG_PLOT_LAST_RUN_FILE = "erg_plot_last_run.json"
# Each panel draws two KDE layers: recent window vs an older comparison window (days before now).
# Period 1: [now - recent_days, now]. Period 2: [now - older_far_days, now - older_near_days).
_PANEL_WINDOWS: Dict[str, Tuple[Tuple[int, int], Tuple[int, int]]] = {
    "historical": ((365, 0), (548, 183)),  # last 12 mo vs 6–18 mo ago
    "six_months": ((183, 0), (274, 91)),  # last 6 mo vs 3–9 mo ago
    "one_month": ((30, 0), (42, 14)),  # last ~30 d vs 2–6 wk ago
}
# Default recent / older pair when plotting a single athlete.
_SINGLE_ATHLETE_PERIOD_COLORS = ("#1f77b4", "#ff7f0e")


def _rate_limit_sleep_headers(headers: Dict[str, str]) -> None:
    try:
        usage = headers.get("X-RateLimit-Usage") or headers.get("x-ratelimit-usage", "0,0")
        limit = headers.get("X-RateLimit-Limit") or headers.get("x-ratelimit-limit", "200,2000")
        rem = int(usage.split(",")[0])
        lim = int(limit.split(",")[0])
    except (ValueError, IndexError):
        return
    if lim > 0 and rem / lim > 0.9:
        time.sleep(0.35)


def strava_get(transport: StravaTransport, url: str, **kwargs) -> StravaHttpResponse:
    r = transport.get(url, timeout=120, **kwargs)
    _rate_limit_sleep_headers(r.headers)
    if r.status_code >= 400:
        raise RuntimeError(
            f"Strava HTTP {r.status_code} for {url}: {r.content[:400]!r}"
        )
    return r


def fetch_athlete_id(transport: StravaTransport) -> int:
    r = strava_get(transport, f"{STRAVA_API}/athlete")
    return int(r.json()["id"])


def resolve_available_strava_credentials(
    athletes: List[AthleteCfg],
    config_base: Path,
) -> Optional[Tuple[StravaTransport, int, Path]]:
    """
    Return the first working Strava transport among athletes' token_dir paths.

    When several athletes share one token directory (e.g. only Jack's OAuth files),
    this yields a single transport reused for every athlete; callers skip athletes
    whose config id does not match the token owner.
    """
    seen: Set[Path] = set()
    default_lighties = (config_base / "../lighties").resolve()
    for acfg in athletes:
        td = acfg.token_dir or default_lighties
        td = td.resolve()
        if td in seen:
            continue
        seen.add(td)
        try:
            client = get_strava_client(td)
            transport = StravalibStravaTransport(client)
            owner_id = fetch_athlete_id(transport)
            return transport, owner_id, td
        except Exception as e:
            print(
                f"Strava credentials unavailable in {td}: {e}",
                file=sys.stderr,
            )
    return None


def fetch_activity_pages(
    transport: StravaTransport,
    stop_if_all: Optional[Callable[[List[dict]], bool]] = None,
    max_pages: int = 5000,
) -> List[dict]:
    """Paginate athlete activities (newest first). Optionally stop early."""
    out: List[dict] = []
    page = 1
    while page <= max_pages:
        r = transport.get(
            f"{STRAVA_API}/athlete/activities",
            params={"page": page, "per_page": 200},
            timeout=120,
        )
        _rate_limit_sleep_headers(r.headers)
        if r.status_code == 429:
            time.sleep(15)
            continue
        if r.status_code >= 400:
            raise RuntimeError(
                f"Strava HTTP {r.status_code} listing activities: {r.content[:400]!r}"
            )
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if stop_if_all and stop_if_all(batch):
            break
        if len(batch) < 200:
            break
        page += 1
    return out


def is_erg_summary(
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


def is_endurance_summary(act: Mapping[str, Any]) -> bool:
    st = str(act.get("sport_type") or act.get("type") or "")
    return st in {"Ride", "Run", "VirtualRide", "VirtualRun"}


def try_download_fit(transport: StravaTransport, activity_id: int) -> Optional[bytes]:
    r = transport.get(
        f"{STRAVA_API}/activities/{activity_id}/export_original",
        timeout=120,
    )
    _rate_limit_sleep_headers(r.headers)
    if r.status_code != 200:
        return None
    return r.content


def extract_fit_bytes(content: bytes) -> Optional[bytes]:
    if not content:
        return None
    if content[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(content))
            for name in z.namelist():
                if name.lower().endswith(".fit"):
                    return z.read(name)
        except zipfile.BadZipFile:
            return None
    if len(content) > 100:
        return content
    return None


def fetch_streams(transport: StravaTransport, activity_id: int) -> Optional[dict]:
    r = transport.get(
        f"{STRAVA_API}/activities/{activity_id}/streams",
        params={
            "keys": "time,distance,heartrate",
            "key_by_type": "true",
        },
        timeout=120,
    )
    _rate_limit_sleep_headers(r.headers)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    return normalize_streams_payload(data)


def fetch_activity_detail(transport: StravaTransport, activity_id: int) -> dict:
    r = strava_get(transport, f"{STRAVA_API}/activities/{activity_id}")
    return r.json()


def is_training_activity_for_adherence(
    act: dict,
    erg_types: frozenset,
    require_trainer_for_rowing: bool,
    gym_cfg: GymCfg,
) -> bool:
    """Erg, on-water rowing, gym, or other named training types for plan adherence."""
    if is_erg_summary(act, erg_types, require_trainer_for_rowing):
        return True
    if is_gym_activity(act, gym_cfg.sport_types, gym_cfg.name_patterns):
        return True
    st = act.get("sport_type") or act.get("type") or ""
    if st == "Rowing" and not act.get("trainer"):
        return True
    return False


def load_cached_activity_details(
    paths: Dict[str, Path], activity_ids: Set[int]
) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    for aid in activity_ids:
        detail_path = paths["details"] / f"{aid}.json"
        if not detail_path.is_file():
            continue
        try:
            out[aid] = json.loads(detail_path.read_text())
        except json.JSONDecodeError:
            continue
    return out


def sync_training_activity_details(
    transport: StravaTransport,
    acfg: AthleteCfg,
    paths: Dict[str, Path],
    activities: List[dict],
    erg_types: frozenset,
    require_trainer_for_rowing: bool,
    gym_cfg: GymCfg,
    refresh: bool = False,
) -> Set[int]:
    """Fetch Strava activity details for adherence/gym parsing (cached under activity_details/)."""
    fetched: Set[int] = set()
    for act in activities:
        if not is_training_activity_for_adherence(
            act, erg_types, require_trainer_for_rowing, gym_cfg
        ):
            continue
        aid = int(act["id"])
        if aid <= 0:
            continue
        detail_path = paths["details"] / f"{aid}.json"
        if detail_path.is_file() and not refresh:
            continue
        try:
            detail = fetch_activity_detail(transport, aid)
            detail_path.write_text(json.dumps(detail))
            fetched.add(aid)
        except Exception as e:
            print(f"[{acfg.label}] Activity detail {aid}: {e}")
    return fetched


def collect_activities_in_weeks(
    athletes: List[AthleteCfg],
    cache_dir: Path,
    week_starts: List[date],
    *,
    suunto_cfg: Optional[SuuntoCfg] = None,
    erg_types: Optional[frozenset] = None,
    require_trainer_for_rowing: bool = True,
) -> List[dict]:
    """Union of Suunto-first activities (plus unmatched Strava erg rows) in given weeks."""
    bounds_list: List[WeekBounds] = [
        week_bounds_from_monday(ws) for ws in week_starts
    ]
    seen: Set[int] = set()
    out: List[dict] = []
    suunto_cfg = suunto_cfg or SuuntoCfg(
        enabled=True,
        primary=True,
        suuntool_path=None,
        session_file=None,
        indoor_rowing_activity_ids=frozenset({57}),
        gym_activity_ids=frozenset({23}),
    )
    erg_types = erg_types or frozenset({"VirtualRow", "Rowing"})

    for acfg in athletes:
        matched_strava: Set[int] = set()
        for rec in list_suunto_erg_workouts(cache_dir, acfg.id, suunto_cfg):
            start = suunto_start_dt(rec)
            if start is None:
                continue
            act = suunto_workout_as_activity(rec, suunto_cfg)
            aid = int(act["id"])
            if aid in seen:
                continue
            for wb in bounds_list:
                if week_contains(wb, start):
                    seen.add(aid)
                    out.append(act)
                    if act.get("strava_activity_id") is not None:
                        matched_strava.add(int(act["strava_activity_id"]))
                    break

        for rec in list_suunto_endurance_workouts(cache_dir, acfg.id, suunto_cfg):
            start = suunto_start_dt(rec)
            if start is None:
                continue
            act = suunto_workout_as_activity(rec, suunto_cfg)
            aid = int(act["id"])
            if aid in seen:
                continue
            for wb in bounds_list:
                if week_contains(wb, start):
                    seen.add(aid)
                    out.append(act)
                    if act.get("strava_activity_id") is not None:
                        matched_strava.add(int(act["strava_activity_id"]))
                    break

        paths = athlete_paths(cache_dir, acfg.id)
        idx = load_index(paths["index"])
        for act in idx.get("activities", []):
            if not (
                is_erg_summary(act, erg_types, require_trainer_for_rowing)
                or is_endurance_summary(act)
            ):
                continue
            aid = int(act["id"])
            if aid in seen or aid in matched_strava:
                continue
            start = parse_start_date(act.get("start_date"))
            if start is None:
                continue
            for wb in bounds_list:
                if week_contains(wb, start):
                    seen.add(aid)
                    legacy = dict(act)
                    legacy.setdefault("source", "strava")
                    out.append(legacy)
                    break
    return out


def load_all_activity_details(
    athletes: List[AthleteCfg],
    cache_dir: Path,
    activity_ids: Set[int],
) -> Dict[int, dict]:
    details: Dict[int, dict] = {}
    for acfg in athletes:
        paths = athlete_paths(cache_dir, acfg.id)
        details.update(load_cached_activity_details(paths, activity_ids))
    return details


def normalize_streams_payload(data: object) -> Optional[dict]:
    """Strava returns either a dict keyed by stream type or a list of stream objects."""
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        out: dict = {}
        for item in data:
            if isinstance(item, dict) and item.get("type"):
                out[str(item["type"])] = item
        return out if out else None
    return None


@dataclass
class StravaCfg:
    optional: bool = True
    link_for_photos: bool = True


@dataclass
class AthleteCfg:
    id: int
    label: str
    token_dir: Optional[Path]


@dataclass
class Concept2PhotoCfg:
    enabled: bool
    device_substrings: Tuple[str, ...]
    time_window_sec: float
    split_tolerance_sec: float
    multi_stagger_sec: float


@dataclass
class GymCfg:
    sport_types: frozenset
    name_patterns: Tuple[str, ...]


def load_config(
    path: Path,
) -> Tuple[
    Path,
    List[AthleteCfg],
    frozenset,
    bool,
    Concept2PhotoCfg,
    GymCfg,
    SuuntoCfg,
    StravaCfg,
]:
    if yaml is None:
        raise RuntimeError("PyYAML is required: pip install pyyaml")
    raw = yaml.safe_load(path.read_text())
    base = path.parent.resolve()
    cache_dir = Path(raw.get("cache_dir", "./erg_strava_cache"))
    if not cache_dir.is_absolute():
        cache_dir = (base / cache_dir).resolve()
    erg_types = frozenset(
        raw.get("erg_sport_types", ["VirtualRow", "Rowing"])
    )
    require_trainer = bool(raw.get("require_trainer_for_rowing", True))

    auth_raw = raw.get("auth") or {}
    if str(auth_raw.get("mode", "token")).lower() == "browser":
        raise ValueError(
            "config auth.mode: browser is no longer supported. Use OAuth token "
            "files per athlete (token_dir with strava_token, same as lighties/). "
            "See lighties/STRAVA_SETUP.md — remove auth.mode: browser from YAML."
        )

    athletes = []
    for a in raw.get("athletes", []):
        tid = int(a["id"])
        label = str(a.get("label", f"athlete_{tid}"))
        td_raw = a.get("token_dir")
        if td_raw:
            td = Path(td_raw)
            if not td.is_absolute():
                td = (base / td).resolve()
        else:
            td = (base / "../lighties").resolve()
        athletes.append(AthleteCfg(id=tid, label=label, token_dir=td))
    pc = raw.get("concept2_photos") or {}
    photo_cfg = Concept2PhotoCfg(
        enabled=bool(pc.get("enabled", True)),
        device_substrings=tuple(
            s.lower() for s in pc.get("device_substrings", ["suunto"])
        ),
        time_window_sec=float(pc.get("time_window_sec", 25)),
        split_tolerance_sec=float(pc.get("split_tolerance_sec", 18)),
        multi_stagger_sec=float(pc.get("multi_stagger_sec", 4.0)),
    )
    gc = raw.get("gym") or {}
    gym_types = frozenset(
        gc.get("sport_types", list(DEFAULT_GYM_SPORT_TYPES))
    )
    name_pats = tuple(gc.get("name_patterns", list(DEFAULT_GYM_NAME_PATTERNS)))
    gym_cfg = GymCfg(sport_types=gym_types, name_patterns=name_pats)
    suunto_cfg = load_suunto_cfg(raw, base)
    sc = raw.get("strava") or {}
    strava_cfg = StravaCfg(
        optional=bool(sc.get("optional", True)),
        link_for_photos=bool(sc.get("link_for_photos", True)),
    )
    set_plan_timezone(str(raw.get("plan_timezone", DEFAULT_PLAN_TIMEZONE)))
    return (
        cache_dir,
        athletes,
        erg_types,
        require_trainer,
        photo_cfg,
        gym_cfg,
        suunto_cfg,
        strava_cfg,
    )


def athlete_paths(cache_dir: Path, athlete_id: int) -> Dict[str, Path]:
    root = cache_dir / f"athlete_{athlete_id}"
    return {
        "root": root,
        "index": root / "index.json",
        "streams": root / "streams",
        "fits": root / "fits",
        "details": root / "activity_details",
        "photos": root / "activity_photos",
        "parsed": root / "parsed",
        "metrics": root / "metrics",
    }


def load_index(path: Path) -> dict:
    if not path.is_file():
        return {"activities": []}
    return json.loads(path.read_text())


def strava_activity_index_entry(act: Mapping[str, Any]) -> Dict[str, Any]:
    """Compact Strava list-row fields persisted in athlete ``index.json``.

    Duration fields are required so weekly unprescribed Ride/Run tallies can
    use moving/elapsed time without fetching activity details.
    """
    return {
        "id": int(act["id"]),
        "name": act.get("name"),
        "sport_type": act.get("sport_type") or act.get("type"),
        "start_date": act.get("start_date"),
        "trainer": act.get("trainer"),
        "distance": act.get("distance"),
        "moving_time": act.get("moving_time"),
        "elapsed_time": act.get("elapsed_time"),
    }


def save_index(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def parse_start_date(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def compute_latest_synced_start(activities: List[dict]) -> Optional[str]:
    best_dt: Optional[datetime] = None
    best_raw: Optional[str] = None
    for act in activities:
        raw = act.get("start_date")
        sd = parse_start_date(raw)
        if sd is None:
            continue
        if best_dt is None or sd > best_dt:
            best_dt = sd
            best_raw = str(raw)
    return best_raw


def activity_listing_floor_date(now: Optional[datetime] = None) -> date:
    """Earliest local activity start (Mon) to fetch on incremental Strava listing."""
    return previous_week_bounds(plan_week_bounds(now)).week_start


def page_is_before_listing_floor(batch: List[dict], floor: date) -> bool:
    """True when every activity on the page starts before floor (local Mon)."""
    for act in batch:
        sd = parse_start_date(act.get("start_date"))
        if sd is None:
            return False
        if activity_local_date(sd) >= floor:
            return False
    return True


def page_is_older_than_watermark(batch: List[dict], watermark: datetime) -> bool:
    for act in batch:
        sd = parse_start_date(act.get("start_date"))
        if sd is None or sd > watermark:
            return False
    return True


def fetch_activity_page(transport: StravaTransport, page: int) -> List[dict]:
    while True:
        r = transport.get(
            f"{STRAVA_API}/athlete/activities",
            params={"page": page, "per_page": 200},
            timeout=120,
        )
        _rate_limit_sleep_headers(r.headers)
        if r.status_code == 429:
            time.sleep(15)
            continue
        if r.status_code >= 400:
            raise RuntimeError(
                f"Strava HTTP {r.status_code} listing activities: {r.content[:400]!r}"
            )
        batch = r.json()
        return batch if isinstance(batch, list) else []


def photo_cfg_hash(photo_cfg: Concept2PhotoCfg) -> str:
    parts = (
        str(photo_cfg.enabled),
        ",".join(photo_cfg.device_substrings),
        str(photo_cfg.time_window_sec),
        str(photo_cfg.split_tolerance_sec),
        str(photo_cfg.multi_stagger_sec),
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def activity_fingerprint(
    paths: Dict[str, Path],
    activity_id: int,
    photo_hash: str,
    *,
    suunto_key: Optional[str] = None,
    athlete_id: Optional[int] = None,
    cache_dir: Optional[Path] = None,
) -> str:
    parts = [PARSER_VERSION, photo_hash]
    file_paths: List[Tuple[str, Path]] = []
    if suunto_key and athlete_id is not None and cache_dir is not None:
        sf = suunto_fit_path(cache_dir, athlete_id, suunto_key)
        if sf is not None:
            file_paths.append(("suunto_fit", sf))
    if activity_id > 0:
        file_paths.extend(
            [
                ("fits", paths["fits"] / f"{activity_id}.fit"),
                ("streams", paths["streams"] / f"{activity_id}.json"),
                ("details", paths["details"] / f"{activity_id}.json"),
                ("photo_manifest", paths["photos"] / str(activity_id) / "manifest.json"),
            ]
        )
    elif athlete_id is not None and cache_dir is not None and suunto_key:
        strava_id = None
        from suunto_sync import suunto_workout_record_by_key

        rec = suunto_workout_record_by_key(cache_dir, athlete_id, suunto_key)
        if rec and rec.get("strava_activity_id") is not None:
            strava_id = int(rec["strava_activity_id"])
        if strava_id:
            file_paths.extend(
                [
                    ("fits", paths["fits"] / f"{strava_id}.fit"),
                    ("streams", paths["streams"] / f"{strava_id}.json"),
                    ("details", paths["details"] / f"{strava_id}.json"),
                    ("photo_manifest", paths["photos"] / str(strava_id) / "manifest.json"),
                ]
            )
    for name, p in file_paths:
        if p.is_file():
            st = p.stat()
            parts.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def load_parsed_manifest(paths: Dict[str, Path]) -> dict:
    p = paths["parsed"] / "manifest.json"
    if not p.is_file():
        return {"parser_version": PARSER_VERSION, "photo_cfg_hash": "", "activities": {}}
    return json.loads(p.read_text())


def save_parsed_manifest(paths: Dict[str, Path], data: dict) -> None:
    paths["parsed"].mkdir(parents=True, exist_ok=True)
    (paths["parsed"] / "manifest.json").write_text(json.dumps(data, indent=2))


def parsed_manifest_globals_ok(manifest: dict, photo_hash: str) -> bool:
    return (
        manifest.get("parser_version") == PARSER_VERSION
        and manifest.get("photo_cfg_hash") == photo_hash
    )


def write_parsed_activity_cache(
    paths: Dict[str, Path],
    manifest: dict,
    photo_hash: str,
    activity_id: int,
    df: pd.DataFrame,
    fingerprint: str,
    start_date: Optional[str],
    point_source: str,
    *,
    suunto_key: Optional[str] = None,
) -> None:
    paths["parsed"].mkdir(parents=True, exist_ok=True)
    df.to_parquet(paths["parsed"] / f"{activity_id}.parquet", index=False)
    manifest["parser_version"] = PARSER_VERSION
    manifest["photo_cfg_hash"] = photo_hash
    activities = manifest.setdefault("activities", {})
    manifest_key = suunto_key or str(activity_id)
    activities[manifest_key] = {
        "fingerprint": fingerprint,
        "start_date": start_date,
        "point_source": point_source,
        "activity_id": activity_id,
        "suunto_key": suunto_key,
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    save_parsed_manifest(paths, manifest)


def prune_parsed_cache(
    paths: Dict[str, Path], manifest: dict, active_manifest_keys: Set[str]
) -> None:
    activities = manifest.get("activities", {})
    stale = [k for k in activities if k not in active_manifest_keys]
    for k in stale:
        entry = activities.get(k) or {}
        aid = entry.get("activity_id")
        if aid is None:
            try:
                aid = int(k)
            except ValueError:
                aid = None
        if aid is not None:
            pq = paths["parsed"] / f"{aid}.parquet"
            if pq.is_file():
                pq.unlink()
        del activities[k]


def ensure_parsed_activity_cache(
    acfg: AthleteCfg,
    cache_dir: Path,
    paths: Dict[str, Path],
    suunto_rec: dict,
    photo_cfg: Concept2PhotoCfg,
    photo_hash: str,
    manifest: dict,
    *,
    strava_act: Optional[dict] = None,
) -> Tuple[Optional[pd.DataFrame], bool]:
    """Return (plot points, from_cache) for one Suunto erg workout."""
    suunto_key = str(suunto_rec.get("key") or "")
    strava_id = suunto_rec.get("strava_activity_id")
    strava_id_int = int(strava_id) if strava_id is not None else None
    activity_id = stable_activity_id(suunto_key, strava_id_int)
    fingerprint = activity_fingerprint(
        paths,
        activity_id,
        photo_hash,
        suunto_key=suunto_key,
        athlete_id=acfg.id,
        cache_dir=cache_dir,
    )
    pq = paths["parsed"] / f"{activity_id}.parquet"
    manifest_key = suunto_key or str(activity_id)
    entry = manifest.get("activities", {}).get(manifest_key, {})
    if not entry and activity_id > 0:
        entry = manifest.get("activities", {}).get(str(activity_id), {})
        if entry:
            manifest_key = str(activity_id)
    if (
        parsed_manifest_globals_ok(manifest, photo_hash)
        and entry.get("fingerprint") == fingerprint
        and pq.is_file()
    ):
        return pd.read_parquet(pq), True

    parsed = parse_suunto_workout(
        acfg, cache_dir, suunto_rec, photo_cfg, strava_act=strava_act
    )
    if not parsed:
        if pq.is_file():
            pq.unlink()
        manifest.get("activities", {}).pop(manifest_key, None)
        return None, False
    rolled, start_dt = parsed
    point_source = str(rolled["point_source"].iloc[0]) if not rolled.empty else "stream_10s"
    start_raw = (
        (strava_act or {}).get("start_date")
        or start_dt.isoformat()
    )
    write_parsed_activity_cache(
        paths,
        manifest,
        photo_hash,
        activity_id,
        rolled,
        fingerprint,
        start_raw,
        point_source,
        suunto_key=suunto_key,
    )
    return rolled, False


def sync_athlete(
    acfg: AthleteCfg,
    cache_dir: Path,
    erg_types: frozenset,
    require_trainer_for_rowing: bool,
    force_refresh_streams: bool,
    photo_cfg: Concept2PhotoCfg,
    refresh_photos: bool,
    transport: Optional[StravaTransport],
    suunto_cfg: SuuntoCfg,
    strava_cfg: StravaCfg,
    config_base: Path,
    gym_cfg: GymCfg,
    full_sync: bool = False,
    refresh_suunto: bool = False,
) -> bool:
    """Sync one athlete (Suunto-first). Returns False only when Strava required but unavailable."""
    paths = athlete_paths(cache_dir, acfg.id)
    paths["root"].mkdir(parents=True, exist_ok=True)
    paths["streams"].mkdir(exist_ok=True)
    paths["fits"].mkdir(exist_ok=True)
    paths["details"].mkdir(exist_ok=True)
    paths["photos"].mkdir(exist_ok=True)
    paths["parsed"].mkdir(exist_ok=True)

    photo_hash = photo_cfg_hash(photo_cfg)
    parsed_manifest = load_parsed_manifest(paths)

    if suunto_sync_enabled_for_athlete(suunto_cfg, acfg.id):
        sync_suunto_workouts_for_athlete(
            athlete_id=acfg.id,
            athlete_label=acfg.label,
            cache_dir=cache_dir,
            cfg=suunto_cfg,
            config_base=config_base,
            refresh=refresh_suunto,
        )
    elif suunto_cfg.enabled:
        print(
            f"[{acfg.label}] Suunto sync skipped (not listed in suunto.athlete_ids).",
            file=sys.stderr,
        )

    strava_ok = False
    if transport is not None:
        try:
            token_athlete_id = fetch_athlete_id(transport)
            strava_ok = token_athlete_id == acfg.id
            if not strava_ok:
                print(
                    f"[{acfg.label}] Strava credentials are for athlete "
                    f"{token_athlete_id}, not config id {acfg.id}."
                )
        except Exception as e:
            print(
                f"[{acfg.label}] Strava unavailable ({e}); continuing Suunto-only.",
                file=sys.stderr,
            )

    if not strava_ok and not strava_cfg.optional:
        print(f"[{acfg.label}] Strava required but unavailable.", file=sys.stderr)
        return False

    idx = load_index(paths["index"])
    known = {int(x["id"]): x for x in idx.get("activities", [])}
    pre_known: Set[int] = set(known.keys())
    erg_acts: List[dict] = []
    gym_acts: List[dict] = []

    if strava_ok and transport is not None:
        listing_floor = activity_listing_floor_date()
        print(
            f"[{acfg.label}] Listing Strava activities (cached index has {len(known)} ids"
            f"{', incremental' if not full_sync else ', full'}"
            f"{f', floor {listing_floor.isoformat()}' if not full_sync else ''}..."
        )
        page = 1
        pages_fetched = 0
        while page <= 5000:
            batch = fetch_activity_page(transport, page)
            if not batch:
                break
            pages_fetched += 1
            for act in batch:
                aid = int(act["id"])
                entry = strava_activity_index_entry(act)
                if aid not in known:
                    known[aid] = entry
                else:
                    known[aid].update(entry)
            if not full_sync and page_is_before_listing_floor(batch, listing_floor):
                break
            if len(batch) < 200:
                break
            page += 1

        idx["activities"] = sorted(
            known.values(), key=lambda x: x.get("start_date") or "", reverse=True
        )
        latest = compute_latest_synced_start(idx["activities"])
        if latest:
            idx["latest_synced_start"] = latest
        save_index(paths["index"], idx)
        if not full_sync:
            print(
                f"[{acfg.label}] Incremental Strava listing: {pages_fetched} page(s)"
                f" (floor {listing_floor.isoformat()})"
            )

        erg_acts = [
            a
            for a in idx["activities"]
            if is_erg_summary(a, erg_types, require_trainer_for_rowing)
        ]
        gym_acts = [
            a
            for a in idx["activities"]
            if is_gym_activity(a, gym_cfg.sport_types, gym_cfg.name_patterns)
        ]
        print(
            f"[{acfg.label}] Strava index: {len(erg_acts)} erg, {len(gym_acts)} gym."
        )

    link_suunto_workouts_to_strava(
        athlete_id=acfg.id,
        athlete_label=acfg.label,
        cache_dir=cache_dir,
        cfg=suunto_cfg,
        erg_acts=erg_acts,
        gym_acts=gym_acts,
    )

    strava_by_id = {int(a["id"]): a for a in erg_acts}
    raw_touched: Set[int] = set()

    if strava_ok and transport is not None and strava_cfg.link_for_photos:

        def stream_ok(aid: int) -> bool:
            if force_refresh_streams:
                return False
            p = paths["streams"] / f"{aid}.json"
            return p.is_file() and p.stat().st_size > 10

        def fit_ok(aid: int) -> bool:
            p = paths["fits"] / f"{aid}.fit"
            return p.is_file() and p.stat().st_size > 10

        linked_erg = [
            a
            for a in erg_acts
            if suunto_key_for_strava(cache_dir, acfg.id, int(a["id"])) is not None
        ]
        for act in linked_erg:
            aid = int(act["id"])
            if aid not in pre_known and not fit_ok(aid) and not stream_ok(aid):
                pass
            fit_path = paths["fits"] / f"{aid}.fit"
            stream_path = paths["streams"] / f"{aid}.json"
            if not fit_path.is_file():
                blob = try_download_fit(transport, aid)
                if blob:
                    fit_bytes = extract_fit_bytes(blob)
                    if fit_bytes:
                        fit_path.write_bytes(fit_bytes)
                        raw_touched.add(aid)
            if not stream_ok(aid):
                streams = fetch_streams(transport, aid)
                if streams:
                    stream_path.write_text(json.dumps(streams))
                    raw_touched.add(aid)
            if stream_ok(aid) and _streams_have_heartrate(stream_path):
                known[aid]["has_hr"] = True
            elif fit_path.is_file() and _fit_has_heartrate(fit_path):
                known[aid]["has_hr"] = True

        idx["activities"] = sorted(
            known.values(), key=lambda x: x.get("start_date") or "", reverse=True
        )
        save_index(paths["index"], idx)

        photo_aids: Optional[Set[int]] = None
        if photo_cfg.enabled:
            photo_aids = set()
            for act in linked_erg:
                aid = int(act["id"])
                if aid not in pre_known or refresh_photos:
                    photo_aids.add(aid)
                    continue
                manifest_path = paths["photos"] / str(aid) / "manifest.json"
                if not manifest_path.is_file():
                    photo_aids.add(aid)
            sync_suunto_concept2_photos(
                transport,
                acfg,
                paths,
                linked_erg,
                erg_types,
                require_trainer_for_rowing,
                photo_cfg,
                refresh_photos,
                only_aids=photo_aids,
                raw_touched=raw_touched,
            )

    suunto_erg = list_suunto_erg_workouts(cache_dir, acfg.id, suunto_cfg)
    print(f"[{acfg.label}] {len(suunto_erg)} Suunto erg workout(s) in index.")
    parse_keys: Set[str] = set()
    for rec in suunto_erg:
        key = str(rec.get("key") or "")
        if not key:
            continue
        parse_keys.add(key)
        strava_id = rec.get("strava_activity_id")
        strava_act = (
            strava_by_id.get(int(strava_id)) if strava_id is not None else None
        )
        try:
            ensure_parsed_activity_cache(
                acfg,
                cache_dir,
                paths,
                rec,
                photo_cfg,
                photo_hash,
                parsed_manifest,
                strava_act=strava_act,
            )
        except Exception as e:
            print(
                f"[{acfg.label}] Suunto parse {key}: {e}",
                file=sys.stderr,
            )

    active_manifest_keys = set(parse_keys)
    for k, entry in list(parsed_manifest.get("activities", {}).items()):
        sk = entry.get("suunto_key") or k
        if sk in parse_keys:
            active_manifest_keys.add(k)
    prune_parsed_cache(paths, parsed_manifest, active_manifest_keys)
    save_parsed_manifest(paths, parsed_manifest)
    return True


def _activity_cache_has_hr(
    paths: Dict[str, Path],
    aid: int,
    index_row: Optional[dict] = None,
    *,
    athlete_id: Optional[int] = None,
    cache_dir: Optional[Path] = None,
) -> bool:
    if index_row and index_row.get("has_hr") is True:
        return True
    if athlete_id is not None and cache_dir is not None:
        suunto_fit = suunto_fit_path_for_strava(cache_dir, athlete_id, aid)
        if suunto_fit is not None and _fit_has_heartrate(suunto_fit):
            return True
    return _streams_have_heartrate(paths["streams"] / f"{aid}.json") or _fit_has_heartrate(
        paths["fits"] / f"{aid}.fit"
    )


def sync_suunto_concept2_photos(
    transport: StravaTransport,
    acfg: AthleteCfg,
    paths: Dict[str, Path],
    erg_acts: List[dict],
    erg_types: frozenset,
    require_trainer_for_rowing: bool,
    photo_cfg: Concept2PhotoCfg,
    refresh_photos: bool,
    only_aids: Optional[Set[int]] = None,
    raw_touched: Optional[Set[int]] = None,
) -> None:
    touched = raw_touched if raw_touched is not None else set()
    for act in erg_acts:
        if not is_erg_summary(act, erg_types, require_trainer_for_rowing):
            continue
        aid = int(act["id"])
        if only_aids is not None and aid not in only_aids:
            continue
        detail_path = paths["details"] / f"{aid}.json"
        if refresh_photos or not detail_path.is_file():
            try:
                detail = fetch_activity_detail(transport, aid)
                detail_path.write_text(json.dumps(detail))
            except Exception as e:
                print(f"[{acfg.label}] Activity detail {aid}: {e}")
                continue
        else:
            detail = json.loads(detail_path.read_text())

        if not activity_is_suunto_like(detail, photo_cfg.device_substrings):
            continue

        if not _activity_cache_has_hr(
            paths,
            aid,
            act,
            athlete_id=acfg.id,
            cache_dir=paths["root"].parent,
        ):
            continue

        act_dir = paths["photos"] / str(aid)
        act_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = act_dir / "manifest.json"
        if manifest_path.is_file() and not refresh_photos:
            continue

        photos = fetch_activity_photos(transport, aid)
        if not photos:
            save_manifest(
                manifest_path,
                {
                    "activity_id": aid,
                    "activity_start_date": detail.get("start_date")
                    or act.get("start_date"),
                    "photos": [],
                    "pipeline": "suunto_concept2",
                },
            )
            touched.add(aid)
            print(f"[{acfg.label}] Suunto activity {aid}: no Strava photos")
            continue

        start_date = detail.get("start_date") or act.get("start_date")
        manifest_photos: List[dict] = []
        for ph in photos:
            url = pick_largest_photo_url(ph)
            if not url:
                continue
            uid = str(ph.get("unique_id") or ph.get("id") or ph.get("uid") or url[-16:])
            img_path = act_dir / f"{uid}.img"
            if refresh_photos or not img_path.is_file():
                blob = download_binary(transport, url)
                if not blob:
                    continue
                img_path.write_bytes(blob)
            else:
                blob = img_path.read_bytes()
            splits, raw_text = ocr_concept2_splits_from_bytes(blob)
            manifest_photos.append(
                {
                    "unique_id": uid,
                    "created_at": ph.get("created_at") or ph.get("uploaded_at"),
                    "local_file": img_path.name,
                    "splits_sec": splits,
                    "raw_text": raw_text[:2000],
                }
            )

        save_manifest(
            manifest_path,
            {
                "activity_id": aid,
                "activity_start_date": start_date,
                "photos": manifest_photos,
                "pipeline": "suunto_concept2",
            },
        )
        touched.add(aid)
        nsp = sum(1 for p in manifest_photos if p.get("splits_sec"))
        print(
            f"[{acfg.label}] Suunto activity {aid}: "
            f"{len(manifest_photos)} photos, {nsp} with OCR splits"
        )


def parse_suunto_workout(
    acfg: AthleteCfg,
    cache_dir: Path,
    suunto_rec: dict,
    photo_cfg: Concept2PhotoCfg,
    *,
    strava_act: Optional[dict] = None,
) -> Optional[Tuple[pd.DataFrame, datetime]]:
    """Return (dataframe with split_500, hr, time) and activity start (UTC)."""
    paths = athlete_paths(cache_dir, acfg.id)
    suunto_key = str(suunto_rec.get("key") or "")
    strava_id = suunto_rec.get("strava_activity_id")
    strava_id_int = int(strava_id) if strava_id is not None else None
    activity_id = stable_activity_id(suunto_key, strava_id_int)

    raw: Optional[pd.DataFrame] = None
    suunto_fit = suunto_fit_path(cache_dir, acfg.id, suunto_key)
    if suunto_fit is not None:
        raw = _records_from_fit(suunto_fit.read_bytes())
    if raw is None and strava_id_int is not None:
        fit_path = paths["fits"] / f"{strava_id_int}.fit"
        if fit_path.is_file():
            raw = _records_from_fit(fit_path.read_bytes())
    if raw is None and strava_id_int is not None:
        stream_path = paths["streams"] / f"{strava_id_int}.json"
        if stream_path.is_file():
            streams = json.loads(stream_path.read_text())
            raw = _records_from_streams(streams)
    if raw is None or raw.empty:
        return None

    start_dt = suunto_start_dt(suunto_rec)
    if start_dt is None and strava_act:
        start = strava_act.get("start_date")
        start_dt = parse_start_date(start)
    if start_dt is None and strava_id_int is not None:
        detail_path = paths["details"] / f"{strava_id_int}.json"
        if detail_path.is_file():
            detail = json.loads(detail_path.read_text())
            start_dt = parse_start_date(detail.get("start_date"))
    if start_dt is None:
        start_dt = datetime.now(timezone.utc)

    has_hr = bool(raw["hr"].notna().any())
    manifest_path = (
        paths["photos"] / str(strava_id_int) / "manifest.json"
        if strava_id_int is not None
        else None
    )
    detail: dict = {}
    if strava_id_int is not None:
        detail_path = paths["details"] / f"{strava_id_int}.json"
        if detail_path.is_file():
            detail = json.loads(detail_path.read_text())

    manifest_pre = (
        load_manifest(manifest_path)
        if photo_cfg.enabled and has_hr and manifest_path is not None
        else None
    )
    suunto_manifest = (
        manifest_pre is not None
        and manifest_pre.get("pipeline") == "suunto_concept2"
    )
    use_photos = photo_cfg.enabled and has_hr and strava_id_int is not None and (
        activity_is_suunto_like(detail, photo_cfg.device_substrings) or suunto_manifest
    )
    manifest = manifest_pre if use_photos else None
    if (
        use_photos
        and manifest
        and manifest.get("photos")
        and any(p.get("splits_sec") for p in manifest["photos"])
    ):
        if not manifest.get("activity_start_date"):
            manifest["activity_start_date"] = start_dt.isoformat()
            save_manifest(manifest_path, manifest)
        photo_pts = build_photo_split_points(
            raw,
            manifest,
            photo_cfg.time_window_sec,
            photo_cfg.split_tolerance_sec,
            photo_cfg.multi_stagger_sec,
        )
        if photo_pts:
            rolled = pd.DataFrame(
                {
                    "time": [p.time_sec for p in photo_pts],
                    "split_500": [p.split_500 for p in photo_pts],
                    "hr": [p.hr for p in photo_pts],
                    "point_source": ["concept2_photo"] * len(photo_pts),
                }
            )
            rolled["athlete"] = acfg.label
            rolled["activity_id"] = activity_id
            rolled["suunto_key"] = suunto_key
            rolled["activity_start"] = start_dt
            return rolled, start_dt

    inst = samples_to_split_hr_frame(raw)
    if inst.empty:
        return None
    rolled = rolling_10s_means(inst)
    if rolled.empty:
        return None
    rolled["point_source"] = "stream_10s"
    rolled["athlete"] = acfg.label
    rolled["activity_id"] = activity_id
    rolled["suunto_key"] = suunto_key
    rolled["activity_start"] = start_dt
    return rolled, start_dt


def parse_activity_file(
    acfg: AthleteCfg,
    cache_dir: Path,
    act: dict,
    photo_cfg: Concept2PhotoCfg,
) -> Optional[Tuple[pd.DataFrame, datetime]]:
    """Legacy wrapper: parse via linked Suunto workout or Strava-only fallback."""
    from suunto_sync import suunto_key_for_strava, suunto_workout_record_by_key

    aid = int(act["id"])
    key = suunto_key_for_strava(cache_dir, acfg.id, aid)
    if key:
        rec = suunto_workout_record_by_key(cache_dir, acfg.id, key)
        if rec:
            return parse_suunto_workout(
                acfg, cache_dir, rec, photo_cfg, strava_act=act
            )
    synthetic = {
        "key": f"strava-{aid}",
        "strava_activity_id": aid,
        "startTime": None,
    }
    start = parse_start_date(act.get("start_date"))
    if start:
        synthetic["startTime"] = int(start.timestamp() * 1000)
    return parse_suunto_workout(
        acfg, cache_dir, synthetic, photo_cfg, strava_act=act
    )


def collect_all_points(
    athletes: List[AthleteCfg],
    cache_dir: Path,
    erg_types: frozenset,
    require_trainer_for_rowing: bool,
    photo_cfg: Concept2PhotoCfg,
    suunto_cfg: Optional[SuuntoCfg] = None,
) -> pd.DataFrame:
    from erg_plot_points import collect_athlete_plot_points

    frames: List[pd.DataFrame] = []
    photo_hash = photo_cfg_hash(photo_cfg)
    suunto_cfg = suunto_cfg or SuuntoCfg(
        enabled=True,
        primary=True,
        suuntool_path=None,
        session_file=None,
        indoor_rowing_activity_ids=frozenset({57}),
        gym_activity_ids=frozenset({23}),
    )
    for acfg in athletes:
        paths = athlete_paths(cache_dir, acfg.id)
        paths["parsed"].mkdir(parents=True, exist_ok=True)
        manifest = load_parsed_manifest(paths)
        athlete_frames, _, _ = collect_athlete_plot_points(
            acfg,
            cache_dir,
            erg_types,
            require_trainer_for_rowing,
            photo_cfg,
            photo_hash,
            paths,
            manifest,
            suunto_cfg,
        )
        frames.extend(athlete_frames)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if out.empty:
        return out
    out["activity_start"] = pd.to_datetime(out["activity_start"], utc=True, errors="coerce")
    dropped = int(out["activity_start"].isna().sum())
    if dropped:
        print(
            f"Warning: dropped {dropped} split/HR rows with missing activity_start "
            "(time-window panels need a valid start time).",
            file=sys.stderr,
        )
        out = out.loc[out["activity_start"].notna()].reset_index(drop=True)
    if out.empty:
        return out
    return out.loc[out["hr"] >= HR_MIN_PLOT_BPM].reset_index(drop=True)


def _activity_starts_utc(df: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(df["activity_start"], utc=True, errors="coerce")


def _utc_now_ts(now: datetime) -> pd.Timestamp:
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        return now_ts.tz_localize("UTC")
    return now_ts.tz_convert("UTC")


def panel_period_bounds(
    which: str, period: int, now: datetime
) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """Inclusive start/end UTC timestamps for one KDE layer in a panel."""
    if which not in _PANEL_WINDOWS:
        raise ValueError(which)
    if period not in (1, 2):
        raise ValueError(period)
    now_ts = _utc_now_ts(now)
    recent_days, recent_end_days = _PANEL_WINDOWS[which][0]
    older_far_days, older_near_days = _PANEL_WINDOWS[which][1]
    if period == 1:
        return (
            now_ts - pd.Timedelta(days=recent_days),
            now_ts - pd.Timedelta(days=recent_end_days),
        )
    return (
        now_ts - pd.Timedelta(days=older_far_days),
        now_ts - pd.Timedelta(days=older_near_days),
    )


def panel_period_mask(
    df: pd.DataFrame, which: str, period: int, now: datetime
) -> pd.Series:
    """Mask rows for one comparison window in a panel."""
    start, end = panel_period_bounds(which, period, now)
    starts = _activity_starts_utc(df)
    if period == 2:
        return (starts >= start) & (starts < end)
    return (starts >= start) & (starts <= end)


def _format_yy_mm_dd(ts: pd.Timestamp) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.strftime("%y-%m-%d")


def panel_period_labels(which: str, now: datetime) -> Tuple[str, str]:
    """Legend labels for the two KDE layers in a panel."""
    p1_start, p1_end = panel_period_bounds(which, 1, now)
    p2_start, p2_end = panel_period_bounds(which, 2, now)
    return (
        f"Last {_format_yy_mm_dd(p1_start)} – {_format_yy_mm_dd(p1_end)}",
        f"{_format_yy_mm_dd(p2_start)} – {_format_yy_mm_dd(p2_end)}",
    )


def subplot_mask(df: pd.DataFrame, which: str, now: datetime) -> pd.Series:
    """
    Union of both comparison windows for a panel vs ``now`` (UTC). Use pandas
    timestamps throughout so masks compare consistently with ``activity_start``.
    """
    return panel_period_mask(df, which, 1, now) | panel_period_mask(
        df, which, 2, now
    )


def _subplot_window_bounds(
    which: str, now: datetime, df: pd.DataFrame
) -> Tuple[pd.Timestamp, pd.Timestamp]:
    """Outer calendar bounds for panel titles (aligned with ``subplot_mask``)."""
    p1_start, p1_end = panel_period_bounds(which, 1, now)
    p2_start, p2_end = panel_period_bounds(which, 2, now)
    return min(p1_start, p2_start), max(p1_end, p2_end)


def subplot_title_with_range(base: str, which: str, now: datetime, df: pd.DataFrame) -> str:
    start, end = _subplot_window_bounds(which, now, df)
    return f"{base}\n{_format_yy_mm_dd(start)} to {_format_yy_mm_dd(end)}"


def athlete_labels_in_plot(
    df: pd.DataFrame, athletes: Optional[List[AthleteCfg]] = None
) -> List[str]:
    """Config ``label`` values for athletes with rows in ``df`` (config order if given)."""
    present = set(df["athlete"].unique())
    if athletes:
        return [a.label for a in athletes if a.label in present]
    return sorted(present)


def athlete_period_color_map(plot_athletes: List[str]) -> Dict[Tuple[str, int], str]:
    """Map ``(athlete_label, period)`` to a distinct hex color (period 1=recent, 2=older)."""
    if len(plot_athletes) == 1:
        athlete = plot_athletes[0]
        return {
            (athlete, 1): _SINGLE_ATHLETE_PERIOD_COLORS[0],
            (athlete, 2): _SINGLE_ATHLETE_PERIOD_COLORS[1],
        }
    import matplotlib.colors as mcolors
    import matplotlib.pyplot as plt

    paired = plt.colormaps["Paired"]
    out: Dict[Tuple[str, int], str] = {}
    for i, athlete in enumerate(plot_athletes):
        out[(athlete, 1)] = mcolors.to_hex(paired(i * 2))
        out[(athlete, 2)] = mcolors.to_hex(paired(i * 2 + 1))
    return out


def kde_density_peaks(
    x: np.ndarray,
    y: np.ndarray,
    *,
    n_peaks: int = 2,
    bw_adjust: float = 1.0,
    grid_n: int = 100,
) -> List[Tuple[float, float]]:
    """Return up to ``n_peaks`` local maxima of a 2D Gaussian KDE (split, hr)."""
    from scipy.ndimage import maximum_filter
    from scipy.stats import gaussian_kde

    x_arr = np.asarray(x, dtype=float)
    y_arr = np.asarray(y, dtype=float)
    valid = np.isfinite(x_arr) & np.isfinite(y_arr)
    x_arr = x_arr[valid]
    y_arr = y_arr[valid]
    if len(x_arr) < 4:
        return []

    try:
        kde = gaussian_kde(np.vstack([x_arr, y_arr]))
        kde.set_bandwidth(kde.factor * bw_adjust)
    except np.linalg.LinAlgError:
        return []

    xi = np.linspace(float(x_arr.min()), float(x_arr.max()), grid_n)
    yi = np.linspace(float(y_arr.min()), float(y_arr.max()), grid_n)
    xx, yy = np.meshgrid(xi, yi)
    z = kde(np.vstack([xx.ravel(), yy.ravel()])).reshape(xx.shape)

    neighborhood = max(3, grid_n // 20)
    local_max = z == maximum_filter(z, size=neighborhood)
    scored = [
        (float(xi[col]), float(yi[row]), float(z[row, col]))
        for row, col in np.argwhere(local_max)
    ]
    scored.sort(key=lambda item: item[2], reverse=True)

    min_sep_x = max((float(x_arr.max()) - float(x_arr.min())) * 0.08, 1.0)
    min_sep_y = max((float(y_arr.max()) - float(y_arr.min())) * 0.08, 1.0)
    selected: List[Tuple[float, float]] = []
    for px, py, _ in scored:
        if all(
            abs(px - sx) > min_sep_x or abs(py - sy) > min_sep_y
            for sx, sy in selected
        ):
            selected.append((px, py))
        if len(selected) >= n_peaks:
            break
    return selected


def _format_split_seconds_mmss(sec: float, _pos: Optional[int] = None) -> str:
    """Matplotlib axis tick: 500 m pace stored as seconds → M:SS (or M:SS.s)."""
    if not np.isfinite(sec) or sec < 0:
        return ""
    m = int(sec // 60)
    s = sec - 60 * m
    if abs(s - round(s)) < 0.02:
        return f"{m}:{int(round(s)):02d}"
    return f"{m}:{s:04.1f}"


def plot_three_panels(
    df: pd.DataFrame,
    output: Path,
    now: Optional[datetime] = None,
    athletes: Optional[List[AthleteCfg]] = None,
) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter
    import seaborn as sns

    if df.empty:
        print("No data to plot.")
        return
    now = now or datetime.now(timezone.utc)
    plot_athletes = list(df["athlete"].unique())
    single = len(plot_athletes) == 1
    period_colors = athlete_period_color_map(plot_athletes)
    bw_adjust = 0.9 if single else 1.0

    subplots = [
        ("historical", "12 months vs 6–18 mo ago"),
        ("six_months", "6 months vs 3–9 mo ago"),
        ("one_month", "~30 days vs 2–6 wk ago"),
    ]

    x_all = df["split_500"].astype(float)
    y_all = df["hr"].astype(float)
    xmax, xmin = float(x_all.max()), float(x_all.min())
    ymax, ymin = float(y_all.max()), float(y_all.min())
    pad_x = (xmax - xmin) * 0.05 + 1.0
    pad_y = (ymax - ymin) * 0.05 + 1.0
    x_hi = min(xmax + pad_x, SPLIT_500_MAX_DISPLAY_SEC)
    x_lo = max(xmin - pad_x, float(SPLIT_500_MIN_DISPLAY_SEC))
    if x_lo >= x_hi:
        # All splits slower than 2:45 (or narrow range): keep a readable window up to the cap.
        x_hi = float(SPLIT_500_MAX_DISPLAY_SEC)
        x_lo = max(float(SPLIT_500_MIN_DISPLAY_SEC), x_hi - 75.0)

    fig, axes = plt.subplots(
        1,
        3,
        figsize=(13.2, 4.1) if single else (14, 4.5),
        sharex=True,
        sharey=True,
    )
    kde_common = dict(
        x="split_500",
        y="hr",
        fill=False,
        levels=12,
        thresh=0.05,
        bw_adjust=bw_adjust,
        alpha=0.7,
    )

    def _kdeplot_points(
        ax,
        pts,
        *,
        color: str,
        linewidth: float,
        zorder: int,
    ) -> None:
        if pts.empty:
            return
        plot_pts = pts
        if len(plot_pts) > KDE_MAX_POINTS:
            plot_pts = plot_pts.sample(n=KDE_MAX_POINTS, random_state=42)
        sns.kdeplot(
            data=plot_pts,
            color=color,
            linewidth=linewidth,
            ax=ax,
            zorder=zorder,
            **kde_common,
        )
        for px, py in kde_density_peaks(
            plot_pts["split_500"].to_numpy(),
            plot_pts["hr"].to_numpy(),
            n_peaks=2,
            bw_adjust=bw_adjust,
        ):
            ax.plot(
                px,
                py,
                marker="x",
                color=color,
                markersize=7,
                markeredgewidth=1.4,
                linestyle="none",
                zorder=zorder + 1,
            )

    for ax, (key, title) in zip(axes, subplots):
        period_labels = panel_period_labels(key, now)
        # Older window first; recent window last so it renders on top.
        for period in (2, 1):
            period_zorder = 2 if period == 2 else 10
            for athlete_idx, athlete in enumerate(plot_athletes):
                mask = panel_period_mask(df, key, period, now) & (
                    df["athlete"] == athlete
                )
                pts = df.loc[mask]
                _kdeplot_points(
                    ax,
                    pts,
                    color=period_colors[(athlete, period)],
                    linewidth=1.4 if single else 1.2,
                    zorder=period_zorder + athlete_idx,
                )
        if single:
            athlete = plot_athletes[0]
            ax.legend(
                handles=[
                    plt.Line2D(
                        [0],
                        [0],
                        color=period_colors[(athlete, period)],
                        linewidth=1.4,
                        alpha=0.7,
                        label=label,
                    )
                    for period, label in zip((1, 2), period_labels, strict=True)
                ],
                loc="upper right",
                fontsize=8,
                framealpha=0.85,
            )
        ax.set_title(subplot_title_with_range(title, key, now, df))
        ax.set_xlabel("500 m split (MM:SS)")
        ax.grid(True, alpha=0.22 if single else 0.3)

    split_fmt = FuncFormatter(_format_split_seconds_mmss)
    for ax in axes:
        ax.xaxis.set_major_formatter(split_fmt)

    axes[0].set_ylabel("Heart rate (bpm)")
    for ax in axes:
        ax.set_xlim(x_lo, x_hi)
        ax.set_ylim(ymin - pad_y, ymax + pad_y)

    plot_labels = athlete_labels_in_plot(df, athletes)
    if plot_labels:
        fig.suptitle(", ".join(plot_labels), y=1.14 if not single else 1.02)

    if not single:
        handles = []
        for athlete in plot_athletes:
            for period, suffix in ((1, "recent"), (2, "older")):
                handles.append(
                    plt.Line2D(
                        [0],
                        [0],
                        color=period_colors[(athlete, period)],
                        linewidth=1.4,
                        alpha=0.7,
                        label=f"{athlete} ({suffix})",
                    )
                )
        fig.legend(
            handles=handles,
            loc="upper center",
            ncol=min(len(handles), 4),
            bbox_to_anchor=(0.5, 1.14),
            fontsize=8,
        )
        fig.tight_layout()
    else:
        # Tighter top margin since we omit the legend.
        fig.tight_layout(rect=[0, 0, 1, 0.98])
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote plot: {output}")


def erg_plot_last_run_path(cache_dir: Path) -> Path:
    return cache_dir / ERG_PLOT_LAST_RUN_FILE


def load_erg_plot_last_run(cache_dir: Path) -> Optional[dict]:
    path = erg_plot_last_run_path(cache_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_erg_plot_last_run(
    cache_dir: Path,
    run_at: datetime,
    activity_ids: Set[int],
    suunto_keys: Optional[Set[str]] = None,
) -> None:
    path = erg_plot_last_run_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_at": run_at.astimezone(timezone.utc).isoformat(),
        "activity_ids": sorted(int(aid) for aid in activity_ids),
        "suunto_keys": sorted(suunto_keys or []),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _activity_distance_km(act: dict, paths: Dict[str, Path]) -> Optional[float]:
    dist = act.get("distance")
    if dist is None:
        detail_path = paths["details"] / f"{int(act['id'])}.json"
        if detail_path.is_file():
            try:
                detail = json.loads(detail_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                detail = {}
            dist = detail.get("distance")
    if dist is None:
        return None
    try:
        return round(float(dist) / 1000.0, 1)
    except (TypeError, ValueError):
        return None


def _suunto_distance_km(rec: dict) -> Optional[float]:
    dist = rec.get("totalDistance")
    if dist is None:
        return None
    try:
        return round(float(dist) / 1000.0, 1)
    except (TypeError, ValueError):
        return None


def _plotted_erg_activity_lookup(
    df: pd.DataFrame,
    athletes: List[AthleteCfg],
    cache_dir: Path,
    erg_types: frozenset,
    require_trainer: bool,
) -> Dict[int, Tuple[dict, Dict[str, Path]]]:
    if df.empty or "activity_id" not in df.columns:
        return {}
    ids_in_df = {int(aid) for aid in df["activity_id"].unique()}
    out: Dict[int, Tuple[dict, Dict[str, Path]]] = {}
    for acfg in athletes:
        paths = athlete_paths(cache_dir, acfg.id)
        idx = load_index(paths["index"])
        for act in idx.get("activities", []):
            aid = int(act["id"])
            if aid not in ids_in_df or aid in out:
                continue
            if not is_erg_summary(act, erg_types, require_trainer):
                continue
            out[aid] = (act, paths)
    return out


def _plotted_suunto_lookup(
    df: pd.DataFrame,
    athletes: List[AthleteCfg],
    cache_dir: Path,
    suunto_cfg: SuuntoCfg,
) -> Dict[str, dict]:
    if df.empty or "suunto_key" not in df.columns:
        return {}
    keys_in_df = {str(k) for k in df["suunto_key"].dropna().unique() if str(k)}
    out: Dict[str, dict] = {}
    for acfg in athletes:
        for rec in list_suunto_erg_workouts(cache_dir, acfg.id, suunto_cfg):
            key = str(rec.get("key") or "")
            if key and key in keys_in_df:
                out[key] = rec
    return out


def format_new_erg_data_summary(
    df: pd.DataFrame,
    athletes: List[AthleteCfg],
    cache_dir: Path,
    erg_types: frozenset,
    require_trainer: bool,
    last_run: Optional[dict],
    suunto_cfg: Optional[SuuntoCfg] = None,
) -> Optional[str]:
    """Return Zulip caption text for erg activities added since the last plot post."""
    if last_run is None or df.empty or "suunto_key" not in df.columns:
        return None
    run_at = parse_start_date(last_run.get("run_at"))
    if run_at is None:
        return None

    suunto_cfg = suunto_cfg or SuuntoCfg(
        enabled=True,
        primary=True,
        suuntool_path=None,
        session_file=None,
        indoor_rowing_activity_ids=frozenset({57}),
        gym_activity_ids=frozenset({23}),
    )
    prev_keys = {str(k) for k in last_run.get("suunto_keys") or []}
    current_keys = {str(k) for k in df["suunto_key"].dropna().unique() if str(k)}
    new_keys = current_keys - prev_keys
    last_date = activity_local_date(run_at).isoformat()
    if not new_keys:
        return f"new data since {last_date}:\n(none)"

    lookup = _plotted_suunto_lookup(df, athletes, cache_dir, suunto_cfg)
    grouped = (
        df.groupby("suunto_key")
        .agg(activity_start=("activity_start", "min"))
        .sort_values("activity_start", ascending=False)
    )

    lines = [f"new data since {last_date}:"]
    for key in grouped.index:
        key_str = str(key)
        if key_str not in new_keys:
            continue
        rec = lookup.get(key_str, {})
        km = _suunto_distance_km(rec)
        km_str = f"{km:g} km" if km is not None else "? km"
        start_dt = parse_start_date(grouped.loc[key, "activity_start"])
        if start_dt is None:
            start_dt = suunto_start_dt(rec)
        date_str = activity_local_date(start_dt).isoformat() if start_dt else "?"
        strava_part = rec.get("strava_activity_id") or "-"
        lines.append(f"{key_str}, {strava_part}, {km_str}, {date_str}")
    return "\n".join(lines)


def post_plot_to_zulip(
    png_path: Path,
    stream: str = ZULIP_STREAM,
    topic: str = ZULIP_TOPIC,
    initial_comment: Optional[str] = None,
) -> bool:
    """Upload the plot PNG to Zulip (credentials in repo-root ``rrcc-zuliprc``)."""
    if not png_path.is_file():
        print(f"Plot not found, skipping Zulip upload: {png_path}", file=sys.stderr)
        return False
    return send_png_to_zulip(
        os.fspath(png_path),
        stream=stream,
        topic=topic,
        initial_comment=initial_comment,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Strava erg split vs HR (cached).")
    parser.add_argument(
        "config",
        nargs="?",
        default=str(Path(__file__).parent / "config.yaml"),
        help="YAML config path (default: erg_strava/config.yaml)",
    )
    parser.add_argument(
        "--plot-only",
        action="store_true",
        help="Skip Strava sync; use cache only.",
    )
    parser.add_argument(
        "--full-sync",
        action="store_true",
        help="Paginate full Strava history (ignore incremental listing floor).",
    )
    parser.add_argument(
        "--refresh-streams",
        action="store_true",
        help="Re-download streams even if cached.",
    )
    parser.add_argument(
        "--refresh-photos",
        action="store_true",
        help="Re-fetch activity details, photos, and OCR for Suunto / Concept2 pipeline.",
    )
    parser.add_argument(
        "--refresh-suunto",
        action="store_true",
        help="Re-download Suunto workout metadata and FIT files.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="erg_split_hr.png",
        help="Output image path (default: erg_split_hr.png)",
    )
    parser.add_argument(
        "--no-zulip",
        "--no-slack",
        dest="no_zulip",
        action="store_true",
        help="Skip posting the plot to Zulip after saving.",
    )
    parser.add_argument(
        "--no-kagi",
        action="store_true",
        help="Skip Kagi weekly plan generation.",
    )
    parser.add_argument(
        "--no-lifting",
        action="store_true",
        help="Omit weightlifting sessions from the Kagi plan.",
    )
    parser.add_argument(
        "--refresh-gym-metrics",
        action="store_true",
        help="Re-parse gym activity descriptions via Kagi (metrics cache).",
    )
    parser.add_argument(
        "--refresh-season-plan",
        action="store_true",
        help="LLM-regenerate macro targets and rewrite season_master_plan.md (overwrites macro edits).",
    )
    args = parser.parse_args()
    cfg_path = Path(args.config).resolve()
    if not cfg_path.is_file():
        print(f"Config not found: {cfg_path}", file=sys.stderr)
        print("Copy erg_strava/config.example.yaml to erg_strava/config.yaml and edit.", file=sys.stderr)
        sys.exit(1)

    try:
        cache_dir, athletes, erg_types, require_trainer, photo_cfg, gym_cfg, suunto_cfg, strava_cfg = (
            load_config(cfg_path)
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    raw_config = yaml.safe_load(cfg_path.read_text()) if yaml else {}

    if not args.plot_only:
        target_week = plan_week_bounds()
        prev_week = previous_week_bounds(target_week)
        config_base = cfg_path.parent.resolve()
        creds = resolve_available_strava_credentials(athletes, config_base)
        transport: Optional[StravaTransport] = None
        token_owner_id: Optional[int] = None
        if creds is None:
            if strava_cfg.optional:
                print(
                    "No Strava credentials found; running Suunto-first sync only.",
                    file=sys.stderr,
                )
            else:
                print(
                    "No Strava credentials found; skipping API sync (cache/plot only).",
                    file=sys.stderr,
                )
        else:
            transport, token_owner_id, token_dir = creds
        try:
            for acfg in athletes:
                acfg_transport = transport
                if transport is not None and token_owner_id != acfg.id:
                    print(
                        f"[{acfg.label}] Strava sync skipped: only credentials "
                        f"for athlete {token_owner_id}."
                    )
                    acfg_transport = None
                if not sync_athlete(
                    acfg,
                    cache_dir,
                    erg_types,
                    require_trainer_for_rowing=require_trainer,
                    force_refresh_streams=args.refresh_streams,
                    photo_cfg=photo_cfg,
                    refresh_photos=args.refresh_photos,
                    transport=acfg_transport,
                    suunto_cfg=suunto_cfg,
                    strava_cfg=strava_cfg,
                    config_base=config_base,
                    gym_cfg=gym_cfg,
                    full_sync=args.full_sync,
                    refresh_suunto=args.refresh_suunto,
                ):
                    continue
                if acfg_transport is not None and not args.no_kagi:
                    week_acts = collect_activities_in_weeks(
                        [acfg],
                        cache_dir,
                        [prev_week.week_start, target_week.week_start],
                        suunto_cfg=suunto_cfg,
                        erg_types=erg_types,
                        require_trainer_for_rowing=require_trainer,
                    )
                    if week_acts:
                        paths = athlete_paths(cache_dir, acfg.id)
                        n = len(
                            sync_training_activity_details(
                                acfg_transport,
                                acfg,
                                paths,
                                week_acts,
                                erg_types,
                                require_trainer,
                                gym_cfg,
                                refresh=args.refresh_photos,
                            )
                        )
                        if n:
                            print(
                                f"[{acfg.label}] Fetched {n} training activity "
                                "detail(s) for weekly plan / adherence."
                            )
        finally:
            if transport is not None:
                transport.close()

    df = collect_all_points(
        athletes,
        cache_dir,
        erg_types,
        require_trainer,
        photo_cfg,
        suunto_cfg=suunto_cfg,
    )
    out = Path(args.output)
    if not out.is_absolute():
        out = (cfg_path.parent / out).resolve()
    plot_three_panels(df, out, athletes=athletes)
    if not args.no_zulip and not df.empty and out.is_file():
        last_run = load_erg_plot_last_run(cache_dir)
        summary = format_new_erg_data_summary(
            df,
            athletes,
            cache_dir,
            erg_types,
            require_trainer,
            last_run,
            suunto_cfg=suunto_cfg,
        )
        if post_plot_to_zulip(out, initial_comment=summary):
            suunto_keys = {
                str(k) for k in df.get("suunto_key", pd.Series(dtype=str)).dropna() if str(k)
            }
            save_erg_plot_last_run(
                cache_dir,
                datetime.now(timezone.utc),
                {int(aid) for aid in df["activity_id"].unique()},
                suunto_keys=suunto_keys,
            )

    if not args.no_kagi and not df.empty and OPENROUTER_API_KEY:
        summary = build_training_summary(df)
        print("\n" + summary)
        target_week = plan_week_bounds()
        prev_week = previous_week_bounds(target_week)
        week_activities = collect_activities_in_weeks(
            athletes,
            cache_dir,
            [prev_week.week_start, target_week.week_start],
            suunto_cfg=suunto_cfg,
            erg_types=erg_types,
            require_trainer_for_rowing=require_trainer,
        )
        activity_ids = {int(a["id"]) for a in week_activities}
        activity_details = load_all_activity_details(
            athletes, cache_dir, activity_ids
        )
        tonnage_window_start = previous_week_bounds(
            plan_week_bounds()
        ).week_start
        metrics_activities = [
            a
            for a in week_activities
            if parse_start_date(a.get("start_date"))
            and tonnage_window_start
            <= activity_local_date(parse_start_date(a.get("start_date")))  # type: ignore[arg-type]
            <= plan_week_bounds().week_end
        ]
        activity_metrics = sync_activity_metrics_cache(
            athletes,
            cache_dir,
            athlete_paths,
            metrics_activities,
            activity_details,
            gym_cfg.sport_types,
            gym_cfg.name_patterns,
            token=OPENROUTER_API_KEY,
            erg_df=df,
            refresh=args.refresh_gym_metrics,
        )
        if activity_metrics:
            print(
                f"Activity metrics cached/updated for {len(activity_metrics)} session(s).",
                flush=True,
            )
        try:
            from erg_session_merge import rebuild_merged_erg_sessions_for_athlete

            for acfg in athletes:
                rebuild_merged_erg_sessions_for_athlete(
                    cache_dir,
                    acfg.id,
                    athlete_label=acfg.label,
                    erg_types=erg_types,
                    require_trainer_for_rowing=require_trainer,
                )
        except Exception as e:
            print(f"Merged erg session rebuild: {e}", flush=True)
        report, record = run_weekly_training_pipeline(
            summary,
            token=OPENROUTER_API_KEY,
            cache_dir=cache_dir,
            week_activities=week_activities,
            activity_details=activity_details,
            gym_types=gym_cfg.sport_types,
            gym_name_patterns=gym_cfg.name_patterns,
            include_lifting=not args.no_lifting,
            activity_metrics=activity_metrics,
            erg_df=df,
            erg_types=erg_types,
            require_trainer_for_rowing=require_trainer,
            config_path=cfg_path,
            zuliprc_path=Path(
                os.environ.get("ZULIPRC_PATH", str(_ERG_DIR.parent / "rrcc-zuliprc"))
            ),
            season_config=raw_config,
            refresh_season_plan=args.refresh_season_plan,
        )
        print("\n=== Weekly training report ===\n")
        print(report)
        print(
            f"\nCached plan: {record.week_id} "
            f"({weekly_plans_dir(cache_dir) / (record.week_id + '.json')})",
            flush=True,
        )
        if not args.no_zulip:
            post_plan_to_zulip(
                format_public_weekly_plan_post(record),
                ZULIP_STREAM,
                ZULIP_TOPIC,
            )
    elif not args.no_kagi and not OPENROUTER_API_KEY:
        print(
            "Skipping weekly plan: OPENROUTER_API_KEY not set. "
            "Add it to coach_bot/.env (copy from coach_bot/.env.example).",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
