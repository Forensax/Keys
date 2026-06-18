from __future__ import annotations

import httpx
import pytest

from app.openai_compat import build_url, fetch_models, normalize_base_url, run_chat_completion_test


def test_base_url_normalization() -> None:
    assert normalize_base_url(" https://example.com/v1/ ") == "https://example.com/v1"
    assert build_url("https://example.com/v1/", "/models") == "https://example.com/v1/models"


@pytest.mark.asyncio
async def test_fetch_models_parses_openai_data_array() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://relay.example/v1/models"
        assert request.headers["authorization"] == "Bearer sk-test"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-test", "owned_by": "relay"},
                    {"id": "gpt-other"},
                    {"object": "model"},
                ],
            },
        )

    models = await fetch_models("https://relay.example/v1", "sk-test", transport=httpx.MockTransport(handler))

    assert [model.model_id for model in models] == ["gpt-test", "gpt-other"]
    assert models[0].owned_by == "relay"


@pytest.mark.asyncio
async def test_chat_completion_success() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == "https://relay.example/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})

    result = await run_chat_completion_test(
        "https://relay.example/v1",
        "sk-test",
        "gpt-test",
        transport=httpx.MockTransport(handler),
    )

    assert len(calls) == 1
    assert result.status == "success"
    assert result.error_message == ""
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_chat_completion_http_failure_is_recordable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    result = await run_chat_completion_test(
        "https://relay.example/v1",
        "bad-key",
        "gpt-test",
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "failed"
    assert "HTTP 401" in result.error_message
