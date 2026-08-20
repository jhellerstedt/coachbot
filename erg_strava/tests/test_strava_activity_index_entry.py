"""Strava activity index summaries must keep duration fields for volume tallies."""

from __future__ import annotations

from strava_erg_hr_plot import strava_activity_index_entry


def test_strava_activity_index_entry_preserves_moving_and_elapsed_time():
    act = {
        "id": 18889259021,
        "name": "Meating time",
        "sport_type": "Ride",
        "type": "Ride",
        "start_date": "2026-06-12T11:07:15Z",
        "trainer": False,
        "distance": 4776.0,
        "moving_time": 762,
        "elapsed_time": 900,
        "extra_ignored": True,
    }

    entry = strava_activity_index_entry(act)

    assert entry == {
        "id": 18889259021,
        "name": "Meating time",
        "sport_type": "Ride",
        "start_date": "2026-06-12T11:07:15Z",
        "trainer": False,
        "distance": 4776.0,
        "moving_time": 762,
        "elapsed_time": 900,
    }


def test_strava_activity_index_entry_falls_back_to_type_for_sport():
    entry = strava_activity_index_entry(
        {
            "id": 1,
            "name": "Bobbie’s",
            "type": "Run",
            "start_date": "2026-07-01T00:00:00Z",
            "moving_time": 1729,
        }
    )
    assert entry["sport_type"] == "Run"
    assert entry["moving_time"] == 1729
