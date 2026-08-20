# Recommended Extra Erg Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional uncapped `recommended_erg` to weekly plans while keeping Tuesday/Thursday under the 45-minute cap.

**Architecture:** Parse/render a top-level `RecommendedErg` on `WeeklyPlan`. Library selection picks a third template (prefer over-cap) and injects it after LLM generation. Calendar-day cap, volume, and `session_for_date` stay seven-day only.

**Tech Stack:** Python 3, pytest, existing `weekly_plan_schema` / `session_library` / `weekly_plan_master_align`.

## Global Constraints

- Tuesday/Thursday must still pass `SESSION_CAP_MINUTES` (45).
- Recommended extra is not a calendar day and has no second duration cap.
- LLM JSON schema stays 7 days only; extra is injected after generation.
- Prescribed volume/adherence count only `plan.days`.
- `session_for_date` ignores `recommended_erg`.
- Weekend spill behaviour is unchanged.
- Render header: `Recommended extra erg (Fri/Sat if you have time):`

## Files

- Modify: `erg_strava/weekly_plan_schema.py` — `RecommendedErg`, parse/to_dict/render/`with_days`, personalize HR
- Modify: `erg_strava/session_library.py` — cap flag, recommended pick, inject into plan JSON
- Modify: `erg_strava/weekly_plan_master_align.py` — preserve extra on `WeeklyPlan` rebuilds
- Modify: `erg_strava/generate_training_plan.py` — copy extra onto athlete plans
- Modify: `erg_strava/tests/test_weekly_plan_schema.py`
- Modify: `erg_strava/tests/test_session_library.py`
- Modify: `erg_strava/tests/test_weekly_plan_master_align.py`

---

### Task 1: Schema round-trip and render

**Files:** `weekly_plan_schema.py`, `tests/test_weekly_plan_schema.py`

- [ ] Failing tests: parse/`to_dict` keep `recommended_erg`; malformed extra is dropped; render appends after Sunday and Friday stays rest; prescribed minutes ignore extra; over-cap extra does not fail `validate_plan_session_constraints` while over-cap Tuesday still does.
- [ ] Implement `RecommendedErg`, parse, `to_dict`, `render_plan_text`, `WeeklyPlan.with_days`.
- [ ] Tests pass.

### Task 2: Library selection and injection

**Files:** `session_library.py`, `tests/test_session_library.py`

- [ ] Failing tests: `enforce_cap=True` rejects over-cap; `False` accepts; `select_sessions_for_week` returns recommended ≠ Tue/Thu and can be over-cap; `apply_library_sessions_to_plan` writes `recommended_erg` + `session_library.recommended`; recent ids include recommended.
- [ ] Implement `validate_session_template(..., enforce_cap=True)`, `_pick_recommended_session`, extend `select_sessions_for_week` / `apply_library_sessions_to_plan` / `recent_session_ids_from_plan` / prompt.
- [ ] Tests pass.

### Task 3: Alignment preserve + athlete HR overlay

**Files:** `weekly_plan_master_align.py`, `generate_training_plan.py`, tests

- [ ] Failing tests: `_enforce_session_duration_caps` keeps `recommended_erg`; `personalize_recommended_erg` rewrites HR from profile, keeps splits.
- [ ] Use `with_days` on all master_align `WeeklyPlan` rebuilds; overlay athlete extra from squad after generation.
- [ ] Tests pass.

---
