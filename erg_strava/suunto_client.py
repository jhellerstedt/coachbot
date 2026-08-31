"""Programmatic suuntool CLI client (same backend as ``suuntool mcp``)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

DEFAULT_INDOOR_ROWING_ACTIVITY_IDS = frozenset({57})
DEFAULT_GYM_ACTIVITY_IDS = frozenset({23})
_LIST_SINCE_DAYS = 120


@dataclass
class SuuntoCfg:
    enabled: bool
    primary: bool
    suuntool_path: Optional[Path]
    session_file: Optional[Path]
    indoor_rowing_activity_ids: frozenset
    gym_activity_ids: frozenset
    list_since_days: int = _LIST_SINCE_DAYS
    athlete_ids: Optional[frozenset] = None


def load_suunto_cfg(raw: Mapping[str, Any], base: Path) -> SuuntoCfg:
    sc = raw.get("suunto") or {}
    tool_raw = sc.get("suuntool_path")
    tool_path = Path(str(tool_raw)) if tool_raw else None
    session_raw = sc.get("session_file")
    session_path = Path(str(session_raw)) if session_raw else None
    ids = sc.get("indoor_rowing_activity_ids", list(DEFAULT_INDOOR_ROWING_ACTIVITY_IDS))
    gym_ids = sc.get("gym_activity_ids", list(DEFAULT_GYM_ACTIVITY_IDS))
    raw_athlete_ids = sc.get("athlete_ids")
    athlete_ids = (
        frozenset(int(x) for x in raw_athlete_ids) if raw_athlete_ids else None
    )
    return SuuntoCfg(
        enabled=bool(sc.get("enabled", True)),
        primary=bool(sc.get("primary", True)),
        suuntool_path=tool_path,
        session_file=session_path,
        indoor_rowing_activity_ids=frozenset(int(x) for x in ids),
        gym_activity_ids=frozenset(int(x) for x in gym_ids),
        list_since_days=int(sc.get("list_since_days", _LIST_SINCE_DAYS)),
        athlete_ids=athlete_ids,
    )


def suunto_sync_enabled_for_athlete(cfg: SuuntoCfg, athlete_id: int) -> bool:
    if not cfg.enabled:
        return False
    if cfg.athlete_ids is None:
        return True
    return athlete_id in cfg.athlete_ids


def resolve_suuntool_binary(cfg: SuuntoCfg, base: Path) -> Path:
    candidates: list[Path] = []
    configured: Path | None = None

    if cfg.suuntool_path:
        configured = (
            cfg.suuntool_path
            if cfg.suuntool_path.is_absolute()
            else (base / cfg.suuntool_path)
        )
        configured = configured.resolve()
        candidates.append(configured)

    candidates.extend(
        [
            base / "bin" / "suuntool",
            base.parent / "bin" / "suuntool",
            base.parent.parent / "RRC-scripts" / "bin" / "suuntool",
            Path.home() / "RRC-scripts" / "bin" / "suuntool",
        ]
    )
    which_path = shutil.which("suuntool")
    if which_path:
        candidates.append(Path(which_path))

    for candidate in candidates:
        if candidate and candidate.is_file():
            found = candidate.resolve()
            if configured is not None and not configured.is_file() and found != configured:
                print(
                    f"suuntool: configured path missing ({configured}); using {found}",
                    file=sys.stderr,
                )
            return found

    raise FileNotFoundError(
        "suuntool binary not found. Install via "
        "'brew install tajchert/tap/suuntool' or place a release binary in erg_strava/../bin/suuntool"
    )


class SuuntoClient:
    """Thin wrapper around suuntool subprocess calls."""

    def __init__(self, cfg: SuuntoCfg, config_base: Path) -> None:
        self.cfg = cfg
        self.config_base = config_base.resolve()
        self._binary = resolve_suuntool_binary(cfg, self.config_base)

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.cfg.session_file:
            sf = (
                self.cfg.session_file
                if self.cfg.session_file.is_absolute()
                else (self.config_base / self.cfg.session_file)
            )
            env["SUUNTOOL_SESSION_FILE"] = str(sf.resolve())
        return env

    def run(
        self,
        args: Sequence[str],
        *,
        output_path: Optional[Path] = None,
        timeout: float = 180.0,
    ) -> Any:
        cmd = [str(self._binary), *args]
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                [*cmd, "-o", str(output_path)],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._env(),
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    proc.stderr.strip()
                    or proc.stdout.strip()
                    or f"suuntool exit {proc.returncode}"
                )
            if output_path.suffix == ".json" and output_path.is_file():
                return json.loads(output_path.read_text(encoding="utf-8"))
            return output_path.read_bytes()

        proc = subprocess.run(
            [*cmd, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._env(),
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            raise RuntimeError(err or f"suuntool exit {proc.returncode}")
        if not proc.stdout.strip():
            return {}
        return json.loads(proc.stdout)

    def whoami(self) -> Mapping[str, Any]:
        result = self.run(["whoami"])
        return result if isinstance(result, dict) else {}

    def list_workouts(self, since_days: int, limit: int = 100) -> list[dict]:
        since_days = max(7, int(since_days))
        listed = self.run(
            ["workouts", "list", "--since", f"{since_days}d", "--limit", str(limit)]
        )
        items = listed.get("items") if isinstance(listed, dict) else listed
        if not isinstance(items, list):
            return []
        return [x for x in items if isinstance(x, dict)]

    def get_workout(self, key: str) -> dict:
        detail = self.run(["workouts", "get", key])
        return detail if isinstance(detail, dict) else {}

    def download_fit(self, key: str, output_path: Path) -> None:
        self.run(["workouts", "fit", key], output_path=output_path)
