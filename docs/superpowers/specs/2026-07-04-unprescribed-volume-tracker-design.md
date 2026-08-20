# Unprescribed Endurance Volume Tracker — Design Spec

**Date:** 2026-07-04  
**Status:** Approved

## Goal

Add a separate weekly tracker for unprescribed endurance volume so off-plan ergs, rides, and runs are visible in coaching output and weekly progress without changing the meaning of the existing prescribed-session tracker.

The current prescribed tracker remains the authoritative answer to "how much of the weekly rowing plan did this athlete complete as prescribed?" The new tracker answers "how much extra endurance did they log that was not matched to the weekly session plan?"

## Decisions

| Topic | Choice |
|-------|--------|
| Headline model | Keep prescribed progress unchanged; add separate unprescribed and combined totals |
| Included sources | Off-plan ergs, rides, and runs |
| Eligible days | Any day of the week |
| Matching rule | Any endurance activity not matched to a weekly planned session counts as unprescribed |
| Same-day extras | Extra same-day endurance beyond a matched planned session counts as unprescribed bonus volume |
| Persistence | No new ledger; derive from existing cached activity and merged-session data |
| Sync boundary | Use existing `erg_plot_last_run.json` cursor for "new activities since last run" checks |
| Zone mix | Keep rowing zone-mix lines tied to prescribed matched rowing only |

## User-Facing Output

Weekly progress becomes a dual-tracker block:

- `Prescribed rowing logged: X / Y min`
- `Unprescribed endurance: U min`
- `Total endurance logged incl. unprescribed: X + U / Y min`

Where:

- `X` is matched prescribed rowing minutes
- `Y` is the weekly prescribed rowing target from the plan
- `U` is off-plan endurance minutes from unmatched ergs, rides, and runs

Example:

```text
**Week zone volume** (personalised plan, through 2026-07-04):
- Prescribed rowing logged: 118 / 166 min (71% of prescribed)
- Unprescribed endurance: 45 min
- Total endurance logged incl. unprescribed: 163 / 166 min
- Z2/T2: 118 / 166 min (71% of prescribed)
- Logged rowing zone mix: Z2 100% (season week goal: Z2 ~85% of erg/row time)
```

The prescribed line remains unchanged so plan adherence stays explicit. The unprescribed line makes bonus work visible. The combined line answers "how much total endurance volume did they log relative to the prescribed target?"

## Activity Classification

Each weekly endurance activity is classified into one of two buckets:

### Prescribed

An activity, or part of an activity, counts as prescribed when it matches a weekly planned rowing session for that athlete and date.

For v1, only rowing sessions from the weekly plan can be matched as prescribed. Rides and runs do not match weekly plan slots because the current plan schema only prescribes rowing/on-water/gym/rest/recovery days.

### Unprescribed

An activity, or part of an activity, counts as unprescribed when:

- it is an erg, ride, or run with usable duration
- and it does not match any weekly planned rowing session

This includes:

- off-plan erg sessions on rest or recovery days
- rides and runs on any day
- same-day extra endurance beyond a matched planned rowing session

Examples:

- Friday top-up erg on a rest day: fully unprescribed
- Tuesday planned erg plus an extra easy ride later that day: erg may be prescribed, ride is unprescribed
- Thursday planned erg plus an additional erg later that day: matched planned portion stays prescribed; extra erg duration is unprescribed

## Matching Rules

Matching happens in two passes.

### Pass 1: Match planned rowing sessions

Use the existing structured-plan rowing matching flow as the base:

- session date from the weekly plan
- prescribed rowing segments for that day
- existing part-to-phase matching for warm-up / main set / cool-down
- current erg alternative logic for on-water days done on ergs

The existing prescribed tracker remains authoritative for matched rowing minutes.

### Pass 2: Classify unmatched endurance volume

After planned rowing has been matched:

- unmatched merged erg-session duration becomes unprescribed
- rides with usable duration become unprescribed
- runs with usable duration become unprescribed
- same-day extra endurance not consumed by the plan match becomes unprescribed

For v1, unprescribed is duration-based. Only prescribed matched rowing contributes to rowing zone buckets and rowing intensity mix.

## Source Of Truth

Do not add a new persisted "unprescribed activity ledger" in v1.

Instead, derive weekly totals from existing cached data:

- merged erg sessions for erg-specific matching and deduplication
- athlete activity index rows for non-erg endurance activities when Strava/index data is available
- widened lightweight Suunto workout visibility for non-erg endurance activities when Suunto is the upstream source
- existing weekly plan JSON for prescribed-session matching

This keeps the feature deterministic and avoids cache migration.

## Sync And "New Activity" Detection

The existing sync boundary in `erg_plot_last_run.json` should be reused for "new activities since last run" checks.

When the activity sync runs:

- inspect all activities since the last run, not only planned-session candidates
- classify new endurance activities as matched-to-plan or unprescribed
- make off-plan ergs, rides, and runs eligible for the new tracker immediately after sync

### Suunto-specific change

Current Suunto sync only persists tracked erg and gym workouts:

- `indoor_rowing_activity_ids`
- `gym_activity_ids`

For this feature, "new activity" detection must widen beyond those tracked workout types so rides and runs are discoverable when Suunto is the upstream source of truth.

However, deep sync work remains scoped:

- lightweight detection/listing should inspect all workouts since the last run
- FIT download and erg/gym-specific parsing can stay limited to the existing tracked workout types unless later features require more

This separates "activity is visible for unprescribed tracking" from "activity needs full erg/gym parsing."

## Module Impact

### `erg_strava/erg_prescription_compare.py`

Primary home of the new aggregation logic.

Add derived helpers for:

- prescribed matched rowing minutes
- unprescribed endurance minutes
- combined endurance total

`format_week_zone_volume_progress()` should render the new dual-tracker output while preserving existing rowing zone lines.

### `erg_strava/erg_session_merge.py`

Use merged erg sessions as the erg-side source for classifying:

- matched planned erg volume
- unmatched/off-plan erg volume

This avoids regressions from raw same-day erg-score dedupe and preserves the existing merged-session view.

### `erg_strava/suunto_sync.py`

Widen new-activity detection so rides and runs can surface for unprescribed tracking when Suunto is primary.

Do not expand full FIT/gym parsing scope in this change beyond what is necessary for visibility and duration accounting. A lightweight persisted summary for newly seen non-erg endurance workouts is acceptable if needed to make weekly classification deterministic.

### `erg_strava/strava_erg_hr_plot.py`

Reuse `erg_plot_last_run.json` as the incremental activity boundary and ensure the weekly pipeline sees newly discovered off-plan endurance activities after sync.

### `erg_strava/generate_training_plan.py`

Continue to include the deterministic weekly progress block in coaching prompts, now with:

- prescribed rowing progress
- unprescribed endurance minutes
- combined total

## Activity Type Scope

V1 scope is:

- erg sessions
- rides
- runs

Non-endurance sessions such as gym/strength remain outside the unprescribed endurance tracker.

If a candidate activity lacks reliable duration, ignore it rather than guessing.

## Edge Cases

- If there is no weekly plan cached, do not render prescribed/unprescribed weekly progress.
- If an erg session occurs on a rest/recovery day, count full duration as unprescribed.
- If a planned rowing session is only partially matched, matched minutes stay prescribed and unmatched endurance remainder becomes unprescribed.
- If a ride or run occurs on a day that also contains a prescribed rowing session, the ride/run remains unprescribed.
- If the same erg session is re-logged, existing merged-session/deduplication behavior should prevent double counting.

## Testing

Add focused unit coverage for:

- off-plan erg on a rest day contributes to unprescribed only
- ride contributes to unprescribed only
- run contributes to unprescribed only
- mixed week renders prescribed, unprescribed, and combined totals correctly
- same-day planned erg plus extra ride counts erg as prescribed and ride as unprescribed
- same-day planned erg plus extra erg counts matched portion as prescribed and extra duration as unprescribed
- rowing zone mix remains based on prescribed matched rowing only
- no double counting across merged erg sessions and indexed activities
- Suunto-driven new activity detection includes non-erg endurance activities since the last run

## Non-Goals

- changing the weekly plan schema to prescribe rides or runs
- changing the meaning of the existing prescribed tracker
- introducing a new persisted unprescribed-volume cache format
- folding rides/runs into rowing-specific zone-mix percentages
