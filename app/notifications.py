from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from html import escape

import httpx
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from .models import MonitoringCheck, MonitoringTask, NetworkProxy, NotificationChannel, utc_now
from .proxy_support import build_proxy_url, sanitize_proxy_error
from .security import decrypt_secret_with_fernet, encrypt_secret_with_fernet, get_setting, set_setting


SETTING_TELEGRAM_ENABLED = "telegram_enabled"
SETTING_TELEGRAM_BOT_TOKEN = "telegram_bot_token_encrypted"
SETTING_TELEGRAM_CHAT_ID = "telegram_chat_id_encrypted"
SETTING_TELEGRAM_PROXY_ID = "telegram_proxy_id"
SETTING_LEGACY_TELEGRAM_MIGRATED = "notification_channels_legacy_telegram_migrated"

CHANNEL_TYPE_TELEGRAM = "telegram"
CHANNEL_TYPE_FEISHU_WEBHOOK = "feishu_webhook"
CHANNEL_TYPE_FEISHU_APP = "feishu_app"
VALID_NOTIFICATION_CHANNEL_TYPES = {
    CHANNEL_TYPE_TELEGRAM,
    CHANNEL_TYPE_FEISHU_WEBHOOK,
    CHANNEL_TYPE_FEISHU_APP,
}
NOTIFICATION_CHANNEL_TYPE_LABELS = {
    CHANNEL_TYPE_TELEGRAM: "Telegram",
    CHANNEL_TYPE_FEISHU_WEBHOOK: "飞书自定义机器人",
    CHANNEL_TYPE_FEISHU_APP: "飞书应用机器人",
}


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


def channel_config(channel: NotificationChannel) -> dict:
    try:
        payload = json.loads(channel.config_json or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def channel_secrets(channel: NotificationChannel, fernet: Fernet) -> dict:
    if not channel.encrypted_secret_json:
        return {}
    try:
        payload = decrypt_secret_with_fernet(channel.encrypted_secret_json, fernet)
        parsed = json.loads(payload)
    except Exception as exc:
        raise ValueError("通知渠道凭据解密失败。") from exc
    return parsed if isinstance(parsed, dict) else {}


def encrypt_channel_secrets(secrets: dict, fernet: Fernet) -> str:
    return encrypt_secret_with_fernet(json.dumps(secrets, ensure_ascii=False), fernet)


def channel_has_credentials(channel: NotificationChannel) -> bool:
    return bool(channel.encrypted_secret_json)


def notification_channel_proxy_url(db: Session, channel: NotificationChannel, fernet: Fernet) -> str | None:
    if channel.proxy_id is None:
        return None
    proxy = db.get(NetworkProxy, channel.proxy_id)
    if proxy is None:
        raise ValueError(f"通知渠道“{channel.name}”的代理不存在。")
    if not proxy.enabled:
        raise ValueError(f"通知渠道“{channel.name}”的代理“{proxy.name}”已禁用。")
    return build_proxy_url(proxy, fernet)


def legacy_telegram_channel(db: Session) -> NotificationChannel | None:
    from sqlalchemy import select

    return db.scalar(select(NotificationChannel).where(NotificationChannel.name == "默认 Telegram"))


def migrate_legacy_telegram_channel(db: Session, fernet: Fernet, *, force: bool = False) -> NotificationChannel | None:
    channel = legacy_telegram_channel(db)
    if get_setting(db, SETTING_LEGACY_TELEGRAM_MIGRATED) == "true" and not force:
        return channel
    encrypted_token = get_setting(db, SETTING_TELEGRAM_BOT_TOKEN)
    encrypted_chat_id = get_setting(db, SETTING_TELEGRAM_CHAT_ID)
    if encrypted_token and encrypted_chat_id:
        bot_token = decrypt_secret_with_fernet(encrypted_token, fernet)
        chat_id = decrypt_secret_with_fernet(encrypted_chat_id, fernet)
        proxy_id = None
        proxy_value = (get_setting(db, SETTING_TELEGRAM_PROXY_ID) or "").strip()
        if proxy_value:
            try:
                proxy_id = int(proxy_value)
            except ValueError:
                proxy_id = None
        if channel is None:
            channel = NotificationChannel(
                name="默认 Telegram",
                channel_type=CHANNEL_TYPE_TELEGRAM,
                enabled=get_setting(db, SETTING_TELEGRAM_ENABLED) == "true",
                proxy_id=proxy_id,
                config_json="{}",
                encrypted_secret_json=encrypt_channel_secrets(
                    {"bot_token": bot_token, "chat_id": chat_id},
                    fernet,
                ),
            )
            db.add(channel)
            db.flush()
        else:
            channel.channel_type = CHANNEL_TYPE_TELEGRAM
            channel.enabled = get_setting(db, SETTING_TELEGRAM_ENABLED) == "true"
            channel.proxy_id = proxy_id
            channel.config_json = "{}"
            channel.encrypted_secret_json = encrypt_channel_secrets(
                {"bot_token": bot_token, "chat_id": chat_id},
                fernet,
            )
    if channel is not None:
        from .models import MonitoringTaskNotificationChannel
        from sqlalchemy import select

        task_ids = db.scalars(select(MonitoringTask.id)).all()
        for task_id in task_ids:
            existing = db.scalar(
                select(MonitoringTaskNotificationChannel).where(
                    MonitoringTaskNotificationChannel.task_id == task_id
                )
            )
            if existing is None:
                db.add(
                    MonitoringTaskNotificationChannel(
                        task_id=task_id,
                        channel_id=channel.id,
                        channel_name_snapshot=channel.name,
                        channel_type_snapshot=channel.channel_type,
                    )
                )
    set_setting(db, SETTING_LEGACY_TELEGRAM_MIGRATED, "true")
    return channel


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


def format_telegram_time(value: datetime | None) -> str:
    if value is None:
        return "未知"
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


def build_failure_message(task: MonitoringTask, check: MonitoringCheck) -> str:
    error = check.error_message or "未返回具体错误"
    return "\n".join(
        [
            "<b>中转站变为不可用</b>",
            f"监控任务：{escape(task.name)}",
            f"站点：{escape(check.provider_name_snapshot)}",
            f"模型：{escape(check.model_id)}",
            f"客户端：{escape(check.client_profile)}",
            f"网络：{escape(check.network_route)}",
            f"错误：{escape(error)}",
            f"时间：{format_telegram_time(check.checked_at)}",
        ]
    )


def build_notification_message(task: MonitoringTask, check: MonitoringCheck, event: str) -> str:
    return build_failure_message(task, check) if event == "failure" else build_recovery_message(task, check)


def clean_feishu_text(value: object, *, max_length: int = 600) -> str:
    text = str(value or "").strip().replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return "未知"
    if len(text) > max_length:
        return text[: max_length - 3].rstrip() + "..."
    return text


def escape_feishu_lark_md(value: object, *, max_length: int = 600) -> str:
    text = clean_feishu_text(value, max_length=max_length)
    return (
        text.replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("`", "\\`")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def feishu_card_field(label: str, value: object, *, short: bool = True, max_length: int = 600) -> dict[str, object]:
    return {
        "is_short": short,
        "text": {
            "tag": "lark_md",
            "content": f"**{label}**\n{escape_feishu_lark_md(value, max_length=max_length)}",
        },
    }


def build_feishu_notification_card(task: MonitoringTask, check: MonitoringCheck, event: str) -> dict[str, object]:
    is_failure = event == "failure"
    title = "中转站变为不可用" if is_failure else "中转站恢复可用"
    status = "不可用" if is_failure else "恢复可用"
    latency_or_error = (check.error_message or "未返回具体错误") if is_failure else (
        "未知" if check.latency_ms is None else f"{check.latency_ms} ms"
    )
    latency_or_error_label = "错误" if is_failure else "延迟"
    latency_or_error_max_length = 900 if is_failure else 200
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "red" if is_failure else "green",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    feishu_card_field("状态", status),
                    feishu_card_field("监控任务", task.name),
                    feishu_card_field("站点", check.provider_name_snapshot),
                    feishu_card_field("模型", check.model_id),
                    feishu_card_field("客户端", check.client_profile),
                    feishu_card_field("网络", check.network_route),
                    feishu_card_field(
                        latency_or_error_label,
                        latency_or_error,
                        short=not is_failure,
                        max_length=latency_or_error_max_length,
                    ),
                    feishu_card_field("时间", format_telegram_time(check.checked_at)),
                ],
            }
        ],
    }


async def post_feishu_webhook_message(
    *,
    webhook_url: str,
    text: str = "",
    card: dict[str, object] | None = None,
    signing_secret: str = "",
    proxy_url: str | None = None,
) -> None:
    payload: dict[str, object]
    if card is None:
        payload = {"msg_type": "text", "content": {"text": text}}
    else:
        payload = {"msg_type": "interactive", "card": card}
    if signing_secret:
        timestamp = str(int(datetime.now().timestamp()))
        sign_key = f"{timestamp}\n{signing_secret}".encode("utf-8")
        sign = base64.b64encode(hmac.new(sign_key, b"", digestmod=hashlib.sha256).digest()).decode("ascii")
        payload["timestamp"] = timestamp
        payload["sign"] = sign
    async with httpx.AsyncClient(timeout=10.0, proxy=proxy_url, trust_env=False) as client:
        response = await client.post(webhook_url, json=payload)
        if response.is_error:
            raise ValueError(feishu_api_error_message("飞书 Webhook", response))
        try:
            body = response.json()
        except ValueError:
            body = {}
        code = body.get("code") if isinstance(body, dict) else None
        if code not in (None, 0):
            raise ValueError(f"飞书 Webhook API code {code}: {body.get('msg') or body.get('message') or '请求失败'}")


async def post_feishu_app_message(
    *,
    app_id: str,
    app_secret: str,
    receive_id_type: str,
    receive_id: str,
    text: str = "",
    card: dict[str, object] | None = None,
    proxy_url: str | None = None,
) -> None:
    async with httpx.AsyncClient(timeout=10.0, proxy=proxy_url, trust_env=False) as client:
        token_response = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        if token_response.is_error:
            raise ValueError(feishu_api_error_message("飞书租户 token", token_response))
        token_payload = token_response.json()
        tenant_access_token = token_payload.get("tenant_access_token") if isinstance(token_payload, dict) else None
        if not tenant_access_token:
            raise ValueError(f"飞书租户 token 获取失败：{token_payload.get('msg') if isinstance(token_payload, dict) else '无响应'}")
        message_response = await client.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            headers={"Authorization": f"Bearer {tenant_access_token}"},
            json={
                "receive_id": receive_id,
                "msg_type": "text" if card is None else "interactive",
                "content": json.dumps({"text": text} if card is None else card, ensure_ascii=False),
            },
        )
        if message_response.is_error:
            raise ValueError(feishu_api_error_message("飞书消息", message_response))
        message_payload = message_response.json()
        code = message_payload.get("code") if isinstance(message_payload, dict) else None
        if code not in (None, 0):
            raise ValueError(f"飞书消息 API code {code}: {message_payload.get('msg') or '请求失败'}")


def feishu_api_error_message(prefix: str, response: httpx.Response) -> str:
    detail = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("msg", "message", "error_description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                detail = value.strip()
                break
    if not detail:
        detail = response.text.strip() or response.reason_phrase or "请求失败"
    return f"{prefix} HTTP {response.status_code}: {detail}"


def webhook_url_from_secret(secret: dict) -> str:
    webhook_url = str(secret.get("webhook_url") or "").strip()
    webhook_token = str(secret.get("webhook_token") or "").strip()
    if webhook_url:
        return webhook_url
    if webhook_token:
        return f"https://open.feishu.cn/open-apis/bot/v2/hook/{webhook_token}"
    raise ValueError("请先配置飞书 Webhook URL 或 Hook Token。")


async def send_notification_channel_message(
    db: Session,
    channel: NotificationChannel,
    task: MonitoringTask,
    check: MonitoringCheck,
    fernet: Fernet,
    *,
    event: str,
) -> tuple[bool, str]:
    if not channel.enabled:
        return False, "通知渠道已停用。"
    try:
        secret = channel_secrets(channel, fernet)
        config = channel_config(channel)
        proxy_url = notification_channel_proxy_url(db, channel, fernet)
        if channel.channel_type == CHANNEL_TYPE_TELEGRAM:
            text = build_notification_message(task, check, event)
            await post_telegram_message(
                bot_token=str(secret.get("bot_token") or ""),
                chat_id=str(secret.get("chat_id") or ""),
                text=text,
                proxy_url=proxy_url,
            )
        elif channel.channel_type == CHANNEL_TYPE_FEISHU_WEBHOOK:
            card = build_feishu_notification_card(task, check, event)
            await post_feishu_webhook_message(
                webhook_url=webhook_url_from_secret(secret),
                signing_secret=str(secret.get("signing_secret") or ""),
                card=card,
                proxy_url=proxy_url,
            )
        elif channel.channel_type == CHANNEL_TYPE_FEISHU_APP:
            card = build_feishu_notification_card(task, check, event)
            await post_feishu_app_message(
                app_id=str(secret.get("app_id") or ""),
                app_secret=str(secret.get("app_secret") or ""),
                receive_id_type=str(config.get("receive_id_type") or "chat_id"),
                receive_id=str(secret.get("receive_id") or ""),
                card=card,
                proxy_url=proxy_url,
            )
        else:
            return False, "通知渠道类型无效。"
        return True, ""
    except Exception as exc:
        return False, sanitize_proxy_error(exc)


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


async def send_monitoring_failure_notification(
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
            text=build_failure_message(task, check),
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


async def send_test_notification_channel(db: Session, channel: NotificationChannel, fernet: Fernet) -> None:
    check = MonitoringCheck(
        task_name_snapshot="测试通知",
        provider_name_snapshot="Keys",
        provider_base_url_snapshot="",
        model_id="test",
        client_profile="test",
        network_route="test",
        status="success",
        checked_at=utc_now(),
    )
    task = MonitoringTask(
        name="通知渠道测试",
        provider_name_snapshot="Keys",
        provider_base_url_snapshot="",
        model_id="test",
    )
    ok, error = await send_notification_channel_message(db, channel, task, check, fernet, event="recovery")
    if not ok:
        raise ValueError(error or "测试消息发送失败。")
