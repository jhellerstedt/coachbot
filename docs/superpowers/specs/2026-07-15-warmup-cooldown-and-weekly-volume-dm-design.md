# Warm-up/Cool-down Duration Matching & Weekly Volume DM — Design Spec

**Date:** 2026-07-15  
**Status:** Approved  
**Related:** `docs/superpowers/specs/2026-07-04-unprescribed-volume-tracker-design.md`

## Goal

Fix two coaching bugs observed on 2026-07-14:

1. Multi-screenshot erg logging swapped warm-up and cool-down when the plan prescribed different durations for those segments.
2. Mid-week coachbot replies always showed `Unprescribed endurance` (often `0 min`) even though ride/run and other non-explicitly-logged volume only becomes complete after the weekly sync. That summary should appear in the weekly athlete plan DM after the weekly run, not in day-of erg replies.

## Decisions

| Topic | Choice |
|-------|--------|
| WU/CD role rule | Prefer closest match to prescribed WU/CD minutes when those durations differ; else keep split heuristic |
| Where to fix roles | At normalize time (`erg_session_parts`), so persisted scores and zone accounting stay aligned |
| Mid-week zone block | Omit unprescribed + combined-endurance lines |
| Weekly volume delivery | Prepend deterministic last-week volume block into existing next-week plan DM |
| Volume computation | Reuse `format_week_zone_volume_progress` (no new LLM numbers) |
| Zero-log athletes | Keep existing plan-DM skip gate; no volume-only DM in this change |
| Historical rescoring | Out of scope; fix applies to new parses |

## Bug evidence (2026-07-14)

Squad Tuesday plan prescribed:

- Warm-up: **21 min**
- Main set: 35 min
- Cool-down: **14 min**

Logged score `33ae83db-dade-458b-90e9-605f9037806c` after normalize:

- Warm-up: **14 min** (slowest split)
- Main: 35 min
- Cool-down: **21 min**

Coachbot then compared each part to the wrong prescribed segment. Split-based normalization chose roles without consulting prescribed durations.

The same reply included mid-week week-zone lines with `Unprescribed endurance: 0 min`.

## Part 1 — Prescription-aware warm-up / cool-down roles

### Behaviour

In `normalize_multi_screenshot_session` / `_build_warmup_main_cooldown_roles`:

1. Identify the main piece as today (interval/structure heuristics or three-way split fallback).
2. For the remaining two pieces:
   - If optional prescribed warm-up minutes and cool-down minutes are both present **and unequal**, assign roles with a bipartite match: choose the pairing of the two non-main parts to `(warm_up, cool_down)` that minimizes the sum of absolute minute errors. If both pairings tie, fall back to the split heuristic for those two parts.
   - Otherwise keep the current split heuristic (slowest → warm-up, other → cool-down for the two non-main pieces; three-way split path unchanged when main is not structure-identified).

### Inputs

Call sites that already know the day’s plan (multi-screenshot Zulip erg parse) pass:

- `prescribed_warmup_min: Optional[float]`
- `prescribed_cooldown_min: Optional[float]`

Derived from that day’s plan JSON rowing (or erg-alternative) segments with phases `warm_up` and `cool_down`, using the same duration parsing already used for plan minutes elsewhere.

If plan context is unavailable, omit the kwargs and behaviour stays as today.

### Persistence

Corrected `session_parts` roles continue to be written on the erg score record. Assumption note may mention duration-based re-role when that path runs.

### Tests

- Fixture with unequal prescribed WU/CD (21 vs 14) and parts whose splits would swap them → roles follow prescribed durations.
- Equal prescribed WU/CD (or no prescribed kwargs) → existing split-based expectations still pass.
- Existing structure/main-interval tests remain green.

## Part 2 — Unprescribed lines only after weekly sync (DM)

### Mid-week coachbot

`format_week_zone_volume_progress` gains `include_unprescribed: bool = False` so day-of coachbot replies stay safe by default.

Coachbot day-of erg replies keep the default (`False`):

- Prescribed rowing logged
- Zone lines / mix
- **No** unprescribed or combined-endurance lines

Existing unit tests that assert unprescribed lines must pass `include_unprescribed=True`.

### Weekly athlete plan DM

After the weekly run has synced activities and while assembling each plan DM in `send_weekly_athlete_plan_dms`:

1. For each athlete who already qualifies for a plan DM, compute last-week volume via `format_week_zone_volume_progress(..., session_date=review_week.week_end, include_unprescribed=True)`.
2. Prepend that block, then the existing next-week plan header + body.

Header for the weekly DM volume block uses the review-week date range, not the mid-week “through {session_date}” title. Implementation may either:

- add an optional title override / `for_weekly_dm` formatting helper, or
- keep bullet lines from the formatter and wrap them with  
  `**Last week volume** ({week_start} – {week_end})`.

Example shape:

```text
**Last week volume** (2026-07-06 – 2026-07-12)
- Prescribed rowing logged: X / Y min (…)
- Unprescribed endurance: U min
- Total endurance logged incl. unprescribed: X+U / Y min
- Z2/T2: …
- Logged zone mix: …

**Your weekly plan** (2026-07-13 – 2026-07-19)
_Personalised session targets — squad averages are in the public topic._

<plan body>
```

Reuse the existing dual-tracker bullet wording from the unprescribed-volume tracker spec.

### Gates

Unchanged relative to today’s plan DMs:

- Skip if no `zulip_user_id` / `zulip_email`
- Skip if `athlete_has_week_training_log` is false for the review week

Volume-only DMs for zero-adherence athletes are explicitly out of scope.

### Tests

- `format_week_zone_volume_progress(..., include_unprescribed=False)` omits the two unprescribed lines even when unprescribed minutes would be non-zero.
- `include_unprescribed=True` still emits the dual-tracker lines (existing unprescribed tests update to pass the flag or rely on weekly default where appropriate).
- Weekly DM assembly test (or helper): volume block appears before the plan header; plan body unchanged.

## Module impact

| Module | Change |
|--------|--------|
| `erg_strava/erg_session_parts.py` | Duration-match WU/CD when prescribed minutes differ |
| `erg_strava/generate_training_plan.py` | Pass prescribed WU/CD mins into normalize; prepend volume in `send_weekly_athlete_plan_dms` |
| `erg_strava/erg_prescription_compare.py` | `include_unprescribed` on `format_week_zone_volume_progress`; helper to read prescribed WU/CD minutes from plan day if needed |
| `coach_bot/handler.py` | Keep default mid-week call (no unprescribed lines) |
| `erg_strava/tests/test_erg_session_parts.py` | Duration-match cases |
| `erg_strava/tests/test_erg_prescription_compare.py` | Flag / DM-related assertions |

## Non-goals

- Re-normalizing historical erg scores already in cache
- Changing public squad weekly adherence copy beyond existing weekly report
- Sending a separate volume DM
- LLM-authored volume numbers (`get_kagi_athlete_weekly_compliance_dm` remains unused for this feature)

## Success criteria

1. Logging a session with unequal prescribed WU/CD durations assigns parts by duration even when splits would swap them.
2. Coachbot erg replies no longer show mid-week unprescribed `0 min` noise.
3. After the weekly run, each athlete who receives a plan DM sees last-week prescribed + unprescribed volume above the next-week plan.
