# Zone / HR Consistency for Weekly Plan Generation — Design Spec

**Date:** 2026-07-23  
**Status:** Approved  
**Approach:** Validator-first (deterministic checks + existing structured/repair retry loops)  
**Related:** interval structure repair in `generate_training_plan.ensure_realistic_interval_sessions`; zone tables in `athlete_profile.py`

## Goal

Stop weekly plans from shipping incoherent intensity labels: incompatible `zone_z`/`zone_t` pairs, high T-zones in base/deload/recovery main sets, and (on personalised plans) HR bands that do not match the athlete’s pre-computed Z/T bpm tables.

## Non-goals

- Renaming schema fields to British Rowing UT2/UT1/AT/TR/AN only (keep `zone_z` / `zone_t`)
- Switching from % Max HR to Karvonen HRR (no resting HR in profiles)
- TypeScript session schemas
- Cursor `.cursorrules` as the control plane (production path is OpenRouter + Python validators)
- Squad-plan bpm validation against a default or Jack’s Max HR

## Decisions

| Topic | Choice |
|-------|--------|
| Enforcement style | Validator-first: reject → structured retry / repair feedback |
| Squad vs athlete | Squad: Z↔T + phase intensity only. Athlete DMs: those plus HR∈band vs profile |
| Zone naming | Keep Z1–Z5 + T1–T7; optional display synonyms from existing `SEVEN_ZONE_LABELS` later (out of scope for this change) |
| Auto-rewrite in alignment | No silent zone/HR rewrites; fail validation instead |
| Templates | Few-shot canonical sessions in structured + interval-repair prompts |

## Problem evidence

Observed failure modes:

1. Labels like `Z4/T6` with HR ~140–150 bpm for athletes whose T6/Z4 bands sit much higher — physiologically UT1-ish work mislabeled as threshold/AT.
2. Dual Z/T encoding is intentional in this repo (`ZONE_Z`, `ZONE_T`, season guidance, adherence). The bug is inconsistency across fields, not “mixing two random systems.”
3. Prompt-only rules already exist and are insufficient (same class of failure as flat Tuesday intervals before code validation).

## Part 1 — Z↔T compatibility matrix

### Behaviour

For every rowing segment (primary path and `erg_alternative`), require `(zone_z, zone_t)` ∈ compatibility matrix. Warm-up / cool-down / rest / active_recovery segments are included (they must still be coherent).

### Matrix (authoritative)

Aligned with `SEVEN_ZONE_LABELS` and current season language:

| zone_z | Allowed zone_t |
|--------|----------------|
| Z1 | T1, T2 |
| Z2 | T2, T3, T4 |
| Z3 | T3, T4, T5 |
| Z4 | T5, T6 |
| Z5 | T6, T7 |

Reject with a clear message, e.g.  
`Tuesday: incompatible zones Z2/T6 — Z2 allows T2–T4`.

### Placement

- New helper in `weekly_plan_schema.py` (or small `rowing_zone_rules.py` if schema grows further; prefer schema unless file size becomes painful)
- Called from `validate_plan_session_constraints` so squad and athlete structured paths both get it

## Part 2 — Phase intensity on main work

### Behaviour

For segments with `phase` in `{main_set, work, build}` only:

| Season phase | Rule |
|--------------|------|
| `deload`, `recovery` | `zone_t` must be ≤ T4 and `zone_z` ≤ Z3 |
| `base`, `taper` | `zone_t` must be ≤ T5 and `zone_z` ≤ Z4; **T6 and T7 forbidden** on main work |
| `build` | T6/T7 allowed; existing weekly Z5% caps in master-align remain the volume governor |
| `peak`, `race` | T6/T7 allowed; existing caps remain |

If `phase` is missing/unknown, skip this check (do not invent a phase).

### Placement

- `validate_plan_session_constraints(plan, *, phase: Optional[str] = None)` gains optional `phase`
- `weekly_plan_harness.validate_parsed_weekly_plan` / `finalize_imported_plan_json` already receive `phase` — pass it through
- Alignment post-check should re-run the same constraint helper when phase is known

## Part 3 — Athlete HR ∈ zone bands

### Behaviour

Only when an `AthleteProfile` with `max_hr_bpm` is available (personalised athlete plan path):

For each rowing segment:

1. Resolve bpm ranges for `zone_z` and `zone_t` via existing profile helpers (`zone_bpm_range` / five- and seven-zone maps).
2. Require the prescribed `[hr_bpm_min, hr_bpm_max]` to **overlap** the Z band **and** overlap the T band:
   - `hr_min <= zone_hi and hr_max >= zone_lo` for both Z and T.
3. Also require `hr_bpm_min <= hr_bpm_max`.

On failure, message must include weekday, zones, prescribed HR, and expected band(s), so retry feedback is actionable.

### Non-behaviour

- Squad public plan: **no** bpm overlap check
- Missing `max_hr_bpm`: skip HR check (log/skip silently in validator return `None` for that check)
- Do not auto-clamp HR to the band in alignment

### Placement

- `validate_athlete_hr_zone_consistency(plan, profile) -> Optional[str]`
- Invoke from athlete validation path in `validate_parsed_weekly_plan` when `squad_plan_json is not None` **and** a profile can be supplied
- Call sites that generate athlete plans must pass profile (or load by athlete id from config) into validation; if wiring profile through every call is heavy, accept an optional `athlete_profile` kwarg defaulting to `None` (check skipped when None — tests pass profile explicitly; production athlete generation must pass it)

## Part 4 — Canonical session templates (prompt only)

### Behaviour

Add a short few-shot block to:

1. Structured weekly plan system rules (`_STRUCTURED_PLAN_JSON_RULES` / rowing alignment clauses in `generate_training_plan.py`)
2. Interval repair system prompt (`_INTERVAL_SESSION_REPAIR_SYSTEM`)

Content (Z/T vocabulary, not UT-only primary labels):

- Z2/T3–T4 steady or long aerobic: continuous or `3×20 min / 2 min rest`
- Z3/T5 UT1-style: `4×10 min / 3 min rest` or `2×20 min / 3 min rest`
- Z4/T6 threshold: `5×5 min / 3 min rest` or `5×6 min / 2–3 min rest` with HR inside athlete T6/Z4 bands when personalised
- Z5/T7 VO2: short reps, rest ≥ 3 min; only when phase/cap allows
- Rest scales with intensity; easy work prefers `priority: hr`

No new schema fields (`zoneDistribution`, TypeScript, etc.).

## Validation flow (end-to-end)

```text
structured JSON
  → parse_structured_plan_or_error
  → validate_plan_session_constraints(plan, phase=...)
       includes interval rest, session cap, Z↔T, phase intensity
  → (athlete) validate_athlete_hr_zone_consistency(plan, profile)
  → on error: build_validation_retry_feedback → retry
prose import / interval repair
  → same finalize validators
alignment
  → must not reintroduce incompatible Z/T or forbidden phase intensity
  → athlete path: re-check HR bands when profile available
```

## Testing

- Unit tests for matrix accept/reject pairs
- Phase intensity: deload main T6 fails; build main T6 passes
- Athlete HR: Z4/T6 with 140–150 vs high MHR bands fails; overlapping band passes
- Squad path: no HR failure without profile
- Prompt constants contain at least one canonical template string (lightweight assert or snapshot of substring)

## Success criteria

1. Personalised plans cannot ship HR bands that miss both stated Z and T tables.
2. Any plan (squad or athlete) cannot ship Z2/T6-style incompatible pairs.
3. Base/deload/recovery/taper cannot ship T6/T7 as main-set intensity.
4. Canonical templates appear in generation + repair prompts.
5. Existing interval-structure and render tests remain green.
