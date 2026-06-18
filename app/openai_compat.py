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

CODEX_USER_AGENT = "codex_cli_rs/0.125.0 (Ubuntu 22.4.0; x86_64) xterm-256color"
CLAUDE_CODE_USER_AGENT = "claude-cli/2.1.181 (external, sdk-cli)"
CLAUDE_CODE_BETA = (
    "claude-code-20250219,interleaved-thinking-2025-05-14,"
    "context-management-2025-06-27,prompt-caching-scope-2026-01-05,advisor-tool-2026-03-01"
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
                "originator": "codex_cli_rs",
                "OpenAI-Beta": "responses=experimental",
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


def _claude_code_metadata_user_id() -> str:
    return f"user_{secrets.token_hex(32)}_account_{uuid.uuid4()}_session_{uuid.uuid4()}"


def _codex_session_id() -> str:
    return str(uuid.uuid4())


def build_connectivity_request(client_profile: str, model_id: str) -> tuple[str, dict[str, Any]]:
    profile = validate_client_profile(client_profile)
    if profile == CLIENT_PROFILE_CODEX:
        session_id = _codex_session_id()
        return "/responses", {
            "model": model_id,
            "instructions": "You are Codex, a coding agent. Reply briefly to this connectivity check.",
            "input": "ping",
            "max_output_tokens": 16,
            "prompt_cache_key": session_id,
            "stream": False,
            "store": False,
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
        headers["Session_id"] = str(payload["prompt_cache_key"])

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
        excerpt = response_excerpt(response)
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
