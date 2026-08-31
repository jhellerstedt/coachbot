"""KDE collection must not plot another athlete's leftover Suunto cache."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from strava_erg_hr_plot import (
    AthleteCfg,
    Concept2PhotoCfg,
    activity_fingerprint,
    athlete_paths,
    collect_all_points,
    format_new_erg_data_summary,
    photo_cfg_hash,
    write_parsed_activity_cache,
)
from suunto_client import SuuntoCfg
from suunto_sync import save_suunto_index, stable_activity_id, suunto_paths


JACK_ID = 53603359
VINI_ID = 98530402
SUUNTO_KEY = "leftoverjackerg"


def _photo_cfg() -> Concept2PhotoCfg:
    return Concept2PhotoCfg(
        enabled=False,
        device_substrings=("suunto",),
        time_window_sec=25.0,
        split_tolerance_sec=18.0,
        multi_stagger_sec=4.0,
    )


def _suunto_cfg(*, athlete_ids: frozenset[int] | None) -> SuuntoCfg:
    return SuuntoCfg(
        enabled=True,
        primary=True,
        suuntool_path=None,
        session_file=None,
        indoor_rowing_activity_ids=frozenset({57}),
        gym_activity_ids=frozenset({23}),
        athlete_ids=athlete_ids,
    )


def _seed_leftover_suunto_plot_cache(cache_dir: Path, athlete_id: int, label: str) -> None:
    start = datetime(2026, 6, 23, 21, 4, 32, tzinfo=timezone.utc)
    rec = {
        "key": SUUNTO_KEY,
        "activityId": 57,
        "startTime": int(start.timestamp() * 1000),
        "totalTime": 600,
        "totalDistance": 2210,
        "activityName": "INDOOR_ROWING",
        "strava_activity_id": None,
        "has_hr_fit": True,
    }
    paths_suunto = suunto_paths(cache_dir, athlete_id)
    save_suunto_index(paths_suunto["index"], {"workouts": {SUUNTO_KEY: rec}, "by_strava_id": {}})

    paths = athlete_paths(cache_dir, athlete_id)
    for key in ("streams", "fits", "details", "photos", "parsed"):
        paths[key].mkdir(parents=True, exist_ok=True)
    (paths["index"]).write_text('{"activities": []}')

    activity_id = stable_activity_id(SUUNTO_KEY, None)
    photo_hash = photo_cfg_hash(_photo_cfg())
    fingerprint = activity_fingerprint(
        paths,
        activity_id,
        photo_hash,
        suunto_key=SUUNTO_KEY,
        athlete_id=athlete_id,
        cache_dir=cache_dir,
    )
    df = pd.DataFrame(
        {
            "time": [0.0, 10.0],
            "split_500": [125.0, 126.0],
            "hr": [137.0, 138.0],
            "point_source": ["stream_10s", "stream_10s"],
            "athlete": [label, label],
            "activity_id": [activity_id, activity_id],
            "suunto_key": [SUUNTO_KEY, SUUNTO_KEY],
            "zulip_score_id": [None, None],
            "activity_start": [start, start],
        }
    )
    write_parsed_activity_cache(
        paths,
        {"parser_version": "", "photo_cfg_hash": "", "activities": {}},
        photo_hash,
        activity_id,
        df,
        fingerprint,
        start.isoformat(),
        "stream_10s",
        suunto_key=SUUNTO_KEY,
    )


def test_collect_all_points_ignores_suunto_cache_outside_athlete_ids(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    _seed_leftover_suunto_plot_cache(cache_dir, VINI_ID, "Vini Salazar")
    vini = AthleteCfg(id=VINI_ID, label="Vini Salazar", token_dir=None)
    df = collect_all_points(
        [vini],
        cache_dir,
        frozenset({"VirtualRow", "Rowing"}),
        True,
        _photo_cfg(),
        suunto_cfg=_suunto_cfg(athlete_ids=frozenset({JACK_ID})),
    )
    assert df.empty


def test_collect_all_points_keeps_suunto_cache_for_listed_athlete(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    _seed_leftover_suunto_plot_cache(cache_dir, JACK_ID, "Jack H")
    jack = AthleteCfg(id=JACK_ID, label="Jack H", token_dir=None)
    df = collect_all_points(
        [jack],
        cache_dir,
        frozenset({"VirtualRow", "Rowing"}),
        True,
        _photo_cfg(),
        suunto_cfg=_suunto_cfg(athlete_ids=frozenset({JACK_ID})),
    )
    assert not df.empty
    assert set(df["athlete"].unique()) == {"Jack H"}
    assert set(df["suunto_key"].dropna().unique()) == {SUUNTO_KEY}


def test_caption_lists_screenshot_when_no_new_suunto_keys(tmp_path: Path):
    scores = tmp_path / f"athlete_{JACK_ID}" / "erg_scores"
    scores.mkdir(parents=True)
    (scores / "screenshot-1.json").write_text(
        json.dumps(
            {
                "id": "screenshot-1",
                "session_date": "2026-08-27",
                "source": "zulip_screenshot_vision_multi",
                "metrics": {"distance_m": 8170},
            }
        )
    )
    df = pd.DataFrame({"suunto_key": [SUUNTO_KEY]})

    text = format_new_erg_data_summary(
        df,
        [AthleteCfg(id=JACK_ID, label="Jack H", token_dir=None)],
        tmp_path,
        frozenset({"VirtualRow", "Rowing"}),
        True,
        {
            "run_at": "2026-08-23T10:00:00+00:00",
            "suunto_keys": [SUUNTO_KEY],
        },
    )

    assert text is not None
    assert "screenshot screenshot-1, 8170 m, 2026-08-27" in text
    assert "(none)" not in text
