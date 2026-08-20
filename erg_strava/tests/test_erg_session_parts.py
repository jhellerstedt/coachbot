"""Tests for multi-screenshot session part normalization."""

from __future__ import annotations

from datetime import date

from erg_prescription_compare import compare_erg_session_to_prescription
from erg_session_parts import normalize_multi_screenshot_session
from generate_training_plan import find_erg_score_for_reaction_message


def _mislabeled_thursday_metrics() -> dict:
    return {
        "session_date": "2026-06-25",
        "distance_m": 7089,
        "duration_sec": 1920,
        "avg_split_500_sec": 136.66,
        "avg_split_500_fmt": "2:16.7",
        "workout_type": "intervals",
        "session_parts": [
            {
                "role": "interval_block",
                "screenshot_index": 0,
                "distance_m": 2194,
                "duration_sec": 600,
                "avg_split_500_fmt": "2:16.7",
                "avg_hr": 136,
            },
            {
                "role": "main",
                "screenshot_index": 1,
                "distance_m": 2779,
                "duration_sec": 720,
                "avg_split_500_fmt": "2:09.5",
                "avg_hr": 147,
            },
            {
                "role": "warmup",
                "screenshot_index": 2,
                "distance_m": 2118,
                "duration_sec": 600,
                "avg_split_500_fmt": "2:21.6",
                "avg_hr": 132,
            },
        ],
    }


def _mislabeled_extractions() -> list:
    return [
        {
            "index": 0,
            "metrics": {
                "workout_type": "intervals",
                "duration_sec": 600,
                "intervals": [{"time_sec": 120}] * 5,
            },
        },
        {
            "index": 1,
            "metrics": {
                "workout_type": "intervals",
                "duration_sec": 720,
                "intervals": [
                    {"time_sec": 240},
                    {"time_sec": 240},
                    {"time_sec": 240},
                ],
            },
        },
        {
            "index": 2,
            "metrics": {
                "workout_type": "steady",
                "duration_sec": 600,
            },
        },
    ]


def test_normalize_re_roles_cooldown_and_warmup():
    metrics = normalize_multi_screenshot_session(
        {"session_parts": _mislabeled_thursday_metrics()["session_parts"]},
        _mislabeled_extractions(),
    )
    parts = {p["role"]: p for p in metrics["session_parts"]}
    assert parts["warmup"]["avg_split_500_fmt"] == "2:21.6"
    assert parts["main"]["avg_split_500_fmt"] == "2:09.5"
    assert parts["cooldown"]["avg_split_500_fmt"] == "2:16.7"
    assert metrics["duration_sec"] == 1920


def test_find_erg_score_for_reaction_by_upload_message(tmp_path):
    from generate_training_plan import save_erg_score_record

    athlete_id = 53603359
    record = {
        "id": "abc",
        "zulip_message_id": 101587,
        "session_date": "2026-06-25",
        "metrics": {"distance_m": 1000},
    }
    save_erg_score_record(tmp_path, athlete_id, record)
    found = find_erg_score_for_reaction_message(tmp_path, 101587)
    assert found is not None
    assert found[0] == athlete_id
    assert found[1]["id"] == "abc"


def test_prescription_after_normalize_uses_single_main_line():
    from test_erg_prescription_compare import (
        _jack_thursday_erg_alt_record,
        _thursday_on_water_plan,
    )

    record = _jack_thursday_erg_alt_record()
    record["metrics"] = normalize_multi_screenshot_session(
        record["metrics"], _mislabeled_extractions()
    )
    lines = compare_erg_session_to_prescription(
        record, _thursday_on_water_plan(), date(2026, 6, 25)
    )
    text = "\n".join(lines)
    assert text.count("Main set") == 1
    assert "Cool-down" in text
    assert "not logged" not in text


def test_normalize_three_all_main_roles_by_split():
    metrics = {
        "session_parts": [
            {
                "role": "main",
                "screenshot_index": 0,
                "duration_sec": 600,
                "avg_split_500_fmt": "2:16.7",
            },
            {
                "role": "main",
                "screenshot_index": 1,
                "duration_sec": 720,
                "avg_split_500_fmt": "2:09.5",
            },
            {
                "role": "main",
                "screenshot_index": 2,
                "duration_sec": 600,
                "avg_split_500_fmt": "2:21.6",
            },
        ],
    }
    out = normalize_multi_screenshot_session(metrics, [])
    parts = {p["role"]: p for p in out["session_parts"]}
    assert parts["warmup"]["avg_split_500_fmt"] == "2:21.6"
    assert parts["main"]["avg_split_500_fmt"] == "2:09.5"
    assert parts["cooldown"]["avg_split_500_fmt"] == "2:16.7"


def _tuesday_threshold_parts_split_would_swap() -> list:
    """14m slow + 35m main + 21m mid-split — split heuristic swaps WU/CD vs a 21/14 plan."""
    return [
        {
            "role": "other",
            "screenshot_index": 0,
            "duration_sec": 840,
            "avg_split_500_fmt": "2:28.8",
            "avg_hr": 123,
        },
        {
            "role": "main",
            "screenshot_index": 1,
            "duration_sec": 2100,
            "avg_split_500_fmt": "2:05.4",
            "avg_hr": 158,
        },
        {
            "role": "other",
            "screenshot_index": 2,
            "duration_sec": 1260,
            "avg_split_500_fmt": "2:15.6",
            "avg_hr": 136,
        },
    ]


def test_normalize_assigns_warmup_cooldown_by_prescribed_duration_when_unequal():
    metrics = {"session_parts": _tuesday_threshold_parts_split_would_swap()}
    out = normalize_multi_screenshot_session(
        metrics,
        [],
        prescribed_warmup_min=21.0,
        prescribed_cooldown_min=14.0,
    )
    parts = {p["role"]: p for p in out["session_parts"]}
    assert parts["warmup"]["duration_sec"] == 1260
    assert parts["main"]["duration_sec"] == 2100
    assert parts["cooldown"]["duration_sec"] == 840


def test_normalize_keeps_split_roles_without_prescribed_durations():
    metrics = {"session_parts": _tuesday_threshold_parts_split_would_swap()}
    out = normalize_multi_screenshot_session(metrics, [])
    parts = {p["role"]: p for p in out["session_parts"]}
    assert parts["warmup"]["duration_sec"] == 840
    assert parts["main"]["duration_sec"] == 2100
    assert parts["cooldown"]["duration_sec"] == 1260
