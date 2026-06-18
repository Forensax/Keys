from __future__ import annotations

import ipaddress
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet

from .config import settings
from .models import NetworkProxy
from .security import decrypt_secret_with_fernet


VALID_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


@dataclass(frozen=True)
class ProxyTestResult:
    status: str
    exit_ip: str
    latency_ms: int
    error_message: str


def validate_proxy_fields(name: str, scheme: str, host: str, port: str | int) -> list[str]:
    errors: list[str] = []
    if not name.strip():
        errors.append("名称必填。")
    if scheme not in VALID_PROXY_SCHEMES:
        errors.append("代理协议无效。")
    clean_host = host.strip().strip("[]")
    if not clean_host:
        errors.append("主机必填。")
    elif any(char.isspace() for char in clean_host) or "://" in clean_host or "/" in clean_host:
        errors.append("主机只能填写域名或 IP 地址，不能包含协议、路径或空格。")
    try:
        parsed_port = int(port)
        if not 1 <= parsed_port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("端口必须是 1 到 65535 之间的整数。")
    return errors


def build_proxy_url(proxy: NetworkProxy, fernet: Fernet) -> str:
    if proxy.scheme not in VALID_PROXY_SCHEMES:
        raise ValueError("代理协议无效。")
    username = decrypt_secret_with_fernet(proxy.encrypted_username, fernet)
    password = decrypt_secret_with_fernet(proxy.encrypted_password, fernet)
    auth = ""
    if username or password:
        auth = quote(username, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    host = proxy.host.strip().strip("[]")
    if not host:
        raise ValueError("代理主机为空。")
    if ":" in host:
        host = f"[{host}]"
    return f"{proxy.scheme}://{auth}{host}:{proxy.port}"


def sanitize_proxy_error(exc: Exception) -> str:
    message = re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^@\s/]+@", r"\1***@", str(exc))
    return f"{type(exc).__name__}: {message}"[:500]


async def test_proxy_connection(
    proxy_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProxyTestResult:
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            proxy=proxy_url,
            transport=transport,
            trust_env=False,
        ) as client:
            response = await client.get(settings.proxy_test_url, headers={"Accept": "application/json"})
        latency_ms = int((time.perf_counter() - started) * 1000)
        response.raise_for_status()
        try:
            payload = response.json()
            exit_ip = str(payload.get("ip") or "").strip() if isinstance(payload, dict) else ""
        except ValueError:
            exit_ip = response.text.strip()
        ipaddress.ip_address(exit_ip)
        return ProxyTestResult("success", exit_ip, latency_ms, "")
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ProxyTestResult("failed", "", latency_ms, sanitize_proxy_error(exc))
