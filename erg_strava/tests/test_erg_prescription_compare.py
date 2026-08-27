"""Tests for deterministic erg prescription comparison."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from erg_prescription_compare import (
    compare_erg_session_to_prescription,
    format_erg_session_comparison,
    format_week_zone_volume_progress,
    parse_split_seconds,
    prescribed_rowing_minutes_by_zone,
)
from generate_training_plan import (
    infer_makeup_prescribed_date,
    save_athlete_weekly_plan,
    week_bounds_from_monday,
)
from strava_erg_hr_plot import AthleteCfg, athlete_paths, collect_activities_in_weeks, save_index
from weekly_plan_schema import weekly_plan_to_dict

def _steady_segment(phase: str, label: str, duration: str, split_min: str, split_max: str):
    return {
        "phase": phase,
        "label": label,
        "duration": duration,
        "split_min": split_min,
        "split_max": split_max,
        "zone_z": "Z2",
        "zone_t": "T3",
        "hr_bpm_min": 126,
        "hr_bpm_max": 136,
        "priority": "hr",
        "notes": None,
    }


def _rest_day(weekday: str, day_date: str) -> dict:
    return {
        "weekday": weekday,
        "date": day_date,
        "session_type": "rest",
        "session_subtype": None,
        "gym": None,
        "rowing": None,
        "notes": None,
    }


def _tuesday_personal_plan() -> dict:
    return {
        "version": 1,
        "personalised": True,
        "greeting": "Hi Jack,",
        "days": [
            _rest_day("Monday", "2026-06-22"),
            {
                "weekday": "Tuesday",
                "date": "2026-06-23",
                "session_type": "erg",
                "session_subtype": "steady-state",
                "gym": None,
                "rowing": {
                    "segments": [
                        _steady_segment("warm_up", "Warm-up", "10 min", "2:12", "2:17"),
                        _steady_segment(
                            "main_set", "Steady-state", "40 min", "2:10", "2:20"
                        ),
                        _steady_segment("cool_down", "Cool-down", "10 min", "2:10", "2:15"),
                    ],
                    "erg_alternative": None,
                },
                "notes": None,
            },
            _rest_day("Wednesday", "2026-06-24"),
            _rest_day("Thursday", "2026-06-25"),
            _rest_day("Friday", "2026-06-26"),
            _rest_day("Saturday", "2026-06-27"),
            _rest_day("Sunday", "2026-06-28"),
        ],
    }


def _jack_session_record() -> dict:
    return {
        "id": "test-session",
        "session_date": "2026-06-23",
        "metrics": {
            "distance_m": 10914,
            "duration_sec": 3000,
            "avg_split_500_sec": 137.0,
            "avg_split_500_fmt": "2:17.0",
            "avg_hr": 135,
            "workout_type": "steady",
            "session_parts": [
                {
                    "role": "warmup",
                    "distance_m": 2210,
                    "duration_sec": 600,
                    "avg_split_500_sec": 135.7,
                    "avg_split_500_fmt": "2:15.7",
                    "avg_hr": 136,
                },
                {
                    "role": "main",
                    "distance_m": 8704,
                    "duration_sec": 2400,
                    "avg_split_500_sec": 137.8,
                    "avg_split_500_fmt": "2:17.8",
                    "avg_hr": 134,
                },
            ],
        },
    }


def test_parse_split_seconds():
    assert parse_split_seconds("2:17.0") == pytest.approx(137.0)
    assert parse_split_seconds("2:10") == pytest.approx(130.0)
    assert parse_split_seconds("2:20") == pytest.approx(140.0)


def test_compare_jack_tuesday_session_on_plan():
    plan = _tuesday_personal_plan()
    lines = compare_erg_session_to_prescription(
        _jack_session_record(), plan, date(2026, 6, 23)
    )
    text = "\n".join(lines)
    assert "Warm-up" in text
    assert "Main set" in text
    assert "Cool-down: not logged" in text
    assert "**on plan**" in text
    assert "slower than max" not in text


def test_main_set_split_within_range():
    plan = _tuesday_personal_plan()
    lines = compare_erg_session_to_prescription(
        _jack_session_record(), plan, date(2026, 6, 23)
    )
    main_line = next(line for line in lines if "Main set" in line)
    assert "2:17.8" in main_line
    assert "2:10–2:20" in main_line
    assert "**on plan**" in main_line


def test_prescribed_zone_minutes_tuesday_only():
    metrics = prescribed_rowing_minutes_by_zone(_tuesday_personal_plan())
    assert metrics["total"] == 60
    assert metrics["z2"] == 60
    assert metrics["z5"] == 0


def test_week_zone_progress_with_cached_plan(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 6, 22))
    athlete_id = 42
    save_athlete_weekly_plan(
        tmp_path,
        athlete_id,
        week,
        plan_text="Tuesday erg",
        plan_json=_tuesday_personal_plan(),
    )
    scores_dir = tmp_path / f"athlete_{athlete_id}" / "erg_scores"
    scores_dir.mkdir(parents=True)
    (scores_dir / "test-session.json").write_text(
        json.dumps(_jack_session_record()), encoding="utf-8"
    )

    progress = format_week_zone_volume_progress(
        tmp_path, athlete_id, date(2026, 6, 23)
    )
    assert "personalised plan" in progress
    assert "50 / 60 min" in progress
    assert "83%" in progress
    assert "Z2/T2: 50 / 60 min" in progress


def test_format_erg_session_comparison_uses_personal_plan(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 6, 22))
    athlete_id = 7
    save_athlete_weekly_plan(
        tmp_path,
        athlete_id,
        week,
        plan_text="Tuesday erg",
        plan_json=_tuesday_personal_plan(),
    )
    text = format_erg_session_comparison(
        tmp_path,
        athlete_id,
        _jack_session_record(),
        date(2026, 6, 23),
    )
    assert "personalised plan" in text
    assert "2:12–2:17" in text


def test_infer_makeup_prescribed_date():
    logged = date(2026, 8, 27)  # Thursday
    assert infer_makeup_prescribed_date("made up Tuesday erg this morning", logged) == date(
        2026, 8, 25
    )
    assert infer_makeup_prescribed_date("makeup Tuesday erg", logged) == date(2026, 8, 25)
    assert infer_makeup_prescribed_date("Tuesday erg this morning", logged) is None
    assert infer_makeup_prescribed_date("made up erg this morning", logged) is None


def test_format_erg_session_comparison_makeup_uses_other_day_plan(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 8, 24))
    athlete_id = 7
    plan = _tuesday_personal_plan()
    # Align dates to Aug 24 week and add Thursday on-water day.
    for day in plan["days"]:
        if day["weekday"] == "Monday":
            day["date"] = "2026-08-24"
        elif day["weekday"] == "Tuesday":
            day["date"] = "2026-08-25"
        elif day["weekday"] == "Wednesday":
            day["date"] = "2026-08-26"
        elif day["weekday"] == "Thursday":
            day.update(_thursday_on_water_plan()["days"][3])
            day["date"] = "2026-08-27"
    save_athlete_weekly_plan(
        tmp_path,
        athlete_id,
        week,
        plan_text="Week plan",
        plan_json=plan,
    )
    record = _jack_session_record()
    record["session_date"] = "2026-08-27"
    text = format_erg_session_comparison(
        tmp_path,
        athlete_id,
        record,
        date(2026, 8, 27),
        prescribed_session_date=date(2026, 8, 25),
    )
    assert "Tuesday prescription, makeup on Thursday" in text
    assert "2:12–2:17" in text


def _thursday_on_water_plan() -> dict:
    def seg(phase, label, duration, split_min, split_max):
        return _steady_segment(phase, label, duration, split_min, split_max)

    return {
        "version": 1,
        "personalised": True,
        "greeting": None,
        "days": [
            _rest_day("Monday", "2026-06-22"),
            _rest_day("Tuesday", "2026-06-23"),
            _rest_day("Wednesday", "2026-06-24"),
            {
                "weekday": "Thursday",
                "date": "2026-06-25",
                "session_type": "on_water",
                "session_subtype": "intervals",
                "gym": None,
                "rowing": {
                    "segments": [
                        seg("warm_up", "Warm-up", "10m", "2:12", "2:17"),
                        {
                            **_steady_segment(
                                "main_set", "On-water intervals", "3x1000m", "2:10", "2:15"
                            ),
                            "zone_z": "Z3",
                            "zone_t": "T4",
                            "hr_bpm_min": 136,
                            "hr_bpm_max": 146,
                            "priority": "split",
                        },
                        seg("cool_down", "Cool-down", "10m", "2:10", "2:15"),
                    ],
                    "erg_alternative": {
                        "description": "If on ergs",
                        "segments": [
                            seg("warm_up", "Warm-up", "10m", "2:12", "2:17"),
                            {
                                **_steady_segment(
                                    "main_set",
                                    "Intervals",
                                    "3x4 min / 2 min rest",
                                    "2:10",
                                    "2:15",
                                ),
                                "zone_z": "Z3",
                                "zone_t": "T4",
                                "hr_bpm_min": 136,
                                "hr_bpm_max": 146,
                                "priority": "split",
                            },
                            seg("cool_down", "Cool-down", "10m", "2:10", "2:15"),
                        ],
                    },
                },
                "notes": None,
            },
            _rest_day("Friday", "2026-06-26"),
            _rest_day("Saturday", "2026-06-27"),
            _rest_day("Sunday", "2026-06-28"),
        ],
    }


def _jack_thursday_erg_alt_record() -> dict:
    return {
        "id": "thursday-erg",
        "session_date": "2026-06-25",
        "metrics": {
            "distance_m": 7089,
            "duration_sec": 1920,
            "avg_split_500_sec": 135.5,
            "avg_split_500_fmt": "2:15.5",
            "workout_type": "intervals",
            "session_parts": [
                {
                    "role": "warmup",
                    "distance_m": 2118,
                    "duration_sec": 600,
                    "avg_split_500_fmt": "2:21.6",
                    "avg_hr": 132,
                },
                {
                    "role": "interval_block",
                    "distance_m": 2779,
                    "duration_sec": 720,
                    "avg_split_500_fmt": "2:09.5",
                    "avg_hr": 147,
                },
                {
                    "role": "cooldown",
                    "distance_m": 2194,
                    "duration_sec": 600,
                    "avg_split_500_fmt": "2:16.7",
                    "avg_hr": 136,
                },
            ],
        },
    }


def _mixed_week_personal_plan() -> dict:
    plan = _tuesday_personal_plan()
    plan["days"][3] = _thursday_on_water_plan()["days"][3]
    return plan


def _friday_topup_erg_record() -> dict:
    return {
        "id": "friday-topup",
        "session_date": "2026-06-26",
        "recorded_at": "2026-06-26T08:00:00+00:00",
        "metrics": {
            "distance_m": 10394,
            "duration_sec": 2700,
            "avg_split_500_sec": 129.8,
            "avg_split_500_fmt": "2:09.8",
            "avg_hr": 138,
            "workout_type": "steady",
            "session_parts": [
                {
                    "role": "main",
                    "distance_m": 10394,
                    "duration_sec": 2700,
                    "avg_split_500_sec": 129.8,
                    "avg_split_500_fmt": "2:09.8",
                    "avg_hr": 138,
                }
            ],
        },
    }


def _same_day_extra_erg_record() -> dict:
    return {
        "id": "same-day-extra",
        "session_date": "2026-06-23",
        "recorded_at": "2026-06-23T18:00:00+00:00",
        "metrics": {
            "distance_m": 10125,
            "duration_sec": 2700,
            "avg_split_500_sec": 133.3,
            "avg_split_500_fmt": "2:13.3",
            "avg_hr": 136,
            "workout_type": "steady",
            "session_parts": [
                {
                    "role": "main",
                    "distance_m": 10125,
                    "duration_sec": 2700,
                    "avg_split_500_sec": 133.3,
                    "avg_split_500_fmt": "2:13.3",
                    "avg_hr": 136,
                }
            ],
        },
    }


def _ride_activity(day_date: str, minutes: int = 45) -> dict:
    return {
        "id": 9001 + minutes,
        "name": "Bonus Ride",
        "sport_type": "Ride",
        "type": "Ride",
        "start_date": f"{day_date}T07:00:00Z",
        "distance": minutes * 500,
        "moving_time": minutes * 60,
    }


def _run_activity(day_date: str, minutes: int = 30) -> dict:
    return {
        "id": 9501 + minutes,
        "name": "Bonus Run",
        "sport_type": "Run",
        "type": "Run",
        "start_date": f"{day_date}T07:00:00Z",
        "distance": minutes * 200,
        "moving_time": minutes * 60,
    }


def _write_index_activities(cache_dir: Path, athlete_id: int, activities: list[dict]) -> None:
    paths = athlete_paths(cache_dir, athlete_id)
    save_index(paths["index"], {"activities": activities})


def _write_suunto_workouts(cache_dir: Path, athlete_id: int, workouts: dict[str, dict]) -> None:
    suunto_dir = cache_dir / f"athlete_{athlete_id}" / "suunto"
    suunto_dir.mkdir(parents=True, exist_ok=True)
    (suunto_dir / "index.json").write_text(
        json.dumps({"workouts": workouts, "by_strava_id": {}, "updated_at": "2026-06-27T00:00:00Z"}),
        encoding="utf-8",
    )


def test_thursday_erg_alternative_prescription_check():
    plan = _thursday_on_water_plan()
    lines = compare_erg_session_to_prescription(
        _jack_thursday_erg_alt_record(), plan, date(2026, 6, 25)
    )
    text = "\n".join(lines)
    assert "interval_block" not in text
    assert "Main set" in text
    assert "2:09.5" in text
    assert "not logged" not in text


def test_prescribed_minutes_uses_erg_alternative_on_on_water_days():
    metrics = prescribed_rowing_minutes_by_zone(_thursday_on_water_plan())
    assert metrics["total"] == 32
    assert metrics["total"] < 100


def test_week_zone_volume_realistic_for_mixed_week(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 6, 22))
    athlete_id = 99
    plan = _tuesday_personal_plan()
    thursday = _thursday_on_water_plan()
    plan["days"][3] = thursday["days"][3]
    save_athlete_weekly_plan(
        tmp_path,
        athlete_id,
        week,
        plan_text="week",
        plan_json=plan,
    )
    scores_dir = tmp_path / f"athlete_{athlete_id}" / "erg_scores"
    scores_dir.mkdir(parents=True)
    for rec in (_jack_session_record(), _jack_thursday_erg_alt_record()):
        (scores_dir / f"{rec['id']}.json").write_text(json.dumps(rec))

    progress = format_week_zone_volume_progress(
        tmp_path, athlete_id, date(2026, 6, 25)
    )
    assert "3080" not in progress
    assert "92 min" in progress


def test_week_zone_volume_dedupes_same_day_relogs(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 6, 22))
    athlete_id = 99
    plan = _tuesday_personal_plan()
    thursday = _thursday_on_water_plan()
    plan["days"][3] = thursday["days"][3]
    save_athlete_weekly_plan(
        tmp_path,
        athlete_id,
        week,
        plan_text="week",
        plan_json=plan,
    )
    scores_dir = tmp_path / f"athlete_{athlete_id}" / "erg_scores"
    scores_dir.mkdir(parents=True)
    tuesday = _jack_session_record()
    thursday_old = _jack_thursday_erg_alt_record()
    thursday_old["id"] = "thursday-v1"
    thursday_new = _jack_thursday_erg_alt_record()
    thursday_new["id"] = "thursday-v2"
    thursday_new["recorded_at"] = "2026-06-25T20:00:00"
    for rec in (tuesday, thursday_old, thursday_new):
        (scores_dir / f"{rec['id']}.json").write_text(json.dumps(rec))

    progress = format_week_zone_volume_progress(
        tmp_path, athlete_id, date(2026, 6, 25)
    )
    assert "136 / 92" not in progress
    assert "82 / 92" in progress


def test_week_zone_progress_counts_rest_day_topup_erg_as_unprescribed(tmp_path: Path):
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
        (scores_dir / f"{rec['id']}.json").write_text(json.dumps(rec), encoding="utf-8")

    progress = format_week_zone_volume_progress(
        tmp_path, athlete_id, date(2026, 6, 26), include_unprescribed=True
    )

    assert "Prescribed rowing logged: 50 / 92 min" in progress
    assert "Unprescribed endurance: 45 min" in progress
    assert "Total endurance logged incl. unprescribed: 95 / 92 min" in progress
    assert "Z2/T2: 50 / 92 min" in progress


def test_week_zone_progress_omits_unprescribed_by_default(tmp_path: Path):
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
        (scores_dir / f"{rec['id']}.json").write_text(json.dumps(rec), encoding="utf-8")

    progress = format_week_zone_volume_progress(
        tmp_path, athlete_id, date(2026, 6, 26)
    )

    assert "Prescribed rowing logged: 50 / 92 min" in progress
    assert "Unprescribed endurance" not in progress
    assert "Total endurance logged incl. unprescribed" not in progress
    assert "Z2/T2: 50 / 92 min" in progress


def test_week_zone_progress_counts_ride_and_run_as_unprescribed(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 6, 22))
    athlete_id = 112
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
    _write_index_activities(
        tmp_path,
        athlete_id,
        [_ride_activity("2026-06-27", 45), _run_activity("2026-06-28", 30)],
    )

    progress = format_week_zone_volume_progress(
        tmp_path, athlete_id, date(2026, 6, 28), include_unprescribed=True
    )

    assert "Prescribed rowing logged: 50 / 92 min" in progress
    assert "Unprescribed endurance: 75 min" in progress
    assert "Total endurance logged incl. unprescribed: 125 / 92 min" in progress
    assert "Z2/T2: 50 / 92 min" in progress


def test_week_zone_progress_counts_unmatched_rowing_as_unprescribed(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 6, 22))
    athlete_id = 119
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
    _write_index_activities(
        tmp_path,
        athlete_id,
        [
            {
                "id": 9991,
                "name": "Bonus Row",
                "sport_type": "Rowing",
                "type": "Rowing",
                "start_date": "2026-06-27T07:00:00Z",
                "distance": 9000,
                "moving_time": 2700,
                "trainer": False,
            }
        ],
    )

    progress = format_week_zone_volume_progress(
        tmp_path, athlete_id, date(2026, 6, 28), include_unprescribed=True
    )

    assert "Prescribed rowing logged: 50 / 92 min" in progress
    assert "Unprescribed endurance: 45 min" in progress
    assert "Total endurance logged incl. unprescribed: 95 / 92 min" in progress


def test_week_zone_progress_skips_trainer_rowing_as_unprescribed(tmp_path: Path):
    """Indoor erg Strava rows are prescribed via erg scores; do not double-count."""
    week = week_bounds_from_monday(date(2026, 6, 22))
    athlete_id = 120
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
    _write_index_activities(
        tmp_path,
        athlete_id,
        [
            {
                "id": 9992,
                "name": "Morning Row",
                "sport_type": "Rowing",
                "type": "Rowing",
                "start_date": "2026-06-27T07:00:00Z",
                "distance": 9000,
                "moving_time": 2700,
                "trainer": True,
            }
        ],
    )

    progress = format_week_zone_volume_progress(
        tmp_path, athlete_id, date(2026, 6, 28), include_unprescribed=True
    )

    assert "Prescribed rowing logged: 50 / 92 min" in progress
    assert "Unprescribed endurance: 0 min" in progress


def test_week_zone_progress_counts_suunto_cycling_as_unprescribed(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 6, 22))
    athlete_id = 121
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
    # Mirrors Jack H cache: Suunto list rows often lack a real name and were
    # historically defaulted to INDOOR_ROWING / Workout while activityId=2 is cycling.
    _write_suunto_workouts(
        tmp_path,
        athlete_id,
        {
            "bike-1": {
                "key": "bike-1",
                "activityId": 2,
                "activityName": "INDOOR_ROWING",
                "sport_type": "Workout",
                "startTime": 1782514800000,
                "totalTime": 2700,
                "totalDistance": 12000,
                "strava_activity_id": None,
                "has_hr_fit": False,
            },
            "run-1": {
                "key": "run-1",
                "activityId": 1,
                "activityName": "INDOOR_ROWING",
                "sport_type": "Workout",
                "startTime": 1782601200000,
                "totalTime": 1800,
                "totalDistance": 5000,
                "strava_activity_id": None,
                "has_hr_fit": False,
            },
        },
    )

    progress = format_week_zone_volume_progress(
        tmp_path, athlete_id, date(2026, 6, 28), include_unprescribed=True
    )

    assert "Prescribed rowing logged: 50 / 92 min" in progress
    assert "Unprescribed endurance: 75 min" in progress
    assert "Total endurance logged incl. unprescribed: 125 / 92 min" in progress


def test_week_zone_progress_counts_same_day_extra_erg_as_unprescribed_bonus(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 6, 22))
    athlete_id = 113
    save_athlete_weekly_plan(
        tmp_path,
        athlete_id,
        week,
        plan_text="week",
        plan_json=_mixed_week_personal_plan(),
    )
    scores_dir = tmp_path / f"athlete_{athlete_id}" / "erg_scores"
    scores_dir.mkdir(parents=True)
    for rec in (_jack_session_record(), _same_day_extra_erg_record()):
        (scores_dir / f"{rec['id']}.json").write_text(json.dumps(rec), encoding="utf-8")

    progress = format_week_zone_volume_progress(
        tmp_path, athlete_id, date(2026, 6, 23), include_unprescribed=True
    )

    assert "Prescribed rowing logged: 50 / 92 min" in progress
    assert "Unprescribed endurance: 45 min" in progress
    assert "Total endurance logged incl. unprescribed: 95 / 92 min" in progress


def test_collect_activities_in_weeks_includes_non_erg_endurance_rows(tmp_path: Path):
    athlete_id = 114
    _write_index_activities(tmp_path, athlete_id, [_ride_activity("2026-06-27", 45)])

    acts = collect_activities_in_weeks(
        [AthleteCfg(id=athlete_id, label="Test", token_dir=None)],
        tmp_path,
        [date(2026, 6, 22)],
    )

    assert any((a.get("sport_type") or a.get("type")) == "Ride" for a in acts)


def test_collect_activities_in_weeks_includes_suunto_non_erg_endurance_rows(tmp_path: Path):
    athlete_id = 115
    _write_suunto_workouts(
        tmp_path,
        athlete_id,
        {
            "ride-1": {
                "key": "ride-1",
                "activityId": 99,
                "activityName": "Cycling",
                "sport_type": "Ride",
                "startTime": 1782514800000,
                "totalTime": 3600,
                "totalDistance": 25000,
                "strava_activity_id": None,
                "has_hr_fit": False,
            }
        },
    )

    acts = collect_activities_in_weeks(
        [AthleteCfg(id=athlete_id, label="Test", token_dir=None)],
        tmp_path,
        [date(2026, 6, 22)],
    )

    assert any((a.get("sport_type") or a.get("type")) == "Ride" for a in acts)


def test_collect_activities_in_weeks_includes_suunto_generic_endurance_workout(
    tmp_path: Path,
):
    athlete_id = 120
    _write_suunto_workouts(
        tmp_path,
        athlete_id,
        {
            "workout-1": {
                "key": "workout-1",
                "activityId": 99,
                "activityName": "Long aerobic workout",
                "sport_type": "Workout",
                "startTime": 1782514800000,
                "totalTime": 3600,
                "totalDistance": 12000,
                "strava_activity_id": None,
                "has_hr_fit": False,
            }
        },
    )

    acts = collect_activities_in_weeks(
        [AthleteCfg(id=athlete_id, label="Test", token_dir=None)],
        tmp_path,
        [date(2026, 6, 22)],
    )

    assert any((a.get("sport_type") or a.get("type")) == "Workout" for a in acts)


def _write_season_master_plan_week(cache_dir: Path, week_monday: date, *, z2: int, z5: int) -> None:
    md = (
        "## Weekly progression\n\n"
        "| Week | Phase | Tgt priority | Tgt km | Tgt min | Tgt Z2 | Tgt Z5 | Tgt gym kg "
        "| Pln km | Pln min | Pln Z2 | Pln Z5 | Pln gym kg "
        "| Act km | Act min | Act Z2 | Act Z5 | Act gym kg |\n"
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: "
        "| ---: | ---: | ---: | ---: | ---: "
        "| ---: | ---: | ---: | ---: | ---: |\n"
        f"| {week_monday.isoformat()} | base | hr | 50.0 | 300 | {z2}% | {z5}% | 5000 "
        "| — | — | — | — | — | — | — | — | — | — |\n"
    )
    (cache_dir / "season_master_plan.md").write_text(md, encoding="utf-8")


def test_session_week_zone_progress_omits_season_goals(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 6, 22))
    athlete_id = 116
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

    progress = format_week_zone_volume_progress(
        tmp_path, athlete_id, date(2026, 6, 23)
    )

    assert "Logged zone mix: Z2" in progress
    assert "season week goal" not in progress


def test_week_zone_progress_includes_season_goals_when_requested(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 6, 22))
    athlete_id = 117
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

    progress = format_week_zone_volume_progress(
        tmp_path,
        athlete_id,
        date(2026, 6, 23),
        include_season_goals=True,
    )

    assert "season week goal: Z2 ~78% of erg/row time" in progress
