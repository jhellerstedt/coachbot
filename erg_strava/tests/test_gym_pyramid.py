"""Tests for phase-based gym load pyramids."""

from __future__ import annotations

from gym_pyramid import (
    apply_phase_gym_pyramid,
    apply_pyramid_to_exercise,
    infer_peak_weight_kg,
)
from weekly_plan_schema import GymExercise, GymSession, GymSet


def _flat_exercise(name: str, weight: float, *, sets: int = 3) -> GymExercise:
    return GymExercise(
        name=name,
        sets=[GymSet(reps=8, weight_kg=weight, duration_sec=None) for _ in range(sets)],
    )


def test_infer_peak_weight_from_flat_sets():
    ex = _flat_exercise("Back squat", 80.0)
    assert infer_peak_weight_kg(ex.sets) == 80.0


def test_base_ascending_pyramid_four_sets():
    ex = apply_pyramid_to_exercise(
        _flat_exercise("Back squat", 100.0, sets=4),
        phase="base",
        min_sets=3,
        max_sets=4,
    )
    assert len(ex.sets) == 4
    weights = [s.weight_kg for s in ex.sets]
    assert weights == sorted(weights)
    assert weights[0] < weights[-1]
    assert weights[-1] == 100.0
    assert ex.sets[0].reps == 12
    assert ex.sets[-1].reps == 7


def test_build_reverse_pyramid_three_sets():
    ex = apply_pyramid_to_exercise(
        _flat_exercise("Back squat", 80.0, sets=3),
        phase="build",
        min_sets=3,
        max_sets=4,
    )
    weights = [s.weight_kg for s in ex.sets]
    assert weights == sorted(weights, reverse=True)
    assert weights[0] == 80.0
    assert ex.sets[0].reps == 5


def test_barbell_row_base_example():
    ex = apply_pyramid_to_exercise(
        _flat_exercise("Barbell row", 60.0, sets=4),
        phase="base",
        min_sets=3,
        max_sets=4,
    )
    assert ex.sets[0].weight_kg == 35.0  # 60% of 60
    assert ex.sets[1].weight_kg == 45.0  # 75%
    assert ex.sets[2].weight_kg == 52.5  # 87%
    assert ex.sets[3].weight_kg == 60.0


def test_pullups_rep_pyramid_base():
    ex = apply_pyramid_to_exercise(
        GymExercise(
            name="Pull-ups",
            sets=[GymSet(reps=5, weight_kg=0.0, duration_sec=None)] * 3,
        ),
        phase="base",
        min_sets=3,
        max_sets=4,
    )
    reps = [s.reps for s in ex.sets]
    assert reps == [12, 10, 8]


def test_plank_equal_timed_sets():
    ex = apply_pyramid_to_exercise(
        GymExercise(
            name="Plank",
            sets=[GymSet(reps=1, weight_kg=None, duration_sec=30)],
        ),
        phase="base",
        min_sets=3,
        max_sets=4,
    )
    assert len(ex.sets) == 3
    assert all(s.duration_sec == 30 for s in ex.sets)


def test_apply_phase_gym_pyramid_skips_deload():
    gym = GymSession(
        category="leg",
        goal="strength",
        exercises=[_flat_exercise("Back squat", 80.0)],
    )
    out = apply_phase_gym_pyramid(gym, "deload")
    assert out.exercises[0].sets[0].weight_kg == 80.0


def test_correct_weekly_plan_applies_base_pyramid():
    from test_weekly_plan_master_align import _base_week_plan, base_targets
    from weekly_plan_master_align import correct_weekly_plan
    from weekly_plan_schema import parse_weekly_plan

    corrected, _ = correct_weekly_plan(
        "2026-07-06", _base_week_plan(), base_targets()
    )
    monday = next(d for d in corrected.days if d.weekday == "Monday")
    squat = next(ex for ex in monday.gym.exercises if "squat" in ex.name.lower())
    weights = [s.weight_kg for s in squat.sets]
    assert len(weights) >= 3
    assert weights == sorted(weights)
    assert weights[0] < weights[-1]
