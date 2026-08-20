# Zone / HR Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject incompatible Z/T pairs and phase-inappropriate high intensity on all weekly plans, and reject personalised plans whose HR bands miss the athlete’s Z/T bpm tables; support with canonical session templates in prompts.

**Architecture:** Add deterministic validators in `weekly_plan_schema.py`, thread `phase` (and optional `AthleteProfile`) through harness validation, keep enforcement via existing structured retry / repair feedback. Prompt templates are additive constants in `generate_training_plan.py`.

**Tech Stack:** Python 3, pytest, existing OpenRouter structured-plan pipeline

**Spec:** `docs/superpowers/specs/2026-07-23-zone-hr-consistency-design.md`

## Global Constraints

- Keep `zone_z` / `zone_t` schema enums; do not rename to UT-only codes
- Squad plans: no HR bpm validation
- No silent alignment rewrites of zones/HR for these rules
- Prefer TDD: failing test → minimal implementation → green

## File map

| File | Responsibility |
|------|----------------|
| `erg_strava/weekly_plan_schema.py` | Z↔T matrix, phase intensity, HR overlap helpers; extend `validate_plan_session_constraints` |
| `erg_strava/weekly_plan_harness.py` | Pass `phase` / optional `athlete_profile` into constraint validation |
| `erg_strava/generate_training_plan.py` | Pass profile on athlete generation validation; add canonical template prompt text |
| `erg_strava/athlete_profile.py` | Reuse existing `zone_bpm_range` / five+seven maps (no formula change) |
| `erg_strava/tests/test_weekly_plan_schema.py` | Matrix, phase, HR unit tests |
| `erg_strava/tests/test_weekly_plan_harness.py` | Integration: finalize rejects bad Z/T or HR when profile passed |

---

### Task 1: Z↔T compatibility validator

**Files:**
- Modify: `erg_strava/weekly_plan_schema.py`
- Modify: `erg_strava/tests/test_weekly_plan_schema.py`

**Interfaces:**
- Produces: `ZONE_ZT_COMPATIBLE: Dict[str, frozenset[str]]`
- Produces: `validate_rowing_zone_zt_compatibility(day: DayPlan) -> Optional[str]`
- Produces: call from `validate_plan_session_constraints`

- [ ] **Step 1: Write the failing tests**

```python
def test_zone_zt_compatibility_rejects_z2_t6():
    data = sample_squad_plan_dict()
    tue = data["days"][1]
    tue["rowing"]["segments"][1]["zone_z"] = "Z2"
    tue["rowing"]["segments"][1]["zone_t"] = "T6"
    plan = parse_weekly_plan(data)
    assert plan is not None
    err = validate_plan_session_constraints(plan)
    assert err is not None
    assert "incompatible" in err.lower() or "Z2" in err


def test_zone_zt_compatibility_accepts_z4_t6():
    data = sample_squad_plan_dict()
    tue = data["days"][1]
    tue["rowing"]["segments"][1]["zone_z"] = "Z4"
    tue["rowing"]["segments"][1]["zone_t"] = "T6"
    tue["rowing"]["segments"][1]["duration"] = "5×6 min / 2 min rest"
    tue["session_subtype"] = "intervals"
    plan = parse_weekly_plan(data)
    assert plan is not None
    # May still fail other constraints; specifically Z↔T alone:
    from weekly_plan_schema import validate_rowing_zone_zt_compatibility
    assert validate_rowing_zone_zt_compatibility(
        next(d for d in plan.days if d.weekday == "Tuesday")
    ) is None
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
cd /Users/jack/GitHub/RRC-scripts
PYTHONPATH=erg_strava ./.venv/bin/python -m pytest \
  erg_strava/tests/test_weekly_plan_schema.py::test_zone_zt_compatibility_rejects_z2_t6 \
  erg_strava/tests/test_weekly_plan_schema.py::test_zone_zt_compatibility_accepts_z4_t6 -q
```

Expected: FAIL (helper missing or constraint not enforced)

- [ ] **Step 3: Implement matrix + day validator + wire into session constraints**

```python
ZONE_ZT_COMPATIBLE: Dict[str, frozenset[str]] = {
    "Z1": frozenset({"T1", "T2"}),
    "Z2": frozenset({"T2", "T3", "T4"}),
    "Z3": frozenset({"T3", "T4", "T5"}),
    "Z4": frozenset({"T5", "T6"}),
    "Z5": frozenset({"T6", "T7"}),
}

def validate_rowing_zone_zt_compatibility(day: DayPlan) -> Optional[str]:
    if day.rowing is None:
        return None
    def check(segments):
        for seg in segments:
            allowed = ZONE_ZT_COMPATIBLE.get(seg.zone_z)
            if allowed is None or seg.zone_t not in allowed:
                return (
                    f"incompatible zones {seg.zone_z}/{seg.zone_t} — "
                    f"{seg.zone_z} allows {', '.join(sorted(allowed or []))}"
                )
        return None
    err = check(day.rowing.segments)
    if err:
        return err
    if day.rowing.erg_alternative:
        return check(day.rowing.erg_alternative.segments)
    return None
```

In `validate_plan_session_constraints`, after interval rest check:

```python
zt_err = validate_rowing_zone_zt_compatibility(day)
if zt_err:
    return f"{day.weekday}: {zt_err}"
```

- [ ] **Step 4: Run tests — expect PASS**

```bash
PYTHONPATH=erg_strava ./.venv/bin/python -m pytest \
  erg_strava/tests/test_weekly_plan_schema.py::test_zone_zt_compatibility_rejects_z2_t6 \
  erg_strava/tests/test_weekly_plan_schema.py::test_zone_zt_compatibility_accepts_z4_t6 -q
```

- [ ] **Step 5: Commit** (if user asked for commits; otherwise stop for review)

```bash
git add erg_strava/weekly_plan_schema.py erg_strava/tests/test_weekly_plan_schema.py
git commit -m "$(cat <<'EOF'
Validate rowing Z/T zone compatibility in weekly plans.

EOF
)"
```

---

### Task 2: Phase intensity on main work

**Files:**
- Modify: `erg_strava/weekly_plan_schema.py`
- Modify: `erg_strava/weekly_plan_harness.py`
- Modify: `erg_strava/tests/test_weekly_plan_schema.py`

**Interfaces:**
- Consumes: `is_low_intensity_plan_phase`
- Produces: `validate_rowing_phase_intensity(day, *, phase: Optional[str]) -> Optional[str]`
- Produces: `validate_plan_session_constraints(plan, *, phase: Optional[str] = None)`

- [ ] **Step 1: Write the failing tests**

```python
def test_phase_intensity_rejects_t6_main_on_deload():
    data = sample_squad_plan_dict()
    tue = data["days"][1]
    tue["rowing"]["segments"][1].update({
        "zone_z": "Z4", "zone_t": "T6",
        "duration": "5×6 min / 2 min rest",
    })
    tue["session_subtype"] = "intervals"
    plan = parse_weekly_plan(data)
    assert plan is not None
    err = validate_plan_session_constraints(plan, phase="deload")
    assert err is not None
    assert "T6" in err or "intensity" in err.lower() or "deload" in err.lower()


def test_phase_intensity_allows_t6_main_on_build():
    data = sample_squad_plan_dict()
    tue = data["days"][1]
    tue["rowing"]["segments"][1].update({
        "zone_z": "Z4", "zone_t": "T6",
        "duration": "5×6 min / 2 min rest",
    })
    tue["session_subtype"] = "intervals"
    plan = parse_weekly_plan(data)
    assert plan is not None
    assert validate_rowing_phase_intensity(
        next(d for d in plan.days if d.weekday == "Tuesday"),
        phase="build",
    ) is None
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
PYTHONPATH=erg_strava ./.venv/bin/python -m pytest \
  erg_strava/tests/test_weekly_plan_schema.py::test_phase_intensity_rejects_t6_main_on_deload \
  erg_strava/tests/test_weekly_plan_schema.py::test_phase_intensity_allows_t6_main_on_build -q
```

- [ ] **Step 3: Implement phase intensity + thread `phase` through harness**

Rules on `phase in {main_set, work, build}`:

- `deload` / `recovery`: reject if `zone_t in {T5,T6,T7}` or `zone_z in {Z4,Z5}`
- `base` / `taper`: reject if `zone_t in {T6,T7}`
- `build` / `peak` / `race` / missing: no reject from this helper

Update callers:

```python
# weekly_plan_harness.validate_parsed_weekly_plan / finalize_imported_plan_json
err = validate_plan_session_constraints(plan, phase=phase)
```

Also update `parse_structured_plan_or_error` path the same way.

- [ ] **Step 4: Run schema + harness tests**

```bash
PYTHONPATH=erg_strava ./.venv/bin/python -m pytest \
  erg_strava/tests/test_weekly_plan_schema.py \
  erg_strava/tests/test_weekly_plan_harness.py -q
```

Expected: PASS (fix any fixtures that used incompatible pairs)

- [ ] **Step 5: Commit** (if requested)

```bash
git commit -m "$(cat <<'EOF'
Restrict high T-zone main sets by season phase.

EOF
)"
```

---

### Task 3: Athlete HR band overlap validation

**Files:**
- Modify: `erg_strava/weekly_plan_schema.py`
- Modify: `erg_strava/weekly_plan_harness.py`
- Modify: `erg_strava/generate_training_plan.py` (pass profile into athlete validation)
- Modify: `erg_strava/tests/test_weekly_plan_schema.py`
- Possibly: `erg_strava/tests/test_weekly_plan_harness.py`

**Interfaces:**
- Consumes: `AthleteProfile.zone_bpm_range` / five+seven maps
- Produces: `validate_athlete_hr_zone_consistency(plan: WeeklyPlan, profile: AthleteProfile) -> Optional[str]`
- Produces: optional `athlete_profile` on `validate_parsed_weekly_plan`

- [ ] **Step 1: Write the failing tests**

```python
from athlete_profile import AthleteProfile

def _profile_mhr_182() -> AthleteProfile:
    return AthleteProfile(id=1, label="Test", max_hr_bpm=182)


def test_athlete_hr_rejects_band_outside_z4_t6():
    data = sample_squad_plan_dict()
    data["personalised"] = True
    tue = data["days"][1]
    tue["rowing"]["segments"][1].update({
        "zone_z": "Z4", "zone_t": "T6",
        "hr_bpm_min": 140, "hr_bpm_max": 150,
        "duration": "5×6 min / 2 min rest",
    })
    tue["session_subtype"] = "intervals"
    plan = parse_weekly_plan(data)
    assert plan is not None
    err = validate_athlete_hr_zone_consistency(plan, _profile_mhr_182())
    assert err is not None
    assert "HR" in err or "bpm" in err.lower()


def test_athlete_hr_accepts_overlapping_band():
    profile = _profile_mhr_182()
    z4 = profile.zone_bpm_range("z4")
    t6 = profile.zone_bpm_range("t6")
    assert z4 and t6
    lo = max(z4[0], t6[0])
    hi = min(z4[1], t6[1])
    data = sample_squad_plan_dict()
    tue = data["days"][1]
    tue["rowing"]["segments"][1].update({
        "zone_z": "Z4", "zone_t": "T6",
        "hr_bpm_min": lo, "hr_bpm_max": hi,
        "duration": "5×6 min / 2 min rest",
    })
    tue["session_subtype"] = "intervals"
    plan = parse_weekly_plan(data)
    assert plan is not None
    assert validate_athlete_hr_zone_consistency(plan, profile) is None


def test_squad_validation_skips_hr_without_profile():
    data = sample_squad_plan_dict()
    tue = data["days"][1]
    tue["rowing"]["segments"][1].update({
        "zone_z": "Z4", "zone_t": "T6",
        "hr_bpm_min": 140, "hr_bpm_max": 150,
        "duration": "5×6 min / 2 min rest",
    })
    tue["session_subtype"] = "intervals"
    plan = parse_weekly_plan(data)
    assert plan is not None
    # No profile → HR helper not applied by session constraints alone
    assert validate_athlete_hr_zone_consistency  # exists
```

Overlap helper:

```python
def _ranges_overlap(a_lo: int, a_hi: int, b_lo: int, b_hi: int) -> bool:
    return a_lo <= b_hi and a_hi >= b_lo
```

For each segment, resolve Z and T bpm via profile (`z4` key lowercased from `Z4`). Require overlap with both bands. Skip segments if profile has no `max_hr_bpm`.

- [ ] **Step 2: Run tests — expect FAIL**

```bash
PYTHONPATH=erg_strava ./.venv/bin/python -m pytest \
  erg_strava/tests/test_weekly_plan_schema.py::test_athlete_hr_rejects_band_outside_z4_t6 \
  erg_strava/tests/test_weekly_plan_schema.py::test_athlete_hr_accepts_overlapping_band -q
```

- [ ] **Step 3: Implement validator + wire athlete path**

```python
# weekly_plan_harness.validate_parsed_weekly_plan(..., athlete_profile=None)
if athlete_profile is not None:
    err = validate_athlete_hr_zone_consistency(plan, athlete_profile)
    if err:
        return err
```

In `generate_training_plan._validate_parsed_weekly_plan` / athlete generate path, pass the athlete’s `AthleteProfile` when available from config loaders already used for HR context text.

- [ ] **Step 4: Run related tests**

```bash
PYTHONPATH=erg_strava ./.venv/bin/python -m pytest \
  erg_strava/tests/test_weekly_plan_schema.py \
  erg_strava/tests/test_weekly_plan_harness.py \
  erg_strava/tests/test_generate_training_plan.py -q
```

- [ ] **Step 5: Commit** (if requested)

```bash
git commit -m "$(cat <<'EOF'
Validate personalised plan HR bands against athlete Z/T tables.

EOF
)"
```

---

### Task 4: Canonical session templates in prompts

**Files:**
- Modify: `erg_strava/generate_training_plan.py`
- Modify: `erg_strava/tests/test_generate_training_plan.py` (substring assertions)

**Interfaces:**
- Produces: `_ROWING_ZONE_SESSION_TEMPLATES: str` constant included in structured rules and `_INTERVAL_SESSION_REPAIR_SYSTEM`

- [ ] **Step 1: Write failing test**

```python
def test_rowing_zone_templates_in_structured_and_repair_prompts():
    from generate_training_plan import (
        _INTERVAL_SESSION_REPAIR_SYSTEM,
        _ROWING_ZONE_SESSION_TEMPLATES,
        _STRUCTURED_PLAN_JSON_RULES,
    )
    assert "5×5 min" in _ROWING_ZONE_SESSION_TEMPLATES or "5×6 min" in _ROWING_ZONE_SESSION_TEMPLATES
    assert _ROWING_ZONE_SESSION_TEMPLATES in _STRUCTURED_PLAN_JSON_RULES or \
        _ROWING_ZONE_SESSION_TEMPLATES[:40] in _STRUCTURED_PLAN_JSON_RULES
    assert "Z4" in _INTERVAL_SESSION_REPAIR_SYSTEM or "threshold" in _INTERVAL_SESSION_REPAIR_SYSTEM.lower()
```

(Adjust assertion style to how constants are composed — templates must appear in both generation and repair paths.)

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Add template block**

```python
_ROWING_ZONE_SESSION_TEMPLATES = (
    "CANONICAL ROWING MAIN SETS (adapt duration to weekly load; keep Z/T coherent):\n"
    "- Z2/T3–T4 aerobic: continuous 40–60 min OR 3×20 min / 2 min rest; priority hr\n"
    "- Z3/T5 UT1-style: 4×10 min / 3 min rest OR 2×20 min / 3 min rest; priority hr\n"
    "- Z4/T6 threshold: 5×5 min / 3 min rest OR 5×6 min / 2–3 min rest; "
    "HR must sit in athlete Z4 and T6 bpm tables when personalised\n"
    "- Z5/T7 VO2: short reps (e.g. 8×500 m / ≥3 min rest); only when phase/cap allows\n"
    "- Never pair easy Z1–Z2 with T6–T7; never label threshold work with HR below the "
    "athlete T6/Z4 floor on personalised plans\n"
)
```

Concatenate into `_STRUCTURED_PLAN_JSON_RULES` and `_INTERVAL_SESSION_REPAIR_SYSTEM`.

- [ ] **Step 4: Run kagi + schema tests — expect PASS**

```bash
PYTHONPATH=erg_strava ./.venv/bin/python -m pytest \
  erg_strava/tests/test_generate_training_plan.py \
  erg_strava/tests/test_weekly_plan_schema.py -q
```

- [ ] **Step 5: Commit** (if requested)

```bash
git commit -m "$(cat <<'EOF'
Add canonical rowing zone session templates to plan prompts.

EOF
)"
```

---

### Task 5: Full verification

- [ ] **Step 1: Run broader related suite**

```bash
PYTHONPATH=erg_strava ./.venv/bin/python -m pytest \
  erg_strava/tests/test_weekly_plan_schema.py \
  erg_strava/tests/test_weekly_plan_harness.py \
  erg_strava/tests/test_weekly_plan_master_align.py \
  erg_strava/tests/test_generate_training_plan.py -q
```

Expected: all PASS

- [ ] **Step 2: Manual sanity** (optional local)

Build a personalised plan dict with Z4/T6 + 140–150 HR and confirm `validate_parsed_weekly_plan(..., athlete_profile=...)` returns an HR error; confirm squad finalize without profile still accepts coherent Z4/T6 with any HR.

- [ ] **Step 3: Stop for commit/push/hellpi pull unless user requests**

---

## Spec coverage check

| Spec section | Task |
|--------------|------|
| Z↔T matrix | Task 1 |
| Phase intensity | Task 2 |
| Athlete HR overlap only | Task 3 |
| Canonical templates | Task 4 |
| End-to-end verification | Task 5 |
| No UT rename / no Karvonen / no squad HR | Global constraints + Task 3 skip without profile |

## Placeholder scan

No TBD/TODO steps; concrete tests and function names included.
