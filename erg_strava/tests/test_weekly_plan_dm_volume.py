"""Tests for last-week volume preamble on weekly athlete plan DMs."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from erg_prescription_compare import format_last_week_volume_for_dm
from generate_training_plan import compose_weekly_athlete_plan_dm, week_bounds_from_monday
from test_erg_prescription_compare import (
    _friday_topup_erg_record,
    _jack_session_record,
    _mixed_week_personal_plan,
    _write_season_master_plan_week,
)
from generate_training_plan import save_athlete_weekly_plan


def test_format_last_week_volume_for_dm_uses_week_range_header(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 6, 22))
    athlete_id = 111
    save_athlete_weekly_plan(
        tmp_path,
        athlete_id,
        week,
        plan_text="week",
        plan_json=_mixed_week_personal_plan(),
    )
    scores_dir = tmp_path / f"athlete_{athlete_id}" / "erg_scores"
    scores_dir.mkdir(parents=True)
    for rec in (_jack_session_record(), _friday_topup_erg_record()):
        (scores_dir / f"{rec['id']}.json").write_text(
            json.dumps(rec), encoding="utf-8"
        )

    block = format_last_week_volume_for_dm(tmp_path, athlete_id, week)
    assert block.startswith("**Last week volume** (2026-06-22 – 2026-06-28)")
    assert "Week zone volume" not in block
    assert "Unprescribed endurance: 45 min" in block
    assert "Prescribed rowing logged: 50 / 92 min" in block


def test_format_last_week_volume_for_dm_includes_season_goals(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 6, 22))
    athlete_id = 118
    save_athlete_weekly_plan(
        tmp_path,
        athlete_id,
        week,
        plan_text="week",
        plan_json=_mixed_week_personal_plan(),
    )
    scores_dir = tmp_path / f"athlete_{athlete_id}" / "erg_scores"
    scores_dir.mkdir(parents=True)
    (scores_dir / "test-session.json").write_text(
        json.dumps(_jack_session_record()), encoding="utf-8"
    )
    _write_season_master_plan_week(tmp_path, week.week_start, z2=78, z5=10)

    block = format_last_week_volume_for_dm(tmp_path, athlete_id, week)
    assert "season week goal: Z2 ~78% of erg/row time" in block


def test_compose_weekly_athlete_plan_dm_prepends_volume():
    volume = (
        "**Last week volume** (2026-06-22 – 2026-06-28)\n"
        "- Prescribed rowing logged: 50 / 92 min\n"
        "- Unprescribed endurance: 45 min"
    )
    target = week_bounds_from_monday(date(2026, 6, 29))
    msg = compose_weekly_athlete_plan_dm(volume, target, "Monday: rest\n")
    assert msg.startswith("**Last week volume**")
    assert "**Your weekly plan** (2026-06-29 – 2026-07-05)" in msg
    assert msg.index("**Last week volume**") < msg.index("**Your weekly plan**")
    assert "Monday: rest" in msg
