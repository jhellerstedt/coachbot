"""Maintenance mode: pause the weekly pipeline and prescribe one session."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import yaml

from athlete_profile import AthleteProfile
from generate_training_plan import (
    load_erg_scores_for_athlete,
    load_gym_logs_for_athlete,
    save_athlete_weekly_plan,
    week_for_date,
)
from weekly_plan_schema import (
    WEEKDAYS,
    DayPlan,
    WeeklyPlan,
    _overlay_rowing_hr,
    _parse_day,
    estimate_day_session_minutes,
    render_day_text,
    render_plan_text,
    weekly_plan_to_dict,
)

PAUSED_WEEKLY_PLANS = (
    "Weekly plans are paused. Ask for a single session, "
    "for example: gym session, leg day, 45min."
)
SYNCING_NOTICE = "Syncing your latest sessions…"
SYNC_FAILED_LINE = "Sync failed; this session uses your cached data."
SAVED_LINE = "Saved as today's prescription."
WRITE_FAILED = "I couldn't write that session. Try the request again."

_ON_VALUES = frozenset({"true", "yes", "on", "1"})
_SET_NOTATION_RE = re.compile(
    r"\d+\s*[x×]\s*\d|\d+\s*r\s+\d+|\d+(?:\.\d+)?\s*kg\b",
    re.I,
)
_DURATION_MIN_RE = re.compile(r"\b(\d+)\s*min(?:ute)?s?\b", re.I)
_DURATION_MIN_TIGHT_RE = re.compile(r"\b(\d+)min\b", re.I)
_DURATION_HOUR_NUM_RE = re.compile(r"\b(\d+)\s*h(?:ours?)?\b", re.I)
_AN_HOUR_RE = re.compile(r"\ban hour\b", re.I)
_HALF_HOUR_RE = re.compile(r"\bhalf an hour\b", re.I)
_INTERVAL_RE = re.compile(r"\bintervals?\b", re.I)
_STEADY_RE = re.compile(r"\b(?:steady(?:\s+state)?|ut2|aerobic)\b", re.I)
_ERG_RE = re.compile(r"\berg\b", re.I)
_GYM_RE = re.compile(r"\bgym\b", re.I)
_REP_WORK_RE = re.compile(r"\d+\s*[x×]\s*\d", re.I)
_CUE_RE = re.compile(
    r"\b(?:session|workout|give me|what should|plan me|write me)\b",
    re.I,
)
_FOCUS_PATTERNS = (
    ("full body", "full body"),
    ("leg day", "legs"),
    ("legs", "legs"),
    ("leg", "legs"),
    ("push", "push"),
    ("pull", "pull"),
    ("upper", "upper"),
    ("lower", "lower"),
    ("chest", "chest"),
    ("back", "back"),
    ("arms", "arms"),
)

DecideFn = Callable[[Mapping[str, Any], Mapping[str, Any]], Optional[Mapping[str, Any]]]
CompleteFn = Callable[[str], str]
SyncFn = Callable[[], bool]


@dataclass(frozen=True)
class SessionRequest:
    status: str
    modality: Optional[str] = None
    focus: Optional[str] = None
    duration_min: Optional[int] = None
    clarify: str = ""


def maintenance_mode_enabled(raw: Mapping[str, Any]) -> bool:
    value = raw.get("maintenance_mode")
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() in _ON_VALUES:
        return True
    return False


def config_maintenance_enabled(path: Path) -> bool:
    if not path.is_file():
        return False
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        return False
    return maintenance_mode_enabled(loaded)


def _duration_minutes(text: str) -> Optional[int]:
    if _HALF_HOUR_RE.search(text):
        return 30
    match = _DURATION_MIN_RE.search(text) or _DURATION_MIN_TIGHT_RE.search(text)
    if match:
        return int(match.group(1))
    match = _DURATION_HOUR_NUM_RE.search(text)
    if match:
        return int(match.group(1)) * 60
    if _AN_HOUR_RE.search(text):
        return 60
    return None


def _focus(text: str) -> Optional[str]:
    lowered = text.lower()
    for needle, label in _FOCUS_PATTERNS:
        if re.search(rf"\b{re.escape(needle)}\b", lowered):
            return label
    return None


def _request_cue(text: str) -> bool:
    if _CUE_RE.search(text):
        return True
    return bool(re.search(r"\bI have\b", text, re.I) and _duration_minutes(text))


def _short_candidate(text: str) -> bool:
    if len(text.strip()) >= 80:
        return False
    if _INTERVAL_RE.search(text) or _STEADY_RE.search(text) or _GYM_RE.search(text):
        return True
    if _ERG_RE.search(text) and (_focus(text) or _duration_minutes(text)):
        return True
    return False


def _clarify_for(modality: Optional[str], focus: Optional[str], duration: Optional[int]) -> str:
    if modality == "gym":
        missing_focus = focus is None
        missing_duration = duration is None
        if missing_focus and missing_duration:
            return (
                "Which focus — legs, push, pull, upper, lower, or full body — "
                "and how long do you have?"
            )
        if missing_focus:
            return "Which focus — legs, push, pull, upper, lower, or full body?"
        return "How long do you have?"
    if modality == "interval":
        return "How long do you have?"
    return "Interval or steady state, and how long do you have?"


def _complete(modality: str, focus: Optional[str], duration: Optional[int]) -> bool:
    if modality == "gym":
        return focus is not None and duration is not None
    if modality == "interval":
        return duration is not None
    if modality == "steady":
        return True
    return False


def _modality(text: str, focus: Optional[str]) -> Optional[str]:
    interval = bool(_INTERVAL_RE.search(text))
    steady = bool(_STEADY_RE.search(text))
    erg = bool(_ERG_RE.search(text))
    gym = bool(_GYM_RE.search(text))
    if interval and steady:
        return "both"
    if interval and erg:
        return "interval"
    if steady and erg:
        return "steady"
    if gym or (focus and not erg):
        return "gym"
    if erg:
        return "erg"
    return None


def classify_session_request(
    text: str,
    *,
    decide: Optional[DecideFn] = None,
    extract: Optional[Callable[[str], Mapping[str, Any]]] = None,
    api_key: Optional[str] = None,
) -> SessionRequest:
    from coach_bot.erg_score import looks_like_erg_score_text
    from coach_bot.intents import looks_like_question

    body = (text or "").strip()
    if not body or _SET_NOTATION_RE.search(body) or looks_like_erg_score_text(body):
        return SessionRequest("not_a_request")

    focus = _focus(body)
    duration = _duration_minutes(body)
    modality = _modality(body, focus)
    question = looks_like_question(body)

    if modality == "both":
        if question and not _request_cue(body):
            return SessionRequest("not_a_request")
        return SessionRequest(
            "incomplete",
            modality="both",
            clarify="Interval or steady state?",
        )

    if modality and _complete(modality, focus, duration):
        return SessionRequest(
            "complete",
            modality=modality,
            focus=focus,
            duration_min=duration,
        )

    if question:
        return SessionRequest("not_a_request")

    candidate = _request_cue(body) or _short_candidate(body)
    if modality and candidate and not _complete(modality, focus, duration):
        return SessionRequest(
            "incomplete",
            modality=modality,
            focus=focus,
            duration_min=duration,
            clarify=_clarify_for(modality, focus, duration),
        )

    if not candidate or modality:
        return SessionRequest("not_a_request")

    choice = _decide_session_request(body, decide=decide, api_key=api_key)
    if choice != "session_request":
        return SessionRequest("not_a_request")
    extracted = _extract_request_fields(body, extract=extract, api_key=api_key)
    modality = str(extracted.get("modality") or "") or None
    if modality not in ("gym", "interval", "steady", "erg"):
        modality = None
    focus = extracted.get("focus") or focus
    if extracted.get("duration_min") is not None:
        try:
            duration = int(extracted["duration_min"])
        except (TypeError, ValueError):
            duration = duration
    if modality == "erg" or modality is None or (
        modality and not _complete(modality, focus if isinstance(focus, str) else None, duration)
    ):
        if modality in ("gym", "interval", "erg"):
            return SessionRequest(
                "incomplete",
                modality=modality,
                focus=focus if isinstance(focus, str) else None,
                duration_min=duration,
                clarify=_clarify_for(modality, focus if isinstance(focus, str) else None, duration),
            )
        return SessionRequest("not_a_request")
    return SessionRequest(
        "complete",
        modality=modality,
        focus=focus if isinstance(focus, str) else None,
        duration_min=duration,
    )


def _decide_session_request(
    text: str,
    *,
    decide: Optional[DecideFn],
    api_key: Optional[str],
) -> str:
    questions = {
        "intent": {
            "type": "choice",
            "instructions": "Is this athlete asking for a single workout?",
            "criteria": {
                "session_request": "They want one gym or erg session prescribed.",
                "other": "A question, log, or anything else.",
            },
        }
    }
    if decide is None:
        key = (api_key or os.environ.get("OPENROUTER_API_KEY") or "").strip()
        if not key:
            return "other"

        def decide(state: Mapping[str, Any], asked: Mapping[str, Any]):
            from openrouter_client import call_openrouter_decisions

            return call_openrouter_decisions(
                state=dict(state),
                questions=dict(asked),
                api_key=key,
            )

    try:
        answers = decide({"message": text}, questions)
    except Exception:
        return "other"
    if not isinstance(answers, Mapping):
        return "other"
    raw = answers.get("intent")
    if not isinstance(raw, Mapping):
        return "other"
    if raw.get("choice") != "session_request":
        return "other"
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        return "other"
    if confidence < 0.6:
        return "other"
    return "session_request"


def _extract_request_fields(
    text: str,
    *,
    extract: Optional[Callable[[str], Mapping[str, Any]]],
    api_key: Optional[str],
) -> Mapping[str, Any]:
    if extract is not None:
        try:
            found = extract(text)
        except Exception:
            return {}
        return found if isinstance(found, Mapping) else {}
    key = (api_key or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return {}
    from openrouter_client import call_openrouter

    try:
        raw = call_openrouter(
            system=(
                "Extract a workout request as JSON with keys modality "
                "(gym, interval, steady, or erg), focus (or null), and "
                "duration_min (or null). Reply with JSON only."
            ),
            user=text,
            api_key=key,
        )
        data = json.loads(raw.strip())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _is_interval_day(day: DayPlan) -> bool:
    subtype = (day.session_subtype or "").lower()
    if "interval" in subtype:
        return True
    if day.rowing is None:
        return False
    work = [seg for seg in day.rowing.segments if seg.phase in ("work", "main_set")]
    if len(work) >= 2:
        return True
    return any(
        _REP_WORK_RE.search(seg.duration or "") or _REP_WORK_RE.search(seg.label)
        for seg in day.rowing.segments
    )


def validate_generated_day(day: DayPlan, request: SessionRequest) -> Optional[str]:
    if request.modality == "gym":
        if day.session_type != "gym" or day.gym is None:
            return "expected a gym day"
        if not any(ex.sets for ex in day.gym.exercises):
            return "gym day has no sets"
    else:
        if day.session_type != "erg" or day.rowing is None:
            return "expected an erg day"
        if not day.rowing.segments:
            return "erg day has no segments"
        if any(seg.hr_bpm_min is None or seg.hr_bpm_max is None for seg in day.rowing.segments):
            return "erg segment missing heart rate"
        if request.modality == "interval" and not _is_interval_day(day):
            return "interval request returned a single steady piece"
    if request.duration_min and request.modality != "steady":
        mins = estimate_day_session_minutes(day)
        lo = request.duration_min * 0.5
        hi = request.duration_min * 1.5
        if not lo <= mins <= hi:
            return f"duration {mins} min outside {lo:g}-{hi:g}"
    if request.modality == "steady" and request.duration_min:
        mins = estimate_day_session_minutes(day)
        lo = request.duration_min * 0.5
        hi = request.duration_min * 1.5
        if not lo <= mins <= hi:
            return f"duration {mins} min outside {lo:g}-{hi:g}"
    return None


def day_from_model_text(raw: str, on: date) -> Optional[DayPlan]:
    text = (raw or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and isinstance(data.get("days"), list) and data["days"]:
        data = data["days"][0]
    if not isinstance(data, dict):
        return None
    payload = dict(data)
    payload["weekday"] = WEEKDAYS[on.weekday()]
    payload["date"] = on.isoformat()
    return _parse_day(payload)


def _session_prompt(
    request: SessionRequest,
    *,
    athlete_label: str,
    on: date,
    hr_context: str,
    training_context: str,
    gym_context: str,
) -> str:
    lines = [
        f"Prescribe one session for {athlete_label} on {on.isoformat()} ({WEEKDAYS[on.weekday()]}).",
        f"Request modality: {request.modality}.",
    ]
    if request.focus:
        lines.append(f"Focus: {request.focus}.")
    if request.duration_min:
        lines.append(f"Duration: about {request.duration_min} minutes.")
    else:
        lines.append("Choose the duration and target heart rate from their zones and recent ergs.")
    lines.append(
        "Reply with one DayPlan JSON object. session_type is gym or erg. "
        "Gym days use exactly 4 exercises from the known list, category leg or "
        "upper_core, goal strength, hypertrophy, power, or recovery. "
        "Erg days include rowing segments with phase, label, duration, split_min, "
        "split_max, zone_z, zone_t, hr_bpm_min, hr_bpm_max, and priority. "
        "Use their recent loads. Heart-rate targets are absolute bpm."
    )
    if hr_context:
        lines.append(hr_context)
    if training_context:
        lines.append(training_context)
    if gym_context:
        lines.append(gym_context)
    return "\n\n".join(lines)


def generate_session_day(
    request: SessionRequest,
    *,
    on: date,
    athlete_label: str,
    complete: CompleteFn,
    profile: Optional[AthleteProfile] = None,
    hr_context: str = "",
    training_context: str = "",
    gym_context: str = "",
) -> Optional[DayPlan]:
    prompt = _session_prompt(
        request,
        athlete_label=athlete_label,
        on=on,
        hr_context=hr_context,
        training_context=training_context,
        gym_context=gym_context,
    )
    for _attempt in range(2):
        try:
            raw = complete(prompt)
        except Exception:
            return None
        day = day_from_model_text(raw, on)
        if day is None:
            continue
        if profile is not None and day.rowing is not None:
            day = replace(day, rowing=_overlay_rowing_hr(day.rowing, profile))
        if validate_generated_day(day, request) is None:
            return day
    return None


def has_gym_history(cache_dir: Path, athlete_id: int) -> bool:
    if load_gym_logs_for_athlete(cache_dir, athlete_id):
        return True
    metrics = cache_dir / f"athlete_{athlete_id}" / "metrics"
    if not metrics.is_dir():
        return False
    for path in metrics.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        gym = data.get("gym") if isinstance(data, dict) else None
        if isinstance(gym, dict) and gym.get("exercises"):
            return True
    return False


def has_erg_history(cache_dir: Path, athlete_id: int) -> bool:
    from erg_session_merge import load_merged_erg_sessions_for_athlete

    if load_merged_erg_sessions_for_athlete(cache_dir, athlete_id, limit=1):
        return True
    return bool(load_erg_scores_for_athlete(cache_dir, athlete_id, limit=1))


def empty_history_reply(modality: str, label: str) -> str:
    kind = "gym" if modality == "gym" else "erg"
    return (
        f"I don't have any recent {kind} sessions cached for {label}, "
        "so I can't set loads or targets. Log a session and ask again."
    )


def format_saved_reply(day: DayPlan, *, sync_failed: bool) -> str:
    body = render_day_text(day, absolute_hr_bpm=True)
    lines = []
    if sync_failed:
        lines.append(SYNC_FAILED_LINE)
    lines.append(body)
    lines.append("")
    lines.append(SAVED_LINE)
    return "\n".join(lines)


def _existing_days(cache_dir: Path, athlete_id: int, on: date) -> tuple[list[DayPlan], list[str]]:
    from generate_training_plan import load_athlete_weekly_plan

    record = load_athlete_weekly_plan(cache_dir, athlete_id, week_for_date(on).week_id)
    if not record:
        return [], []
    maintenance_days = [
        str(item) for item in (record.get("maintenance_days") or []) if str(item).strip()
    ]
    raw_days = []
    plan_json = record.get("plan_json")
    if isinstance(plan_json, dict) and isinstance(plan_json.get("days"), list):
        raw_days = plan_json["days"]
    days: list[DayPlan] = []
    for item in raw_days:
        if isinstance(item, dict):
            parsed = _parse_day(item)
            if parsed is not None:
                days.append(parsed)
    if days:
        return days, maintenance_days
    plan_text = str(record.get("plan_text") or "")
    if not plan_text.strip():
        return [], maintenance_days
    try:
        from weekly_plan_harness import import_prose_plan_json

        imported = import_prose_plan_json(
            plan_text,
            week_start=week_for_date(on).week_start.isoformat(),
            personalised=True,
        )
    except Exception:
        imported = None
    if isinstance(imported, dict):
        for item in imported.get("days") or []:
            if isinstance(item, dict):
                parsed = _parse_day(item)
                if parsed is not None:
                    days.append(parsed)
    return days, maintenance_days


def save_maintenance_day(
    cache_dir: Path,
    athlete_id: int,
    day: DayPlan,
    on: date,
) -> Path:
    weekday = WEEKDAYS[on.weekday()]
    iso = on.isoformat()
    day = replace(day, weekday=weekday, date=iso)
    existing, maintenance_days = _existing_days(cache_dir, athlete_id, on)
    kept: list[DayPlan] = []
    replaced = False
    for current in existing:
        if current.date == iso or current.weekday == weekday:
            if not replaced:
                kept.append(day)
                replaced = True
            continue
        kept.append(current)
    if not replaced:
        kept.append(day)
    plan = WeeklyPlan(version=1, personalised=True, days=kept)
    week = week_for_date(on)
    path = save_athlete_weekly_plan(
        cache_dir,
        athlete_id,
        week,
        render_plan_text(plan, absolute_hr_bpm=True),
        plan_json=weekly_plan_to_dict(plan),
    )
    if iso not in maintenance_days:
        maintenance_days.append(iso)
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["maintenance_days"] = maintenance_days
    path.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    return path


def fulfill_session_request(
    request: SessionRequest,
    *,
    cache_dir: Path,
    athlete_id: int,
    athlete_label: str,
    on: date,
    sync: SyncFn,
    complete: CompleteFn,
    profile: Optional[AthleteProfile] = None,
    hr_context: str = "",
    training_context: str = "",
    gym_context: str = "",
) -> str:
    if request.status == "incomplete":
        return request.clarify
    if request.status != "complete":
        return ""
    sync_failed = False
    try:
        if sync() is False:
            sync_failed = True
    except Exception:
        sync_failed = True
    modality = request.modality or ""
    has_history = (
        has_gym_history(cache_dir, athlete_id)
        if modality == "gym"
        else has_erg_history(cache_dir, athlete_id)
    )
    if not has_history:
        return empty_history_reply(modality, athlete_label)
    day = generate_session_day(
        request,
        on=on,
        athlete_label=athlete_label,
        complete=complete,
        profile=profile,
        hr_context=hr_context,
        training_context=training_context,
        gym_context=gym_context,
    )
    if day is None:
        return WRITE_FAILED
    save_maintenance_day(cache_dir, athlete_id, day, on)
    return format_saved_reply(day, sync_failed=sync_failed)


def sync_one_athlete(config_path: Path, athlete_id: int) -> bool:
    """Incremental Suunto-first sync for one athlete. False when sync fails."""
    from erg_session_merge import rebuild_merged_erg_sessions_for_athlete
    from strava_erg_hr_plot import (
        athlete_paths,
        is_gym_activity,
        load_config,
        load_index,
        resolve_available_strava_credentials,
        sync_athlete,
    )

    (
        cache_dir,
        athletes,
        erg_types,
        require_trainer,
        photo_cfg,
        gym_cfg,
        suunto_cfg,
        strava_cfg,
    ) = load_config(config_path)
    acfg = next((athlete for athlete in athletes if athlete.id == athlete_id), None)
    if acfg is None:
        return False
    config_base = config_path.parent.resolve()
    creds = resolve_available_strava_credentials([acfg], config_base)
    transport = None
    token_owner: Optional[int] = None
    if creds is not None:
        transport, token_owner, _token_dir = creds
    acfg_transport = transport if token_owner == acfg.id else None
    ok = True
    try:
        result = sync_athlete(
            acfg,
            cache_dir,
            erg_types,
            require_trainer_for_rowing=require_trainer,
            force_refresh_streams=False,
            photo_cfg=photo_cfg,
            refresh_photos=False,
            transport=acfg_transport,
            suunto_cfg=suunto_cfg,
            strava_cfg=strava_cfg,
            config_base=config_base,
            gym_cfg=gym_cfg,
            full_sync=False,
            refresh_suunto=False,
        )
        if result is False:
            ok = False
    except Exception:
        ok = False
    finally:
        if transport is not None:
            transport.close()
    try:
        rebuild_merged_erg_sessions_for_athlete(
            cache_dir,
            acfg.id,
            athlete_label=acfg.label,
            erg_types=erg_types,
            require_trainer_for_rowing=require_trainer,
        )
    except Exception:
        pass
    token = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if token:
        try:
            from generate_training_plan import sync_activity_metrics_cache

            paths = athlete_paths(cache_dir, acfg.id)
            index = load_index(paths["index"])
            gym_acts = [
                act
                for act in index.get("activities", [])
                if isinstance(act, dict)
                and is_gym_activity(act, gym_cfg.sport_types, gym_cfg.name_patterns)
            ]
            if gym_acts:
                sync_activity_metrics_cache(
                    [acfg],
                    cache_dir,
                    athlete_paths,
                    gym_acts,
                    {},
                    gym_cfg.sport_types,
                    gym_cfg.name_patterns,
                    token=token,
                )
        except Exception:
            pass
    return ok
