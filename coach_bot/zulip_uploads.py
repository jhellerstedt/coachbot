"""Download image uploads from Zulip messages."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlparse

import requests
import zulip

_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic")
_UPLOAD_MARKDOWN_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_BARE_UPLOAD_RE = re.compile(
    r"(https?://[^\s)>]+/user_uploads/[^\s)>]+|/user_uploads/[^\s)>]+)",
    re.I,
)


def _looks_like_image_url(url: str) -> bool:
    if "/user_uploads/" not in url:
        return False
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _IMAGE_EXTENSIONS)


def strip_upload_markdown(content: str) -> str:
    """Remove Zulip upload markdown (image/link) leaving any athlete text."""

    def _drop_if_upload(match: re.Match[str]) -> str:
        url = match.group(1)
        return "" if "/user_uploads/" in url else match.group(0)

    text = re.sub(r"!\[[^\]]*\]\(([^)]+)\)", _drop_if_upload, content or "")
    text = re.sub(r"(?<!!)\[[^\]]*\]\(([^)]+)\)", _drop_if_upload, text)
    return " ".join(text.split()).strip()


def extract_image_upload_urls(content: str) -> List[str]:
    """Return absolute or site-relative user_upload URLs for image attachments."""
    seen: set[str] = set()
    urls: List[str] = []
    for match in _UPLOAD_MARKDOWN_RE.finditer(content or ""):
        candidate = match.group(1).strip()
        if _looks_like_image_url(candidate) and candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)
    for match in _BARE_UPLOAD_RE.finditer(content or ""):
        candidate = match.group(1).strip()
        if _looks_like_image_url(candidate) and candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)
    return urls


def _zulip_site_root(client: zulip.Client) -> str:
    """Site origin (no /api suffix) from a zulip.Client."""
    base_url = str(getattr(client, "base_url", "") or "").strip()
    if base_url:
        parsed = urlparse(base_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

    for attr in ("server_url", "realm_url", "site"):
        site = str(getattr(client, attr, "") or "").strip().rstrip("/")
        if not site:
            continue
        if not site.startswith("http"):
            site = "https://" + site
        return site

    zuliprc = os.environ.get("ZULIPRC_PATH")
    try:
        from send_to_zulip import default_zuliprc_path, load_zuliprc

        creds = load_zuliprc(Path(zuliprc) if zuliprc else default_zuliprc_path())
        return creds["site"].rstrip("/")
    except Exception:
        pass

    raise RuntimeError("Zulip client has no site URL for upload download")


def _parse_user_upload_ref(url: str) -> Tuple[str, str]:
    """Return (realm_id, filename) from a /user_uploads/… path or URL."""
    path = url
    if "://" in url:
        path = urlparse(url).path
    marker = "/user_uploads/"
    idx = path.find(marker)
    if idx < 0:
        raise ValueError(f"Not a user_uploads URL: {url}")
    tail = path[idx + len(marker) :].lstrip("/")
    realm_id, _, filename = tail.partition("/")
    if not realm_id or not filename:
        raise ValueError(f"Invalid user_uploads path: {url}")
    return realm_id, filename


def _absolute_upload_url(client: zulip.Client, url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    site = _zulip_site_root(client)
    return f"{site}{url if url.startswith('/') else '/' + url}"


def _client_auth(client: zulip.Client) -> Tuple[str, str]:
    email = getattr(client, "email", None) or getattr(client, "api_email", None)
    api_key = getattr(client, "api_key", None)
    if not email or not api_key:
        raise RuntimeError("Zulip client missing email/api_key for upload download")
    return str(email), str(api_key)


def _download_via_upload_api(
    client: zulip.Client, url: str, *, timeout: int
) -> bytes:
    """GET /api/v1/user_uploads/{realm}/{file} → temporary URL → bytes."""
    realm_id, filename = _parse_user_upload_ref(url)
    result = client.call_endpoint(
        url=f"user_uploads/{realm_id}/{filename}",
        method="GET",
    )
    if result.get("result") != "success":
        raise RuntimeError(f"Zulip upload API failed: {result}")
    temp_url = str(result.get("url") or "").strip()
    if not temp_url:
        raise RuntimeError(f"Zulip upload API returned no url: {result}")
    if temp_url.startswith("/"):
        temp_url = _zulip_site_root(client) + temp_url
    elif not temp_url.startswith("http"):
        temp_url = _zulip_site_root(client) + "/" + temp_url.lstrip("/")
    resp = requests.get(temp_url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _download_via_authenticated_path(
    client: zulip.Client, url: str, *, timeout: int
) -> bytes:
    """Direct authenticated GET to /user_uploads/… on the Zulip site."""
    full_url = _absolute_upload_url(client, url)
    email, api_key = _client_auth(client)
    resp = requests.get(
        full_url,
        auth=(email, api_key),
        allow_redirects=True,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.content


def download_zulip_upload(client: zulip.Client, url: str, *, timeout: int = 60) -> bytes:
    """Fetch uploaded file bytes using the bot's Zulip credentials."""
    errors: List[str] = []
    for downloader in (_download_via_upload_api, _download_via_authenticated_path):
        try:
            return downloader(client, url, timeout=timeout)
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("; ".join(errors))
