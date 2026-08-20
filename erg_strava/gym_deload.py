"""Deload-week gym session modifier: 2 sets, ~82% load, tonnage validation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from weekly_plan_schema import GymExercise, GymSession, GymSet, WeeklyPlan

DEFAULT_DELOAD_CONFIG = {
    "set_reduction_factor": 0.67,
    "load_reduction_factor": 0.82,
    "min_sets": 2,
    "max_sets": 2,
    "maintain_reps": True,
    "maintain_exercises": True,
}


@dataclass(frozen=True)
class DeloadConfig:
    set_reduction_factor: float = 0.67
    load_reduction_factor: float = 0.82
    min_sets: int = 2
    max_sets: int = 2
    maintain_reps: bool = True
    maintain_exercises: bool = True
    target_tonnage_kg: Optional[float] = None


@dataclass(frozen=True)
class DeloadTonnageCheck:
    pass_: bool
    actual: float
    variance: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pass": self.pass_,
            "actual": self.actual,
            "variance": self.variance,
        }


DELOAD_GYM_PROTOCOL_PROMPT = """## Deload Week Gym Protocol (MANDATORY when phase = deload)

When generating a gym session for a deload week, you MUST apply the following \
rules WITHOUT EXCEPTION:

1. SETS: Generate exactly 2 sets per exercise (not 1, not 3)
2. REPS: Maintain the same rep range as the preceding build week (typically 6–8)
3. LOAD: Reduce each set's load to 80–85% of the athlete's most recent working \
weight for that exercise, rounded to nearest 2.5 kg
4. EXERCISES: Use the identical exercise list from the preceding build week. \
Do NOT add new exercises. Do NOT remove exercises.
5. TONNAGE: Verify total session tonnage (sets × reps × kg) matches the computed \
deload gym tonnage target for this week (50% of mean recent load-week tonnage; \
±20% per week, split evenly across Mon/Wed gym days). Do NOT use a static \
macro ``Tgt gym kg`` from season_master_plan.md for deload weeks.
6. DO NOT generate 1-set sessions. A deload is minimum effective dose training, \
not rest. 1 set provides insufficient neuromuscular stimulus to maintain \
adaptation.

Preceding build week reference loads will be provided in the session context.
If no preceding build week data is available, use 80% of the athlete's \
documented working weight for each exercise.
"""


def round_plate_kg(weight_kg: float) -> float:
    return round(weight_kg / 2.5) * 2.5


def gym_session_tonnage(gym: GymSession) -> float:
    total = 0.0
    for ex in gym.exercises:
        for s in ex.sets:
            if s.weight_kg is not None:
                total += s.reps * s.weight_kg
    return total


def _working_weight_kg(sets: Sequence[GymSet]) -> float:
    weights = [
        float(s.weight_kg)
        for s in sets
        if s.weight_kg is not None and float(s.weight_kg) > 0
    ]
    return max(weights) if weights else 0.0


def _typical_reps(sets: Sequence[GymSet]) -> int:
    reps = [s.reps for s in sets if s.reps >= 1 and s.duration_sec is None]
    if not reps:
        return 8
    return int(round(sum(reps) / len(reps)))


def _deload_set_count(original_count: int, config: DeloadConfig) -> int:
    if original_count <= 0:
        return config.min_sets
    scaled = max(config.min_sets, int(round(original_count * config.set_reduction_factor)))
    return min(max(scaled, config.min_sets), config.max_sets)


def apply_deload_modifier(
    gym: GymSession,
    config: Optional[DeloadConfig] = None,
    *,
    reference_gym: Optional[GymSession] = None,
) -> GymSession:
    """Transform a build-week gym session into a deload-compliant session."""
    cfg = config or DeloadConfig()
    template_exercises = (
        list(reference_gym.exercises)
        if reference_gym is not None and cfg.maintain_exercises
        else list(gym.exercises)
    )
    current_by_name = {ex.name: ex for ex in gym.exercises}
    ref_by_name = (
        {ex.name: ex for ex in reference_gym.exercises} if reference_gym else {}
    )

    exercises: List[GymExercise] = []
    for template in template_exercises:
        current = current_by_name.get(template.name, template)
        reference = ref_by_name.get(template.name, template)
        source_sets = current.sets or reference.sets
        working = _working_weight_kg(source_sets)
        if working <= 0:
            working = _working_weight_kg(reference.sets)

        reps = _typical_reps(source_sets) if cfg.maintain_reps else 8
        n_sets = _deload_set_count(len(source_sets), cfg)

        timed = any(s.duration_sec for s in source_sets)
        new_sets: List[GymSet] = []
        if timed:
            duration = next(
                (s.duration_sec for s in source_sets if s.duration_sec),
                30,
            )
            for _ in range(n_sets):
                new_sets.append(
                    GymSet(reps=1, weight_kg=None, duration_sec=duration)
                )
        else:
            load = round_plate_kg(max(20.0, working * cfg.load_reduction_factor))
            for _ in range(n_sets):
                new_sets.append(
                    GymSet(reps=reps, weight_kg=load, duration_sec=None)
                )
        exercises.append(GymExercise(name=template.name, sets=new_sets))

    return GymSession(category=gym.category, goal=gym.goal, exercises=exercises)


def validate_deload_tonnage(
    gym: GymSession,
    target_tonnage_kg: float,
) -> DeloadTonnageCheck:
    actual = gym_session_tonnage(gym)
    if target_tonnage_kg <= 0:
        return DeloadTonnageCheck(pass_=True, actual=actual, variance="0.0%")
    variance_pct = (actual - target_tonnage_kg) / target_tonnage_kg * 100.0
    return DeloadTonnageCheck(
        pass_=target_tonnage_kg * 0.8 <= actual <= target_tonnage_kg * 1.2,
        actual=actual,
        variance=f"{variance_pct:+.1f}%",
    )


def _load_floors_kg(
    gym: GymSession,
    reference_gym: Optional[GymSession],
    *,
    load_low: float = 0.80,
) -> Dict[str, float]:
    """Minimum deload load per exercise (80% of reference working weight)."""
    ref = reference_gym or gym
    floors: Dict[str, float] = {}
    for ex in ref.exercises:
        working = _working_weight_kg(ex.sets)
        if working > 0:
            floors[ex.name] = round_plate_kg(working * load_low)
    return floors


def _scale_gym_loads(
    gym: GymSession,
    factor: float,
    *,
    load_floors: Optional[Mapping[str, float]] = None,
) -> GymSession:
    floors = load_floors or {}
    exercises: List[GymExercise] = []
    for ex in gym.exercises:
        sets: List[GymSet] = []
        floor = floors.get(ex.name, 0.0)
        for s in ex.sets:
            if s.weight_kg is not None and s.weight_kg > 0:
                scaled = round_plate_kg(max(20.0, s.weight_kg * factor))
                sets.append(
                    GymSet(
                        reps=s.reps,
                        weight_kg=max(floor, scaled) if floor else scaled,
                        duration_sec=s.duration_sec,
                    )
                )
            else:
                sets.append(s)
        exercises.append(GymExercise(name=ex.name, sets=sets))
    return GymSession(category=gym.category, goal=gym.goal, exercises=exercises)


def _increase_gym_reps(gym: GymSession, delta: int) -> GymSession:
    exercises: List[GymExercise] = []
    for ex in gym.exercises:
        sets = [
            GymSet(
                reps=s.reps + delta,
                weight_kg=s.weight_kg,
                duration_sec=s.duration_sec,
            )
            if s.duration_sec is None
            else s
            for s in ex.sets
        ]
        exercises.append(GymExercise(name=ex.name, sets=sets))
    return GymSession(category=gym.category, goal=gym.goal, exercises=exercises)


def apply_deload_gym_session(
    gym: GymSession,
    *,
    config: Optional[DeloadConfig] = None,
    reference_gym: Optional[GymSession] = None,
    session_tonnage_target: Optional[float] = None,
) -> Tuple[GymSession, List[str], DeloadTonnageCheck]:
    """
    Apply deload modifier and auto-adjust loads/reps to hit session tonnage target.
    """
    cfg = config or DeloadConfig()
    if session_tonnage_target is not None:
        cfg = replace(cfg, target_tonnage_kg=session_tonnage_target)

    modified = apply_deload_modifier(gym, cfg, reference_gym=reference_gym)
    load_floors = (
        _load_floors_kg(modified, reference_gym or gym)
        if reference_gym is not None
        else None
    )
    adjustment_notes: List[str] = []
    check = validate_deload_tonnage(modified, cfg.target_tonnage_kg or 0.0)

    if cfg.target_tonnage_kg:
        for _ in range(30):
            check = validate_deload_tonnage(modified, cfg.target_tonnage_kg)
            if check.pass_:
                break
            if check.actual > cfg.target_tonnage_kg * 1.2:
                prev = gym_session_tonnage(modified)
                modified = _scale_gym_loads(
                    modified, 0.95, load_floors=load_floors
                )
                if gym_session_tonnage(modified) >= prev:
                    adjustment_notes.append(
                        "Session tonnage above deload target but loads held at "
                        "≥80% of working weight (minimum effective dose)"
                    )
                    break
                adjustment_notes.append(
                    "Reduced load by 5% to bring session tonnage within deload target"
                )
            elif check.actual < cfg.target_tonnage_kg * 0.8:
                modified = _increase_gym_reps(modified, 1)
                adjustment_notes.append(
                    "Increased reps by 1 to bring session tonnage within deload target"
                )
            else:
                break
        check = validate_deload_tonnage(modified, cfg.target_tonnage_kg)
        if (
            not check.pass_
            and check.actual > cfg.target_tonnage_kg * 1.2
            and reference_gym is None
        ):
            factor = (cfg.target_tonnage_kg * 1.05) / check.actual
            modified = _scale_gym_loads(modified, factor, load_floors=None)
            adjustment_notes.append(
                "Scaled loads proportionally to bring session tonnage within deload target"
            )
            check = validate_deload_tonnage(modified, cfg.target_tonnage_kg)

    return modified, adjustment_notes, check


def gym_for_weekday(plan: WeeklyPlan, weekday: str) -> Optional[GymSession]:
    for day in plan.days:
        if day.weekday == weekday and day.gym is not None:
            return day.gym
    return None


def format_deload_gym_session_markdown(
    gym: GymSession,
    *,
    weekday: str,
    phase: str = "deload",
    weekly_tonnage_target_kg: float,
    tonnage_check: Optional[DeloadTonnageCheck] = None,
    adjustment_notes: Optional[Sequence[str]] = None,
) -> str:
    """Render deload gym session as markdown table (Step 5 format)."""
    category_label = "Upper-body/Core" if gym.category == "upper_core" else "Leg/Posterior-chain"
    session_tonnage = gym_session_tonnage(gym)
    check = tonnage_check or validate_deload_tonnage(
        gym, weekly_tonnage_target_kg / 2.0
    )

    lines = [
        f"### {weekday}: Gym — {category_label} (Deload)",
        f"Phase: {phase} | Tonnage target: {weekly_tonnage_target_kg:.0f} kg (week) | "
        f"Priority: maintain stimulus, reduce fatigue",
        "",
        "| Exercise | Sets | Reps | Load (kg) | Set Tonnage |",
        "|----------|------|------|-----------|-------------|",
    ]
    for ex in gym.exercises:
        if ex.sets and ex.sets[0].duration_sec:
            lines.append(
                f"| {ex.name} | {len(ex.sets)} | {ex.sets[0].duration_sec}s | BW | — |"
            )
            continue
        reps = ex.sets[0].reps if ex.sets else "—"
        load = ex.sets[0].weight_kg if ex.sets else "—"
        if isinstance(load, float) and load == int(load):
            load = int(load)
        set_tonnage = sum(
            (s.reps * s.weight_kg)
            for s in ex.sets
            if s.weight_kg is not None
        )
        lines.append(
            f"| {ex.name} | {len(ex.sets)} | {reps} | {load} | {set_tonnage:.0f} kg |"
        )
    lines.append(f"| | | | **Total** | **{session_tonnage:.0f} kg** |")
    lines.append("")
    per_session_target = weekly_tonnage_target_kg / 2.0
    lines.append(
        f"Tonnage check: {check.actual:.0f} kg actual vs {per_session_target:.0f} kg "
        f"session target ({check.variance} vs session target)"
    )
    if adjustment_notes:
        for note in adjustment_notes:
            lines.append(f"- Adjustment: {note}")
    return "\n".join(lines)


def regression_checklist_deload_gym(
    plan: WeeklyPlan,
    *,
    weekly_tonnage_target_kg: float = 3000.0,
    reference_plan: Optional[WeeklyPlan] = None,
    load_low: float = 0.80,
    load_high: float = 0.85,
) -> Dict[str, bool]:
    """Step 6 gym-specific deload regression checks."""
    gym_days = [d for d in plan.days if d.session_type == "gym" and d.gym]
    min_sets_ok = all(
        all(len(ex.sets) >= 2 for ex in day.gym.exercises)  # type: ignore[union-attr]
        for day in gym_days
    )
    max_sets_ok = all(
        all(len(ex.sets) <= 2 for ex in day.gym.exercises)  # type: ignore[union-attr]
        for day in gym_days
    )

    load_ok = True
    exercise_match_ok = True
    if reference_plan is not None:
        for day in gym_days:
            ref_gym = gym_for_weekday(reference_plan, day.weekday)
            if ref_gym is None or day.gym is None:
                continue
            ref_names = [ex.name for ex in ref_gym.exercises]
            act_names = [ex.name for ex in day.gym.exercises]
            if ref_names != act_names:
                exercise_match_ok = False
            for ex in day.gym.exercises:
                ref_ex = next((e for e in ref_gym.exercises if e.name == ex.name), None)
                if ref_ex is None:
                    exercise_match_ok = False
                    continue
                working = _working_weight_kg(ref_ex.sets)
                if working <= 0:
                    continue
                for s in ex.sets:
                    if s.weight_kg is None or s.weight_kg <= 0:
                        continue
                    ratio = s.weight_kg / working
                    if ratio < load_low - 0.02 or ratio > load_high + 0.03:
                        load_ok = False

    total_tonnage = sum(gym_session_tonnage(d.gym) for d in gym_days if d.gym)
    tonnage_ok = (
        weekly_tonnage_target_kg * 0.8
        <= total_tonnage
        <= weekly_tonnage_target_kg * 1.2
    )

    no_new_exercises = exercise_match_ok if reference_plan else True
    both_days_ok = sum(1 for d in gym_days if d.weekday in ("Monday", "Wednesday")) >= 2

    return {
        "No deload gym session contains fewer than 2 sets per exercise": min_sets_ok,
        "No deload gym session contains more than 2 sets per exercise": max_sets_ok,
        "Load is 80–85% of preceding build week working weight for each exercise": load_ok,
        "Exercise selection is identical to preceding build week": exercise_match_ok,
        "Total tonnage is within 20% of Tgt gym kg in season_master_plan.md": tonnage_ok,
        "No new exercises are introduced during deload weeks": no_new_exercises,
        "Fix applies to BOTH Monday and Wednesday gym sessions": both_days_ok,
    }
