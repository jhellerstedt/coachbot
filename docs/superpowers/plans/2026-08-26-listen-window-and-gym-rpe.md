# Stream Listen Window and Gym Session RPE — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unmentioned stream follow-ups are handled for 10 minutes after a conversational coach reply, and a session-level RPE on the original gym post is applied to the last weighted set so the bot does not ask again.

**Architecture:** Persist `listen_until` per stream|topic under the cache dir. Unmentioned in-window messages skip triage for structured RPE and otherwise LLM-triage before the existing stream handler. After gym parse, overlay transcript RPE onto the last weighted set; `gym_log_missing_rpe` checks only that set.

**Tech Stack:** Python 3, pytest, existing OpenRouter `_call_llm` / `call_openrouter`, Zulip event loop in `coach_bot/main.py`.

## Global Constraints

- Event-driven listen state only; no Zulip polling or topic-history recency inference.
- Window opens only after a successful stream `_reply` from the bot (not cron weekly-plan / HR plots).
- Each conversational stream reply refreshes 10 minutes (`duration_seconds=600`).
- Triage failure or squad chatter: stay silent; `@coach` always works.
- Session-level gym RPE is last weighted set only; bare number lines in a transcript are not session RPE.
- DMs unchanged.

## Files

- Create: `coach_bot/listen_window.py` — load/save/activate/`listen_window_open`
- Create: `coach_bot/followup_triage.py` — ack-only skip + LLM `should_reply_to_followup`
- Create: `coach_bot/tests/test_listen_window.py`
- Create: `coach_bot/tests/test_followup_triage.py`
- Modify: `erg_strava/gym_program.py` — `extract_session_rpe_from_transcript`, `gym_log_missing_rpe` last-set-only
- Modify: `erg_strava/generate_training_plan.py` — overlay session RPE after parse
- Modify: `erg_strava/weekly_plan_schema.py` — harness rule 11
- Modify: `coach_bot/handler.py` — unmentioned in-window stream path
- Modify: `coach_bot/main.py` — activate window after successful stream reply
- Test: `erg_strava/tests/test_gym_program.py`
- Test: `coach_bot/tests/test_gym_mentions.py`

---

### Task 1: Session-level gym RPE

**Files:** `erg_strava/gym_program.py`, `erg_strava/generate_training_plan.py`, `erg_strava/weekly_plan_schema.py`, `erg_strava/tests/test_gym_program.py`

**Produces:**
- `extract_session_rpe_from_transcript(text: str) -> Optional[float]`
- `apply_session_rpe_from_transcript(metrics, transcript: str) -> metrics`
- `gym_log_missing_rpe` true only when the last weighted set exists and has no RPE

- [ ] **Step 1: Failing tests** in `erg_strava/tests/test_gym_program.py`

```python
def test_extract_session_rpe_from_transcript():
    assert extract_session_rpe_from_transcript("Back squat\n8r 40, 5r 80\nRPE 4") == 4.0
    assert extract_session_rpe_from_transcript("easy") == 5.0
    assert extract_session_rpe_from_transcript("Back squat\n8r 40\n6") is None
    assert extract_session_rpe_from_transcript("8r 40, 5r 80") is None

def test_gym_log_missing_rpe_only_checks_last_weighted_set():
    record = {
        "gym": {
            "exercises": [
                {"name": "Back squat", "sets": [
                    {"reps": 8, "weight_kg": 40.0},
                    {"reps": 5, "weight_kg": 80.0, "rpe": 4.0},
                ]},
                {"name": "Plank", "sets": [
                    {"reps": 1, "weight_kg": 0.0, "duration_sec": 60},
                ]},
            ]
        }
    }
    assert gym_log_missing_rpe(record) is False
```

- [ ] **Step 2: Run tests, confirm they fail** (import / assertion)

Run: `PYTHONPATH=".:erg_strava:lighties" python -m pytest erg_strava/tests/test_gym_program.py::test_extract_session_rpe_from_transcript erg_strava/tests/test_gym_program.py::test_gym_log_missing_rpe_only_checks_last_weighted_set -q`

- [ ] **Step 3: Implement** `extract_session_rpe_from_transcript`; change `gym_log_missing_rpe`; overlay in `parse_gym_session_metrics`; update harness rule 11 to say session-level effort applies to the last working set.

- [ ] **Step 4: Tests pass** including existing `test_gym_log_missing_rpe_and_follow_up`

- [ ] **Step 5: Commit**

---

### Task 2: Listen window state

**Files:** `coach_bot/listen_window.py`, `coach_bot/tests/test_listen_window.py`

**Produces:**
- `LISTEN_WINDOW_SECONDS = 600`
- `listen_state_path(cache_dir: Path) -> Path`
- `load_listen_state(path: Path) -> dict`
- `save_listen_state(path: Path, state: dict) -> None`
- `activate_listen_window(state, stream, topic, *, now, duration_seconds=600) -> None`
- `listen_window_open(state, stream, topic, *, now) -> bool`

- [ ] **Step 1: Failing tests** — activate, expire after 10 min, refresh, missing/corrupt file empty

- [ ] **Step 2: Run, confirm fail**

- [ ] **Step 3: Implement module** (recipe_bot `state.py` pattern: threads keyed `stream|topic`, ISO UTC `listen_until`)

- [ ] **Step 4: Tests pass**

- [ ] **Step 5: Commit**

---

### Task 3: Follow-up triage

**Files:** `coach_bot/followup_triage.py`, `coach_bot/tests/test_followup_triage.py`

**Produces:**
- `should_reply_to_followup(user_text, *, llm_call, use_llm=True) -> bool`
- Ack-only (`thanks`, `ok`, `yep`, …) → False without LLM
- Malformed JSON / exception → False (skip)
- `{"should_reply": true}` → True

- [ ] **Step 1: Failing tests** with a stub `llm_call`

- [ ] **Step 2: Run, confirm fail**

- [ ] **Step 3: Implement** (recipe_bot `conversation.py` prompt, coaching-specific)

- [ ] **Step 4: Tests pass**

- [ ] **Step 5: Commit**

---

### Task 4: Handler + main wiring

**Files:** `coach_bot/handler.py`, `coach_bot/main.py`, `coach_bot/tests/test_gym_mentions.py`

**Produces:**
- Stream body handled if `bot_mentioned` OR listen window open
- Unmentioned + structured RPE → existing RPE handler, no Q&A LLM
- Unmentioned + not RPE → triage; no → None; yes → same pipeline as mention
- `CoachMessageHandler.activate_listen_window(message)` after successful stream `_reply` in `main.py`
- Handler returning None does not refresh (main never calls activate)

- [ ] **Step 1: Failing tests** — unmentioned `RPE 4` in window records RPE; chatter does not reply; `@coach` works with no window

- [ ] **Step 2: Run, confirm fail**

- [ ] **Step 3: Implement handler path + `main.py` activate after stream `_reply`

- [ ] **Step 4: Full suite** `PYTHONPATH=".:erg_strava:lighties" python -m pytest erg_strava/tests/ coach_bot/tests/ -q`

- [ ] **Step 5: Commit**
