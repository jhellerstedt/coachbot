# Recommended Extra Erg Session — Design Spec

**Date:** 2026-08-20  
**Status:** Approved  
**Related:** curated erg library in `erg_strava/data/erg_sessions/curated/`

## Goal

Keep Tuesday and Thursday prescribed ergs at or under the 45-minute session cap, while offering one optional longer library session for the week that is not bound to a calendar day and is not capped.

## Decisions

| Topic | Choice |
|-------|--------|
| Placement | Top-level `recommended_erg` on `WeeklyPlan`, not a Friday/Saturday day |
| Tuesday/Thursday | Still must pass `SESSION_CAP_MINUTES` (45) |
| Recommended extra | Same library; may exceed 45 minutes; no second duration cap |
| LLM JSON schema | Unchanged (7 days only); extra is injected after generation |
| Volume / adherence | Count only the seven calendar days; skipping the extra does not fail the week |
| Date lookup | `session_for_date` ignores `recommended_erg` (it has no weekday) |
| Weekend spill | Existing Fri/Sat cap-spill path is unchanged |

## Context

`select_sessions_for_week` currently returns only `tuesday` and `thursday`. `validate_session_template` rejects any library piece whose estimated minutes exceed 45, so longer catalog sessions (`z2-30-continuous`, `z3-2x15`, `threshold-3x10`, `threshold-4x2k`) never appear in weekly plans.

The fixed morning schedule stays: Monday gym, Tuesday erg, Wednesday gym, Thursday erg or on-water, Friday/Saturday rest, Sunday rest or recovery.

## Data model

```python
@dataclass
class RecommendedErg:
    id: str
    name: str
    rowing: RowingSession
```

`WeeklyPlan` gains `recommended_erg: Optional[RecommendedErg] = None`.

Plan JSON:

```json
"recommended_erg": {
  "id": "threshold-4x2k",
  "name": "4×2000 m threshold",
  "rowing": { "segments": [ ... ], "erg_alternative": null }
}
```

`session_library` on the plan dict also stores `"recommended": "<id>"` next to `tuesday` / `thursday`, so `recent_session_ids_from_plan` can avoid repeating the extra next week.

Missing or malformed `recommended_erg` on old cached plans parses as `None`; the rest of the plan is still valid.

`WEEKLY_PLAN_JSON_SCHEMA` does **not** include this field. OpenRouter strict output stays seven days. Python `parse_weekly_plan` / `weekly_plan_to_dict` do include it so alignment and cache round-trips cannot drop it.

## Selection

`select_sessions_for_week` returns `{"tuesday", "thursday", "recommended"}`. `recommended` may be omitted when no eligible template exists.

Tuesday / Thursday:

- Unchanged day-role and subtype preference maps.
- `validate_session_template(template, enforce_cap=True)` (today’s behaviour).

Recommended extra:

- Same phase filter.
- Exclude the chosen Tuesday and Thursday ids and `recent_session_ids` (including last week’s recommended id).
- `validate_session_template(template, enforce_cap=False)` — still requires a parseable rowing session, WU/CD ≤ 15 min, interval rest, and Z/T coherence.
- Rank: over-cap first (`total_minutes > 45`), then recommended subtype preference, then longer duration, then id.
- Recommended subtype preference: deload/recovery → steady-state; base → steady-state, intervals, threshold; build → threshold, intervals, vo2, steady-state; peak → threshold, vo2, intervals.

No new `day_role` tags are required on catalog JSON.

## Validation

`validate_plan_session_constraints` continues to apply the 45-minute cap only to `days` with `session_type` in `{erg, on_water}`. It does not cap `recommended_erg`.

If `recommended_erg` is present, validate its rowing the same way as an erg day except duration cap: WU/CD caps, interval rest, Z/T. Failure of that extra must not invalidate the seven-day plan; omit the extra instead.

`_enforce_session_duration_caps` in `weekly_plan_master_align.py` still trims only calendar days. Every `WeeklyPlan(...)` rebuild in that module must copy `recommended_erg` through (helper: replace days, keep other fields).

## Injection and personalisation

1. Squad LLM emits Mon–Sun only.
2. `apply_library_sessions_to_plan` writes Tuesday/Thursday segments as today, plus `recommended_erg` and `session_library.recommended`.
3. `render_plan_text` includes the extra.
4. Athlete LLM schema still has no extra field. After athlete generation, copy `recommended_erg` from the squad plan (same id, name, and segment structure). Rewrite each segment’s `hr_bpm_min` / `hr_bpm_max` from `AthleteProfile.zone_bpm_range` using `zone_z` (and `zone_t` when a T-band exists). Leave splits unchanged from the squad/library template.
5. Interval-repair and season alignment round-trip through `parse_weekly_plan` / `weekly_plan_to_dict`, which preserve `recommended_erg`.

If structure validation fails after injection, drop `recommended_erg` and continue.

## Display

After the Sunday block, `render_plan_text` appends:

```
Recommended extra erg (Fri/Sat if you have time):
<name>
  Warm Up: ...
  Main Set: ...
  Cool Down: ...
```

Reuse `_format_rowing_session` so segment lines match calendar erg days. This is optional copy, not a weekday header.

`format_session_library_prompt` tells the model Tuesday/Thursday **must** use those library ids, and that a recommended extra will be attached after generation (do not put it on Tuesday/Thursday or invent Friday).

## Volume and logging

`planned_metrics_from_plan_json`, `format_plan_prescribed_summary`, and `prescribed_rowing_minutes_by_zone` iterate `plan.days` only. They must not add `recommended_erg`.

A Friday erg log still compares to Friday’s calendar day (rest, unless weekend spill created a real Friday session). Matching logs to `recommended_erg` is out of scope.

## Tests

- `select_sessions_for_week` returns a recommended id distinct from Tuesday/Thursday.
- Over-cap catalog ids can be recommended and must not be selected for Tuesday/Thursday.
- `validate_session_template(..., enforce_cap=True)` still fails over-cap; `enforce_cap=False` accepts them if otherwise valid.
- Parse / `to_dict` round-trip keeps `recommended_erg`.
- `render_plan_text` includes the extra after Sunday and does not add a Friday weekday session.
- `validate_plan_session_constraints` still errors when Tuesday exceeds 45 minutes and does not error when only `recommended_erg` exceeds 45.
- Alignment helpers that rebuild `WeeklyPlan` do not drop `recommended_erg`.
- Prescribed minute totals ignore the extra.
- Athlete overlay keeps extra structure and rewrites HR from the profile when zone bands exist.

## Out of scope

- Raising `SESSION_CAP_MINUTES` for Tuesday/Thursday.
- Scheduling the extra as Friday or Saturday.
- Using `recommended_erg` for `session_for_date` or adherence matching.
- Changing weekend volume-spill behaviour.
- Recalibrating catalog splits to each athlete’s 2K.
