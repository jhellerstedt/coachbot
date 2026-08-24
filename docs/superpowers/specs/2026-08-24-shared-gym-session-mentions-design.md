# Shared Gym Session Mentions — Design Spec

**Date:** 2026-08-24  
**Status:** Approved

## Goal

On a stream `@coach` gym log, tagging other athletes copies the same workout onto each of them. The sender keeps credit. Each tagged, mapped athlete also gets credit. The confirmation compares the shared workout to each credited athlete’s prescribed gym.

## Decisions

| Topic | Choice |
|-------|--------|
| Session types | Gym logs only. Erg scores, Q&A, and profile updates are unchanged. |
| Channel | Stream `@coach` messages only. Private DMs stay sender-only, including when they contain mentions. |
| Payload | Identical `gym` dict (sets, reps, weights, tonnage) for every credited athlete. |
| Recipients | Sender (if mapped) plus every other mapped `@`-mention, duplicates dropped. |
| Unmapped sender | Mentioned mapped athletes only (today’s “log for the tagged person”). |
| Unresolved mentions | Skip silently. Mapped recipients still log. |
| Confirmation | One reply; per-athlete heading, log id, and prescribed-vs-actual comparison. |
| RPE follow-up | Once at the end if the shared workout is missing RPE. |
| Thumbs-down | Deletes only the reactor’s copy. Other copies stay. |
| Parse | Once. Never one LLM parse per recipient. |

## Current behaviour (bug)

`resolve_coach_subject` in `coach_bot/config.py` sets subject to the first mentioned athlete who is not the sender. `CoachMessageHandler._reply_kagi` then calls `record_gym_session_from_zulip` once for `subject`. Tagging someone therefore credits **them instead of** the sender. DMs already force `subject = sender`.

Q&A still uses that subject switch. This spec does not change that.

## Recipients

New helper, e.g. `resolve_gym_log_recipients` in `coach_bot/config.py`, used only by the gym-log path. Reuse `extract_zulip_user_mentions` and `resolve_athlete_from_mention`; skip the bot user id.

On a **stream** gym log, recipients are:

1. The sender, if they resolve to a mapped athlete.
2. Every `@`-mentioned mapped athlete, in message order, excluding the bot and anyone already in the list.

On a **private DM**, recipients are `[sender]` when the sender is mapped; mentions are ignored.

If there are no recipients, do not log (same as today’s `gym_session_log and subject` gate).

Examples:

| Message | Recipients |
|---------|------------|
| Jack (mapped) gym log, no mentions | Jack |
| Jack gym log, mentions Sarah and Tom | Jack, Sarah, Tom |
| Jack gym log, mentions Jack and Sarah | Jack, Sarah |
| Unmapped coach gym log, mentions Sarah | Sarah |
| Jack DM gym log, mentions Sarah | Jack |

## Write path

Keep intent routing as today: `interpret_coach_message_with_kagi` returns `gym_session_log`. After intent is known, replace the single `record_gym_session_from_zulip(..., subject.id, ...)` call with a multi-recipient write.

1. **Parse once.** Use `parse_gym_session_metrics` (the parse inside `record_gym_session_from_zulip`) with `body_weight_kg` from the sender if the sender is a recipient, otherwise from the first recipient. Parse failure raises as today: no files written, reply explains the error.
2. **Reuse if already parsed for this Zulip message.** If any recipient already has a gym log with this `zulip_message_id` (`find_gym_log_by_zulip_message`), copy that log’s `gym` dict instead of calling the LLM again.
3. **Write one file per recipient** via `save_gym_log_record` under `athlete_{id}/gym_logs/{log_id}.json` (`gym_log_path`). Each record has a new `id`, that athlete’s `athlete_id` / `athlete_label`, and that athlete’s `body_weight_kg` on the record metadata. The `gym` object is a deep copy of the shared parse. Shared fields stay as today: `source`, `zulip_message_id`, `zulip_sender_email`, `raw_text`, `session_date`, `recorded_at`, `parser_version`.
4. **Idempotent per athlete.** If `find_gym_log_by_zulip_message(cache_dir, athlete_id, zulip_message_id)` already returns a record, do not rewrite that athlete’s file; include the existing record in the confirmation.

Do not introduce a shared multi-athlete log format. Adherence and weekly tonnage already load per-athlete `gym_logs/` (`load_gym_logs_for_athlete`) and will count each copy.

Refactor `record_gym_session_from_zulip` so parse and persist are separable (parse once, persist N times). Callers other than the bot may keep the combined function.

## Confirmation reply

One Zulip reply. For each credited athlete, in recipient order:

- Athlete label as a heading
- `format_gym_log_confirmation` for their record
- `prescribed_gym_section_for_log` + `format_gym_session_comparison` for **that** athlete’s id, plan, and `body_weight_kg`

If `gym_log_missing_rpe` is true for the shared payload, append `format_rpe_follow_up()` once after all athlete blocks.

`_react_thumbs_up` the original athlete message once, as today.

If persist fails for one athlete after a successful parse, keep the successful files, still thumbs-up, and name the failure in the reply. Do not abort already-written copies.

## Coach-reply linkage and thumbs-down

Today `_pending_gym_log` is a single `(athlete_id, log_id)`. `register_gym_log_coach_reply` stamps `coach_reply_zulip_message_id` only on that record via `set_gym_log_coach_reply_message_id`. `find_gym_log_for_reaction_message` / `find_gym_log_by_coach_reply_message` return the first match across athletes.

Changes:

- `_pending_gym_log` becomes a list of `(athlete_id, log_id)` for every successfully written (or reused) copy. `register_gym_log_coach_reply` stamps `coach_reply_zulip_message_id` on **all** of them.
- Thumbs-down (`_handle_gym_thumbs_down`) resolves the reactor with `_resolve_reactor_from_reaction`, then finds **that athlete’s** copy among logs with this confirmation id or original `zulip_message_id`.
- Delete that file only (`delete_gym_log_record`). Other recipients keep their copies.
- If the reactor is unmapped, keep today’s `format_unmatched_sender_help`.
- If the reactor is mapped but has no copy from this message, refuse with the existing “Only the athlete who logged that session can remove it with 👎.” Other copies stay.

Do not parse a single log id from the confirmation body as the primary lookup: the confirmation will contain several ids. Stamp + athlete id is the source of truth. `_find_gym_log_from_coach_message` must not delete the first id it sees when several are present; prefer stamped `coach_reply_zulip_message_id` + reactor id.

## Out of scope

- Copying erg screenshots or typed erg scores to tagged athletes
- Changing Q&A / profile-update subject resolution (`resolve_coach_subject`)
- Multi-recipient logging in DMs
- A shared log record with a list of athlete ids
- Re-parsing bodyweight tonnage per athlete (payload stays identical, including tonnage)

## Testing

No live Zulip or LLM. Stub parse / records as `coach_bot/tests/test_gym_reaction.py` does.

- Stream gym + two mapped mentions → three files, identical `gym` payload, distinct ids; reply names all three; each comparison uses that athlete’s prescribed session
- Stream gym, no mentions → sender only
- Unmapped sender + one mapped mention → mention only
- Mapped sender DM with mentions → sender only
- Same `zulip_message_id` delivered twice → no extra files
- 👎 by one tagged athlete → only that file removed
- 👎 by a mapped athlete with no copy → refused; others untouched
- Unresolved mention plus one mapped mention → mapped people logged; unknown skipped
