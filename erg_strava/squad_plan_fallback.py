"""Deterministic squad weekly plan built from approved session libraries."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

from gym_program import apply_program_gym_to_plan, next_gym_week_index
from session_library import (
    ErgSessionTemplate,
    apply_library_sessions_to_plan,
    select_sessions_for_week,
)

if TYPE_CHECKING:
    from generate_training_plan import WeekBounds


def build_library_squad_plan_json(
    *,
    plan_week: "WeekBounds",
    phase: str,
    include_lifting: bool,
    peak_kg_by_exercise: Mapping[str, float],
    prev_plan_json: Optional[Mapping[str, Any]],
    session_selections: Optional[Mapping[str, ErgSessionTemplate]] = None,
) -> Dict[str, Any]:
    """Build a valid squad plan without an LLM response."""
    weekdays = (
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    )
    session_types = {
        "Tuesday": "erg",
        "Thursday": "on_water",
    }
    if include_lifting:
        session_types.update({"Monday": "gym", "Wednesday": "gym"})
    days = []
    for offset, weekday in enumerate(weekdays):
        session_type = session_types.get(weekday, "rest")
        rowing = (
            {"segments": [], "erg_alternative": None}
            if session_type in ("erg", "on_water")
            else None
        )
        days.append(
            {
                "weekday": weekday,
                "date": (plan_week.week_start + timedelta(days=offset)).isoformat(),
                "session_type": session_type,
                "session_subtype": None,
                "gym": None,
                "rowing": rowing,
                "notes": None,
            }
        )

    skeleton: Dict[str, Any] = {
        "version": 1,
        "personalised": False,
        "greeting": None,
        "days": days,
    }
    selections = session_selections or select_sessions_for_week(phase=phase)
    patched = apply_library_sessions_to_plan(skeleton, selections)

    for key, weekday in (("tuesday", "Tuesday"), ("thursday", "Thursday")):
        day = next(item for item in patched["days"] if item["weekday"] == weekday)
        template = selections.get(key)
        if template is not None and not day["rowing"]["segments"]:
            day["rowing"] = copy.deepcopy(template.rowing)
            day["session_subtype"] = template.session_subtype

    thursday = next(
        item for item in patched["days"] if item["weekday"] == "Thursday"
    )
    if thursday["rowing"]["erg_alternative"] is None:
        thursday["rowing"]["erg_alternative"] = {
            "description": "Complete the same session on the erg.",
            "segments": copy.deepcopy(thursday["rowing"]["segments"]),
        }

    if include_lifting:
        patched = apply_program_gym_to_plan(
            patched,
            phase=phase,
            week_index=next_gym_week_index(prev_plan_json),
            peak_kg_by_exercise=peak_kg_by_exercise,
            prev_plan_json=prev_plan_json,
        )
    return patched
