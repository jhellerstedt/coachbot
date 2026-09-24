"""Tests for OpenRouter chat completions client routing."""

from __future__ import annotations

from typing import Any, Dict

import openrouter_client as client


class _FakeResponse:
    def __init__(self, payload: Dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = str(payload)

    def json(self) -> Dict[str, Any]:
        return self._payload


def _ok_payload(content: str = "ok", model: str = "anthropic/claude-sonnet-4.5") -> Dict[str, Any]:
    return {
        "id": "gen-test",
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }


def test_default_models_are_openrouter_auto(monkeypatch):
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_VISION_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_STRUCTURED_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_JEV_MODEL", raising=False)

    assert client.openrouter_model() == "openrouter/auto"
    assert client.openrouter_vision_model() == "openrouter/auto"
    assert client.openrouter_structured_model() == "openrouter/auto"
    assert client.openrouter_jev_model() == "~typesafe/jev-latest"


def test_call_openrouter_posts_auto_router_payload(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(_ok_payload())

    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_COST_TIER", raising=False)
    monkeypatch.setattr(client.requests, "post", fake_post)

    text = client.call_openrouter(
        system="You are a coach.",
        user="What is Z2?",
        api_key="sk-test",
    )

    assert text == "ok"
    assert captured["url"] == client.OPENROUTER_CHAT_URL
    payload = captured["json"]
    assert payload["model"] == "openrouter/auto"
    assert payload["plugins"] == [{"id": "auto-router", "cost_tier": "low"}]
    assert "provider" not in payload


def test_cost_tier_env_overrides_auto_router_plugin(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(_ok_payload())

    monkeypatch.setenv("OPENROUTER_COST_TIER", "medium")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.setattr(client.requests, "post", fake_post)

    client.call_openrouter(system="sys", user="hi", api_key="sk-test")

    assert captured["json"]["plugins"] == [{"id": "auto-router", "cost_tier": "medium"}]


def test_pinned_model_skips_auto_router_plugin(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(_ok_payload())

    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setattr(client.requests, "post", fake_post)

    client.call_openrouter(system="sys", user="hi", api_key="sk-test")

    assert captured["json"]["model"] == "openai/gpt-4o-mini"
    assert "plugins" not in captured["json"]


def test_json_schema_keeps_require_parameters_with_auto(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(_ok_payload('{"ok": true}'))

    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.setattr(client.requests, "post", fake_post)

    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "plan", "schema": {"type": "object"}},
    }
    client.call_openrouter(
        system="sys",
        user="hi",
        api_key="sk-test",
        response_format=response_format,
    )

    payload = captured["json"]
    assert payload["model"] == "openrouter/auto"
    assert payload["response_format"] == response_format
    assert payload["provider"] == {"require_parameters": True}
    assert payload["plugins"] == [{"id": "auto-router", "cost_tier": "low"}]


def test_session_id_is_sent_for_conversation_stickiness(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(_ok_payload())

    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.setattr(client.requests, "post", fake_post)

    client.call_openrouter(
        system="sys",
        user="follow up",
        api_key="sk-test",
        conversation_history=[{"role": "user", "content": "first"}],
        session_id="zulip-topic-123",
    )

    assert captured["json"]["session_id"] == "zulip-topic-123"


def test_vision_call_uses_auto_router(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(_ok_payload())

    monkeypatch.delenv("OPENROUTER_VISION_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_COST_TIER", raising=False)
    monkeypatch.setattr(client.requests, "post", fake_post)

    client.call_openrouter_vision(
        system="sys",
        user="read this screenshot",
        images=["data:image/png;base64,abc"],
        api_key="sk-test",
    )

    payload = captured["json"]
    assert payload["model"] == "openrouter/auto"
    assert payload["plugins"] == [{"id": "auto-router", "cost_tier": "low"}]
    assert payload["messages"][1]["content"][1]["type"] == "image_url"


def _answers_payload() -> Dict[str, Any]:
    return {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {"should_reply": {"type": "noul", "noul": 0.91}},
    }


def test_decisions_posts_jev_latest(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(_answers_payload())

    monkeypatch.delenv("OPENROUTER_JEV_MODEL", raising=False)
    monkeypatch.setattr(client.requests, "post", fake_post)

    answers = client.call_openrouter_decisions(
        state={"message": "how was that split?"},
        questions={"should_reply": {"type": "noul", "instructions": "Reply?"}},
        api_key="sk-test",
    )

    assert answers == {"should_reply": {"type": "noul", "noul": 0.91}}
    assert captured["url"] == client.OPENROUTER_DECISIONS_URL
    assert captured["url"] != client.OPENROUTER_CHAT_URL
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["headers"]["HTTP-Referer"] == client.DEFAULT_HTTP_REFERER
    payload = captured["json"]
    assert payload["model"] == "~typesafe/jev-latest"
    assert payload["state"]["message"] == "how was that split?"
    assert "messages" not in payload
    assert "plugins" not in payload


def test_decisions_model_env_override(monkeypatch):
    captured: Dict[str, Any] = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(_answers_payload())

    monkeypatch.setenv("OPENROUTER_JEV_MODEL", "typesafe/jev-1.13")
    monkeypatch.setattr(client.requests, "post", fake_post)

    client.call_openrouter_decisions(
        state="hi",
        questions={"q": {"type": "noul", "instructions": "Yes?"}},
        api_key="sk-test",
    )

    assert captured["json"]["model"] == "typesafe/jev-1.13"
    assert captured["json"]["state"] == {"text": "hi"}


def test_decisions_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: _FakeResponse({"error": {"message": "nope"}}, status_code=400),
    )

    assert (
        client.call_openrouter_decisions(
            state={"text": "hi"},
            questions={"q": {"type": "noul", "instructions": "Yes?"}},
            api_key="sk-test",
        )
        is None
    )


def test_decisions_missing_key_skips_http(monkeypatch):
    def fail_post(*args, **kwargs):
        raise AssertionError("should not post")

    monkeypatch.setattr(client.requests, "post", fail_post)

    assert (
        client.call_openrouter_decisions(
            state={"text": "hi"},
            questions={"q": {"type": "noul", "instructions": "Yes?"}},
            api_key="",
        )
        is None
    )
