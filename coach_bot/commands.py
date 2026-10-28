"""Exact Zulip commands that produce an action and do not call a model."""

from __future__ import annotations

import threading

KDE_NOTICE = (
    "Syncing Strava and Suunto, merging erg screenshots, "
    "checking Hammerhead + SRM bike power, then posting the KDE plot…"
)
KDE_BUSY = "A KDE refresh is already running."

_KDE_LOCK = threading.Lock()

_HELP = """**Commands**
- `kde` — sync Strava and Suunto for athletes with credentials, merge each Suunto workout with an erg screenshot logged for the same activity, draw the KDE plot (erg split and watts on the top row, Hammerhead + SRM bike power and heart rate since 2025-03-07 on the bottom row), and post it to `#general > project-640`. No weekly plan."""


def match_action_command(body: str) -> str | None:
    """Return help or kde when the whole message is that command."""
    key = (body or "").strip().casefold()
    if key in ("help", "kde"):
        return key
    return None


def help_text() -> str:
    return _HELP


def try_begin_kde_refresh() -> bool:
    """True when this caller owns the single in-flight KDE refresh."""
    return _KDE_LOCK.acquire(blocking=False)


def finish_kde_refresh() -> None:
    _KDE_LOCK.release()
