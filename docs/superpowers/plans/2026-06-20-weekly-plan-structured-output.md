# Weekly Plan Structured Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** OpenRouter JSON-schema weekly plans for squad + athletes, dual storage, JSON-first consumers.

**Architecture:** New `weekly_plan_schema.py` holds schema, dataclasses, parse/validate, render, and metrics. `openrouter_client.py` gains `response_format`. `generate_training_plan.py` orchestrates structured generation with retry + prose fallback.

**Tech Stack:** Python 3, requests, OpenRouter chat completions, stdlib json/dataclasses/re.

**Spec:** `docs/superpowers/specs/2026-06-20-weekly-plan-structured-output-design.md`

---

### Task 1: Core schema module

**Files:**
- Create: `erg_strava/weekly_plan_schema.py`
- Create: `erg_strava/tests/test_weekly_plan_schema.py`

Implement dataclasses, `WEEKLY_PLAN_JSON_SCHEMA`, parse/validate, `render_plan_text`, `session_for_date`, gym extraction, `planned_metrics_from_plan_json`.

### Task 2: OpenRouter structured calls

**Files:**
- Modify: `erg_strava/openrouter_client.py`

Add `response_format` to `_post_chat` / `call_openrouter`.

### Task 3: Structured plan generation

**Files:**
- Modify: `erg_strava/generate_training_plan.py`

- Add `plan_json` to `WeeklyPlanRecord` and athlete cache
- `generate_squad_weekly_plan` / `generate_athlete_weekly_plan` with retry + fallback
- Rename legacy text generators to `*_prose`

### Task 4: Consumer migration

**Files:**
- Modify: `erg_strava/generate_training_plan.py`, `erg_strava/season_master_plan.py`, `coach_bot/handler.py`

JSON-first session lookup; season metrics from JSON; coach bot uses unified helper.

### Task 5: Tests and verification

**Files:**
- `erg_strava/tests/test_weekly_plan_schema.py`

Run: `python -m pytest erg_strava/tests/test_weekly_plan_schema.py -v`
