#!/usr/bin/env python3
"""One-off: generate next-week plan assuming prior week completed as prescribed."""

from __future__ import annotations

import json
import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

_ERG = Path(__file__).resolve().parent
if str(_ERG) not in sys.path:
    sys.path.insert(0, str(_ERG))

from athlete_profile import athlete_profiles_by_id, load_athlete_profiles
from generate_training_plan import (
    apply_season_master_plan_alignment,
    build_athlete_training_summary,
    build_training_summary,
    filter_metrics_for_athlete,
    finalize_plan_text_for_display,
    format_exercise_history_for_plan,
    format_previous_week_gym_exercises,
    generate_athlete_weekly_plan,
    generate_squad_weekly_plan,
    get_kagi_goal_tracking,
    load_activity_metrics,
    load_athlete_weekly_plan,
    load_weekly_plan,
    plan_phase_for_week,
    week_bounds_from_monday,
)
from season_master_plan import (
    load_season_config,
    load_season_plan_merged,
    load_season_week_macro_context,
    save_season_master_plan,
    write_season_master_plan_md,
)
from strava_erg_hr_plot import collect_all_points, load_coach_bot_dotenv, load_config
from weekly_plan_schema import (
    parse_weekly_plan,
    planned_metrics_from_plan_json,
    render_plan_text,
)

JACK_ID = 53603359
JACK_LABEL = "Jack H"
PREV_WEEK_MONDAY = date(2026, 6, 29)
TARGET_WEEK_MONDAY = date(2026, 7, 6)
HYPOTHETICAL_NOW = datetime(2026, 7, 7, 9, 0, tzinfo=ZoneInfo("Australia/Melbourne"))


def main() -> None:
    load_coach_bot_dotenv()
    import os

    token = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not token:
        raise SystemExit("OPENROUTER_API_KEY not set (coach_bot/.env)")

    cfg_path = _ERG / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    (
        cache_dir,
        athletes,
        erg_types,
        require_trainer,
        photo_cfg,
        _gym_cfg,
        suunto_cfg,
        _strava_cfg,
    ) = load_config(cfg_path)
    season_cfg = load_season_config(raw.get("season") or {})

    prev_week = week_bounds_from_monday(PREV_WEEK_MONDAY)
    target_week = week_bounds_from_monday(TARGET_WEEK_MONDAY)

    jack_prev = load_athlete_weekly_plan(cache_dir, JACK_ID, prev_week.week_id)
    if not jack_prev or not isinstance(jack_prev.get("plan_json"), dict):
        raise SystemExit(f"Missing Jack H deload plan at {prev_week.week_id}")
    jack_prev_json = jack_prev["plan_json"]
    jack_prev_text = jack_prev.get("plan_text") or render_plan_text(
        parse_weekly_plan(jack_prev_json)
    )

    squad_prev = load_weekly_plan(cache_dir, prev_week.week_id)
    squad_prev_json = squad_prev.plan_json if squad_prev else None

    jack_actual = planned_metrics_from_plan_json(jack_prev_json)
    squad_actual = (
        planned_metrics_from_plan_json(squad_prev_json)
        if squad_prev_json
        else jack_actual
    )

    season_json_path = cache_dir / "season_master_plan.json"
    backup_path = cache_dir / "season_master_plan.json.hypothetical_backup"
    if season_json_path.is_file():
        shutil.copy2(season_json_path, backup_path)

    try:
        data = load_season_plan_merged(cache_dir, season_cfg)
        weeks = data.setdefault("weeks", {})
        for week_id, actual in (
            (prev_week.week_id, jack_actual),
        ):
            row = weeks.setdefault(week_id, {})
            row["actual"] = actual
            row["actual_updated_at"] = datetime.now(timezone.utc).isoformat()
            if squad_prev_json and week_id == prev_week.week_id:
                row["planned"] = squad_actual
        save_season_master_plan(cache_dir, data)
        write_season_master_plan_md(cache_dir, data)

        profiles = athlete_profiles_by_id(load_athlete_profiles(raw))
        jack_profile = profiles.get(JACK_ID)

        df = collect_all_points(
            athletes,
            cache_dir,
            erg_types,
            require_trainer,
            photo_cfg,
            suunto_cfg=suunto_cfg,
        )
        training_summary = build_training_summary(df, now=HYPOTHETICAL_NOW)
        athlete_summary = build_athlete_training_summary(
            df, JACK_LABEL, now=HYPOTHETICAL_NOW
        )

        metrics_dir = cache_dir / f"athlete_{JACK_ID}" / "metrics"
        activity_metrics: dict = {}
        if metrics_dir.is_dir():
            for mpath in metrics_dir.glob("*.json"):
                try:
                    aid = int(mpath.stem)
                except ValueError:
                    continue
                record = load_activity_metrics(mpath)
                if record:
                    activity_metrics[aid] = record

        jack_metrics = filter_metrics_for_athlete(cache_dir, JACK_ID, activity_metrics)
        gym_history = format_exercise_history_for_plan(jack_metrics)
        from gym_program import lift_logs_from_metrics, median_latest_peak_kg

        jack_peaks = median_latest_peak_kg(jack_metrics)
        jack_logs = lift_logs_from_metrics(jack_metrics)
        prev_gym = format_previous_week_gym_exercises(jack_prev_text, jack_prev_json)

        adherence = (
            f"{JACK_LABEL}: hypothetical full adherence to deload week "
            f"({prev_week.week_start}–{prev_week.week_end}). "
            f"Completed Monday/Wednesday gym (2 working sets per exercise at prescribed "
            f"loads), Tuesday erg Z2 steady-state, Thursday on-water Z2 steady-state. "
            f"Logged volume matches prescription: "
            f"{jack_actual.get('rowing_minutes')} min rowing, "
            f"Z2 share {jack_actual.get('z2_percent')}%, "
            f"Z5 {jack_actual.get('z5_percent')}%, "
            f"gym tonnage {jack_actual.get('gym_tonnage_kg')} kg."
        )

        phase = plan_phase_for_week(cache_dir, season_cfg, target_week)
        season_ctx = load_season_week_macro_context(cache_dir, target_week, season_cfg)

        print(f"=== Hypothetical base week {target_week.week_id} for {JACK_LABEL} ===")
        print(f"Assumes deload week completed as prescribed ({prev_week.week_id}).")
        print(f"Season phase: {phase}")
        if season_ctx:
            print("\n--- Season macro context ---\n")
            print(season_ctx.strip())

        goal_tracking = get_kagi_goal_tracking(
            athlete_summary,
            token,
            adherence_review=adherence,
            phase=phase,
        )
        print("\n--- Goal tracking ---\n")
        print(goal_tracking.strip())

        generated_squad = generate_squad_weekly_plan(
            training_summary,
            token,
            plan_week=target_week,
            adherence_review=adherence,
            goal_tracking=goal_tracking,
            gym_exercise_history=gym_history,
            previous_week_gym_exercises=prev_gym,
            season_week_context=season_ctx,
            phase=phase,
            prev_plan_json=squad_prev_json,
            peak_kg_by_exercise=jack_peaks,
        )
        squad_json, squad_log = apply_season_master_plan_alignment(
            cache_dir,
            season_cfg,
            target_week,
            generated_squad.plan_json,
            plan_text=generated_squad.plan_text,
            previous_week_plan=squad_prev_json,
            plan_label="Squad weekly plan (hypothetical)",
        )
        print(f"\n{squad_log}")
        if squad_json:
            parsed = parse_weekly_plan(squad_json)
            squad_text = (
                render_plan_text(parsed) if parsed else generated_squad.plan_text
            )
        else:
            squad_text = generated_squad.plan_text
            squad_json = None

        print("\n--- Squad plan (hypothetical) ---\n")
        print(finalize_plan_text_for_display(squad_text, squad_json).strip())

        hr_ctx = jack_profile.hr_zone_context_text() if jack_profile else None
        generated_athlete = generate_athlete_weekly_plan(
            JACK_LABEL,
            athlete_summary,
            token,
            squad_plan_json=squad_json,
            squad_plan_text=squad_text,
            plan_week=target_week,
            gym_exercise_history=gym_history,
            recent_sessions_summary=adherence,
            athlete_hr_context=hr_ctx,
            season_week_context=season_ctx,
            lift_logs_by_exercise=jack_logs,
        )
        athlete_json, athlete_log = apply_season_master_plan_alignment(
            cache_dir,
            season_cfg,
            target_week,
            generated_athlete.plan_json,
            plan_text=generated_athlete.plan_text,
            personalised=True,
            greeting=(
                (generated_athlete.plan_json or {}).get("greeting")
                if isinstance(generated_athlete.plan_json, dict)
                else None
            ),
            previous_week_plan=jack_prev_json,
            reference_plan=squad_json,
            plan_label=f"Athlete plan ({JACK_LABEL}, hypothetical)",
        )
        print(f"\n{athlete_log}")
        if athlete_json:
            parsed_a = parse_weekly_plan(athlete_json)
            athlete_text = (
                render_plan_text(parsed_a)
                if parsed_a
                else generated_athlete.plan_text
            )
        else:
            athlete_text = generated_athlete.plan_text

        print(f"\n=== {JACK_LABEL} personalised plan ({target_week.week_id}) ===\n")
        print(finalize_plan_text_for_display(athlete_text, athlete_json).strip())

        if athlete_json:
            m = planned_metrics_from_plan_json(athlete_json)
            print("\n--- Prescribed metrics ---")
            print(json.dumps(m, indent=2))

    finally:
        if backup_path.is_file():
            shutil.copy2(backup_path, season_json_path)
            backup_path.unlink()
            if season_json_path.is_file():
                data = json.loads(season_json_path.read_text())
                from season_master_plan import render_season_master_plan_md

                (cache_dir / "season_master_plan.md").write_text(
                    render_season_master_plan_md(data)
                )
            print("\n(Restored season_master_plan.json from backup — hypothetical actuals not persisted.)")


if __name__ == "__main__":
    main()
