"""Tests for squad_adherence_stats."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from athlete_profile import AthleteProfile
from generate_training_plan import WeekBounds, week_bounds_from_monday
from squad_adherence_stats import (
    _bucket_erg_from_scores,
    _bucket_erg_points,
    _median_std,
    compose_adherence_review,
    compute_squad_week_adherence_stats,
    format_squad_adherence_stats,
    SquadAdherenceStats,
    AthleteWeekAdherenceStats,
)
from generate_training_plan import rowing_prescriptions_for_phase


def test_median_std():
    med, std = _median_std([100.0, 200.0, 300.0])
    assert med == 200.0
    assert std is not None and abs(std - 81.65) < 0.1


def test_bucket_erg_points_splits_zones():
    profile = AthleteProfile(id=1, label="Test", max_hr_bpm=200)
    rows = []
    for i, (hr, split) in enumerate(
        [
            (120, 140.0),
            (125, 138.0),
            (170, 115.0),
            (175, 112.0),
        ]
    ):
        rows.append(
            {
                "activity_id": 1,
                "time": float(i * 10),
                "hr": hr,
                "split_500": split,
            }
        )
    sub = pd.DataFrame(rows)
    z13_split, z13_min, z4p_split, z4p_min = _bucket_erg_points(sub, profile)
    assert z13_split is not None
    assert z13_min > 0
    assert z4p_split is not None
    assert z4p_min > 0


def test_screenshot_score_fills_zero_stream_minutes(tmp_path):
    week = week_bounds_from_monday(datetime(2026, 8, 24).date())
    scores = tmp_path / "athlete_1" / "erg_scores"
    scores.mkdir(parents=True)
    (scores / "s.json").write_text(
        json.dumps(
            {
                "id": "s",
                "athlete_id": 1,
                "athlete_label": "Jack H",
                "session_date": "2026-08-27",
                "source": "zulip_screenshot_vision_multi",
                "metrics": {
                    "duration_sec": 2160,
                    "avg_hr": 141,
                    "avg_split_500_sec": 133.0,
                },
            }
        )
    )

    stats = compute_squad_week_adherence_stats(
        week,
        [],
        {},
        pd.DataFrame(),
        cache_dir=tmp_path,
        athlete_profiles={
            1: AthleteProfile(id=1, label="Jack H", max_hr_bpm=185)
        },
        gym_types=frozenset(),
        gym_name_patterns=(),
    )

    jack = stats.athlete_stats[0]
    assert jack.z13_minutes >= 30
    assert jack.z13_split_median_sec == 133.0


def test_screenshot_score_is_not_double_counted_with_merged_stream(tmp_path):
    week = week_bounds_from_monday(datetime(2026, 8, 24).date())
    scores = tmp_path / "athlete_1" / "erg_scores"
    scores.mkdir(parents=True)
    (scores / "s.json").write_text(
        json.dumps(
            {
                "id": "s",
                "session_date": "2026-08-27",
                "merged_suunto_workout_key": "suunto-1",
                "metrics": {
                    "duration_sec": 2160,
                    "avg_hr": 141,
                    "avg_split_500_sec": 133.0,
                },
            }
        )
    )
    erg_df = pd.DataFrame(
        {
            "activity_id": [99, 99],
            "suunto_key": ["suunto-1", "suunto-1"],
            "activity_start": [
                "2026-08-27T08:00:00Z",
                "2026-08-27T08:00:10Z",
            ],
            "athlete": ["Jack H", "Jack H"],
            "time": [0.0, 10.0],
            "hr": [141.0, 141.0],
            "split_500": [133.0, 133.0],
        }
    )

    stats = compute_squad_week_adherence_stats(
        week,
        [],
        {},
        erg_df,
        cache_dir=tmp_path,
        athlete_profiles={
            1: AthleteProfile(id=1, label="Jack H", max_hr_bpm=185)
        },
        gym_types=frozenset(),
        gym_name_patterns=(),
    )

    assert stats.athlete_stats[0].z13_minutes < 1


def test_screenshot_score_plural_suunto_keys_are_not_double_counted(tmp_path):
    week = week_bounds_from_monday(datetime(2026, 8, 24).date())
    scores = tmp_path / "athlete_1" / "erg_scores"
    scores.mkdir(parents=True)
    (scores / "s.json").write_text(
        json.dumps(
            {
                "id": "s",
                "session_date": "2026-08-27",
                "merged_suunto_workout_keys": ["k1", "k2"],
                "metrics": {
                    "duration_sec": 2160,
                    "avg_hr": 141,
                    "avg_split_500_sec": 133.0,
                },
            }
        )
    )
    erg_df = pd.DataFrame(
        {
            "activity_id": [99, 99],
            "suunto_key": ["k1", "k1"],
            "activity_start": [
                "2026-08-27T08:00:00Z",
                "2026-08-27T08:00:10Z",
            ],
            "athlete": ["Jack H", "Jack H"],
            "time": [0.0, 10.0],
            "hr": [141.0, 141.0],
            "split_500": [133.0, 133.0],
        }
    )

    stats = compute_squad_week_adherence_stats(
        week,
        [],
        {},
        erg_df,
        cache_dir=tmp_path,
        athlete_profiles={
            1: AthleteProfile(id=1, label="Jack H", max_hr_bpm=185)
        },
        gym_types=frozenset(),
        gym_name_patterns=(),
    )

    assert stats.athlete_stats[0].z13_minutes < 1


def test_screenshot_session_parts_are_zoned_individually():
    profile = AthleteProfile(id=1, label="Jack H", max_hr_bpm=185)
    records = [
        {
            "metrics": {
                "avg_split_500_sec": 130.0,
                "session_parts": [
                    {"duration_sec": 1200, "avg_hr": 141},
                    {
                        "duration_sec": 600,
                        "avg_hr": 170,
                        "avg_split_500_sec": 115.0,
                    },
                ],
            }
        }
    ]

    z13_splits, z13_min, z4p_splits, z4p_min = _bucket_erg_from_scores(
        records, profile
    )

    assert z13_splits == [130.0]
    assert z13_min == 20
    assert z4p_splits == [115.0]
    assert z4p_min == 10


def test_format_squad_adherence_stats():
    week = week_bounds_from_monday(datetime(2026, 6, 30, tzinfo=timezone.utc).date())
    stats = SquadAdherenceStats(
        athlete_stats=(
            AthleteWeekAdherenceStats(
                athlete_id=1,
                label="A",
                gym_tonnage_kg=6000,
                z13_split_median_sec=128.0,
                z13_minutes=90,
                z4p_split_median_sec=115.0,
                z4p_minutes=20,
            ),
            AthleteWeekAdherenceStats(
                athlete_id=2,
                label="B",
                gym_tonnage_kg=7000,
                z13_split_median_sec=130.0,
                z13_minutes=100,
                z4p_split_median_sec=118.0,
                z4p_minutes=25,
            ),
        )
    )
    text = format_squad_adherence_stats(stats, week)
    assert "Gym tonnage" in text
    assert "Erg Z1–Z3" in text
    assert "Erg Z4+" in text
    assert "σ" in text
    assert "n=2" in text


def test_compose_adherence_review():
    combined = compose_adherence_review(
        "**Logged squad stats**\n- Gym tonnage: median 6500 kg",
        "Overall compliance was good.",
    )
    assert combined.startswith("**Logged squad stats**")
    assert "Overall compliance was good." in combined


def test_rowing_prescriptions_include_phase():
    text = rowing_prescriptions_for_phase("base")
    assert "### This week's rowing prescriptions (base week)" in text
    assert "threshold intervals" in text

    deload = rowing_prescriptions_for_phase("deload")
    assert "(deload week)" in deload
    assert "No Z4/Z5" in deload or "Z2/T3" in deload
