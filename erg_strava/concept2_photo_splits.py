# -*- coding: utf-8 -*-
"""
Concept2 PM-style split extraction from activity photos + fuzzy alignment to
timeline using stream/FIT-derived pace as a weak prior and Strava photo times.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

# Optional OCR
try:
    import pytesseract
    from PIL import Image, ImageOps
except ImportError:
    pytesseract = None  # type: ignore
    Image = None  # type: ignore
    ImageOps = None  # type: ignore

# Match strava_erg_hr_plot.HR_MIN_PLOT_BPM (skip unphysical HR from photo-derived points).
_HR_MIN_BPM = 50.0

_SPLIT_PATTERNS = [
    re.compile(r"\b(\d{1,2}):(\d{2})\.(\d)\b"),  # 2:05.3
    re.compile(r"\b(\d{1,2}):(\d{2})\b"),  # 2:05
]


def parse_mmss_to_seconds(m: str, s: str, frac: Optional[str] = None) -> float:
    minutes = int(m)
    seconds = int(s)
    out = minutes * 60 + seconds
    if frac is not None and frac.isdigit():
        out += int(frac) / 10.0
    return float(out)


def extract_split_seconds_from_text(text: str) -> List[float]:
    found: List[float] = []
    for pat in _SPLIT_PATTERNS:
        for m in pat.finditer(text or ""):
            g = m.groups()
            if len(g) == 3:
                sec = parse_mmss_to_seconds(g[0], g[1], g[2])
            else:
                sec = parse_mmss_to_seconds(g[0], g[1], None)
            if 55 <= sec <= 620:
                found.append(sec)
    # de-dupe preserving order
    out: List[float] = []
    for x in found:
        if not out or min(abs(x - y) for y in out) > 0.5:
            out.append(x)
    return out


def _ocr_image_splits(image_bytes: bytes) -> Tuple[List[float], str]:
    if pytesseract is None or Image is None:
        return [], ""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        img = ImageOps.autocontrast(img)
        img = img.resize((img.width * 2, img.height * 2))
        cfg = "--psm 6"
        text = pytesseract.image_to_string(img, config=cfg)
        splits = extract_split_seconds_from_text(text)
        if splits:
            return splits, text
        # Tighter whitelist pass (helps noisy PM photos)
        cfg2 = '--psm 6 -c tessedit_char_whitelist=0123456789:.'
        text2 = pytesseract.image_to_string(img, config=cfg2)
        splits2 = extract_split_seconds_from_text(text2)
        return splits2, (text + "\n" + text2).strip()
    except Exception:
        return [], ""


def ocr_concept2_splits_from_bytes(image_bytes: bytes) -> Tuple[List[float], str]:
    return _ocr_image_splits(image_bytes)


def activity_is_suunto_like(detail: dict, substrings: Tuple[str, ...]) -> bool:
    dn = (detail.get("device_name") or "").lower()
    return any(s in dn for s in substrings)


def parse_strava_datetime(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def photo_elapsed_seconds(photo_created_at: str, activity_start: str) -> float:
    p = parse_strava_datetime(photo_created_at)
    a = parse_strava_datetime(activity_start)
    if p is None or a is None:
        return 0.0
    dt = (p - a.astimezone(p.tzinfo or timezone.utc)).total_seconds()
    return max(0.0, float(dt))


def pick_largest_photo_url(photo: dict) -> Optional[str]:
    urls = photo.get("urls")
    if not isinstance(urls, dict) or not urls:
        return None
    try:
        best_key = max((int(k) for k in urls.keys() if str(k).isdigit()), default=None)
    except ValueError:
        best_key = None
    if best_key is not None and str(best_key) in urls:
        return urls[str(best_key)]
    # fall back to any URL
    for _k, u in sorted(urls.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0):
        if isinstance(u, str) and u.startswith("http"):
            return u
    return None


def fetch_activity_photos(transport, activity_id: int) -> List[dict]:
    r = transport.get(
        f"https://www.strava.com/api/v3/activities/{activity_id}/photos",
        params={"photo_sources": "true", "size": 2048},
        timeout=120,
    )
    if r.status_code != 200:
        return []
    data = r.json()
    return data if isinstance(data, list) else []


def download_binary(transport, url: str) -> Optional[bytes]:
    r = transport.get(url, timeout=120)
    if r.status_code != 200:
        return None
    return r.content


def hr_at_time_linear(t: np.ndarray, hr: np.ndarray, t_query: float) -> float:
    if len(t) == 0:
        return float("nan")
    j = int(np.searchsorted(t, t_query))
    j = max(1, min(j, len(t) - 1))
    t0, t1 = t[j - 1], t[j]
    h0, h1 = hr[j - 1], hr[j]
    if t1 == t0:
        return float(h0)
    w = (t_query - t0) / (t1 - t0)
    return float(h0 + w * (h1 - h0))


def refine_time_with_pace(
    t_inst: np.ndarray,
    split_inst: np.ndarray,
    t_prior: float,
    ocr_split: float,
    window_sec: float,
    tolerance: float,
) -> float:
    """Pick time within window around t_prior that best matches OCR split to noisy pace curve."""
    lo = max(float(t_inst[0]), t_prior - window_sec)
    hi = min(float(t_inst[-1]), t_prior + window_sec)
    m = (t_inst >= lo) & (t_inst <= hi) & np.isfinite(split_inst)
    if not np.any(m):
        return t_prior
    idx = np.where(m)[0]
    err = np.abs(split_inst[idx] - ocr_split)
    best_sub = int(idx[np.argmin(err)])
    if err[np.argmin(err)] <= tolerance:
        return float(t_inst[best_sub])
    return float(t_inst[best_sub])


@dataclass
class PhotoSplitPoint:
    time_sec: float
    split_500: float
    hr: float
    photo_id: str


def build_photo_split_points(
    raw,
    manifest: dict,
    time_window_sec: float,
    split_tolerance_sec: float,
    multi_stagger_sec: float,
) -> List[PhotoSplitPoint]:
    """Correlate OCR splits to HR using photo timestamps + fuzzy pace match."""
    if raw is None or raw.empty:
        return []
    inst = raw.sort_values("time").reset_index(drop=True)
    t_inst = inst["time"].to_numpy(dtype=float)
    hr_inst = inst["hr"].to_numpy(dtype=float)
    dt = np.diff(t_inst, prepend=np.nan)
    dd = np.diff(inst["distance"].to_numpy(dtype=float), prepend=np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        speed = np.where(dt > 0, dd / dt, np.nan)
    split_inst = np.divide(
        500.0,
        speed,
        out=np.full_like(speed, np.nan, dtype=float),
        where=(speed > 0.05),
    )
    valid = (
        (dt > 0)
        & np.isfinite(split_inst)
        & (split_inst > 55)
        & (split_inst < 650)
    )
    if not np.any(valid):
        return []
    tv = t_inst[valid]
    sv = split_inst[valid]
    hv = hr_inst[valid]

    points: List[PhotoSplitPoint] = []
    for ph in manifest.get("photos", []):
        pid = str(ph.get("unique_id") or ph.get("id") or ph.get("uid") or "unknown")
        created = ph.get("created_at") or ph.get("uploaded_at")
        start = manifest.get("activity_start_date")
        if not created or not start:
            continue
        t_photo = photo_elapsed_seconds(created, start)
        splits = ph.get("splits_sec") or []
        if not splits:
            continue
        n = len(splits)
        for i, sp in enumerate(splits):
            if n == 1:
                t_prior = t_photo
            else:
                t_prior = t_photo + (i - (n - 1) / 2.0) * multi_stagger_sec
            t_ref = refine_time_with_pace(tv, sv, t_prior, sp, time_window_sec, split_tolerance_sec)
            hr_v = hr_at_time_linear(tv, hv, t_ref)
            if np.isnan(hr_v) or hr_v < _HR_MIN_BPM:
                continue
            points.append(
                PhotoSplitPoint(
                    time_sec=t_ref,
                    split_500=float(sp),
                    hr=hr_v,
                    photo_id=pid,
                )
            )
    return points


def load_manifest(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def save_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
