# Weekly Run Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sunday weekly cron always saves squad `plan_json`, DMs athletes who logged that week, counts screenshot/Suunto ergs in public stats, rotates the gym accessory weekly, and privately warns a mapped athlete when their configured data source fails.

**Architecture:** Recover missing squad JSON from the session library + gym program instead of posting prose. Resolve `suuntool` via fallbacks when the configured path is absent. Fold screenshot scores into squad adherence and the plot caption. Send at most one operational Zulip DM per athlete per run when a mapped source errors.

**Tech Stack:** Python 3, pytest, existing `session_library`, `gym_program`, `athlete_plan_lock`, `send_to_zulip`.

**Spec:** `docs/superpowers/specs/2026-08-31-weekly-run-recovery-design.md`

## Global Constraints

- Never save or post a squad plan without `plan_json`.
- Never DM raw LLM prose as a weekly plan (structure-lock spec still holds).
- Error DMs only for athletes with Zulip mapping **and** a config mapping for the failing source.
- Do not DM “Suunto skipped (not listed in suunto.athlete_ids)”.
- One error DM per athlete per weekly run; it is not the weekly plan.
- Gym accessory rotation: `after_weeks: 1`.
- No live Zulip or LLM in tests.

## Files

- Create: `erg_strava/athlete_data_alerts.py` — mapping rules, alert records, DM copy, send helper
- Create: `erg_strava/tests/test_athlete_data_alerts.py`
- Create: `erg_strava/squad_plan_fallback.py` — deterministic library + gym squad JSON
- Create: `erg_strava/tests/test_squad_plan_fallback.py`
- Modify: `erg_strava/suunto_client.py` — `resolve_suuntool_binary` fallbacks
- Create: `erg_strava/tests/test_suunto_client.py`
- Modify: `erg_strava/suunto_sync.py` — return error text on sync failure
- Modify: `erg_strava/strava_erg_hr_plot.py` — collect alerts, send DMs, screenshot “new data”
- Modify: `erg_strava/generate_training_plan.py` — stamp library+gym always; use fallback; walk-back week_index
- Modify: `erg_strava/gym_program.py` — `next_gym_week_index_from_plans`
- Modify: `erg_strava/data/gym_programs/base.json` and `build.json` — `after_weeks: 1`
- Modify: `erg_strava/erg_session_merge.py` — gym logs count as a training log
- Modify: `erg_strava/squad_adherence_stats.py` — screenshot minutes
- Modify: `erg_strava/tests/test_gym_program.py`
- Modify: `erg_strava/tests/test_squad_adherence_stats.py`
- Modify: `erg_strava/tests/test_generate_training_plan.py`
- Modify: `erg_strava/config.example.yaml` — comment that missing `suuntool_path` falls back

---

### Task 1: Resolve suuntool when the configured path is missing

**Files:**
- Modify: `erg_strava/suunto_client.py`
- Create: `erg_strava/tests/test_suunto_client.py`

**Interfaces:**
- Consumes: `SuuntoCfg.suuntool_path`, `base: Path`
- Produces: `resolve_suuntool_binary(cfg, base) -> Path` still raises `FileNotFoundError` only after all candidates fail

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
from suunto_client import SuuntoCfg, resolve_suuntool_binary

def _cfg(path: Path | None) -> SuuntoCfg:
    return SuuntoCfg(
        enabled=True,
        primary=True,
        suuntool_path=path,
        session_file=None,
        indoor_rowing_activity_ids=frozenset({57}),
        gym_activity_ids=frozenset({23}),
    )

def test_resolve_falls_back_when_configured_path_missing(tmp_path: Path):
    missing = tmp_path / "nope" / "suuntool"
    fallback_dir = tmp_path / "bin"
    fallback_dir.mkdir()
    fallback = fallback_dir / "suuntool"
    fallback.write_text("#!/bin/sh\n")
    fallback.chmod(0o755)
    found = resolve_suuntool_binary(_cfg(missing), tmp_path)
    assert found == fallback.resolve()

def test_resolve_raises_when_nothing_exists(tmp_path: Path):
    try:
        resolve_suuntool_binary(_cfg(tmp_path / "missing"), tmp_path)
    except FileNotFoundError as exc:
        assert "suuntool" in str(exc).lower()
    else:
        raise AssertionError("expected FileNotFoundError")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_suunto_client.py -q
```

Expected: FAIL — current code raises on the configured path without trying `{base}/bin/suuntool`.

- [ ] **Step 3: Implement fallbacks**

In `resolve_suuntool_binary`, collect candidates in order:

1. Configured path (absolute, or `base / relative`)
2. `base / "bin" / "suuntool"`
3. `base.parent / "bin" / "suuntool"`
4. `base.parent.parent / "RRC-scripts" / "bin" / "suuntool"`
5. `Path.home() / "RRC-scripts" / "bin" / "suuntool"`
6. `shutil.which("suuntool")`

Return the first existing file. If the configured path was missing and a later candidate hit, `print` a one-line warning to stderr: `suuntool: configured path missing ({p}); using {found}`. Raise `FileNotFoundError` only if none exist.

- [ ] **Step 4: Run tests**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_suunto_client.py -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add erg_strava/suunto_client.py erg_strava/tests/test_suunto_client.py erg_strava/config.example.yaml
git commit -m "$(cat <<'EOF'
fix: fall back when configured suuntool path is missing

EOF
)"
```

---

### Task 2: Athlete data-alert mapping and DM copy

**Files:**
- Create: `erg_strava/athlete_data_alerts.py`
- Create: `erg_strava/tests/test_athlete_data_alerts.py`

**Interfaces:**
- Consumes: athlete id/label/zulip fields, `SuuntoCfg`, optional `token_dir`
- Produces:
  - `AthleteDataAlert(athlete_id, label, source, message)`
  - `source_mapped_for_athlete(source, *, athlete_id, suunto_cfg, token_dir, zulip_user_id, zulip_email) -> bool`
  - `zulip_recipient(zulip_user_id, zulip_email) -> int | str | None`
  - `should_send_alert(alert, *, ...) -> bool`
  - `merge_alerts(alerts) -> dict[int, AthleteDataAlert]` (one combined message per athlete)
  - `format_alert_dm(alert) -> str`
  - `send_athlete_data_alerts(alerts, athletes, *, send_fn) -> int`

- [ ] **Step 1: Write the failing tests**

```python
from athlete_data_alerts import (
    AthleteDataAlert,
    format_alert_dm,
    merge_alerts,
    should_send_alert,
    source_mapped_for_athlete,
)
from suunto_client import SuuntoCfg

JACK = 53603359
EMIL = 116259013

def _suunto() -> SuuntoCfg:
    return SuuntoCfg(
        enabled=True,
        primary=True,
        suuntool_path=None,
        session_file=None,
        indoor_rowing_activity_ids=frozenset({57}),
        gym_activity_ids=frozenset({23}),
        athlete_ids=frozenset({JACK}),
    )

def test_suunto_mapped_only_for_listed_athlete():
    cfg = _suunto()
    assert source_mapped_for_athlete(
        "suunto", athlete_id=JACK, suunto_cfg=cfg, token_dir=None,
        zulip_user_id=73, zulip_email=None,
    )
    assert not source_mapped_for_athlete(
        "suunto", athlete_id=EMIL, suunto_cfg=cfg, token_dir=None,
        zulip_user_id=77, zulip_email=None,
    )

def test_skip_alert_without_zulip_or_unmapped_source():
    cfg = _suunto()
    alert = AthleteDataAlert(JACK, "Jack H", "suunto", "sync failed")
    assert should_send_alert(
        alert, suunto_cfg=cfg, token_dir=None, zulip_user_id=73, zulip_email=None,
    )
    assert not should_send_alert(
        alert, suunto_cfg=cfg, token_dir=None, zulip_user_id=None, zulip_email=None,
    )
    emil = AthleteDataAlert(EMIL, "Emil", "suunto", "sync failed")
    assert not should_send_alert(
        emil, suunto_cfg=cfg, token_dir=None, zulip_user_id=77, zulip_email=None,
    )

def test_format_and_merge_alerts():
    a = AthleteDataAlert(JACK, "Jack H", "suunto", "Your Suunto workouts did not sync this week.")
    body = format_alert_dm(a)
    assert "not your weekly plan" in body.lower()
    assert "Suunto" in body
    merged = merge_alerts([a, AthleteDataAlert(JACK, "Jack H", "suunto", "No matching indoor row.")])
    assert list(merged) == [JACK]
    assert "did not sync" in merged[JACK].message
    assert "matching indoor row" in merged[JACK].message
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_athlete_data_alerts.py -q
```

Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement `athlete_data_alerts.py`**

`source_mapped_for_athlete`:

- `suunto`: `suunto_cfg.enabled` and `suunto_sync_enabled_for_athlete(suunto_cfg, athlete_id)`
- `strava`: `token_dir` is not None
- `screenshot`: `zulip_user_id is not None or bool(zulip_email)`

`should_send_alert`: `zulip_recipient(...) is not None` **and** `source_mapped_for_athlete(alert.source, ...)`

`format_alert_dm`: title `**Coachbot could not refresh your {Source} data**` (screenshot → “logged erg stats”), body = `alert.message`, footer `_This is not your weekly plan._`

`send_athlete_data_alerts`: filter with `should_send_alert`, merge, call `send_fn(content, [recipient])`, return count. Do not catch send errors (caller may wrap).

Also export helpers used later:

```python
SUUNTO_SYNC_FAIL_MESSAGE = (
    "Your Suunto workouts did not sync this week, so watch/HR streams and "
    "off-plan ergs may be missing from the squad summary.\n\n"
    "If you posted a Concept2 screenshot to Zulip, that still counts. "
    "Otherwise reply here with the session."
)
SUUNTO_SCREENSHOT_GAP_MESSAGE = (
    "A Concept2 screenshot is in your log this week, but no matching Suunto "
    "indoor row was found. Off-plan watch sessions stay out of the HR plot "
    "until Suunto syncs."
)
```

- [ ] **Step 4: Run tests**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_athlete_data_alerts.py -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add erg_strava/athlete_data_alerts.py erg_strava/tests/test_athlete_data_alerts.py
git commit -m "$(cat <<'EOF'
feat: Zulip alerts when a mapped athlete data source fails

EOF
)"
```

---

### Task 3: Collect Suunto sync errors and screenshot gaps; send DMs from the weekly run

**Files:**
- Modify: `erg_strava/suunto_sync.py`
- Modify: `erg_strava/strava_erg_hr_plot.py`
- Modify: `erg_strava/tests/test_athlete_data_alerts.py` (gap detector)
- Modify: `erg_strava/tests/test_athlete_data_alerts.py`

**Interfaces:**
- Consumes: Task 2 helpers; existing `sync_suunto_workouts_for_athlete`
- Produces:
  - `sync_suunto_workouts_for_athlete(...) -> int` unchanged
  - `sync_suunto_workouts_for_athlete_detailed(...) -> tuple[int, Optional[str]]`
  - `screenshot_without_suunto_alert(cache_dir, athlete_id, label, week, suunto_cfg) -> Optional[AthleteDataAlert]`
  - Weekly `main()` sends alerts after athlete sync, before plot

Do **not** break the existing `int` return of `sync_suunto_workouts_for_athlete`. Add the detailed function and have the old one call it.

- [ ] **Step 1: Write failing tests for detailed sync + gap detector**

In `test_athlete_data_alerts.py`:

```python
from datetime import date
from pathlib import Path
import json
from athlete_data_alerts import screenshot_without_suunto_alert
from generate_training_plan import week_bounds_from_monday

def test_screenshot_gap_alert_when_no_suunto_in_week(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 8, 24))
    scores = tmp_path / "athlete_53603359" / "erg_scores"
    scores.mkdir(parents=True)
    (scores / "abc.json").write_text(json.dumps({
        "id": "abc",
        "athlete_id": 53603359,
        "session_date": "2026-08-27",
        "source": "zulip_screenshot_vision_multi",
        "metrics": {"duration_sec": 2160, "avg_hr": 141, "distance_m": 8170},
    }))
    alert = screenshot_without_suunto_alert(
        tmp_path, 53603359, "Jack H", week, _suunto()
    )
    assert alert is not None
    assert alert.source == "suunto"

def test_no_gap_alert_without_screenshot(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 8, 24))
    (tmp_path / "athlete_53603359").mkdir()
    assert screenshot_without_suunto_alert(
        tmp_path, 53603359, "Jack H", week, _suunto()
    ) is None
```

In `test_suunto_client.py` or a thin `test_suunto_sync.py`, mock `SuuntoClient` to raise `FileNotFoundError` and assert `sync_suunto_workouts_for_athlete_detailed` returns `(0, "suuntool not found...")`.

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_athlete_data_alerts.py erg_strava/tests/test_suunto_client.py -q
```

Expected: FAIL (`screenshot_without_suunto_alert` missing)

- [ ] **Step 3: Implement detailed sync + gap + weekly send**

`suunto_sync.py`: wrap the existing try/except around `SuuntoClient` / `list_workouts` so `sync_suunto_workouts_for_athlete_detailed` returns `(n, str(exc))` on `FileNotFoundError` or `RuntimeError`, else `(n_synced, None)`. Keep printing the current skip line.

`screenshot_without_suunto_alert` (in `athlete_data_alerts.py`):

- If not `suunto_sync_enabled_for_athlete`, return None
- `load_erg_scores_for_week` has any screenshot-like source (`"screenshot" in str(source)`)
- `list_suunto_erg_workouts` has none whose local date is in `week`
- Then return `AthleteDataAlert(..., source="suunto", message=SUUNTO_SCREENSHOT_GAP_MESSAGE)`

In `strava_erg_hr_plot.sync_athlete`, capture detailed error. After all `sync_athlete` calls in `main()`, for each pipeline athlete (`load_pipeline_athletes(cfg_path)`):

- If suunto detailed error and source mapped → append `AthleteDataAlert` with `SUUNTO_SYNC_FAIL_MESSAGE`
- Else if gap detector fires → append gap alert
- `send_athlete_data_alerts(..., send_fn=lambda content, to, **k: send_private_message_to_zulip(content, to, zuliprc_path=zuliprc))`

Skip sending when `--no-zulip`.

- [ ] **Step 4: Run tests**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_athlete_data_alerts.py erg_strava/tests/test_suunto_client.py -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add erg_strava/suunto_sync.py erg_strava/strava_erg_hr_plot.py erg_strava/athlete_data_alerts.py erg_strava/tests/test_athlete_data_alerts.py
git commit -m "$(cat <<'EOF'
feat: DM mapped athletes when Suunto sync or screenshot match fails

EOF
)"
```

---

### Task 4: Deterministic squad `plan_json` from library + gym

**Files:**
- Create: `erg_strava/squad_plan_fallback.py`
- Create: `erg_strava/tests/test_squad_plan_fallback.py`

**Interfaces:**
- Consumes: `WeekBounds`, `select_sessions_for_week`, `apply_library_sessions_to_plan`, `apply_program_gym_to_plan`
- Produces: `build_library_squad_plan_json(*, plan_week, phase, include_lifting, peak_kg_by_exercise, prev_plan_json, session_selections=None) -> dict` that `parse_weekly_plan` accepts and `validate_plan_session_constraints` returns None

- [ ] **Step 1: Write the failing test**

```python
from datetime import date
from generate_training_plan import week_bounds_from_monday
from weekly_plan_schema import parse_weekly_plan, validate_plan_session_constraints
from squad_plan_fallback import build_library_squad_plan_json

def test_fallback_plan_is_valid_structured_week():
    week = week_bounds_from_monday(date(2026, 8, 31))
    data = build_library_squad_plan_json(
        plan_week=week,
        phase="build",
        include_lifting=True,
        peak_kg_by_exercise={"Back squat": 80.0},
        prev_plan_json={"gym_program": {"id": "build-a", "week_index": 0}},
    )
    plan = parse_weekly_plan(data)
    assert plan is not None
    assert validate_plan_session_constraints(plan) is None
    by = {d.weekday: d for d in plan.days}
    assert by["Monday"].session_type == "gym"
    assert by["Tuesday"].session_type == "erg"
    assert by["Tuesday"].rowing is not None
    assert {s.phase for s in by["Tuesday"].rowing.segments} >= {"warm_up", "main_set", "cool_down"}
    assert by["Thursday"].session_type == "on_water"
    assert by["Thursday"].rowing is not None
    assert by["Thursday"].rowing.erg_alternative is not None
    assert by["Sunday"].session_type == "rest"
    assert by["Sunday"].rowing is None
    names = [e.name for e in by["Monday"].gym.exercises]
    assert "Back squat" in names
    assert data["gym_program"]["week_index"] == 1
```

Do not assert kettlebell vs BSS here; Task 6 owns `after_weeks`.

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_squad_plan_fallback.py -q
```

Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement `build_library_squad_plan_json`**

Skeleton days (dates from `plan_week.week_start` + timedelta):

- Monday / Wednesday: `session_type="gym"`, `gym=None`, `rowing=None`
- Tuesday: `session_type="erg"`, `rowing={"segments": [], "erg_alternative": None}`
- Thursday: `session_type="on_water"`, same empty rowing
- Fri/Sat/Sun: rest

Then:

```python
selections = session_selections or select_sessions_for_week(phase=phase)
patched = apply_library_sessions_to_plan(skeleton, selections)
# If Thursday still has no erg_alternative, copy rowing into erg_alternative.
if include_lifting:
    patched = apply_program_gym_to_plan(
        patched,
        phase=phase,
        week_index=next_gym_week_index(prev_plan_json),
        peak_kg_by_exercise=peak_kg_by_exercise,
        prev_plan_json=prev_plan_json,
    )
```

If Tuesday/Thursday rowing segments are empty after apply (library skipped empty rowing), assign `template.rowing` directly and set `session_subtype`.

Copy Thursday `rowing` to `erg_alternative` when missing so on-water days stay valid.

- [ ] **Step 4: Run tests**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_squad_plan_fallback.py -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add erg_strava/squad_plan_fallback.py erg_strava/tests/test_squad_plan_fallback.py
git commit -m "$(cat <<'EOF'
feat: build squad plan JSON from session library when the LLM fails

EOF
)"
```

---

### Task 5: Always stamp library + gym; never save prose-only squad plans

**Files:**
- Modify: `erg_strava/generate_training_plan.py` (`generate_squad_weekly_plan`, `run_weekly_training_pipeline` log line)
- Modify: `erg_strava/gym_program.py`
- Modify: `erg_strava/tests/test_generate_training_plan.py`
- Modify: `erg_strava/tests/test_gym_program.py`

**Interfaces:**
- Consumes: `build_library_squad_plan_json`, `next_gym_week_index`
- Produces: `generate_squad_weekly_plan(...)` always returns `plan_json` that parses; `next_gym_week_index_from_plans(records) -> int`

- [ ] **Step 1: Write failing tests**

`test_gym_program.py`:

```python
def test_next_gym_week_index_from_plans_skips_missing_meta():
    from gym_program import next_gym_week_index_from_plans
    assert next_gym_week_index_from_plans([{"plan_json": None}]) == 0
    assert next_gym_week_index_from_plans(
        [{"plan_json": None}, {"plan_json": {"gym_program": {"week_index": 0}}}]
    ) == 1
```

`test_generate_training_plan.py` — monkeypatch `_generate_structured_plan_with_fallback` to return prose-only, then:

```python
def test_squad_plan_recovers_json_when_llm_returns_prose_only(monkeypatch):
    from generate_training_plan import generate_squad_weekly_plan, week_bounds_from_monday
    from datetime import date

    monkeypatch.setattr(
        "generate_training_plan._generate_structured_plan_with_fallback",
        lambda *a, **k: GeneratedWeeklyPlan(plan_text="Sunday: rowing", plan_json=None),
    )
    out = generate_squad_weekly_plan(
        "summary",
        "tok",
        plan_week=week_bounds_from_monday(date(2026, 8, 31)),
        phase="build",
        prev_plan_json={"gym_program": {"id": "build-a", "week_index": 0}},
    )
    assert out.plan_json is not None
    assert parse_weekly_plan(out.plan_json) is not None
    assert "Sunday: rowing" not in out.plan_text
```

Also add: `send_weekly_athlete_plan_dms` with this recovered JSON still sends (existing lock test already covers clone).

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_generate_training_plan.py::test_squad_plan_recovers_json_when_llm_returns_prose_only erg_strava/tests/test_gym_program.py::test_next_gym_week_index_from_plans_skips_missing_meta -q
```

Expected: FAIL (prose-only still returned)

- [ ] **Step 3: Implement**

`next_gym_week_index_from_plans`: iterate records newest-first; first dict `plan_json` with `gym_program.week_index` → return that int + 1; else 0.

In `generate_squad_weekly_plan`, after `_generate_structured_plan_with_fallback`:

```python
generated = _generate_structured_plan_with_fallback(...)
week_index = next_gym_week_index(prev_plan_json)
plan_json = generated.plan_json
if plan_json is None:
    print("Weekly plan: structured JSON failed; using library/gym fallback.", flush=True)
    plan_json = build_library_squad_plan_json(
        plan_week=plan_week,
        phase=phase,
        include_lifting=include_lifting,
        peak_kg_by_exercise=peak_kg_by_exercise,
        prev_plan_json=prev_plan_json,
        session_selections=session_selections,
    )
else:
    plan_json = apply_library_sessions_to_plan(plan_json, session_selections)
    if include_lifting:
        plan_json = apply_program_gym_to_plan(
            plan_json,
            phase=phase,
            week_index=week_index,
            peak_kg_by_exercise=peak_kg_by_exercise,
            prev_plan_json=prev_plan_json,
        )
parsed = parse_weekly_plan(plan_json)
if parsed is None:
    plan_json = build_library_squad_plan_json(...)
    parsed = parse_weekly_plan(plan_json)
return GeneratedWeeklyPlan(plan_json=plan_json, plan_text=render_plan_text(parsed))
```

Remove the old `if generated.plan_json: patched = apply_library...` block so gym overlay is not skipped.

In `run_weekly_training_pipeline`, delete the “saved prose-only plan” branch; if `plan_json is still None` after alignment, call `build_library_squad_plan_json` again before `save_weekly_plan`.

If alignment returns None because it tried to import invalid prose, **keep** the fallback JSON already in hand — do not replace a valid `plan_json` with None. Read `apply_season_master_plan_alignment`: if it can null out JSON, pass the fallback as `plan_json` and only accept alignment output when `parse_weekly_plan(aligned)` succeeds.

- [ ] **Step 4: Run tests**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_generate_training_plan.py erg_strava/tests/test_gym_program.py erg_strava/tests/test_squad_plan_fallback.py -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add erg_strava/generate_training_plan.py erg_strava/gym_program.py erg_strava/tests/test_generate_training_plan.py erg_strava/tests/test_gym_program.py
git commit -m "$(cat <<'EOF'
fix: never save a squad weekly plan without structured JSON

EOF
)"
```

---

### Task 6: Rotate gym accessory after one week

**Files:**
- Modify: `erg_strava/data/gym_programs/base.json`
- Modify: `erg_strava/data/gym_programs/build.json`
- Modify: `erg_strava/tests/test_gym_program.py` (only if any test hard-codes `after_weeks == 3`)

**Interfaces:**
- Consumes: existing `materialize_week` rotation
- Produces: `load_program("build").rotations[0].after_weeks == 1`

- [ ] **Step 1: Write the failing test**

```python
def test_build_and_base_rotate_accessory_after_one_week():
    from gym_program import load_program, materialize_week
    for phase in ("base", "build"):
        program = load_program(phase)
        assert program.rotations[0].after_weeks == 1
        before, _ = materialize_week(program, week_index=0)
        after, _ = materialize_week(program, week_index=1)
        assert "Bulgarian split squat" in [e.name for e in before.exercises]
        assert "Kettlebell swings" in [e.name for e in after.exercises]
        assert "Bulgarian split squat" not in [e.name for e in after.exercises]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_gym_program.py::test_build_and_base_rotate_accessory_after_one_week -q
```

Expected: FAIL (`after_weeks` is 3)

- [ ] **Step 3: Set `"after_weeks": 1` in both JSON files**

- [ ] **Step 4: Run gym program tests**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_gym_program.py -q
```

Expected: PASS (`test_rotation_replaces_exercise_after_configured_weeks` uses `rotation.after_weeks` dynamically)

- [ ] **Step 5: Commit**

```bash
git add erg_strava/data/gym_programs/base.json erg_strava/data/gym_programs/build.json erg_strava/tests/test_gym_program.py
git commit -m "$(cat <<'EOF'
fix: rotate gym accessory the week after it is first programmed

EOF
)"
```

---

### Task 7: Count Zulip gym logs as a weekly training log

**Files:**
- Modify: `erg_strava/erg_session_merge.py` (`athlete_has_week_training_log`)
- Modify: `erg_strava/tests/test_generate_training_plan.py` or add `erg_strava/tests/test_erg_session_merge.py` coverage

**Interfaces:**
- Consumes: `load_gym_logs_for_athlete(cache_dir, athlete_id, week=week)`
- Produces: `athlete_has_week_training_log` True when only a gym transcript exists

- [ ] **Step 1: Write the failing test**

```python
from datetime import date
from generate_training_plan import week_bounds_from_monday
from erg_session_merge import athlete_has_week_training_log

def test_gym_log_counts_as_week_training_log(tmp_path):
    week = week_bounds_from_monday(date(2026, 8, 24))
    gym = tmp_path / "athlete_1" / "gym_logs"
    gym.mkdir(parents=True)
    (gym / "g1.json").write_text(
        '{"session_date": "2026-08-26", "gym": {"total_tonnage_kg": 5000}}'
    )
    assert athlete_has_week_training_log(tmp_path, 1, "James", week)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_generate_training_plan.py -k gym_log_counts -q
```

Expected: FAIL (False)

- [ ] **Step 3: At the end of `athlete_has_week_training_log`, if still false:**

```python
from generate_training_plan import load_gym_logs_for_athlete
return bool(load_gym_logs_for_athlete(cache_dir, athlete_id, week=week))
```

Avoid import cycles: if `load_gym_logs_for_athlete` cannot be imported from `erg_session_merge`, duplicate the tiny glob (same as `squad_adherence_stats` already calls `load_gym_logs_for_athlete` from `generate_training_plan`). Prefer importing inside the function as `generate_training_plan` already lazy-imports `erg_session_merge`.

- [ ] **Step 4: Run tests**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_generate_training_plan.py -q
```

Expected: PASS including existing DM lock test

- [ ] **Step 5: Commit**

```bash
git add erg_strava/erg_session_merge.py erg_strava/tests/test_generate_training_plan.py
git commit -m "$(cat <<'EOF'
fix: treat Zulip gym logs as weekly training so plan DMs are not skipped

EOF
)"
```

---

### Task 8: Include screenshot ergs in public squad averages and plot caption

**Files:**
- Modify: `erg_strava/squad_adherence_stats.py`
- Modify: `erg_strava/tests/test_squad_adherence_stats.py`
- Modify: `erg_strava/strava_erg_hr_plot.py` (`format_new_erg_data_summary`)
- Modify: `erg_strava/tests/test_erg_plot_points.py` (`format_new_erg_data_summary`)

**Interfaces:**
- Consumes: `load_erg_scores_for_week`, `_classify_hr_zone`, existing `_bucket_erg_points`
- Produces: screenshot-only 36 min @ 141 bpm → `z13_minutes` ≈ 36, split from `avg_split_500_sec`; caption lists screenshot lines when Suunto keys are unchanged

- [ ] **Step 1: Write failing tests**

```python
def test_screenshot_score_fills_zero_stream_minutes(tmp_path):
    from datetime import date
    from generate_training_plan import week_bounds_from_monday
    from squad_adherence_stats import compute_squad_week_adherence_stats
    week = week_bounds_from_monday(date(2026, 8, 24))
    scores = tmp_path / "athlete_1" / "erg_scores"
    scores.mkdir(parents=True)
    (scores / "s.json").write_text(json.dumps({
        "id": "s",
        "athlete_id": 1,
        "athlete_label": "Jack H",
        "session_date": "2026-08-27",
        "source": "zulip_screenshot_vision_multi",
        "metrics": {
            "duration_sec": 2160,
            "avg_hr": 141,
            "avg_split_500_sec": 133.0,
        },
    }))
    stats = compute_squad_week_adherence_stats(
        week, [], {}, pd.DataFrame(),
        cache_dir=tmp_path,
        athlete_profiles={1: AthleteProfile(id=1, label="Jack H", max_hr_bpm=185)},
        gym_types=frozenset(),
        gym_name_patterns=(),
    )
    jack = stats.athlete_stats[0]
    assert jack.z13_minutes >= 30
    assert jack.z13_split_median_sec == 133.0
```

For the caption, extend `format_new_erg_data_summary` so when `new_keys` is empty but a screenshot `session_date` is after `last_run.run_at`’s local date, the text includes the score id / metres / date instead of only `(none)`.

```python
def test_caption_lists_screenshot_when_no_new_suunto_keys():
    # last_run run_at = 2026-08-23; screenshot session_date 2026-08-27
    text = format_new_erg_data_summary(...)
    assert "2026-08-27" in text
    assert "(none)" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_squad_adherence_stats.py -q
```

Expected: FAIL (`z13_minutes == 0`)

- [ ] **Step 3: Implement**

Add `_bucket_erg_from_scores(records, profile) -> tuple` in `squad_adherence_stats.py`:

- Skip records whose `merged_suunto_workout_key` / `merged_strava_activity_id` is already in `athlete_erg["suunto_key"]` / `activity_id`
- If `metrics.session_parts` is a list, sum duration per part and classify each part’s `avg_hr`
- Else use `metrics.duration_sec` and `metrics.avg_hr`
- Split samples: `avg_split_500_sec`

In `compute_squad_week_adherence_stats`, after `_bucket_erg_points`, add screenshot bucket minutes/splits (concatenate split lists, add minutes).

`format_squad_adherence_stats`: keep the same three bullets. Rounding: use `int(round(med))` as today; 36 min must not become 0.

Caption: after computing `new_keys`, also load scores per athlete with `session_date > last_date` (or `>=` the day after last run). Append lines `screenshot {id}, {metres} m, {session_date}`. If those exist, do not return the bare `(none)` block.

- [ ] **Step 4: Run tests**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_squad_adherence_stats.py erg_strava/tests/test_erg_plot_points.py -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add erg_strava/squad_adherence_stats.py erg_strava/strava_erg_hr_plot.py erg_strava/tests/test_squad_adherence_stats.py
git commit -m "$(cat <<'EOF'
fix: count screenshot ergs in squad averages and the weekly plot caption

EOF
)"
```

---

### Task 9: Full regression + config comment

**Files:**
- Modify: `erg_strava/config.example.yaml` (suuntool fallback comment)
- Tests only

- [ ] **Step 1: Add a comment above `suuntool_path` in `config.example.yaml`**

```yaml
  # If this path is missing, coachbot tries ../bin/suuntool, ../../RRC-scripts/bin/suuntool,
  # ~/RRC-scripts/bin/suuntool, and PATH. Mapped Suunto athletes get a Zulip DM on failure.
  # suuntool_path: ../bin/suuntool
```

- [ ] **Step 2: Run the weekly-plan / suunto / gym / adherence suite**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_suunto_client.py erg_strava/tests/test_athlete_data_alerts.py erg_strava/tests/test_squad_plan_fallback.py erg_strava/tests/test_gym_program.py erg_strava/tests/test_generate_training_plan.py erg_strava/tests/test_squad_adherence_stats.py erg_strava/tests/test_athlete_plan_lock.py -q
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add erg_strava/config.example.yaml
git commit -m "$(cat <<'EOF'
docs: note suuntool path fallbacks and mapped-athlete error DMs

EOF
)"
```

---

## Spec coverage

| Spec requirement | Task |
|------------------|------|
| Suunto binary fallbacks | 1 |
| Error DM mapping + copy | 2 |
| Sync/gap alerts sent from weekly run | 3 |
| Deterministic squad JSON | 4 |
| Never save prose-only; always stamp gym | 5 |
| `after_weeks: 1` | 6 |
| Gym logs open plan DMs | 7 |
| Screenshot minutes in public stats + caption | 8 |
| Config comment | 9 |

## Deploy note (not a code task)

After merge, pull `main` on hellpi and restart the bot as usual. Confirm `resolve_suuntool_binary` logs the RRC-scripts binary (or copy/symlink into `~/coachbot/bin/suuntool`). The next Sunday run should DM Jack a plan (and only an error DM if Suunto still fails).
