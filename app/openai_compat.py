from __future__ import annotations

import json
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from .config import settings
from .proxy_support import sanitize_proxy_error


CLIENT_PROFILE_OPENAI_CHAT = "openai_chat"
CLIENT_PROFILE_CODEX = "codex"
CLIENT_PROFILE_CLAUDE_CODE = "claude_code"
CLIENT_PROFILE_LABELS = {
    CLIENT_PROFILE_OPENAI_CHAT: "标准 OpenAI",
    CLIENT_PROFILE_CODEX: "Codex",
    CLIENT_PROFILE_CLAUDE_CODE: "Claude Code",
}
VALID_CLIENT_PROFILES = frozenset(CLIENT_PROFILE_LABELS)

CODEX_USER_AGENT = "Codex Desktop/0.141.0 (Windows 10.0.26200; x86_64) unknown (codex_exec; 0.141.0)"
CODEX_ORIGINATOR = "Codex Desktop"
CLAUDE_CODE_USER_AGENT = "claude-cli/2.1.181 (external, sdk-cli)"
CLAUDE_CODE_BETA = (
    "claude-code-20250219,interleaved-thinking-2025-05-14,"
    "context-management-2025-06-27,context-1m-2025-08-07,"
    "prompt-caching-scope-2026-01-05,advisor-tool-2026-03-01"
)
CLAUDE_CODE_SYSTEM_PROMPT = "You are Claude Code, Anthropic's official CLI for Claude."


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    owned_by: str
    raw_json: dict[str, Any]


@dataclass(frozen=True)
class ConnectivityTestResult:
    status: str
    latency_ms: int
    error_message: str
    raw_response_excerpt: str


def normalize_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def build_url(base_url: str, path: str) -> str:
    return f"{normalize_base_url(base_url)}/{path.lstrip('/')}"


def validate_client_profile(client_profile: str) -> str:
    if client_profile not in VALID_CLIENT_PROFILES:
        raise ValueError("不支持的客户端模式。")
    return client_profile


def client_profile_label(client_profile: str) -> str:
    return CLIENT_PROFILE_LABELS.get(client_profile, client_profile)


def auth_headers(api_key: str, client_profile: str = CLIENT_PROFILE_OPENAI_CHAT) -> dict[str, str]:
    profile = validate_client_profile(client_profile)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if profile == CLIENT_PROFILE_CODEX:
        headers.update(
            {
                "User-Agent": CODEX_USER_AGENT,
                "originator": CODEX_ORIGINATOR,
            }
        )
    elif profile == CLIENT_PROFILE_CLAUDE_CODE:
        headers.update(
            {
                "User-Agent": CLAUDE_CODE_USER_AGENT,
                "X-App": "cli",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": CLAUDE_CODE_BETA,
                "anthropic-dangerous-direct-browser-access": "true",
            }
        )
    return headers


def compact_json(value: Any, limit: int = 4000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        text = str(value)
    return text[:limit]


def response_excerpt(response: httpx.Response | None = None, text: str | None = None, limit: int = 1200) -> str:
    if text is None and response is not None:
        text = response.text
    return (text or "")[:limit]


def codex_response_excerpt(response: httpx.Response, limit: int = 1200) -> str:
    deltas: list[str] = []
    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "response.output_text.delta" and isinstance(event.get("delta"), str):
            deltas.append(event["delta"])
    output_text = "".join(deltas).strip()
    return response_excerpt(response, text=output_text or None, limit=limit)


def _claude_code_metadata_user_id() -> str:
    return f"user_{secrets.token_hex(32)}_account_{uuid.uuid4()}_session_{uuid.uuid4()}"


def _codex_request_context() -> dict[str, str]:
    session_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    installation_id = str(uuid.uuid4())
    window_id = f"{session_id}:0"
    turn_metadata = compact_json(
        {
            "installation_id": installation_id,
            "session_id": session_id,
            "thread_id": session_id,
            "turn_id": turn_id,
            "window_id": window_id,
            "request_kind": "turn",
            "sandbox": "none",
            "turn_started_at_unix_ms": int(time.time() * 1000),
        }
    )
    return {
        "session_id": session_id,
        "thread_id": session_id,
        "turn_id": turn_id,
        "installation_id": installation_id,
        "window_id": window_id,
        "turn_metadata": turn_metadata,
    }


def build_connectivity_request(client_profile: str, model_id: str) -> tuple[str, dict[str, Any]]:
    profile = validate_client_profile(client_profile)
    if profile == CLIENT_PROFILE_CODEX:
        context = _codex_request_context()
        return "/responses", {
            "model": model_id,
            "instructions": "You are Codex, a coding agent based on GPT-5. Reply briefly.",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "This is a connectivity check. Do not call tools."}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Reply exactly with pong."}],
                },
            ],
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "reasoning": {"effort": "medium"},
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": context["session_id"],
            "text": {"verbosity": "low"},
            "client_metadata": {
                "session_id": context["session_id"],
                "thread_id": context["thread_id"],
                "turn_id": context["turn_id"],
                "x-codex-installation-id": context["installation_id"],
                "x-codex-window-id": context["window_id"],
                "x-codex-turn-metadata": context["turn_metadata"],
            },
        }
    if profile == CLIENT_PROFILE_CLAUDE_CODE:
        return "/messages", {
            "model": model_id,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
            "system": [{"type": "text", "text": CLAUDE_CODE_SYSTEM_PROMPT}],
            "metadata": {"user_id": _claude_code_metadata_user_id()},
        }
    return "/chat/completions", {
        "model": model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "temperature": 0,
    }


async def fetch_models(
    base_url: str,
    api_key: str,
    client_profile: str = CLIENT_PROFILE_OPENAI_CHAT,
    transport: httpx.AsyncBaseTransport | None = None,
    proxy_url: str | None = None,
) -> list[ModelInfo]:
    url = build_url(base_url, "/models")
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        transport=transport,
        proxy=proxy_url,
        trust_env=False,
    ) as client:
        response = await client.get(url, headers=auth_headers(api_key, client_profile))
        response.raise_for_status()
        payload = response.json()

    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("模型列表响应中没有 data 数组。")

    models: list[ModelInfo] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        owned_by = item.get("owned_by")
        models.append(ModelInfo(model_id=model_id, owned_by=owned_by if isinstance(owned_by, str) else "", raw_json=item))
    return models


async def run_connectivity_test(
    base_url: str,
    api_key: str,
    model_id: str,
    client_profile: str = CLIENT_PROFILE_OPENAI_CHAT,
    transport: httpx.AsyncBaseTransport | None = None,
    proxy_url: str | None = None,
) -> ConnectivityTestResult:
    path, payload = build_connectivity_request(client_profile, model_id)
    url = build_url(base_url, path)
    headers = auth_headers(api_key, client_profile)
    if client_profile == CLIENT_PROFILE_CODEX:
        metadata = payload["client_metadata"]
        headers.update(
            {
                "Accept": "text/event-stream",
                "session-id": metadata["session_id"],
                "thread-id": metadata["thread_id"],
                "x-client-request-id": metadata["thread_id"],
                "x-codex-window-id": metadata["x-codex-window-id"],
                "x-codex-turn-metadata": metadata["x-codex-turn-metadata"],
            }
        )

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            transport=transport,
            proxy=proxy_url,
            trust_env=False,
        ) as client:
            response = await client.post(url, headers=headers, json=payload)
        latency_ms = int((time.perf_counter() - started) * 1000)
        excerpt = codex_response_excerpt(response) if client_profile == CLIENT_PROFILE_CODEX else response_excerpt(response)
        if response.is_success:
            return ConnectivityTestResult("success", latency_ms, "", excerpt)
        error_message = f"HTTP {response.status_code}: {excerpt[:300]}"
        if (
            client_profile == CLIENT_PROFILE_CODEX
            and "only codex clients" in excerpt.lower()
            and "go-http-client" in excerpt.lower()
        ):
            error_message += "；中间层未透传 Codex User-Agent，请让 new-api 管理员启用 Codex CLI 请求头透传。"
        return ConnectivityTestResult("failed", latency_ms, error_message, excerpt)
    except httpx.TimeoutException as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        detail = sanitize_proxy_error(exc) if proxy_url else str(exc)
        return ConnectivityTestResult("failed", latency_ms, f"请求超时：{detail}", "")
    except httpx.RequestError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        detail = sanitize_proxy_error(exc) if proxy_url else str(exc)
        return ConnectivityTestResult("failed", latency_ms, f"请求错误：{detail}", "")


async def run_chat_completion_test(
    base_url: str,
    api_key: str,
    model_id: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ConnectivityTestResult:
    return await run_connectivity_test(
        base_url,
        api_key,
        model_id,
        CLIENT_PROFILE_OPENAI_CHAT,
        transport,
    )
