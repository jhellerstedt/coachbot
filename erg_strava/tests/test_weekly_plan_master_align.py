"""Tests for weekly_plan_master_align (season master plan validation/correction)."""

from __future__ import annotations

import pytest

from weekly_plan_master_align import (
    correct_weekly_plan,
    enforce_weekly_plan_alignment,
    extract_weekly_targets,
    format_regression_checklist,
    regression_checklist_deload_2026_06_29,
    validate_weekly_plan,
    weekly_targets_to_json,
    WeeklyTarget,
)
from weekly_plan_schema import (
    GYM_CATEGORY_LEG,
    GYM_CATEGORY_UPPER_CORE,
    parse_weekly_plan,
    planned_metrics_from_plan_json,
    weekly_plan_to_dict,
)


SAMPLE_SEASON_MD = """# Season Master Plan

**Season:** 2026-06-01 – 2027-03-15 (Australia/Melbourne)
**Macro generated:** 2026-06-01T00:00:00
**Races:** Head of the Yarra (2026-11-30)

## Weekly progression

| Week | Phase | Tgt priority | Tgt km | Tgt min | Tgt Z2 | Tgt Z5 | Tgt gym kg | Pln km | Pln min | Pln Z2 | Pln Z5 | Pln gym kg | Act km | Act min | Act Z2 | Act Z5 | Act gym kg |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-06-08 | build | hr | 48 | 280 | 85% | 5% | 6000 | — | — | — | — | — | — | — | — | — | 6200 |
| 2026-06-15 | build | hr | 48 | 280 | 85% | 5% | 6500 | — | — | — | — | — | — | — | — | — | 6400 |
| 2026-06-22 | build | hr | 48 | 280 | 85% | 5% | 7000 | — | — | — | — | — | — | — | — | — | 6600 |
| 2026-06-29 | deload | hr | 42 | 250 | 85% | 5% | 3000 | — | — | — | — | — | — | — | — | — | — |
| 2026-07-06 | base | hr | 48 | 280 | 85% | 5% | 3500 | — | — | — | — | — | — | — | — | — | — |
| 2026-10-12 | peak | split | 38 | 220 | 70% | 20% | 3200 | — | — | — | — | — | — | — | — | — | — |
"""


def _gym_day(weekday: str, day_date: str, category: str, *, heavy: bool = True) -> dict:
    if category == GYM_CATEGORY_LEG:
        exercises = [
            {"name": "Back squat", "sets": [{"reps": 6, "weight_kg": 60, "duration_sec": None}] * 3},
            {"name": "Hex-bar deadlift", "sets": [{"reps": 5, "weight_kg": 80, "duration_sec": None}] * 3},
            {"name": "Bulgarian split squat", "sets": [{"reps": 8, "weight_kg": 30, "duration_sec": None}] * 3},
            {"name": "Kettlebell swings", "sets": [{"reps": 12, "weight_kg": 24, "duration_sec": None}] * 3},
        ]
    else:
        exercises = [
            {"name": "Incline bench press", "sets": [{"reps": 8, "weight_kg": 40, "duration_sec": None}, {"reps": 8, "weight_kg": 45, "duration_sec": None}, {"reps": 8, "weight_kg": 50, "duration_sec": None}]},
            {"name": "Barbell row", "sets": [{"reps": 8, "weight_kg": 50, "duration_sec": None}, {"reps": 8, "weight_kg": 55, "duration_sec": None}, {"reps": 8, "weight_kg": 55, "duration_sec": None}]},
            {"name": "Lat pull-down", "sets": [{"reps": 8, "weight_kg": 90, "duration_sec": None}, {"reps": 8, "weight_kg": 95, "duration_sec": None}]},
            {"name": "Plank", "sets": [{"reps": 1, "weight_kg": None, "duration_sec": 30}]},
        ]
    return {
        "weekday": weekday,
        "date": day_date,
        "session_type": "gym",
        "session_subtype": "strength",
        "gym": {"category": category, "goal": "strength", "exercises": exercises},
        "rowing": None,
        "notes": None,
    }


def _hi_intensity_erg_day(weekday: str, day_date: str) -> dict:
    return {
        "weekday": weekday,
        "date": day_date,
        "session_type": "erg",
        "session_subtype": "threshold",
        "gym": None,
        "rowing": {
            "segments": [
                {
                    "phase": "warm_up",
                    "label": "Warm-up",
                    "duration": "8 min",
                    "split_min": "2:05",
                    "split_max": "2:15",
                    "zone_z": "Z2",
                    "zone_t": "T3",
                    "hr_bpm_min": 130,
                    "hr_bpm_max": 148,
                    "priority": "split",
                    "notes": None,
                },
                {
                    "phase": "main_set",
                    "label": "Main Set: 6×3 min / 2 min rest",
                    "duration": "6×3 min / 2 min rest",
                    "split_min": "1:58",
                    "split_max": "2:02",
                    "zone_z": "Z4",
                    "zone_t": "T6",
                    "hr_bpm_min": 165,
                    "hr_bpm_max": 178,
                    "priority": "split",
                    "notes": None,
                },
                {
                    "phase": "cool_down",
                    "label": "Cool-down",
                    "duration": "8 min",
                    "split_min": "2:15",
                    "split_max": "2:25",
                    "zone_z": "Z2",
                    "zone_t": "T2",
                    "hr_bpm_min": 120,
                    "hr_bpm_max": 140,
                    "priority": "split",
                    "notes": None,
                },
            ],
            "erg_alternative": None,
        },
        "notes": None,
    }


def _sprint_erg_day(weekday: str, day_date: str) -> dict:
    return {
        "weekday": weekday,
        "date": day_date,
        "session_type": "erg",
        "session_subtype": "race-pace",
        "gym": None,
        "rowing": {
            "segments": [
                {
                    "phase": "warm_up",
                    "label": "Warm-up",
                    "duration": "10 min",
                    "split_min": "2:05",
                    "split_max": "2:15",
                    "zone_z": "Z2",
                    "zone_t": "T3",
                    "hr_bpm_min": 130,
                    "hr_bpm_max": 148,
                    "priority": "split",
                    "notes": None,
                },
                {
                    "phase": "main_set",
                    "label": "Main Set: 5×500 m / 3 min rest",
                    "duration": "5×500 m / 3 min rest",
                    "split_min": "1:48",
                    "split_max": "1:52",
                    "zone_z": "Z5",
                    "zone_t": "T7",
                    "hr_bpm_min": 175,
                    "hr_bpm_max": 183,
                    "priority": "split",
                    "notes": None,
                },
                {
                    "phase": "cool_down",
                    "label": "Cool-down",
                    "duration": "8 min",
                    "split_min": "2:15",
                    "split_max": "2:25",
                    "zone_z": "Z2",
                    "zone_t": "T2",
                    "hr_bpm_min": 120,
                    "hr_bpm_max": 140,
                    "priority": "split",
                    "notes": None,
                },
            ],
            "erg_alternative": None,
        },
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


def misaligned_deload_plan_dict() -> dict:
    """CoachBot-style plan ignoring deload phase constraints."""
    return {
        "version": 1,
        "personalised": False,
        "greeting": None,
        "days": [
            _gym_day("Monday", "2026-06-29", GYM_CATEGORY_LEG, heavy=True),
            _hi_intensity_erg_day("Tuesday", "2026-06-30"),
            _gym_day("Wednesday", "2026-07-01", GYM_CATEGORY_UPPER_CORE, heavy=True),
            _sprint_erg_day("Thursday", "2026-07-02"),
            _rest_day("Friday", "2026-07-03"),
            _rest_day("Saturday", "2026-07-04"),
            _rest_day("Sunday", "2026-07-05"),
        ],
    }


def deload_targets() -> dict[str, WeeklyTarget]:
    return {
        "2026-06-29": WeeklyTarget(
            week="2026-06-29",
            phase="deload",
            tgt_priority="hr",
            tgt_z2_percent=85.0,
            tgt_z5_percent=5.0,
            tgt_km=42.0,
            tgt_min=250,
            tgt_gym_kg=3200.0,
        )
    }


def test_extract_weekly_targets_from_season_md():
    targets = extract_weekly_targets(SAMPLE_SEASON_MD)
    assert "2026-06-29" in targets
    w = targets["2026-06-29"]
    assert w.phase == "deload"
    assert w.tgt_priority == "hr"
    assert w.tgt_z2_percent == 85.0
    assert w.tgt_z5_percent == 5.0
    assert w.tgt_km == 42.0
    assert w.tgt_min == 250
    assert w.tgt_gym_kg == 3200.0


def test_weekly_targets_json_export():
    exported = weekly_targets_to_json(SAMPLE_SEASON_MD)
    assert "weeklyTargets" in exported
    assert exported["weeklyTargets"]["2026-07-06"]["phase"] == "base"
    assert exported["weeklyTargets"]["2026-10-12"]["tgt_priority"] == "split"


def test_validate_misaligned_deload_plan_flags_violations():
    plan = misaligned_deload_plan_dict()
    result = validate_weekly_plan("2026-06-29", plan, deload_targets())
    fields = {v.field for v in result.violations}
    assert "zone_distribution" in fields
    assert "split_priority" in fields
    assert "phase_intensity" in fields
    assert "missing_modality" in fields
    assert any(v.severity == "critical" for v in result.violations)


def test_enforce_weekly_plan_alignment_always_runs_when_target_exists():
    """Even a compliant plan must pass through the corrector when targets exist."""
    misaligned = misaligned_deload_plan_dict()
    corrected_plan, _ = correct_weekly_plan(
        "2026-06-29", misaligned, deload_targets()
    )
    compliant = weekly_plan_to_dict(corrected_plan)
    before_violations = validate_weekly_plan(
        "2026-06-29", compliant, deload_targets()
    ).violations

    result = enforce_weekly_plan_alignment(
        "2026-06-29",
        compliant,
        deload_targets(),
    )
    assert result.aligned is True
    assert result.plan_text.strip()
    assert result.plan_markdown.strip()
    assert isinstance(result.plan_json, dict)
    if before_violations:
        assert result.corrected is True


def test_enforce_alignment_preserves_gym_program_metadata():
    compliant = weekly_plan_to_dict(
        correct_weekly_plan("2026-06-29", misaligned_deload_plan_dict(), deload_targets())[0]
    )
    compliant["gym_program"] = {"id": "build-a", "week_index": 4}
    compliant["session_library"] = {"tuesday": "z2-steady-25"}
    result = enforce_weekly_plan_alignment(
        "2026-06-29",
        compliant,
        deload_targets(),
    )
    assert result.plan_json["gym_program"]["week_index"] == 4
    assert result.plan_json["session_library"]["tuesday"] == "z2-steady-25"


def test_correct_deload_plan_passes_regression_checklist():
    misaligned = misaligned_deload_plan_dict()
    corrected_plan, after = correct_weekly_plan(
        "2026-06-29", misaligned, deload_targets()
    )
    assert all(v.severity != "critical" for v in after.violations)

    checklist = regression_checklist_deload_2026_06_29(
        weekly_plan_to_dict(corrected_plan)
    )
    report = format_regression_checklist(checklist)
    assert "Regression test results" in report
    assert all(checklist.values()), report

    metrics = planned_metrics_from_plan_json(weekly_plan_to_dict(corrected_plan))
    assert metrics["z2_percent"] >= 80.0
    assert 2560 <= metrics["gym_tonnage_kg"] <= 3840

    for day in corrected_plan.days:
        if day.session_type == "gym" and day.gym:
            for ex in day.gym.exercises:
                assert len(ex.sets) == 2, ex.name


def _z2_steady_erg_day(weekday: str, day_date: str) -> dict:
    z2_seg = {
        "phase": "main_set",
        "label": "Aerobic steady-state",
        "duration": "25 min",
        "split_min": "2:05",
        "split_max": "2:15",
        "zone_z": "Z2",
        "zone_t": "T3",
        "hr_bpm_min": 130,
        "hr_bpm_max": 148,
        "priority": "hr",
        "notes": None,
    }
    warm = {
        "phase": "warm_up",
        "label": "Warm-up",
        "duration": "8 min",
        "split_min": "2:15",
        "split_max": "2:25",
        "zone_z": "Z2",
        "zone_t": "T2",
        "hr_bpm_min": 120,
        "hr_bpm_max": 140,
        "priority": "hr",
        "notes": None,
    }
    cool = {
        "phase": "cool_down",
        "label": "Cool-down",
        "duration": "8 min",
        "split_min": "2:20",
        "split_max": "2:30",
        "zone_z": "Z2",
        "zone_t": "T2",
        "hr_bpm_min": 118,
        "hr_bpm_max": 135,
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
            "segments": [warm, z2_seg, cool],
            "erg_alternative": None
            if weekday == "Tuesday"
            else {
                "description": "Erg alternative",
                "segments": [warm, z2_seg, cool],
            },
        },
        "notes": None,
    }


def base_targets() -> dict[str, WeeklyTarget]:
    return {
        "2026-07-06": WeeklyTarget(
            week="2026-07-06",
            phase="base",
            tgt_priority="hr",
            tgt_z2_percent=80.0,
            tgt_z5_percent=8.0,
            tgt_km=50.0,
            tgt_min=300,
            tgt_gym_kg=4600.0,
        )
    }


def _rowing_main_only(day_date: str, *, z5: bool = False) -> dict:
    main = {
        "phase": "main_set",
        "label": "Main Set: 5×500 m",
        "duration": "35 min",
        "split_min": "1:50" if z5 else "2:00",
        "split_max": "1:55" if z5 else "2:08",
        "zone_z": "Z5" if z5 else "Z4",
        "zone_t": "T7" if z5 else "T6",
        "hr_bpm_min": 160,
        "hr_bpm_max": 178,
        "priority": "hr",
        "notes": None,
    }
    return {
        "weekday": "Tuesday",
        "date": day_date,
        "session_type": "erg",
        "session_subtype": "threshold" if not z5 else "intervals",
        "gym": None,
        "rowing": {"segments": [main], "erg_alternative": None},
        "notes": None,
    }


def test_correct_base_adds_warmup_cooldown_and_preserves_z4():
    data = {
        "version": 1,
        "personalised": False,
        "greeting": None,
        "days": [
            _gym_day("Monday", "2026-07-06", GYM_CATEGORY_LEG, heavy=True),
            _rowing_main_only("2026-07-07", z5=False),
            _gym_day("Wednesday", "2026-07-08", GYM_CATEGORY_UPPER_CORE, heavy=True),
            _z2_steady_erg_day("Thursday", "2026-07-09"),
            _rest_day("Friday", "2026-07-10"),
            _rest_day("Saturday", "2026-07-11"),
            _rest_day("Sunday", "2026-07-12"),
        ],
    }
    data["days"][3]["session_type"] = "on_water"
    corrected, _ = correct_weekly_plan("2026-07-06", data, base_targets())
    tuesday = next(d for d in corrected.days if d.weekday == "Tuesday")
    phases = {s.phase for s in tuesday.rowing.segments}
    assert "warm_up" in phases
    assert "cool_down" in phases
    main = next(s for s in tuesday.rowing.segments if s.phase == "main_set")
    assert main.zone_z == "Z4"


def test_correct_base_downgrades_z5_to_threshold():
    data = {
        "version": 1,
        "personalised": False,
        "greeting": None,
        "days": [
            _gym_day("Monday", "2026-07-06", GYM_CATEGORY_LEG, heavy=True),
            _rowing_main_only("2026-07-07", z5=True),
            _gym_day("Wednesday", "2026-07-08", GYM_CATEGORY_UPPER_CORE, heavy=True),
            _z2_steady_erg_day("Thursday", "2026-07-09"),
            _rest_day("Friday", "2026-07-10"),
            _rest_day("Saturday", "2026-07-11"),
            _rest_day("Sunday", "2026-07-12"),
        ],
    }
    corrected, _ = correct_weekly_plan("2026-07-06", data, base_targets())
    tuesday = next(d for d in corrected.days if d.weekday == "Tuesday")
    main = next(s for s in tuesday.rowing.segments if s.phase == "main_set")
    assert main.zone_z == "Z4"
    assert main.zone_t == "T6"


def test_correct_base_expands_gym_to_three_sets():
    data = {
        "version": 1,
        "personalised": False,
        "greeting": None,
        "days": [
            _gym_day("Monday", "2026-07-06", GYM_CATEGORY_LEG, heavy=False),
            _z2_steady_erg_day("Tuesday", "2026-07-07"),
            _gym_day("Wednesday", "2026-07-08", GYM_CATEGORY_UPPER_CORE, heavy=False),
            _z2_steady_erg_day("Thursday", "2026-07-09"),
            _rest_day("Friday", "2026-07-10"),
            _rest_day("Saturday", "2026-07-11"),
            _rest_day("Sunday", "2026-07-12"),
        ],
    }
    for day in data["days"]:
        if day.get("gym"):
            for ex in day["gym"]["exercises"]:
                ex["sets"] = ex["sets"][:2]
    corrected, _ = correct_weekly_plan("2026-07-06", data, base_targets())
    for day in corrected.days:
        if day.session_type == "gym" and day.gym:
            for ex in day.gym.exercises:
                assert len(ex.sets) >= 3, ex.name
                if ex.sets[0].duration_sec:
                    continue
                if ex.sets[0].weight_kg and ex.sets[0].weight_kg > 0:
                    weights = [s.weight_kg for s in ex.sets]
                    assert weights == sorted(weights), ex.name
                    assert weights[0] < weights[-1], ex.name


def _base_week_plan(*, personalised: bool = False) -> dict:
    return {
        "version": 1,
        "personalised": personalised,
        "greeting": "Jack," if personalised else None,
        "days": [
            _gym_day("Monday", "2026-07-06", GYM_CATEGORY_LEG, heavy=False),
            _hi_intensity_erg_day("Tuesday", "2026-07-07"),
            _gym_day("Wednesday", "2026-07-08", GYM_CATEGORY_UPPER_CORE, heavy=False),
            _z2_steady_erg_day("Thursday", "2026-07-09"),
            _rest_day("Friday", "2026-07-10"),
            _rest_day("Saturday", "2026-07-11"),
            _rest_day("Sunday", "2026-07-12"),
        ],
    }


def test_repair_fixed_schedule_restores_missed_gym_and_erg():
    from weekly_plan_master_align import repair_fixed_weekly_schedule

    data = {
        "version": 1,
        "personalised": False,
        "greeting": None,
        "days": [
            _gym_day("Monday", "2026-07-06", GYM_CATEGORY_LEG, heavy=False),
            _rest_day("Tuesday", "2026-07-07"),
            _rest_day("Wednesday", "2026-07-08"),
            _z2_steady_erg_day("Thursday", "2026-07-09"),
            _rest_day("Friday", "2026-07-10"),
            _rest_day("Saturday", "2026-07-11"),
            _rest_day("Sunday", "2026-07-12"),
        ],
    }
    data["days"][3]["session_type"] = "on_water"
    plan = parse_weekly_plan(data)
    assert plan is not None
    repaired = repair_fixed_weekly_schedule(
        plan, include_lifting=True, phase="base", priority="hr"
    )
    tuesday = next(d for d in repaired.days if d.weekday == "Tuesday")
    wednesday = next(d for d in repaired.days if d.weekday == "Wednesday")
    assert tuesday.session_type == "erg"
    assert tuesday.rowing is not None
    assert wednesday.session_type == "gym"
    assert wednesday.gym is not None
    assert wednesday.gym.category == GYM_CATEGORY_UPPER_CORE
    warm = {s.phase for s in tuesday.rowing.segments}
    assert "warm_up" in warm and "cool_down" in warm


def test_repair_uses_squad_rowing_when_athlete_tuesday_is_rest():
    from weekly_plan_master_align import repair_fixed_weekly_schedule

    squad_data = _base_week_plan()
    squad_data["days"][3]["session_type"] = "on_water"
    athlete_data = _base_week_plan(personalised=True)
    athlete_data["days"][1] = _rest_day("Tuesday", "2026-07-07")
    squad = parse_weekly_plan(squad_data)
    athlete = parse_weekly_plan(athlete_data)
    assert squad is not None and athlete is not None
    repaired = repair_fixed_weekly_schedule(
        athlete,
        include_lifting=True,
        phase="base",
        reference_plan=squad,
        priority="hr",
    )
    tuesday = next(d for d in repaired.days if d.weekday == "Tuesday")
    assert tuesday.session_type == "erg"
    main = next(s for s in tuesday.rowing.segments if s.phase == "main_set")
    assert main.zone_z == "Z4"


def test_base_week_plan_passes_session_cap_with_on_water_erg_alt():
    from weekly_plan_schema import validate_plan_session_constraints

    plan = parse_weekly_plan(_base_week_plan())
    assert plan is not None
    assert validate_plan_session_constraints(plan) is None


def test_enforce_session_cap_trims_oversized_steady_state():
    from weekly_plan_master_align import _enforce_session_duration_caps

    data = _base_week_plan()
    thu = next(d for d in data["days"] if d["weekday"] == "Thursday")
    thu["rowing"]["segments"][1]["duration"] = "35 min"
    plan = parse_weekly_plan(data)
    assert plan is not None
    thu_before = next(d for d in plan.days if d.weekday == "Thursday")
    from weekly_plan_schema import estimate_rowing_session_minutes

    assert estimate_rowing_session_minutes(thu_before.rowing) > 45
    capped = _enforce_session_duration_caps(plan, priority="hr")
    thu_after = next(d for d in capped.days if d.weekday == "Thursday")
    assert estimate_rowing_session_minutes(thu_after.rowing) <= 45


def test_enforce_session_cap_keeps_recommended_erg():
    from weekly_plan_master_align import _enforce_session_duration_caps
    from test_weekly_plan_schema import _recommended_erg_dict, sample_squad_plan_dict

    data = sample_squad_plan_dict()
    data["recommended_erg"] = _recommended_erg_dict()
    plan = parse_weekly_plan(data)
    assert plan is not None
    capped = _enforce_session_duration_caps(plan, priority="hr")
    assert capped.recommended_erg is not None
    assert capped.recommended_erg.id == "z2-30-continuous"
