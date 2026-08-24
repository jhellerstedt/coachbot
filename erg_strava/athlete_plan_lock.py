"""Clone squad weekly-plan structure and overlay athlete-only numbers."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from athlete_profile import AthleteProfile
from gym_program import LiftLog
from weekly_plan_schema import (
    _SPLIT_RE,
    _split_to_seconds,
    DayPlan,
    RowingSegment,
    RowingSession,
    WeeklyPlan,
    parse_weekly_plan,
    personalize_plan_rowing_hr,
    weekly_plan_to_dict,
)


def lock_athlete_plan_to_squad(
    squad_plan_json: Mapping[str, Any],
    *,
    proposal_json: Optional[Mapping[str, Any]] = None,
    athlete_profile: Optional[AthleteProfile] = None,
    lift_logs_by_exercise: Optional[Mapping[str, Sequence[LiftLog]]] = None,
    greeting: Optional[str] = None,
    include_lifting: bool = True,
) -> Dict[str, Any]:
    """Clone squad structure; overlay athlete gym kg, HR, and optional splits."""
    from gym_program import load_program_from_plan, personalize_plan_gym_loads
    from session_library import copy_recommended_erg_from_squad

    clone: Dict[str, Any] = json.loads(json.dumps(squad_plan_json))
    clone["personalised"] = True
    if greeting is not None:
        clone["greeting"] = greeting
    if include_lifting:
        clone = personalize_plan_gym_loads(
            clone,
            squad_plan_json,
            lift_logs_by_exercise=lift_logs_by_exercise,
            program=load_program_from_plan(squad_plan_json),
        )
    parsed = parse_weekly_plan(clone)
    if parsed is None:
        raise ValueError("squad plan_json is not a valid weekly plan")
    if athlete_profile is not None:
        parsed = personalize_plan_rowing_hr(parsed, athlete_profile)
    parsed = _overlay_proposal_splits(parsed, proposal_json)
    out = weekly_plan_to_dict(parsed)
    for key in ("gym_program", "session_library"):
        if key in clone:
            out[key] = clone[key]
    return copy_recommended_erg_from_squad(
        out, squad_plan_json, profile=athlete_profile
    )


def _valid_split_pair(split_min: str, split_max: str) -> bool:
    if not _SPLIT_RE.match(split_min or "") or not _SPLIT_RE.match(split_max or ""):
        return False
    return _split_to_seconds(split_min) <= _split_to_seconds(split_max)


def _phase_queues(
    days: Sequence[DayPlan],
) -> Dict[str, Dict[str, List[RowingSegment]]]:
    queues: Dict[str, Dict[str, List[RowingSegment]]] = {}
    for day in days:
        if day.rowing is None:
            continue
        by_phase: Dict[str, List[RowingSegment]] = {}
        for seg in day.rowing.segments:
            by_phase.setdefault(seg.phase, []).append(seg)
        queues[day.weekday] = by_phase
        if day.rowing.erg_alternative is None:
            continue
        alt_key = f"{day.weekday}::alt"
        alt_phases: Dict[str, List[RowingSegment]] = {}
        for seg in day.rowing.erg_alternative.segments:
            alt_phases.setdefault(seg.phase, []).append(seg)
        queues[alt_key] = alt_phases
    return queues


def _take_proposal_segment(
    queues: Mapping[str, Mapping[str, List[RowingSegment]]],
    key: str,
    phase: str,
) -> Optional[RowingSegment]:
    bucket = queues.get(key, {}).get(phase)
    if not bucket:
        return None
    return bucket.pop(0)


def _apply_split(
    seg: RowingSegment, proposal: Optional[RowingSegment]
) -> RowingSegment:
    if proposal is None:
        return seg
    if not _valid_split_pair(proposal.split_min, proposal.split_max):
        return seg
    return replace_split(seg, proposal.split_min, proposal.split_max)


def replace_split(seg: RowingSegment, split_min: str, split_max: str) -> RowingSegment:
    from dataclasses import replace

    return replace(seg, split_min=split_min, split_max=split_max)


def _overlay_session_splits(
    rowing: RowingSession,
    weekday: str,
    queues: Mapping[str, Mapping[str, List[RowingSegment]]],
) -> RowingSession:
    from dataclasses import replace

    segments = [
        _apply_split(seg, _take_proposal_segment(queues, weekday, seg.phase))
        for seg in rowing.segments
    ]
    alt = rowing.erg_alternative
    if alt is not None:
        alt_key = f"{weekday}::alt"
        alt = replace(
            alt,
            segments=[
                _apply_split(seg, _take_proposal_segment(queues, alt_key, seg.phase))
                for seg in alt.segments
            ],
        )
    return replace(rowing, segments=segments, erg_alternative=alt)


def _overlay_proposal_splits(
    plan: WeeklyPlan,
    proposal_json: Optional[Mapping[str, Any]],
) -> WeeklyPlan:
    from dataclasses import replace

    if not proposal_json:
        return plan
    proposal = parse_weekly_plan(proposal_json)
    if proposal is None:
        return plan
    queues = _phase_queues(proposal.days)
    days: List[DayPlan] = []
    for day in plan.days:
        if day.rowing is None:
            days.append(day)
            continue
        days.append(
            replace(
                day,
                rowing=_overlay_session_splits(day.rowing, day.weekday, queues),
            )
        )
    return replace(plan, days=days)
