"""Tests for structured weekly plan harness (parse, repair, finalize)."""

from __future__ import annotations

import json

from athlete_profile import AthleteProfile
from weekly_plan_harness import (
    parse_structured_plan_or_error,
    repair_parsed_weekly_plan,
    validate_parsed_weekly_plan,
)
from weekly_plan_schema import parse_weekly_plan, validate_plan_session_constraints

from test_weekly_plan_master_align import (
    _base_week_plan,
    _rest_day,
)


def test_repair_uses_squad_rowing_for_athlete_tuesday():
    squad = _base_week_plan()
    squad["days"][3]["session_type"] = "on_water"
    athlete = _base_week_plan(personalised=True)
    athlete["days"][1] = _rest_day("Tuesday", "2026-07-07")
    plan = parse_weekly_plan(athlete)
    assert plan is not None
    repaired = repair_parsed_weekly_plan(
        plan,
        include_lifting=True,
        phase="base",
        reference_plan=squad,
    )
    tuesday = next(d for d in repaired.days if d.weekday == "Tuesday")
    assert tuesday.session_type == "erg"
    main = next(s for s in tuesday.rowing.segments if s.phase == "main_set")
    assert main.zone_z == "Z4"


def test_finalize_imported_repairs_schedule_with_squad_reference():
    squad = _base_week_plan()
    imported = _base_week_plan(personalised=True)
    imported["days"][1] = _rest_day("Tuesday", "2026-07-07")
    parsed = parse_weekly_plan(imported)
    assert parsed is not None
    repaired = repair_parsed_weekly_plan(
        parsed,
        include_lifting=True,
        phase="base",
        reference_plan=squad,
    )
    tuesday = next(d for d in repaired.days if d.weekday == "Tuesday")
    assert tuesday.session_type == "erg"
    assert tuesday.rowing is not None
    main = next(s for s in tuesday.rowing.segments if s.phase == "main_set")
    assert main.zone_z == "Z4"


def test_parse_structured_accepts_repaired_broken_schedule():
    from weekly_plan_schema import weekly_plan_to_dict

    parsed = parse_weekly_plan(_base_week_plan())
    assert parsed is not None
    broken = weekly_plan_to_dict(parsed)
    broken["days"][1] = _rest_day("Tuesday", "2026-07-07")
    raw = json.dumps(broken)
    plan_dict, err = parse_structured_plan_or_error(
        raw, include_lifting=True, phase="base"
    )
    assert err is None, err
    assert validate_plan_session_constraints(parse_weekly_plan(plan_dict)) is None


def test_validate_parsed_weekly_plan_applies_athlete_hr_profile():
    athlete = _base_week_plan(personalised=True)
    main = athlete["days"][1]["rowing"]["segments"][1]
    main.update({"hr_bpm_min": 100, "hr_bpm_max": 110})
    plan = parse_weekly_plan(athlete)
    assert plan is not None

    err = validate_parsed_weekly_plan(
        plan,
        include_lifting=True,
        athlete_profile=AthleteProfile(id=1, label="Test", max_hr_bpm=182),
    )

    assert err is not None
    assert "HR" in err or "bpm" in err.lower()


def test_validate_parsed_weekly_plan_skips_hr_without_profile():
    athlete = _base_week_plan(personalised=True)
    main = athlete["days"][1]["rowing"]["segments"][1]
    main.update({"hr_bpm_min": 100, "hr_bpm_max": 110})
    plan = parse_weekly_plan(athlete)
    assert plan is not None

    assert validate_parsed_weekly_plan(plan, include_lifting=True) is None
