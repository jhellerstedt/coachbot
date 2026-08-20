# -*- coding: utf-8 -*-
"""
Strava OAuth credentials on disk — same layout as ``lighties/``:

- ``strava_token`` — access token (required)
- ``strava_refresh_token`` — optional; enables refresh when paired with app credentials
- ``strava_token_expires`` — optional epoch seconds; helps stravalib refresh timing
- ``.env`` in the same directory may set ``STRAVA_CLIENT_ID`` and ``STRAVA_CLIENT_SECRET``

See ``lighties/STRAVA_SETUP.md`` and ``lighties/regenerate_strava_token.py``.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore

from stravalib import Client

_STRAVA_LOGGING_CONFIGURED = False


def configure_strava_warnings() -> None:
    """Reduce noisy Strava warnings that are not actionable here."""
    global _STRAVA_LOGGING_CONFIGURED
    if _STRAVA_LOGGING_CONFIGURED:
        return

    warnings.filterwarnings(
        "ignore",
        message=r'The "limit" parameter is deprecated.*',
        category=DeprecationWarning,
    )
    warnings.filterwarnings(
        "ignore",
        message=r'The "series_type" parameter is undocumented.*',
        category=FutureWarning,
    )

    class _StravaNoiseFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            if "Unexpected activity type" in msg:
                return False
            if "No rates present in response headers" in msg:
                return False
            return True

    noise_filter = _StravaNoiseFilter()
    for logger_name in (
        "stravalib",
        "stravalib.model",
        "stravalib.util",
        "stravalib.util.limiter",
        "stravalib.util.limiter.SleepingRateLimitRule",
    ):
        logger = logging.getLogger(logger_name)
        logger.addFilter(noise_filter)
        if "limiter" in logger_name:
            logger.setLevel(logging.ERROR)

    _STRAVA_LOGGING_CONFIGURED = True


def get_strava_client(token_dir: Optional[Path] = None) -> Client:
    """
    Build a :class:`stravalib.Client` from token files under ``token_dir``.

    ``token_dir`` defaults to this package directory (rarely useful); callers
    should pass the same path used for ``lighties`` (e.g. ``../lighties``).
    """
    configure_strava_warnings()
    base = Path(token_dir).resolve() if token_dir else Path(__file__).resolve().parent

    if load_dotenv:
        env = base / ".env"
        if env.is_file():
            load_dotenv(env)
        else:
            load_dotenv()

    token_file = base / "strava_token"
    if not token_file.is_file():
        raise FileNotFoundError(
            f"Strava token file not found: {token_file}\n"
            "Create it with your access token (same as lighties), or see "
            "lighties/STRAVA_SETUP.md"
        )

    access_token = token_file.read_text().strip()

    refresh_path = base / "strava_refresh_token"
    refresh_token = refresh_path.read_text().strip() if refresh_path.is_file() else None

    expires_path = base / "strava_token_expires"
    token_expires = None
    if expires_path.is_file():
        try:
            token_expires = int(expires_path.read_text().strip())
        except ValueError:
            pass

    # STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET come from the environment (see .env);
    # stravalib's ApiV3 reads them for refresh inside ``protocol``.
    return Client(
        access_token=access_token,
        refresh_token=refresh_token,
        token_expires=token_expires,
    )


def close_strava_client(client: Optional[Client]) -> None:
    """Close the underlying :class:`requests.Session` (``protocol.rsession``)."""
    if client is None:
        return
    proto = getattr(client, "protocol", None)
    if proto is not None:
        sess = getattr(proto, "rsession", None)
        if sess is not None and hasattr(sess, "close"):
            try:
                sess.close()
            except Exception:
                pass
            return
    for attr in ("session", "_session"):
        session = getattr(client, attr, None)
        if session is not None and hasattr(session, "close"):
            try:
                session.close()
            except Exception:
                pass
            break
