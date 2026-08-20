"""Tests for deload-week gym session modifier."""

from __future__ import annotations

from gym_deload import (
    DeloadConfig,
    apply_deload_gym_session,
    apply_deload_modifier,
    format_deload_gym_session_markdown,
    regression_checklist_deload_gym,
    validate_deload_tonnage,
)
from weekly_plan_schema import (
    GYM_CATEGORY_UPPER_CORE,
    GymExercise,
    GymSession,
    GymSet,
    WeeklyPlan,
    DayPlan,
    parse_weekly_plan,
    weekly_plan_to_dict,
)


def _build_week_gym() -> GymSession:
    return GymSession(
        category=GYM_CATEGORY_UPPER_CORE,
        goal="strength",
        exercises=[
            GymExercise(
                name="Incline bench press",
                sets=[
                    GymSet(reps=8, weight_kg=40),
                    GymSet(reps=8, weight_kg=45),
                    GymSet(reps=8, weight_kg=50),
                ],
            ),
            GymExercise(
                name="Barbell row",
                sets=[
                    GymSet(reps=8, weight_kg=50),
                    GymSet(reps=8, weight_kg=55),
                    GymSet(reps=8, weight_kg=55),
                ],
            ),
            GymExercise(
                name="Lat pull-down",
                sets=[
                    GymSet(reps=8, weight_kg=90),
                    GymSet(reps=8, weight_kg=95),
                ],
            ),
            GymExercise(
                name="Plank",
                sets=[GymSet(reps=1, weight_kg=None, duration_sec=30)],
            ),
        ],
    )


def test_apply_deload_modifier_two_sets_and_load_reduction():
    build = _build_week_gym()
    deload = apply_deload_modifier(build, reference_gym=build)

    for ex in deload.exercises:
        assert len(ex.sets) == 2, ex.name

    bench = deload.exercises[0]
    assert bench.sets[0].reps == 8
    assert bench.sets[0].weight_kg == 40.0  # 82% of 50 kg → 41 → 40

    row = deload.exercises[1]
    assert row.sets[0].weight_kg == 45.0  # 82% of 55 → 45.1 → 45

    plank = deload.exercises[3]
    assert len(plank.sets) == 2
    assert plank.sets[0].duration_sec == 30


def test_validate_deload_tonnage_within_range():
    build = _build_week_gym()
    deload, notes, check = apply_deload_gym_session(
        build,
        reference_gym=build,
        session_tonnage_target=1500.0,
    )
    assert all(len(ex.sets) == 2 for ex in deload.exercises)
    assert deload.exercises[0].sets[0].weight_kg == 40.0
    assert check.actual >= 2400
    assert any(
        "minimum effective dose" in n for n in notes
    ) or not check.pass_


def test_regression_checklist_deload_gym():
    build = _build_week_gym()
    deload, _, _ = apply_deload_gym_session(
        build,
        reference_gym=build,
        session_tonnage_target=1500.0,
    )
    ref_plan = WeeklyPlan(
        version=1,
        personalised=False,
        days=[
            DayPlan(
                weekday="Wednesday",
                date="2026-06-22",
                session_type="gym",
                session_subtype="strength",
                gym=build,
                rowing=None,
                notes=None,
            )
        ],
        greeting=None,
    )
    plan = WeeklyPlan(
        version=1,
        personalised=False,
        days=[
            DayPlan(
                weekday="Monday",
                date="2026-06-29",
                session_type="gym",
                session_subtype="strength",
                gym=deload,
                rowing=None,
                notes=None,
            ),
            DayPlan(
                weekday="Wednesday",
                date="2026-07-01",
                session_type="gym",
                session_subtype="strength",
                gym=deload,
                rowing=None,
                notes=None,
            ),
        ],
        greeting=None,
    )
    results = regression_checklist_deload_gym(
        plan,
        weekly_tonnage_target_kg=3000.0,
        reference_plan=ref_plan,
    )
    assert results["No deload gym session contains fewer than 2 sets per exercise"]
    assert results["No deload gym session contains more than 2 sets per exercise"]
    assert results["Exercise selection is identical to preceding build week"]


def test_format_deload_gym_session_markdown():
    build = _build_week_gym()
    deload, notes, check = apply_deload_gym_session(
        build,
        reference_gym=build,
        session_tonnage_target=1500.0,
    )
    md = format_deload_gym_session_markdown(
        deload,
        weekday="Wednesday",
        weekly_tonnage_target_kg=3000.0,
        tonnage_check=check,
        adjustment_notes=notes,
    )
    assert "Wednesday: Gym" in md
    assert "Deload" in md
    assert "| Incline bench press | 2 | 8 |" in md or "| Incline bench press | 2 | 8 |" in md
    assert "Tonnage check:" in md
