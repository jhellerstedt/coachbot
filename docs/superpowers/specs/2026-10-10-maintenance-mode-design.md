# Maintenance Mode — Design Spec

**Date:** 2026-10-10
**Status:** Approved

## Goal

A config flag pauses the Sunday squad pipeline. While it is on, an athlete can ask coachbot for one session. The bot syncs that athlete, writes a single day from their latest training data and the request, replies in Zulip, and stores the day as today's prescription so a later gym or erg log is checked against it.

## Decisions

| Topic | Choice |
|-------|--------|
| Switch | Top-level `maintenance_mode` in `erg_strava/config.yaml`. On only when the value is boolean `true` or the strings `true`, `yes`, `on`, or `1` (any case). Missing, `false`, and anything else are off. |
| Sunday job | `strava_erg_hr_plot.py` exits 0 immediately after a successful config load. No sync, plot, Zulip post, or plan generation. Manual flags on that process (`--plot-only`, `--full-sync`, `--refresh-season-plan`, and the rest) do not bypass the exit. |
| Session storage | Replace only today's day on that athlete's personalised week plan. Gym and erg log comparison already prefer that file. |
| Same-day repeat | A second request overwrites today's day. Other days in that week stay. |
| Logs and questions | Erg screenshots, erg text, gym logs, RPE follow-ups, and profile updates stay on their current paths. |
| Plan adjustments | While the flag is on, do not enqueue. Reply that weekly plans are paused. |
| Coaching Q&A | While the flag is on, answer from cached athlete history even when this week has no squad plan. Include today's saved day when one exists. |
| Who is synced | The existing coach subject: the sender, or another athlete they @-mention. |

## Flag

Document the key in `erg_strava/config.example.yaml`, commented off:

```yaml
# maintenance_mode: true
```

Pure helpers live in `erg_strava/maintenance_mode.py`: the flag check, request classification, day validation, and saving today's day. The handler calls them and owns the Zulip posts and the sync call. The weekly script and the bot both load the same YAML file (`CONFIG_PATH` for the bot, the script's config argument for Sunday).

## Sunday exit

In `strava_erg_hr_plot.main`, after `load_config` succeeds and the raw YAML is loaded, if the flag is on:

1. Print `Maintenance mode is on; skipping weekly sync and plan generation.`
2. `sys.exit(0)`.

This happens before credential lookup, the `--plot-only` branch, sync, plotting, Zulip, and Kagi. `deploy/run_weekly_plan.sh` stays the cron entry and does not grow its own check.

A missing config file still exits with the existing error. The maintenance exit runs only when config loaded.

## What the bot still does

`CoachMessageHandler.handle` keeps today's order for logs:

1. Erg screenshot (mention or DM).
2. Erg score text.
3. Gym RPE follow-up.
4. Nearby erg screenshot on stream mentions.

Those return before any maintenance classification.

Then, only while the flag is on, one helper runs for DMs, @-mentions, and listen-window follow-ups that already reach the stream body handler:

1. **Plan adjustment.** `looks_like_plan_adjustment` replies exactly: `Weekly plans are paused. Ask for a single session, for example: gym session, leg day, 45min.` Nothing is written to `plan_adjustments/`. If a message skips this regex and `_reply_kagi` still classifies it as `plan_adjustment`, that path replies with the same sentence and does not enqueue.
2. **Incomplete session request.** One clarifying question that names the missing piece (focus, duration, or interval versus steady). No sync, no model call, no save. The reply does not contain `Saved as today's prescription.`
3. **Complete session request.** The sync-and-prescribe flow below.
4. **Anything else.** Existing `_reply_kagi`.

`_reply_kagi` in maintenance mode does not return `missing_plan_reply` when the squad week file is absent. It still includes the subject's training context and, when the personalised plan has a day for today, that day's rendered text.

An unmapped sender with no resolvable subject gets the existing unmatched-account reply. No sync and no save.

## Session requests

Classification returns one of `complete`, `incomplete`, or `not_a_request`.

A message is a candidate only when it asks for a session. Request cues are `session`, `workout`, `give me`, `what should`, `plan me`, and `write me`. `I have` counts only when a duration follows (`I have an hour`, `I have 45`). A message under 80 characters that names gym or erg and a focus or a duration is also a candidate. Set/rep/load notation (`5x5`, `8r 40`, a weight in kg) is `not_a_request` even if the word gym or erg appears, so a completed workout still falls through to logging.

Regex is the certain path for candidates. These three messages are complete:

- `gym session, leg day, 45min`
- `interval erg, I have an hour`
- `steady state erg time and target HR`

Rules when the regex can see a modality:

| Request | Complete when | Otherwise |
|---------|---------------|-----------|
| Gym | A focus and a duration are both present. Focus is one of legs, push, pull, upper, lower, full body, chest, back, arms. | Incomplete. Ask for the missing focus, the missing duration, or both. |
| Interval erg | A duration is present (`45min`, `45 min`, `an hour`, `60 minutes`, and the same shape). | Incomplete. Ask how long they have. |
| Steady erg | Always, once it is a steady, steady-state, UT2, or aerobic erg request. Duration and heart rate may be absent; the model chooses both from zones and recent ergs. | — |
| Erg with neither interval nor steady | — | Incomplete. Ask whether they want intervals or steady state, and how long. |

If both interval and steady markers match, the request is incomplete: ask which one.

If no regex modality matches, call the existing OpenRouter choice helper with a `session_request` versus `other` question, and only while maintenance mode is on. Confidence below the existing `0.6` floor, a missing API key, or an error yields `not_a_request`. When the choice is `session_request`, a second structured extract returns modality, optional focus, and optional duration. The table above then decides complete versus incomplete.

`not_a_request` includes questions, erg logs, and gym logs that the earlier handlers did not already catch.

An incomplete reply asks for the missing piece in one sentence and does not offer a workout.

## Sync, then one day

For a complete request:

1. Post `Syncing your latest sessions…` to the same DM or stream topic, via the handler's Zulip client. Skip that post when the client is absent (tests).
2. Run the existing incremental `sync_athlete` for the subject only (Suunto first, same config objects the weekly job uses). Then rebuild that athlete's merged erg sessions and refresh gym metrics for their newly fetched activities, using the same helpers the weekly job uses after a sync.
3. Generate one `DayPlan` and save it.
4. Return the workout as the normal handler reply, so the listen window still opens after a stream reply.

Sync is inline on the bot's event loop. One athlete, incremental.

**Sync failure.** `sync_athlete` returning `False`, or any exception, is a failure. Keep whatever the cache already holds, including Suunto writes that landed before a later Strava failure. The workout reply starts with `Sync failed; this session uses your cached data.` Generation still runs when the modality has history.

**Empty history.** Checked after the sync attempt and the rebuild:

- A gym request needs at least one cached gym log or parsed gym activity for that athlete.
- An erg request needs at least one merged erg session, or an erg score if no merged session exists.

If that modality is empty, reply exactly `I don't have any recent {gym|erg} sessions cached for {label}, so I can't set loads or targets. Log a session and ask again.` Use `gym` or `erg` and the athlete's label. Do not call the model. Do not save.

## Generation

One OpenRouter call returns a single `DayPlan` JSON object, not a week. The prompt includes:

- the athlete's request, plus the parsed modality, focus, and duration when present
- their heart-rate zone text
- recent merged erg sessions (the existing coach training-context builder)
- recent gym logs and the existing gym tonnage summary
- an instruction to prescribe one session that fits the request, using their recent loads and zones, with absolute heart-rate targets on erg pieces

After a successful parse, overlay erg heart rate from the athlete profile the same way weekly plans do.

Accept the day only when all of these hold:

- `session_type` is `gym` for a gym request and `erg` for an erg request
- a gym day has at least one exercise with sets
- an erg day has rowing segments, and after the overlay every segment has `hr_bpm_min` and `hr_bpm_max`
- an interval day is not a single steady piece (repeated work, or an interval `session_subtype`)
- when the athlete named a duration, `estimate_day_session_minutes` is between half and one-and-a-half times that duration
- a steady request with no named duration skips the duration check

Anything else is invalid. Retry the model call once. If the second result is still invalid, or the call errors, reply `I couldn't write that session. Try the request again.` Leave the personalised plan file unchanged.

## Save

Target file: `cache_dir/athlete_{id}/weekly_plans/{week_id}.json` for the plan-timezone week that contains today. The squad week file is never written.

Steps:

1. Load the athlete's plan for that week, if any.
2. If `plan_json` is missing, import it from `plan_text` with the existing prose importer. If that import fails, continue with an empty day list.
3. Replace the day whose `date` or `weekday` is today. If no such day exists, append today's day.
4. Re-render `plan_text` from the resulting `WeeklyPlan`.
5. Save through the existing athlete-plan writer, and record `maintenance_days` on the week JSON: a list of ISO dates this path has written. Writing today adds today once; it does not remove earlier dates in the list.

The Zulip workout reply is `render_day_text` for that day with absolute heart-rate numbers, then a blank line, then `Saved as today's prescription.` A sync-failure line, when needed, is the first line of that reply.

## Later logs

`prescribed_gym_section_for_log` and `prescribed_erg_section_for_log` already prefer the personalised plan for the session date. No new lookup. A gym or erg log on that date therefore compares against the saved day.

## Production sync

The weekly sync runs on the host. The bot runs in Docker, and that image does not contain `suuntool`. For an in-container `sync_athlete` to reach Suunto, `coach_bot/docker-compose.yml` gains read-only mounts:

- the host `bin/suuntool` at `/usr/local/bin/suuntool`
- the host Suunto session file at `/suunto/session.json`

Compose sets `SUUNTOOL_SESSION_FILE=/suunto/session.json`. `SuuntoClient` already copies the process environment, so that variable is visible when config has no `session_file`. If config sets an absolute `session_file` that is not mounted in the container, sync fails and the cache fallback above applies. `deploy/README.md` states these mounts.

The cache volume stays writable. `config.yaml` stays read-only. A missing binary or session is a sync failure, not a crash of the bot.

## Tests

- Flag: missing, `false`, `true`, and the string `true` (on). The string `no` is off.
- With the flag on, `strava_erg_hr_plot.main` returns before sync. A spy on `sync_athlete` is not called.
- With the flag off, that early return does not run.
- The three example prompts classify as complete session requests.
- A question, an erg-score text log, and a gym-log text do not.
- `gym session` and `interval erg` classify as incomplete and do not call sync or the model.
- Saving with no week file creates one day. Saving over an existing week replaces only today's day. A second save overwrites that day and leaves `maintenance_days` containing today once.
- `prescribed_gym_section_for_log` and `prescribed_erg_section_for_log` return the saved day's text.
- A sync function that raises still generates from a non-empty cache, and the reply contains `Sync failed; this session uses your cached data.`
- An empty gym cache on a gym request does not call the model and does not write a plan file.
- Invalid model JSON, then a second invalid result, does not write a plan file.
- A plan-adjustment message in maintenance mode does not append to `plan_adjustments/pending.jsonl`.
