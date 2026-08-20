#!/usr/bin/env python3
"""Batch-align cached squad and athlete weekly plans with season_master_plan.md."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from generate_training_plan import (
    apply_season_master_plan_alignment,
    finalize_plan_text_for_display,
    load_athlete_weekly_plan,
    load_weekly_plan,
    previous_week_bounds,
    week_bounds_from_monday,
)
from plan_text_import import import_weekly_plan_json_from_text
from season_master_plan import load_season_config, load_season_master_plan
from weekly_plan_schema import parse_weekly_plan, render_plan_text


def _resolve_plan_json(
    record: Dict[str, Any],
    *,
    personalised: bool,
) -> Tuple[Optional[Dict[str, Any]], str]:
    existing = record.get("plan_json")
    if isinstance(existing, dict) and parse_weekly_plan(existing) is not None:
        return existing, "existing plan_json"

    week_start = str(record.get("week_start") or record.get("week_id", "")[:10])
    greeting = record.get("greeting")
    if greeting is None and personalised:
        text = str(record.get("plan_text") or "")
        first = text.splitlines()[0].strip() if text else ""
        if first and not first.startswith(("Monday", "**", "Tuesday")):
            greeting = first

    imported = import_weekly_plan_json_from_text(
        str(record.get("plan_text") or ""),
        week_start=week_start,
        personalised=personalised,
        greeting=greeting if personalised else None,
    )
    if imported is not None:
        return imported, "imported from plan_text"
    return None, "could not import plan_text"


def _previous_week_plan_json(
    cache_dir: Path,
    week: Any,
    *,
    athlete_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    prev = previous_week_bounds(week)
    if athlete_id is not None:
        rec = load_athlete_weekly_plan(cache_dir, athlete_id, prev.week_id)
        if isinstance(rec, dict):
            pj = rec.get("plan_json")
            return pj if isinstance(pj, dict) else None
        return None
    rec_obj = load_weekly_plan(cache_dir, prev.week_id)
    if rec_obj and isinstance(rec_obj.plan_json, dict):
        return rec_obj.plan_json
    return None


def _squad_plan_json_for_week(
    cache_dir: Path,
    week_id: str,
    *,
    aligned_by_week: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    if aligned_by_week and week_id in aligned_by_week:
        pj = aligned_by_week[week_id].get("plan_json")
        return pj if isinstance(pj, dict) else None
    rec = load_weekly_plan(cache_dir, week_id)
    if rec and isinstance(rec.plan_json, dict):
        return rec.plan_json
    return None


def _align_record(
    record: Dict[str, Any],
    cache_dir: Path,
    season_cfg: Any,
    *,
    athlete_id: Optional[int] = None,
    label: str,
    reference_plan: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], str]:
    week_start = str(record.get("week_start") or record.get("week_id", "")[:10])
    week = week_bounds_from_monday(__import__("datetime").date.fromisoformat(week_start[:10]))
    personalised = athlete_id is not None or bool(
        (record.get("plan_json") or {}).get("personalised")
    )

    plan_json, source = _resolve_plan_json(record, personalised=personalised)
    if plan_json is None:
        return record, f"{label}: skipped (no structured JSON; {source})"

    prev_json = _previous_week_plan_json(cache_dir, week, athlete_id=athlete_id)
    aligned_json, log = apply_season_master_plan_alignment(
        cache_dir,
        season_cfg,
        week,
        plan_json,
        previous_week_plan=prev_json,
        reference_plan=reference_plan,
        personalised=personalised,
        greeting=(
            (record.get("greeting") or (plan_json or {}).get("greeting"))
            if personalised
            else None
        ),
        plan_label=label,
    )
    if aligned_json is None:
        return record, f"{label}: skipped ({log})"

    parsed = parse_weekly_plan(aligned_json)
    if parsed is None:
        return record, f"{label}: skipped (aligned JSON invalid)"

    record = dict(record)
    record["plan_json"] = aligned_json
    record["plan_text"] = finalize_plan_text_for_display(
        render_plan_text(parsed),
        aligned_json,
    )
    record["aligned_at"] = datetime.now(timezone.utc).isoformat()
    return record, f"{label}: aligned ({source})\n{log}"


def align_cache(
    cache_dir: Path,
    *,
    dry_run: bool = False,
) -> List[str]:
    logs: List[str] = []
    season_cfg = load_season_config({})
    if not season_master_plan_md_path(cache_dir).is_file() and not load_season_master_plan(cache_dir):
        logs.append("WARNING: no season master plan found in cache")

    squad_dir = cache_dir / "weekly_plans"
    squad_files = sorted(squad_dir.glob("*.json")) if squad_dir.is_dir() else []
    aligned_squads: Dict[str, Dict[str, Any]] = {}
    for path in squad_files:
        record = json.loads(path.read_text(encoding="utf-8"))
        week_id = record.get("week_id", path.stem)
        updated, msg = _align_record(
            record,
            cache_dir,
            season_cfg,
            label=f"Squad {week_id}",
        )
        logs.append(msg)
        if not dry_run and ": aligned" in msg:
            path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
            aligned_squads[week_id] = updated
        elif ": aligned" in msg:
            aligned_squads[week_id] = updated

    for athlete_dir in sorted(cache_dir.glob("athlete_*/weekly_plans")):
        athlete_id = int(athlete_dir.parent.name.split("_", 1)[1])
        for path in sorted(athlete_dir.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            week_id = record.get("week_id", path.stem)
            squad_ref = _squad_plan_json_for_week(
                cache_dir, week_id, aligned_by_week=aligned_squads
            )
            updated, msg = _align_record(
                record,
                cache_dir,
                season_cfg,
                athlete_id=athlete_id,
                label=f"Athlete {athlete_id} {week_id}",
                reference_plan=squad_ref,
            )
            logs.append(msg)
            if not dry_run and ": aligned" in msg:
                path.write_text(json.dumps(updated, indent=2), encoding="utf-8")

    return logs


def season_master_plan_md_path(cache_dir: Path) -> Path:
    return cache_dir / "season_master_plan.md"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "erg_strava_cache",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logs = align_cache(args.cache_dir, dry_run=args.dry_run)
    for line in logs:
        print(line)
    aligned = sum(1 for line in logs if ": aligned" in line)
    skipped = sum(1 for line in logs if ": skipped" in line)
    print(f"\nDone: {aligned} aligned, {skipped} skipped, dry_run={args.dry_run}")
    return 0 if aligned > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
