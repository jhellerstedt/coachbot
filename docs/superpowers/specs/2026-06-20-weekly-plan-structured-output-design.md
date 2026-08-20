# Weekly Plan Structured Output — Design Spec

**Date:** 2026-06-20  
**Status:** Approved

## Goal

Generate squad and athlete weekly training plans via OpenRouter structured outputs (`response_format.type = json_schema`), store validated JSON as source of truth, render human-readable `plan_text` for display, and migrate all consumers to read `plan_json` first.

## Decisions

| Topic | Choice |
|-------|--------|
| Storage | Dual: `plan_json` + rendered `plan_text` |
| Scope | Squad plan + athlete personalised plans |
| Failure handling | Retry structured call once; fall back to legacy prose-only generation |
| Consumers | All read `plan_json` first; `plan_text` is display-only |
| Module layout | New `erg_strava/weekly_plan_schema.py`; extend `openrouter_client.py` |

## JSON Schema

Top-level object:

```json
{
  "version": 1,
  "personalised": false,
  "greeting": null,
  "days": [ /* exactly 7, Monday–Sunday */ ]
}
```

### Day

| Field | Type | Notes |
|-------|------|-------|
| `weekday` | enum | Monday … Sunday |
| `date` | ISO date string | |
| `session_type` | enum | `gym`, `erg`, `on_water`, `rest`, `recovery` |
| `session_subtype` | string \| null | e.g. steady-state, threshold, intervals, race-pace |
| `gym` | object \| null | Required when `session_type == gym` |
| `rowing` | object \| null | Required when `session_type` is `erg` or `on_water` |
| `notes` | string \| null | Especially for on-water conditions |

### Gym session

- `category`: `leg` \| `upper_core` (Mon/Wed must differ)
- `goal`: `strength` \| `hypertrophy` \| `power` \| `recovery`
- `exercises`: exactly 4 items from the approved exercise pool
- Each exercise: `name` (enum), `sets`: `[{ reps, weight_kg|null, duration_sec|null }]`

### Rowing session

- `segments`: array (warm_up, main_set, cool_down, work, rest, build, active_recovery)
  - `phase`, `label`, `duration` (nullable), `split_min`, `split_max` (M:SS)
  - `zone_z` (Z1–Z5), `zone_t` (T1–T7)
  - `hr_bpm_min`, `hr_bpm_max`, `priority` (`split` \| `hr`)
  - `notes` (nullable; on-water flexibility)
- `erg_alternative`: nullable; on-water days only — time-based erg fallback with same segment shape

Athlete plans use the same schema with `personalised: true`, optional one-line `greeting`, and athlete-specific targets. Gym exercise names/order are fixed from the squad plan JSON.

## OpenRouter Integration

- `_post_chat` accepts optional `response_format`
- Structured generation uses `strict: true`
- Parse response JSON → validate via dataclasses → render `plan_text`
- On parse/validation failure: one retry, then legacy prose prompt

## Consumer Migration

| Consumer | JSON path |
|----------|-----------|
| `extract_session_for_date` | `session_for_date(plan_json, d)` + legacy text fallback |
| Gym rotation / previous-week context | `extract_gym_exercises_by_day_from_json` |
| Season planned metrics | `planned_metrics_from_plan_json` (no second LLM call) |
| Coach Q&A / post-log coaching | Session lookup via JSON |
| Athlete DMs | Squad JSON in; athlete JSON out |
| Zulip posts | `plan_text` (rendered) |

## Backward Compatibility

Cached plans without `plan_json` continue to work via existing text parsers. Prose-fallback weeks store `plan_json: null`.

## Testing

Unit tests for parse, validate, render, gym extraction, planned metrics, and legacy fallback.
