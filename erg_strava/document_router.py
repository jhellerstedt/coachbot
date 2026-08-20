"""Cost/accuracy-aware conditional router for document (text + image) inputs.

The router inspects an input for an image attachment and dispatches to one of two
OpenRouter branches:

- **text-only** (no image): a cheap, low-cost LLM (``OPENROUTER_MODEL``) handles
  the text. This branch stays deliberately lightweight — no image encoding, no
  multimodal payload, no OCR.
- **vision** (image present): the image (plus any text context) goes straight to
  a high-performance multimodal model (``OPENROUTER_VISION_MODEL``). There is no
  local OCR; the MLLM does the visual reasoning.

This module is framework-agnostic: it knows nothing about Zulip, the coach bot,
or any persistence layer. Callers build an :class:`InputData`, choose system
prompts, and consume the returned :class:`RouterResult`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional, Sequence

from openrouter_client import (
    ChatMessage,
    call_openrouter,
    call_openrouter_vision,
    image_to_data_uri,
    openrouter_model,
    openrouter_vision_model,
)

# System prompt for the vision branch when the image is a Concept2 erg screenshot.
# Kept here as a ready-to-use default; callers may pass any prompt instead.
ERG_SCREENSHOT_SYSTEM_PROMPT = (
    "You are an expert sports data analyst. Extract data from the provided "
    "Concept2 ergometer screenshot.\n"
    "- Output the data in the following format:\n"
    "  @**coach** erg log YYYY-MM-DD\n"
    "  [Total Time] work, [Total Distance] m total, avg split [Split], "
    "stroke rate [Rate] spm, [Intensity/Notes]\n"
    "  Intervals ([Interval Duration] each):\n"
    "  1) [Dist] m, [Split], [Rate] spm\n"
    "  ...\n"
    "- If the image is blurry or data is missing, return a clear error message "
    "instead of hallucinating values."
)

DEFAULT_TEXT_SYSTEM_PROMPT = (
    "You are a helpful assistant. Answer the user's message concisely and "
    "accurately."
)

TEXT_BRANCH = "text"
VISION_BRANCH = "vision"


@dataclass
class InputData:
    """A unit of work for the router: text content and an optional image.

    Provide at most one image source. ``image_bytes`` is base64-encoded into a
    ``data:`` URI; ``image_url`` is passed through to the MLLM as-is (it must be
    publicly reachable).
    """

    text: str = ""
    image_bytes: Optional[bytes] = None
    image_url: Optional[str] = None
    image_mime: str = "image/png"

    def has_image(self) -> bool:
        return bool(self.image_bytes) or bool((self.image_url or "").strip())

    def image_payload(self) -> List[str]:
        """Image references for the vision payload (base64 data URI or URL)."""
        images: List[str] = []
        if self.image_bytes:
            images.append(image_to_data_uri(self.image_bytes, self.image_mime))
        url = (self.image_url or "").strip()
        if url:
            images.append(url)
        return images


@dataclass
class RouterResult:
    """Outcome of a routed call."""

    response: str
    branch: str
    model: str


def _resolve_api_key(api_key: Optional[str]) -> str:
    key = (api_key or os.environ.get("OPENROUTER_API_KEY", "")).strip()
    if not key:
        raise ValueError(
            "OpenRouter API key required: pass api_key or set OPENROUTER_API_KEY."
        )
    return key


def process_input(
    input_data: InputData,
    *,
    api_key: Optional[str] = None,
    text_system_prompt: str = DEFAULT_TEXT_SYSTEM_PROMPT,
    vision_system_prompt: str = ERG_SCREENSHOT_SYSTEM_PROMPT,
    text_model: Optional[str] = None,
    vision_model: Optional[str] = None,
    conversation_history: Optional[Sequence[ChatMessage]] = None,
    text_timeout: int = 60,
    vision_timeout: int = 120,
) -> RouterResult:
    """Route ``input_data`` to the cheap text LLM or the vision MLLM.

    The branch is chosen purely on image presence. The two branches share no
    payload-building code, keeping the text-only path as lightweight as possible.
    """
    key = _resolve_api_key(api_key)
    if input_data.has_image():
        return _route_vision(
            input_data,
            api_key=key,
            system_prompt=vision_system_prompt,
            model=vision_model,
            timeout=vision_timeout,
        )
    return _route_text(
        input_data,
        api_key=key,
        system_prompt=text_system_prompt,
        model=text_model,
        conversation_history=conversation_history,
        timeout=text_timeout,
    )


def _route_text(
    input_data: InputData,
    *,
    api_key: str,
    system_prompt: str,
    model: Optional[str],
    conversation_history: Optional[Sequence[ChatMessage]],
    timeout: int,
) -> RouterResult:
    chosen = model or openrouter_model()
    response = call_openrouter(
        system=system_prompt,
        user=input_data.text,
        api_key=api_key,
        conversation_history=conversation_history,
        model=chosen,
        timeout=timeout,
    )
    return RouterResult(response=response, branch=TEXT_BRANCH, model=chosen)


def _route_vision(
    input_data: InputData,
    *,
    api_key: str,
    system_prompt: str,
    model: Optional[str],
    timeout: int,
) -> RouterResult:
    chosen = model or openrouter_vision_model()
    response = call_openrouter_vision(
        system=system_prompt,
        user=input_data.text,
        images=input_data.image_payload(),
        api_key=api_key,
        model=chosen,
        timeout=timeout,
    )
    return RouterResult(response=response, branch=VISION_BRANCH, model=chosen)
