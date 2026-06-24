from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape

import httpx
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from .models import MonitoringCheck, MonitoringTask, NetworkProxy
from .proxy_support import build_proxy_url, sanitize_proxy_error
from .security import decrypt_secret_with_fernet, get_setting


SETTING_TELEGRAM_ENABLED = "telegram_enabled"
SETTING_TELEGRAM_BOT_TOKEN = "telegram_bot_token_encrypted"
SETTING_TELEGRAM_CHAT_ID = "telegram_chat_id_encrypted"
SETTING_TELEGRAM_PROXY_ID = "telegram_proxy_id"


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool
    has_credentials: bool
    proxy_id: int | None


def read_telegram_config(db: Session) -> TelegramConfig:
    proxy_id: int | None = None
    proxy_value = (get_setting(db, SETTING_TELEGRAM_PROXY_ID) or "").strip()
    if proxy_value:
        try:
            proxy_id = int(proxy_value)
        except ValueError:
            proxy_id = None
    return TelegramConfig(
        enabled=get_setting(db, SETTING_TELEGRAM_ENABLED) == "true",
        has_credentials=bool(get_setting(db, SETTING_TELEGRAM_BOT_TOKEN) and get_setting(db, SETTING_TELEGRAM_CHAT_ID)),
        proxy_id=proxy_id,
    )


def telegram_proxy_url(db: Session, fernet: Fernet) -> str | None:
    proxy_value = (get_setting(db, SETTING_TELEGRAM_PROXY_ID) or "").strip()
    if not proxy_value:
        return None
    try:
        proxy_id = int(proxy_value)
    except ValueError as exc:
        raise ValueError("Telegram 代理配置无效。") from exc
    proxy = db.get(NetworkProxy, proxy_id)
    if proxy is None:
        raise ValueError("Telegram 代理不存在。")
    if not proxy.enabled:
        raise ValueError(f"Telegram 代理“{proxy.name}”已禁用。")
    return build_proxy_url(proxy, fernet)


def decrypt_telegram_credentials(db: Session, fernet: Fernet) -> tuple[str, str]:
    encrypted_token = get_setting(db, SETTING_TELEGRAM_BOT_TOKEN)
    encrypted_chat_id = get_setting(db, SETTING_TELEGRAM_CHAT_ID)
    if not encrypted_token or not encrypted_chat_id:
        raise ValueError("请先配置 Telegram Bot Token 和 Chat ID。")
    return (
        decrypt_secret_with_fernet(encrypted_token, fernet),
        decrypt_secret_with_fernet(encrypted_chat_id, fernet),
    )


async def post_telegram_message(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    proxy_url: str | None = None,
) -> None:
    async with httpx.AsyncClient(timeout=10.0, proxy=proxy_url, trust_env=False) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        )
        if response.is_error:
            raise ValueError(telegram_api_error_message(response))


def telegram_api_error_message(response: httpx.Response) -> str:
    detail = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        description = payload.get("description")
        if isinstance(description, str):
            detail = description.strip()
    if not detail:
        detail = response.text.strip() or response.reason_phrase or "请求失败"
    return f"Telegram API HTTP {response.status_code}: {detail}"


def format_telegram_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def build_recovery_message(task: MonitoringTask, check: MonitoringCheck) -> str:
    latency = "未知" if check.latency_ms is None else f"{check.latency_ms} ms"
    return "\n".join(
        [
            "<b>中转站恢复可用</b>",
            f"监控任务：{escape(task.name)}",
            f"站点：{escape(check.provider_name_snapshot)}",
            f"模型：{escape(check.model_id)}",
            f"客户端：{escape(check.client_profile)}",
            f"网络：{escape(check.network_route)}",
            f"延迟：{latency}",
            f"时间：{format_telegram_time(check.checked_at)}",
        ]
    )


async def send_monitoring_recovery_notification(
    db: Session,
    task: MonitoringTask,
    check: MonitoringCheck,
    fernet: Fernet,
) -> tuple[bool, str]:
    if get_setting(db, SETTING_TELEGRAM_ENABLED) != "true":
        return False, "Telegram 通知未启用。"
    try:
        bot_token, chat_id = decrypt_telegram_credentials(db, fernet)
        proxy_url = telegram_proxy_url(db, fernet)
        await post_telegram_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=build_recovery_message(task, check),
            proxy_url=proxy_url,
        )
        return True, ""
    except Exception as exc:
        return False, sanitize_proxy_error(exc)


async def send_test_telegram_message(db: Session, fernet: Fernet) -> None:
    bot_token, chat_id = decrypt_telegram_credentials(db, fernet)
    proxy_url = telegram_proxy_url(db, fernet)
    await post_telegram_message(
        bot_token=bot_token,
        chat_id=chat_id,
        text="<b>Keys 监控测试消息</b>\nTelegram 通知配置可用。",
        proxy_url=proxy_url,
    )
