from __future__ import annotations

import json
import re

import httpx
import pytest

from app.openai_compat import (
    CLAUDE_CODE_BETA,
    CLAUDE_CODE_SYSTEM_PROMPT,
    CLIENT_PROFILE_CLAUDE_CODE,
    CLIENT_PROFILE_CODEX,
    build_url,
    fetch_models,
    normalize_base_url,
    run_chat_completion_test,
    run_connectivity_test,
)


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
async def test_fetch_models_uses_default_profile_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk-test"
        assert request.headers["user-agent"].startswith("claude-cli/2.1.114")
        assert request.headers["x-app"] == "cli"
        assert request.headers["anthropic-version"] == "2023-06-01"
        return httpx.Response(200, json={"data": []})

    await fetch_models(
        "https://relay.example/v1",
        "sk-test",
        CLIENT_PROFILE_CLAUDE_CODE,
        transport=httpx.MockTransport(handler),
    )


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


@pytest.mark.asyncio
async def test_codex_profile_builds_responses_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://relay.example/v1/responses"
        assert request.headers["authorization"] == "Bearer sk-test"
        assert request.headers["user-agent"].startswith("codex_cli_rs/")
        assert request.headers["originator"] == "codex_cli_rs"
        assert request.headers["openai-beta"] == "responses=experimental"
        body = json.loads(request.content)
        assert body["model"] == "gpt-codex"
        assert body["instructions"]
        assert body["input"] == "ping"
        assert re.fullmatch(r"[0-9a-f-]{36}", body["prompt_cache_key"])
        assert request.headers["session_id"] == body["prompt_cache_key"]
        assert body["stream"] is False
        assert body["store"] is False
        return httpx.Response(204)

    result = await run_connectivity_test(
        "https://relay.example/v1",
        "sk-test",
        "gpt-codex",
        CLIENT_PROFILE_CODEX,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "success"


@pytest.mark.asyncio
async def test_claude_code_profile_builds_messages_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://relay.example/v1/messages"
        assert request.headers["authorization"] == "Bearer sk-test"
        assert "x-api-key" not in request.headers
        assert request.headers["user-agent"].startswith("claude-cli/2.1.114")
        assert request.headers["x-app"] == "cli"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers["anthropic-beta"] == CLAUDE_CODE_BETA
        assert request.headers["anthropic-dangerous-direct-browser-access"] == "true"
        body = json.loads(request.content)
        assert body["model"] == "claude-test"
        assert body["messages"] == [{"role": "user", "content": "ping"}]
        assert body["system"] == [{"type": "text", "text": CLAUDE_CODE_SYSTEM_PROMPT}]
        assert re.fullmatch(
            r"user_[0-9a-f]{64}_account_[0-9a-f-]{36}_session_[0-9a-f-]{36}",
            body["metadata"]["user_id"],
        )
        return httpx.Response(200, text="not-json")

    result = await run_connectivity_test(
        "https://relay.example/v1",
        "sk-test",
        "claude-test",
        CLIENT_PROFILE_CLAUDE_CODE,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "success"
    assert result.raw_response_excerpt == "not-json"


@pytest.mark.asyncio
async def test_connectivity_request_error_is_recordable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    result = await run_connectivity_test(
        "https://relay.example/v1",
        "sk-test",
        "gpt-test",
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "failed"
    assert "请求错误" in result.error_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_profile", "status_code"),
    [(CLIENT_PROFILE_CODEX, 404), (CLIENT_PROFILE_CLAUDE_CODE, 403)],
)
async def test_profile_http_failures_are_recordable(client_profile: str, status_code: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"message": "request rejected"}})

    result = await run_connectivity_test(
        "https://relay.example/v1",
        "sk-test",
        "missing-model",
        client_profile,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "failed"
    assert f"HTTP {status_code}" in result.error_message


@pytest.mark.asyncio
async def test_connectivity_timeout_is_recordable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    result = await run_connectivity_test(
        "https://relay.example/v1",
        "sk-test",
        "gpt-test",
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "failed"
    assert "请求超时" in result.error_message


@pytest.mark.asyncio
async def test_codex_go_client_rejection_explains_new_api_passthrough() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "Only Codex clients can use this group (detected: Go-http-client/1.1)"}},
        )

    result = await run_connectivity_test(
        "https://relay.example/v1",
        "sk-test",
        "gpt-test",
        CLIENT_PROFILE_CODEX,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "failed"
    assert "中间层未透传 Codex User-Agent" in result.error_message
    assert "new-api" in result.error_message
