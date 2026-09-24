"""Jev decision layer for weekly-plan locks and gym-log verification.

Questions are Choice, Score, and Noul calls on the OpenRouter Decisions API.
The chat model still writes prose and numeric session content.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from weekly_plan_schema import (
    ErgAlternative,
    RowingSession,
    parse_weekly_plan,
    weekly_plan_to_dict,
)

CHOICE_CONFIDENCE_FLOOR = 0.6
NOUL_LOCK_THRESHOLD = 0.8
VERIFY_REJECT_THRESHOLD = 0.85

DecideFn = Callable[[Mapping[str, Any], Mapping[str, Any]], Optional[Mapping[str, Any]]]

_VOLUME_LEVELS = ("less", "same", "more")
_WEEKEND_WEEKDAY = {"friday": "Friday", "saturday": "Saturday", "sunday": "Sunday"}

PLAN_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "thursday": {
        "type": "choice",
        "instructions": "What should Thursday morning be?",
        "criteria": {
            "on_water": "Rowing on the water.",
            "erg": "Ergometer session.",
        },
    },
    "friday": {
        "type": "choice",
        "instructions": "What should Friday be?",
        "criteria": {
            "rest": "Rest day.",
            "recovery": "Easy recovery only.",
            "extra_volume": "An extra erg session because Tuesday and Thursday cannot hold the volume.",
        },
    },
    "saturday": {
        "type": "choice",
        "instructions": "What should Saturday be?",
        "criteria": {
            "rest": "Rest day.",
            "recovery": "Easy recovery only.",
            "extra_volume": "An extra erg session because Tuesday and Thursday cannot hold the volume.",
        },
    },
    "sunday": {
        "type": "choice",
        "instructions": "What should Sunday be?",
        "criteria": {
            "rest": "Rest day.",
            "recovery": "Easy recovery only.",
            "extra_volume": "An extra erg session because Tuesday and Thursday cannot hold the volume.",
        },
    },
    "deload": {
        "type": "noul",
        "instructions": "This week should be a deload.",
        "criteria": {
            "true": "Fatigue, missed work, or the phase calls for less load.",
            "false": "A normal training week is appropriate.",
        },
    },
    "plan_change": {
        "type": "noul",
        "instructions": "The athlete asked to change a future weekly plan.",
        "criteria": {
            "true": "They want a future session, volume, or intensity changed.",
            "false": "They are logging, asking, or chatting, not changing the plan.",
        },
    },
    "volume": {
        "type": "score",
        "instructions": "How should this week's rowing volume compare with last week?",
        "criteria": ["less", "same", "more"],
    },
}


@dataclass(frozen=True)
class PlanLocks:
    thursday: Optional[str] = None
    friday: Optional[str] = None
    saturday: Optional[str] = None
    sunday: Optional[str] = None
    deload: Optional[bool] = None
    plan_change: Optional[bool] = None
    volume: Optional[str] = None

    def any(self) -> bool:
        return any(
            value is not None
            for value in (
                self.thursday,
                self.friday,
                self.saturday,
                self.sunday,
                self.deload,
                self.plan_change,
                self.volume,
            )
        )


def _decide(
    state: Mapping[str, Any],
    questions: Mapping[str, Any],
    api_key: str,
    decide: Optional[DecideFn],
) -> Optional[Mapping[str, Any]]:
    if not questions:
        return None
    if decide is not None:
        return decide(state, questions)
    if not (api_key or "").strip():
        return None
    from openrouter_client import call_openrouter_decisions

    return call_openrouter_decisions(
        state=dict(state),
        questions=dict(questions),
        api_key=api_key,
    )


def _confident_choice(
    answers: Mapping[str, Any], key: str, allowed: Tuple[str, ...]
) -> Optional[str]:
    raw = answers.get(key)
    if not isinstance(raw, Mapping):
        return None
    choice = raw.get("choice")
    if choice not in allowed:
        return None
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        return None
    if confidence < CHOICE_CONFIDENCE_FLOOR:
        return None
    return str(choice)


def _noul_flag(answers: Mapping[str, Any], key: str) -> Optional[bool]:
    raw = answers.get(key)
    if not isinstance(raw, Mapping):
        return None
    try:
        probability = float(raw.get("noul"))
    except (TypeError, ValueError):
        return None
    if probability >= NOUL_LOCK_THRESHOLD:
        return True
    if probability <= 1.0 - NOUL_LOCK_THRESHOLD:
        return False
    return None


def _volume_level(answers: Mapping[str, Any]) -> Optional[str]:
    raw = answers.get("volume")
    if not isinstance(raw, Mapping):
        return None
    try:
        confidence = float(raw.get("confidence"))
        score = float(raw.get("score"))
    except (TypeError, ValueError):
        return None
    if confidence < CHOICE_CONFIDENCE_FLOOR:
        return None
    index = int(round(score))
    if index < 0 or index >= len(_VOLUME_LEVELS):
        return None
    return _VOLUME_LEVELS[index]


def _weekend_session(choice: Optional[str]) -> Optional[str]:
    if choice == "extra_volume":
        return "erg"
    if choice in ("rest", "recovery"):
        return choice
    return None


def locks_from_answers(answers: Mapping[str, Any]) -> PlanLocks:
    return PlanLocks(
        thursday=_confident_choice(answers, "thursday", ("on_water", "erg")),
        friday=_weekend_session(
            _confident_choice(answers, "friday", ("rest", "recovery", "extra_volume"))
        ),
        saturday=_weekend_session(
            _confident_choice(
                answers, "saturday", ("rest", "recovery", "extra_volume")
            )
        ),
        sunday=_weekend_session(
            _confident_choice(answers, "sunday", ("rest", "recovery", "extra_volume"))
        ),
        deload=_noul_flag(answers, "deload"),
        plan_change=_noul_flag(answers, "plan_change"),
        volume=_volume_level(answers),
    )


def predecide_weekly_plan(
    state: Mapping[str, Any],
    api_key: str,
    *,
    decide: Optional[DecideFn] = None,
) -> Optional[PlanLocks]:
    """One fan-out over the plan context. None when Jev is unavailable."""
    context = dict(state)
    text = context.get("context")
    if isinstance(text, str) and len(text) > 24000:
        context["context"] = text[:24000]
    answers = _decide(context, PLAN_QUESTIONS, api_key, decide)
    if answers is None:
        return None
    return locks_from_answers(answers)


def format_plan_lock_prompt(locks: Optional[PlanLocks]) -> str:
    if locks is None or not locks.any():
        return ""
    lines = [
        "LOCKED DECISIONS (non-negotiable; do not invent different session types):"
    ]
    if locks.thursday:
        lines.append(f"- Thursday session_type = {locks.thursday}")
    for key, weekday in _WEEKEND_WEEKDAY.items():
        value = getattr(locks, key)
        if not value:
            continue
        label = "extra erg volume" if value == "erg" else value
        lines.append(f"- {weekday} session_type = {value} ({label})")
    if locks.deload is True:
        lines.append("- This week is a deload: reduce load and volume.")
    elif locks.deload is False:
        lines.append("- This week is not a deload.")
    if locks.volume:
        lines.append(f"- Rowing volume versus last week must be {locks.volume}.")
    if locks.plan_change:
        lines.append("- Apply the athlete's requested plan change.")
    return "\n" + "\n".join(lines) + "\n"


def _lock_rowing_day(day, session_type: str):
    rowing = day.rowing
    if rowing is None:
        return None
    if session_type == "on_water" and rowing.erg_alternative is None:
        rowing = RowingSession(
            segments=list(rowing.segments),
            erg_alternative=ErgAlternative(
                description="Group erg fallback",
                segments=list(rowing.segments),
            ),
        )
    return replace(day, session_type=session_type, gym=None, rowing=rowing)


def apply_plan_locks(
    plan_dict: Dict[str, Any], locks: PlanLocks
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Write locked session types onto a parsed plan.

    Returns the original dict plus an error when a lock needs a rowing session
    the plan does not have yet. The caller retries generation with that error.
    """
    if not locks.any():
        return plan_dict, None
    plan = parse_weekly_plan(plan_dict)
    if plan is None:
        return plan_dict, "locked decisions could not be applied to invalid JSON"
    errors = []
    days = []
    for day in plan.days:
        target = None
        if day.weekday == "Thursday":
            target = locks.thursday
        elif day.weekday == "Friday":
            target = locks.friday
        elif day.weekday == "Saturday":
            target = locks.saturday
        elif day.weekday == "Sunday":
            target = locks.sunday
        if target is None or day.session_type == target:
            days.append(day)
            continue
        if target in ("rest", "recovery"):
            days.append(
                replace(
                    day,
                    session_type=target,
                    session_subtype=None,
                    gym=None,
                    rowing=None,
                )
            )
            continue
        locked = _lock_rowing_day(day, target)
        if locked is None:
            label = "extra erg volume" if target == "erg" else target
            errors.append(
                f"{day.weekday} is locked to {label} but has no rowing session"
            )
            days.append(day)
            continue
        days.append(locked)
    if errors:
        return plan_dict, "; ".join(errors)
    return weekly_plan_to_dict(plan.with_days(days)), None


def _violation_questions(
    locks: Optional[PlanLocks],
    *,
    personalised: bool,
    greeting: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    questions: Dict[str, Dict[str, Any]] = {}
    if locks is not None and locks.thursday:
        questions["thursday_mismatch"] = {
            "type": "noul",
            "instructions": (
                "Thursday's prescription disagrees with the locked "
                f"session_type {locks.thursday}."
            ),
            "criteria": {
                "true": "Thursday is a different modality than the lock.",
                "false": "Thursday matches the locked modality.",
            },
        }
    if locks is not None and locks.volume:
        questions["volume_mismatch"] = {
            "type": "noul",
            "instructions": (
                "Prescribed rowing volume change disagrees with the locked "
                f"volume {locks.volume} versus last week."
            ),
            "criteria": {
                "true": "The plan's volume change contradicts the lock.",
                "false": "The plan's volume change matches the lock.",
            },
        }
    name = (greeting or "").split(",")[0].strip()
    if personalised and name:
        questions["greeting_mismatch"] = {
            "type": "noul",
            "instructions": (
                f"The greeting does not include the athlete's first name {name}."
            ),
            "criteria": {
                "true": "The greeting names someone else or names nobody.",
                "false": f"The greeting includes {name}.",
            },
        }
    return questions


def _noul_above(answers: Mapping[str, Any], key: str, threshold: float) -> bool:
    raw = answers.get(key)
    if not isinstance(raw, Mapping):
        return False
    try:
        return float(raw.get("noul")) > threshold
    except (TypeError, ValueError):
        return False


def verify_weekly_plan(
    plan_dict: Mapping[str, Any],
    locks: Optional[PlanLocks],
    api_key: str,
    *,
    personalised: bool = False,
    greeting: Optional[str] = None,
    decide: Optional[DecideFn] = None,
) -> Optional[str]:
    """Return a retry hint when a violation Noul is above 0.85.

    Missing Jev or a probability at or below the threshold does not block.
    """
    questions = _violation_questions(
        locks, personalised=personalised, greeting=greeting
    )
    if not questions:
        return None
    answers = _decide(
        {"plan": dict(plan_dict), "locks": _lock_state(locks)},
        questions,
        api_key,
        decide,
    )
    if not answers:
        return None
    violations = [
        str(questions[key]["instructions"])
        for key in questions
        if _noul_above(answers, key, VERIFY_REJECT_THRESHOLD)
    ]
    if not violations:
        return None
    return "; ".join(violations)


def _lock_state(locks: Optional[PlanLocks]) -> Dict[str, Any]:
    if locks is None:
        return {}
    return {
        "thursday": locks.thursday,
        "friday": locks.friday,
        "saturday": locks.saturday,
        "sunday": locks.sunday,
        "deload": locks.deload,
        "plan_change": locks.plan_change,
        "volume": locks.volume,
    }


GYM_VERIFY_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "invented_weight": {
        "type": "noul",
        "instructions": "A weight or rep count in the parsed log was invented and does not appear in the transcript.",
        "criteria": {
            "true": "The parsed JSON contains a load or rep count the transcript does not support.",
            "false": "Every parsed weight and rep count appears in the transcript.",
        },
    },
    "missing_set": {
        "type": "noul",
        "instructions": "A set from the transcript is missing from the parsed log.",
        "criteria": {
            "true": "The transcript lists a set the parsed JSON dropped.",
            "false": "Every transcript set is present in the parsed JSON.",
        },
    },
}


def gym_harness_violation(
    transcript: str,
    parsed: Mapping[str, Any],
    api_key: str,
    *,
    decide: Optional[DecideFn] = None,
) -> Optional[str]:
    """Reject a gym JSON extraction when a violation Noul is above 0.85."""
    answers = _decide(
        {"transcript": transcript, "parsed": dict(parsed)},
        GYM_VERIFY_QUESTIONS,
        api_key,
        decide,
    )
    if not answers:
        return None
    violations = [
        str(GYM_VERIFY_QUESTIONS[key]["instructions"])
        for key in GYM_VERIFY_QUESTIONS
        if _noul_above(answers, key, VERIFY_REJECT_THRESHOLD)
    ]
    if not violations:
        return None
    return "; ".join(violations)
