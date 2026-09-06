"""Tests for the athlete-facing season-week blurb on weekly plans."""

from __future__ import annotations

from datetime import date

from generate_training_plan import week_bounds_from_monday
from season_master_plan import format_weekly_plan_week_blurb


def test_format_weekly_plan_week_blurb_deload_before_hoty():
    week = week_bounds_from_monday(date(2026, 9, 7))
    weeks = {
        "2026-08-31_2026-09-06": {
            "week_start": "2026-08-31",
            "phase": "build",
        },
        "2026-09-07_2026-09-13": {
            "week_start": "2026-09-07",
            "phase": "deload",
        },
        "2026-09-14_2026-09-20": {
            "week_start": "2026-09-14",
            "phase": "raceprep",
        },
    }
    text = format_weekly_plan_week_blurb(
        week,
        season_start=date(2026, 6, 8),
        weeks=weeks,
        races=[{"name": "Head of the Yarra", "date": "2026-11-30"}],
    )
    assert text == (
        "This is week 14. Recovery week before HOTY race prep starts 14 Sep."
    )


def test_format_weekly_plan_week_blurb_build_week():
    week = week_bounds_from_monday(date(2026, 8, 31))
    weeks = {
        "2026-08-31_2026-09-06": {
            "week_start": "2026-08-31",
            "phase": "build",
        },
        "2026-09-07_2026-09-13": {
            "week_start": "2026-09-07",
            "phase": "deload",
        },
    }
    text = format_weekly_plan_week_blurb(
        week,
        season_start=date(2026, 6, 8),
        weeks=weeks,
        races=[{"name": "Head of the Yarra", "date": "2026-11-30"}],
    )
    assert text == "This is week 13. Build week before recovery week starts 7 Sep."
