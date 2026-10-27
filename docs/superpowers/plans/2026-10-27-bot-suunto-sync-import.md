# Bot Suunto Sync Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** An athlete session request can import and run the Suunto-first sync inside the coach bot image, and a sync exception is printed to the container log.

**Architecture:** `sync_one_athlete` imports `strava_erg_hr_plot`, which imports `stravalib` at module load. The bot image only installs `coach_bot/requirements.txt` (`zulip`, `pyyaml`, `requests`, `pandas`), so the import raises `ModuleNotFoundError` before `suuntool` runs. `fulfill_session_request` catches that exception and posts `Sync failed; this session uses your cached data.` with no log line. Install the same `stravalib` and `fitparse` floors the erg pipeline already uses, and print the exception.

**Tech Stack:** Python 3, pip, Docker `python:3.12-slim`, pytest.

## Global Constraints

- Sync-failed copy stays exactly `Sync failed; this session uses your cached data.`
- A failed sync still generates the session from cache when history exists.
- Do not commit `erg_strava/config.yaml`.
- Dependency floors match `erg_strava/requirements.txt`: `stravalib>=1.5` and `fitparse>=1.2`.
- Do not add matplotlib, scipy, seaborn, or Tesseract to the bot image.

---

### Task 1: Log the sync exception

**Files:**
- Modify: `erg_strava/maintenance_mode.py` (`fulfill_session_request` around line 726, `sync_one_athlete` around line 807)
- Test: `erg_strava/tests/test_maintenance_mode.py`

**Interfaces:**
- Consumes: `fulfill_session_request`, `SYNC_FAILED_LINE`
- Produces: stdout line `Athlete sync failed: ModuleNotFoundError: No module named 'stravalib'` when `sync` raises that error

- [x] **Step 1: Write the failing test**

```python
def test_sync_exception_is_logged_and_uses_cache(tmp_path, capsys):
    _write_erg_score(tmp_path, 7)

    def sync() -> bool:
        raise ModuleNotFoundError("No module named 'stravalib'")

    def complete(_prompt: str) -> str:
        return json.dumps(_interval_payload())

    reply = fulfill_session_request(
        classify_session_request("interval erg, I have an hour"),
        cache_dir=tmp_path,
        athlete_id=7,
        athlete_label="Ada",
        on=ON,
        sync=sync,
        complete=complete,
        profile=AthleteProfile(id=7, label="Ada", max_hr_bpm=185),
    )
    assert reply.startswith("Sync failed; this session uses your cached data.")
    assert "Saved as today's prescription." in reply
    assert "No module named 'stravalib'" in capsys.readouterr().out
```

- [x] **Step 2: Run it and confirm it fails**

Run: `pytest erg_strava/tests/test_maintenance_mode.py::test_sync_exception_is_logged_and_uses_cache -v`

Expected: FAIL because stdout does not contain `stravalib`.

- [x] **Step 3: Print the exception**

In `fulfill_session_request`:

```python
    except Exception as exc:
        print(f"Athlete sync failed: {type(exc).__name__}: {exc}", flush=True)
        sync_failed = True
```

In `sync_one_athlete`, replace `except Exception: ok = False` with:

```python
    except Exception as exc:
        print(
            f"[{acfg.label}] Athlete sync failed: {type(exc).__name__}: {exc}",
            flush=True,
        )
        ok = False
```

- [x] **Step 4: Run the maintenance tests**

Run: `pytest erg_strava/tests/test_maintenance_mode.py -v`

Expected: PASS

### Task 2: Install the sync imports in the bot image

**Files:**
- Modify: `coach_bot/requirements.txt`
- Modify: `coach_bot/Dockerfile`
- Test: `erg_strava/tests/test_maintenance_mode.py`

**Interfaces:**
- Consumes: `erg_strava/requirements.txt` floors
- Produces: bot image install list containing `stravalib>=1.5` and `fitparse>=1.2`, and `PYTHONUNBUFFERED=1`

- [x] **Step 1: Write the failing test**

```python
def test_bot_image_installs_sync_dependencies():
    root = Path(__file__).resolve().parents[2]
    requirements = (root / "coach_bot" / "requirements.txt").read_text(encoding="utf-8")
    dockerfile = (root / "coach_bot" / "Dockerfile").read_text(encoding="utf-8")
    assert "stravalib>=1.5" in requirements
    assert "fitparse>=1.2" in requirements
    assert "PYTHONUNBUFFERED=1" in dockerfile
```

Add `from pathlib import Path` to the test module imports.

- [x] **Step 2: Run it and confirm it fails**

Run: `pytest erg_strava/tests/test_maintenance_mode.py::test_bot_image_installs_sync_dependencies -v`

Expected: FAIL on the missing `stravalib` line.

- [x] **Step 3: Add the packages and unbuffered logs**

Append to `coach_bot/requirements.txt`:

```text
stravalib>=1.5
fitparse>=1.2
```

In `coach_bot/Dockerfile`, after the existing `ENV PYTHONPATH=...` line:

```dockerfile
ENV PYTHONUNBUFFERED=1
```

- [x] **Step 4: Run the maintenance tests**

Run: `pytest erg_strava/tests/test_maintenance_mode.py coach_bot/tests -q`

Expected: PASS
