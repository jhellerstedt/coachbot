"""Derive session/part heart rate from interval rows when the vision model omits it."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence


def _row_hr(row: Mapping[str, Any]) -> Optional[float]:
    for key in ("avg_hr", "hr", "heart_rate", "hr_bpm"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            val = float(raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            continue
    return None


def _row_weight_sec(row: Mapping[str, Any]) -> float:
    raw = row.get("time_sec")
    if raw is not None:
        try:
            val = float(raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    dist = row.get("distance_m")
    split = row.get("split_500_sec")
    if dist is not None and split is not None:
        try:
            return float(dist) * float(split) / 500.0
        except (TypeError, ValueError):
            pass
    return 1.0


def weighted_avg_hr(rows: Sequence[Mapping[str, Any]]) -> Optional[float]:
    """Time-weighted average HR across interval or part rows."""
    total = 0.0
    weight = 0.0
    for row in rows:
        hr = _row_hr(row)
        if hr is None:
            continue
        w = _row_weight_sec(row)
        total += hr * w
        weight += w
    if weight <= 0:
        return None
    return round(total / weight, 1)


def _extraction_intervals(
    extractions: Sequence[Mapping[str, Any]], screenshot_index: Any
) -> List[Dict[str, Any]]:
    for ex in extractions:
        if ex.get("index") != screenshot_index:
            continue
        metrics = ex.get("metrics")
        if not isinstance(metrics, dict):
            return []
        intervals = metrics.get("intervals") or []
        if not isinstance(intervals, list):
            return []
        return [row for row in intervals if isinstance(row, dict)]
    return []


def enrich_erg_metrics_hr(
    metrics: Dict[str, Any],
    extractions: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Fill missing avg_hr on intervals, session_parts, and session totals."""
    out = dict(metrics)
    intervals = [
        dict(row)
        for row in (out.get("intervals") or [])
        if isinstance(row, dict)
    ]
    for row in intervals:
        if _row_hr(row) is None:
            continue
        if row.get("avg_hr") is None:
            row["avg_hr"] = _row_hr(row)
    if intervals:
        out["intervals"] = intervals

    parts = [
        dict(part)
        for part in (out.get("session_parts") or [])
        if isinstance(part, dict)
    ]
    for part in parts:
        if _row_hr(part) is not None:
            if part.get("avg_hr") is None:
                part["avg_hr"] = _row_hr(part)
            continue
        idx = part.get("screenshot_index")
        source_intervals = (
            _extraction_intervals(extractions or [], idx)
            if idx is not None and extractions
            else []
        )
        derived = weighted_avg_hr(source_intervals) if source_intervals else None
        if derived is not None:
            part["avg_hr"] = derived
    if parts:
        out["session_parts"] = parts

    if _row_hr(out) is None:
        session_hr = weighted_avg_hr(parts) if parts else None
        if session_hr is None and intervals:
            session_hr = weighted_avg_hr(intervals)
        if session_hr is not None:
            out["avg_hr"] = session_hr

    return out
