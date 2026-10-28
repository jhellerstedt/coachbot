"""Hammerhead + SRM bike power/HR points for the shared-power KDE plot."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from erg_parse import FIT_EPOCH, HR_MIN_PLOT_BPM

try:
    from fitparse import FitFile
except ImportError:
    FitFile = None  # type: ignore

# Concept2: watts = 2.80 / pace^3, pace in seconds per metre.
CONCEPT2_WATT_FACTOR = 2.80
BIKE_POWER_SINCE = date(2025, 3, 7)
CYCLING_ACTIVITY_ID = 2
_HAMMERHEAD = "hammerhead"
_SRM = "srm"


def split_seconds_to_watts(split_s: float) -> float:
    """Map a 500 m split in seconds to Concept2 watts."""
    pace = float(split_s) / 500.0
    if pace <= 0:
        return float("nan")
    return CONCEPT2_WATT_FACTOR / pace**3


def watts_to_split_seconds(watts: float) -> float:
    """Inverse of :func:`split_seconds_to_watts`."""
    if watts <= 0:
        return float("nan")
    pace = (CONCEPT2_WATT_FACTOR / float(watts)) ** (1.0 / 3.0)
    return pace * 500.0


def add_concept2_watts(df: pd.DataFrame) -> pd.DataFrame:
    """Copy ``df`` with a ``watts`` column derived from ``split_500``."""
    out = df.copy()
    if out.empty or "split_500" not in out.columns:
        out["watts"] = pd.Series(dtype=float)
        return out
    out["watts"] = out["split_500"].astype(float).map(split_seconds_to_watts)
    return out


def _device_names(fit: Any) -> set[str]:
    names: set[str] = set()
    for msg in fit.get_messages("device_info"):
        for field in msg.fields:
            if field.name == "manufacturer" and field.value is not None:
                names.add(str(field.value).casefold())
    return names


def fit_bytes_are_hammerhead_srm(content: bytes) -> bool:
    """True when the file was recorded on a Hammerhead with an SRM power meter."""
    if FitFile is None:
        return False
    try:
        import io

        names = _device_names(FitFile(io.BytesIO(content)))
    except Exception:
        return False
    return _HAMMERHEAD in names and _SRM in names


def _record_timestamp(ts: Any) -> Optional[float]:
    if ts is None:
        return None
    if hasattr(ts, "timestamp"):
        return float(ts.timestamp())
    if isinstance(ts, datetime):
        return float(ts.replace(tzinfo=timezone.utc).timestamp())
    try:
        return float((FIT_EPOCH + timedelta(seconds=float(ts))).timestamp())
    except (TypeError, ValueError):
        return None


def power_hr_samples_from_fit(content: bytes) -> Optional[pd.DataFrame]:
    """1 Hz-ish power and HR samples. None when the file has no power."""
    if FitFile is None:
        return None
    import io

    try:
        fit = FitFile(io.BytesIO(content))
    except Exception:
        return None
    times: List[float] = []
    watts: List[float] = []
    hrs: List[float] = []
    for msg in fit.get_messages("record"):
        row = {field.name: field.value for field in msg.fields}
        t_unix = _record_timestamp(row.get("timestamp"))
        power = row.get("power")
        if t_unix is None or power is None:
            continue
        try:
            power_f = float(power)
        except (TypeError, ValueError):
            continue
        if power_f <= 0:
            continue
        hr = row.get("heart_rate")
        times.append(t_unix)
        watts.append(power_f)
        hrs.append(float(hr) if hr is not None else np.nan)
    if len(times) < 3:
        return None
    df = pd.DataFrame({"time": times, "watts": watts, "hr": hrs})
    df = df.sort_values("time").reset_index(drop=True)
    df["time"] = df["time"] - float(df["time"].iloc[0])
    df = df.loc[df["hr"].notna() & (df["hr"] >= HR_MIN_PLOT_BPM)].reset_index(drop=True)
    if len(df) < 3:
        return None
    return df


def rolling_10s_power_hr(track: pd.DataFrame) -> pd.DataFrame:
    """Time-based 10 s rolling mean of power and HR, matching the erg plot."""
    if track.empty:
        return track
    t = pd.to_timedelta(track["time"], unit="s")
    td = pd.DataFrame(
        {"watts": track["watts"].values, "hr": track["hr"].values},
        index=t,
    ).sort_index()
    rolled = td.rolling("10s", min_periods=2).mean()
    rolled = rolled.dropna(subset=["watts", "hr"], how="any")
    time_sec = rolled.index.total_seconds()
    rolled = rolled.reset_index(drop=True)
    rolled["time"] = time_sec
    return rolled


def bike_power_dir(cache_dir: Path, athlete_id: int) -> Path:
    return cache_dir / f"athlete_{athlete_id}" / "bike_power"


def _index_path(cache_dir: Path, athlete_id: int) -> Path:
    return bike_power_dir(cache_dir, athlete_id) / "index.json"


def load_bike_power_index(cache_dir: Path, athlete_id: int) -> Dict[str, Any]:
    path = _index_path(cache_dir, athlete_id)
    if not path.is_file():
        return {"workouts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"workouts": {}}
    if not isinstance(data, dict):
        return {"workouts": {}}
    data.setdefault("workouts", {})
    return data


def save_bike_power_index(cache_dir: Path, athlete_id: int, index: Dict[str, Any]) -> None:
    path = _index_path(cache_dir, athlete_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2), encoding="utf-8")


def _start_dt(workout: Dict[str, Any]) -> Optional[datetime]:
    raw = workout.get("startTime")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(float(raw) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def rides_to_fetch(
    items: List[Dict[str, Any]],
    known: Dict[str, Any],
    since: date,
) -> tuple[List[Dict[str, Any]], bool]:
    """Return new cycling workouts on this page, and whether paging should stop.

    ``items`` are newest-first. Stop once a page extends before ``since``.
    """
    if not items:
        return [], True
    selected: List[Dict[str, Any]] = []
    stop = False
    for workout in items:
        start = _start_dt(workout)
        if start is None:
            continue
        if start.date() < since:
            stop = True
            continue
        if int(workout.get("activityId") or 0) != CYCLING_ACTIVITY_ID:
            continue
        key = str(workout.get("key") or "")
        if not key or key in known:
            continue
        selected.append(workout)
    return selected, stop


def sync_bike_power_fits(
    cache_dir: Path,
    athlete_id: int,
    *,
    list_page: Callable[[int], List[Dict[str, Any]]],
    download_fit: Callable[[str, Path], bytes],
    since: date = BIKE_POWER_SINCE,
    max_pages: int = 200,
) -> int:
    """Download cycling FITs and keep Hammerhead rides that include SRM power.

    Returns the number of newly kept rides.
    """
    index = load_bike_power_index(cache_dir, athlete_id)
    known: Dict[str, Any] = index["workouts"]
    root = bike_power_dir(cache_dir, athlete_id)
    fit_dir = root / "fits"
    fit_dir.mkdir(parents=True, exist_ok=True)
    kept_new = 0
    for page in range(max_pages):
        items = list_page(page * 100)
        selected, stop = rides_to_fetch(items, known, since)
        for workout in selected:
            key = str(workout["key"])
            start = _start_dt(workout)
            dest = fit_dir / f"{key}.fit"
            try:
                content = download_fit(key, dest)
            except Exception as exc:
                print(f"Bike FIT {key} skipped: {exc}", flush=True)
                if dest.is_file():
                    dest.unlink()
                continue
            if not dest.is_file():
                dest.write_bytes(content)
            else:
                content = dest.read_bytes()
            kept = fit_bytes_are_hammerhead_srm(content) and (
                power_hr_samples_from_fit(content) is not None
            )
            known[key] = {
                "start": start.isoformat() if start else None,
                "kept": kept,
            }
            if kept:
                kept_new += 1
            elif dest.is_file():
                dest.unlink()
        if stop or len(items) < 100:
            break
    index["since"] = since.isoformat()
    save_bike_power_index(cache_dir, athlete_id, index)
    return kept_new


def collect_bike_power_points(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
) -> pd.DataFrame:
    """10 s power/HR points for kept Hammerhead + SRM rides."""
    index = load_bike_power_index(cache_dir, athlete_id)
    fit_dir = bike_power_dir(cache_dir, athlete_id) / "fits"
    frames: List[pd.DataFrame] = []
    for key, rec in (index.get("workouts") or {}).items():
        if not isinstance(rec, dict) or not rec.get("kept"):
            continue
        path = fit_dir / f"{key}.fit"
        if not path.is_file():
            continue
        samples = power_hr_samples_from_fit(path.read_bytes())
        if samples is None or samples.empty:
            continue
        rolled = rolling_10s_power_hr(samples)
        if rolled.empty:
            continue
        start_raw = rec.get("start")
        start = pd.Timestamp(start_raw) if start_raw else pd.NaT
        if start is not pd.NaT and start.tzinfo is None:
            start = start.tz_localize("UTC")
        frames.append(
            pd.DataFrame(
                {
                    "athlete": athlete_label,
                    "activity_start": start,
                    "suunto_key": key,
                    "watts": rolled["watts"].values,
                    "hr": rolled["hr"].values,
                }
            )
        )
    if not frames:
        return pd.DataFrame(
            columns=["athlete", "activity_start", "suunto_key", "watts", "hr"]
        )
    return pd.concat(frames, ignore_index=True)


def sync_and_collect_bike_power(
    athletes: List[Any],
    cache_dir: Path,
    suunto_cfg: Any,
    config_base: Path,
) -> pd.DataFrame:
    """Sync Suunto cycling FITs for enabled athletes and return plot points."""
    from suunto_client import SuuntoClient, suunto_sync_enabled_for_athlete

    frames: List[pd.DataFrame] = []
    client: Optional[SuuntoClient] = None
    for athlete in athletes:
        if not suunto_sync_enabled_for_athlete(suunto_cfg, athlete.id):
            continue
        try:
            if client is None:
                client = SuuntoClient(suunto_cfg, config_base)
        except Exception as exc:
            print(
                f"[{athlete.label}] Bike power sync failed: {type(exc).__name__}: {exc}",
                flush=True,
            )
            frames.append(
                collect_bike_power_points(cache_dir, athlete.id, athlete.label)
            )
            continue

        def list_page(offset: int, _client: SuuntoClient = client) -> List[Dict[str, Any]]:
            listed = _client.run(
                [
                    "workouts",
                    "list",
                    "--since",
                    BIKE_POWER_SINCE.isoformat(),
                    "--limit",
                    "100",
                    "--offset",
                    str(offset),
                ]
            )
            items = listed.get("items") if isinstance(listed, dict) else listed
            if not isinstance(items, list):
                return []
            return [item for item in items if isinstance(item, dict)]

        def download(key: str, dest: Path, _client: SuuntoClient = client) -> bytes:
            _client.download_fit(key, dest)
            return dest.read_bytes() if dest.is_file() else b""

        try:
            n = sync_bike_power_fits(
                cache_dir,
                athlete.id,
                list_page=list_page,
                download_fit=download,
            )
            if n:
                print(
                    f"[{athlete.label}] Kept {n} new Hammerhead + SRM ride(s).",
                    flush=True,
                )
        except Exception as exc:
            print(
                f"[{athlete.label}] Bike power sync failed: {type(exc).__name__}: {exc}",
                flush=True,
            )
        frames.append(
            collect_bike_power_points(cache_dir, athlete.id, athlete.label)
        )
    if not frames:
        return pd.DataFrame(
            columns=["athlete", "activity_start", "suunto_key", "watts", "hr"]
        )
    return pd.concat(frames, ignore_index=True)
