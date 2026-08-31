from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from athlete_data_alerts import (
    SUUNTO_SYNC_FAIL_MESSAGE,
    AthleteDataAlert,
    format_alert_dm,
    merge_alerts,
    screenshot_without_suunto_alert,
    send_athlete_data_alerts,
    should_send_alert,
    source_mapped_for_athlete,
)
from generate_training_plan import week_bounds_from_monday
from strava_erg_hr_plot import collect_suunto_alerts
from suunto_client import SuuntoCfg

JACK = 53603359
EMIL = 116259013


def _suunto() -> SuuntoCfg:
    return SuuntoCfg(
        enabled=True,
        primary=True,
        suuntool_path=None,
        session_file=None,
        indoor_rowing_activity_ids=frozenset({57}),
        gym_activity_ids=frozenset({23}),
        athlete_ids=frozenset({JACK}),
    )


def test_suunto_mapped_only_for_listed_athlete():
    cfg = _suunto()
    assert source_mapped_for_athlete(
        "suunto",
        athlete_id=JACK,
        suunto_cfg=cfg,
        token_dir=None,
        zulip_user_id=73,
        zulip_email=None,
    )
    assert not source_mapped_for_athlete(
        "suunto",
        athlete_id=EMIL,
        suunto_cfg=cfg,
        token_dir=None,
        zulip_user_id=77,
        zulip_email=None,
    )


def test_skip_alert_without_zulip_or_unmapped_source():
    cfg = _suunto()
    alert = AthleteDataAlert(JACK, "Jack H", "suunto", "sync failed")
    assert should_send_alert(
        alert,
        suunto_cfg=cfg,
        token_dir=None,
        zulip_user_id=73,
        zulip_email=None,
    )
    assert not should_send_alert(
        alert,
        suunto_cfg=cfg,
        token_dir=None,
        zulip_user_id=None,
        zulip_email=None,
    )
    emil = AthleteDataAlert(EMIL, "Emil", "suunto", "sync failed")
    assert not should_send_alert(
        emil,
        suunto_cfg=cfg,
        token_dir=None,
        zulip_user_id=77,
        zulip_email=None,
    )


def test_format_and_merge_alerts():
    a = AthleteDataAlert(
        JACK, "Jack H", "suunto", "Your Suunto workouts did not sync this week."
    )
    body = format_alert_dm(a)
    assert "not your weekly plan" in body.lower()
    assert "Suunto" in body
    merged = merge_alerts(
        [
            a,
            AthleteDataAlert(JACK, "Jack H", "suunto", "No matching indoor row."),
        ]
    )
    assert list(merged) == [JACK]
    assert "did not sync" in merged[JACK].message
    assert "matching indoor row" in merged[JACK].message


def test_merge_dedupes_identical_messages():
    a1 = AthleteDataAlert(JACK, "Jack H", "suunto", SUUNTO_SYNC_FAIL_MESSAGE)
    a2 = AthleteDataAlert(JACK, "Jack H", "suunto", SUUNTO_SYNC_FAIL_MESSAGE)
    merged = merge_alerts([a1, a2])
    assert merged[JACK].message == SUUNTO_SYNC_FAIL_MESSAGE


def test_merge_mixed_source():
    suunto = AthleteDataAlert(JACK, "Jack H", "suunto", "Suunto issue")
    screenshot = AthleteDataAlert(JACK, "Jack H", "screenshot", "Screenshot issue")
    merged = merge_alerts([suunto, screenshot])
    assert merged[JACK].source == "mixed"
    body = format_alert_dm(merged[JACK])
    assert "**Coachbot data issue**" in body
    assert "Suunto issue" in body
    assert "Screenshot issue" in body


def test_send_athlete_data_alerts_omits_suunto_when_cfg_not_passed():
    jack = SimpleNamespace(
        id=JACK, zulip_user_id=73, zulip_email=None, token_dir=None
    )
    alerts = [AthleteDataAlert(JACK, "Jack H", "suunto", "sync failed")]
    sent: list[tuple[str, list[int | str]]] = []

    def send_fn(content: str, recipients: list[int | str]) -> None:
        sent.append((content, recipients))

    count = send_athlete_data_alerts(alerts, [jack], send_fn=send_fn)
    assert count == 0
    assert sent == []


def test_send_athlete_data_alerts_filters_by_mapping():
    jack = SimpleNamespace(
        id=JACK, zulip_user_id=73, zulip_email=None, token_dir=None
    )
    emil = SimpleNamespace(
        id=EMIL, zulip_user_id=77, zulip_email=None, token_dir=None
    )
    alerts = [
        AthleteDataAlert(JACK, "Jack H", "suunto", "sync failed"),
        AthleteDataAlert(EMIL, "Emil", "suunto", "sync failed"),
    ]
    sent: list[tuple[str, list[int | str]]] = []

    def send_fn(content: str, recipients: list[int | str]) -> None:
        sent.append((content, recipients))

    count = send_athlete_data_alerts(
        alerts,
        [jack, emil],
        send_fn=send_fn,
        suunto_cfg=_suunto(),
    )
    assert count == 1
    assert len(sent) == 1
    assert sent[0][1] == [73]


def test_screenshot_gap_alert_when_no_suunto_in_week(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 8, 24))
    scores = tmp_path / f"athlete_{JACK}" / "erg_scores"
    scores.mkdir(parents=True)
    (scores / "abc.json").write_text(
        json.dumps(
            {
                "id": "abc",
                "athlete_id": JACK,
                "session_date": "2026-08-27",
                "source": "zulip_screenshot_vision_multi",
                "metrics": {
                    "duration_sec": 2160,
                    "avg_hr": 141,
                    "distance_m": 8170,
                },
            }
        )
    )

    alert = screenshot_without_suunto_alert(
        tmp_path, JACK, "Jack H", week, _suunto()
    )

    assert alert is not None
    assert alert.source == "suunto"


def test_no_gap_alert_without_screenshot(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 8, 24))
    (tmp_path / f"athlete_{JACK}").mkdir()

    assert (
        screenshot_without_suunto_alert(
            tmp_path, JACK, "Jack H", week, _suunto()
        )
        is None
    )


def test_no_gap_alert_when_suunto_erg_exists_in_week(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 8, 24))
    athlete_dir = tmp_path / f"athlete_{JACK}"
    scores = athlete_dir / "erg_scores"
    scores.mkdir(parents=True)
    (scores / "abc.json").write_text(
        json.dumps(
            {
                "id": "abc",
                "athlete_id": JACK,
                "session_date": "2026-08-27",
                "source": "zulip_screenshot_vision_multi",
            }
        )
    )
    suunto = athlete_dir / "suunto"
    suunto.mkdir()
    start = datetime(2026, 8, 27, 8, tzinfo=timezone.utc)
    (suunto / "index.json").write_text(
        json.dumps(
            {
                "workouts": {
                    "wk": {
                        "key": "wk",
                        "activityId": 57,
                        "startTime": int(start.timestamp() * 1000),
                    }
                }
            }
        )
    )

    assert (
        screenshot_without_suunto_alert(
            tmp_path, JACK, "Jack H", week, _suunto()
        )
        is None
    )


def test_sync_error_takes_precedence_over_screenshot_gap(tmp_path: Path):
    week = week_bounds_from_monday(date(2026, 8, 24))
    scores = tmp_path / f"athlete_{JACK}" / "erg_scores"
    scores.mkdir(parents=True)
    (scores / "abc.json").write_text(
        json.dumps(
            {
                "id": "abc",
                "athlete_id": JACK,
                "session_date": "2026-08-27",
                "source": "zulip_screenshot_vision_multi",
            }
        )
    )
    athletes = [SimpleNamespace(id=JACK, label="Jack H")]

    alerts = collect_suunto_alerts(
        athletes,
        cache_dir=tmp_path,
        week=week,
        suunto_cfg=_suunto(),
        sync_errors={JACK: "suuntool not found"},
    )

    assert alerts == [
        AthleteDataAlert(JACK, "Jack H", "suunto", SUUNTO_SYNC_FAIL_MESSAGE)
    ]
