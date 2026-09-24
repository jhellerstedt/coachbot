"""Tests for Jev plan locks and verification thresholds."""

from __future__ import annotations

import json

from generate_training_plan import _generate_structured_plan_with_fallback
from jev_harness import (
    VERIFY_REJECT_THRESHOLD,
    PlanLocks,
    apply_plan_locks,
    format_plan_lock_prompt,
    gym_harness_violation,
    locks_from_answers,
    predecide_weekly_plan,
    verify_weekly_plan,
)
from test_weekly_plan_master_align import _base_week_plan


def _answers(**overrides):
    base = {
        "thursday": {"type": "choice", "choice": "on_water", "confidence": 0.9},
        "friday": {"type": "choice", "choice": "rest", "confidence": 0.9},
        "saturday": {"type": "choice", "choice": "extra_volume", "confidence": 0.4},
        "sunday": {"type": "choice", "choice": "recovery", "confidence": 0.8},
        "deload": {"type": "noul", "noul": 0.91},
        "plan_change": {"type": "noul", "noul": 0.4},
        "volume": {"type": "score", "score": 0.1, "confidence": 0.95},
    }
    base.update(overrides)
    return base


def test_locks_drop_low_confidence_and_map_extra_volume():
    locks = locks_from_answers(_answers())
    assert locks.thursday == "on_water"
    assert locks.friday == "rest"
    assert locks.saturday is None
    assert locks.sunday == "recovery"
    assert locks.deload is True
    assert locks.plan_change is None
    assert locks.volume == "less"


def test_predecide_returns_none_when_jev_unavailable():
    assert (
        predecide_weekly_plan({"context": "week"}, "token", decide=lambda *_: None)
        is None
    )


def test_lock_prompt_states_session_types():
    text = format_plan_lock_prompt(
        PlanLocks(thursday="on_water", friday="erg", deload=True, volume="less")
    )
    assert "Thursday session_type = on_water" in text
    assert "Friday session_type = erg (extra erg volume)" in text
    assert "deload" in text
    assert "must be less" in text


def test_apply_thursday_on_water_clones_erg_alternative():
    plan = _base_week_plan()
    locked, err = apply_plan_locks(plan, PlanLocks(thursday="on_water"))
    assert err is None
    thursday = next(day for day in locked["days"] if day["weekday"] == "Thursday")
    assert thursday["session_type"] == "on_water"
    assert thursday["rowing"]["erg_alternative"]["segments"]


def test_apply_weekend_without_rowing_is_a_retry_error():
    plan = _base_week_plan()
    locked, err = apply_plan_locks(plan, PlanLocks(saturday="erg"))
    assert err is not None
    assert "Saturday" in err
    assert locked == plan


def test_apply_sunday_recovery():
    plan = _base_week_plan()
    locked, err = apply_plan_locks(plan, PlanLocks(sunday="recovery"))
    assert err is None
    sunday = next(day for day in locked["days"] if day["weekday"] == "Sunday")
    assert sunday["session_type"] == "recovery"
    assert sunday["rowing"] is None


def test_verify_blocks_only_above_threshold():
    plan = _base_week_plan()
    locks = PlanLocks(thursday="erg")

    def below(_state, _questions):
        return {"thursday_mismatch": {"noul": VERIFY_REJECT_THRESHOLD}}

    def above(_state, questions):
        assert "thursday_mismatch" in questions
        return {"thursday_mismatch": {"noul": VERIFY_REJECT_THRESHOLD + 0.01}}

    assert (
        verify_weekly_plan(plan, locks, "token", decide=below) is None
    )
    hint = verify_weekly_plan(plan, locks, "token", decide=above)
    assert hint is not None
    assert "Thursday" in hint


def test_verify_unavailable_does_not_block():
    assert (
        verify_weekly_plan(
            _base_week_plan(),
            PlanLocks(volume="more"),
            "token",
            decide=lambda *_: None,
        )
        is None
    )


def test_gym_violation_threshold():
    parsed = {"exercises": []}

    def high(_state, questions):
        assert "invented_weight" in questions
        assert "missing_set" in questions
        return {
            "invented_weight": {"noul": 0.9},
            "missing_set": {"noul": 0.1},
        }

    def low(_state, _questions):
        return {
            "invented_weight": {"noul": 0.85},
            "missing_set": {"noul": 0.85},
        }

    assert gym_harness_violation("8r 40", parsed, "token", decide=high)
    assert gym_harness_violation("8r 40", parsed, "token", decide=low) is None


def test_structured_plan_prompt_includes_locks_and_retries_violations(monkeypatch):
    plan = _base_week_plan()
    captured = {"systems": [], "users": []}
    checks = {"n": 0}

    def fake_llm(system, user, api_key, **kwargs):
        captured["systems"].append(system)
        captured["users"].append(user)
        return json.dumps(plan)

    def fake_verify(*_args, **_kwargs):
        checks["n"] += 1
        if checks["n"] == 1:
            return "Thursday's prescription disagrees with the locked session_type erg."
        return None

    monkeypatch.setattr("generate_training_plan._call_llm", fake_llm)
    monkeypatch.setattr(
        "generate_training_plan.ensure_realistic_interval_sessions",
        lambda plan_json, api_key, **kwargs: plan_json,
    )
    monkeypatch.setattr(
        "jev_harness.predecide_weekly_plan",
        lambda *args, **kwargs: PlanLocks(thursday="erg", volume="less"),
    )
    monkeypatch.setattr("jev_harness.verify_weekly_plan", fake_verify)

    result = _generate_structured_plan_with_fallback(
        "system",
        "user context",
        "token",
        prose_fallback=lambda: "nope",
    )

    assert result.plan_json is not None
    assert "Thursday session_type = erg" in captured["systems"][0]
    assert "must be less" in captured["systems"][0]
    assert checks["n"] == 2
    assert "disagrees" in captured["users"][1]
