# Athlete Plan Structure Lock — Design Spec

**Date:** 2026-08-24  
**Status:** Approved  
**Trigger:** Zulip 2026-08-23 — squad plan (stream 105696) vs Jack H DM (105694). Gym exercises/reps matched; Tue/Thu warm-up/cool-down durations, Thursday erg alternative, and recommended 4×2000 m did not. Cron: `Athlete plan (Jack H): alignment skipped (imported plan_text failed validation: Thursday: session_type must match squad)`. Cached athlete plan had `plan_text` only.

**Supersedes (athlete DMs only):** 2026-06-20 structured-output spec, “On parse/validation failure: … fall back to legacy prose-only generation.” Squad public posts are unchanged.

## Goal

Athlete weekly-plan DMs must be the squad week’s session structure with only two classes of numeric change: gym kg, and erg split / HR targets. If the LLM does not return usable athlete JSON, still send that locked plan (squad splits, athlete HR bpm and gym loads).

## Decisions

| Topic | Choice |
|-------|--------|
| Structure source | Squad `plan_json` is canonical. Athlete JSON is a clone plus overlays. |
| LLM role | Optional source of `split_min` / `split_max` only. Never durations, exercises, reps, session types, extra segments, or gym kg. |
| Gym kg | Existing `personalize_plan_gym_loads` (lift history + squad pyramid). Not LLM weights. |
| HR | Overlay bpm from `AthleteProfile` zone tables onto every rowing segment (days, erg alternative, `recommended_erg`). |
| LLM / import failure | Still send the locked clone. Never DM raw prose. |
| Missing JSON after lock | Treat as a bug; do not fall back to `plan_text`. |
| Prose importer | Still used only to harvest splits. Fix `**Session Type:**` parsing so Thursday on-water is not classified as rest. |

## What must stay identical

Copied from squad onto the athlete plan:

- Day count, weekday, date, `session_type`, `session_subtype`
- Gym category, goal, exercise names and order, set count, reps, hold `duration_sec`
- Rowing segment count, `phase`, `label`, `duration`, `zone_z`, `zone_t`, `priority`, notes
- On-water `erg_alternative` presence and segment shape
- Top-level `recommended_erg` (same library id / interval format)
- Fri/Sat spill sessions and Sunday rest/recovery as in the squad plan

## What may change

| Field | Source when LLM succeeds | Source when LLM fails |
|-------|--------------------------|------------------------|
| Gym `weight_kg` | Lift-history scaling of the squad pyramid | Same |
| `hr_bpm_min` / `hr_bpm_max` | Athlete zone bpm for that segment’s T then Z code | Same |
| `split_min` / `split_max` | LLM or imported prose, if valid, matched by weekday + phase | Squad splits unchanged |
| `personalised` / `greeting` | `true` / athlete first name | Same |

If the athlete has no `max_hr_bpm`, leave squad HR values. If an exercise has no lift logs, `personalize_plan_gym_loads` keeps the squad peak (scale factor 1).

## Data flow

```
squad_plan_json
        │
        ▼
generate_athlete_weekly_plan  ──►  optional LLM JSON or imported prose (proposal)
        │
        ▼
lock_athlete_plan_to_squad(squad, proposal, profile, lift_logs)
        │
        ├─ clone squad days + recommended_erg
        ├─ personalize_plan_gym_loads
        ├─ overlay HR from AthleteProfile
        ├─ overlay valid splits from proposal (ignore everything else)
        └─ copy_recommended_erg_from_squad (replaces extra from squad, then HR)
        │
        ▼
validate_athlete_plan_against_squad (expanded) → render plan_text → DM + cache
```

Call `lock_athlete_plan_to_squad` inside `generate_athlete_weekly_plan` whenever `squad_plan_json` is present, so hypothetical-week and phase-review callers get the same guarantee. Remove the later `personalize_plan_gym_loads` / `copy_recommended_erg_from_squad` calls from that function so gym kg is not scaled twice.

Proposal passed into lock:

1. Structured `plan_json` from the LLM if it parsed, else
2. Raw imported prose JSON even when `finalize_imported_plan_json` failed (lock only reads splits; a Thursday typed as `rest` simply yields no Thursday overlay), else
3. `None`

`send_weekly_athlete_plan_dms` must not use `generated.plan_text` when `plan_json` is missing. `lock_athlete_plan_to_squad` always returns a dict if squad JSON parses; if squad JSON does not parse, skip the DM and log — do not send markdown `### Thursday` prose.

## `lock_athlete_plan_to_squad`

New focused module: `erg_strava/athlete_plan_lock.py`.

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
    """Clone squad structure; overlay athlete gym kg, HR, and optional splits."""
```

### Clone

Deep-copy squad JSON. Set `personalised=True` and `greeting` (first name + comma, same as today). Keep `session_library` / `gym_program` from squad.

### Gym

If `include_lifting`, call existing `personalize_plan_gym_loads(clone, squad_plan_json, lift_logs_by_exercise=..., program=load_program_from_plan(squad))`. That already keeps names, order, and the descending 5/7/8 (or hold) pyramid and only scales kg.

Do not copy LLM gym weights onto the clone.

### HR overlay

Extend the existing `_hr_range_for_segment` / `personalize_recommended_erg` logic to every rowing segment on every day, including `erg_alternative` segments. Prefer T-zone range, else Z-zone. Skip overlay when the profile has no matching range.

Put the shared overlay helper next to `personalize_recommended_erg` in `weekly_plan_schema.py` (e.g. `personalize_plan_rowing_hr(plan, profile) -> WeeklyPlan`).

### Split overlay

If `proposal_json` parses:

- For each squad rowing segment (and each erg-alternative segment), find the proposal segment on the same weekday with the same `phase`, matching in order among that phase.
- Copy only `split_min` and `split_max` when both match `^\d{1,2}:\d{2}$` and min seconds ≤ max seconds.
- Ignore extra proposal segments (e.g. a separate `rest` line). Ignore `duration`, `label`, `session_type`, gym, and `recommended_erg` from the proposal.

If `proposal_json` is missing or unparseable, leave squad splits.

### Recommended extra

Keep calling `copy_recommended_erg_from_squad` after the clone so a missing extra on the proposal cannot drop the squad 4×2k (or whatever the squad chose). That helper already overlays recommended-erg HR from the profile. Day-session HR overlay runs on the seven days separately.

## Expanded structure check

Extend `validate_athlete_plan_against_squad` so a locked plan (and tests) fail if structure drifted:

- Existing: day count, weekday, `session_type`, gym category, exercise names/order
- New: per gym exercise, set count, each set’s `reps` and `duration_sec`
- New: per rowing day, segment count; per segment `phase` and `duration` (string equality after strip)
- New: `erg_alternative` is None on both or present on both with the same segment count / phases / durations
- New: `recommended_erg` is None on both, or both present with the same `id` and the same segment phases / durations

This check does **not** compare kg, splits, or HR bpm.

After lock, `generate_athlete_weekly_plan` runs this validator. A violation is a lock bug: log it; still do not send unlocked prose.

## Prose importer (secondary)

`plan_text_import._ATHLETE_SESSION_TYPE_RE` currently is `\*Session Type:\s*(.+?)\*`. On Zulip/markdown `**Session Type:** On Water` the non-greedy group captures empty, so Thursday becomes `rest` and import fails squad-type validation.

Change the header match to allow optional wrapping asterisks and capture the type text, e.g. treat `**Session Type:** On Water` as `On Water` → `on_water`. Keep using import only as `proposal_json` for split overlay, not as the DM body.

`_generate_structured_plan_with_fallback` may still return `plan_json=None` plus prose. Import that prose as `proposal_json` even when `finalize_imported_plan_json` would fail; lock only reads splits.

## Out of scope

- Regenerating or re-DMing the 2026-08-24 week
- Changing how the squad plan is generated or posted
- Deterministic split targets from erg history (LLM remains the split source when it returns valid M:SS)
- Skipping athlete DMs when the LLM fails

## Testing

Add tests (no live Zulip/LLM):

1. **Lock vs this week’s drift.** Squad-shaped fixture with Tue/Thu 18 min WU/CD, Thursday `on_water` + erg alternative, `recommended_erg` 4×2k, gym 5/7/8 descending kg. Proposal = Jack’s DM structure (12 min WU/CD, extra Rest segments, no erg alt, no recommended extra, inverted gym kg). Locked result must keep 18/18, erg alt, 4×2k, 5/7/8 reps and descending pyramid; HR from a Jack-like profile; splits from proposal only where phases match.

2. **No proposal.** `proposal_json=None` still returns valid personalised JSON; splits equal squad; gym scaled from logs; `validate_athlete_plan_against_squad` passes.

3. **DM path.** `send_weekly_athlete_plan_dms` (or the render helper it uses) never sends a body that starts with `### Monday` markdown when lock can run. If `generate_athlete_weekly_plan` returns `plan_json=None` without a squad JSON, skip send (existing skip/log).

4. **Importer.** `import_weekly_plan_json_from_text` on a `**Session Type:** On Water` Thursday block yields `session_type=on_water`, not `rest`.

5. **Expanded validator.** Athlete JSON with squad gym names but 12 min vs 18 min WU fails `validate_athlete_plan_against_squad`.
