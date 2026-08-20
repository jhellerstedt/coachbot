"""Tests for erg session library."""

from __future__ import annotations

from session_library import (
    apply_library_sessions_to_plan,
    ensure_curated_seed_files,
    load_all_sessions,
    recent_session_ids_from_plan,
    select_sessions_for_week,
    validate_session_template,
)
from weekly_plan_schema import SESSION_CAP_MINUTES


def test_curated_sessions_validate():
    ensure_curated_seed_files()
    sessions = load_all_sessions()
    assert len(sessions) >= 12
    ids = {template.id for template in sessions}
    for sid in (
        "vo2-8x500",
        "threshold-3x10",
        "z2-30-continuous",
        "vo2-6x3min",
        "anaerobic-10x1min",
        "z3-2x15",
        "threshold-4x2k",
        "pyramid-1k-2k-2k-1k",
    ):
        assert sid in ids
    for template in sessions:
        err = validate_session_template(template)
        if template.total_minutes > SESSION_CAP_MINUTES:
            assert err is not None
            assert "cap" in err, f"{template.id}: expected cap error, got {err}"
        else:
            assert err is None, f"{template.id}: {err}"


def test_select_sessions_for_week_picks_two_different():
    selections = select_sessions_for_week(phase="build", recent_session_ids=[])
    assert "tuesday" in selections
    assert "thursday" in selections
    assert selections["tuesday"].id
    assert selections["thursday"].id


def test_select_sessions_avoids_recent_ids():
    recent = ["threshold-4x5", "threshold-3x6", "threshold-4x4", "z4-5x4"]
    selections = select_sessions_for_week(phase="build", recent_session_ids=recent)
    assert selections["tuesday"].id not in recent


def test_apply_library_sessions_to_plan_sets_metadata():
    from test_weekly_plan_schema import sample_squad_plan_dict

    plan = sample_squad_plan_dict()
    selections = select_sessions_for_week(phase="base")
    patched = apply_library_sessions_to_plan(plan, selections)
    assert patched["session_library"]["tuesday"] == selections["tuesday"].id
    assert patched["session_library"]["thursday"] == selections["thursday"].id
    tue = next(d for d in patched["days"] if d["weekday"] == "Tuesday")
    main = next(s for s in tue["rowing"]["segments"] if s["phase"] == "main_set")
    lib_main = next(
        s for s in selections["tuesday"].rowing["segments"] if s["phase"] == "main_set"
    )
    assert main["duration"] == lib_main["duration"]


def test_recent_session_ids_from_plan():
    ids = recent_session_ids_from_plan(
        {"session_library": {"tuesday": "z2-steady-25", "thursday": "z3-3x8"}}
    )
    assert ids == ["z2-steady-25", "z3-3x8"]
