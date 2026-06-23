from __future__ import annotations

import httpx
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from .models import ConnectivityTest, NetworkProxy, Provider, ScheduledTask
from .proxy_support import build_proxy_url
from .security import decrypt_secret_with_fernet, get_setting, set_setting


async def send_telegram_notification(
    db: Session,
    provider: Provider,
    test: ConnectivityTest,
    fernet: Fernet
) -> bool:
    """
    发送 Telegram 通知（状态变化时）

    Returns:
        True 如果发送成功，False 如果跳过或失败
    """
    # 1. 检查是否启用
    enabled = get_setting(db, "telegram_enabled") == "true"
    if not enabled:
        return False

    # 2. 获取加密凭据
    encrypted_token = get_setting(db, "telegram_bot_token_encrypted")
    encrypted_chat_id = get_setting(db, "telegram_chat_id_encrypted")
    if not encrypted_token or not encrypted_chat_id:
        return False

    try:
        bot_token = decrypt_secret_with_fernet(encrypted_token, fernet)
        chat_id = decrypt_secret_with_fernet(encrypted_chat_id, fernet)
    except Exception:
        return False

    # 3. 检查状态是否变化
    setting_key = f"provider_{provider.id}_last_notified_status"
    last_notified = get_setting(db, setting_key) or ""

    if test.status == last_notified:
        return False  # 状态未变化，不发送

    # 4. 构建消息
    status_emoji = "✅" if test.status == "success" else "❌"
    status_text = "恢复可用" if test.status == "success" else "连接失败"

    message = f"""
{status_emoji} <b>中转站状态变化</b>

<b>站点</b>: {provider.name}
<b>状态</b>: {status_text}
<b>模型</b>: {test.model_id}
<b>延迟</b>: {test.latency_ms}ms
<b>时间</b>: {test.tested_at.strftime('%Y-%m-%d %H:%M:%S')}
""".strip()

    if test.status == "failed" and test.error_message:
        message += f"\n<b>错误</b>: {test.error_message[:200]}"

    # 5. 获取代理配置
    proxy_id_str = get_setting(db, "telegram_proxy_id")
    proxies = None

    if proxy_id_str:
        try:
            proxy_id = int(proxy_id_str)
            proxy = db.get(NetworkProxy, proxy_id)
            if proxy and proxy.enabled:
                proxy_url = build_proxy_url(proxy, fernet)
                proxies = {"all://": proxy_url}
        except Exception:
            pass  # 代理配置失败，使用直连

    # 6. 发送请求
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            proxies=proxies,
            trust_env=False
        ) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML"
                }
            )
            response.raise_for_status()

        # 7. 记录通知状态
        set_setting(db, setting_key, test.status)
        db.commit()

        return True
    except Exception:
        return False


def should_notify_for_task(
    task: ScheduledTask,
    test_status: str
) -> bool:
    """判断任务是否需要发送通知"""
    if not task.enable_telegram_notification:
        return False

    if test_status == "success" and task.notify_on_success:
        return True

    if test_status == "failed" and task.notify_on_failure:
        return True

    return False
