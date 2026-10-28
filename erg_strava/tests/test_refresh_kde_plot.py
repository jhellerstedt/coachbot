"""KDE refresh syncs, merges screenshots, plots, and does not plan."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from strava_erg_hr_plot import (
    AthleteCfg,
    Concept2PhotoCfg,
    GymCfg,
    StravaCfg,
    refresh_kde_plot,
)
from suunto_client import SuuntoCfg


def _athlete() -> AthleteCfg:
    return AthleteCfg(id=7, label="Ada", token_dir=None)


def _loaded(tmp_path: Path):
    return (
        tmp_path,
        [_athlete()],
        frozenset({"VirtualRow", "Rowing"}),
        True,
        Concept2PhotoCfg(
            enabled=False,
            device_substrings=(),
            time_window_sec=120,
            split_tolerance_sec=2,
            multi_stagger_sec=8,
        ),
        GymCfg(sport_types=frozenset({"WeightTraining"}), name_patterns=()),
        SuuntoCfg(
            enabled=True,
            primary=True,
            suuntool_path=None,
            session_file=None,
            indoor_rowing_activity_ids=frozenset({57}),
            gym_activity_ids=frozenset({23}),
        ),
        StravaCfg(optional=True, link_for_photos=True),
    )


def _points() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "activity_id": [11],
            "suunto_key": ["wk"],
            "athlete": ["Ada"],
        }
    )


def _patches(tmp_path: Path, df: pd.DataFrame, *, sync_result=True, post_ok=True):
    def plot(_df, out: Path, athletes=None):
        if not _df.empty:
            out.write_bytes(b"png")

    return (
        patch("strava_erg_hr_plot.load_config", return_value=_loaded(tmp_path)),
        patch("strava_erg_hr_plot.resolve_available_strava_credentials", return_value=None),
        patch("strava_erg_hr_plot.sync_athlete", return_value=sync_result),
        patch(
            "erg_session_merge.rebuild_merged_erg_sessions_for_athlete",
            return_value=[],
        ),
        patch("strava_erg_hr_plot.collect_all_points", return_value=df),
        patch("strava_erg_hr_plot.plot_three_panels", side_effect=plot),
        patch("strava_erg_hr_plot.post_plot_to_zulip", return_value=post_ok),
        patch("strava_erg_hr_plot.run_weekly_training_pipeline"),
        patch("strava_erg_hr_plot.sync_activity_metrics_cache"),
        patch("strava_erg_hr_plot.send_athlete_data_alerts_safely"),
    )


def test_refresh_syncs_merges_and_posts_without_a_plan(tmp_path: Path):
    patches = _patches(tmp_path, _points())
    with patches[0], patches[1], patches[2] as sync, patches[3] as merge, patches[4], patches[5], patches[6] as post, patches[7] as plan, patches[8] as metrics, patches[9] as alerts:
        reply = refresh_kde_plot(tmp_path / "config.yaml")
    assert reply == "Posted the KDE plot to #general > project-640."
    sync.assert_called_once()
    assert sync.call_args.args[0].id == 7
    assert sync.call_args.kwargs["full_sync"] is False
    assert sync.call_args.kwargs["refresh_suunto"] is False
    merge.assert_called_once()
    assert merge.call_args.args[1] == 7
    post.assert_called_once()
    assert post.call_args.kwargs["stream"] == "general"
    assert post.call_args.kwargs["topic"] == "project-640"
    plan.assert_not_called()
    metrics.assert_not_called()
    alerts.assert_not_called()
    assert (tmp_path / "erg_plot_last_run.json").is_file()


def test_empty_points_skip_the_zulip_post(tmp_path: Path):
    patches = _patches(tmp_path, pd.DataFrame())
    with patches[0], patches[1], patches[2], patches[3] as merge, patches[4], patches[5] as plot, patches[6] as post, patches[7]:
        reply = refresh_kde_plot(tmp_path / "config.yaml")
    assert reply == "Sync finished, but there were no erg points to plot."
    merge.assert_called_once()
    plot.assert_called_once()
    post.assert_not_called()


def test_failed_sync_still_merges_and_plots_from_cache(tmp_path: Path):
    patches = _patches(tmp_path, _points(), sync_result=False)
    with patches[0], patches[1], patches[2], patches[3] as merge, patches[4], patches[5], patches[6] as post, patches[7] as plan:
        reply = refresh_kde_plot(tmp_path / "config.yaml")
    merge.assert_called_once()
    post.assert_called_once()
    plan.assert_not_called()
    assert "Sync failed for Ada." in reply
    assert "Posted the KDE plot to #general > project-640." in reply


def test_failed_post_is_reported(tmp_path: Path):
    patches = _patches(tmp_path, _points(), post_ok=False)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
        reply = refresh_kde_plot(tmp_path / "config.yaml")
    assert reply == "Drew the KDE plot but could not post it to Zulip."
