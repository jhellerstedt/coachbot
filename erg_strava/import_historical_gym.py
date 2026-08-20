#!/usr/bin/env python3
"""Import handwritten / spreadsheet gym logs into athlete gym_logs/ cache."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from gym_tonnage_xlsx import TonnageXlsxSession, parse_tonnage_xlsx
from generate_training_plan import (
    find_gym_log_by_session_date,
    format_exercise_history_for_plan,
    record_historical_gym_session,
    record_historical_gym_tonnage_session,
    set_plan_timezone,
    zulip_gym_logs_as_metrics_records,
)

DEFAULT_ATHLETE_ID = 53603359
DEFAULT_ATHLETE_LABEL = "Jack H"


def _session_exercises(
    session: Dict[str, Any],
    columns: Dict[str, str],
) -> List[Tuple[str, Sequence[float]]]:
    exercises: List[Tuple[str, Sequence[float]]] = []
    for key, label in columns.items():
        raw = session.get(key)
        if raw is None:
            continue
        if isinstance(raw, (int, float)):
            weights = [float(raw)]
        else:
            weights = [float(w) for w in raw]
        if weights:
            exercises.append((label, weights))
    return exercises


def _import_session(
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    session_date: date,
    *,
    force: bool,
    import_fn,
    **kwargs,
) -> Tuple[Dict[str, Any], bool]:
    skipped = (
        not force
        and find_gym_log_by_session_date(
            cache_dir, athlete_id, session_date, source="historical_import"
        )
        is not None
    )
    record = import_fn(
        cache_dir,
        athlete_id,
        athlete_label,
        session_date,
        skip_existing=not force,
        **kwargs,
    )
    return record, skipped


def _print_session_line(session_date: date, record: Dict[str, Any], skipped: bool) -> None:
    gym = record.get("gym") or {}
    suffix = " [skipped — already imported]" if skipped else ""
    fmt = record.get("import_format", "?")
    print(
        f"{session_date}: {float(gym.get('total_tonnage_kg', 0)):.0f} kg "
        f"({len(gym.get('exercises') or [])} exercises, {fmt}){suffix}",
        flush=True,
    )


def import_from_json(
    cache_dir: Path,
    data_path: Path,
    *,
    force: bool = False,
) -> Tuple[int, int]:
    payload = json.loads(data_path.read_text())
    athlete_id = int(payload["athlete_id"])
    athlete_label = str(payload.get("athlete_label") or f"athlete_{athlete_id}")
    columns = dict(payload.get("exercise_columns") or {})
    sessions = payload.get("sessions") or []
    written = 0
    for session in sessions:
        session_date = date.fromisoformat(str(session["date"])[:10])
        record, skipped = _import_session(
            cache_dir,
            athlete_id,
            athlete_label,
            session_date,
            force=force,
            import_fn=record_historical_gym_session,
            exercises=_session_exercises(session, columns),
        )
        _print_session_line(session_date, record, skipped)
        if not skipped:
            written += 1
    return written, athlete_id


def import_from_xlsx(
    cache_dir: Path,
    data_path: Path,
    *,
    athlete_id: int,
    athlete_label: str,
    force: bool = False,
) -> Tuple[int, int]:
    sessions: List[TonnageXlsxSession] = parse_tonnage_xlsx(data_path)
    written = 0
    for session in sessions:
        record, skipped = _import_session(
            cache_dir,
            athlete_id,
            athlete_label,
            session.session_date,
            force=force,
            import_fn=record_historical_gym_tonnage_session,
            exercises=session.exercises_for_import(),
            body_weight_kg=session.body_weight_kg,
        )
        _print_session_line(session.session_date, record, skipped)
        if not skipped:
            written += 1
    return written, athlete_id


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import historical gym sessions into erg_strava_cache."
    )
    parser.add_argument(
        "data",
        type=Path,
        help="JSON (per-set weights) or .xlsx (tonnage) file — not in git; copy locally",
    )
    parser.add_argument(
        "--format",
        choices=("auto", "json", "xlsx"),
        default="auto",
        help="Input format (default: infer from file extension)",
    )
    parser.add_argument(
        "--athlete-id",
        type=int,
        default=DEFAULT_ATHLETE_ID,
        help=f"Strava athlete id for .xlsx imports (default: {DEFAULT_ATHLETE_ID})",
    )
    parser.add_argument(
        "--athlete-label",
        default=DEFAULT_ATHLETE_LABEL,
        help=f"Athlete label for .xlsx imports (default: {DEFAULT_ATHLETE_LABEL})",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "erg_strava_cache",
        help="erg_strava cache directory (default: ./erg_strava_cache)",
    )
    parser.add_argument(
        "--timezone",
        default="Australia/Melbourne",
        help="IANA timezone for plan week matching",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing historical_import records for the same dates",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print lift history summary after import",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    data_path = args.data.resolve()
    fmt = args.format
    if fmt == "auto":
        fmt = "xlsx" if data_path.suffix.lower() == ".xlsx" else "json"

    cache_dir = args.cache_dir.resolve()
    set_plan_timezone(str(args.timezone))

    if fmt == "xlsx":
        count, athlete_id = import_from_xlsx(
            cache_dir,
            data_path,
            athlete_id=args.athlete_id,
            athlete_label=args.athlete_label,
            force=args.force,
        )
    else:
        count, athlete_id = import_from_json(cache_dir, data_path, force=args.force)

    print(f"Imported {count} session(s) from {data_path.name} → {cache_dir}", flush=True)

    if args.summary:
        metrics = zulip_gym_logs_as_metrics_records(cache_dir, athlete_id)
        print(format_exercise_history_for_plan(metrics), flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
