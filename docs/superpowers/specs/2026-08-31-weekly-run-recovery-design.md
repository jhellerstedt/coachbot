# Weekly Run Recovery — Design Spec

**Date:** 2026-08-31  
**Status:** Draft (for implementation plan)  
**Trigger:** Sunday 30 Aug weekly cron. Public squad plan posted as prose-only; no athlete plan DMs; plot caption `new data since 2026-08-23: (none)`; logged erg stats ~0 min despite Jack’s Tue 27 Aug Zulip screenshot (8170 m / 36 min) and off-plan Suunto ergs; gym exercises identical to the previous week.

**Related:** `docs/superpowers/specs/2026-08-24-athlete-plan-structure-lock-design.md` (athlete DMs require squad `plan_json`; this spec supplies that JSON when the LLM fails).

## Goal

The weekly cron always produces a structured squad `plan_json`, always DMs athletes who logged anything that week (erg, screenshot, or gym), includes screenshot and Suunto sessions in public averages, rotates the configured gym accessory, and DMs a mapped athlete when *their* configured data source fails to parse or sync.

## Decisions

| Topic | Choice |
|-------|--------|
| Squad JSON on LLM/import failure | Build a deterministic week from the session library + gym program. Never save or post a prose-only plan. |
| Gym overlay | Always stamp `apply_program_gym_to_plan` after library sessions, including on the deterministic fallback. |
| Gym accessory rotation | `after_weeks: 1` in `base.json` and `build.json` (BSS → kettlebell swings the week after the first programmed week). Matches the 30 Aug expectation. |
| Gym week_index if last cache has no `gym_program` | Walk back through saved weekly plans until a `gym_program.week_index` is found; else start at 0. |
| Athlete plan DMs | Unchanged lock path. Once squad JSON exists, clone + overlay. Still never DM raw prose. |
| Who gets a plan DM | Anyone with Zulip mapping **and** a training log that week: Strava, merged erg, screenshot score, **or Zulip gym log**. |
| Public erg averages | HR-stream points remain primary. Fold in screenshot / merged-score minutes when that session is not already in the stream dataframe (dedupe on suunto key / Strava id / score id). |
| Plot “new data” caption | Keep Suunto-key diffs. Also list screenshot scores whose `session_date` is after the last plot run. |
| Suunto binary | If the configured path is missing, try fallbacks (do not raise on the first miss). Log which path was used. |
| Athlete error DMs | One private message per athlete per weekly run when a **mapped** source fails. Requires `zulip_user_id` or `zulip_email`. Do not DM athletes who are not mapped to that source (Emil is not in `suunto.athlete_ids` → no Suunto alert). |

## Mapping rules (error DMs)

A source is mapped for an athlete only if all of the following hold:

| Source | Mapped when | Typical failure |
|--------|-------------|-----------------|
| `suunto` | `suunto.enabled` and (`suunto.athlete_ids` is null **or** contains the athlete id) | Binary missing, whoami/list/FIT error, screenshot in week with no matching indoor-row workout |
| `strava` | Athlete has `token_dir` in config | Token athlete-id mismatch, auth error **when Suunto is not the primary path for that athlete** |
| `screenshot` | Athlete has `zulip_user_id` or `zulip_email` | Weekly run finds an erg_score in the review week with missing `duration_sec` / unusable metrics |

Not an error (do not DM):

- Suunto skipped because the athlete is **not** in `suunto.athlete_ids`
- Sync succeeded and the athlete simply has no new workouts (rest week) **unless** a screenshot exists in the same week with no matching Suunto indoor row

Send at most **one** error DM per athlete per weekly run. Concatenate distinct issues. This is not the weekly plan; say so in the message.

Copy (Suunto sync failure):

```
**Coachbot could not refresh your Suunto data**

Your Suunto workouts did not sync this week, so watch/HR streams and off-plan ergs may be missing from the squad summary.

If you posted a Concept2 screenshot to Zulip, that still counts. Otherwise reply here with the session.

_This is not your weekly plan._
```

## Deterministic squad JSON

Fixed schedule (same as the LLM prompt):

- Monday gym, Tuesday erg, Wednesday gym, Thursday on-water (erg alternative = same library rowing), Friday rest, Saturday rest, Sunday rest
- Tuesday / Thursday / `recommended_erg` from `select_sessions_for_week`
- Monday / Wednesday from `apply_program_gym_to_plan` at `next_gym_week_index`

Sunday stays **rest**. Do not invent optional recovery rowing without warm-up/cool-down (that is what failed validation on 30 Aug).

Render with `render_plan_text` and post that. Season alignment still runs on this JSON.

## Public stats from screenshots

For each athlete-week screenshot/merged score not already covered by stream points:

- Minutes: `session_parts[].duration_sec` if present, else `metrics.duration_sec`
- Zone: classify `avg_hr` (part or session) with `AthleteProfile.classify_five_zone_hr`
- Split: `avg_split_500_sec`

A 36 min screenshot at 141 bpm must contribute ~36 min to Z1–Z3 (or the matching zone), not round to 0.

## Out of scope

- Changing intensity-cap policy or the 45 min session cap
- DMing the public stream about Suunto outages
- Adding Emil/James/Vini to `suunto.athlete_ids`
- Live screenshot-ingest DMs inside the coach bot (weekly run only for this change)
- Production symlink (optional ops after deploy; code fallbacks should make it unnecessary)
