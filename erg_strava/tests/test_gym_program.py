"""Tests for gym mesocycle programs and load progression."""

from __future__ import annotations

import copy
from gym_program import (
    LiftLog,
    ProgressionRule,
    apply_gym_program_to_plan,
    apply_progression,
    format_lift_review,
    format_rpe_follow_up,
    gate_gym_session_for_recovery,
    gym_log_missing_rpe,
    infer_poor_recovery,
    lift_logs_from_metrics,
    load_program,
    materialize_week,
    median_latest_peak_kg,
    next_gym_week_index,
    personalize_plan_gym_loads,
    progression_decision,
    validate_program,
)
from weekly_plan_schema import (
    GYM_CATEGORY_LEG,
    GYM_CATEGORY_UPPER_CORE,
    GYM_EXERCISE_NAMES,
    GymExercise,
    GymSession,
    GymSet,
    parse_gym_session_harness_json,
)
from test_weekly_plan_schema import sample_squad_plan_dict


def test_seed_programs_validate():
    for phase in ("base", "build"):
        program = load_program(phase)
        err = validate_program(program)
        assert err is None, f"{phase}: {err}"
        assert len(program.monday.exercises) == 4
        assert len(program.wednesday.exercises) == 4
        assert program.monday.category == GYM_CATEGORY_LEG
        assert program.wednesday.category == GYM_CATEGORY_UPPER_CORE
        for day in (program.monday, program.wednesday):
            for ex in day.exercises:
                assert ex.name in GYM_EXERCISE_NAMES


def test_apply_gym_program_to_plan_overwrites_llm_names():
    plan = sample_squad_plan_dict()
    assert plan["days"][0]["gym"]["exercises"][0]["name"] == "Back squat"
    program = load_program("base")
    monday, wednesday = materialize_week(
        program, week_index=0, peak_kg_by_exercise={}
    )
    patched = apply_gym_program_to_plan(
        plan, monday, wednesday, week_index=0, program_id=program.id
    )
    mon_names = [e["name"] for e in patched["days"][0]["gym"]["exercises"]]
    wed_names = [e["name"] for e in patched["days"][2]["gym"]["exercises"]]
    assert mon_names == [e.name for e in program.monday.exercises]
    assert wed_names == [e.name for e in program.wednesday.exercises]
    assert patched["gym_program"]["id"] == program.id
    assert patched["gym_program"]["week_index"] == 0
    # Original LLM plan is not mutated.
    assert plan["days"][0]["gym"]["exercises"][1]["name"] == "Hex-bar deadlift"


def test_deload_week_reuses_previous_exercise_names():
    program = load_program("base")
    prev_monday, prev_wednesday = materialize_week(
        program, week_index=0, peak_kg_by_exercise={"Back squat": 80.0}
    )
    prev_plan = apply_gym_program_to_plan(
        sample_squad_plan_dict(), prev_monday, prev_wednesday, week_index=0
    )
    monday, wednesday = materialize_week(
        program,
        week_index=1,
        peak_kg_by_exercise={"Back squat": 80.0},
        prev_plan_json=prev_plan,
        phase="deload",
    )
    assert [e.name for e in monday.exercises] == [e.name for e in prev_monday.exercises]
    assert [e.name for e in wednesday.exercises] == [
        e.name for e in prev_wednesday.exercises
    ]


def test_rotation_replaces_exercise_after_configured_weeks():
    program = load_program("base")
    assert program.rotations
    rotation = program.rotations[0]
    before, _ = materialize_week(program, week_index=rotation.after_weeks - 1)
    after, _ = materialize_week(program, week_index=rotation.after_weeks)
    before_names = [e.name for e in before.exercises]
    after_names = [e.name for e in after.exercises]
    assert rotation.replace in before_names
    assert rotation.replace not in after_names
    assert rotation.with_name in after_names


def test_median_latest_peak_kg_per_athlete():
    metrics = {
        1: {
            "athlete_id": 10,
            "start_date": "2026-06-08T12:00:00+00:00",
            "gym": {
                "exercises": [
                    {"name": "Back squat", "max_weight_kg": 80.0, "sets": []}
                ]
            },
        },
        2: {
            "athlete_id": 20,
            "start_date": "2026-06-08T12:00:00+00:00",
            "gym": {
                "exercises": [
                    {"name": "Back squat", "max_weight_kg": 100.0, "sets": []}
                ]
            },
        },
        3: {
            "athlete_id": 10,
            "start_date": "2026-06-01T12:00:00+00:00",
            "gym": {
                "exercises": [
                    {"name": "Back squat", "max_weight_kg": 120.0, "sets": []}
                ]
            },
        },
    }
    peaks = median_latest_peak_kg(metrics)
    assert peaks["Back squat"] == 90.0


def test_progression_two_hits_increments():
    rule = ProgressionRule(increment_kg=2.5, hit_sessions_required=2)
    logs = [
        LiftLog(peak_kg=100.0, hit=True),
        LiftLog(peak_kg=100.0, hit=True),
    ]
    assert progression_decision(logs, rule) == "progress"
    assert apply_progression(logs, rule) == 102.5


def test_progression_single_hit_holds():
    rule = ProgressionRule(increment_kg=2.5, hit_sessions_required=2)
    logs = [LiftLog(peak_kg=100.0, hit=True)]
    assert progression_decision(logs, rule) == "hold"
    assert apply_progression(logs, rule) == 100.0


def test_personalize_plan_keeps_squad_names_and_uses_athlete_peak():
    program = load_program("base")
    monday, wednesday = materialize_week(
        program, week_index=0, peak_kg_by_exercise={"Back squat": 70.0}
    )
    squad = apply_gym_program_to_plan(
        sample_squad_plan_dict(), monday, wednesday, week_index=0
    )
    athlete = copy.deepcopy(squad)
    athlete["personalised"] = True
    # Pretend the LLM invented different kg.
    athlete["days"][0]["gym"]["exercises"][0]["sets"][0]["weight_kg"] = 40.0
    logs = {
        "Back squat": [
            LiftLog(peak_kg=90.0, hit=True),
            LiftLog(peak_kg=90.0, hit=True),
        ]
    }
    patched = personalize_plan_gym_loads(
        athlete, squad, lift_logs_by_exercise=logs, program=program
    )
    squad_names = [e["name"] for e in squad["days"][0]["gym"]["exercises"]]
    athlete_names = [e["name"] for e in patched["days"][0]["gym"]["exercises"]]
    assert athlete_names == squad_names
    squat_sets = patched["days"][0]["gym"]["exercises"][0]["sets"]
    peaks = [s["weight_kg"] for s in squat_sets if s.get("weight_kg")]
    assert max(peaks) == 92.5


def test_next_gym_week_index_increments_from_prev_plan():
    assert next_gym_week_index(None) == 0
    assert next_gym_week_index({"gym_program": {"week_index": 2}}) == 3


def test_rpe_grind_holds_even_when_reps_hit():
    rule = ProgressionRule(increment_kg=2.5, hit_sessions_required=2)
    logs = [
        LiftLog(peak_kg=100.0, hit=True, rpe=7.0),
        LiftLog(peak_kg=100.0, hit=True, rpe=9.5),
    ]
    assert progression_decision(logs, rule) == "hold"
    assert apply_progression(logs, rule) == 100.0


def test_rpe_easy_hits_progress():
    rule = ProgressionRule(increment_kg=2.5, hit_sessions_required=2)
    logs = [
        LiftLog(peak_kg=80.0, hit=True, rpe=6.0),
        LiftLog(peak_kg=80.0, hit=True, rpe=7.0),
    ]
    assert progression_decision(logs, rule) == "progress"


def test_reps_missed_regresses_one_increment():
    rule = ProgressionRule(increment_kg=2.5, hit_sessions_required=2)
    logs = [LiftLog(peak_kg=100.0, hit=False, rpe=8.0)]
    assert progression_decision(logs, rule) == "regress"
    assert apply_progression(logs, rule) == 97.5


def test_parse_harness_json_keeps_optional_rpe():
    raw = {
        "unit": "kg",
        "exercises": [
            {
                "name": "Back squat",
                "sets": [
                    {"reps": 8, "weight_kg": 70, "duration_sec": None, "rpe": 7},
                ],
            }
        ],
        "assumptions": None,
    }
    import json

    parsed = parse_gym_session_harness_json(json.dumps(raw))
    assert parsed is not None
    assert parsed["exercises"][0]["sets"][0]["rpe"] == 7


def test_gym_log_missing_rpe_and_follow_up():
    record = {
        "gym": {
            "exercises": [
                {
                    "name": "Back squat",
                    "sets": [{"reps": 8, "weight_kg": 70}],
                }
            ]
        }
    }
    assert gym_log_missing_rpe(record) is True
    record["gym"]["exercises"][0]["sets"][0]["rpe"] = 7
    assert gym_log_missing_rpe(record) is False
    text = format_rpe_follow_up()
    assert "RPE" in text
    assert "easy" in text.lower()


def test_lift_logs_from_metrics_use_sets_and_rpe():
    metrics = {
        1: {
            "athlete_id": 10,
            "start_date": "2026-06-08T12:00:00+00:00",
            "gym": {
                "exercises": [
                    {
                        "name": "Back squat",
                        "max_weight_kg": 100.0,
                        "sets": [
                            {"reps": 8, "weight_kg": 80.0},
                            {"reps": 7, "weight_kg": 100.0, "rpe": 6},
                        ],
                    }
                ]
            },
        }
    }
    logs = lift_logs_from_metrics(metrics)
    assert logs["Back squat"][0].peak_kg == 100.0
    assert logs["Back squat"][0].rpe == 6.0
    assert logs["Back squat"][0].hit is True


def test_format_lift_review_mentions_progress_and_stall():
    text = format_lift_review(
        {
            "Back squat": "progress",
            "Romanian deadlift": "hold",
        }
    )
    assert "Back squat" in text
    assert "progress" in text.lower()
    assert "Romanian deadlift" in text


def test_gate_gym_session_scales_today_only():
    gym = GymSession(
        category=GYM_CATEGORY_LEG,
        goal="strength",
        exercises=[
            GymExercise(
                name="Back squat",
                sets=[
                    GymSet(8, 60.0, None),
                    GymSet(8, 70.0, None),
                    GymSet(8, 80.0, None),
                ],
            )
        ],
    )
    original_sets = list(gym.exercises[0].sets)
    gated = gate_gym_session_for_recovery(gym, poor_recovery=True)
    assert len(gated.exercises[0].sets) == 2
    assert gym.exercises[0].sets == original_sets
    unchanged = gate_gym_session_for_recovery(gym, poor_recovery=False)
    assert len(unchanged.exercises[0].sets) == 3


def test_infer_poor_recovery_from_message():
    assert infer_poor_recovery("slept terribly and feel wrecked")
    assert infer_poor_recovery("HRV is in the dirt, go easy today")
    assert not infer_poor_recovery("what's today's session?")


def test_materialize_uses_provided_peak_kg():
    program = load_program("base")
    monday, _ = materialize_week(
        program, week_index=0, peak_kg_by_exercise={"Back squat": 85.0}
    )
    squat = next(e for e in monday.exercises if e.name == "Back squat")
    assert infer_peak_from_sets(squat.sets) == 85.0


def test_apply_program_gym_to_plan_sets_program_id():
    from gym_program import apply_program_gym_to_plan

    patched = apply_program_gym_to_plan(
        sample_squad_plan_dict(),
        phase="base",
        week_index=0,
        peak_kg_by_exercise={"Back squat": 85.0},
    )
    assert patched["gym_program"]["id"] == "base-a"
    names = [e["name"] for e in patched["days"][0]["gym"]["exercises"]]
    assert names[0] == "Back squat"
    squat = patched["days"][0]["gym"]["exercises"][0]
    peaks = [s["weight_kg"] for s in squat["sets"] if s.get("weight_kg")]
    assert max(peaks) == 85.0


def test_template_gym_session_matches_program():
    from gym_program import template_gym_session

    gym = template_gym_session("Monday", phase="base")
    assert gym.category == GYM_CATEGORY_LEG
    assert gym.exercises[0].name == "Back squat"
    assert len(gym.exercises) == 4


def infer_peak_from_sets(sets) -> float:
    weights = [float(s.weight_kg) for s in sets if s.weight_kg]
    return max(weights) if weights else 0.0
