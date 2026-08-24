# Athlete Plan Structure Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Athlete weekly-plan DMs are always the squad session structure with only gym kg and erg split/HR changed; never send raw LLM prose.

**Architecture:** Clone squad `plan_json`, overlay gym loads from lift history, HR from `AthleteProfile`, and optional LLM splits matched by weekday+phase. Expand `validate_athlete_plan_against_squad` so duration/exercise/reps/extras cannot drift. Wire the lock into `generate_athlete_weekly_plan` and the DM sender.

**Tech Stack:** Python 3, pytest, existing `weekly_plan_schema` / `gym_program` / `session_library` / `plan_text_import`.

**Spec:** `docs/superpowers/specs/2026-08-24-athlete-plan-structure-lock-design.md`

## Global Constraints

- Squad `plan_json` owns structure; athlete JSON is a clone plus numeric overlays.
- LLM may supply `split_min` / `split_max` only.
- Gym kg comes from `personalize_plan_gym_loads`, not the LLM.
- HR bpm comes from athlete zone tables (T then Z).
- If the LLM fails, still send the locked clone (squad splits).
- Never DM `plan_text` without locked `plan_json`.
- No live Zulip or LLM in tests.

## Files

- Create: `erg_strava/athlete_plan_lock.py` — `lock_athlete_plan_to_squad`
- Create: `erg_strava/tests/test_athlete_plan_lock.py`
- Modify: `erg_strava/weekly_plan_schema.py` — expanded validator, `personalize_plan_rowing_hr`
- Modify: `erg_strava/plan_text_import.py` — `**Session Type:**` regex
- Modify: `erg_strava/generate_training_plan.py` — lock after generation; never send unlocked prose
- Modify: `erg_strava/tests/test_weekly_plan_schema.py`
- Modify: `erg_strava/tests/test_plan_text_import.py`
- Modify: `erg_strava/tests/test_generate_training_plan.py`

---

### Task 1: Import `**Session Type:** On Water` as on_water

**Files:**
- Modify: `erg_strava/plan_text_import.py`
- Modify: `erg_strava/tests/test_plan_text_import.py`

**Interfaces:**
- Consumes: existing `import_weekly_plan_json_from_text`
- Produces: Thursday `session_type == "on_water"` for Zulip-bold headers

- [ ] **Step 1: Write the failing test**

Add to `test_plan_text_import.py`:

```python
def test_import_bold_session_type_on_water():
    text = """Jack H

### Thursday, 2026-08-27
**Session Type:** On Water
Warm-up: 12 min @ Z2/T3, split 2:15–2:20, HR 111–139 bpm, priority: HR
Main Set: 2×12 min @ Z2/T3, split 2:10–2:15, HR 111–139 bpm, priority: HR
Cool-down: 12 min @ Z2/T3, split 2:15–2:20, HR 111–139 bpm, priority: HR
"""
    data = import_weekly_plan_json_from_text(
        text, week_start="2026-08-24", personalised=True, greeting="Jack,"
    )
    assert data is not None
    thursday = next(d for d in data["days"] if d["weekday"] == "Thursday")
    assert thursday["session_type"] == "on_water"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_plan_text_import.py::test_import_bold_session_type_on_water -q
```

Expected: FAIL (`session_type` is `rest`).

- [ ] **Step 3: Fix `_ATHLETE_SESSION_TYPE_RE`**

In `plan_text_import.py`, match optional wrapping asterisks and capture the type, e.g.:

```python
_ATHLETE_SESSION_TYPE_RE = re.compile(
    r"\*+Session Type:\*+\s*(.+)",
    re.I,
)
```

Strip captured text; `_session_type_from_header` already maps `"On Water"` → `on_water`. Keep existing `*Session Type: Gym*` fixtures passing.

- [ ] **Step 4: Run tests**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_plan_text_import.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add erg_strava/plan_text_import.py erg_strava/tests/test_plan_text_import.py
git commit -m "Fix athlete plan import of Zulip-bold session type headers."
```

---

### Task 2: Expand `validate_athlete_plan_against_squad`

**Files:**
- Modify: `erg_strava/weekly_plan_schema.py` (`validate_athlete_plan_against_squad`)
- Modify: `erg_strava/tests/test_weekly_plan_schema.py`

**Interfaces:**
- Consumes: `WeeklyPlan` athlete + squad
- Produces: error string if gym set reps/`duration_sec`, rowing segment count/phase/duration, erg alternative shape, or `recommended_erg` id/shape differ. Does not compare kg, splits, or HR.

- [ ] **Step 1: Write failing tests**

```python
def test_validate_athlete_rejects_warmup_duration_mismatch():
    squad = parse_weekly_plan(sample_squad_plan_dict())
    data = json.loads(json.dumps(sample_squad_plan_dict()))
    data["personalised"] = True
    data["days"][1]["rowing"]["segments"][0]["duration"] = "12 min"
    athlete = parse_weekly_plan(data)
    err = validate_athlete_plan_against_squad(athlete, squad)
    assert err is not None
    assert "duration" in err.lower() or "Tuesday" in err


def test_validate_athlete_rejects_missing_recommended_erg():
    squad_data = sample_squad_plan_dict()
    squad_data["recommended_erg"] = _recommended_erg_dict()
    squad = parse_weekly_plan(squad_data)
    athlete_data = json.loads(json.dumps(sample_squad_plan_dict()))
    athlete_data["personalised"] = True
    athlete = parse_weekly_plan(athlete_data)
    err = validate_athlete_plan_against_squad(athlete, squad)
    assert err is not None
    assert "recommended" in err.lower()
```

Keep `test_validate_athlete_must_match_squad_gym_exercises` passing.

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_weekly_plan_schema.py::test_validate_athlete_rejects_warmup_duration_mismatch erg_strava/tests/test_weekly_plan_schema.py::test_validate_athlete_rejects_missing_recommended_erg -q
```

Expected: FAIL (validator currently returns None).

- [ ] **Step 3: Extend `validate_athlete_plan_against_squad`**

After existing gym-name checks, compare:

- Per gym exercise: `len(sets)`, each set `reps` and `duration_sec`
- Per rowing day: `len(segments)`; each segment `phase` and stripped `duration`
- `erg_alternative` both None or same segment count/phases/durations
- `recommended_erg` both None or same `id` plus segment phases/durations

Helper for a list of segments is fine. Do not compare kg/splits/HR.

- [ ] **Step 4: Run schema tests**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_weekly_plan_schema.py -q
```

Expected: PASS. If other tests now fail because fixtures intentionally differ on duration, tighten those fixtures so athlete clones match squad structure (they should).

- [ ] **Step 5: Commit**

```bash
git add erg_strava/weekly_plan_schema.py erg_strava/tests/test_weekly_plan_schema.py
git commit -m "Reject athlete plans that change squad session shape."
```

---

### Task 3: Overlay HR on all rowing days

**Files:**
- Modify: `erg_strava/weekly_plan_schema.py`
- Modify: `erg_strava/tests/test_weekly_plan_schema.py`

**Interfaces:**
- Consumes: `WeeklyPlan`, `AthleteProfile`
- Produces: `personalize_plan_rowing_hr(plan, profile) -> WeeklyPlan` using `_hr_range_for_segment` on every day segment and `erg_alternative`. Refactor `personalize_recommended_erg` to use the same segment overlay.

- [ ] **Step 1: Write the failing test**

```python
def test_personalize_plan_rowing_hr_overlays_days_and_alternative():
    from weekly_plan_schema import personalize_plan_rowing_hr
    profile = AthleteProfile(id=1, label="Jack H", max_hr_bpm=185)
    plan = parse_weekly_plan(sample_squad_plan_dict())
    updated = personalize_plan_rowing_hr(plan, profile)
    tue = next(d for d in updated.days if d.weekday == "Tuesday")
    t3 = profile.zone_bpm_range("T3")
    assert tue.rowing.segments[0].hr_bpm_min == t3[0]
    thu = next(d for d in updated.days if d.weekday == "Thursday")
    assert thu.rowing.erg_alternative is not None
    assert thu.rowing.erg_alternative.segments[0].hr_bpm_min == t3[0]
    assert tue.rowing.segments[0].split_min == plan.days[1].rowing.segments[0].split_min
```

- [ ] **Step 2: Run test to verify it fails**

Expected: FAIL (`personalize_plan_rowing_hr` not defined).

- [ ] **Step 3: Implement**

Extract `_overlay_segment_hr(seg, profile)` from `personalize_recommended_erg`. Map it over `plan.days` rowing + alternatives. Skip when range is None. Keep splits.

- [ ] **Step 4: Run tests**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_weekly_plan_schema.py -q
```

- [ ] **Step 5: Commit**

```bash
git add erg_strava/weekly_plan_schema.py erg_strava/tests/test_weekly_plan_schema.py
git commit -m "Overlay athlete HR zones onto every weekly rowing segment."
```

---

### Task 4: `lock_athlete_plan_to_squad`

**Files:**
- Create: `erg_strava/athlete_plan_lock.py`
- Create: `erg_strava/tests/test_athlete_plan_lock.py`

**Interfaces:**
- Consumes: squad JSON, optional proposal JSON, `AthleteProfile`, lift logs, greeting, `include_lifting`
- Produces: `lock_athlete_plan_to_squad(...) -> Dict[str, Any]` personalised clone that passes expanded `validate_athlete_plan_against_squad`

Implementation outline:

```python
def lock_athlete_plan_to_squad(
    squad_plan_json: Mapping[str, Any],
    *,
    proposal_json: Optional[Mapping[str, Any]] = None,
    athlete_profile: Optional[AthleteProfile] = None,
    lift_logs_by_exercise: Optional[Mapping[str, Sequence[LiftLog]]] = None,
    greeting: Optional[str] = None,
    include_lifting: bool = True,
) -> Dict[str, Any]:
```

1. Deep-copy squad; set `personalised=True`, `greeting`.
2. If `include_lifting`: `personalize_plan_gym_loads(clone, squad, ...)`.
3. Parse clone; `personalize_plan_rowing_hr` if profile given; dump.
4. Overlay splits from `proposal_json`: same weekday, same `phase`, in-order among that phase; copy `split_min`/`split_max` only if both match `^\d{1,2}:\d{2}$` and min seconds ≤ max seconds. Apply to day segments and erg-alternative segments. Ignore extra Rest lines.
5. `copy_recommended_erg_from_squad(clone, squad, profile=athlete_profile)`.
6. Return dict.

- [ ] **Step 1: Write failing tests** in `test_athlete_plan_lock.py`

Use `sample_squad_plan_dict` + `_recommended_erg_dict`. Build a proposal that: shortens Tue WU to 12 min, adds a Rest segment, drops Thursday erg alternative, omits `recommended_erg`, inverts Monday squat kg (set 2 heavier than set 1), and changes Tue main split to `1:50–1:55`.

Assert locked plan:

- Tue WU duration still squad (10 min in the sample fixture)
- No extra Rest segment
- Thursday still has erg alternative
- `recommended_erg.id` matches squad
- Squat reps match squad; peak kg scaled from logs (or squad pyramid if no logs — not inverted)
- Tue main `split_min`/`split_max` are `1:50`/`1:55`
- `validate_athlete_plan_against_squad` is None
- `personalised` is True and greeting is set

Second test: `proposal_json=None` keeps squad splits, still personalised, validator passes.

- [ ] **Step 2: Run tests to verify they fail**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_athlete_plan_lock.py -q
```

Expected: FAIL (module missing).

- [ ] **Step 3: Implement `athlete_plan_lock.py`**

- [ ] **Step 4: Run lock + related tests**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_athlete_plan_lock.py erg_strava/tests/test_weekly_plan_schema.py erg_strava/tests/test_gym_program.py -q
```

- [ ] **Step 5: Commit**

```bash
git add erg_strava/athlete_plan_lock.py erg_strava/tests/test_athlete_plan_lock.py
git commit -m "Lock athlete weekly plans to squad session structure."
```

---

### Task 5: Wire generation and DMs

**Files:**
- Modify: `erg_strava/generate_training_plan.py`
- Modify: `erg_strava/tests/test_generate_training_plan.py`

**Interfaces:**
- Consumes: `lock_athlete_plan_to_squad`, `import_prose_plan_json`
- Produces: `generate_athlete_weekly_plan` always returns locked `plan_json` when `squad_plan_json` is present. `send_weekly_athlete_plan_dms` never sets `plan_body = generated.plan_text` if squad JSON exists.

- [ ] **Step 1: Write failing tests**

Monkeypatch `_generate_structured_plan_with_fallback` to return `GeneratedWeeklyPlan(plan_text="### Monday, 2026-06-15\n**Session Type:** Gym\n", plan_json=None)`.

Call `generate_athlete_weekly_plan("Jack H", "summary", "tok", squad_plan_json=sample_squad_plan_dict(), athlete_profile=AthleteProfile(id=1, label="Jack H", max_hr_bpm=185))`.

Assert `result.plan_json` is not None, `validate_athlete_plan_against_squad` passes vs squad, and `result.plan_text` does not start with `### Monday`.

Second: in `send_weekly_athlete_plan_dms`, if generated JSON is None and squad JSON is present, the sent content must still be rendered locked JSON (mock `send_private_message_to_zulip` and the generator). Skip the athlete rather than sending `###` markdown if squad JSON is missing.

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Wire**

In `generate_athlete_weekly_plan`, after `_generate_structured_plan_with_fallback`:

```python
proposal = generated.plan_json
if proposal is None and squad_plan_json and generated.plan_text.strip():
    from weekly_plan_harness import import_prose_plan_json
    proposal = import_prose_plan_json(
        generated.plan_text,
        week_start=plan_week.week_start.isoformat(),
        personalised=True,
        greeting=athlete_label.split()[0] + ",",
    )
if squad_plan_json:
    from athlete_plan_lock import lock_athlete_plan_to_squad
    locked = lock_athlete_plan_to_squad(
        squad_plan_json,
        proposal_json=proposal,
        athlete_profile=athlete_profile,
        lift_logs_by_exercise=lift_logs_by_exercise,
        greeting=athlete_label.split()[0] + ",",
        include_lifting=include_lifting,
    )
    parsed = parse_weekly_plan(locked)
    squad_parsed = parse_weekly_plan(squad_plan_json)
    if parsed and squad_parsed:
        err = validate_athlete_plan_against_squad(parsed, squad_parsed)
        if err:
            print(f"Athlete plan lock validator: {err}", flush=True)
        return GeneratedWeeklyPlan(
            plan_json=locked,
            plan_text=render_plan_text(parsed, absolute_hr_bpm=False),
        )
```

Remove the old `personalize_plan_gym_loads` / `copy_recommended_erg_from_squad` block (lock does both).

In `send_weekly_athlete_plan_dms`, replace `else: plan_body = generated.plan_text` with: if `squad_plan_json` is present, lock (same args) and render; else log skip and `continue` (do not send prose).

- [ ] **Step 4: Run tests**

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/test_generate_training_plan.py erg_strava/tests/test_athlete_plan_lock.py erg_strava/tests/test_plan_text_import.py erg_strava/tests/test_weekly_plan_schema.py -q
```

Then full suite:

```bash
PYTHONPATH=".:erg_strava:lighties" pytest erg_strava/tests/ coach_bot/tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add erg_strava/generate_training_plan.py erg_strava/tests/test_generate_training_plan.py
git commit -m "Send locked athlete weekly plans instead of LLM prose fallback."
```
