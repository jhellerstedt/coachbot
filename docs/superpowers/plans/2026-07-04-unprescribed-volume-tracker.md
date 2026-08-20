# Unprescribed Endurance Volume Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add weekly `unprescribed endurance` and `total endurance incl. unprescribed` tracking so off-plan ergs, rides, and runs count toward weekly volume visibility without changing the meaning of prescribed rowing progress.

**Architecture:** Keep the existing prescribed rowing tracker authoritative, but derive weekly progress from two sources: merged erg sessions for rowing/session matching and weekly endurance activities for unmatched rides/runs. Add a lightweight widening of Suunto visibility so non-erg endurance workouts can be seen since the last run without expanding full FIT parsing beyond erg/gym.

**Tech Stack:** Python 3, pytest, stdlib dataclasses/json/datetime, existing erg/Strava/Suunto cache formats.

**Spec:** `docs/superpowers/specs/2026-07-04-unprescribed-volume-tracker-design.md`

---

### Task 1: Lock down weekly-progress behavior with failing tests

**Files:**
- Modify: `erg_strava/tests/test_erg_prescription_compare.py`

- [ ] **Step 1: Add plan/activity fixtures for off-plan endurance**

Add helpers for:

```python
def _mixed_week_personal_plan() -> dict: ...
def _friday_topup_erg_record() -> dict: ...
def _same_day_extra_erg_record() -> dict: ...
def _ride_activity(day_date: str, minutes: int = 45) -> dict: ...
def _run_activity(day_date: str, minutes: int = 30) -> dict: ...
```

The plan should include:
- Tuesday planned erg
- Thursday planned erg or on-water with erg alternative
- Friday/Saturday rest
- Sunday recovery

- [ ] **Step 2: Write failing tests for unprescribed endurance**

Add focused tests:

```python
def test_week_zone_progress_counts_rest_day_topup_erg_as_unprescribed(tmp_path: Path): ...
def test_week_zone_progress_counts_ride_and_run_as_unprescribed(tmp_path: Path): ...
def test_week_zone_progress_counts_same_day_extra_erg_as_unprescribed_bonus(tmp_path: Path): ...
```

Assertions should verify:
- the prescribed rowing line stays unchanged
- `Unprescribed endurance: ... min` appears
- `Total endurance logged incl. unprescribed: ... / ... min` appears
- rides/runs do not change rowing zone lines

- [ ] **Step 3: Add a failing test for widened weekly activity collection**

Add one focused regression test around weekly collection:

```python
def test_collect_activities_in_weeks_includes_non_erg_endurance_rows(tmp_path: Path): ...
```

Set up cached activity/index or Suunto-visible workout rows so a ride/run in the target week is returned by the weekly collection path.

- [ ] **Step 4: Run the targeted tests and verify RED**

Run:

```bash
python -m pytest erg_strava/tests/test_erg_prescription_compare.py -v
```

Expected:
- new tests fail because unprescribed tracker lines do not exist yet
- existing tests continue to pass or fail only where the new behavior is being asserted

### Task 2: Implement dual weekly-progress aggregation

**Files:**
- Modify: `erg_strava/erg_prescription_compare.py`
- Modify: `erg_strava/erg_session_merge.py` (only if a small helper/export improves reuse; avoid unrelated refactors)

- [ ] **Step 1: Implement helpers for prescribed and unprescribed weekly minutes**

Add helpers along these lines:

```python
def _merged_session_total_minutes(session: Mapping[str, Any]) -> int: ...
def _prescribed_and_unprescribed_from_merged_session(
    session: Mapping[str, Any],
    plan_json: Optional[Mapping[str, Any]],
) -> Dict[str, int]: ...
def _is_unprescribed_endurance_activity(act: Mapping[str, Any]) -> bool: ...
def _activity_endurance_minutes(act: Mapping[str, Any]) -> int: ...
```

Rules:
- prescribed minutes come from existing rowing-segment matching
- unmatched remainder from a merged erg session becomes unprescribed
- rides/runs with usable duration are fully unprescribed
- clamp negative remainders to zero

- [ ] **Step 2: Switch weekly progress from raw erg-score logs to merged sessions + activities**

Update `format_week_zone_volume_progress()` to:
- keep `prescribed = prescribed_rowing_minutes_by_zone(plan_json)`
- load merged erg sessions for the week
- sum prescribed rowing buckets from matched merged sessions
- sum unprescribed endurance minutes from unmatched merged-erg remainder plus ride/run activities
- render:

```python
lines = [
    f"**Week zone volume** ({source}, through {session_date.isoformat()}):",
    f"- Prescribed rowing logged: {actual['total']} / {prescribed['total']} min ...",
    f"- Unprescribed endurance: {unprescribed_total} min",
    f"- Total endurance logged incl. unprescribed: {actual['total'] + unprescribed_total} / {prescribed['total']} min",
]
```

- [ ] **Step 3: Keep rowing zone mix scoped to prescribed matched rowing**

Ensure:
- `Z2/T2`, `Z5/T5`, `Other zones`, and rowing mix still use only prescribed matched rowing minutes
- rides/runs never contribute to rowing zone buckets

- [ ] **Step 4: Run tests and verify GREEN**

Run:

```bash
python -m pytest erg_strava/tests/test_erg_prescription_compare.py -v
```

Expected:
- all tests in `test_erg_prescription_compare.py` pass

### Task 3: Widen activity visibility for off-plan endurance

**Files:**
- Modify: `erg_strava/suunto_sync.py`
- Modify: `erg_strava/strava_erg_hr_plot.py`
- Test: `erg_strava/tests/test_erg_prescription_compare.py` or a new focused test file if the helper surface grows too large

- [ ] **Step 1: Persist lightweight non-erg endurance visibility from Suunto**

Update Suunto sync so workout listing can surface non-erg endurance workouts without expanding full FIT parsing. The minimal shape should preserve enough for weekly classification:

```python
{
    "key": key,
    "activityId": wk.get("activityId"),
    "startTime": wk.get("startTime"),
    "totalTime": wk.get("totalTime"),
    "totalDistance": wk.get("totalDistance"),
    "activityName": wk.get("activityName"),
    "source": "suunto",
}
```

Keep FIT download and erg/gym-specific detail fetch scoped to tracked erg/gym workouts unless needed for duration visibility.

- [ ] **Step 2: Include non-erg endurance rows in weekly collection**

Update `collect_activities_in_weeks()` so weekly collection can return:
- existing Suunto/Strava erg rows
- cached non-erg endurance activities (rides/runs) for the same weeks

Avoid double counting:
- do not emit both a matched Strava erg and its merged/Suunto counterpart
- do not treat erg rows as ride/run unprescribed rows

- [ ] **Step 3: Run focused verification for the widened collection path**

Run:

```bash
python -m pytest erg_strava/tests/test_erg_prescription_compare.py -v
```

If a new test file was introduced, run it too:

```bash
python -m pytest erg_strava/tests/test_<new_file>.py -v
```

Expected:
- ride/run visibility test passes
- previously green weekly-progress tests stay green

### Task 4: End-to-end verification and lint check

**Files:**
- Verify only

- [ ] **Step 1: Run the targeted regression suite**

Run:

```bash
python -m pytest erg_strava/tests/test_erg_prescription_compare.py -v
```

Expected:
- PASS with all weekly progress regressions green

- [ ] **Step 2: Run an adjacent safety suite if the collection helpers changed materially**

Run:

```bash
python -m pytest erg_strava/tests/test_weekly_plan_schema.py -v
```

Expected:
- PASS

- [ ] **Step 3: Check edited files for lints**

Use the editor lint tooling on:
- `erg_strava/erg_prescription_compare.py`
- `erg_strava/suunto_sync.py`
- `erg_strava/strava_erg_hr_plot.py`
- `erg_strava/tests/test_erg_prescription_compare.py`

- [ ] **Step 4: Commit**

```bash
git add erg_strava/erg_prescription_compare.py erg_strava/suunto_sync.py erg_strava/strava_erg_hr_plot.py erg_strava/tests/test_erg_prescription_compare.py docs/superpowers/plans/2026-07-04-unprescribed-volume-tracker.md
git commit -m "feat: track unprescribed endurance volume"
```
