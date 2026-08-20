"""Phase-based gym set pyramids: ascending (base) and reverse (build)."""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from gym_deload import round_plate_kg
from weekly_plan_schema import GymExercise, GymSession, GymSet, is_load_gym_phase

# (fraction of peak 8RM weight, target reps)
ASCENDING_PYRAMID_4: Tuple[Tuple[float, int], ...] = (
    (0.60, 12),
    (0.75, 10),
    (0.87, 8),
    (1.00, 7),
)
ASCENDING_PYRAMID_3: Tuple[Tuple[float, int], ...] = (
    (0.60, 12),
    (0.75, 10),
    (1.00, 8),
)

REVERSE_PYRAMID_4: Tuple[Tuple[float, int], ...] = (
    (1.00, 5),
    (0.85, 6),
    (0.78, 8),
    (0.70, 10),
)
REVERSE_PYRAMID_3: Tuple[Tuple[float, int], ...] = (
    (1.00, 5),
    (0.80, 7),
    (0.72, 8),
)

_BODYWEIGHT_EXERCISE_RE = re.compile(
    r"\b(pull[- ]?ups?|chin[- ]?ups?|dips?)\b",
    re.I,
)
_TIMED_HOLD_RE = re.compile(r"\b(plank|hold|dead\s*bug)\b", re.I)


def _is_bodyweight_exercise(name: str) -> bool:
    return bool(_BODYWEIGHT_EXERCISE_RE.search(name or ""))


def _is_timed_hold_exercise(name: str, sets: Sequence[GymSet]) -> bool:
    if any(s.duration_sec for s in sets):
        return True
    return bool(_TIMED_HOLD_RE.search(name or ""))


def infer_peak_weight_kg(sets: Sequence[GymSet]) -> float:
    """Peak working weight (8RM proxy) from prescribed sets."""
    weights = [
        float(s.weight_kg)
        for s in sets
        if s.weight_kg is not None and float(s.weight_kg) > 0
    ]
    if weights:
        return max(weights)
    return 40.0


def _target_set_count(
    current_count: int,
    *,
    min_sets: int,
    max_sets: int,
) -> int:
    if current_count >= max_sets:
        return max_sets
    if current_count >= min_sets:
        return current_count
    return min_sets


def _ascending_table(num_sets: int) -> Tuple[Tuple[float, int], ...]:
    if num_sets >= 4:
        return ASCENDING_PYRAMID_4
    return ASCENDING_PYRAMID_3[:num_sets]


def _reverse_table(num_sets: int) -> Tuple[Tuple[float, int], ...]:
    if num_sets >= 4:
        return REVERSE_PYRAMID_4
    return REVERSE_PYRAMID_3[:num_sets]


def _bodyweight_rep_pyramid(num_sets: int, *, reverse: bool) -> List[int]:
    ascending = [12, 10, 8, 6]
    if reverse:
        ascending = [5, 7, 8, 10]
    if num_sets >= 4:
        return ascending
    return ascending[:num_sets]


def apply_pyramid_to_exercise(
    exercise: GymExercise,
    *,
    phase: str,
    min_sets: int,
    max_sets: int,
) -> GymExercise:
    """Restructure one exercise into a phase-appropriate load pyramid."""
    sets = list(exercise.sets)
    if not sets:
        sets = [GymSet(reps=8, weight_kg=40.0, duration_sec=None)]

    num_sets = _target_set_count(len(sets), min_sets=min_sets, max_sets=max_sets)

    if _is_timed_hold_exercise(exercise.name, sets):
        duration = next((s.duration_sec for s in sets if s.duration_sec), 30)
        return GymExercise(
            name=exercise.name,
            sets=[
                GymSet(reps=1, weight_kg=None, duration_sec=duration)
                for _ in range(num_sets)
            ],
        )

    phase_norm = (phase or "").strip().lower()
    reverse = phase_norm == "build"

    if _is_bodyweight_exercise(exercise.name):
        reps_seq = _bodyweight_rep_pyramid(num_sets, reverse=reverse)
        return GymExercise(
            name=exercise.name,
            sets=[
                GymSet(reps=reps, weight_kg=0.0, duration_sec=None)
                for reps in reps_seq
            ],
        )

    peak = infer_peak_weight_kg(sets)
    table = _reverse_table(num_sets) if reverse else _ascending_table(num_sets)
    new_sets: List[GymSet] = []
    for factor, reps in table:
        load = round_plate_kg(max(2.5, peak * factor))
        new_sets.append(GymSet(reps=reps, weight_kg=load, duration_sec=None))
    return GymExercise(name=exercise.name, sets=new_sets)


def apply_phase_gym_pyramid(
    gym: GymSession,
    phase: str,
    *,
    min_sets: Optional[int] = None,
    max_sets: Optional[int] = None,
) -> GymSession:
    """Apply ascending (base) or reverse (build) pyramids to every exercise."""
    from weekly_plan_schema import gym_working_set_bounds

    if not is_load_gym_phase(phase):
        return gym
    lo, hi = gym_working_set_bounds(phase)
    min_s = min_sets if min_sets is not None else lo
    max_s = max_sets if max_sets is not None else hi
    exercises = [
        apply_pyramid_to_exercise(
            ex, phase=phase, min_sets=min_s, max_sets=max_s
        )
        for ex in gym.exercises
    ]
    return GymSession(category=gym.category, goal=gym.goal, exercises=exercises)


GYM_PYRAMID_PROTOCOL_PROMPT = """## Gym set structure (MANDATORY for base and build weeks)

Set 1 must NEVER be the heaviest working weight. Use graduated loading on every exercise.

**Base phase — ascending pyramid (hypertrophy), 3–4 sets per exercise:**
| Set | Load (% of 8RM) | Reps |
| Set 1 | ~60% | 12 |
| Set 2 | ~75% | 10 |
| Set 3 | ~87% | 8 |
| Set 4 (optional) | ~100% | 6–8 |

**Build phase — reverse pyramid (strength/power), 3–4 sets per exercise:**
| Set | Load (% of 8RM) | Reps |
| Set 1 | ~100% (heaviest) | 4–6 |
| Set 2 | ~80–85% | 6–8 |
| Set 3 | ~72–78% | 8–10 |
| Set 4 (optional) | ~70% | 10 |

Bodyweight exercises (pull-ups, dips): pyramid reps instead of load.
Timed holds (plank): equal duration each set (3–4 sets base/build).
Round all loads to nearest 2.5 kg.
Deload weeks: flat 2 sets at 80–85% of working weight only (no pyramid).
"""
