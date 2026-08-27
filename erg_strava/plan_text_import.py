"""Best-effort import of cached plan_text into weekly plan JSON."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from weekly_plan_schema import GYM_EXERCISE_NAMES, WEEKDAYS, parse_weekly_plan

_SEGMENT_LINE_RE = re.compile(
    r"^[-•]\s*(Warm-up|Main Set|Cool-down|Followed by):\s*(.+)$",
    re.I,
)
_ROWING_BULLET_RE = re.compile(
    r"^[-•]\s*(Warm-up|Main Set|Cool-down|Followed by):\s*"
    r"(?:(\d+)\s*min\s*)?(?:@\s*)?(Z\d)/(T\d)?,?\s*"
    r"split\s*([^,]+),\s*HR\s*(\d+)\s*[–-]\s*(\d+)\s*bpm,?\s*"
    r"priority:\s*(\w+)",
    re.I,
)
_SET_LINE_RE = re.compile(
    r"Set\s+(\d+):\s*"
    r"(?:(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*kg|(\d+)s\s*hold|(\d+)\s*s\s*hold)",
    re.I,
)
_EXERCISE_NUM_RE = re.compile(r"^\d+\.\s*\*?\*?([^*\n]+?)\*?\*?\s*$")
_ATHLETE_DAY_HEADER_RE = re.compile(
    r"^\*?\*?(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r"(?:\s*,\s*\d{4}-\d{2}-\d{2})?\*?\*?\s*$",
    re.I,
)
_ATHLETE_SESSION_TYPE_RE = re.compile(
    r"Session Type:\s*(.+)",
    re.I,
)
_ATHLETE_ROWING_LINE_RE = re.compile(
    r"^(Warm-up|Main Set|Cool-down|Rest):\s*(.+?)\s*@\s*(Z\d)/(T\d),?\s*"
    r"split\s*([^,]+),\s*HR\s*(\d+)\s*[–-]\s*(\d+)\s*bpm"
    r"(?:,?\s*priority:\s*(\w+))?",
    re.I,
)
_ATHLETE_DAY_MD3_RE = re.compile(
    r"^#{1,3}\s*(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r"(?:\s*,\s*\d{4}-\d{2}-\d{2})?\s*$",
    re.I,
)
_MAIN_SET_HEADER_RE = re.compile(r"^\*\*Main Set:\*\*\s*(.+)$", re.I | re.M)
_GYM_GOAL_RE = re.compile(r"\*Goal:\s*(\w+)\*", re.I)
_DAY_HEADER_MD_RE = re.compile(
    r"\*?\*?(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r"(?:\s*[,\(][^*\n]*)?\s*:?\s*(.*?)\*?\*?\s*$",
    re.I,
)
_DAY_HEADER_RENDER_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday):\s*(.*)$",
    re.I,
)
_RENDER_SEGMENT_RE = re.compile(
    r"^\s*(Warm Up|Main Set|Cool Down|Work|Rest):\s*(.+?),?\s*@\s*(Z\d)/(T\d),\s*"
    r"split\s*([^,]+),\s*HR\s*(\d+)\s*[–-]\s*(\d+)\s*bpm,\s*priority:\s*(\w+)",
    re.I,
)
_ZONE_SPLIT_HR_RE = re.compile(
    r"@\s*(Z\d)/(T\d),?\s*split\s*~?\s*([^,]+),\s*"
    r"HR\s*~?\s*(\d+)(?:\s*[–-]\s*(\d+))?\s*bpm,?\s*"
    r"priority:\s*(\w+)",
    re.I,
)
_CANONICAL_GYM = {n.lower(): n for n in GYM_EXERCISE_NAMES}


def _monday_of(week_start: str) -> date:
    return date.fromisoformat(week_start[:10])


def _day_dates(week_start: str) -> Dict[str, str]:
    monday = _monday_of(week_start)
    return {
        wd: (monday + timedelta(days=i)).isoformat()
        for i, wd in enumerate(WEEKDAYS)
    }


def _normalize_split_range(raw: str) -> Tuple[str, str]:
    text = raw.strip().replace("~", "").replace("–", "-").strip()
    if "-" in text:
        a, b = [p.strip() for p in text.split("-", 1)]
        return a, b
    return text, text


def _normalize_priority(raw: str) -> str:
    return "split" if raw.strip().lower() == "split" else "hr"


def _canonical_exercise(name: str) -> Optional[str]:
    key = re.sub(r"\s+", " ", name.strip()).lower()
    key = key.replace("lat pull-downs", "lat pull-down").replace("lat pulls", "lat pull-down")
    if key in _CANONICAL_GYM:
        return _CANONICAL_GYM[key]
    for official in GYM_EXERCISE_NAMES:
        if official.lower() in key or key in official.lower():
            return official
    return None


def _phase_from_label(label: str) -> str:
    lower = label.lower()
    if "warm" in lower:
        return "warm_up"
    if "cool" in lower:
        return "cool_down"
    if "rest" in lower:
        return "rest"
    return "main_set"


def _parse_gym_block(text: str, category: str) -> Optional[Dict[str, Any]]:
    goal_m = _GYM_GOAL_RE.search(text)
    goal = (goal_m.group(1).lower() if goal_m else "strength")
    if goal not in ("strength", "hypertrophy", "power", "recovery"):
        goal = "strength"
    exercises: List[Dict[str, Any]] = []
    current_name: Optional[str] = None
    current_sets: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        ex_m = _EXERCISE_NUM_RE.match(line)
        if ex_m:
            if current_name and current_sets:
                exercises.append({"name": current_name, "sets": current_sets})
            current_name = _canonical_exercise(ex_m.group(1))
            current_sets = []
            continue
        set_m = _SET_LINE_RE.search(line)
        if set_m and current_name:
            if set_m.group(4):
                current_sets.append(
                    {"reps": 1, "weight_kg": None, "duration_sec": int(set_m.group(4))}
                )
            elif set_m.group(5):
                current_sets.append(
                    {"reps": 1, "weight_kg": None, "duration_sec": int(set_m.group(5))}
                )
            else:
                current_sets.append(
                    {
                        "reps": int(set_m.group(2)),
                        "weight_kg": float(set_m.group(3)),
                        "duration_sec": None,
                    }
                )
    if current_name and current_sets:
        exercises.append({"name": current_name, "sets": current_sets})
    if len(exercises) != 4:
        return None
    return {"category": category, "goal": goal, "exercises": exercises}


def _parse_rowing_segment_line(label: str, rest: str) -> Optional[Dict[str, Any]]:
    zm = _ZONE_SPLIT_HR_RE.search(rest)
    if not zm:
        return None
    duration = rest.split("@", 1)[0].strip(" :-")
    if not duration:
        duration = rest.strip()
    split_min, split_max = _normalize_split_range(zm.group(3))
    hr_min = int(zm.group(4))
    hr_max = int(zm.group(5)) if zm.group(5) else hr_min
    return {
        "phase": _phase_from_label(label),
        "label": f"{label}: {duration}",
        "duration": duration,
        "split_min": split_min,
        "split_max": split_max,
        "zone_z": zm.group(1).upper(),
        "zone_t": zm.group(2).upper(),
        "hr_bpm_min": hr_min,
        "hr_bpm_max": hr_max,
        "priority": _normalize_priority(zm.group(6)),
        "notes": None,
    }


def _parse_render_segment(line: str) -> Optional[Dict[str, Any]]:
    m = _RENDER_SEGMENT_RE.match(line.strip())
    if not m:
        return None
    label, body, zone_z, zone_t, split_raw, hr_min, hr_max, priority = m.groups()
    split_min, split_max = _normalize_split_range(split_raw)
    duration = body.split("—", 1)[-1].strip() if "—" in body else body.strip()
    return {
        "phase": _phase_from_label(label),
        "label": f"{label}: {body.strip()}",
        "duration": duration.replace("m", " min") if duration.endswith("m") else duration,
        "split_min": split_min,
        "split_max": split_max,
        "zone_z": zone_z.upper(),
        "zone_t": zone_t.upper(),
        "hr_bpm_min": int(hr_min),
        "hr_bpm_max": int(hr_max),
        "priority": _normalize_priority(priority),
        "notes": None,
    }


def _session_type_from_header(header: str) -> str:
    lower = header.lower()
    if "gym" in lower or "strength" in lower or "hypertrophy" in lower:
        return "gym"
    if "on-water" in lower or "on water" in lower or "rowing on" in lower:
        return "on_water"
    if "erg" in lower or "steady" in lower or "threshold" in lower or "interval" in lower:
        return "erg"
    if "recovery" in lower:
        return "recovery"
    if "rest" in lower:
        return "rest"
    return "rest"


def _gym_category_for_day(weekday: str, header: str) -> str:
    lower = header.lower()
    if "upper" in lower or "core" in lower or "hypertrophy" in lower:
        return "upper_core"
    if weekday == "Wednesday":
        return "upper_core"
    if weekday == "Monday":
        return "leg"
    return "leg"


def _parse_athlete_rowing_line(line: str) -> Optional[Dict[str, Any]]:
    m = _ATHLETE_ROWING_LINE_RE.match(line.strip())
    if not m:
        return None
    label, body, zone_z, zone_t, split_raw, hr_min, hr_max, priority = m.groups()
    split_min, split_max = _normalize_split_range(split_raw)
    duration = body.strip()
    if label.lower() == "rest":
        phase = "rest"
    else:
        phase = _phase_from_label(label)
    return {
        "phase": phase,
        "label": f"{label}: {duration}",
        "duration": duration,
        "split_min": split_min,
        "split_max": split_max,
        "zone_z": zone_z.upper(),
        "zone_t": zone_t.upper(),
        "hr_bpm_min": int(hr_min),
        "hr_bpm_max": int(hr_max),
        "priority": _normalize_priority(priority or "hr"),
        "notes": None,
    }


def _main_set_header_spec(body: str) -> Optional[str]:
    m = _MAIN_SET_HEADER_RE.search(body)
    if not m:
        return None
    spec = m.group(1).strip()
    if "/" in spec and "rest" in spec.lower():
        return spec
    return None


def _apply_main_set_header_rest(
    segment: Dict[str, Any], main_set_header: Optional[str]
) -> None:
    if not main_set_header or segment.get("phase") != "main_set":
        return
    duration = str(segment.get("duration") or "").strip()
    if "/" in duration and "rest" in duration.lower():
        return
    segment["duration"] = main_set_header
    segment["label"] = f"Main Set: {main_set_header}"


def _split_day_blocks(text: str) -> List[Tuple[str, str]]:
    lines = text.splitlines()
    blocks: List[Tuple[str, str]] = []
    current_day: Optional[str] = None
    current_lines: List[str] = []
    for line in lines:
        stripped = line.strip()
        athlete_day = _ATHLETE_DAY_HEADER_RE.match(stripped)
        md3 = _ATHLETE_DAY_MD3_RE.match(stripped) if not athlete_day else None
        md = _DAY_HEADER_MD_RE.match(stripped) if not athlete_day and not md3 else None
        render = (
            _DAY_HEADER_RENDER_RE.match(stripped)
            if not md and not md3 and not athlete_day
            else None
        )
        if athlete_day or md3 or md or render:
            if current_day:
                blocks.append((current_day, "\n".join(current_lines)))
            if athlete_day or md3:
                current_day = (athlete_day or md3).group(1).title()  # type: ignore[union-attr]
                current_lines = []
            else:
                current_day = (md or render).group(1).title()  # type: ignore[union-attr]
                header_tail = (md or render).group(2).strip()  # type: ignore[union-attr]
                current_lines = [header_tail] if header_tail else []
        elif current_day:
            current_lines.append(line)
    if current_day:
        blocks.append((current_day, "\n".join(current_lines)))
    if blocks:
        return blocks

    # Combined Fri-Sun block
    combined = re.split(
        r"(?=(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b)",
        text,
        flags=re.I,
    )
    for chunk in combined:
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b(.*)", chunk, re.I | re.S)
        if m:
            blocks.append((m.group(1).title(), m.group(2).strip()))
    return blocks


def import_weekly_plan_json_from_text(
    plan_text: str,
    *,
    week_start: str,
    personalised: bool = False,
    greeting: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Parse cached plan_text into weekly plan JSON when possible."""
    text = (plan_text or "").strip()
    if not text:
        return None
    if greeting is None and personalised:
        first = text.splitlines()[0].strip()
        if first and first not in WEEKDAYS and not first.startswith("**") and not first.startswith("Monday"):
            greeting = first
            text = "\n".join(text.splitlines()[1:]).strip()

    day_dates = _day_dates(week_start)
    blocks = _split_day_blocks(text)
    if not blocks:
        return None

    days_by_name: Dict[str, Dict[str, Any]] = {}
    for weekday, body in blocks:
        if weekday not in WEEKDAYS:
            continue
        if "to sunday" in body.lower()[:40]:
            for wd in ("Friday", "Saturday", "Sunday"):
                days_by_name[wd] = {
                    "weekday": wd,
                    "date": day_dates[wd],
                    "session_type": "recovery" if wd == "Sunday" else "rest",
                    "session_subtype": None,
                    "gym": None,
                    "rowing": None,
                    "notes": body.strip()[:200] or None,
                }
            continue
        header_line = body.splitlines()[0] if body else ""
        session_m = _ATHLETE_SESSION_TYPE_RE.search(body[:300])
        if session_m:
            raw_type = session_m.group(1).replace("*", "").strip()
            session_type = _session_type_from_header(raw_type)
        else:
            session_type = _session_type_from_header(header_line + " " + body[:200])
        day: Dict[str, Any] = {
            "weekday": weekday,
            "date": day_dates[weekday],
            "session_type": session_type,
            "session_subtype": None,
            "gym": None,
            "rowing": None,
            "notes": None,
        }
        if session_type == "gym":
            cat = _gym_category_for_day(weekday, header_line)
            gym = _parse_gym_block(body, cat)
            if gym:
                day["gym"] = gym
            else:
                day["session_type"] = "rest"
        elif session_type in ("erg", "on_water"):
            segments: List[Dict[str, Any]] = []
            erg_alt_segments: List[Dict[str, Any]] = []
            main_set_header = _main_set_header_spec(body)
            in_alt = False
            for line in body.splitlines():
                stripped = line.strip()
                if not stripped or stripped == "---":
                    continue
                if stripped.lower().startswith("erg alternative"):
                    in_alt = True
                    continue
                athlete_seg = _parse_athlete_rowing_line(stripped)
                if athlete_seg:
                    if not in_alt:
                        _apply_main_set_header_rest(athlete_seg, main_set_header)
                    (erg_alt_segments if in_alt else segments).append(athlete_seg)
                    continue
                render_seg = _parse_render_segment(stripped)
                if render_seg:
                    (erg_alt_segments if in_alt else segments).append(render_seg)
                    continue
                seg_line = _SEGMENT_LINE_RE.match(stripped)
                if seg_line:
                    seg = _parse_rowing_segment_line(seg_line.group(1), seg_line.group(2))
                    if seg:
                        (erg_alt_segments if in_alt else segments).append(seg)
            if segments:
                rowing: Dict[str, Any] = {"segments": segments, "erg_alternative": None}
                if session_type == "on_water" and erg_alt_segments:
                    rowing["erg_alternative"] = {
                        "description": "Group erg fallback",
                        "segments": erg_alt_segments,
                    }
                elif session_type == "on_water" and not erg_alt_segments:
                    rowing["erg_alternative"] = {
                        "description": "Group erg fallback",
                        "segments": segments,
                    }
                day["rowing"] = rowing
            else:
                day["session_type"] = "rest"
        days_by_name[weekday] = day

    # Expand Fri-Sun combined rest
    if "Friday" in days_by_name and "Saturday" not in days_by_name:
        for wd in ("Saturday", "Sunday"):
            days_by_name.setdefault(
                wd,
                {
                    "weekday": wd,
                    "date": day_dates[wd],
                    "session_type": "rest",
                    "session_subtype": None,
                    "gym": None,
                    "rowing": None,
                    "notes": None,
                },
            )

    days: List[Dict[str, Any]] = []
    for wd in WEEKDAYS:
        if wd in days_by_name:
            days.append(days_by_name[wd])
        else:
            days.append(
                {
                    "weekday": wd,
                    "date": day_dates[wd],
                    "session_type": "rest",
                    "session_subtype": None,
                    "gym": None,
                    "rowing": None,
                    "notes": None,
                }
            )

    candidate = {
        "version": 1,
        "personalised": personalised,
        "greeting": greeting,
        "days": days,
    }
    return candidate if parse_weekly_plan(candidate) is not None else None
