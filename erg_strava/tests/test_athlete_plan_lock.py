"""Tests for locking athlete weekly plans to squad structure."""

from __future__ import annotations

import copy

from athlete_profile import AthleteProfile
from test_weekly_plan_schema import _recommended_erg_dict, sample_squad_plan_dict
from weekly_plan_schema import parse_weekly_plan, validate_athlete_plan_against_squad

from athlete_plan_lock import lock_athlete_plan_to_squad


def _squad_with_pyramid_and_extra() -> dict:
    squad = sample_squad_plan_dict()
    squad["recommended_erg"] = _recommended_erg_dict()
    squat = squad["days"][0]["gym"]["exercises"][0]
    squat["sets"] = [
        {"reps": 5, "weight_kg": 70.0, "duration_sec": None},
        {"reps": 7, "weight_kg": 55.0, "duration_sec": None},
        {"reps": 8, "weight_kg": 50.0, "duration_sec": None},
    ]
    return squad


def _drifted_proposal(squad: dict) -> dict:
    proposal = copy.deepcopy(squad)
    proposal["personalised"] = True
    proposal.pop("recommended_erg", None)
    tue = proposal["days"][1]["rowing"]
    tue["segments"][0]["duration"] = "12 min"
    tue["segments"][1]["split_min"] = "1:50"
    tue["segments"][1]["split_max"] = "1:55"
    tue["segments"].insert(
        2,
        {
            "phase": "rest",
            "label": "Rest",
            "duration": "2 min",
            "split_min": "2:20",
            "split_max": "2:25",
            "zone_z": "Z1",
            "zone_t": "T1",
            "hr_bpm_min": 92,
            "hr_bpm_max": 111,
            "priority": "hr",
            "notes": None,
        },
    )
    proposal["days"][3]["rowing"]["erg_alternative"] = None
    proposal["days"][0]["gym"]["exercises"][0]["sets"] = [
        {"reps": 5, "weight_kg": 65.0, "duration_sec": None},
        {"reps": 7, "weight_kg": 70.0, "duration_sec": None},
        {"reps": 8, "weight_kg": 55.0, "duration_sec": None},
    ]
    return proposal


def test_lock_keeps_squad_structure_and_overlays_splits():
    squad = _squad_with_pyramid_and_extra()
    proposal = _drifted_proposal(squad)
    profile = AthleteProfile(id=1, label="Jack H", max_hr_bpm=185)
    locked = lock_athlete_plan_to_squad(
        squad,
        proposal_json=proposal,
        athlete_profile=profile,
        greeting="Jack,",
    )
    parsed = parse_weekly_plan(locked)
    squad_plan = parse_weekly_plan(squad)
    assert parsed is not None and squad_plan is not None
    assert validate_athlete_plan_against_squad(parsed, squad_plan) is None
    assert parsed.personalised is True
    assert parsed.greeting == "Jack,"

    tue = next(d for d in parsed.days if d.weekday == "Tuesday")
    assert tue.rowing is not None
    assert tue.rowing.segments[0].duration == squad["days"][1]["rowing"]["segments"][0]["duration"]
    assert [s.phase for s in tue.rowing.segments] == ["warm_up", "main_set", "cool_down"]
    assert tue.rowing.segments[1].split_min == "1:50"
    assert tue.rowing.segments[1].split_max == "1:55"

    thu = next(d for d in parsed.days if d.weekday == "Thursday")
    assert thu.rowing is not None
    assert thu.rowing.erg_alternative is not None

    assert parsed.recommended_erg is not None
    assert parsed.recommended_erg.id == squad["recommended_erg"]["id"]

    squat_sets = parsed.days[0].gym.exercises[0].sets
    assert [s.reps for s in squat_sets] == [5, 7, 8]
    weights = [s.weight_kg for s in squat_sets]
    assert weights == [70.0, 55.0, 50.0]

    t3 = profile.zone_bpm_range("T3")
    assert t3 is not None
    assert tue.rowing.segments[0].hr_bpm_min == t3[0]


def test_lock_without_proposal_keeps_squad_splits():
    squad = _squad_with_pyramid_and_extra()
    locked = lock_athlete_plan_to_squad(squad, proposal_json=None, greeting="Jack,")
    parsed = parse_weekly_plan(locked)
    squad_plan = parse_weekly_plan(squad)
    assert parsed is not None and squad_plan is not None
    assert validate_athlete_plan_against_squad(parsed, squad_plan) is None
    tue = next(d for d in parsed.days if d.weekday == "Tuesday")
    assert tue.rowing is not None
    assert tue.rowing.segments[1].split_min == squad["days"][1]["rowing"]["segments"][1]["split_min"]
    assert parsed.personalised is True
    assert parsed.greeting == "Jack,"
