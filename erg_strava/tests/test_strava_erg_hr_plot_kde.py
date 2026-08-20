"""Unit tests for erg split/HR KDE plotting helpers."""

from __future__ import annotations

import numpy as np

from strava_erg_hr_plot import athlete_period_color_map, kde_density_peaks


def test_athlete_period_color_map_single_athlete():
    colors = athlete_period_color_map(["Jack H"])
    assert colors[("Jack H", 1)] == "#1f77b4"
    assert colors[("Jack H", 2)] == "#ff7f0e"


def test_athlete_period_color_map_multi_athlete_unique_pairs():
    athletes = ["Jack H", "Emil", "James Merrett", "Vini Salazar"]
    colors = athlete_period_color_map(athletes)
    assert len(colors) == 8
    assert len(set(colors.values())) == 8
    for athlete in athletes:
        assert colors[(athlete, 1)] != colors[(athlete, 2)]


def test_kde_density_peaks_returns_empty_for_singular_screenshot_points():
    # One avg HR per screenshot session: zero HR variance makes gaussian_kde singular.
    x = np.array([120.0, 122.0, 125.0, 128.0, 130.0, 132.0])
    y = np.full_like(x, 148.0)
    assert kde_density_peaks(x, y, n_peaks=2) == []


def test_kde_density_peaks_returns_up_to_two_modes():
    rng = np.random.default_rng(0)
    cluster_a = rng.normal(loc=[95.0, 150.0], scale=[2.0, 4.0], size=(120, 2))
    cluster_b = rng.normal(loc=[110.0, 175.0], scale=[2.0, 4.0], size=(120, 2))
    pts = np.vstack([cluster_a, cluster_b])
    peaks = kde_density_peaks(pts[:, 0], pts[:, 1], n_peaks=2, bw_adjust=1.0)
    assert len(peaks) == 2
    peak_x = sorted(px for px, _ in peaks)
    assert peak_x[0] < 102.0
    assert peak_x[1] > 103.0
