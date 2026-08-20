"""Suunto activityId → sport_type mapping for endurance classification."""

from __future__ import annotations

from suunto_client import SuuntoCfg
from suunto_sync import _suunto_sport_type


def _cfg() -> SuuntoCfg:
    return SuuntoCfg(
        enabled=True,
        primary=True,
        suuntool_path=None,
        session_file=None,
        indoor_rowing_activity_ids=frozenset({57}),
        gym_activity_ids=frozenset({23}),
    )


def test_suunto_activity_id_2_is_ride_even_when_mislabeled_indoor_rowing():
    rec = {
        "activityId": 2,
        "activityName": "INDOOR_ROWING",
        "sport_type": "Workout",
        "totalTime": 769,
        "totalDistance": 3762,
    }
    assert _suunto_sport_type(rec, _cfg()) == "Ride"


def test_suunto_activity_id_1_is_run_even_when_mislabeled():
    rec = {
        "activityId": 1,
        "activityName": "INDOOR_ROWING",
        "sport_type": "Workout",
        "totalTime": 1859,
        "totalDistance": 5114,
    }
    assert _suunto_sport_type(rec, _cfg()) == "Run"


def test_suunto_activity_id_57_remains_virtual_row():
    rec = {"activityId": 57, "activityName": "INDOOR_ROWING", "sport_type": "Workout"}
    assert _suunto_sport_type(rec, _cfg()) == "VirtualRow"
