# -*- coding: utf-8 -*-
"""HTTP transport for Strava API v3 (Bearer via stravalib / requests)."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional, Protocol

from requests.exceptions import (
    ChunkedEncodingError,
    ConnectionError,
    RequestException,
    SSLError,
    Timeout,
)

from strava_token_client import close_strava_client

# Strava returns 429 under heavy sync (photos + streams); peers may RST during back-off.
_GET_MAX_ATTEMPTS = 10
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# POST /oauth/token uses stravalib's raw session (not our GET loop), so retry separately.
_REFRESH_MAX_ATTEMPTS = 6
_NETWORK_REQUEST_ERRORS = (
    ChunkedEncodingError,
    ConnectionError,
    Timeout,
    SSLError,
)

if TYPE_CHECKING:
    from stravalib import Client


@dataclass
class StravaHttpResponse:
    status_code: int
    headers: Dict[str, str]
    content: bytes

    def json(self) -> Any:
        return json.loads(self.content.decode())


class StravaTransport(Protocol):
    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 120,
    ) -> StravaHttpResponse: ...

    def close(self) -> None: ...


def _refresh_expired_token_with_retries(p: Any) -> None:
    """Retry OAuth refresh; stravalib uses ``rsession`` POST without our GET retry loop."""
    for k in range(_REFRESH_MAX_ATTEMPTS):
        try:
            p.refresh_expired_token()
            return
        except RequestException as e:
            if not isinstance(e, _NETWORK_REQUEST_ERRORS):
                raise
            if k >= _REFRESH_MAX_ATTEMPTS - 1:
                raise
            time.sleep(min(120.0, 3.0 * (2**k)))


def _retry_sleep_after_response(
    status: int, headers: Dict[str, str], attempt: int
) -> float:
    if status == 429:
        ra = headers.get("Retry-After") or headers.get("retry-after")
        if ra:
            try:
                return float(min(900.0, max(15.0, float(ra))))
            except ValueError:
                pass
        return min(900.0, 45.0 * (attempt + 1))
    return min(120.0, 3.0 * (2**attempt))


class StravalibStravaTransport:
    """
    Strava v3 GETs using :class:`stravalib.Client` (token refresh + rate limiter).

    Uses ``Authorization: Bearer`` like the REST docs. Does not use stravalib's
    JSON-only ``protocol._request`` so binary responses (e.g. FIT export) work.
    """

    def __init__(self, client: Client) -> None:
        self._c = client

    def get(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 120,
    ) -> StravaHttpResponse:
        p = self._c.protocol
        resolved = url if url.startswith("http") else p.resolve_url(url)
        params = params or {}
        last_network_error: Optional[BaseException] = None

        for attempt in range(_GET_MAX_ATTEMPTS):
            if "/oauth/token" not in url and p.client_id and p.client_secret:
                _refresh_expired_token_with_retries(p)
            token = p.access_token
            if not token:
                raise RuntimeError("Strava access_token missing on client")

            try:
                raw = p.rsession.get(
                    resolved,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=timeout,
                )
            except _NETWORK_REQUEST_ERRORS as e:
                last_network_error = e
                if attempt >= _GET_MAX_ATTEMPTS - 1:
                    raise
                time.sleep(min(120.0, 2.0**attempt))

            else:
                p.rate_limiter(raw.headers, "GET")
                hdrs = {k.lower(): str(v) for k, v in raw.headers.items()}
                st = raw.status_code
                if st in _TRANSIENT_STATUS and attempt < _GET_MAX_ATTEMPTS - 1:
                    time.sleep(
                        _retry_sleep_after_response(st, hdrs, attempt),
                    )
                    continue
                return StravaHttpResponse(st, hdrs, raw.content)

        if last_network_error is not None:
            raise last_network_error
        raise RuntimeError("Strava GET retry loop exhausted")

    def close(self) -> None:
        close_strava_client(self._c)
