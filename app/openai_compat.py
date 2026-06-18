from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import settings


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    owned_by: str
    raw_json: dict[str, Any]


@dataclass(frozen=True)
class ChatTestResult:
    status: str
    latency_ms: int
    error_message: str
    raw_response_excerpt: str


def normalize_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def build_url(base_url: str, path: str) -> str:
    return f"{normalize_base_url(base_url)}/{path.lstrip('/')}"


def auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


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


async def fetch_models(
    base_url: str,
    api_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[ModelInfo]:
    url = build_url(base_url, "/models")
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, transport=transport) as client:
        response = await client.get(url, headers=auth_headers(api_key))
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


async def run_chat_completion_test(
    base_url: str,
    api_key: str,
    model_id: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ChatTestResult:
    url = build_url(base_url, "/chat/completions")
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "temperature": 0,
    }

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout_seconds, transport=transport) as client:
            response = await client.post(url, headers=auth_headers(api_key), json=payload)
        latency_ms = int((time.perf_counter() - started) * 1000)
        excerpt = response_excerpt(response)
        if response.is_success:
            return ChatTestResult("success", latency_ms, "", excerpt)
        return ChatTestResult("failed", latency_ms, f"HTTP {response.status_code}: {excerpt[:300]}", excerpt)
    except httpx.TimeoutException as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ChatTestResult("failed", latency_ms, f"请求超时：{exc}", "")
    except httpx.RequestError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ChatTestResult("failed", latency_ms, f"请求错误：{exc}", "")
