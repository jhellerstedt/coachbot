"""Per-athlete physiology settings for personalised HR zone targets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

# 5-zone HR model (Z1–Z5) and 7-zone rowing T-level model (T1–T7), both as
# fractions of athlete max HR. Defaults drive per-segment split/HR targets in
# generated training plans when max_hr_bpm is set.

FIVE_ZONE_ORDER: Tuple[str, ...] = ("z1", "z2", "z3", "z4", "z5")

DEFAULT_FIVE_ZONE_PCT: Dict[str, Tuple[float, float]] = {
    "z1": (0.50, 0.60),
    "z2": (0.60, 0.70),
    "z3": (0.70, 0.80),
    "z4": (0.80, 0.90),
    "z5": (0.90, 1.00),
}

FIVE_ZONE_LABELS: Dict[str, str] = {
    "z1": "recovery",
    "z2": "aerobic base",
    "z3": "tempo / aerobic",
    "z4": "threshold",
    "z5": "VO2max / race pace",
}

SEVEN_ZONE_ORDER: Tuple[str, ...] = ("t1", "t2", "t3", "t4", "t5", "t6", "t7")

DEFAULT_SEVEN_ZONE_PCT: Dict[str, Tuple[float, float]] = {
    "t1": (0.50, 0.60),
    "t2": (0.60, 0.68),
    "t3": (0.68, 0.75),
    "t4": (0.75, 0.82),
    "t5": (0.82, 0.88),
    "t6": (0.88, 0.94),
    "t7": (0.94, 1.00),
}

SEVEN_ZONE_LABELS: Dict[str, str] = {
    "t1": "recovery",
    "t2": "UT3 / extensive recovery",
    "t3": "UT2-low",
    "t4": "UT2 / steady state",
    "t5": "UT1 / extensive aerobic",
    "t6": "AT / threshold",
    "t7": "TR–AN / race pace",
}

# Backward-compatible aliases (T1–T5 configs and imports).
TRAINING_ZONE_ORDER: Tuple[str, ...] = SEVEN_ZONE_ORDER
DEFAULT_TRAINING_ZONE_PCT: Dict[str, Tuple[float, float]] = DEFAULT_SEVEN_ZONE_PCT
TRAINING_ZONE_LABELS: Dict[str, str] = SEVEN_ZONE_LABELS


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _pct_range(raw: Any, default: Tuple[float, float]) -> Tuple[float, float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return default
    try:
        lo, hi = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return default
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _five_zone_pct_from_config(zones: Mapping[str, Any]) -> Dict[str, Tuple[float, float]]:
    """Merge optional per-athlete z1..z5 overrides over default zone bands."""
    out = dict(DEFAULT_FIVE_ZONE_PCT)
    for key in FIVE_ZONE_ORDER:
        if key in zones:
            out[key] = _pct_range(zones.get(key), out[key])
    return out


def _seven_zone_pct_from_config(zones: Mapping[str, Any]) -> Dict[str, Tuple[float, float]]:
    """Merge optional per-athlete t1..t7 overrides over the default zone bands."""
    out = dict(DEFAULT_SEVEN_ZONE_PCT)
    for key in SEVEN_ZONE_ORDER:
        if key in zones:
            out[key] = _pct_range(zones.get(key), out[key])
    return out


def _training_zone_pct_from_config(
    zones: Mapping[str, Any],
) -> Dict[str, Tuple[float, float]]:
    return _seven_zone_pct_from_config(zones)


@dataclass(frozen=True)
class AthleteProfile:
    id: int
    label: str
    body_weight_kg: Optional[float] = None
    max_hr_bpm: Optional[int] = None
    zulip_email: Optional[str] = None
    zulip_user_id: Optional[int] = None
    hr_z2_pct: Tuple[float, float] = (0.60, 0.75)
    hr_z5_pct: Tuple[float, float] = (0.90, 1.00)
    five_zone_pct: Mapping[str, Tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_FIVE_ZONE_PCT)
    )
    seven_zone_pct: Mapping[str, Tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_SEVEN_ZONE_PCT)
    )

    @property
    def training_zone_pct(self) -> Mapping[str, Tuple[float, float]]:
        """Alias for seven_zone_pct (backward compatible)."""
        return self.seven_zone_pct

    def zone_bpm_range(self, zone: str) -> Optional[Tuple[int, int]]:
        """HR bpm range for a zone key (``z1``..``z5``, ``t1``..``t7``, or legacy ``z2``/``z5``)."""
        if self.max_hr_bpm is None:
            return None
        key = zone.strip().lower()
        if key == "z2":
            lo_pct, hi_pct = self.hr_z2_pct
        elif key == "z5":
            lo_pct, hi_pct = self.hr_z5_pct
        elif key in self.five_zone_pct:
            lo_pct, hi_pct = self.five_zone_pct[key]
        elif key in self.seven_zone_pct:
            lo_pct, hi_pct = self.seven_zone_pct[key]
        else:
            return None
        return (
            int(round(self.max_hr_bpm * lo_pct)),
            int(round(self.max_hr_bpm * hi_pct)),
        )

    def _zones_bpm_rows(
        self,
        order: Tuple[str, ...],
        pct_map: Mapping[str, Tuple[float, float]],
        labels: Mapping[str, str],
    ) -> List[Tuple[str, str, Optional[Tuple[int, int]]]]:
        rows: List[Tuple[str, str, Optional[Tuple[int, int]]]] = []
        for key in order:
            if key not in pct_map:
                continue
            rows.append(
                (key.upper(), labels.get(key, ""), self.zone_bpm_range(key))
            )
        return rows

    def five_zones_bpm(self) -> List[Tuple[str, str, Optional[Tuple[int, int]]]]:
        """(code, label, bpm range) for Z1→Z5."""
        return self._zones_bpm_rows(FIVE_ZONE_ORDER, self.five_zone_pct, FIVE_ZONE_LABELS)

    def seven_zones_bpm(self) -> List[Tuple[str, str, Optional[Tuple[int, int]]]]:
        """(code, label, bpm range) for T1→T7."""
        return self._zones_bpm_rows(
            SEVEN_ZONE_ORDER, self.seven_zone_pct, SEVEN_ZONE_LABELS
        )

    def training_zones_bpm(self) -> List[Tuple[str, str, Optional[Tuple[int, int]]]]:
        """(code, label, bpm range) for each T-zone (alias for seven_zones_bpm)."""
        return self.seven_zones_bpm()

    def _zones_context_block(
        self,
        title: str,
        order: Tuple[str, ...],
        pct_map: Mapping[str, Tuple[float, float]],
        labels: Mapping[str, str],
    ) -> str:
        if self.max_hr_bpm is None:
            return ""
        lines = [title]
        for key in order:
            if key not in pct_map:
                continue
            rng = self.zone_bpm_range(key)
            if rng is None:
                continue
            lo_pct, hi_pct = pct_map[key]
            code = key.upper()
            label = labels.get(key, "")
            lines.append(
                f"{code} ({label}): {rng[0]}–{rng[1]} bpm "
                f"({lo_pct * 100:.0f}–{hi_pct * 100:.0f}% max HR)"
            )
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    def five_zones_context_text(self) -> str:
        """Prompt block: Z1–Z5 HR ranges derived from Max HR."""
        return self._zones_context_block(
            f"--- {self.label}: 5-zone HR (Z1–Z5 from Max HR "
            f"{self.max_hr_bpm} bpm) ---",
            FIVE_ZONE_ORDER,
            self.five_zone_pct,
            FIVE_ZONE_LABELS,
        )

    def seven_zones_context_text(self) -> str:
        """Prompt block: T1–T7 HR ranges derived from Max HR."""
        return self._zones_context_block(
            f"--- {self.label}: 7-zone T-level (T1–T7 from Max HR "
            f"{self.max_hr_bpm} bpm) ---",
            SEVEN_ZONE_ORDER,
            self.seven_zone_pct,
            SEVEN_ZONE_LABELS,
        )

    def training_zones_context_text(self) -> str:
        """Prompt block: per-segment HR ranges (T1–T7) derived from Max HR."""
        return self.seven_zones_context_text()

    def hr_zone_context_text(self) -> str:
        """Coach/plan prompt block with athlete-specific HR targets."""
        lines: List[str] = []
        if self.body_weight_kg is not None:
            lines.append(f"Body weight: {self.body_weight_kg:g} kg")
        if self.max_hr_bpm is not None:
            lines.append(f"Max HR: {self.max_hr_bpm} bpm")
        blocks: List[str] = []
        if lines:
            blocks.append(f"--- {self.label}: athlete profile ---\n" + "\n".join(lines))
        five_block = self.five_zones_context_text()
        seven_block = self.seven_zones_context_text()
        if five_block:
            blocks.append(five_block)
        if seven_block:
            blocks.append(seven_block)
        return "\n\n".join(blocks)

    def classify_hr(self, hr_bpm: float) -> Optional[str]:
        """Return 'z2' or 'z5' when HR falls in that athlete's zone band."""
        z2 = self.zone_bpm_range("z2")
        if z2 and z2[0] <= hr_bpm <= z2[1]:
            return "z2"
        z5 = self.zone_bpm_range("z5")
        if z5 and z5[0] <= hr_bpm <= z5[1]:
            return "z5"
        return None

    def classify_five_zone_hr(self, hr_bpm: float) -> Optional[str]:
        """Return z1–z5 when HR falls in that athlete's five-zone band (highest first)."""
        if self.max_hr_bpm is None:
            return None
        for key in reversed(FIVE_ZONE_ORDER):
            rng = self.zone_bpm_range(key)
            if rng and rng[0] <= hr_bpm <= rng[1]:
                return key
        return None


def athlete_profile_from_config(entry: Mapping[str, Any]) -> AthleteProfile:
    zones = entry.get("hr_zones") or {}
    five_zone_pct = _five_zone_pct_from_config(zones)
    # Legacy z2/z5 config keys override the matching five-zone bands.
    if "z2" in zones:
        five_zone_pct["z2"] = _pct_range(zones.get("z2"), five_zone_pct["z2"])
    if "z5" in zones:
        five_zone_pct["z5"] = _pct_range(zones.get("z5"), five_zone_pct["z5"])
    return AthleteProfile(
        id=int(entry["id"]),
        label=str(entry.get("label", f"athlete_{entry['id']}")),
        body_weight_kg=_optional_float(entry.get("body_weight_kg")),
        max_hr_bpm=_optional_int(entry.get("max_hr_bpm")),
        zulip_email=(
            str(entry["zulip_email"]).strip().lower()
            if entry.get("zulip_email")
            else None
        ),
        zulip_user_id=(
            _optional_int(entry.get("zulip_user_id"))
            if entry.get("zulip_user_id") is not None
            else None
        ),
        hr_z2_pct=five_zone_pct["z2"],
        hr_z5_pct=five_zone_pct["z5"],
        five_zone_pct=five_zone_pct,
        seven_zone_pct=_seven_zone_pct_from_config(zones),
    )


def load_athlete_profiles(raw: Mapping[str, Any]) -> List[AthleteProfile]:
    out: List[AthleteProfile] = []
    for entry in raw.get("athletes") or []:
        if not isinstance(entry, dict) or "id" not in entry:
            continue
        out.append(athlete_profile_from_config(entry))
    return out


def athlete_profiles_by_id(profiles: List[AthleteProfile]) -> Dict[int, AthleteProfile]:
    return {p.id: p for p in profiles}


def profile_for_label(
    profiles: List[AthleteProfile], label: str
) -> Optional[AthleteProfile]:
    for profile in profiles:
        if profile.label == label:
            return profile
    return None
