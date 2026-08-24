# Shared Gym Session Mentions — Implementation Plan

> **For agentic workers:** Use TDD. Spec: `docs/superpowers/specs/2026-08-24-shared-gym-session-mentions-design.md`.

**Goal:** Stream `@coach` gym logs credit the sender and every tagged mapped athlete with identical workout copies.

**Architecture:** Resolve recipients in `coach_bot/config.py`. Parse the workout once in `generate_training_plan.py`, persist one `gym_logs` file per recipient. Handler confirmation lists each athlete; thumbs-down deletes only the reactor’s copy.

**Tech Stack:** Python 3, pytest, existing Zulip mention parsing and gym log cache.

## Global Constraints

- Gym logs only; erg / Q&A / profile-update subject resolution unchanged.
- Stream only; DMs stay sender-only.
- Identical `gym` payload (including tonnage) for every copy.
- Parse once (or reuse an existing copy’s `gym` dict for the same `zulip_message_id`).
- Thumbs-down removes only the reactor’s file.

## Files

- Modify: `coach_bot/config.py` — `resolve_gym_log_recipients`
- Modify: `erg_strava/generate_training_plan.py` — parse/persist split; multi-athlete write; reaction lookup by athlete
- Modify: `coach_bot/handler.py` — gym log path, pending list, thumbs-down
- Test: `coach_bot/tests/test_gym_mentions.py` (new)
- Test: `coach_bot/tests/test_gym_reaction.py` — reactor-only delete
- Test: `erg_strava/tests/test_gym_transcript.py` — persist copies / idempotency

---

### Task 1: Recipient resolution

**Files:** `coach_bot/config.py`, `coach_bot/tests/test_gym_mentions.py`

```python
def resolve_gym_log_recipients(
    athletes: List[CoachAthleteCfg],
    *,
    sender_email: str,
    sender_full_name: str = "",
    sender_id: Optional[int] = None,
    message_content: str = "",
    bot_user_id: Optional[int] = None,
    private_dm: bool = False,
) -> List[CoachAthleteCfg]:
```

- Stream: sender (if mapped) then mapped `@` mentions in order, skip bot and duplicates.
- DM: `[sender]` if mapped; ignore mentions.
- Unmapped sender + mapped mention → `[mention]`.
- Unresolved mentions skipped.

- [ ] Failing tests for the table in the spec
- [ ] Implement helper (reuse `extract_zulip_user_mentions` + `resolve_athlete_from_mention`)
- [ ] Tests pass
- [ ] Commit

### Task 2: Parse once, persist N copies

**Files:** `erg_strava/generate_training_plan.py`, `erg_strava/tests/test_gym_transcript.py`

Keep `record_gym_session_from_zulip` for one athlete. Add:

```python
def record_gym_sessions_from_zulip_for_athletes(
    cache_dir: Path,
    recipients: Sequence[Tuple[int, str, Optional[float]]],  # id, label, body_weight_kg
    workout_text: str,
    token: str,
    *,
    zulip_message_id: Optional[int] = None,
    zulip_sender_email: str = "",
    recorded_at: Optional[datetime] = None,
    session_hint_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
```

- Parse with `parse_gym_session_metrics` using the first recipient’s `body_weight_kg` (handler puts sender first).
- If any recipient already has this `zulip_message_id`, reuse that `gym` dict (no LLM).
- Persist via `save_gym_log_record`: new id, athlete fields, deepcopy `gym`.
- Skip rewrite when that athlete already has this message id.
- Parse failure: raise, write nothing.

- [ ] Failing tests: two athletes, identical gym, distinct ids; replay no extras; reuse gym without second parse
- [ ] Implement
- [ ] Tests pass
- [ ] Commit

### Task 3: Handler confirmation

**Files:** `coach_bot/handler.py`, `coach_bot/tests/test_gym_mentions.py`

On `gym_session_log`, call `resolve_gym_log_recipients` (not `subject` alone). If empty, do not log.

For each record: heading with `athlete_label`, `format_gym_log_confirmation`, that athlete’s `prescribed_gym_section_for_log` + `format_gym_session_comparison`. One `format_rpe_follow_up` at the end if missing RPE. One thumbs-up.

`_pending_gym_log` becomes `list[tuple[int, str]]`. `register_gym_log_coach_reply` stamps all copies.

Partial persist failure: keep successes, name the failure.

- [ ] Failing handler tests (stub interpret + parse): stream two mentions → three files + names in reply; DM mentions → sender only; no mentions → sender only; unmapped sender → mention only
- [ ] Implement
- [ ] Tests pass
- [ ] Commit

### Task 4: Thumbs-down reactor copy only

**Files:** `generate_training_plan.py`, `handler.py`, `test_gym_reaction.py`, `test_gym_mentions.py`

- `find_gym_log_for_reaction_message(..., athlete_id=reactor.id)` returns that athlete’s copy among confirmation / original message ids.
- `_find_gym_log_from_coach_message` finds the reactor’s id among several `**Logged gym session** (`id`)` lines (`findall`, not first match).
- Mapped reactor with no copy → existing “Only the athlete who logged that session can remove it with 👎.”

- [ ] Failing tests: two copies, 👎 by tagged athlete deletes only theirs; 👎 by mapped non-recipient refuses; single-athlete 👎 still works
- [ ] Implement
- [ ] Tests pass
- [ ] Commit

### Task 5: Full suite

```
PYTHONPATH=".:erg_strava:lighties" pytest coach_bot/tests/test_gym_mentions.py coach_bot/tests/test_gym_reaction.py erg_strava/tests/test_gym_transcript.py -q
PYTHONPATH=".:erg_strava:lighties" pytest coach_bot/tests/ erg_strava/tests/test_gym_transcript.py -q
```
