# Maintenance Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A config flag makes the Sunday job exit immediately, and an athlete request syncs that athlete and saves one prescribed day.

**Architecture:** Pure helpers in `erg_strava/maintenance_mode.py` classify requests, validate a single `DayPlan`, and write it onto the athlete's personalised week file. The bot calls those helpers. `strava_erg_hr_plot.main` exits before sync when the flag is on.

**Tech Stack:** Python 3, pytest, existing weekly-plan schema, OpenRouter, Zulip.

## Global Constraints

- `maintenance_mode` is on only for boolean `true` or the strings `true`, `yes`, `on`, `1` (any case).
- Sunday print line: `Maintenance mode is on; skipping weekly sync and plan generation.` then exit 0.
- Paused plan reply: `Weekly plans are paused. Ask for a single session, for example: gym session, leg day, 45min.`
- Empty history reply: `I don't have any recent {gym|erg} sessions cached for {label}, so I can't set loads or targets. Log a session and ask again.`
- Failure reply: `I couldn't write that session. Try the request again.`
- Sync notice: `Syncing your latest sessions…`
- Sync-failed line: `Sync failed; this session uses your cached data.`
- Saved line: `Saved as today's prescription.`
- Squad week file is never written. `maintenance_days` records ISO dates once.

## Files

- Create: `erg_strava/maintenance_mode.py`
- Create: `erg_strava/tests/test_maintenance_mode.py`
- Create: `coach_bot/tests/test_maintenance_mode.py`
- Modify: `erg_strava/strava_erg_hr_plot.py` — exit after config load
- Modify: `coach_bot/handler.py` — session path, Q&A without a squad plan, no enqueue
- Modify: `erg_strava/config.example.yaml`, `coach_bot/docker-compose.yml`, `deploy/README.md`

---

### Task 1: Flag, classification, validation, save

- [x] Failing tests in `erg_strava/tests/test_maintenance_mode.py` for the flag, the three example prompts, incomplete requests, question/log rejection, save/replace, prescription lookup, sync failure, empty history, and invalid model output.
- [x] Implement `maintenance_mode.py` until those tests pass.

### Task 2: Sunday exit

- [x] Test `main` exits 0 before `sync_athlete` when the flag is on, and reaches sync when it is off.
- [x] Insert the exit in `strava_erg_hr_plot.main` immediately after the raw YAML load.

### Task 3: Bot wiring

- [x] Tests: plan adjustment does not append `pending.jsonl`; a question does not return `missing_plan_reply`; a complete request posts the syncing line.
- [x] Call the maintenance helper from private and stream handling before `_reply_kagi`. In maintenance mode, coaching uses a stub plan when the squad file is missing, includes today's saved day, and does not enqueue plan adjustments.

### Task 4: Docs and compose

- [x] Comment `maintenance_mode` in `config.example.yaml`.
- [x] Mount `suuntool` and the Suunto session in `docker-compose.yml` and document them in `deploy/README.md`.
