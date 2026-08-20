# Warm-up/Cool-down Duration Matching & Weekly Volume DM Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Assign multi-screenshot WU/CD by prescribed duration when those differ, hide unprescribed lines mid-week, and prepend last-week volume into weekly athlete plan DMs.

**Architecture:** Extend `normalize_multi_screenshot_session` with optional prescribed WU/CD minutes; gate unprescribed bullets in `format_week_zone_volume_progress`; assemble weekly DM content via a small helper that wraps volume + plan.

**Tech Stack:** Python 3, existing erg_strava test suite (pytest)

**Spec:** `docs/superpowers/specs/2026-07-15-warmup-cooldown-and-weekly-volume-dm-design.md`

---

### Task 1: Duration-match WU/CD roles

**Files:**
- Modify: `erg_strava/erg_session_parts.py`
- Modify: `erg_strava/tests/test_erg_session_parts.py`
- Modify: `erg_strava/generate_training_plan.py` (pass prescribed mins)
- Possibly: `erg_strava/erg_prescription_compare.py` (helper to extract WU/CD mins from plan day)

- [x] Write failing test: unequal prescribed 21/14 vs swapped splits → roles follow duration
- [x] Write failing test: no prescribed kwargs → existing split behaviour
- [x] Implement bipartite duration match in role builder
- [x] Wire prescribed mins from plan into multi-screenshot normalize call site
- [x] Run `pytest erg_strava/tests/test_erg_session_parts.py -q`

### Task 2: Gate unprescribed mid-week lines

**Files:**
- Modify: `erg_strava/erg_prescription_compare.py`
- Modify: `erg_strava/tests/test_erg_prescription_compare.py`
- Modify: `coach_bot/handler.py` only if it needs an explicit flag (default False is enough)

- [x] Write failing test: `include_unprescribed=False` omits unprescribed lines when U > 0
- [x] Update existing unprescribed tests to pass `include_unprescribed=True`
- [x] Implement flag (default `False`)
- [x] Run prescription-compare tests

### Task 3: Prepend last-week volume into plan DM

**Files:**
- Modify: `erg_strava/generate_training_plan.py` (`send_weekly_athlete_plan_dms` + helper)
- Modify or create tests under `erg_strava/tests/`

- [x] Write failing test for DM assembly helper (volume header + bullets before plan header)
- [x] Implement helper; call from `send_weekly_athlete_plan_dms` with `include_unprescribed=True`
- [x] Run targeted pytest

### Task 4: Verify

- [x] Run `pytest erg_strava/tests/test_erg_session_parts.py erg_strava/tests/test_erg_prescription_compare.py -q` plus new DM helper test
- [ ] Stop for commit unless user asks to commit
