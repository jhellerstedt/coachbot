"""Deterministic fixes for multi-screenshot erg session part roles and totals."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from erg_prescription_compare import parse_split_seconds


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


def _extraction_metrics(
    part: Mapping[str, Any],
    extractions: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    idx = part.get("screenshot_index")
    if idx is None:
        return {}
    for ex in extractions:
        if ex.get("index") == idx and isinstance(ex.get("metrics"), dict):
            return ex["metrics"]
    return {}


def _interval_work_signature(metrics: Mapping[str, Any]) -> bool:
    intervals = metrics.get("intervals") or []
    if not isinstance(intervals, list) or not intervals:
        return False
    long_pieces = 0
    for row in intervals:
        if not isinstance(row, dict):
            continue
        try:
            t = float(row.get("time_sec") or 0)
        except (TypeError, ValueError):
            t = 0.0
        if t >= 180:
            long_pieces += 1
    return long_pieces >= 2


def _is_main_interval_piece(
    part: Mapping[str, Any],
    extractions: Sequence[Mapping[str, Any]],
) -> bool:
    dur = _part_duration_sec(part)
    ext = _extraction_metrics(part, extractions)
    if _interval_work_signature(ext):
        return True
    if _interval_work_signature(part):
        return True
    if dur >= 660 and str(ext.get("workout_type") or part.get("workout_type") or "").lower() in (
        "intervals",
        "interval",
    ):
        return True
    if dur >= 660 and dur <= 900:
        split = _part_split_sec(part)
        if split is not None and split < 138:
            return True
    return False


def _recompute_session_totals(parts: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    total_dist = 0.0
    total_time = 0.0
    weighted_split = 0.0
    hr_weighted = 0.0
    hr_weight = 0.0
    for part in parts:
        dist = part.get("distance_m")
        dur = _part_duration_sec(part)
        if dist is not None:
            try:
                total_dist += float(dist)
            except (TypeError, ValueError):
                pass
        if dur > 0:
            total_time += dur
            split = _part_split_sec(part)
            if split is not None:
                weighted_split += split * dur
            hr = part.get("avg_hr")
            if hr is not None:
                try:
                    hr_f = float(hr)
                    hr_weighted += hr_f * dur
                    hr_weight += dur
                except (TypeError, ValueError):
                    pass
    out: Dict[str, Any] = {}
    if total_dist > 0:
        out["distance_m"] = int(round(total_dist))
    if total_time > 0:
        out["duration_sec"] = int(round(total_time))
        if weighted_split > 0:
            avg_split = weighted_split / total_time
            out["avg_split_500_sec"] = round(avg_split, 2)
            from generate_training_plan import _fmt_split

            out["avg_split_500_fmt"] = _fmt_split(avg_split)
    if hr_weight > 0:
        out["avg_hr"] = round(hr_weighted / hr_weight, 1)
    return out


def _assign_roles_by_split_three(parts: Sequence[Mapping[str, Any]]) -> Optional[Dict[int, str]]:
    """Warmup = slowest split, main = fastest, cooldown = middle (3-screenshot sessions)."""
    if len(parts) != 3:
        return None
    splits: List[tuple[int, float]] = []
    for i, part in enumerate(parts):
        split = _part_split_sec(part)
        if split is None:
            return None
        splits.append((i, split))
    if len({split for _, split in splits}) < 2:
        return None
    by_split = sorted(splits, key=lambda row: row[1], reverse=True)
    warmup_i, cooldown_i, main_i = by_split[0][0], by_split[1][0], by_split[2][0]
    return {warmup_i: "warmup", main_i: "main", cooldown_i: "cooldown"}


def _assign_warmup_cooldown_by_split(
    indexed: Sequence[Mapping[str, Any]],
    a: int,
    b: int,
) -> Dict[int, str]:
    split_a = _part_split_sec(indexed[a]) or 0.0
    split_b = _part_split_sec(indexed[b]) or 0.0
    if split_a >= split_b:
        return {a: "warmup", b: "cooldown"}
    return {a: "cooldown", b: "warmup"}


def _assign_warmup_cooldown_by_prescribed_duration(
    indexed: Sequence[Mapping[str, Any]],
    a: int,
    b: int,
    *,
    prescribed_warmup_min: float,
    prescribed_cooldown_min: float,
) -> Dict[int, str]:
    """Pick the WU/CD pairing that minimizes sum of absolute minute errors."""
    dur_a = _part_duration_sec(indexed[a]) / 60.0
    dur_b = _part_duration_sec(indexed[b]) / 60.0
    err_ab = abs(dur_a - prescribed_warmup_min) + abs(dur_b - prescribed_cooldown_min)
    err_ba = abs(dur_b - prescribed_warmup_min) + abs(dur_a - prescribed_cooldown_min)
    if err_ab < err_ba:
        return {a: "warmup", b: "cooldown"}
    if err_ba < err_ab:
        return {a: "cooldown", b: "warmup"}
    return _assign_warmup_cooldown_by_split(indexed, a, b)


def _pair_warmup_cooldown_roles(
    indexed: Sequence[Mapping[str, Any]],
    a: int,
    b: int,
    *,
    prescribed_warmup_min: Optional[float] = None,
    prescribed_cooldown_min: Optional[float] = None,
) -> Dict[int, str]:
    if (
        prescribed_warmup_min is not None
        and prescribed_cooldown_min is not None
        and prescribed_warmup_min != prescribed_cooldown_min
    ):
        return _assign_warmup_cooldown_by_prescribed_duration(
            indexed,
            a,
            b,
            prescribed_warmup_min=float(prescribed_warmup_min),
            prescribed_cooldown_min=float(prescribed_cooldown_min),
        )
    return _assign_warmup_cooldown_by_split(indexed, a, b)


def _build_warmup_main_cooldown_roles(
    indexed: Sequence[Mapping[str, Any]],
    extractions: Sequence[Mapping[str, Any]],
    *,
    prescribed_warmup_min: Optional[float] = None,
    prescribed_cooldown_min: Optional[float] = None,
) -> Optional[Dict[int, str]]:
    main_indices = [
        i
        for i, part in enumerate(indexed)
        if _is_main_interval_piece(part, extractions)
    ]
    if len(main_indices) == 1:
        main_i = main_indices[0]
        others = [i for i in range(len(indexed)) if i != main_i]
        roles: Dict[int, str] = {main_i: "main"}
        if len(others) >= 2:
            a, b = others[0], others[1]
            roles.update(
                _pair_warmup_cooldown_roles(
                    indexed,
                    a,
                    b,
                    prescribed_warmup_min=prescribed_warmup_min,
                    prescribed_cooldown_min=prescribed_cooldown_min,
                )
            )
            for i in others[2:]:
                roles[i] = "other"
        elif len(others) == 1:
            roles[others[0]] = "warmup"
        return roles
    if len(indexed) == 3:
        roles = _assign_roles_by_split_three(indexed)
        if not roles:
            return None
        main_i = next(i for i, role in roles.items() if role == "main")
        others = [i for i in range(len(indexed)) if i != main_i]
        if len(others) == 2:
            a, b = others[0], others[1]
            roles.update(
                _pair_warmup_cooldown_roles(
                    indexed,
                    a,
                    b,
                    prescribed_warmup_min=prescribed_warmup_min,
                    prescribed_cooldown_min=prescribed_cooldown_min,
                )
            )
        return roles
    return None


def _apply_part_roles(
    metrics: Dict[str, Any],
    indexed: Sequence[Dict[str, Any]],
    roles: Dict[int, str],
    extractions: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    normalized: List[Dict[str, Any]] = []
    changed = False
    for i, part in enumerate(indexed):
        row = dict(part)
        new_role = roles.get(i)
        if new_role and row.get("role") != new_role:
            row["role"] = new_role
            changed = True
        normalized.append(row)
    if not changed:
        return metrics

    out = dict(metrics)
    out["session_parts"] = normalized
    out.update(_recompute_session_totals(normalized))
    if any(r.get("role") == "main" for r in normalized):
        out["workout_type"] = "intervals"
    assumptions = str(out.get("assumptions") or "").strip()
    note = "Normalized screenshot part roles (warmup/main/cooldown) from structure."
    out["assumptions"] = f"{assumptions}; {note}" if assumptions else note
    from erg_hr_enrich import enrich_erg_metrics_hr

    return enrich_erg_metrics_hr(out, extractions)


def normalize_multi_screenshot_session(
    metrics: Dict[str, Any],
    extractions: Sequence[Mapping[str, Any]],
    *,
    prescribed_warmup_min: Optional[float] = None,
    prescribed_cooldown_min: Optional[float] = None,
) -> Dict[str, Any]:
    """Re-role warmup/main/cooldown parts when synthesis mis-orders screenshots."""
    parts = metrics.get("session_parts")
    if not isinstance(parts, list) or len(parts) < 2:
        return metrics

    indexed = [p for p in parts if isinstance(p, dict)]
    if len(indexed) < 2:
        return metrics

    roles = _build_warmup_main_cooldown_roles(
        indexed,
        extractions,
        prescribed_warmup_min=prescribed_warmup_min,
        prescribed_cooldown_min=prescribed_cooldown_min,
    )
    if not roles:
        return metrics
    return _apply_part_roles(metrics, indexed, roles, extractions)
