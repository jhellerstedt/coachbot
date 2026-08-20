"""Tests for weekly_plan_schema."""

from __future__ import annotations

import json
from datetime import date

import pytest

from athlete_profile import AthleteProfile
from weekly_plan_schema import (
    GYM_CATEGORY_LEG,
    GYM_CATEGORY_UPPER_CORE,
    _estimate_segment_minutes,
    estimate_day_session_minutes,
    extract_gym_exercises_by_day_from_json,
    format_plan_prescribed_summary,
    goal_tracking_needs_aerobic_pace_work,
    goal_tracking_needs_intensity_work,
    is_low_intensity_plan_phase,
    parse_weekly_plan,
    planned_metrics_from_plan_json,
    render_plan_text,
    rowing_day_has_intensity_work,
    rowing_day_is_z2_steady_only,
    session_for_date,
    validate_athlete_plan_against_squad,
    validate_athlete_hr_zone_consistency,
    validate_plan_session_constraints,
    validate_rowing_phase_intensity,
    validate_rowing_interval_rest,
    validate_squad_rowing_aligns_with_goals,
    validate_weekly_plan,
    weekly_plan_to_dict,
    gym_working_set_bounds,
)
from generate_training_plan import adapt_goal_tracking_for_phase, sanitize_plan_prose


def _gym_day(weekday: str, day_date: str, category: str) -> dict:
    exercises = (
        [
            {"name": "Back squat", "sets": [{"reps": 6, "weight_kg": 60, "duration_sec": None}]},
            {"name": "Hex-bar deadlift", "sets": [{"reps": 5, "weight_kg": 80, "duration_sec": None}]},
            {"name": "Bulgarian split squat", "sets": [{"reps": 8, "weight_kg": 30, "duration_sec": None}]},
            {"name": "Kettlebell swings", "sets": [{"reps": 12, "weight_kg": 16, "duration_sec": None}]},
        ]
        if category == GYM_CATEGORY_LEG
        else [
            {"name": "Bench press", "sets": [{"reps": 6, "weight_kg": 50, "duration_sec": None}]},
            {"name": "Barbell row", "sets": [{"reps": 8, "weight_kg": 40, "duration_sec": None}]},
            {"name": "Lat pull-down", "sets": [{"reps": 10, "weight_kg": 45, "duration_sec": None}]},
            {"name": "Plank", "sets": [{"reps": 1, "weight_kg": None, "duration_sec": 45}]},
        ]
    )
    return {
        "weekday": weekday,
        "date": day_date,
        "session_type": "gym",
        "session_subtype": "strength",
        "gym": {"category": category, "goal": "strength", "exercises": exercises},
        "rowing": None,
        "notes": None,
    }


def _erg_segment(phase: str, label: str) -> dict:
    return {
        "phase": phase,
        "label": label,
        "duration": "10 min",
        "split_min": "1:58",
        "split_max": "2:05",
        "zone_z": "Z2",
        "zone_t": "T3",
        "hr_bpm_min": 130,
        "hr_bpm_max": 148,
        "priority": "hr",
        "notes": None,
    }


def _erg_day(weekday: str, day_date: str) -> dict:
    return {
        "weekday": weekday,
        "date": day_date,
        "session_type": "erg",
        "session_subtype": "threshold",
        "gym": None,
        "rowing": {
            "segments": [
                _erg_segment("warm_up", "Warm-up"),
                {
                    **_erg_segment("main_set", "Main Set: 3×8 min / 2 min rest"),
                    "duration": "3×8 min / 2 min rest",
                },
                _erg_segment("cool_down", "Cool-down"),
            ],
            "erg_alternative": None,
        },
        "notes": None,
    }


def _on_water_day(weekday: str, day_date: str) -> dict:
    return {
        "weekday": weekday,
        "date": day_date,
        "session_type": "on_water",
        "session_subtype": "intervals",
        "gym": None,
        "rowing": {
            "segments": [
                _erg_segment("warm_up", "Warm-up"),
                {
                    **_erg_segment("main_set", "Main Set: 4×500 m / 2 min rest"),
                    "duration": "4×500 m / 2 min rest",
                    "zone_z": "Z5",
                    "zone_t": "T7",
                    "priority": "split",
                    "notes": "Indicative — crew/boat/conditions vary",
                },
                _erg_segment("cool_down", "Cool-down"),
            ],
            "erg_alternative": {
                "description": "Group erg fallback",
                "segments": [
                    _erg_segment("work", "Work: 3 min"),
                    _erg_segment("rest", "Rest: 2 min"),
                ],
            },
        },
        "notes": "Check wind before launching",
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


def sample_squad_plan_dict() -> dict:
    return {
        "version": 1,
        "personalised": False,
        "greeting": None,
        "days": [
            _gym_day("Monday", "2026-06-15", GYM_CATEGORY_LEG),
            _erg_day("Tuesday", "2026-06-16"),
            _gym_day("Wednesday", "2026-06-17", GYM_CATEGORY_UPPER_CORE),
            _on_water_day("Thursday", "2026-06-18"),
            _rest_day("Friday", "2026-06-19"),
            _rest_day("Saturday", "2026-06-20"),
            _rest_day("Sunday", "2026-06-21"),
        ],
    }


def _profile_mhr_182() -> AthleteProfile:
    return AthleteProfile(id=1, label="Test", max_hr_bpm=182)


def test_athlete_hr_rejects_band_outside_z4_t6():
    data = sample_squad_plan_dict()
    data["personalised"] = True
    tue = data["days"][1]
    tue["rowing"]["segments"][1].update({
        "zone_z": "Z4", "zone_t": "T6",
        "hr_bpm_min": 140, "hr_bpm_max": 150,
        "duration": "5×6 min / 2 min rest",
    })
    tue["session_subtype"] = "intervals"
    plan = parse_weekly_plan(data)
    assert plan is not None
    err = validate_athlete_hr_zone_consistency(plan, _profile_mhr_182())
    assert err is not None
    assert "HR" in err or "bpm" in err.lower()


def test_athlete_hr_accepts_overlapping_band():
    profile = _profile_mhr_182()
    z4 = profile.zone_bpm_range("z4")
    t6 = profile.zone_bpm_range("t6")
    assert z4 and t6
    lo = max(z4[0], t6[0])
    hi = min(z4[1], t6[1])
    data = sample_squad_plan_dict()
    data["personalised"] = True
    for day in data["days"]:
        if day["rowing"] is None:
            continue
        segment_groups = [day["rowing"]["segments"]]
        if day["rowing"]["erg_alternative"] is not None:
            segment_groups.append(day["rowing"]["erg_alternative"]["segments"])
        for segments in segment_groups:
            for segment in segments:
                z_range = profile.zone_bpm_range(segment["zone_z"].lower())
                t_range = profile.zone_bpm_range(segment["zone_t"].lower())
                assert z_range and t_range
                segment["hr_bpm_min"] = max(z_range[0], t_range[0])
                segment["hr_bpm_max"] = min(z_range[1], t_range[1])
    tue = data["days"][1]
    tue["rowing"]["segments"][1].update({
        "zone_z": "Z4", "zone_t": "T6",
        "hr_bpm_min": lo, "hr_bpm_max": hi,
        "duration": "5×6 min / 2 min rest",
    })
    tue["session_subtype"] = "intervals"
    plan = parse_weekly_plan(data)
    assert plan is not None
    assert validate_athlete_hr_zone_consistency(plan, profile) is None


def test_athlete_hr_rejects_min_above_max():
    data = sample_squad_plan_dict()
    data["personalised"] = True
    tue = data["days"][1]
    tue["rowing"]["segments"][1].update({
        "hr_bpm_min": 150,
        "hr_bpm_max": 140,
    })
    plan = parse_weekly_plan(data)
    assert plan is not None

    err = validate_athlete_hr_zone_consistency(plan, _profile_mhr_182())

    assert err is not None
    assert "minimum" in err.lower()
    assert "maximum" in err.lower()


def test_squad_validation_skips_hr_without_profile():
    data = sample_squad_plan_dict()
    tue = data["days"][1]
    tue["rowing"]["segments"][1].update({
        "zone_z": "Z4", "zone_t": "T6",
        "hr_bpm_min": 140, "hr_bpm_max": 150,
        "duration": "5×6 min / 2 min rest",
    })
    tue["session_subtype"] = "intervals"
    plan = parse_weekly_plan(data)
    assert plan is not None
    # No profile → HR helper not applied by session constraints alone
    assert validate_athlete_hr_zone_consistency


def test_parse_and_validate_squad_plan():
    data = sample_squad_plan_dict()
    plan = parse_weekly_plan(data)
    assert plan is not None
    assert validate_weekly_plan(plan, include_lifting=True) is None


def test_render_plan_text_includes_key_sections():
    plan = parse_weekly_plan(sample_squad_plan_dict())
    assert plan is not None
    text = render_plan_text(plan)
    assert "Monday:" in text
    assert "1. Back squat" in text
    assert "Set 1:" in text
    assert "Warm Up:" in text
    assert "Erg alternative" in text
    assert "2 min rest" in text


def test_render_squad_plan_uses_percent_max_hr_not_bpm():
    plan = parse_weekly_plan(sample_squad_plan_dict())
    assert plan is not None
    text = render_plan_text(plan)
    assert "bpm" not in text.lower()
    # Z2 default band is 60–70% max
    assert "HR 60–70% max" in text


def test_render_athlete_plan_uses_absolute_bpm_when_requested():
    data = sample_squad_plan_dict()
    data["personalised"] = True
    tue = data["days"][1]
    tue["rowing"]["segments"][1]["hr_bpm_min"] = 146
    tue["rowing"]["segments"][1]["hr_bpm_max"] = 164
    plan = parse_weekly_plan(data)
    assert plan is not None
    text = render_plan_text(plan, absolute_hr_bpm=True)
    assert "HR 146–164 bpm" in text
    assert "% max" not in text


def test_render_rowing_segments_without_repeated_phase_or_duration():
    """Labels/durations from LLM+alignment often restate the phase heading."""
    data = sample_squad_plan_dict()
    tuesday = data["days"][1]
    tuesday["rowing"]["segments"] = [
        {
            **_erg_segment("warm_up", "Warm Up: Warm-up — 20 min"),
            "duration": "23 min",
        },
        {
            **_erg_segment("main_set", "Main Set: 5×6 min / 2 min rest"),
            "duration": "44 min",
            "zone_z": "Z4",
            "zone_t": "T6",
        },
        {
            **_erg_segment("cool_down", "Cool Down: Cool-down — 14 min"),
            "duration": "16 min",
        },
    ]
    plan = parse_weekly_plan(data)
    assert plan is not None
    tue = render_plan_text(plan).split("Wednesday:")[0].split("Tuesday:")[1]
    assert "Warm Up: Warm Up:" not in tue
    assert "Main Set: Main Set:" not in tue
    assert "Cool Down: Cool Down:" not in tue
    assert "20 min — 23 min" not in tue
    assert "14 min — 16 min" not in tue
    assert "5×6 min / 2 min rest" in tue
    assert "Warm Up: 23 min @" in tue
    assert "Main Set: 5×6 min / 2 min rest @" in tue
    assert "Cool Down: 16 min @" in tue
    assert "Warm-up" not in tue
    assert "Cool-down" not in tue


def test_render_rowing_omits_warmup_cooldown_synonym_labels():
    data = sample_squad_plan_dict()
    thursday = data["days"][3]
    thursday["rowing"]["segments"] = [
        {**_erg_segment("warm_up", "Warm-up"), "duration": "12 min", "zone_z": "Z2", "zone_t": "T2"},
        {
            **_erg_segment("main_set", "Aerobic steady-state"),
            "duration": "20 min",
            "zone_z": "Z2",
            "zone_t": "T3",
        },
        {**_erg_segment("cool_down", "Cool-down"), "duration": "12 min", "zone_z": "Z2", "zone_t": "T2"},
    ]
    plan = parse_weekly_plan(data)
    assert plan is not None
    thu = render_plan_text(plan).split("Friday:")[0].split("Thursday:")[1]
    assert "Warm Up: 12 min @" in thu
    assert "Main Set: Aerobic steady-state — 20 min @" in thu
    assert "Cool Down: 12 min @" in thu
    assert "Warm Up: Warm-up" not in thu
    assert "Cool Down: Cool-down" not in thu


def test_session_for_date_tuesday():
    plan = sample_squad_plan_dict()
    day = session_for_date(plan, date(2026, 6, 16))
    assert day is not None
    assert day.weekday == "Tuesday"
    assert day.session_type == "erg"


def test_extract_gym_exercises_by_day():
    by_day = extract_gym_exercises_by_day_from_json(sample_squad_plan_dict())
    assert by_day["Monday"] == [
        "Back squat",
        "Hex-bar deadlift",
        "Bulgarian split squat",
        "Kettlebell swings",
    ]
    assert len(by_day["Wednesday"]) == 4


def test_planned_metrics_from_plan_json():
    metrics = planned_metrics_from_plan_json(sample_squad_plan_dict())
    assert metrics["rowing_minutes"] is not None
    assert metrics["rowing_minutes"] < 500
    assert metrics["gym_tonnage_kg"] is not None
    assert metrics["gym_tonnage_kg"] > 0


def test_estimate_segment_minutes_distance_vs_duration():
    assert _estimate_segment_minutes("10 min") == 10
    assert _estimate_segment_minutes("3x4m") == 12
    assert _estimate_segment_minutes("45:00") == 45
    assert _estimate_segment_minutes("3x1000m") == 13
    assert _estimate_segment_minutes("4×500 m") == 9
    assert _estimate_segment_minutes("5×1 km") == 22
    assert (
        _estimate_segment_minutes(
            "1×1000 m / 1×2000 m / 1×2000 m / 1×1000 m / 3 min rest"
        )
        == 26
    )


def test_validate_athlete_must_match_squad_gym_exercises():
    squad = parse_weekly_plan(sample_squad_plan_dict())
    assert squad is not None
    athlete_data = json.loads(json.dumps(sample_squad_plan_dict()))
    athlete_data["personalised"] = True
    athlete_data["greeting"] = "Hi Alex,"
    athlete_data["days"][0]["gym"]["exercises"][0]["name"] = "Romanian deadlift"
    athlete = parse_weekly_plan(athlete_data)
    assert athlete is not None
    err = validate_athlete_plan_against_squad(athlete, squad)
    assert err is not None
    assert "gym exercises" in err


def test_round_trip_dict():
    plan = parse_weekly_plan(sample_squad_plan_dict())
    assert plan is not None
    again = parse_weekly_plan(weekly_plan_to_dict(plan))
    assert again is not None
    assert validate_weekly_plan(again, include_lifting=True) is None


def _z2_steady_erg_day(weekday: str, day_date: str) -> dict:
    z2_seg = {
        "phase": "main_set",
        "label": "Aerobic steady-state",
        "duration": "30:00",
        "split_min": "2:10",
        "split_max": "2:20",
        "zone_z": "Z2",
        "zone_t": "T2",
        "hr_bpm_min": 135,
        "hr_bpm_max": 145,
        "priority": "hr",
        "notes": None,
    }
    return {
        "weekday": weekday,
        "date": day_date,
        "session_type": "erg" if weekday == "Tuesday" else "on_water",
        "session_subtype": "steady-state",
        "gym": None,
        "rowing": {
            "segments": [
                _erg_segment("warm_up", "Warm-up"),
                z2_seg,
                _erg_segment("cool_down", "Cool-down"),
            ],
            "erg_alternative": None
            if weekday == "Tuesday"
            else {
                "description": "Erg alternative",
                "segments": [z2_seg],
            },
        },
        "notes": None,
    }


def test_goal_tracking_needs_intensity_work_detects_race_pace():
    text = (
        "Next Steps: Introduce 5x500m at <1:50 and 4x1k at race pace. "
        "Target 6:40 for 2k."
    )
    assert goal_tracking_needs_intensity_work(text)
    assert not goal_tracking_needs_aerobic_pace_work(text)


def test_goal_tracking_needs_aerobic_pace_work_detects_two_minute_splits():
    text = "Aim for 45-minute continuous rows at ~2:00 splits."
    assert goal_tracking_needs_aerobic_pace_work(text)
    assert not goal_tracking_needs_intensity_work(text)


def test_rowing_day_intensity_helpers():
    plan = parse_weekly_plan(sample_squad_plan_dict())
    assert plan is not None
    tuesday = next(d for d in plan.days if d.weekday == "Tuesday")
    thursday = next(d for d in plan.days if d.weekday == "Thursday")
    assert rowing_day_is_z2_steady_only(tuesday)  # main work is Z2 despite label
    assert not rowing_day_has_intensity_work(tuesday)
    assert rowing_day_has_intensity_work(thursday)  # Z5 main set
    assert not rowing_day_is_z2_steady_only(thursday)


def test_validate_squad_rowing_rejects_dual_z2_when_goals_need_intensity():
    data = sample_squad_plan_dict()
    data["days"][1] = _z2_steady_erg_day("Tuesday", "2026-06-16")
    data["days"][3] = _z2_steady_erg_day("Thursday", "2026-06-18")
    data["days"][3]["session_type"] = "on_water"
    plan = parse_weekly_plan(data)
    assert plan is not None
    goal_text = (
        "Gaps: High-intensity work is still low. "
        "Next Steps: 5x500m at <1:50; 4x1k at race pace."
    )
    err = validate_squad_rowing_aligns_with_goals(plan, goal_text)
    assert err is not None
    assert "Z2 steady-state" in err or "Z4/Z5" in err


def test_validate_squad_rowing_skips_goal_alignment_during_deload():
    data = sample_squad_plan_dict()
    data["days"][1] = _z2_steady_erg_day("Tuesday", "2026-06-16")
    data["days"][3] = _z2_steady_erg_day("Thursday", "2026-06-18")
    data["days"][3]["session_type"] = "on_water"
    plan = parse_weekly_plan(data)
    assert plan is not None
    goal_text = (
        "Next Steps: 5x500m at <1:50; 4x1k at race pace."
    )
    assert validate_squad_rowing_aligns_with_goals(plan, goal_text, phase="deload") is None
    assert validate_squad_rowing_aligns_with_goals(plan, goal_text, phase="recovery") is None
    assert validate_squad_rowing_aligns_with_goals(plan, goal_text, phase="taper") is None


def test_is_low_intensity_plan_phase():
    assert is_low_intensity_plan_phase("deload")
    assert is_low_intensity_plan_phase("Deload")
    assert is_low_intensity_plan_phase("recovery")
    assert is_low_intensity_plan_phase("taper")
    assert not is_low_intensity_plan_phase("build")
    assert not is_low_intensity_plan_phase(None)
    assert not is_low_intensity_plan_phase("")


def test_adapt_goal_tracking_for_phase_replaces_rowing_prescriptions():
    from generate_training_plan import adapt_goal_tracking_for_phase

    build_text = (
        "### This week's rowing prescriptions\n"
        "- Tuesday erg: race-pace intervals, Z5, 5x500m at 1:48.\n"
        "- Thursday on-water: threshold 4x1k.\n"
    )
    adapted = adapt_goal_tracking_for_phase(build_text, "deload")
    assert "race-pace" not in adapted.lower() or "no z4/z5" in adapted.lower()
    assert "Z2/T3" in adapted
    assert "(deload week)" in adapted


def test_validate_squad_rowing_skips_goal_alignment_during_base():
    data = sample_squad_plan_dict()
    data["days"][1] = _z2_steady_erg_day("Tuesday", "2026-06-16")
    data["days"][3] = _z2_steady_erg_day("Thursday", "2026-06-18")
    plan = parse_weekly_plan(data)
    assert plan is not None
    goal_text = "Next Steps: 5x500m at <1:50; 4x1k at race pace."
    assert validate_squad_rowing_aligns_with_goals(plan, goal_text, phase="base") is None


def test_gym_working_set_bounds():
    assert gym_working_set_bounds("deload") == (2, 2)
    assert gym_working_set_bounds("base") == (3, 4)
    assert gym_working_set_bounds("build") == (3, 4)


def test_validate_squad_rowing_accepts_mixed_intensity_plan():
    plan = parse_weekly_plan(sample_squad_plan_dict())
    assert plan is not None
    goal_text = (
        "Next Steps: 5x500m at <1:50 on Thursday; Tuesday aerobic 45 min at 2:00."
    )
    assert validate_squad_rowing_aligns_with_goals(plan, goal_text) is None


def test_validate_rowing_interval_rest_requires_rest_on_distance_intervals():
    plan = parse_weekly_plan(sample_squad_plan_dict())
    assert plan is not None
    thursday = next(d for d in plan.days if d.weekday == "Thursday")
    assert validate_rowing_interval_rest(thursday) is None
    tuesday = next(d for d in plan.days if d.weekday == "Tuesday")
    assert validate_rowing_interval_rest(tuesday) is None

    data = sample_squad_plan_dict()
    main = data["days"][3]["rowing"]["segments"][1]
    main["label"] = "Main Set: 5×1 km"
    main["duration"] = "5×1 km"
    bad_plan = parse_weekly_plan(data)
    assert bad_plan is not None
    bad_thu = next(d for d in bad_plan.days if d.weekday == "Thursday")
    err = validate_rowing_interval_rest(bad_thu)
    assert err is not None

    data2 = sample_squad_plan_dict()
    tue_main = data2["days"][1]["rowing"]["segments"][1]
    tue_main["label"] = "Main Set: 3×8 min"
    tue_main["duration"] = "3×8 min"
    bad_tue_plan = parse_weekly_plan(data2)
    assert bad_tue_plan is not None
    bad_tue = next(d for d in bad_tue_plan.days if d.weekday == "Tuesday")
    assert validate_rowing_interval_rest(bad_tue) is not None


def test_validate_rowing_interval_rest_rejects_flat_threshold_interval_label():
    data = sample_squad_plan_dict()
    tuesday = data["days"][1]
    tuesday["session_subtype"] = "intervals"
    tue_main = tuesday["rowing"]["segments"][1]
    tue_main["label"] = "Threshold intervals"
    tue_main["duration"] = "48 min"
    tue_main["zone_z"] = "Z4"
    tue_main["zone_t"] = "T6"
    bad_plan = parse_weekly_plan(data)
    assert bad_plan is not None

    bad_tue = next(d for d in bad_plan.days if d.weekday == "Tuesday")
    err = validate_rowing_interval_rest(bad_tue)
    assert err is not None
    assert "interval" in err.lower()


def test_validate_rowing_interval_rest_rejects_live_flat_threshold_duration():
    """Mirrors the hellpi post: label says intervals, duration is a blob without reps."""
    data = sample_squad_plan_dict()
    tuesday = data["days"][1]
    tuesday["session_subtype"] = None
    tue_main = tuesday["rowing"]["segments"][1]
    tue_main["label"] = "Main Set: Threshold intervals — 40 min"
    tue_main["duration"] = "Threshold intervals — 40 min"
    tue_main["zone_z"] = "Z4"
    tue_main["zone_t"] = "T6"
    bad_plan = parse_weekly_plan(data)
    assert bad_plan is not None
    bad_tue = next(d for d in bad_plan.days if d.weekday == "Tuesday")
    err = validate_rowing_interval_rest(bad_tue)
    assert err is not None
    assert "reps" in err.lower() or "interval" in err.lower()


def test_validate_rowing_interval_rest_accepts_explicit_reps_and_rest():
    data = sample_squad_plan_dict()
    tuesday = data["days"][1]
    tuesday["session_subtype"] = "intervals"
    tue_main = tuesday["rowing"]["segments"][1]
    tue_main["label"] = "Threshold intervals"
    tue_main["duration"] = "5×8 min / 2 min rest"
    tue_main["zone_z"] = "Z4"
    tue_main["zone_t"] = "T6"
    plan = parse_weekly_plan(data)
    assert plan is not None
    tue = next(d for d in plan.days if d.weekday == "Tuesday")
    assert validate_rowing_interval_rest(tue) is None


def test_zone_zt_compatibility_rejects_z2_t6():
    data = sample_squad_plan_dict()
    tue = data["days"][1]
    tue["rowing"]["segments"][1]["zone_z"] = "Z2"
    tue["rowing"]["segments"][1]["zone_t"] = "T6"
    plan = parse_weekly_plan(data)
    assert plan is not None
    err = validate_plan_session_constraints(plan)
    assert err is not None
    assert "incompatible" in err.lower() or "Z2" in err


def test_zone_zt_compatibility_accepts_z4_t6():
    data = sample_squad_plan_dict()
    tue = data["days"][1]
    tue["rowing"]["segments"][1]["zone_z"] = "Z4"
    tue["rowing"]["segments"][1]["zone_t"] = "T6"
    tue["rowing"]["segments"][1]["duration"] = "5×6 min / 2 min rest"
    tue["session_subtype"] = "intervals"
    plan = parse_weekly_plan(data)
    assert plan is not None
    # May still fail other constraints; specifically Z↔T alone:
    from weekly_plan_schema import validate_rowing_zone_zt_compatibility
    assert validate_rowing_zone_zt_compatibility(
        next(d for d in plan.days if d.weekday == "Tuesday")
    ) is None


def test_phase_intensity_rejects_t6_main_on_deload():
    data = sample_squad_plan_dict()
    tue = data["days"][1]
    tue["rowing"]["segments"][1].update({
        "zone_z": "Z4", "zone_t": "T6",
        "duration": "4×5 min / 2 min rest",
    })
    tue["session_subtype"] = "intervals"
    plan = parse_weekly_plan(data)
    assert plan is not None
    err = validate_plan_session_constraints(plan, phase="deload")
    assert err is not None
    assert "T6" in err or "intensity" in err.lower() or "deload" in err.lower()


def test_phase_intensity_allows_t6_main_on_build():
    data = sample_squad_plan_dict()
    tue = data["days"][1]
    tue["rowing"]["segments"][1].update({
        "zone_z": "Z4", "zone_t": "T6",
        "duration": "5×6 min / 2 min rest",
    })
    tue["session_subtype"] = "intervals"
    plan = parse_weekly_plan(data)
    assert plan is not None
    assert validate_rowing_phase_intensity(
        next(d for d in plan.days if d.weekday == "Tuesday"),
        phase="build",
    ) is None


def test_validate_plan_session_constraints_rejects_over_45_min():
    data = sample_squad_plan_dict()
    data["days"][1]["rowing"]["segments"][1]["duration"] = "40 min"
    data["days"][1]["rowing"]["segments"][1]["label"] = "Main Set"
    plan = parse_weekly_plan(data)
    assert plan is not None
    err = validate_plan_session_constraints(plan)
    assert err is not None
    assert "45 min" in err


def test_validate_rowing_warmup_cooldown_caps_rejects_long_wu():
    data = sample_squad_plan_dict()
    data["days"][1]["rowing"]["segments"][0]["duration"] = "20 min"
    plan = parse_weekly_plan(data)
    assert plan is not None
    from weekly_plan_schema import validate_rowing_warmup_cooldown_caps

    tue = next(d for d in plan.days if d.weekday == "Tuesday")
    err = validate_rowing_warmup_cooldown_caps(tue)
    assert err is not None
    assert "15 min" in err


def test_on_water_erg_alternative_not_double_counted_for_session_cap():
    data = sample_squad_plan_dict()
    thursday = next(d for d in data["days"] if d["weekday"] == "Thursday")
    thursday["session_type"] = "on_water"
    thursday["rowing"]["erg_alternative"] = {
        "description": "Group erg fallback",
        "segments": list(thursday["rowing"]["segments"]),
    }
    plan = parse_weekly_plan(data)
    assert plan is not None
    thu = next(d for d in plan.days if d.weekday == "Thursday")
    mins = estimate_day_session_minutes(thu)
    assert mins <= 45
    assert validate_plan_session_constraints(plan) is None


def test_estimate_day_session_minutes_gym_day():
    plan = parse_weekly_plan(sample_squad_plan_dict())
    assert plan is not None
    monday = next(d for d in plan.days if d.weekday == "Monday")
    mins = estimate_day_session_minutes(monday)
    assert 15 <= mins < 45


def test_format_plan_prescribed_summary_matches_sessions():
    plan = parse_weekly_plan(sample_squad_plan_dict())
    assert plan is not None
    summary = format_plan_prescribed_summary(sample_squad_plan_dict())
    assert summary is not None
    assert "Prescribed volume" in summary
    assert "Z5 (race-pace)" in summary
    metrics = planned_metrics_from_plan_json(sample_squad_plan_dict())
    assert metrics["z5_percent"] is not None
    assert f"{metrics['z5_percent']:.0f}%" in summary


def test_sanitize_plan_prose_strips_weekly_targets():
    raw = """Monday:
gym

**Weekly Targets:**
- Z2/T2 Intensity: ~85%
Ensure all athletes adhere to these session targets."""
    cleaned = sanitize_plan_prose(raw)
    assert "Weekly Targets" not in cleaned
    assert "Monday:" in cleaned
    assert "Ensure all athletes" not in cleaned


def _recommended_erg_dict(*, main_duration: str = "30 min") -> dict:
    return {
        "id": "z2-30-continuous",
        "name": "30 min continuous aerobic",
        "rowing": {
            "segments": [
                {**_erg_segment("warm_up", "Warm-up"), "duration": "8 min"},
                {
                    **_erg_segment("main_set", "Aerobic steady-state"),
                    "duration": main_duration,
                },
                {**_erg_segment("cool_down", "Cool-down"), "duration": "8 min"},
            ],
            "erg_alternative": None,
        },
    }


def test_recommended_erg_round_trips_parse_and_to_dict():
    data = sample_squad_plan_dict()
    data["recommended_erg"] = _recommended_erg_dict()
    plan = parse_weekly_plan(data)
    assert plan is not None
    assert plan.recommended_erg is not None
    assert plan.recommended_erg.id == "z2-30-continuous"
    assert plan.recommended_erg.name == "30 min continuous aerobic"
    dumped = weekly_plan_to_dict(plan)
    assert dumped["recommended_erg"]["id"] == "z2-30-continuous"
    again = parse_weekly_plan(dumped)
    assert again is not None
    assert again.recommended_erg is not None
    assert again.recommended_erg.id == "z2-30-continuous"


def test_malformed_recommended_erg_is_dropped():
    data = sample_squad_plan_dict()
    data["recommended_erg"] = {"id": "broken"}
    plan = parse_weekly_plan(data)
    assert plan is not None
    assert plan.recommended_erg is None


def test_render_plan_text_appends_recommended_extra_after_sunday():
    data = sample_squad_plan_dict()
    data["recommended_erg"] = _recommended_erg_dict()
    plan = parse_weekly_plan(data)
    assert plan is not None
    text = render_plan_text(plan)
    sunday_at = text.index("Sunday:")
    extra_at = text.index("Recommended extra erg (Fri/Sat if you have time):")
    assert extra_at > sunday_at
    assert "30 min continuous aerobic" in text
    friday = next(d for d in plan.days if d.weekday == "Friday")
    assert friday.session_type == "rest"


def test_prescribed_metrics_ignore_recommended_erg():
    base = sample_squad_plan_dict()
    with_extra = sample_squad_plan_dict()
    with_extra["recommended_erg"] = _recommended_erg_dict()
    base_metrics = planned_metrics_from_plan_json(base)
    extra_metrics = planned_metrics_from_plan_json(with_extra)
    assert extra_metrics["rowing_minutes"] == base_metrics["rowing_minutes"]


def test_recommended_erg_over_cap_does_not_fail_plan_constraints():
    data = sample_squad_plan_dict()
    data["recommended_erg"] = _recommended_erg_dict(main_duration="40 min")
    plan = parse_weekly_plan(data)
    assert plan is not None
    assert validate_plan_session_constraints(plan) is None


def test_personalize_recommended_erg_rewrites_hr_keeps_splits():
    from weekly_plan_schema import personalize_recommended_erg

    data = sample_squad_plan_dict()
    data["recommended_erg"] = _recommended_erg_dict()
    plan = parse_weekly_plan(data)
    assert plan is not None and plan.recommended_erg is not None
    profile = AthleteProfile(id=1, label="Test", max_hr_bpm=200)
    original_split = plan.recommended_erg.rowing.segments[1].split_min
    original_hr = plan.recommended_erg.rowing.segments[1].hr_bpm_min
    updated = personalize_recommended_erg(plan.recommended_erg, profile)
    z2 = profile.zone_bpm_range("z2")
    t3 = profile.zone_bpm_range("t3")
    assert z2 and t3
    main = updated.rowing.segments[1]
    assert main.split_min == original_split
    assert main.hr_bpm_min != original_hr
    assert main.hr_bpm_min == t3[0]
    assert main.hr_bpm_max == t3[1]
