"""Tests for gym session LLM JSON harness and Suunto note parsing."""

from __future__ import annotations

import json

from weekly_plan_schema import (
    GYM_SESSION_HARNESS_INSTRUCTIONS,
    canonical_logged_gym_exercise_name as canonical_exercise_name,
    openrouter_gym_session_response_format,
    parse_gym_session_harness_json,
)
from gym_suunto import parse_coach_gym_transcript, parse_suunto_gym_description
from generate_training_plan import (
    finalize_gym_session_metrics,
    gym_session_metrics_from_harness,
    parse_gym_session_metrics,
    parse_gym_session_with_llm_harness,
    parse_prescribed_gym_session,
)

_JACK_MONDAY_LOG = """gym this morning:
back squat
8r 70, 90, 6r 100, 8r 70
romanian deadlift
8r 60, 80, 4r 90
bulgarian
8r 30, 40, 6r 45
plank
75s, 75s"""

_JACK_MONDAY_HARNESS_JSON = {
    "unit": "kg",
    "exercises": [
        {
            "name": "Back squat",
            "sets": [
                {"reps": 8, "weight_kg": 70, "duration_sec": None},
                {"reps": 8, "weight_kg": 90, "duration_sec": None},
                {"reps": 6, "weight_kg": 100, "duration_sec": None},
                {"reps": 8, "weight_kg": 70, "duration_sec": None},
            ],
        },
        {
            "name": "Romanian deadlift",
            "sets": [
                {"reps": 8, "weight_kg": 60, "duration_sec": None},
                {"reps": 8, "weight_kg": 80, "duration_sec": None},
                {"reps": 4, "weight_kg": 90, "duration_sec": None},
            ],
        },
        {
            "name": "Bulgarian split squat",
            "sets": [
                {"reps": 8, "weight_kg": 30, "duration_sec": None},
                {"reps": 8, "weight_kg": 40, "duration_sec": None},
                {"reps": 6, "weight_kg": 45, "duration_sec": None},
            ],
        },
        {
            "name": "Plank",
            "sets": [
                {"reps": 1, "weight_kg": 0, "duration_sec": 75},
                {"reps": 1, "weight_kg": 0, "duration_sec": 75},
            ],
        },
    ],
    "assumptions": None,
}

_PRESCRIBED_MONDAY = """Goal: strength (leg/posterior-chain)

1. Back squat
Set 1: 8×70 kg
Set 2: 8×80 kg
Set 3: 6×90 kg
Set 4: 8×60 kg

2. Romanian deadlift
Set 1: 8×75 kg
Set 2: 8×80 kg
Set 3: 6×80 kg

3. Bulgarian split squat
Set 1: 8×40 kg
Set 2: 8×45 kg

4. Plank
Set 1: 30s hold
Set 2: 30s hold"""


def test_harness_instructions_cover_shorthand_and_comma_inheritance():
    assert "8r 70, 90, 6r 100" in GYM_SESSION_HARNESS_INSTRUCTIONS
    assert "inherit" in GYM_SESSION_HARNESS_INSTRUCTIONS.lower()
    assert "bulgarian" in GYM_SESSION_HARNESS_INSTRUCTIONS.lower()
    assert "never append extra sets" in GYM_SESSION_HARNESS_INSTRUCTIONS.lower()


_JACK_TODAY_LOG = """Back squat
8r 70, 90, 6r 100
Romanian deadlift
8r 60, 70, 6r 75
Kettlebell swings
15r 15, 8r 20, 6r 22.5
Bulgarian split squat
8r 35, 40"""

_JACK_TODAY_HALLUCINATED_LLM = {
    "unit": "kg",
    "exercises": [
        {
            "name": "Back squat",
            "sets": [
                {"reps": 8, "weight_kg": 70, "duration_sec": None},
                {"reps": 8, "weight_kg": 90, "duration_sec": None},
                {"reps": 6, "weight_kg": 80, "duration_sec": None},
                {"reps": 8, "weight_kg": 70, "duration_sec": None},
            ],
        },
        {
            "name": "Romanian deadlift",
            "sets": [
                {"reps": 8, "weight_kg": 60, "duration_sec": None},
                {"reps": 8, "weight_kg": 70, "duration_sec": None},
                {"reps": 6, "weight_kg": 75, "duration_sec": None},
            ],
        },
        {
            "name": "Kettlebell swings",
            "sets": [
                {"reps": 15, "weight_kg": 15, "duration_sec": None},
                {"reps": 8, "weight_kg": 20, "duration_sec": None},
                {"reps": 6, "weight_kg": 22.5, "duration_sec": None},
            ],
        },
        {
            "name": "Bulgarian split squat",
            "sets": [
                {"reps": 8, "weight_kg": 35, "duration_sec": None},
                {"reps": 8, "weight_kg": 40, "duration_sec": None},
            ],
        },
    ],
    "assumptions": None,
}


def test_openrouter_gym_session_response_format_is_strict_json_schema():
    fmt = openrouter_gym_session_response_format()
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["name"] == "gym_session_log"
    assert "exercises" in fmt["json_schema"]["schema"]["properties"]


_JACK_WEDNESDAY_LOG = """gym this morning:
Incline bench
8r 40, 45, 5r 50
barbell row
8r 50, 6r 55, 55
lat pull-down
8r 54, 6r 63.5
russian twist
20r 7.5, 10"""

_JACK_WEDNESDAY_INCOMPLETE_LLM = {
    "unit": "kg",
    "exercises": [
        {
            "name": "Incline bench press",
            "sets": [
                {"reps": 8, "weight_kg": 40, "duration_sec": None},
                {"reps": 8, "weight_kg": 45, "duration_sec": None},
                {"reps": 5, "weight_kg": 50, "duration_sec": None},
            ],
        },
        {
            "name": "Barbell row",
            "sets": [
                {"reps": 8, "weight_kg": 50, "duration_sec": None},
                {"reps": 6, "weight_kg": 55, "duration_sec": None},
                {"reps": 6, "weight_kg": 55, "duration_sec": None},
            ],
        },
        {
            "name": "Lat pull-down",
            "sets": [
                {"reps": 8, "weight_kg": 54, "duration_sec": None},
                {"reps": 6, "weight_kg": 63.5, "duration_sec": None},
            ],
        },
        {
            "name": "Russian twists",
            "sets": [{"reps": 20, "weight_kg": 7.5, "duration_sec": None}],
        },
    ],
    "assumptions": None,
}


def test_canonical_exercise_name_handles_abbreviations():
    assert canonical_exercise_name("bulgarian") == "Bulgarian split squat"
    assert canonical_exercise_name("rdl") == "Romanian deadlift"
    assert canonical_exercise_name("Back squat") == "Back squat"
    assert canonical_exercise_name("incline bench") == "Incline bench press"
    assert canonical_exercise_name("russian twist") == "Russian twists"


def test_parse_gym_session_harness_json_jack_monday():
    parsed = parse_gym_session_harness_json(json.dumps(_JACK_MONDAY_HARNESS_JSON))
    assert parsed is not None
    assert len(parsed["exercises"]) == 4
    assert parsed["exercises"][0]["sets"][1]["reps"] == 8
    assert parsed["exercises"][0]["sets"][1]["weight_kg"] == 90.0


def test_gym_session_metrics_from_harness_jack_monday():
    data = parse_gym_session_harness_json(json.dumps(_JACK_MONDAY_HARNESS_JSON))
    assert data is not None
    metrics = finalize_gym_session_metrics(
        gym_session_metrics_from_harness(data, 0, "Gym (Zulip DM)")
    )
    assert metrics is not None
    assert metrics.total_tonnage_kg == 5580
    by_name = {ex.name: ex for ex in metrics.exercises}
    assert by_name["Back squat"].tonnage_kg == 2440
    assert by_name["Bulgarian split squat"].tonnage_kg == 1660
    assert len(metrics.exercises) == 4


def test_parse_gym_session_with_llm_harness_uses_structured_call(monkeypatch):
    captured: dict = {}

    def fake_call_llm(system, user, api_key, **kwargs):
        captured["system"] = system
        captured["response_format"] = kwargs.get("response_format")
        return json.dumps(_JACK_MONDAY_HARNESS_JSON)

    monkeypatch.setattr("generate_training_plan._call_llm", fake_call_llm)

    metrics = parse_gym_session_with_llm_harness(
        0, "Gym (Zulip DM)", _JACK_MONDAY_LOG, "test-token"
    )
    assert captured["system"] == GYM_SESSION_HARNESS_INSTRUCTIONS
    assert captured["response_format"]["type"] == "json_schema"
    assert metrics is not None
    assert finalize_gym_session_metrics(metrics).total_tonnage_kg == 5580


def test_coach_transcript_fallback_when_llm_fails(monkeypatch):
    monkeypatch.setattr(
        "generate_training_plan._call_llm",
        lambda *args, **kwargs: "OpenRouter API request failed: HTTP 400",
    )
    metrics = parse_gym_session_metrics(
        0, "Gym (Zulip DM)", _JACK_MONDAY_LOG, "test-token"
    )
    assert metrics is not None
    assert metrics.total_tonnage_kg == 5580


def test_coach_transcript_parses_jack_wednesday_with_inherited_weights():
    raw = parse_coach_gym_transcript(_JACK_WEDNESDAY_LOG)
    assert raw is not None
    metrics = finalize_gym_session_metrics(raw)
    by_name = {ex.name: ex for ex in metrics.exercises}
    assert metrics.total_tonnage_kg == 3153
    assert by_name["Incline bench press"].tonnage_kg == 930
    assert by_name["Russian twists"].tonnage_kg == 350
    assert len(by_name["Russian twists"].sets) == 2
    assert by_name["Russian twists"].sets[1].weight_kg == 10.0


def test_coach_transcript_parses_jack_today_three_set_squat():
    raw = parse_coach_gym_transcript(_JACK_TODAY_LOG)
    assert raw is not None
    metrics = finalize_gym_session_metrics(raw)
    by_name = {ex.name: ex for ex in metrics.exercises}
    assert metrics.total_tonnage_kg == 5090
    assert by_name["Back squat"].max_weight_kg == 100.0
    assert by_name["Back squat"].tonnage_kg == 1880
    assert len(by_name["Back squat"].sets) == 3


def test_reconcile_prefers_transcript_over_hallucinated_llm_sets():
    from gym_suunto import reconcile_gym_metrics_with_transcript

    data = parse_gym_session_harness_json(json.dumps(_JACK_TODAY_HALLUCINATED_LLM))
    assert data is not None
    llm_metrics = gym_session_metrics_from_harness(data, 0, "Gym (Zulip DM)")
    assert llm_metrics is not None
    assert finalize_gym_session_metrics(llm_metrics).total_tonnage_kg == 5530

    merged = reconcile_gym_metrics_with_transcript(llm_metrics, _JACK_TODAY_LOG)
    final = finalize_gym_session_metrics(merged)
    squat = next(ex for ex in final.exercises if ex.name == "Back squat")
    assert final.total_tonnage_kg == 5090
    assert squat.max_weight_kg == 100.0
    assert squat.tonnage_kg == 1880
    assert len(squat.sets) == 3


def test_parse_gym_session_metrics_reconciles_hallucinated_llm(monkeypatch):
    monkeypatch.setattr(
        "generate_training_plan._call_llm",
        lambda *args, **kwargs: json.dumps(_JACK_TODAY_HALLUCINATED_LLM),
    )
    metrics = parse_gym_session_metrics(
        0, "Gym (Zulip DM)", _JACK_TODAY_LOG, "test-token"
    )
    assert metrics is not None
    squat = next(ex for ex in metrics.exercises if ex.name == "Back squat")
    assert metrics.total_tonnage_kg == 5090
    assert squat.max_weight_kg == 100.0


def test_reconcile_adds_missing_russian_twist_set_from_transcript():
    from gym_suunto import reconcile_gym_metrics_with_transcript

    data = parse_gym_session_harness_json(json.dumps(_JACK_WEDNESDAY_INCOMPLETE_LLM))
    assert data is not None
    llm_metrics = gym_session_metrics_from_harness(data, 0, "Gym (Zulip DM)")
    assert llm_metrics is not None
    assert llm_metrics.total_tonnage_kg == 2953

    merged = reconcile_gym_metrics_with_transcript(
        llm_metrics, _JACK_WEDNESDAY_LOG
    )
    final = finalize_gym_session_metrics(merged)
    assert final.total_tonnage_kg == 3153
    twists = next(ex for ex in final.exercises if ex.name == "Russian twists")
    assert len(twists.sets) == 2
    assert twists.sets[1].weight_kg == 10.0


def test_parse_gym_session_metrics_reconciles_incomplete_llm(monkeypatch):
    monkeypatch.setattr(
        "generate_training_plan._call_llm",
        lambda *args, **kwargs: json.dumps(_JACK_WEDNESDAY_INCOMPLETE_LLM),
    )
    metrics = parse_gym_session_metrics(
        0, "Gym (Zulip DM)", _JACK_WEDNESDAY_LOG, "test-token"
    )
    assert metrics is not None
    assert metrics.total_tonnage_kg == 3153


def test_coach_transcript_parses_jack_monday_directly():
    raw = parse_coach_gym_transcript(_JACK_MONDAY_LOG)
    assert raw is not None
    metrics = finalize_gym_session_metrics(raw)
    assert metrics.total_tonnage_kg == 5580


def test_suunto_bold_format_still_parses():
    text = "**Back Squat:** 8r 70, 90, 6r 100, 8r 70\n**Romanian Deadlift:** 8r 60, 80, 4r 90"
    raw = parse_suunto_gym_description(text)
    assert raw is not None
    metrics = finalize_gym_session_metrics(raw)
    assert metrics.total_tonnage_kg == 3920


def test_prescribed_monday_tonnage_includes_per_leg_doubling():
    prescribed = parse_prescribed_gym_session(_PRESCRIBED_MONDAY)
    assert prescribed is not None
    assert prescribed.total_tonnage_kg == 5300


def test_harness_actual_above_prescribed_for_jack_monday():
    data = parse_gym_session_harness_json(json.dumps(_JACK_MONDAY_HARNESS_JSON))
    actual = finalize_gym_session_metrics(
        gym_session_metrics_from_harness(data, 0, "Gym (Zulip DM)")
    )
    prescribed = parse_prescribed_gym_session(_PRESCRIBED_MONDAY)
    assert actual.total_tonnage_kg > prescribed.total_tonnage_kg
