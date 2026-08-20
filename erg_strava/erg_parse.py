"""FIT/stream parsing helpers shared by plot, Suunto sync, and session merge."""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

try:
    from fitparse import FitFile
except ImportError:
    FitFile = None  # type: ignore

HR_MIN_PLOT_BPM = 50.0
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)


def fit_has_heartrate(fit_path) -> bool:
    if FitFile is None or not fit_path.is_file():
        return False
    try:
        fit = FitFile(io.BytesIO(fit_path.read_bytes()))
    except Exception:
        return False
    for msg in fit.get_messages("record"):
        for f in msg.fields:
            if f.name == "heart_rate" and f.value is not None:
                return True
    return False


def streams_have_heartrate(stream_path) -> bool:
    import json

    if not stream_path.is_file():
        return False
    try:
        streams = json.loads(stream_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    hr = streams.get("heartrate") or streams.get("heart_rate")
    if not hr or "data" not in hr:
        return False
    return any(x is not None for x in hr["data"])


def records_from_fit(content: bytes) -> pd.DataFrame | None:
    if FitFile is None:
        return None
    try:
        fit = FitFile(io.BytesIO(content))
        times: list[float] = []
        dists: list[float] = []
        hrs: list[float] = []
        for msg in fit.get_messages("record"):
            row = {f.name: f.value for f in msg.fields}
            ts = row.get("timestamp") or row.get("Timestamp")
            if ts is None:
                continue
            if hasattr(ts, "timestamp"):
                t_unix = ts.timestamp()
            elif isinstance(ts, datetime):
                t_unix = ts.replace(tzinfo=timezone.utc).timestamp()
            else:
                try:
                    t_unix = (FIT_EPOCH + timedelta(seconds=float(ts))).timestamp()
                except (TypeError, ValueError):
                    continue
            d = row.get("distance")
            hr = row.get("heart_rate")
            if d is None:
                continue
            times.append(float(t_unix))
            dists.append(float(d))
            hrs.append(float(hr) if hr is not None else np.nan)

        if len(times) < 3:
            return None
        df = pd.DataFrame({"t_abs": times, "distance": dists, "hr": hrs})
        df = df.sort_values("t_abs").drop_duplicates(subset=["t_abs"], keep="last")
        t0 = df["t_abs"].iloc[0]
        df["time"] = df["t_abs"] - t0
        return df[["time", "distance", "hr"]].reset_index(drop=True)
    except Exception:
        return None


def records_from_streams(streams: dict) -> pd.DataFrame | None:
    def series(name: str):
        block = streams.get(name)
        if not block or "data" not in block:
            return None
        return [float(x) if x is not None else np.nan for x in block["data"]]

    t = series("time")
    d = series("distance")
    hr = series("heartrate")
    if t is None or d is None:
        return None
    n = min(len(t), len(d))
    if hr is None:
        hr = [np.nan] * n
    else:
        n = min(n, len(hr))
    hr_n = (hr[:n] if hr is not None else [np.nan] * n)
    if len(hr_n) < n:
        hr_n = list(hr_n) + [np.nan] * (n - len(hr_n))
    df = pd.DataFrame(
        {
            "time": t[:n],
            "distance": d[:n],
            "hr": hr_n[:n],
        }
    )
    if df["time"].isna().all():
        return None
    return df


def samples_to_split_hr_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Instantaneous 500 m split (s) and HR; drop bad rows."""
    df = df.sort_values("time").reset_index(drop=True)
    dt = df["time"].diff()
    dd = df["distance"].diff()
    speed = dd / dt
    split_500 = 500.0 / speed
    mask = (
        (dt > 0)
        & speed.notna()
        & (speed > 0.05)
        & split_500.notna()
        & np.isfinite(split_500)
        & (split_500 > 60)
        & (split_500 < 600)
        & (df["hr"].isna() | (df["hr"] >= HR_MIN_PLOT_BPM))
    )
    out = pd.DataFrame(
        {
            "time": df.loc[mask, "time"].values,
            "split_500": split_500[mask].values,
            "hr": df.loc[mask, "hr"].values,
        }
    )
    return out.reset_index(drop=True)


def rolling_10s_means(track: pd.DataFrame) -> pd.DataFrame:
    """Time-based 10 s rolling mean on split and HR."""
    if track.empty:
        return track
    t = pd.to_timedelta(track["time"], unit="s")
    td = pd.DataFrame(
        {"split_500": track["split_500"].values, "hr": track["hr"].values},
        index=t,
    )
    td = td.sort_index()
    rolled = td.rolling("10s", min_periods=2).mean()
    rolled = rolled.dropna(subset=["split_500", "hr"], how="any")
    time_sec = rolled.index.total_seconds()
    rolled = rolled.reset_index(drop=True)
    rolled["time"] = time_sec.values
    return rolled
