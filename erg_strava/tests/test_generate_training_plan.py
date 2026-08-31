"""Focused tests for weekly plan post formatting and interval repair."""

from __future__ import annotations

import json

from athlete_profile import AthleteProfile
from generate_training_plan import (
    WeeklyPlanRecord,
    _validate_parsed_weekly_plan,
    ensure_realistic_interval_sessions,
    format_public_weekly_plan_post,
)
from weekly_plan_schema import parse_weekly_plan, validate_rowing_interval_rest, weekly_plan_to_dict
from test_weekly_plan_schema import sample_squad_plan_dict


def test_athlete_generation_validation_forwards_profile():
    data = sample_squad_plan_dict()
    data["personalised"] = True
    data["days"][1]["rowing"]["segments"][1].update({
        "hr_bpm_min": 100,
        "hr_bpm_max": 110,
    })
    plan = parse_weekly_plan(data)
    assert plan is not None

    err = _validate_parsed_weekly_plan(
        plan,
        include_lifting=True,
        athlete_profile=AthleteProfile(id=1, label="Test", max_hr_bpm=182),
    )

    assert err is not None
    assert "HR" in err or "bpm" in err.lower()


def test_format_public_weekly_plan_post_omits_goal_tracking_section():
    record = WeeklyPlanRecord(
        week_id="2026-07-20_2026-07-26",
        week_start="2026-07-20",
        week_end="2026-07-26",
        plan_text="Monday:\nrest",
        plan_json=None,
        generated_at="2026-07-22T00:00:00+00:00",
        training_summary="summary",
        include_lifting=True,
        adherence_review="Two short sentences.",
        goal_tracking="### Progress Snapshot\n\nThis should not be posted.",
        gym_tonnage_summary="tonnage",
    )

    post = format_public_weekly_plan_post(record)

    assert "=== Previous week adherence ===" in post
    assert "=== Season goal tracking ===" not in post
    assert "Progress Snapshot" not in post
    assert "=== Squad weekly plan (2026-07-20 – 2026-07-26) ===" in post


def test_ensure_realistic_interval_sessions_repairs_flat_tuesday(monkeypatch):
    data = sample_squad_plan_dict()
    tuesday = data["days"][1]
    tuesday["session_subtype"] = "intervals"
    tue_main = tuesday["rowing"]["segments"][1]
    tue_main["label"] = "Main Set: Threshold intervals — 40 min"
    tue_main["duration"] = "Threshold intervals — 40 min"
    tue_main["zone_z"] = "Z4"
    tue_main["zone_t"] = "T6"

    repaired_main = {
        "phase": "main_set",
        "label": "Threshold intervals",
        "duration": "5×8 min / 2 min rest",
        "split_min": "2:05",
        "split_max": "2:12",
        "zone_z": "Z4",
        "zone_t": "T6",
        "hr_bpm_min": 140,
        "hr_bpm_max": 150,
        "priority": "hr",
        "notes": None,
    }

    def fake_llm(system, user, api_key, **kwargs):
        return json.dumps(
            {
                "session_subtype": "intervals",
                "segments": [
                    tuesday["rowing"]["segments"][0],
                    repaired_main,
                    tuesday["rowing"]["segments"][2],
                ],
            }
        )

    monkeypatch.setattr("generate_training_plan._call_llm", fake_llm)
    out = ensure_realistic_interval_sessions(data, api_key="test-key", max_attempts=3)
    plan = parse_weekly_plan(out)
    assert plan is not None
    tue = next(d for d in plan.days if d.weekday == "Tuesday")
    assert validate_rowing_interval_rest(tue) is None
    main = next(s for s in tue.rowing.segments if s.phase == "main_set")
    assert "5×8" in (main.duration or "") or "5x8" in (main.duration or "").lower()


def test_apply_season_alignment_rejects_unvalidated_flat_interval_prose(tmp_path, monkeypatch):
    from datetime import date
    from generate_training_plan import apply_season_master_plan_alignment, week_bounds_from_monday

    week = week_bounds_from_monday(date(2026, 7, 20))
    prose = (
        "Tuesday:\nerg\n"
        "  Warm Up: Warm-up — 20 min @ Z1/T1, split 2:15–2:25, HR 120–140 bpm, priority: hr\n"
        "  Main Set: Threshold intervals — 40 min @ Z4/T6, split 2:05–2:15, HR 140–150 bpm, priority: hr\n"
        "  Cool Down: Cool-down — 10 min @ Z1/T1, split 2:20–2:30, HR 120–135 bpm, priority: hr\n"
    )

    # Avoid needing a real season config: force import path with season_cfg None
    # and assert the import helper validates before accepting.
    plan_json, log = apply_season_master_plan_alignment(
        tmp_path,
        None,
        week,
        None,
        plan_text=prose,
        plan_label="Squad weekly plan",
    )
    # Without season config, alignment is skipped after import — but import must
    # still reject invalid interval structure.
    assert plan_json is None or (
        parse_weekly_plan(plan_json) is not None
        and validate_rowing_interval_rest(
            next(d for d in parse_weekly_plan(plan_json).days if d.weekday == "Tuesday")
        )
        is None
    )
    if plan_json is None:
        assert "could not import" in log or "invalid" in log.lower() or "interval" in log.lower() or "alignment skipped" in log


def test_rowing_zone_templates_in_structured_and_repair_prompts():
    from generate_training_plan import (
        _INTERVAL_SESSION_REPAIR_SYSTEM,
        _ROWING_ZONE_SESSION_TEMPLATES,
        _STRUCTURED_PLAN_JSON_RULES,
    )

    assert "4×5 min" in _ROWING_ZONE_SESSION_TEMPLATES or "3×6 min" in _ROWING_ZONE_SESSION_TEMPLATES
    assert (
        _ROWING_ZONE_SESSION_TEMPLATES in _STRUCTURED_PLAN_JSON_RULES
        or _ROWING_ZONE_SESSION_TEMPLATES[:40] in _STRUCTURED_PLAN_JSON_RULES
    )
    assert (
        _ROWING_ZONE_SESSION_TEMPLATES in _INTERVAL_SESSION_REPAIR_SYSTEM
        or _ROWING_ZONE_SESSION_TEMPLATES[:40] in _INTERVAL_SESSION_REPAIR_SYSTEM
    )


def test_today_session_recovery_gate_does_not_rewrite_plan_json():
    from datetime import date
    from generate_training_plan import _today_session_extra_blocks

    plan = sample_squad_plan_dict()
    original = json.dumps(plan)
    monday_sets = len(plan["days"][0]["gym"]["exercises"][0]["sets"])
    record = WeeklyPlanRecord(
        week_id="2026-06-15_2026-06-21",
        week_start="2026-06-15",
        week_end="2026-06-21",
        plan_text="Monday:\ngym\n",
        generated_at="2026-06-14T00:00:00+00:00",
        training_summary="",
        include_lifting=True,
        plan_json=plan,
    )
    blocks = _today_session_extra_blocks(
        record, date(2026, 6, 15), "slept terribly and feel wrecked"
    )
    assert any("recovery-gated" in b for b in blocks)
    assert any("does not change the weekly program" in b for b in blocks)
    assert json.dumps(record.plan_json) == original
    assert len(plan["days"][0]["gym"]["exercises"][0]["sets"]) == monday_sets


def test_lifting_clause_does_not_ask_llm_to_rotate():
    from generate_training_plan import _weekly_plan_lifting_clause

    text = _weekly_plan_lifting_clause(True, phase="base")
    assert "rotate at least" not in text.lower()
    assert "gym program" in text.lower()


def test_squad_plan_recovers_json_when_llm_returns_prose_only(monkeypatch):
    from datetime import date

    from generate_training_plan import (
        GeneratedWeeklyPlan,
        generate_squad_weekly_plan,
        week_bounds_from_monday,
    )

    monkeypatch.setattr(
        "generate_training_plan._generate_structured_plan_with_fallback",
        lambda *a, **k: GeneratedWeeklyPlan(
            plan_text="Sunday: rowing", plan_json=None
        ),
    )
    out = generate_squad_weekly_plan(
        "summary",
        "tok",
        plan_week=week_bounds_from_monday(date(2026, 8, 31)),
        phase="build",
        prev_plan_json={"gym_program": {"id": "build-a", "week_index": 0}},
    )
    assert out.plan_json is not None
    assert parse_weekly_plan(out.plan_json) is not None
    assert "Sunday: rowing" not in out.plan_text


def test_send_weekly_athlete_plan_dms_sends_recovered_squad_json(
    monkeypatch, tmp_path
):
    from datetime import date

    from generate_training_plan import (
        GeneratedWeeklyPlan,
        PipelineAthleteCfg,
        generate_squad_weekly_plan,
        send_weekly_athlete_plan_dms,
        week_bounds_from_monday,
    )

    monkeypatch.setattr(
        "generate_training_plan._generate_structured_plan_with_fallback",
        lambda *a, **k: GeneratedWeeklyPlan(
            plan_text="Sunday: rowing", plan_json=None
        ),
    )
    week = week_bounds_from_monday(date(2026, 8, 31))
    recovered = generate_squad_weekly_plan(
        "summary", "tok", plan_week=week, phase="build"
    )
    monkeypatch.setattr(
        "generate_training_plan.generate_athlete_weekly_plan",
        lambda *a, **k: recovered,
    )
    monkeypatch.setattr(
        "erg_session_merge.athlete_has_week_training_log",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "erg_session_merge.format_athlete_week_training_log",
        lambda *a, **k: "log",
    )
    monkeypatch.setattr(
        "erg_prescription_compare.format_last_week_volume_for_dm",
        lambda *a, **k: "",
    )
    sent: list[str] = []
    monkeypatch.setattr(
        "send_to_zulip.send_private_message_to_zulip",
        lambda content, to, **kwargs: sent.append(content) or {"result": "success"},
    )

    count = send_weekly_athlete_plan_dms(
        tmp_path,
        [PipelineAthleteCfg(id=1, label="Jack H", zulip_user_id=73)],
        recovered.plan_text,
        week,
        week,
        {},
        {},
        "tok",
        squad_plan_json=recovered.plan_json,
    )

    assert count == 1
    assert sent


def test_generate_athlete_plan_locks_when_llm_returns_prose_only(monkeypatch):
    from datetime import date

    from generate_training_plan import GeneratedWeeklyPlan, generate_athlete_weekly_plan, week_bounds_from_monday
    from test_weekly_plan_schema import _recommended_erg_dict
    from weekly_plan_schema import validate_athlete_plan_against_squad

    squad = sample_squad_plan_dict()
    squad["recommended_erg"] = _recommended_erg_dict()

    def fake_fallback(*args, **kwargs):
        return GeneratedWeeklyPlan(
            plan_text="### Monday, 2026-06-15\n**Session Type:** Gym\n",
            plan_json=None,
        )

    monkeypatch.setattr(
        "generate_training_plan._generate_structured_plan_with_fallback",
        fake_fallback,
    )
    result = generate_athlete_weekly_plan(
        "Jack H",
        "summary",
        "tok",
        squad_plan_json=squad,
        athlete_profile=AthleteProfile(id=1, label="Jack H", max_hr_bpm=185),
        plan_week=week_bounds_from_monday(date(2026, 6, 15)),
    )
    assert result.plan_json is not None
    parsed = parse_weekly_plan(result.plan_json)
    squad_plan = parse_weekly_plan(squad)
    assert parsed is not None and squad_plan is not None
    assert validate_athlete_plan_against_squad(parsed, squad_plan) is None
    assert not result.plan_text.strip().startswith("### Monday")


def test_send_weekly_dm_locks_instead_of_prose_when_json_missing(
    monkeypatch, tmp_path
):
    from datetime import date

    from generate_training_plan import (
        GeneratedWeeklyPlan,
        PipelineAthleteCfg,
        send_weekly_athlete_plan_dms,
        week_bounds_from_monday,
    )
    from test_weekly_plan_schema import _recommended_erg_dict

    squad = sample_squad_plan_dict()
    squad["recommended_erg"] = _recommended_erg_dict()
    sent: list[str] = []

    monkeypatch.setattr(
        "generate_training_plan.generate_athlete_weekly_plan",
        lambda *args, **kwargs: GeneratedWeeklyPlan(
            plan_text="### Monday, 2026-06-15\n**Session Type:** Gym\n",
            plan_json=None,
        ),
    )
    monkeypatch.setattr(
        "erg_session_merge.athlete_has_week_training_log",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "erg_session_merge.format_athlete_week_training_log",
        lambda *args, **kwargs: "log",
    )
    monkeypatch.setattr(
        "erg_prescription_compare.format_last_week_volume_for_dm",
        lambda *args, **kwargs: "",
    )
    monkeypatch.setattr(
        "send_to_zulip.send_private_message_to_zulip",
        lambda content, to, **kwargs: sent.append(content) or {"result": "success"},
    )

    week = week_bounds_from_monday(date(2026, 6, 15))
    athlete = PipelineAthleteCfg(
        id=1,
        label="Jack H",
        zulip_user_id=73,
        max_hr_bpm=185,
    )
    n = send_weekly_athlete_plan_dms(
        tmp_path,
        [athlete],
        "squad text",
        week,
        week,
        {},
        {},
        "tok",
        squad_plan_json=squad,
    )
    assert n == 1
    assert sent
    assert "### Monday" not in sent[0]
    assert "Monday:" in sent[0]
