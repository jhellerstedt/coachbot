"""OpenRouter chat completions client for coach bot and training pipeline."""

from __future__ import annotations

import base64
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence, TypedDict

import requests

# Model catalog: https://openrouter.ai/models
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
# Low-cost text-only model (e.g. meta-llama/llama-3-8b-instruct, openai/gpt-4o-mini).
DEFAULT_OPENROUTER_MODEL = "anthropic/claude-3.5-haiku"
# High-performance multimodal model for vision (image) inputs.
DEFAULT_OPENROUTER_VISION_MODEL = "google/gemini-2.5-flash"
# Reliable json_schema model for weekly plan structured output.
DEFAULT_OPENROUTER_STRUCTURED_MODEL = "openai/gpt-4o-mini"
DEFAULT_HTTP_REFERER = "https://rrcc.imipolex.biz"
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
) -> str:
    """POST a prepared message list to OpenRouter and return text or an error string."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.environ.get("OPENROUTER_HTTP_REFERER", DEFAULT_HTTP_REFERER),
        "X-Title": os.environ.get("OPENROUTER_APP_TITLE", DEFAULT_APP_TITLE),
    }
    payload: Dict[str, Any] = {"model": model, "messages": messages}
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
) -> str:
    """
    POST a text-only completion to https://openrouter.ai/api/v1/chat/completions

    Default model: anthropic/claude-3.5-haiku — change via OPENROUTER_MODEL or
    pick another at https://openrouter.ai/models
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
    )


def call_openrouter_vision(
    *,
    system: str,
    user: str,
    images: Sequence[str],
    api_key: str,
    model: Optional[str] = None,
    timeout: int = 120,
) -> str:
    """
    POST a multimodal (text + image) completion to OpenRouter.

    ``images`` are passed straight through as ``image_url`` parts, so each entry
    must be either a base64 ``data:`` URI (see :func:`image_to_data_uri`) or a
    publicly reachable https URL.

    Default model: the high-performance vision model from OPENROUTER_VISION_MODEL
    (e.g. google/gemini-flash-1.5, anthropic/claude-3.5-sonnet).
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
        messages=messages, api_key=api_key, model=model, timeout=timeout
    )
