from datetime import date

from generate_training_plan import week_bounds_from_monday
from squad_plan_fallback import build_library_squad_plan_json
from weekly_plan_schema import parse_weekly_plan, validate_plan_session_constraints


def test_fallback_plan_is_valid_structured_week():
    week = week_bounds_from_monday(date(2026, 8, 31))
    data = build_library_squad_plan_json(
        plan_week=week,
        phase="build",
        include_lifting=True,
        peak_kg_by_exercise={"Back squat": 80.0},
        prev_plan_json={"gym_program": {"id": "build-a", "week_index": 0}},
    )
    plan = parse_weekly_plan(data)
    assert plan is not None
    assert validate_plan_session_constraints(plan) is None
    by = {day.weekday: day for day in plan.days}
    assert by["Monday"].session_type == "gym"
    assert by["Tuesday"].session_type == "erg"
    assert by["Tuesday"].rowing is not None
    assert {segment.phase for segment in by["Tuesday"].rowing.segments} >= {
        "warm_up",
        "main_set",
        "cool_down",
    }
    assert by["Thursday"].session_type == "on_water"
    assert by["Thursday"].rowing is not None
    assert by["Thursday"].rowing.erg_alternative is not None
    assert by["Sunday"].session_type == "rest"
    assert by["Sunday"].rowing is None
    names = [exercise.name for exercise in by["Monday"].gym.exercises]
    assert "Back squat" in names
    assert data["gym_program"]["week_index"] == 1


def test_fallback_without_lifting_uses_rest_days():
    week = week_bounds_from_monday(date(2026, 8, 31))
    data = build_library_squad_plan_json(
        plan_week=week,
        phase="build",
        include_lifting=False,
        peak_kg_by_exercise={},
        prev_plan_json=None,
    )

    plan = parse_weekly_plan(data)
    assert plan is not None
    assert validate_plan_session_constraints(plan) is None
    by = {day.weekday: day for day in plan.days}
    assert by["Monday"].session_type == "rest"
    assert by["Wednesday"].session_type == "rest"
    assert "gym_program" not in data
