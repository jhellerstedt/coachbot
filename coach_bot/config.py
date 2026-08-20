"""Load coach bot settings from environment and erg_strava config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from coach_bot.intents import extract_zulip_user_mentions

import yaml

from generate_training_plan import DEFAULT_PLAN_TIMEZONE, set_plan_timezone

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STREAM = "general"
# Empty or "*" = all topics in the stream; otherwise only that topic name.
DEFAULT_TOPIC = ""


def listens_all_topics(topic: str) -> bool:
    t = topic.strip()
    return not t or t == "*"
BODY_WEIGHT_KG_MIN = 35.0
BODY_WEIGHT_KG_MAX = 250.0
MAX_HR_BPM_MIN = 100
MAX_HR_BPM_MAX = 240


def get_config_path() -> Path:
    return Path(
        os.environ.get("CONFIG_PATH", REPO_ROOT / "erg_strava" / "config.yaml")
    )


@dataclass(frozen=True)
class CoachAthleteCfg:
    id: int
    label: str
    zulip_email: Optional[str] = None
    zulip_full_name: Optional[str] = None
    zulip_user_id: Optional[int] = None
    body_weight_kg: Optional[float] = None
    max_hr_bpm: Optional[int] = None
    hr_z2_pct: Tuple[float, float] = (0.60, 0.75)
    hr_z5_pct: Tuple[float, float] = (0.90, 1.00)
    five_zone_pct: Optional[Mapping[str, Tuple[float, float]]] = None
    seven_zone_pct: Optional[Mapping[str, Tuple[float, float]]] = None
    training_zone_pct: Optional[Mapping[str, Tuple[float, float]]] = None

    def hr_zone_context_text(self) -> str:
        from athlete_profile import (
            DEFAULT_FIVE_ZONE_PCT,
            DEFAULT_SEVEN_ZONE_PCT,
            AthleteProfile,
        )

        seven = (
            dict(self.seven_zone_pct)
            if self.seven_zone_pct
            else (
                dict(self.training_zone_pct)
                if self.training_zone_pct
                else dict(DEFAULT_SEVEN_ZONE_PCT)
            )
        )
        return AthleteProfile(
            id=self.id,
            label=self.label,
            body_weight_kg=self.body_weight_kg,
            max_hr_bpm=self.max_hr_bpm,
            zulip_email=self.zulip_email,
            zulip_user_id=self.zulip_user_id,
            hr_z2_pct=self.hr_z2_pct,
            hr_z5_pct=self.hr_z5_pct,
            five_zone_pct=(
                dict(self.five_zone_pct)
                if self.five_zone_pct
                else dict(DEFAULT_FIVE_ZONE_PCT)
            ),
            seven_zone_pct=seven,
        ).hr_zone_context_text()


def _load_athletes(raw: dict, config_path: Path) -> List[CoachAthleteCfg]:
    try:
        from athlete_profile import load_athlete_profiles

        full_names = {
            int(entry["id"]): str(entry["zulip_full_name"]).strip()
            for entry in raw.get("athletes") or []
            if isinstance(entry, dict)
            and entry.get("id") is not None
            and entry.get("zulip_full_name")
        }
        return [
            CoachAthleteCfg(
                id=p.id,
                label=p.label,
                zulip_email=p.zulip_email,
                zulip_full_name=full_names.get(p.id),
                zulip_user_id=p.zulip_user_id,
                body_weight_kg=p.body_weight_kg,
                max_hr_bpm=p.max_hr_bpm,
                hr_z2_pct=p.hr_z2_pct,
                hr_z5_pct=p.hr_z5_pct,
                five_zone_pct=dict(p.five_zone_pct),
                seven_zone_pct=dict(p.seven_zone_pct),
                training_zone_pct=dict(p.seven_zone_pct),
            )
            for p in load_athlete_profiles(raw)
        ]
    except ImportError:
        pass
    athletes: List[CoachAthleteCfg] = []
    for entry in raw.get("athletes") or []:
        if not isinstance(entry, dict) or "id" not in entry:
            continue
        athletes.append(
            CoachAthleteCfg(
                id=int(entry["id"]),
                label=str(entry.get("label", f"athlete_{entry['id']}")),
                zulip_email=(
                    str(entry["zulip_email"]).strip().lower()
                    if entry.get("zulip_email")
                    else None
                ),
                zulip_full_name=(
                    str(entry["zulip_full_name"]).strip()
                    if entry.get("zulip_full_name")
                    else None
                ),
                zulip_user_id=(
                    int(entry["zulip_user_id"])
                    if entry.get("zulip_user_id") is not None
                    else None
                ),
            )
        )
    return athletes


def resolve_athlete_from_mention(
    athletes: List[CoachAthleteCfg],
    *,
    mention_name: str,
    mention_user_id: Optional[int] = None,
) -> Optional[CoachAthleteCfg]:
    if mention_user_id is not None:
        for athlete in athletes:
            if athlete.zulip_user_id is not None and athlete.zulip_user_id == mention_user_id:
                return athlete
    name = mention_name.strip().lower()
    for athlete in athletes:
        if athlete.zulip_full_name and athlete.zulip_full_name.lower() == name:
            return athlete
    for athlete in athletes:
        if athlete.label.strip().lower() == name:
            return athlete
    return None


def resolve_coach_subject(
    athletes: List[CoachAthleteCfg],
    *,
    sender_email: str,
    sender_full_name: str = "",
    sender_id: Optional[int] = None,
    message_content: str = "",
    bot_user_id: Optional[int] = None,
) -> Tuple[Optional[CoachAthleteCfg], Optional[CoachAthleteCfg]]:
    """
    Map Zulip message to (sender, subject) athletes.

    Subject defaults to sender; if another athlete is @-mentioned, they become subject.
    """
    sender = resolve_athlete_for_sender(
        athletes,
        sender_email=sender_email,
        sender_full_name=sender_full_name,
        sender_id=sender_id,
    )
    mentioned: List[CoachAthleteCfg] = []
    for mention_name, mention_uid in extract_zulip_user_mentions(message_content):
        if bot_user_id is not None and mention_uid == bot_user_id:
            continue
        athlete = resolve_athlete_from_mention(
            athletes,
            mention_name=mention_name,
            mention_user_id=mention_uid,
        )
        if athlete is not None and athlete not in mentioned:
            mentioned.append(athlete)
    subject = sender
    for athlete in mentioned:
        if sender is None or athlete.id != sender.id:
            subject = athlete
            break
    return sender, subject


def resolve_athlete_for_sender(
    athletes: List[CoachAthleteCfg],
    *,
    sender_email: str,
    sender_full_name: str = "",
    sender_id: Optional[int] = None,
) -> Optional[CoachAthleteCfg]:
    email = sender_email.strip().lower()
    name = sender_full_name.strip().lower()
    if sender_id is not None:
        for athlete in athletes:
            if athlete.zulip_user_id is not None and athlete.zulip_user_id == sender_id:
                return athlete
    for athlete in athletes:
        if athlete.zulip_email and athlete.zulip_email == email:
            return athlete
    for athlete in athletes:
        if athlete.zulip_full_name and athlete.zulip_full_name.lower() == name:
            return athlete
    for athlete in athletes:
        if athlete.label.strip().lower() == name:
            return athlete
    return None


def format_unmatched_sender_help(
    *,
    sender_email: str,
    sender_full_name: str,
    sender_id: Optional[int] = None,
) -> str:
    bits = [f"email `{sender_email or '?'}`", f"name `{sender_full_name or '?'}`"]
    if sender_id is not None:
        bits.append(f"user id `{sender_id}`")
    who = ", ".join(bits)
    return (
        f"I could not match your Zulip account ({who}) to an athlete.\n\n"
        "In `erg_strava/config.yaml`, set any of:\n"
        "- `zulip_email` — your Zulip login email (often *not* your Gmail)\n"
        "- `zulip_full_name` — exact Zulip display name\n"
        "- `zulip_user_id` — numeric id from your profile URL\n"
        "- `label` — already used if it matches your Zulip display name (e.g. `Jack H`)\n\n"
        "Restart the coach bot after editing config."
    )


def validate_profile_update_fields(
    *,
    body_weight_kg: Optional[float] = None,
    max_hr_bpm: Optional[int] = None,
) -> Tuple[Optional[float], Optional[int]]:
    """Return validated profile fields; at least one must be set."""
    validated_weight: Optional[float] = None
    validated_hr: Optional[int] = None
    if body_weight_kg is not None:
        weight = float(body_weight_kg)
        if not BODY_WEIGHT_KG_MIN <= weight <= BODY_WEIGHT_KG_MAX:
            raise ValueError(
                f"body_weight_kg must be between {BODY_WEIGHT_KG_MIN:g} and "
                f"{BODY_WEIGHT_KG_MAX:g} kg."
            )
        validated_weight = round(weight, 2)
    if max_hr_bpm is not None:
        hr = int(max_hr_bpm)
        if not MAX_HR_BPM_MIN <= hr <= MAX_HR_BPM_MAX:
            raise ValueError(
                f"max_hr_bpm must be between {MAX_HR_BPM_MIN} and {MAX_HR_BPM_MAX} bpm."
            )
        validated_hr = hr
    if validated_weight is None and validated_hr is None:
        raise ValueError("Provide body weight and/or max HR to update.")
    return validated_weight, validated_hr


def update_athlete_profile_in_config(
    config_path: Path,
    athlete_id: int,
    *,
    body_weight_kg: Optional[float] = None,
    max_hr_bpm: Optional[int] = None,
) -> Dict[str, Any]:
    """Persist body_weight_kg and/or max_hr_bpm for one athlete in config.yaml."""
    body_weight_kg, max_hr_bpm = validate_profile_update_fields(
        body_weight_kg=body_weight_kg,
        max_hr_bpm=max_hr_bpm,
    )
    if not config_path.is_file():
        raise FileNotFoundError(f"Config not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    athletes = raw.get("athletes") or []
    updated: Dict[str, Any] = {}
    found = False
    for entry in athletes:
        if not isinstance(entry, dict):
            continue
        if int(entry.get("id", -1)) != athlete_id:
            continue
        found = True
        if body_weight_kg is not None:
            entry["body_weight_kg"] = body_weight_kg
            updated["body_weight_kg"] = body_weight_kg
        if max_hr_bpm is not None:
            entry["max_hr_bpm"] = max_hr_bpm
            updated["max_hr_bpm"] = max_hr_bpm
        break
    if not found:
        raise ValueError(f"Athlete id {athlete_id} not found in {config_path}.")

    config_path.write_text(
        yaml.safe_dump(
            raw,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return updated


def format_profile_update_confirmation(
    updated: Mapping[str, Any],
    athlete: CoachAthleteCfg,
) -> str:
    parts: List[str] = []
    if "body_weight_kg" in updated:
        parts.append(f"body weight **{updated['body_weight_kg']:g} kg**")
    if "max_hr_bpm" in updated:
        parts.append(f"max HR **{updated['max_hr_bpm']} bpm**")
    lines = [f"**Updated `config.yaml`** — {', '.join(parts)}."]
    if "max_hr_bpm" in updated:
        hr_context = athlete.hr_zone_context_text()
        if hr_context:
            lines.append(hr_context)
    return "\n".join(lines)


def load_bot_config() -> Tuple[Path, str, str, str, Path, List[CoachAthleteCfg]]:
    """
    Returns (cache_dir, zulip_stream, zulip_topic, plan_timezone, zuliprc_path, athletes).
    """
    config_path = get_config_path()
    raw: dict = {}
    if config_path.is_file():
        with config_path.open(encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if isinstance(loaded, dict):
            raw = loaded

    if os.environ.get("CACHE_DIR"):
        cache_dir = Path(os.environ["CACHE_DIR"]).resolve()
    else:
        cache_dir = Path(raw.get("cache_dir", "./erg_strava_cache"))
        if not cache_dir.is_absolute():
            base = (
                config_path.parent
                if config_path.is_file()
                else REPO_ROOT / "erg_strava"
            )
            cache_dir = (base / cache_dir).resolve()

    plan_tz = str(raw.get("plan_timezone", DEFAULT_PLAN_TIMEZONE))
    set_plan_timezone(plan_tz)

    stream = os.environ.get("ZULIP_STREAM", DEFAULT_STREAM)
    topic = os.environ.get("ZULIP_TOPIC", DEFAULT_TOPIC)
    zuliprc = Path(os.environ.get("ZULIPRC_PATH", REPO_ROOT / "rrcc-zuliprc"))
    athletes = _load_athletes(raw, config_path)
    return cache_dir, stream, topic, plan_tz, zuliprc, athletes
