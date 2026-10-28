"""Concept2 watts, Hammerhead+SRM selection, and the shared power axis."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from bike_power import (
    rides_to_fetch,
    split_seconds_to_watts,
    sync_bike_power_fits,
    watts_to_split_seconds,
)
from strava_erg_hr_plot import plot_three_panels


def test_two_minute_split_is_about_203_watts():
    watts = split_seconds_to_watts(120.0)
    assert watts == pytest.approx(202.5, abs=0.2)
    assert watts_to_split_seconds(watts) == pytest.approx(120.0, abs=0.05)


def test_rides_to_fetch_keeps_new_cycling_and_stops_before_the_cutoff():
    since = date(2025, 3, 7)
    after = int(datetime(2025, 4, 1, tzinfo=timezone.utc).timestamp() * 1000)
    before = int(datetime(2025, 3, 1, tzinfo=timezone.utc).timestamp() * 1000)
    items = [
        {"key": "ride", "activityId": 2, "startTime": after},
        {"key": "row", "activityId": 57, "startTime": after},
        {"key": "known", "activityId": 2, "startTime": after},
        {"key": "old", "activityId": 2, "startTime": before},
    ]
    selected, stop = rides_to_fetch(items, {"known": {"kept": False}}, since)
    assert [row["key"] for row in selected] == ["ride"]
    assert stop is True


def test_sync_keeps_only_hammerhead_srm_with_power(tmp_path: Path, monkeypatch):
    start = int(datetime(2025, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)

    def list_page(offset: int):
        if offset:
            return []
        return [
            {"key": "good", "activityId": 2, "startTime": start},
            {"key": "other", "activityId": 2, "startTime": start},
        ]

    def download(key: str, dest: Path) -> bytes:
        payload = b"srm" if key == "good" else b"nope"
        dest.write_bytes(payload)
        return payload

    monkeypatch.setattr(
        "bike_power.fit_bytes_are_hammerhead_srm",
        lambda content: content == b"srm",
    )
    monkeypatch.setattr(
        "bike_power.power_hr_samples_from_fit",
        lambda content: pd.DataFrame(
            {"time": [0, 1, 2], "watts": [180, 190, 200], "hr": [140, 142, 144]}
        ),
    )
    n = sync_bike_power_fits(tmp_path, 7, list_page=list_page, download_fit=download)
    assert n == 1
    assert (tmp_path / "athlete_7" / "bike_power" / "fits" / "good.fit").is_file()
    assert not (tmp_path / "athlete_7" / "bike_power" / "fits" / "other.fit").exists()


def test_plot_rows_share_the_power_axis(tmp_path: Path, monkeypatch):
    import matplotlib.pyplot as plt
    import seaborn as sns

    figures = []
    monkeypatch.setattr(plt, "close", lambda *args, **kwargs: None)
    monkeypatch.setattr(sns, "kdeplot", lambda *args, **kwargs: None)
    monkeypatch.setattr("strava_erg_hr_plot.kde_density_peaks", lambda *args, **kwargs: [])
    real_subplots = plt.subplots

    def subplots(*args, **kwargs):
        fig, axes = real_subplots(*args, **kwargs)
        figures.append((fig, axes))
        return fig, axes

    monkeypatch.setattr(plt, "subplots", subplots)
    start = pd.Timestamp("2026-01-01", tz="UTC")
    erg = pd.DataFrame(
        {
            "athlete": ["Jack H"] * 30,
            "activity_start": [start] * 30,
            "split_500": [115.0 + (i % 5) for i in range(30)],
            "hr": [145.0 + (i % 4) for i in range(30)],
        }
    )
    bike = pd.DataFrame(
        {
            "athlete": ["Jack H"] * 30,
            "activity_start": [start] * 30,
            "watts": [190.0 + (i % 5) * 8 for i in range(30)],
            "hr": [130.0 + (i % 4) for i in range(30)],
        }
    )
    out = tmp_path / "plot.png"
    plot_three_panels(
        erg,
        out,
        now=datetime(2026, 6, 1, tzinfo=timezone.utc),
        bike_df=bike,
    )
    assert figures, "plot did not build a figure"
    _fig, axes = figures[0]
    assert axes.shape == (2, 3)
    assert axes[0, 0].get_shared_x_axes().joined(axes[0, 0], axes[1, 0])
    top_labels = [ax.get_xlabel() for ax in axes[0, 0].child_axes]
    assert any("500 m split" in label for label in top_labels)
    plt.close("all")
