"""Structured weekly plan harness: validation hints, prose import, and finalize."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Sequence, Tuple, Union

from athlete_profile import AthleteProfile
from weekly_plan_schema import (
    WeeklyPlan,
    parse_weekly_plan,
    render_plan_text,
    validate_fixed_weekly_schedule,
    validate_weekly_plan,
    weekly_plan_to_dict,
)

MAX_STRUCTURED_ATTEMPTS = 3

SCHEDULE_RETRY_HINT = (
    "Fixed schedule (non-negotiable): Monday=gym (leg), Tuesday=erg, "
    "Wednesday=gym (upper_core), Thursday=on_water or erg, Friday/Saturday=rest "
    "unless extra volume needed, Sunday=rest or recovery. Never rest on Mon/Wed/Tue/Thu mornings."
)

STRUCTURED_JSON_SKELETON = """
Example day session_type layout (7 days):
Monday: gym (leg) | Tuesday: erg | Wednesday: gym (upper_core) |
Thursday: on_water | Friday: rest | Saturday: rest | Sunday: recovery
Each erg/on_water day: warm_up + main_set + cool_down segments (WU/CD ≤15 min).
"""


def parse_structured_plan_or_error(
    raw: str,
    *,
    include_lifting: bool,
    squad_plan: Optional[Dict[str, Any]] = None,
    phase: Optional[str] = None,
    priority: str = "hr",
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse LLM JSON output; return (plan_dict, error_message)."""
    from weekly_plan_schema import parse_weekly_plan_json

    plan = parse_weekly_plan_json(raw)
    if plan is None:
        return None, "response is not valid weekly plan JSON"
    if validate_fixed_weekly_schedule(plan, include_lifting=include_lifting):
        plan = repair_parsed_weekly_plan(
            plan,
            include_lifting=include_lifting,
            phase=phase,
            reference_plan=squad_plan,
            priority=priority,
        )
    err = validate_weekly_plan(plan, include_lifting=include_lifting)
    if err:
        return None, err
    err = validate_fixed_weekly_schedule(plan, include_lifting=include_lifting)
    if err:
        return None, err
    if squad_plan is not None:
        squad = parse_weekly_plan(squad_plan)
        if squad is None:
            return None, "invalid squad plan JSON"
        from weekly_plan_schema import validate_athlete_plan_against_squad

        athlete_err = validate_athlete_plan_against_squad(plan, squad)
        if athlete_err:
            return None, athlete_err
    from weekly_plan_schema import validate_plan_session_constraints

    err = validate_plan_session_constraints(plan, phase=phase)
    if err:
        return None, err
    return weekly_plan_to_dict(plan), None


def repair_parsed_weekly_plan(
    plan: WeeklyPlan,
    *,
    include_lifting: bool,
    phase: Optional[str] = None,
    prev_plan: Optional[WeeklyPlan] = None,
    reference_plan: Optional[Union[Dict[str, Any], WeeklyPlan]] = None,
    priority: str = "hr",
) -> WeeklyPlan:
    """Restore fixed Mon/Wed/Tue/Thu schedule before validation or alignment."""
    from weekly_plan_master_align import repair_fixed_weekly_schedule

    ref_parsed: Optional[WeeklyPlan] = None
    if reference_plan is not None:
        if isinstance(reference_plan, dict):
            ref_parsed = parse_weekly_plan(reference_plan)
        else:
            ref_parsed = reference_plan
    return repair_fixed_weekly_schedule(
        plan,
        include_lifting=include_lifting,
        phase=phase or "",
        prev_plan=prev_plan,
        reference_plan=ref_parsed,
        priority=priority,
    )


def validate_parsed_weekly_plan(
    plan: WeeklyPlan,
    *,
    include_lifting: bool,
    squad_plan_json: Optional[Dict[str, Any]] = None,
    goal_tracking: Optional[str] = None,
    phase: Optional[str] = None,
    athlete_profile: Optional[AthleteProfile] = None,
    cached_import: bool = False,
) -> Optional[str]:
    """Return first validation error, or None if the plan is acceptable."""
    err = validate_weekly_plan(plan, include_lifting=include_lifting)
    if err:
        return err
    err = validate_fixed_weekly_schedule(plan, include_lifting=include_lifting)
    if err:
        return err
    if squad_plan_json is not None:
        squad = parse_weekly_plan(squad_plan_json)
        if squad is None:
            return "invalid squad plan JSON"
        from weekly_plan_schema import validate_athlete_plan_against_squad

        athlete_err = validate_athlete_plan_against_squad(plan, squad)
        if athlete_err:
            return athlete_err
    from weekly_plan_schema import (
        is_low_intensity_plan_phase,
        validate_athlete_hr_zone_consistency,
        validate_plan_session_constraints,
        validate_squad_rowing_aligns_with_goals,
    )

    err = validate_plan_session_constraints(
        plan, phase=phase, skip_duration_caps=cached_import
    )
    if err:
        return err
    if cached_import:
        return None
    if athlete_profile is not None:
        err = validate_athlete_hr_zone_consistency(plan, athlete_profile)
        if err:
            return err
    phase_norm = (phase or "").strip().lower()
    if (
        squad_plan_json is None
        and goal_tracking
        and not is_low_intensity_plan_phase(phase)
        and phase_norm != "base"
    ):
        err = validate_squad_rowing_aligns_with_goals(
            plan, goal_tracking, phase=phase
        )
        if err:
            return err
    return None


def import_prose_plan_json(
    plan_text: str,
    *,
    week_start: str,
    personalised: bool = False,
    greeting: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Best-effort prose → JSON for alignment when structured generation fails."""
    from plan_text_import import import_weekly_plan_json_from_text

    if not plan_text.strip():
        return None
    return import_weekly_plan_json_from_text(
        plan_text.strip(),
        week_start=week_start,
        personalised=personalised,
        greeting=greeting,
    )


def finalize_imported_plan_json(
    plan_json: Dict[str, Any],
    *,
    include_lifting: bool,
    squad_plan_json: Optional[Dict[str, Any]] = None,
    goal_tracking: Optional[str] = None,
    phase: Optional[str] = None,
    priority: str = "hr",
    athlete_profile: Optional[AthleteProfile] = None,
    cached_import: bool = False,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate imported JSON; return (dict, error)."""
    parsed = parse_weekly_plan(plan_json)
    if parsed is None:
        return None, "imported plan is not valid JSON"
    if validate_fixed_weekly_schedule(parsed, include_lifting=include_lifting):
        parsed = repair_parsed_weekly_plan(
            parsed,
            include_lifting=include_lifting,
            phase=phase,
            reference_plan=squad_plan_json,
            priority=priority,
        )
    err = validate_parsed_weekly_plan(
        parsed,
        include_lifting=include_lifting,
        squad_plan_json=squad_plan_json,
        goal_tracking=goal_tracking,
        phase=phase,
        athlete_profile=athlete_profile,
        cached_import=cached_import,
    )
    if err:
        return None, err
    return weekly_plan_to_dict(parsed), None


def build_retry_feedback(parse_err: Optional[str]) -> str:
    return (
        f"\n\n--- PLAN REJECTED (regenerate) ---\n{parse_err or 'invalid JSON'}\n"
        f"{SCHEDULE_RETRY_HINT}\n"
        "Fix schema: on_water needs erg_alternative; gym 4 exercises with sets arrays; "
        "interval rest between pieces; m/km/min units; each session ≤45 min; "
        "warm_up and cool_down on every erg/on_water day."
    )


def build_validation_retry_feedback(validation_err: str) -> str:
    return (
        f"\n\n--- PLAN REJECTED (regenerate) ---\n{validation_err}\n"
        f"{SCHEDULE_RETRY_HINT}\n"
        "Fix gym layout (Mon=leg, Wed=upper_core, 3–4 sets in base/build, 2 in deload), "
        "rowing warm_up/cool_down, interval rest, on_water erg_alternative."
    )
