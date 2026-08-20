#!/usr/bin/env python3
"""Generate deload / base / build week plans for Jack H review (stdout + file)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
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
from season_master_plan import load_season_config, load_season_week_macro_context
from strava_erg_hr_plot import collect_all_points, load_coach_bot_dotenv, load_config
from weekly_plan_master_align import enforce_weekly_plan_alignment, load_weekly_targets
from weekly_plan_schema import (
    parse_weekly_plan,
    planned_metrics_from_plan_json,
    render_plan_text,
    weekly_plan_to_dict,
)

JACK_ID = 53603359
JACK_LABEL = "Jack H"
REVIEW_WEEK_MAP = {
    "deload": date(2026, 6, 29),
    "base": date(2026, 7, 6),
    "build": date(2026, 7, 20),
}
DEFAULT_REVIEW_ORDER = ("deload", "base", "build")
NOW = datetime(2026, 7, 21, 9, 0, tzinfo=ZoneInfo("Australia/Melbourne"))
OUT_PATH = _ERG / "phase_review_plans.md"


def _load_metrics(cache_dir: Path, athlete_id: int) -> dict:
    metrics_dir = cache_dir / f"athlete_{athlete_id}" / "metrics"
    out: dict = {}
    if metrics_dir.is_dir():
        for mpath in metrics_dir.glob("*.json"):
            try:
                aid = int(mpath.stem)
            except ValueError:
                continue
            record = load_activity_metrics(mpath)
            if record:
                out[aid] = record
    return out


def _realign_cached(
    cache_dir: Path,
    season_cfg,
    week,
    squad_json: dict,
    prev_json: dict | None,
) -> tuple[dict | None, str]:
    targets = load_weekly_targets(cache_dir, season_cfg)
    result = enforce_weekly_plan_alignment(
        week.week_start.isoformat(),
        squad_json,
        targets,
        previous_week_plan=prev_json,
    )
    if result.plan_json:
        return result.plan_json, result.plan_text
    aligned, log = apply_season_master_plan_alignment(
        cache_dir,
        season_cfg,
        week,
        squad_json,
        previous_week_plan=prev_json,
        plan_label=f"Squad ({week.week_id})",
    )
    text = render_plan_text(parse_weekly_plan(aligned)) if aligned else ""
    return aligned, log + "\n" + text


def _generate_week(
    *,
    label: str,
    week,
    prev_week,
    cache_dir: Path,
    season_cfg,
    token: str,
    training_summary: str,
    athlete_summary: str,
    gym_history: str,
    jack_profile,
    use_cached_squad: bool,
) -> str:
    phase = plan_phase_for_week(cache_dir, season_cfg, week) or label
    season_ctx = load_season_week_macro_context(cache_dir, week, season_cfg)
    prev_squad = load_weekly_plan(cache_dir, prev_week.week_id)
    prev_squad_json = prev_squad.plan_json if prev_squad else None
    prev_jack = load_athlete_weekly_plan(cache_dir, JACK_ID, prev_week.week_id)
    prev_jack_json = (
        prev_jack.get("plan_json") if isinstance(prev_jack, dict) else None
    )
    prev_gym = None
    if prev_jack:
        prev_gym = format_previous_week_gym_exercises(
            prev_jack.get("plan_text") or "",
            prev_jack_json if isinstance(prev_jack_json, dict) else None,
        )

    adherence = (
        f"Hypothetical: {JACK_LABEL} completed prior week as prescribed "
        f"({prev_week.week_id})."
    )
    goal_tracking = get_kagi_goal_tracking(
        athlete_summary,
        token,
        adherence_review=adherence,
        phase=phase,
    )

    parts = [
        f"\n\n# {label.upper()} week — {week.week_id}\n",
        f"Phase: **{phase}**\n",
    ]
    if season_ctx:
        parts.append("## Season macro context\n\n" + season_ctx.strip() + "\n")

    squad_json: dict | None = None
    squad_text = ""

    cached = load_weekly_plan(cache_dir, week.week_id)
    if use_cached_squad and cached and cached.plan_json:
        parts.append("\n## Squad plan (re-aligned from cache)\n")
        squad_json, squad_text = _realign_cached(
            cache_dir,
            season_cfg,
            week,
            cached.plan_json,
            prev_squad_json,
        )
        parts.append(finalize_plan_text_for_display(squad_text, squad_json).strip())
    else:
        parts.append("\n## Squad plan (LLM + alignment)\n")
        generated = generate_squad_weekly_plan(
            training_summary,
            token,
            plan_week=week,
            adherence_review=adherence,
            goal_tracking=goal_tracking,
            gym_exercise_history=gym_history,
            previous_week_gym_exercises=prev_gym,
            season_week_context=season_ctx,
            phase=phase,
            prev_plan_json=prev_squad_json,
        )
        squad_json, log = apply_season_master_plan_alignment(
            cache_dir,
            season_cfg,
            week,
            generated.plan_json,
            plan_text=generated.plan_text,
            previous_week_plan=prev_squad_json,
            plan_label=f"Squad ({label})",
        )
        parts.append(log.strip())
        parsed = parse_weekly_plan(squad_json) if squad_json else None
        squad_text = (
            render_plan_text(parsed) if parsed else generated.plan_text
        )
        parts.append(
            finalize_plan_text_for_display(squad_text, squad_json).strip()
        )

    if squad_json:
        parts.append(
            "\n```json\n"
            + json.dumps(planned_metrics_from_plan_json(squad_json), indent=2)
            + "\n```\n"
        )

    parts.append(f"\n## {JACK_LABEL} personalised plan\n")
    if squad_json:
        hr_ctx = jack_profile.hr_zone_context_text() if jack_profile else None
        generated_a = generate_athlete_weekly_plan(
            JACK_LABEL,
            athlete_summary,
            token,
            squad_plan_json=squad_json,
            squad_plan_text=squad_text,
            plan_week=week,
            gym_exercise_history=gym_history,
            recent_sessions_summary=adherence,
            athlete_hr_context=hr_ctx,
            season_week_context=season_ctx,
        )
        athlete_json, alog = apply_season_master_plan_alignment(
            cache_dir,
            season_cfg,
            week,
            generated_a.plan_json,
            plan_text=generated_a.plan_text,
            personalised=True,
            greeting=(
                (generated_a.plan_json or {}).get("greeting")
                if isinstance(generated_a.plan_json, dict)
                else None
            ),
            previous_week_plan=prev_jack_json
            if isinstance(prev_jack_json, dict)
            else None,
            reference_plan=squad_json,
            plan_label=f"Athlete {JACK_LABEL} ({label})",
        )
        parts.append(alog.strip())
        parsed_a = parse_weekly_plan(athlete_json) if athlete_json else None
        athlete_text = (
            render_plan_text(parsed_a) if parsed_a else generated_a.plan_text
        )
        parts.append(
            finalize_plan_text_for_display(athlete_text, athlete_json).strip()
        )
        if athlete_json:
            parts.append(
                "\n```json\n"
                + json.dumps(planned_metrics_from_plan_json(athlete_json), indent=2)
                + "\n```"
            )
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate phase review plans for Jack H")
    parser.add_argument(
        "--weeks",
        default=",".join(DEFAULT_REVIEW_ORDER),
        help="Comma-separated phases: deload,base,build (default: all)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT_PATH,
        help="Output markdown path",
    )
    args = parser.parse_args()
    selected = [w.strip().lower() for w in args.weeks.split(",") if w.strip()]
    for w in selected:
        if w not in REVIEW_WEEK_MAP:
            raise SystemExit(f"Unknown week label {w!r}; use deload, base, or build")

    load_coach_bot_dotenv()
    import os

    token = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not token:
        raise SystemExit("OPENROUTER_API_KEY not set")

    cfg_path = _ERG / "config.yaml"
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    (
        cache_dir,
        athletes,
        erg_types,
        require_trainer,
        photo_cfg,
        _gym,
        suunto_cfg,
        _strava,
    ) = load_config(cfg_path)
    season_cfg = load_season_config(raw.get("season") or {})
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
    training_summary = build_training_summary(df, now=NOW)
    athlete_summary = build_athlete_training_summary(df, JACK_LABEL, now=NOW)
    gym_history = format_exercise_history_for_plan(
        filter_metrics_for_athlete(
            cache_dir, JACK_ID, _load_metrics(cache_dir, JACK_ID)
        )
    )

    sections = [
        "# Phase review plans — Jack H\n",
        f"Generated: {datetime.now(timezone.utc).isoformat()}\n",
        f"Weeks: {', '.join(selected)}.\n",
    ]

    # Chain prev_week from the week before the first selected phase in calendar order.
    ordered = sorted(
        selected,
        key=lambda label: REVIEW_WEEK_MAP[label],
    )
    first_monday = REVIEW_WEEK_MAP[ordered[0]]
    prev = week_bounds_from_monday(first_monday - timedelta(days=7))

    for label in ordered:
        monday = REVIEW_WEEK_MAP[label]
        week = week_bounds_from_monday(monday)
        sections.append(
            _generate_week(
                label=label,
                week=week,
                prev_week=prev,
                cache_dir=cache_dir,
                season_cfg=season_cfg,
                token=token,
                training_summary=training_summary,
                athlete_summary=athlete_summary,
                gym_history=gym_history,
                jack_profile=jack_profile,
                use_cached_squad=(label == "deload"),
            )
        )
        prev = week

    body = "\n".join(sections)
    args.out.write_text(body)
    print(body)
    print(f"\n\n--- Written to {args.out} ---")


if __name__ == "__main__":
    main()
