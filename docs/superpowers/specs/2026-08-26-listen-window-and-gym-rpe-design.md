# Stream Listen Window and Gym Session RPE — Design Spec

**Date:** 2026-08-26  
**Status:** Approved  
**Trigger:** Zulip `#general` / `project-640` 2026-08-26. Jack `@coach` gym log (106118) included a separate `RPE 4` paragraph. Coach logged the session and still asked how the last set felt (106119). Jack replied `RPE 4` with no mention (106120); the bot ignored it. recipe_bot already listens to unmentioned follow-ups in an active thread; coachbot should do the same with a 10-minute rolling window.

**Updates:** 2026-08-24 shared gym-session mentions — “missing RPE” now means the last weighted set only, not every set.

## Goal

Two units, one incident:

1. After a conversational stream reply, listen to that stream/topic for 10 minutes without requiring `@coach`. An LLM triages squad chatter vs coach follow-ups. Structured RPE replies skip triage and use the existing RPE handler.
2. A session-level RPE on the original gym post is applied to the last weighted working set so the bot does not ask for RPE when it was already given.

## Decisions

| Topic | Choice |
|-------|--------|
| Listen model | Event-driven state, same shape as recipe_bot, with a rolling 10-minute idle timeout instead of thumbs-up close. |
| Who can follow up | Anyone in the topic. LLM triage decides whether the message is a coach follow-up. |
| When the window opens | After a conversational coach stream reply (gym/erg confirmation, RPE recorded, coaching Q&A, thumbs-down acknowledgement). |
| When it does not open | Weekly plan and HR-plot posts (cron / `send_to_zulip`, not the bot reply path). |
| Refresh | Each conversational coach reply in that topic resets `listen_until` to now + 10 minutes. |
| `@coach` | Always handled, with or without a window. |
| DMs | Unchanged (already handled without a mention). |
| Structured RPE in window | `RPE 4` / `6` / `easy` / `moderate` / `hard` / `max effort` skip triage and use `_handle_gym_rpe_follow_up`. |
| Other follow-ups | After triage says yes, run the same stream handler as a mention (gym, erg, nearby screenshot, then Q&A). |
| Triage failure | Skip (do not spam `#general`). Mentions are unaffected. |
| Session-level gym RPE | Last weighted working set only, not every set. |
| Ask for RPE | Only when that last weighted set still has no RPE. |

## Current behaviour (bugs)

Stream `CoachMessageHandler.handle` returns immediately unless `bot_mentioned`. Unmentioned `RPE 4` never reaches `_handle_gym_rpe_follow_up`.

Gym parse already *can* put session-level RPE on the last set (106118 stored `rpe: 4` on the last Russian-twist set). `gym_log_missing_rpe` still returned true because it requires RPE on **every** weighted set, so 106119 asked anyway. The follow-up prompt is about the last set; the missing check must match that.

## Listen window

### State

Persist under the bot cache dir, e.g. `coach_listen_state.json`:

```json
{
  "threads": {
    "general|project-640": {
      "listen_until": "2026-08-26T00:00:00+00:00"
    }
  }
}
```

Key is `stream|topic`. `listen_until` is UTC. Missing or corrupt file → empty state; rewrite on the next conversational reply. Container restart keeps an unexpired window.

Helpers (new small module, recipe_bot `state.py` / `triggers.py` as the pattern):

- `activate_listen_window(state, stream, topic, *, now, duration_seconds=600)` — set `listen_until`.
- `listen_window_open(state, stream, topic, *, now) -> bool`
- load / save

Do not poll Zulip. Do not re-fetch topic history to infer recency.

### Open / refresh

In `coach_bot/main.py`, after a successful `_reply` for a **stream** message (including reaction replies that produce a stream post), activate/refresh the window for that stream/topic.

Weekly plan and HR plots never call this path, so they do not open a window.

If `_reply` fails, do not activate. If the handler returns `None`, do not send and do not refresh.

### Incoming unmentioned stream message

Still require `in_scope_stream`. Ignore the bot’s own posts (already). Then:

1. If `listen_window_open` is false → ignore (same as today).
2. If `parse_rpe_follow_up_reply(body)` is not `None` → existing RPE handler. No triage.
3. Else LLM triage (OpenRouter, same key as coaching). Reply only when the message is a coach follow-up (question, correction, extra set, how the session felt, asking what to do next). Skip acknowledgements and squad chatter (“erg tomorrow 7am?”, “Come to the klurb”).
4. If triage says yes → same stream pipeline as a mention: elaboration, erg text, RPE, nearby screenshot, then `_reply_kagi`.
5. If triage says no, or triage fails → silent. Window is not refreshed.

Sender for RPE / gym / Q&A is the **follow-up author**, using existing athlete resolution. Jack’s `RPE 4` updates Jack’s copy and tagged copies that share `zulip_message_id`. James’s `RPE 4` updates only James’s copy. Unmapped sender → existing unmatched-sender help.

### Triage

Coaching-specific prompt, JSON `{"should_reply": bool, "reason": "..."}`. Default `should_reply` to false when the payload is malformed. On API/parse exception: skip (log a warning).

Cheap ack-only regex (thanks / ok / yep) may short-circuit to skip before the LLM, as recipe_bot does.

## Gym session RPE on the original post

### Session-level value

After `parse_gym_session_metrics` returns, scan the workout transcript for lines that are *only* an effort phrase. Last matching line wins. Apply that RPE to the last weighted working set if that set has no `rpe` yet (`apply_rpe_to_last_working_set` already skips plank / duration / weight ≤ 0).

A transcript line counts as session-level RPE only if it contains `rpe` or a word (`easy` / `moderate` / `hard` / `max effort`). A bare number line (`6`) is not treated as session RPE inside a gym log (it can be a weight). A follow-up **message** whose entire body is `6` still counts, via the existing `parse_rpe_follow_up_reply`.

Do not write that session-level value onto every set. Per-set RPE already in the LLM JSON (e.g. a set line that named its own effort) is left alone.

If the LLM already set the last weighted set (this morning’s Russian twist), the deterministic pass is a no-op and the missing check below still prevents a follow-up ask.

Tagged copies share the `gym` dict, so they get the same last-set RPE.

### When to ask

Change `gym_log_missing_rpe` to true only when the last weighted working set has no RPE (or there is no such set). Other sets without RPE do not trigger the prompt.

Harness rule 11: session-level effort → last working set; if none given, `rpe` null. Backup only; deterministic overlay is the source of truth for a trailing `RPE 4`.

## Error handling

- Triage JSON/API failure: skip; `@coach` still works.
- Corrupt listen-state file: empty windows; next conversational reply rewrites the file.
- Structured RPE in an open window, no gym log in the 72-hour window: existing short error; do not fall through to Q&A.
- Handler returns nothing after triage-yes: stay silent; do not refresh.
- Bot’s own posts ignored.

## Out of scope

- Changing recipe_bot.
- Opening a listen window from cron weekly-plan / HR-plot posts.
- Listening in streams/topics the bot is not configured for.
- Fixing gym exercise mash (barbell-row sets mixed with other lifts on 106118).
- Closing the window on thumbs-up (recipe_bot); idle timeout only.
- Persisting triage transcripts.

## Testing

No live Zulip. Stub OpenRouter/triage as existing handler tests stub interpret.

Listen window:

- Activate, expire after 10 minutes, refresh on another conversational reply.
- Missing/corrupt state file behaves as empty.
- Unmentioned `RPE 4` in an open window records RPE and does not call coaching Q&A.
- Unmentioned squad chatter in an open window does not reply (triage false or ack-only).
- `@coach` still works with no window.
- Handler returning nothing does not refresh the window.

Gym:

- Transcript ending in `RPE 4` puts 4 on the last weighted set; tagged copies match; bot does not ask.
- Same workout with no effort line still asks.
- `gym_log_missing_rpe` is true only when the last weighted set has no RPE.
