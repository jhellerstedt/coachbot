#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Post messages, images, and text files to a Zulip stream/topic.

Credentials are read from a Zulip rc file (default: ``rrcc-zuliprc`` in the
repo root) with the standard layout::

    [api]
    email=bot@example.com
    key=API_KEY
    site=https://example.zulipchat.com
"""

from __future__ import annotations

import configparser
import os
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

import requests


DEFAULT_STREAM = "general"
DEFAULT_TOPIC = "project-640"

# Zulip's server default MAX_MESSAGE_LENGTH is 10000 chars; leave headroom for
# Markdown wrappers (file link, optional caption) we add on top of the body.
ZULIP_MAX_MESSAGE_LENGTH = 9500


def default_zuliprc_path() -> Path:
    """Locate ``rrcc-zuliprc`` next to the repo root (parent of ``lighties/``)."""
    return Path(__file__).resolve().parent.parent / "rrcc-zuliprc"


def load_zuliprc(path: Optional[Path] = None) -> dict:
    """Parse a ``zuliprc``-style INI file into a credentials dict."""
    rc_path = Path(path) if path else default_zuliprc_path()
    if not rc_path.is_file():
        raise FileNotFoundError(f"Zulip rc file not found: {rc_path}")
    cp = configparser.ConfigParser()
    cp.read(rc_path)
    if "api" not in cp:
        raise ValueError(f"Missing [api] section in {rc_path}")
    section = cp["api"]
    missing = [k for k in ("email", "key", "site") if k not in section]
    if missing:
        raise ValueError(f"{rc_path} missing keys in [api]: {', '.join(missing)}")
    return {
        "email": section["email"].strip(),
        "key": section["key"].strip(),
        "site": section["site"].strip().rstrip("/"),
    }


def _api_url(creds: dict, route: str) -> str:
    return f"{creds['site']}/api/v1/{route.lstrip('/')}"


def _auth(creds: dict) -> Tuple[str, str]:
    return (creds["email"], creds["key"])


def _absolute_upload_url(creds: dict, payload: dict) -> str:
    """Resolve the upload response into an absolute URL on the Zulip site."""
    uri = payload.get("url") or payload.get("uri")
    if not uri:
        raise RuntimeError(f"Zulip upload returned no url: {payload}")
    if uri.startswith("http://") or uri.startswith("https://"):
        return uri
    return f"{creds['site']}{uri if uri.startswith('/') else '/' + uri}"


def upload_file_to_zulip(
    file_path: str,
    creds: Optional[dict] = None,
    zuliprc_path: Optional[Path] = None,
    upload_filename: Optional[str] = None,
) -> str:
    """Upload a file to ``/api/v1/user_uploads`` and return an absolute URL."""
    if creds is None:
        creds = load_zuliprc(zuliprc_path)
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)
    name = upload_filename or os.path.basename(file_path)
    with open(file_path, "rb") as fh:
        resp = requests.post(
            _api_url(creds, "user_uploads"),
            auth=_auth(creds),
            files={"file": (name, fh)},
            timeout=120,
        )
    resp.raise_for_status()
    data = resp.json()
    if data.get("result") != "success":
        raise RuntimeError(f"Zulip upload failed: {data}")
    return _absolute_upload_url(creds, data)


def send_private_message_to_zulip(
    content: str,
    to: Sequence[Union[int, str]],
    creds: Optional[dict] = None,
    zuliprc_path: Optional[Path] = None,
) -> dict:
    """Send a private message to one or more Zulip users (id or email)."""
    if creds is None:
        creds = load_zuliprc(zuliprc_path)
    if not to:
        raise ValueError("private message requires at least one recipient")
    import json

    resp = requests.post(
        _api_url(creds, "messages"),
        auth=_auth(creds),
        data={
            "type": "private",
            "to": json.dumps(list(to)),
            "content": content,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("result") != "success":
        raise RuntimeError(f"Zulip private message failed: {data}")
    return data


def send_message_to_zulip(
    content: str,
    stream: str = DEFAULT_STREAM,
    topic: str = DEFAULT_TOPIC,
    creds: Optional[dict] = None,
    zuliprc_path: Optional[Path] = None,
) -> dict:
    """Post a stream message to Zulip; return the parsed JSON response."""
    if creds is None:
        creds = load_zuliprc(zuliprc_path)
    resp = requests.post(
        _api_url(creds, "messages"),
        auth=_auth(creds),
        data={
            "type": "stream",
            "to": stream,
            "topic": topic,
            "content": content,
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("result") != "success":
        raise RuntimeError(f"Zulip message failed: {data}")
    return data


def send_png_to_zulip(
    file_path: str,
    stream: str = DEFAULT_STREAM,
    topic: str = DEFAULT_TOPIC,
    initial_comment: Optional[str] = None,
    title: Optional[str] = None,
    zuliprc_path: Optional[Path] = None,
) -> bool:
    """Upload a PNG (or any image) to Zulip and post a message linking it."""
    try:
        creds = load_zuliprc(zuliprc_path)
        url = upload_file_to_zulip(file_path, creds=creds)
        link_text = title or os.path.basename(file_path)
        body_parts = []
        if initial_comment:
            body_parts.append(initial_comment.rstrip())
        body_parts.append(f"[{link_text}]({url})")
        send_message_to_zulip(
            "\n\n".join(body_parts), stream=stream, topic=topic, creds=creds
        )
        print(
            f"PNG file '{os.path.basename(file_path)}' sent to Zulip "
            f"{stream}/{topic}."
        )
        return True
    except Exception as e:
        print(f"Error sending PNG to Zulip: {e}")
        return False


def send_text_to_zulip(
    text: str,
    stream: str = DEFAULT_STREAM,
    topic: str = DEFAULT_TOPIC,
    filename: str = "message.txt",
    initial_comment: Optional[str] = None,
    title: Optional[str] = None,
    zuliprc_path: Optional[Path] = None,
) -> bool:
    """Send text to a Zulip stream/topic.

    Short text is sent inline as a stream message. Anything longer than
    :data:`ZULIP_MAX_MESSAGE_LENGTH` is uploaded as a file and referenced from
    a short message (mirrors the behaviour of the previous Slack helper).
    """
    try:
        creds = load_zuliprc(zuliprc_path)
        header = (initial_comment or title or "").rstrip()
        inline_body = f"{header}\n\n{text}" if header else text
        if len(inline_body) <= ZULIP_MAX_MESSAGE_LENGTH:
            send_message_to_zulip(
                inline_body, stream=stream, topic=topic, creds=creds
            )
        else:
            suffix = os.path.splitext(filename)[1] or ".txt"
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=suffix, delete=False, encoding="utf-8"
            ) as f:
                f.write(text)
                tmp_path = f.name
            try:
                url = upload_file_to_zulip(
                    tmp_path, creds=creds, upload_filename=filename
                )
            finally:
                os.unlink(tmp_path)
            link_text = title or filename
            body_parts = []
            if initial_comment:
                body_parts.append(initial_comment.rstrip())
            body_parts.append(f"[{link_text}]({url})")
            send_message_to_zulip(
                "\n\n".join(body_parts), stream=stream, topic=topic, creds=creds
            )
        print(f"Text sent to Zulip {stream}/{topic}.")
        return True
    except Exception as e:
        print(f"Error sending text to Zulip: {e}")
        return False
