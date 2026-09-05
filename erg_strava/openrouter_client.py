"""OpenRouter chat completions client for coach bot and training pipeline."""

from __future__ import annotations

import base64
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence, TypedDict

import requests

# Auto Router: https://openrouter.ai/docs/guides/routing/routers/auto-router
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
# OpenRouter selects a model per prompt from live task-spend rankings.
DEFAULT_OPENROUTER_MODEL = "openrouter/auto"
DEFAULT_OPENROUTER_VISION_MODEL = "openrouter/auto"
DEFAULT_OPENROUTER_STRUCTURED_MODEL = "openrouter/auto"
# Cost band for openrouter/auto: low, medium, high, xhigh, max (default matches
# the previous cheap pinned models).
DEFAULT_OPENROUTER_COST_TIER = "low"
_AUTO_MODELS = {"openrouter/auto": "auto-router", "openrouter/auto-beta": "auto-beta-router"}
_COST_TIERS = {"low", "medium", "high", "xhigh", "max"}
DEFAULT_HTTP_REFERER = "https://example.com"
DEFAULT_APP_TITLE = "rowing-coach-bot"


class ChatMessage(TypedDict):
    role: str
    content: str


def openrouter_model() -> str:
    return (
        os.environ.get("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL).strip()
        or DEFAULT_OPENROUTER_MODEL
    )


def openrouter_vision_model() -> str:
    return (
        os.environ.get(
            "OPENROUTER_VISION_MODEL", DEFAULT_OPENROUTER_VISION_MODEL
        ).strip()
        or DEFAULT_OPENROUTER_VISION_MODEL
    )


def openrouter_structured_model() -> str:
    """Model for json_schema response_format (needs structured-output support)."""
    return (
        os.environ.get("OPENROUTER_STRUCTURED_MODEL", "").strip()
        or DEFAULT_OPENROUTER_STRUCTURED_MODEL
    )


def openrouter_cost_tier() -> str:
    """Auto Router cost band; invalid values fall back to the default."""
    raw = os.environ.get("OPENROUTER_COST_TIER", DEFAULT_OPENROUTER_COST_TIER).strip().lower()
    return raw if raw in _COST_TIERS else DEFAULT_OPENROUTER_COST_TIER


def _auto_router_plugin(model: str) -> Optional[Dict[str, Any]]:
    plugin_id = _AUTO_MODELS.get((model or "").strip())
    if not plugin_id:
        return None
    return {"id": plugin_id, "cost_tier": openrouter_cost_tier()}


def is_openrouter_error(text: str) -> bool:
    stripped = (text or "").strip()
    return stripped.startswith(
        ("OpenRouter API request failed", "OpenRouter API error")
    )


def image_to_data_uri(image_bytes: bytes, mime_type: str = "image/png") -> str:
    """Encode raw image bytes as a base64 ``data:`` URI for the vision payload."""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def topic_context_to_history(topic_context: str) -> List[ChatMessage]:
    """Turn formatted Zulip topic lines into chat history (user/assistant)."""
    if not topic_context or not topic_context.strip():
        return []
    history: List[ChatMessage] = []
    for line in topic_context.strip().splitlines():
        text = line.strip()
        if not text:
            continue
        role = "assistant" if re.search(r"\bcoach\b", text, re.I) else "user"
        history.append({"role": role, "content": text})
    return history


def _post_chat(
    *,
    messages: List[Dict[str, Any]],
    api_key: str,
    model: str,
    timeout: int,
    response_format: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
) -> str:
    """POST a prepared message list to OpenRouter and return text or an error string."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER", DEFAULT_HTTP_REFERER),
        "X-Title": os.environ.get("OPENROUTER_APP_TITLE", DEFAULT_APP_TITLE),
    }
    payload: Dict[str, Any] = {"model": model, "messages": messages}
    plugin = _auto_router_plugin(model)
    if plugin is not None:
        payload["plugins"] = [plugin]
    session = (session_id or "").strip()
    if session:
        payload["session_id"] = session
    if response_format is not None:
        payload["response_format"] = response_format
        if response_format.get("type") == "json_schema":
            # Only route to providers that support strict json_schema.
            payload["provider"] = {"require_parameters": True}
    retryable_status = {404, 408, 429, 500, 502, 503, 504}
    try:
        response = None
        for attempt in range(2):
            response = requests.post(
                OPENROUTER_CHAT_URL,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            if response.ok or response.status_code not in retryable_status or attempt:
                break
            time.sleep(2.0)
        assert response is not None
        if not response.ok:
            try:
                err_body = response.json()
                err = err_body.get("error") or {}
                msg = err.get("message") or response.text
                code = err.get("code")
                detail = f" ({code})" if code else ""
                return (
                    f"OpenRouter API request failed: HTTP {response.status_code}"
                    f"{detail}: {msg}"
                )
            except (ValueError, TypeError):
                return (
                    f"OpenRouter API request failed: HTTP {response.status_code}: "
                    f"{response.text}"
                )
        body = response.json()
        choices = body.get("choices") or []
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            content = message.get("content")
            if content is not None:
                return str(content)
        err = body.get("error") or {}
        if err:
            return f"OpenRouter API error: {err.get('message', err)}"
        return f"OpenRouter API returned unexpected JSON: {body!r}"
    except requests.RequestException as exc:
        return f"OpenRouter API request failed: {exc}"


def call_openrouter(
    *,
    system: str,
    user: str,
    api_key: str,
    conversation_history: Optional[Sequence[ChatMessage]] = None,
    model: Optional[str] = None,
    timeout: int = 60,
    response_format: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
) -> str:
    """
    POST a text-only completion to https://openrouter.ai/api/v1/chat/completions

    Default model: openrouter/auto — OpenRouter picks a model for the prompt.
    Pin a specific slug via OPENROUTER_MODEL, or change the cost band with
    OPENROUTER_COST_TIER.
    """
    model = model or openrouter_model()
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system.strip()}]
    if conversation_history:
        for msg in conversation_history:
            role = str(msg.get("role") or "").strip()
            content = str(msg.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user.strip()})
    return _post_chat(
        messages=messages,
        api_key=api_key,
        model=model,
        timeout=timeout,
        response_format=response_format,
        session_id=session_id,
    )


def call_openrouter_vision(
    *,
    system: str,
    user: str,
    images: Sequence[str],
    api_key: str,
    model: Optional[str] = None,
    timeout: int = 120,
    session_id: Optional[str] = None,
) -> str:
    """
    POST a multimodal (text + image) completion to OpenRouter.

    ``images`` are passed straight through as ``image_url`` parts, so each entry
    must be either a base64 ``data:`` URI (see :func:`image_to_data_uri`) or a
    publicly reachable https URL.

    Default model: openrouter/auto (vision-capable models are selected when the
    prompt includes images). Pin via OPENROUTER_VISION_MODEL.
    """
    model = model or openrouter_vision_model()
    content: List[Dict[str, Any]] = [{"type": "text", "text": user.strip()}]
    for image in images:
        url = (image or "").strip()
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}})
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system.strip()},
        {"role": "user", "content": content},
    ]
    return _post_chat(
        messages=messages,
        api_key=api_key,
        model=model,
        timeout=timeout,
        session_id=session_id,
    )
