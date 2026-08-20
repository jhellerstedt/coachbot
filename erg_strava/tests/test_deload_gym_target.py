"""Tests for dynamic deload gym tonnage target computation."""

from __future__ import annotations

from season_master_plan import (
    apply_deload_gym_targets_to_season_data,
    compute_deload_gym_target,
    parse_season_master_plan_md,
)
from weekly_plan_master_align import extract_weekly_targets


DELoad_MD = """# Season Master Plan

## Weekly progression

| Week | Phase | Tgt priority | Tgt km | Tgt min | Tgt Z2 | Tgt Z5 | Tgt gym kg | Pln km | Pln min | Pln Z2 | Pln Z5 | Pln gym kg | Act km | Act min | Act Z2 | Act Z5 | Act gym kg |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-06-08 | build | hr | 48 | 280 | 85% | 5% | 6000 | — | — | — | — | — | — | — | — | — | 6200 |
| 2026-06-15 | build | hr | 48 | 280 | 85% | 5% | 6500 | — | — | — | — | — | — | — | — | — | 6400 |
| 2026-06-22 | build | hr | 48 | 280 | 85% | 5% | 7000 | — | — | — | — | — | — | — | — | — | 6600 |
| 2026-06-29 | deload | hr | 42 | 250 | 85% | 5% | 3000 | — | — | — | — | — | — | — | — | — | — |
"""


DELOAD_MD_PLANNED_FALLBACK = """# Season Master Plan

## Weekly progression

| Week | Phase | Tgt priority | Tgt km | Tgt min | Tgt Z2 | Tgt Z5 | Tgt gym kg | Pln km | Pln min | Pln Z2 | Pln Z5 | Pln gym kg | Act km | Act min | Act Z2 | Act Z5 | Act gym kg |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-06-08 | build | hr | 48 | 280 | 85% | 5% | 6000 | — | — | — | — | — | — | — | — | — | — |
| 2026-06-15 | build | hr | 48 | 280 | 85% | 5% | 6500 | — | — | — | — | — | — | — | — | — | — |
| 2026-06-22 | build | hr | 48 | 280 | 85% | 5% | 7000 | — | — | — | — | — | — | — | — | — | — |
| 2026-06-29 | deload | hr | 42 | 250 | 85% | 5% | 3000 | — | — | — | — | — | — | — | — | — | — |
"""


def test_compute_deload_gym_target_from_actuals():
    parsed = parse_season_master_plan_md(DELoad_MD)
    weeks = parsed["weeks"]
    result = compute_deload_gym_target(weeks, "2026-06-29")
    assert result is not None
    assert result.source == "actual"
    assert result.raw_mean_kg == 6400.0
    assert result.target_kg == 3200.0
    assert result.reference_week_ids == ("2026-06-08", "2026-06-15", "2026-06-22")


def test_compute_deload_gym_target_planned_fallback():
    parsed = parse_season_master_plan_md(DELOAD_MD_PLANNED_FALLBACK)
    weeks = parsed["weeks"]
    result = compute_deload_gym_target(weeks, "2026-06-29")
    assert result is not None
    assert result.source == "planned fallback"
    assert result.raw_mean_kg == 6500.0
    assert result.target_kg == 3250.0


def test_extract_weekly_targets_overrides_deload_static_tgt():
    targets = extract_weekly_targets(DELoad_MD)
    assert targets["2026-06-29"].tgt_gym_kg == 3200.0


def test_apply_deload_gym_targets_sets_source_metadata():
    parsed = parse_season_master_plan_md(DELoad_MD)
    results = apply_deload_gym_targets_to_season_data(parsed)
    assert len(results) == 1
    row = parsed["weeks"]["2026-06-29_2026-07-05"]
    assert row["deload_gym_target_source"] == "actual"
    assert row["target"]["gym_tonnage_kg"] == 3200.0
