from __future__ import annotations

import json
import re

import httpx
import pytest

from app.openai_compat import (
    CLAUDE_CODE_BETA,
    CLAUDE_CODE_SYSTEM_PROMPT,
    CONNECTIVITY_CHECK_PROMPT,
    CLIENT_PROFILE_CLAUDE_CODE,
    CLIENT_PROFILE_CODEX,
    CLIENT_PROFILE_OPENAI_RESPONSES,
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
        assert request.headers["user-agent"].startswith("claude-cli/2.1.181")
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
        body = json.loads(request.content)
        assert body["messages"] == [{"role": "user", "content": CONNECTIVITY_CHECK_PROMPT}]
        return httpx.Response(200, json={"choices": [{"message": {"content": "42"}}]})

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
async def test_openai_responses_profile_builds_plain_responses_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://relay.example/v1/responses"
        assert request.headers["authorization"] == "Bearer sk-test"
        assert request.headers["accept"] == "application/json"
        assert "originator" not in request.headers
        assert "session-id" not in request.headers
        assert "thread-id" not in request.headers
        assert "x-client-request-id" not in request.headers
        assert "x-codex-window-id" not in request.headers
        assert "x-codex-turn-metadata" not in request.headers
        body = json.loads(request.content)
        assert body == {
            "model": "gpt-responses",
            "input": CONNECTIVITY_CHECK_PROMPT,
            "max_output_tokens": 8,
            "store": False,
        }
        return httpx.Response(200, json={"output_text": "42"})

    result = await run_connectivity_test(
        "https://relay.example/v1",
        "sk-test",
        "gpt-responses",
        CLIENT_PROFILE_OPENAI_RESPONSES,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "success"
    assert result.error_message == ""


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
        assert request.headers["user-agent"].startswith("Codex Desktop/0.141.0")
        assert request.headers["originator"] == "Codex Desktop"
        assert request.headers["accept"] == "text/event-stream"
        assert "openai-beta" not in request.headers
        body = json.loads(request.content)
        assert body["model"] == "gpt-codex"
        assert body["instructions"] == "You are Codex, a coding agent based on GPT-5. Reply briefly."
        assert body["input"][0]["role"] == "developer"
        assert body["input"][0]["content"][0]["text"] == "This is a connectivity check. Do not call tools."
        assert body["input"][1]["content"][0]["text"] == CONNECTIVITY_CHECK_PROMPT
        assert body["tools"] == []
        assert body["tool_choice"] == "auto"
        assert body["parallel_tool_calls"] is True
        assert body["reasoning"] == {"effort": "medium"}
        assert body["include"] == ["reasoning.encrypted_content"]
        assert body["text"] == {"verbosity": "low"}
        assert "max_output_tokens" not in body
        assert re.fullmatch(r"[0-9a-f-]{36}", body["prompt_cache_key"])
        metadata = body["client_metadata"]
        assert request.headers["session-id"] == body["prompt_cache_key"] == metadata["session_id"]
        assert request.headers["thread-id"] == metadata["thread_id"]
        assert request.headers["x-client-request-id"] == metadata["thread_id"]
        assert request.headers["x-codex-window-id"] == metadata["x-codex-window-id"]
        assert request.headers["x-codex-turn-metadata"] == metadata["x-codex-turn-metadata"]
        turn_metadata = json.loads(metadata["x-codex-turn-metadata"])
        assert turn_metadata["session_id"] == metadata["session_id"]
        assert turn_metadata["thread_id"] == metadata["thread_id"]
        assert turn_metadata["turn_id"] == metadata["turn_id"]
        assert turn_metadata["installation_id"] == metadata["x-codex-installation-id"]
        assert turn_metadata["window_id"] == metadata["x-codex-window-id"]
        assert turn_metadata["request_kind"] == "turn"
        assert turn_metadata["sandbox"] == "none"
        assert isinstance(turn_metadata["turn_started_at_unix_ms"], int)
        assert body["stream"] is True
        assert body["store"] is False
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"4"}\n\n'
                'event: response.output_text.delta\n'
                'data: {"type":"response.output_text.delta","delta":"2"}\n\n'
                'data: [DONE]\n\n'
            ),
        )

    result = await run_connectivity_test(
        "https://relay.example/v1",
        "sk-test",
        "gpt-codex",
        CLIENT_PROFILE_CODEX,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "success"
    assert result.raw_response_excerpt == "42"


@pytest.mark.asyncio
async def test_claude_code_profile_builds_messages_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://relay.example/v1/messages"
        assert request.headers["authorization"] == "Bearer sk-test"
        assert "x-api-key" not in request.headers
        assert request.headers["user-agent"].startswith("claude-cli/2.1.181")
        assert request.headers["x-app"] == "cli"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers["anthropic-beta"] == CLAUDE_CODE_BETA
        assert "context-1m-2025-08-07" in request.headers["anthropic-beta"].split(",")
        assert request.headers["anthropic-dangerous-direct-browser-access"] == "true"
        body = json.loads(request.content)
        assert body["model"] == "claude-test"
        assert body["messages"] == [{"role": "user", "content": CONNECTIVITY_CHECK_PROMPT}]
        assert body["system"] == [{"type": "text", "text": CLAUDE_CODE_SYSTEM_PROMPT}]
        assert re.fullmatch(
            r"user_[0-9a-f]{64}_account_[0-9a-f-]{36}_session_[0-9a-f-]{36}",
            body["metadata"]["user_id"],
        )
        return httpx.Response(200, text="答案是42")

    result = await run_connectivity_test(
        "https://relay.example/v1",
        "sk-test",
        "claude-test",
        CLIENT_PROFILE_CLAUDE_CODE,
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "success"
    assert result.raw_response_excerpt == "答案是42"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["41", "不知道"])
async def test_successful_http_response_is_connectivity_success_even_with_unexpected_answer(answer: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

    result = await run_chat_completion_test(
        "https://relay.example/v1",
        "sk-test",
        "gpt-test",
        transport=httpx.MockTransport(handler),
    )

    assert result.status == "success"
    assert result.error_message == ""
    assert result.raw_response_excerpt == answer


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


@pytest.mark.asyncio
async def test_requests_pass_explicit_proxy_and_disable_environment_proxy(monkeypatch) -> None:
    client_options: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            client_options.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, url, **kwargs):
            request = httpx.Request("GET", url)
            return httpx.Response(200, json={"data": []}, request=request)

        async def post(self, url, **kwargs):
            request = httpx.Request("POST", url)
            return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr("app.openai_compat.httpx.AsyncClient", FakeAsyncClient)
    proxy_url = "socks5h://user:password@proxy.example:1080"

    await fetch_models("https://relay.example/v1", "sk-test", proxy_url=proxy_url)
    await run_connectivity_test("https://relay.example/v1", "sk-test", "gpt-test", proxy_url=proxy_url)

    assert len(client_options) == 2
    assert all(options["proxy"] == proxy_url for options in client_options)
    assert all(options["trust_env"] is False for options in client_options)


@pytest.mark.asyncio
async def test_direct_requests_explicitly_disable_environment_proxy(monkeypatch) -> None:
    captured: dict = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def get(self, url, **kwargs):
            request = httpx.Request("GET", url)
            return httpx.Response(200, json={"data": []}, request=request)

    monkeypatch.setattr("app.openai_compat.httpx.AsyncClient", FakeAsyncClient)

    await fetch_models("https://relay.example/v1", "sk-test")

    assert captured["proxy"] is None
    assert captured["trust_env"] is False
