"""Tests for heart-rate enrichment from interval rows."""

from __future__ import annotations

import pytest

from erg_hr_enrich import enrich_erg_metrics_hr, weighted_avg_hr


def test_weighted_avg_hr_from_intervals():
    intervals = [
        {"time_sec": 180, "avg_hr": 153},
        {"time_sec": 180, "avg_hr": 151},
        {"time_sec": 180, "avg_hr": 154},
    ]
    assert weighted_avg_hr(intervals) == pytest.approx(152.7, abs=0.1)


def test_enrich_part_hr_from_screenshot_intervals():
    extractions = [
        {
            "index": 1,
            "metrics": {
                "intervals": [
                    {"time_sec": 180, "avg_hr": 153},
                    {"time_sec": 180, "avg_hr": 151},
                    {"time_sec": 180, "avg_hr": 154},
                    {"time_sec": 180, "avg_hr": 151},
                    {"time_sec": 180, "avg_hr": 150},
                    {"time_sec": 180, "avg_hr": 154},
                ]
            },
        }
    ]
    metrics = {
        "session_parts": [
            {
                "role": "main",
                "screenshot_index": 1,
                "duration_sec": 1080,
                "avg_split_500_sec": 122.3,
            }
        ]
    }
    out = enrich_erg_metrics_hr(metrics, extractions)
    assert out["session_parts"][0]["avg_hr"] == pytest.approx(152.2, abs=0.1)
    assert out["avg_hr"] == pytest.approx(152.2, abs=0.1)


def test_enrich_session_hr_for_jack_june_30_style_session():
    """Main-set HR from 6x3:00 intervals when synthesis leaves avg_hr null."""
    extractions = [
        {
            "index": 1,
            "metrics": {
                "intervals": [
                    {"time_sec": 180, "avg_hr": 153},
                    {"time_sec": 180, "avg_hr": 151},
                    {"time_sec": 180, "avg_hr": 154},
                    {"time_sec": 180, "avg_hr": 151},
                    {"time_sec": 180, "avg_hr": 150},
                    {"time_sec": 180, "avg_hr": 154},
                ]
            },
        }
    ]
    out = enrich_erg_metrics_hr(
        {
            "session_parts": [
                {
                    "role": "warmup",
                    "screenshot_index": 0,
                    "duration_sec": 600,
                    "avg_hr": 129,
                },
                {
                    "role": "main",
                    "screenshot_index": 1,
                    "duration_sec": 1080,
                    "avg_split_500_sec": 122.3,
                },
                {
                    "role": "cooldown",
                    "screenshot_index": 2,
                    "duration_sec": 600,
                    "avg_hr": 124,
                },
            ]
        },
        extractions,
    )
    main = next(p for p in out["session_parts"] if p["role"] == "main")
    assert main["avg_hr"] == pytest.approx(152.2, abs=0.1)
    assert out["avg_hr"] == pytest.approx(135.1, abs=0.1)
