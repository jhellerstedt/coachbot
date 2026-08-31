"""Zulip DMs when a mapped athlete data source fails during the weekly run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from generate_training_plan import WeekBounds, load_erg_scores_for_week, week_contains
from suunto_client import SuuntoCfg, suunto_sync_enabled_for_athlete
from suunto_sync import list_suunto_erg_workouts, suunto_start_dt

SUUNTO_SYNC_FAIL_MESSAGE = (
    "Your Suunto workouts did not sync this week, so watch/HR streams and "
    "off-plan ergs may be missing from the squad summary.\n\n"
    "If you posted a Concept2 screenshot to Zulip, that still counts. "
    "Otherwise reply here with the session."
)
SUUNTO_SCREENSHOT_GAP_MESSAGE = (
    "A Concept2 screenshot is in your log this week, but no matching Suunto "
    "indoor row was found. Off-plan watch sessions stay out of the HR plot "
    "until Suunto syncs."
)

_TITLE_BY_SOURCE = {
    "suunto": "**Coachbot could not refresh your Suunto data**",
    "strava": "**Coachbot could not refresh your Strava data**",
    "screenshot": "**Coachbot could not refresh your logged erg stats**",
}


def _default_suunto_cfg() -> SuuntoCfg:
    return SuuntoCfg(
        enabled=False,
        primary=True,
        suuntool_path=None,
        session_file=None,
        indoor_rowing_activity_ids=frozenset({57}),
        gym_activity_ids=frozenset({23}),
        athlete_ids=frozenset(),
    )


@dataclass(frozen=True)
class AthleteDataAlert:
    athlete_id: int
    label: str
    source: str
    message: str


def screenshot_without_suunto_alert(
    cache_dir: Path,
    athlete_id: int,
    label: str,
    week: WeekBounds,
    suunto_cfg: SuuntoCfg,
) -> Optional[AthleteDataAlert]:
    if not suunto_sync_enabled_for_athlete(suunto_cfg, athlete_id):
        return None
    scores = load_erg_scores_for_week(cache_dir, athlete_id, week)
    if not any("screenshot" in str(score.get("source")) for score in scores):
        return None
    for workout in list_suunto_erg_workouts(cache_dir, athlete_id, suunto_cfg):
        start = suunto_start_dt(workout)
        if start is not None and week_contains(week, start):
            return None
    return AthleteDataAlert(
        athlete_id=athlete_id,
        label=label,
        source="suunto",
        message=SUUNTO_SCREENSHOT_GAP_MESSAGE,
    )


def zulip_recipient(
    zulip_user_id: Optional[int],
    zulip_email: Optional[str],
) -> int | str | None:
    if zulip_user_id is not None:
        return zulip_user_id
    if zulip_email:
        return zulip_email
    return None


def source_mapped_for_athlete(
    source: str,
    *,
    athlete_id: int,
    suunto_cfg: SuuntoCfg,
    token_dir: Optional[Path],
    zulip_user_id: Optional[int],
    zulip_email: Optional[str],
) -> bool:
    if source == "suunto":
        return suunto_cfg.enabled and suunto_sync_enabled_for_athlete(
            suunto_cfg, athlete_id
        )
    if source == "strava":
        return token_dir is not None
    if source == "screenshot":
        return zulip_user_id is not None or bool(zulip_email)
    return False


def should_send_alert(
    alert: AthleteDataAlert,
    *,
    suunto_cfg: SuuntoCfg,
    token_dir: Optional[Path],
    zulip_user_id: Optional[int],
    zulip_email: Optional[str],
) -> bool:
    if zulip_recipient(zulip_user_id, zulip_email) is None:
        return False
    return source_mapped_for_athlete(
        alert.source,
        athlete_id=alert.athlete_id,
        suunto_cfg=suunto_cfg,
        token_dir=token_dir,
        zulip_user_id=zulip_user_id,
        zulip_email=zulip_email,
    )


def merge_alerts(alerts: Sequence[AthleteDataAlert]) -> dict[int, AthleteDataAlert]:
    merged: dict[int, AthleteDataAlert] = {}
    messages_by_athlete: dict[int, list[str]] = {}
    sources_by_athlete: dict[int, set[str]] = {}
    for alert in alerts:
        existing = merged.get(alert.athlete_id)
        if existing is None:
            merged[alert.athlete_id] = alert
            messages_by_athlete[alert.athlete_id] = [alert.message]
            sources_by_athlete[alert.athlete_id] = {alert.source}
            continue
        messages = messages_by_athlete[alert.athlete_id]
        if alert.message not in messages:
            messages.append(alert.message)
        sources = sources_by_athlete[alert.athlete_id]
        sources.add(alert.source)
        source = "mixed" if len(sources) > 1 else next(iter(sources))
        merged[alert.athlete_id] = AthleteDataAlert(
            alert.athlete_id,
            existing.label,
            source,
            "\n\n".join(messages),
        )
    return merged


def format_alert_dm(alert: AthleteDataAlert) -> str:
    title = _TITLE_BY_SOURCE.get(alert.source, "**Coachbot data issue**")
    footer = "_This is not your weekly plan._"
    return f"{title}\n\n{alert.message}\n\n{footer}"


def send_athlete_data_alerts(
    alerts: Sequence[AthleteDataAlert],
    athletes: Sequence[object],
    *,
    send_fn: Callable[[str, list[int | str]], object],
    suunto_cfg: SuuntoCfg | None = None,
) -> int:
    if suunto_cfg is None:
        suunto_cfg = _default_suunto_cfg()
    by_id = {athlete.id: athlete for athlete in athletes}
    sendable: list[AthleteDataAlert] = []
    for alert in alerts:
        athlete = by_id.get(alert.athlete_id)
        if athlete is None:
            continue
        token_dir = getattr(athlete, "token_dir", None)
        if not should_send_alert(
            alert,
            suunto_cfg=suunto_cfg,
            token_dir=token_dir,
            zulip_user_id=getattr(athlete, "zulip_user_id", None),
            zulip_email=getattr(athlete, "zulip_email", None),
        ):
            continue
        sendable.append(alert)

    sent = 0
    for alert in merge_alerts(sendable).values():
        athlete = by_id[alert.athlete_id]
        recipient = zulip_recipient(
            getattr(athlete, "zulip_user_id", None),
            getattr(athlete, "zulip_email", None),
        )
        assert recipient is not None
        send_fn(format_alert_dm(alert), [recipient])
        sent += 1
    return sent
