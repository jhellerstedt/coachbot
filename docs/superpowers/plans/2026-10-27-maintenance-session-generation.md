# Maintenance Session Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** A maintenance-mode request such as `@coach interval session 45 minutes` generates one DayPlan, saves it as today's prescription, and posts that session instead of `I couldn't write that session. Try the request again.`

**Architecture:** On 2026-10-27 the live model did return a 45-minute interval workout for Jack H. `generate_session_day` rejected both attempts because the JSON was not a DayPlan: segments sat at the top level, phases were `warmup`/`main`/`cooldown`, `zone_z` was a number or `z2`, `zone_t` was `endurance`/`UT2`, `priority` was `required`/`high`, and duration was a `duration_min` number. `day_from_model_text` then built an erg day with `rowing=None`, `validate_generated_day` returned `expected an erg day`, and `fulfill_session_request` replied with `WRITE_FAILED` without saving a file or logging the reason. The weekly-plan generator already avoids this with a strict `json_schema` response format. Maintenance generation will use the existing single-day schema the same way, put that shape in the prompt, and on the second attempt include the parser or validator rejection.

**Tech Stack:** Python 3, pytest, existing `weekly_plan_schema._day_schema`, OpenRouter `response_format` via `call_openrouter`.

## Global Constraints

- Failure copy stays exactly `I couldn't write that session. Try the request again.`
- Saved copy stays exactly `Saved as today's prescription.`
- Sync notice stays exactly `Syncing your latest sessions…`
- `interval session 45 minutes` stays a complete interval request of 45 minutes. Do not start requiring the word `erg` on that path.
- Sunday maintenance exit, athlete sync, and plan-file layout stay as they are.
- Do not commit `erg_strava/config.yaml`.
- DayPlan enums stay: phases `warm_up`, `main_set`, `cool_down`, `work`, `rest`, `build`, `active_recovery`; `zone_z` `Z1`–`Z5`; `zone_t` `T1`–`T7`; `priority` `split` or `hr`; splits `H:MM`.
- Duration strings the estimator counts are `N min` and `NxNmin`. The accepted band stays 0.5×–1.5× the requested minutes.
- A first model exception or invalid day must be retried once. Both failures still return `WRITE_FAILED` and leave the plan file unchanged.

---

### Task 1: Strict DayPlan response format

**Files:**
- Modify: `erg_strava/weekly_plan_schema.py` (immediately after `openrouter_response_format`, around line 424)
- Test: `erg_strava/tests/test_weekly_plan_schema.py`

**Interfaces:**
- Consumes: `_day_schema()` already defined in `erg_strava/weekly_plan_schema.py`
- Produces: `openrouter_day_response_format(*, name: str = "maintenance_day") -> dict` for Task 2

- [x] **Step 1: Write the failing test**

Add this import to the existing `weekly_plan_schema` import block in `erg_strava/tests/test_weekly_plan_schema.py`:

```python
    openrouter_day_response_format,
```

Add this test at the end of that file:

```python
def test_openrouter_day_response_format_is_one_strict_day():
    fmt = openrouter_day_response_format()
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["name"] == "maintenance_day"
    schema = fmt["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) >= {
        "weekday",
        "date",
        "session_type",
        "session_subtype",
        "gym",
        "rowing",
        "notes",
    }
    rowing = schema["properties"]["rowing"]["anyOf"][0]
    phase_enum = rowing["properties"]["segments"]["items"]["properties"]["phase"]["enum"]
    assert "warm_up" in phase_enum
    assert "warmup" not in phase_enum
    priority_enum = rowing["properties"]["segments"]["items"]["properties"]["priority"]["enum"]
    assert priority_enum == ["split", "hr"]
```

- [x] **Step 2: Run the test to verify it fails**

Run: `pytest erg_strava/tests/test_weekly_plan_schema.py::test_openrouter_day_response_format_is_one_strict_day -v`

Expected: FAIL with `ImportError` or `cannot import name 'openrouter_day_response_format'`

- [x] **Step 3: Add the helper**

In `erg_strava/weekly_plan_schema.py`, immediately after `openrouter_response_format`:

```python
def openrouter_day_response_format(*, name: str = "maintenance_day") -> Dict[str, Any]:
    """Strict schema for one maintenance-mode DayPlan."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": _day_schema(),
        },
    }
```

- [x] **Step 4: Run the test to verify it passes**

Run: `pytest erg_strava/tests/test_weekly_plan_schema.py::test_openrouter_day_response_format_is_one_strict_day -v`

Expected: PASS

- [x] **Step 5: Commit**

```bash
git add erg_strava/weekly_plan_schema.py erg_strava/tests/test_weekly_plan_schema.py
git commit -m "$(cat <<'EOF'
Add a strict OpenRouter schema for one DayPlan.

Maintenance session generation needs the same JSON shape the weekly plan parser accepts.
EOF
)"
```

---

### Task 2: Prompt shape and a retry that reports the rejection

**Files:**
- Modify: `erg_strava/maintenance_mode.py` (`_session_prompt` around line 423, `generate_session_day` around line 459)
- Test: `erg_strava/tests/test_maintenance_mode.py`

**Interfaces:**
- Consumes: `validate_generated_day`, `day_from_model_text`, `SessionRequest`, `_interval_payload()` in the test file
- Produces: second-attempt prompt text containing either `expected an erg day` or `The previous reply was rejected because it is not a DayPlan.` A first `Exception` from `complete` is retried once instead of returning `None` immediately.

- [x] **Step 1: Write the failing tests**

In `erg_strava/tests/test_maintenance_mode.py`, add `classify_session_request` is already imported. Also import `_session_prompt` is private; import it in the test:

```python
from maintenance_mode import (
    PAUSED_WEEKLY_PLANS,
    _session_prompt,
    classify_session_request,
    fulfill_session_request,
    maintenance_mode_enabled,
    save_maintenance_day,
)
```

Add these tests after `test_invalid_model_output_does_not_save`:

```python
def test_interval_prompt_shows_the_dayplan_shape():
    request = classify_session_request("interval erg, I have an hour")
    text = _session_prompt(
        request,
        athlete_label="Ada",
        on=ON,
        hr_context="",
        training_context="",
        gym_context="",
    )
    assert '"rowing"' in text
    assert '"phase": "warm_up"' in text
    assert '"phase": "work"' in text
    assert '"duration": "6x3min"' in text
    assert '"priority": "split"' in text
    assert "segments at the top level" in text


def test_rejected_shape_is_retried_with_the_validator_reason(tmp_path):
    _write_erg_score(tmp_path, 7)
    prompts = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return json.dumps(
                {
                    "session_type": "erg",
                    "segments": [
                        {
                            "phase": "warmup",
                            "duration_min": 10,
                            "zone_z": 2,
                            "zone_t": "endurance",
                            "priority": "required",
                        }
                    ],
                }
            )
        return json.dumps(_interval_payload())

    reply = fulfill_session_request(
        classify_session_request("interval erg, I have an hour"),
        cache_dir=tmp_path,
        athlete_id=7,
        athlete_label="Ada",
        on=ON,
        sync=lambda: True,
        complete=complete,
        profile=AthleteProfile(id=7, label="Ada", max_hr_bpm=185),
    )
    assert len(prompts) == 2
    assert "expected an erg day" in prompts[1]
    assert "Saved as today's prescription." in reply
    assert (tmp_path / "athlete_7" / "weekly_plans").is_dir()


def test_model_exception_is_retried_once(tmp_path):
    _write_erg_score(tmp_path, 7)
    calls = {"n": 0}

    def complete(_prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("timeout")
        return json.dumps(_interval_payload())

    reply = fulfill_session_request(
        classify_session_request("interval erg, I have an hour"),
        cache_dir=tmp_path,
        athlete_id=7,
        athlete_label="Ada",
        on=ON,
        sync=lambda: True,
        complete=complete,
        profile=AthleteProfile(id=7, label="Ada", max_hr_bpm=185),
    )
    assert calls["n"] == 2
    assert "Saved as today's prescription." in reply
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest erg_strava/tests/test_maintenance_mode.py::test_interval_prompt_shows_the_dayplan_shape erg_strava/tests/test_maintenance_mode.py::test_rejected_shape_is_retried_with_the_validator_reason erg_strava/tests/test_maintenance_mode.py::test_model_exception_is_retried_once -v`

Expected: FAIL. The prompt test fails on the missing skeleton. The rejected-shape test fails because the reply is `I couldn't write that session. Try the request again.` The exception test fails because `calls["n"]` is 1.

- [x] **Step 3: Put the DayPlan shape in the prompt and retry with the reason**

In `erg_strava/maintenance_mode.py`, add this constant next to the other reply constants (after `WRITE_FAILED`):

```python
_DAYPLAN_SHAPE = """\
Reply with one DayPlan JSON object in this shape. Do not put segments at the top level.
phase is warm_up, main_set, cool_down, work, rest, build, or active_recovery.
zone_z is Z1, Z2, Z3, Z4, or Z5. zone_t is T1, T2, T3, T4, T5, T6, or T7.
priority is split or hr. duration is a string such as "10 min" or "6x3min", not a number.
For an interval request, session_subtype is "intervals" and the work duration matches NxNmin, such as 6x3min, or there are at least two phase=work segments.
{
  "weekday": "Tuesday",
  "date": "2026-10-27",
  "session_type": "erg",
  "session_subtype": "intervals",
  "gym": null,
  "rowing": {
    "segments": [
      {
        "phase": "warm_up",
        "label": "Warm-up",
        "duration": "10 min",
        "split_min": "2:15",
        "split_max": "2:25",
        "zone_z": "Z2",
        "zone_t": "T2",
        "hr_bpm_min": 120,
        "hr_bpm_max": 140,
        "priority": "hr",
        "notes": null
      },
      {
        "phase": "work",
        "label": "6x3min",
        "duration": "6x3min",
        "split_min": "1:58",
        "split_max": "2:04",
        "zone_z": "Z4",
        "zone_t": "T5",
        "hr_bpm_min": 165,
        "hr_bpm_max": 180,
        "priority": "split",
        "notes": null
      }
    ],
    "erg_alternative": null
  },
  "notes": null
}"""

_PARSE_RETRY = (
    "\n\nThe previous reply was rejected because it is not a DayPlan. "
    "Put the pieces in rowing.segments. "
    "phase must be one of warm_up, main_set, cool_down, work, rest, build, active_recovery. "
    "zone_z must be Z1, Z2, Z3, Z4, or Z5. zone_t must be T1 through T7. "
    "priority must be split or hr. duration must be a string such as 10 min or 6x3min."
)
```

In `_session_prompt`, append the shape after the existing field-list paragraph and before the hr/training/gym context blocks:

```python
    lines.append(_DAYPLAN_SHAPE)
```

Replace the attempt loop inside `generate_session_day` with:

```python
    feedback = ""
    last_reason = "not a DayPlan"
    for _attempt in range(2):
        try:
            raw = complete(prompt + feedback)
        except Exception as exc:
            last_reason = f"model call raised {type(exc).__name__}: {exc}"
            feedback = (
                "\n\nThe previous reply could not be parsed. "
                "Reply with one DayPlan JSON object only."
            )
            continue
        if str(raw).startswith("OpenRouter API"):
            last_reason = str(raw)[:300]
            feedback = (
                "\n\nThe previous reply could not be parsed. "
                "Reply with one DayPlan JSON object only."
            )
            continue
        day = day_from_model_text(raw, on)
        if day is None:
            last_reason = "not a DayPlan"
            feedback = _PARSE_RETRY
            continue
        if profile is not None and day.rowing is not None:
            day = replace(day, rowing=_overlay_rowing_hr(day.rowing, profile))
        reason = validate_generated_day(day, request)
        if reason is None:
            return day
        last_reason = reason
        feedback = (
            f"\n\nThe previous day was rejected: {reason}. "
            "Reply with one corrected DayPlan JSON object."
        )
    print(f"maintenance session rejected: {last_reason}", flush=True)
    return None
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `pytest erg_strava/tests/test_maintenance_mode.py -v`

Expected: PASS, including the three new tests and the existing `test_invalid_model_output_does_not_save` (two non-JSON replies still return `WRITE_FAILED` and do not save).

- [x] **Step 5: Commit**

```bash
git add erg_strava/maintenance_mode.py erg_strava/tests/test_maintenance_mode.py
git commit -m "$(cat <<'EOF'
Retry a maintenance session with the DayPlan rejection reason.

The model was returning a usable interval workout in the wrong JSON shape, and the first failure was discarded with no reason for the second attempt.
EOF
)"
```

---

### Task 3: Send the day schema from the bot

**Files:**
- Modify: `erg_strava/maintenance_mode.py` (add `session_day_completion` after `generate_session_day`)
- Modify: `coach_bot/handler.py` (`_maybe_maintenance_reply` complete closure, around line 1087)
- Test: `erg_strava/tests/test_maintenance_mode.py`
- Test: `coach_bot/tests/test_maintenance_bot.py`

**Interfaces:**
- Consumes: `openrouter_day_response_format()` from Task 1, `call_openrouter(..., response_format=)`
- Produces: `session_day_completion(prompt: str, api_key: str) -> str`. The handler's `complete` callable passed to `fulfill_session_request` calls it.

- [x] **Step 1: Write the failing tests**

Append to `erg_strava/tests/test_maintenance_mode.py`:

```python
def test_session_day_completion_sends_the_day_schema(monkeypatch):
    captured = {}

    def fake_call_openrouter(**kwargs):
        captured.update(kwargs)
        return "{}"

    monkeypatch.setattr(
        "openrouter_client.call_openrouter",
        fake_call_openrouter,
    )
    from maintenance_mode import session_day_completion

    assert session_day_completion("prescribe Tuesday", "test-key") == "{}"
    assert captured["user"] == "prescribe Tuesday"
    assert captured["api_key"] == "test-key"
    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["name"] == "maintenance_day"
    assert captured["response_format"]["json_schema"]["strict"] is True
```

In `coach_bot/tests/test_maintenance_bot.py`, add this test next to `test_complete_request_posts_syncing_notice`. It uses the same `_handler`, `_config`, `_message`, and `patch` already imported in that file:

```python
def test_complete_request_completes_with_the_day_schema(tmp_path: Path):
    client = MagicMock()
    handler = _handler(tmp_path, client)
    with _config(handler), patch(
        "coach_bot.handler.config_maintenance_enabled", return_value=True
    ), patch(
        "coach_bot.handler.fulfill_session_request", return_value="the workout"
    ) as fulfill, patch(
        "maintenance_mode.session_day_completion", return_value="{}"
    ) as complete_day:
        reply = handler.handle(_message("gym session, leg day, 45min"))
    assert reply == "the workout"
    complete = fulfill.call_args.kwargs["complete"]
    assert complete("a prompt") == "{}"
    complete_day.assert_called_once()
    assert complete_day.call_args.args[0] == "a prompt"
```

The handler import must happen inside `complete`, after the patch is in place:

```python
        def complete(prompt: str) -> str:
            from maintenance_mode import session_day_completion

            return session_day_completion(prompt, token)
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `pytest erg_strava/tests/test_maintenance_mode.py::test_session_day_completion_sends_the_day_schema coach_bot/tests/test_maintenance_bot.py::test_complete_request_completes_with_the_day_schema -v`

Expected: FAIL. `session_day_completion` is missing, and the handler test fails because `complete("a prompt")` calls `call_openrouter` instead of the patched helper.

- [x] **Step 3: Add the completion helper and use it in the handler**

In `erg_strava/maintenance_mode.py`, after `generate_session_day`:

```python
def session_day_completion(prompt: str, api_key: str) -> str:
    """Ask OpenRouter for one DayPlan, constrained to the day schema."""
    from openrouter_client import call_openrouter
    from weekly_plan_schema import openrouter_day_response_format

    return call_openrouter(
        system="You are a rowing coach. Reply with one DayPlan JSON object only.",
        user=prompt,
        api_key=api_key,
        response_format=openrouter_day_response_format(),
    )
```

In `coach_bot/handler.py`, replace the `complete` closure inside `_maybe_maintenance_reply` with:

```python
        def complete(prompt: str) -> str:
            from maintenance_mode import session_day_completion

            return session_day_completion(prompt, token)
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `pytest erg_strava/tests/test_maintenance_mode.py coach_bot/tests/test_maintenance_bot.py erg_strava/tests/test_weekly_plan_schema.py::test_openrouter_day_response_format_is_one_strict_day -v`

Expected: PASS

- [x] **Step 5: Commit**

```bash
git add erg_strava/maintenance_mode.py coach_bot/handler.py erg_strava/tests/test_maintenance_mode.py coach_bot/tests/test_maintenance_bot.py
git commit -m "$(cat <<'EOF'
Constrain maintenance session generation to the DayPlan schema.

Free-form JSON was a different shape from the parser, so a valid interval request never saved.
EOF
)"
```
