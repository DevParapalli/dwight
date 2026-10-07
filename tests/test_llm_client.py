from types import SimpleNamespace

import pytest

from app.llm import client
from app.settings import settings


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _install(monkeypatch, response) -> FakeMessages:
    messages = FakeMessages(response)
    monkeypatch.setattr(client.anthropic, "Anthropic", lambda **_: SimpleNamespace(messages=messages))
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "dwight_mode", "agent")
    return messages


def _response(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        usage=SimpleNamespace(output_tokens=12),
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
    )


def test_anthropic_serves_call_at_low_effort(monkeypatch):
    messages = _install(monkeypatch, _response('```json\n{"field": "employee_id"}\n```'))
    result, _ = client.complete_json("system", "user")
    assert result == {"field": "employee_id"}
    assert client.active_model() == settings.anthropic_model
    sent = messages.calls[0]
    assert sent["model"] == settings.anthropic_model
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "low"}
    assert sent["max_tokens"] >= settings.anthropic_max_output_tokens


def test_anthropic_refusal_fails_loudly(monkeypatch):
    _install(monkeypatch, _response("", stop_reason="refusal"))
    with pytest.raises(RuntimeError, match="refusal"):
        client.complete_json("system", "user")


def test_automation_mode_makes_no_call_even_with_a_key(monkeypatch):
    messages = _install(monkeypatch, _response("{}"))
    monkeypatch.setattr(settings, "dwight_mode", "automation")
    with pytest.raises(client.LLMNotConfigured):
        client.complete_json("system", "user")
    assert messages.calls == []


def test_anthropic_reply_with_reasoning_before_json(monkeypatch):
    # At low effort the model can reason in prose, with a brace in it, before
    # the answer.
    text = ('The {designation} value is a known abbreviation, so expand it.\n\n'
            '{"field": "designation", "proposed_value": "Senior Software Engineer", "confidence": 0.8}')
    _install(monkeypatch, _response(text))
    result, _ = client.complete_json("system", "user")
    assert result["field"] == "designation"
    assert result["proposed_value"] == "Senior Software Engineer"


def test_anthropic_reply_without_json_raises(monkeypatch):
    _install(monkeypatch, _response("I cannot suggest a fix for this record."))
    with pytest.raises(ValueError, match="without a trailing JSON object"):
        client.complete_json("system", "user")
