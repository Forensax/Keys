from __future__ import annotations

import httpx
from cryptography.fernet import Fernet

from app.models import NetworkProxy
from app.proxy_support import build_proxy_url, sanitize_proxy_error, validate_proxy_fields
from app.security import encrypt_secret_with_fernet


def test_proxy_url_encodes_credentials_and_ipv6_host() -> None:
    fernet = Fernet(Fernet.generate_key())
    proxy = NetworkProxy(
        name="IPv6 Proxy",
        scheme="socks5h",
        host="2001:db8::10",
        port=1080,
        encrypted_username=encrypt_secret_with_fernet("user@example", fernet),
        encrypted_password=encrypt_secret_with_fernet("p:a/ss word", fernet),
    )

    url = build_proxy_url(proxy, fernet)

    assert url == "socks5h://user%40example:p%3Aa%2Fss%20word@[2001:db8::10]:1080"
    assert "user@example" not in proxy.encrypted_username
    assert "p:a/ss word" not in proxy.encrypted_password


def test_proxy_field_validation_rejects_invalid_values() -> None:
    assert validate_proxy_fields("", "ftp", "https://proxy.example/path", "70000") == [
        "名称必填。",
        "代理协议无效。",
        "主机只能填写域名或 IP 地址，不能包含协议、路径或空格。",
        "端口必须是 1 到 65535 之间的整数。",
    ]


def test_proxy_error_sanitizer_removes_credentials() -> None:
    error = RuntimeError("failed via socks5://user:secret@proxy.example:1080")

    sanitized = sanitize_proxy_error(error)

    assert "user" not in sanitized
    assert "secret" not in sanitized
    assert "socks5://***@proxy.example:1080" in sanitized


def test_proxy_error_sanitizer_expands_empty_connect_error_cause() -> None:
    try:
        raise OSError("proxy refused connection")
    except OSError as cause:
        error = httpx.ConnectError("")
        error.__cause__ = cause

    sanitized = sanitize_proxy_error(error)

    assert sanitized == "ConnectError: proxy refused connection"


def test_proxy_error_sanitizer_redacts_telegram_bot_token() -> None:
    request = httpx.Request("POST", "https://api.telegram.org/bot123456:secret-token/sendMessage")
    response = httpx.Response(400, request=request)
    error = httpx.HTTPStatusError(
        "Client error '400 Bad Request' for url 'https://api.telegram.org/bot123456:secret-token/sendMessage'",
        request=request,
        response=response,
    )

    sanitized = sanitize_proxy_error(error)

    assert "123456" not in sanitized
    assert "secret-token" not in sanitized
    assert "https://api.telegram.org/bot***/sendMessage" in sanitized


def test_proxy_error_sanitizer_redacts_feishu_webhook_and_app_secrets() -> None:
    error = RuntimeError(
        "failed for https://open.feishu.cn/open-apis/bot/v2/hook/abc-secret "
        '{"app_secret":"app-secret","tenant_access_token":"tenant-token"}'
    )

    sanitized = sanitize_proxy_error(error)

    assert "abc-secret" not in sanitized
    assert "app-secret" not in sanitized
    assert "tenant-token" not in sanitized
    assert "https://open.feishu.cn/open-apis/bot/v2/hook/***" in sanitized
    assert '"app_secret":"***"' in sanitized
    assert '"tenant_access_token":"***"' in sanitized
