"""Maintenance mode: flag, session requests, and one saved day."""

from __future__ import annotations

import json
from datetime import date

import pytest

from athlete_profile import AthleteProfile
from generate_training_plan import (
    prescribed_gym_section_for_log,
    save_athlete_weekly_plan,
    week_for_date,
)
from erg_prescription_compare import prescribed_erg_section_for_log
from maintenance_mode import (
    PAUSED_WEEKLY_PLANS,
    classify_session_request,
    fulfill_session_request,
    maintenance_mode_enabled,
    save_maintenance_day,
)
from weekly_plan_schema import DayPlan, _parse_day


ON = date(2026, 10, 10)  # Saturday


def _set(reps: int, weight: float) -> dict:
    return {"reps": reps, "weight_kg": weight, "duration_sec": None}


def _gym_payload() -> dict:
    exercises = []
    for name, weight in (
        ("Back squat", 80),
        ("Romanian deadlift", 70),
        ("Bulgarian split squat", 24),
        ("Kettlebell swings", 16),
    ):
        exercises.append(
            {"name": name, "sets": [_set(6, weight), _set(6, weight), _set(6, weight)]}
        )
    return {
        "weekday": "Saturday",
        "date": ON.isoformat(),
        "session_type": "gym",
        "session_subtype": "strength",
        "gym": {"category": "leg", "goal": "strength", "exercises": exercises},
        "rowing": None,
        "notes": None,
    }


def _segment(phase: str, label: str, minutes: int) -> dict:
    return {
        "phase": phase,
        "label": label,
        "duration": f"{minutes} min",
        "split_min": "2:00",
        "split_max": "2:05",
        "zone_z": "Z2",
        "zone_t": "T4",
        "hr_bpm_min": 130,
        "hr_bpm_max": 145,
        "priority": "hr",
        "notes": None,
    }


def _interval_payload() -> dict:
    segments = [
        _segment("warm_up", "Warm-up", 10),
        _segment("work", "Work", 10),
        _segment("rest", "Rest", 3),
        _segment("work", "Work", 10),
        _segment("rest", "Rest", 3),
        _segment("work", "Work", 10),
        _segment("cool_down", "Cool-down", 10),
    ]
    return {
        "weekday": "Saturday",
        "date": ON.isoformat(),
        "session_type": "erg",
        "session_subtype": "intervals",
        "gym": None,
        "rowing": {"segments": segments, "erg_alternative": None},
        "notes": None,
    }


def _steady_payload() -> dict:
    return {
        "weekday": "Saturday",
        "date": ON.isoformat(),
        "session_type": "erg",
        "session_subtype": "steady",
        "gym": None,
        "rowing": {
            "segments": [_segment("main_set", "Steady", 40)],
            "erg_alternative": None,
        },
        "notes": None,
    }


def _rest_day(weekday: str, day: str) -> dict:
    return {
        "weekday": weekday,
        "date": day,
        "session_type": "rest",
        "session_subtype": None,
        "gym": None,
        "rowing": None,
        "notes": None,
    }


def test_maintenance_flag_values():
    assert maintenance_mode_enabled({}) is False
    assert maintenance_mode_enabled({"maintenance_mode": False}) is False
    assert maintenance_mode_enabled({"maintenance_mode": True}) is True
    assert maintenance_mode_enabled({"maintenance_mode": "true"}) is True
    assert maintenance_mode_enabled({"maintenance_mode": "YES"}) is True
    assert maintenance_mode_enabled({"maintenance_mode": "no"}) is False


def test_example_prompts_are_complete_session_requests():
    gym = classify_session_request("gym session, leg day, 45min")
    assert gym.status == "complete"
    assert gym.modality == "gym"
    assert gym.focus == "legs"
    assert gym.duration_min == 45

    interval = classify_session_request("interval erg, I have an hour")
    assert interval.status == "complete"
    assert interval.modality == "interval"
    assert interval.duration_min == 60

    steady = classify_session_request("steady state erg time and target HR")
    assert steady.status == "complete"
    assert steady.modality == "steady"
    assert steady.duration_min is None


def test_questions_and_logs_are_not_session_requests():
    def decide(_state, _questions):
        raise AssertionError("regex path must not call the model")

    assert (
        classify_session_request(
            "why was my heart rate high yesterday?", decide=decide
        ).status
        == "not_a_request"
    )
    assert (
        classify_session_request(
            "2:05.4 split over 5000m in 20:00 on the erg", decide=decide
        ).status
        == "not_a_request"
    )
    assert (
        classify_session_request("Back squat 5x5 80kg felt solid", decide=decide).status
        == "not_a_request"
    )


def test_incomplete_requests_do_not_sync_or_call_the_model(tmp_path):
    calls = {"sync": 0, "model": 0}

    def sync() -> bool:
        calls["sync"] += 1
        return True

    def complete(_prompt: str) -> str:
        calls["model"] += 1
        return "{}"

    for text in ("gym session", "interval erg"):
        request = classify_session_request(text)
        assert request.status == "incomplete"
        assert "Saved as today's prescription." not in request.clarify
        reply = fulfill_session_request(
            request,
            cache_dir=tmp_path,
            athlete_id=7,
            athlete_label="A",
            on=ON,
            sync=sync,
            complete=complete,
        )
        assert reply == request.clarify
    assert calls == {"sync": 0, "model": 0}


def test_save_creates_one_day_and_prescription_lookup_reads_it(tmp_path):
    gym = _parse_day(_gym_payload())
    assert isinstance(gym, DayPlan)
    save_maintenance_day(tmp_path, 7, gym, ON)
    record_path = (
        tmp_path / "athlete_7" / "weekly_plans" / f"{week_for_date(ON).week_id}.json"
    )
    saved = json.loads(record_path.read_text())
    assert len(saved["plan_json"]["days"]) == 1
    assert saved["plan_json"]["days"][0]["date"] == ON.isoformat()
    assert saved["maintenance_days"] == [ON.isoformat()]
    section = prescribed_gym_section_for_log(tmp_path, 7, ON)
    assert section and "Back squat" in section

    erg = _parse_day(_interval_payload())
    assert isinstance(erg, DayPlan)
    save_maintenance_day(tmp_path, 8, erg, ON)
    erg_section = prescribed_erg_section_for_log(tmp_path, 8, ON)
    assert erg_section and "erg" in erg_section


def test_second_save_replaces_only_today(tmp_path):
    week = week_for_date(ON)
    monday = week.week_start
    days = []
    for offset in range(7):
        day = date.fromordinal(monday.toordinal() + offset)
        name = (
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        )[offset]
        if name == "Monday":
            payload = _gym_payload()
            payload["weekday"] = "Monday"
            payload["date"] = day.isoformat()
            payload["gym"]["exercises"][0]["name"] = "Bench press"
            payload["gym"]["category"] = "upper_core"
            payload["gym"]["exercises"] = [
                {
                    "name": "Bench press",
                    "sets": [_set(5, 50), _set(5, 50), _set(5, 50)],
                },
                {
                    "name": "Barbell row",
                    "sets": [_set(8, 40), _set(8, 40), _set(8, 40)],
                },
                {
                    "name": "Lat pull-down",
                    "sets": [_set(8, 40), _set(8, 40), _set(8, 40)],
                },
                {"name": "Plank", "sets": [{"reps": 1, "weight_kg": None, "duration_sec": 45}]},
            ]
            days.append(payload)
        else:
            days.append(_rest_day(name, day.isoformat()))
    plan = {
        "version": 1,
        "personalised": True,
        "greeting": None,
        "days": days,
        "recommended_erg": None,
    }
    save_athlete_weekly_plan(
        tmp_path,
        7,
        week,
        "Monday:\nbench\n\nSaturday:\nrest\n",
        plan_json=plan,
    )
    first = _parse_day(_gym_payload())
    assert first is not None
    save_maintenance_day(tmp_path, 7, first, ON)
    second_payload = _gym_payload()
    second_payload["notes"] = "second"
    second = _parse_day(second_payload)
    assert second is not None
    save_maintenance_day(tmp_path, 7, second, ON)

    record_path = tmp_path / "athlete_7" / "weekly_plans" / f"{week.week_id}.json"
    saved = json.loads(record_path.read_text())
    by_day = {d["weekday"]: d for d in saved["plan_json"]["days"]}
    assert by_day["Monday"]["gym"]["exercises"][0]["name"] == "Bench press"
    assert by_day["Saturday"]["notes"] == "second"
    assert saved["maintenance_days"] == [ON.isoformat()]
    assert by_day["Tuesday"]["session_type"] == "rest"


def _write_gym_log(cache: Path, athlete_id: int) -> None:
    root = cache / f"athlete_{athlete_id}" / "gym_logs"
    root.mkdir(parents=True)
    (root / "log.json").write_text(
        json.dumps({"id": "log", "session_date": "2026-10-01"}),
        encoding="utf-8",
    )


def _write_erg_score(cache: Path, athlete_id: int) -> None:
    root = cache / f"athlete_{athlete_id}" / "erg_scores"
    root.mkdir(parents=True)
    (root / "score.json").write_text(
        json.dumps({"id": "score", "session_date": "2026-10-01"}),
        encoding="utf-8",
    )


def test_sync_error_still_uses_cache(tmp_path):
    _write_gym_log(tmp_path, 7)

    def sync() -> bool:
        raise RuntimeError("suunto down")

    def complete(_prompt: str) -> str:
        return json.dumps(_gym_payload())

    reply = fulfill_session_request(
        classify_session_request("gym session, leg day, 45min"),
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
    assert (tmp_path / "athlete_7" / "weekly_plans").is_dir()


def test_empty_gym_history_does_not_call_the_model_or_save(tmp_path):
    def complete(_prompt: str) -> str:
        raise AssertionError("model must not be called")

    reply = fulfill_session_request(
        classify_session_request("gym session, leg day, 45min"),
        cache_dir=tmp_path,
        athlete_id=7,
        athlete_label="Ada",
        on=ON,
        sync=lambda: True,
        complete=complete,
    )
    assert reply == (
        "I don't have any recent gym sessions cached for Ada, "
        "so I can't set loads or targets. Log a session and ask again."
    )
    assert not (tmp_path / "athlete_7" / "weekly_plans").exists()


def test_invalid_model_output_does_not_save(tmp_path):
    _write_erg_score(tmp_path, 7)
    calls = {"n": 0}

    def complete(_prompt: str) -> str:
        calls["n"] += 1
        return "not json"

    reply = fulfill_session_request(
        classify_session_request("interval erg, I have an hour"),
        cache_dir=tmp_path,
        athlete_id=7,
        athlete_label="Ada",
        on=ON,
        sync=lambda: True,
        complete=complete,
    )
    assert calls["n"] == 2
    assert reply == "I couldn't write that session. Try the request again."
    assert not (tmp_path / "athlete_7" / "weekly_plans").exists()


def test_plan_adjustment_constant_matches_spec():
    assert PAUSED_WEEKLY_PLANS == (
        "Weekly plans are paused. Ask for a single session, "
        "for example: gym session, leg day, 45min."
    )


def test_steady_request_accepts_model_day_without_named_duration(tmp_path):
    _write_erg_score(tmp_path, 7)

    def complete(_prompt: str) -> str:
        return json.dumps(_steady_payload())

    reply = fulfill_session_request(
        classify_session_request("steady state erg time and target HR"),
        cache_dir=tmp_path,
        athlete_id=7,
        athlete_label="Ada",
        on=ON,
        sync=lambda: False,
        complete=complete,
        profile=AthleteProfile(id=7, label="Ada", max_hr_bpm=185),
    )
    assert "Sync failed; this session uses your cached data." in reply
    assert "Saved as today's prescription." in reply


def test_main_exits_before_sync_when_maintenance(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "cache_dir: ./cache\nmaintenance_mode: true\nathletes:\n  - id: 1\n    label: A\n",
        encoding="utf-8",
    )
    called = {"sync": 0}

    def sync_athlete(*_args, **_kwargs):
        called["sync"] += 1
        return True

    monkeypatch.setattr("strava_erg_hr_plot.sync_athlete", sync_athlete)
    monkeypatch.setattr(
        "sys.argv",
        ["strava_erg_hr_plot.py", str(cfg), "--no-zulip", "--no-kagi"],
    )
    from strava_erg_hr_plot import main

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert called["sync"] == 0
    assert (
        "Maintenance mode is on; skipping weekly sync and plan generation."
        in capsys.readouterr().out
    )


def test_main_reaches_sync_when_maintenance_off(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "cache_dir: ./cache\nmaintenance_mode: false\nathletes:\n  - id: 1\n    label: A\n"
        "strava:\n  optional: true\n",
        encoding="utf-8",
    )
    called = {"sync": 0}

    def sync_athlete(*_args, **_kwargs):
        called["sync"] += 1
        return False

    class PastMaintenance(Exception):
        pass

    def collect_all_points(*_args, **_kwargs):
        raise PastMaintenance()

    monkeypatch.setattr("strava_erg_hr_plot.sync_athlete", sync_athlete)
    monkeypatch.setattr("strava_erg_hr_plot.collect_all_points", collect_all_points)
    monkeypatch.setattr(
        "sys.argv",
        ["strava_erg_hr_plot.py", str(cfg), "--no-zulip", "--no-kagi"],
    )
    from strava_erg_hr_plot import main

    with pytest.raises(PastMaintenance):
        main()
    assert called["sync"] == 1

