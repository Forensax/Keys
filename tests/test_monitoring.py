from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select

from app import main as main_module
from app import notifications as notifications_module
from app import scheduler as scheduler_module
from app.config import settings
from app.db import Base, SessionLocal, ensure_monitoring_columns
from app.main import app
from app.models import (
    ConnectivityTest,
    MonitoringCheck,
    MonitoringNotificationDelivery,
    MonitoringTask,
    MonitoringTaskNotificationChannel,
    NetworkProxy,
    NotificationChannel,
    Provider,
)
from app.openai_compat import CLIENT_PROFILE_CODEX, ConnectivityTestResult
from app.security import (
    authorize_scheduler_vault,
    encrypt_secret_with_fernet,
    get_scheduler_fernet,
    get_setting,
    set_setting,
)

def setup_client() -> TestClient:
    client = TestClient(app)
    client.__enter__()
    response = client.post(
        "/setup",
        data={"password": "long-test-password", "confirm_password": "long-test-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def create_provider(client: TestClient, *, enabled: bool = True) -> int:
    data = {
        "name": "Relay",
        "base_url": "https://relay.example/v1",
        "api_key": "sk-test-secret",
        "notes": "",
        "client_profile": "openai_chat",
    }
    if enabled:
        data["enabled"] = "on"
    response = client.post("/providers", data=data, follow_redirects=False)
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[-1])


def authorize_background() -> None:
    with SessionLocal() as db:
        authorize_scheduler_vault(db, "long-test-password", settings.session_secret)
        db.commit()


def create_telegram_channel(client: TestClient, *, name: str = "TG 主群") -> int:
    response = client.post(
        "/notification-channels",
        data={
            "name": name,
            "channel_type": "telegram",
            "bot_token": "123:token",
            "chat_id": "-100",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with SessionLocal() as db:
        channel = db.scalar(select(NotificationChannel).where(NotificationChannel.name == name))
        assert channel is not None
        return channel.id


def monitoring_fernet():
    with SessionLocal() as db:
        fernet = get_scheduler_fernet(db, settings.session_secret)
        assert fernet is not None
        return fernet


def assert_monitoring_panel_close_button(page_text: str) -> None:
    assert 'class="panel-close"' in page_text
    assert 'href="/monitoring"' in page_text
    assert 'aria-label="关闭并返回任务列表"' in page_text
    assert ">返回任务列表<" not in page_text


def test_monitoring_tables_are_created_and_migration_is_repeatable(tmp_path) -> None:
    legacy = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    Base.metadata.create_all(bind=legacy)
    ensure_monitoring_columns(legacy)
    ensure_monitoring_columns(legacy)

    tables = set(inspect(legacy).get_table_names())
    assert {
        "monitoring_tasks",
        "monitoring_checks",
        "notification_channels",
        "monitoring_task_notification_channels",
        "monitoring_notification_deliveries",
    } <= tables
    task_columns = {column["name"] for column in inspect(legacy).get_columns("monitoring_tasks")}
    check_columns = {column["name"] for column in inspect(legacy).get_columns("monitoring_checks")}
    assert {
        "model_id",
        "client_profile",
        "network_route",
        "current_success_notified",
        "current_failure_notified",
        "notify_on_recovery",
        "notify_on_failure",
        "retry_attempts",
        "retry_interval_seconds",
    } <= task_columns
    assert {
        "notification_status",
        "notification_error",
        "notification_event",
        "checked_at",
        "attempt_count",
        "notification_attempt_count",
    } <= check_columns
    delivery_columns = {column["name"] for column in inspect(legacy).get_columns("monitoring_notification_deliveries")}
    assert {"check_id", "channel_id", "event", "status", "attempt_count", "error", "sent_at"} <= delivery_columns


def test_monitoring_page_create_task_and_requires_vault_for_enabled_task() -> None:
    client = setup_client()
    provider_id = create_provider(client, enabled=False)

    page = client.get("/monitoring")
    assert page.status_code == 200
    assert "监控" in page.text
    assert '<a class="button" href="/monitoring?panel=vault">锁定后台密钥</a>' in page.text
    assert '<a class="button" href="/notification-channels">通知渠道</a>' in page.text
    assert '<a class="button primary" href="/monitoring?panel=new_task">新建监控任务</a>' in page.text
    assert "监控任务" in page.text
    assert "最近检测" in page.text
    assert "保存通知配置" not in page.text
    assert 'name="retry_attempts"' not in page.text
    assert 'name="retry_interval_seconds"' not in page.text
    assert 'name="notify_on_recovery"' not in page.text
    assert 'name="notify_on_failure"' not in page.text

    failed_authorize = client.post(
        "/monitoring/authorize",
        data={"password": "wrong-password"},
        follow_redirects=False,
    )
    assert failed_authorize.status_code == 303
    assert failed_authorize.headers["location"] == "/monitoring?panel=vault"
    locked = client.post("/monitoring/lock", follow_redirects=False)
    assert locked.status_code == 303
    assert locked.headers["location"] == "/monitoring?panel=vault"

    vault_panel = client.get("/monitoring?panel=vault")
    assert vault_panel.status_code == 200
    assert "后台密钥" in vault_panel.text
    assert_monitoring_panel_close_button(vault_panel.text)
    assert "保存通知配置" not in vault_panel.text
    assert 'name="retry_attempts"' not in vault_panel.text

    invalid_panel = client.get("/monitoring?panel=telegram")
    assert invalid_panel.status_code == 200
    assert "监控任务" in invalid_panel.text
    assert "保存通知配置" not in invalid_panel.text

    new_task_panel = client.get("/monitoring?panel=new_task")
    assert new_task_panel.status_code == 200
    assert "新建监控任务" in new_task_panel.text
    assert "/monitoring/tasks" in new_task_panel.text
    assert_monitoring_panel_close_button(new_task_panel.text)
    assert 'name="retry_attempts"' in new_task_panel.text
    assert 'name="retry_interval_seconds"' in new_task_panel.text
    assert 'name="notify_on_recovery"' in new_task_panel.text
    assert 'name="notify_on_failure"' in new_task_panel.text
    assert "保存通知配置" not in new_task_panel.text

    no_notification_type = client.post(
        "/monitoring/tasks",
        data={
            "name": "watch relay",
            "provider_id": str(provider_id),
            "model_id": "gpt-watch",
            "client_profile": CLIENT_PROFILE_CODEX,
            "network_route": "direct",
            "interval_minutes": "5",
            "retry_attempts": "1",
            "retry_interval_seconds": "10",
            "notification_preferences_present": "1",
        },
        follow_redirects=False,
    )
    assert no_notification_type.status_code == 400
    assert "请至少选择一种通知类型" in no_notification_type.text
    assert 'name="retry_attempts"' in no_notification_type.text
    assert "保存通知配置" not in no_notification_type.text

    invalid_retry = client.post(
        "/monitoring/tasks",
        data={
            "name": "watch relay",
            "provider_id": str(provider_id),
            "model_id": "gpt-watch",
            "client_profile": CLIENT_PROFILE_CODEX,
            "network_route": "direct",
            "interval_minutes": "5",
            "retry_attempts": "6",
            "retry_interval_seconds": "0",
        },
        follow_redirects=False,
    )
    assert invalid_retry.status_code == 400
    assert "尝试次数必须在 1 到 5 次之间" in invalid_retry.text
    assert "重试间隔必须在 1 到 300 秒之间" in invalid_retry.text

    blocked = client.post(
        "/monitoring/tasks",
        data={
            "name": "watch relay",
            "provider_id": str(provider_id),
            "model_id": "gpt-watch",
            "client_profile": CLIENT_PROFILE_CODEX,
            "network_route": "direct",
            "interval_minutes": "5",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert blocked.status_code == 400
    assert "请先授权后台密钥" in blocked.text

    created = client.post(
        "/monitoring/tasks",
        data={
            "name": "watch relay",
            "provider_id": str(provider_id),
            "model_id": "gpt-watch",
            "client_profile": CLIENT_PROFILE_CODEX,
            "network_route": "direct",
            "interval_minutes": "5",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    with SessionLocal() as db:
        task = db.scalar(select(MonitoringTask).where(MonitoringTask.name == "watch relay"))
        assert task is not None
        assert task.enabled is False
        assert task.next_run_at is None
        assert task.provider_id == provider_id
        assert task.model_id == "gpt-watch"
        assert task.retry_attempts == 1
        assert task.retry_interval_seconds == 10
        assert task.notify_on_recovery is True
        assert task.notify_on_failure is False
        task_id = task.id

    task_page = client.get("/monitoring")
    assert task_page.status_code == 200
    table_start = task_page.text.index('<table class="monitoring-table">')
    table_end = task_page.text.index("</table>", table_start)
    task_table = task_page.text[table_start:table_end]
    assert "<th>任务</th><th>模型</th><th>周期</th><th>状态</th><th>下次检测</th><th></th>" in task_table
    assert "<th>中转站</th>" not in task_table
    assert "<th>通知</th>" not in task_table
    assert "<th>最近通知</th>" not in task_table
    assert "watch relay" in task_table
    assert "Relay" in task_table
    assert "gpt-watch" in task_table
    assert "monitoring-period-cell" in task_table
    assert "每 5 分钟 · 重试 1 次" in task_table
    assert "monitoring-timeline-row" in task_table
    assert "过去 24 小时" in task_table
    assert "暂无可用率 · 故障 0 次" in task_table
    assert "monitoring-timeline-lane is-success" in task_table
    assert "monitoring-timeline-lane is-failed" in task_table
    assert "monitoring-channel-summary" in task_table
    assert "未选择渠道" in task_table
    assert '<span class="muted small">恢复</span>' not in task_table
    assert "恢复/不可用" not in task_table
    assert "立即检测" in task_table
    assert "停用" in task_table
    assert "编辑" in task_table
    assert "删除" in task_table
    assert '<section class="panel monitoring-task-panel">' in task_page.text
    assert '<section class="panel monitoring-recent-panel">' in task_page.text

    styles = Path("app/static/styles.css").read_text(encoding="utf-8")
    assert ".monitoring-table,\n.monitoring-check-table" not in styles
    assert ".monitoring-table {\n  min-width: 0;" in styles
    assert ".monitoring-task-panel {\n  margin-bottom: 12px;" in styles
    assert ".monitoring-recent-panel {\n  margin-top: 0;" in styles
    assert ".monitoring-table .schedule-actions {\n  flex-wrap: nowrap;" in styles
    assert ".monitoring-table .schedule-actions button.small,\n.monitoring-table .schedule-actions .button.small" in styles
    assert "line-height: 1.2;" in styles
    assert ".monitoring-period-cell span {\n  display: block;" in styles
    assert ".monitoring-timeline-track" in styles
    assert ".monitoring-timeline-plot" in styles
    assert ".monitoring-timeline-lane.is-success" in styles
    assert ".monitoring-timeline-lane.is-failed" in styles
    assert ".monitoring-timeline-segment.is-success" in styles
    assert ".monitoring-timeline-segment.is-neutral" not in styles
    assert ".monitoring-task-error" in styles
    assert ".response-cell" in styles
    assert ".response-preview" in styles
    assert ".dismissible-panel {\n  position: relative;" in styles
    assert ".panel-close" in styles
    assert "white-space: nowrap;" in styles
    assert ".monitoring-check-table" in styles and "min-width: 1040px" in styles

    edit_page = client.get(f"/monitoring/tasks/{task_id}/edit")
    assert edit_page.status_code == 200
    assert 'name="retry_attempts"' in edit_page.text
    assert 'name="retry_interval_seconds"' in edit_page.text
    assert 'name="notify_on_recovery"' in edit_page.text
    assert 'name="notify_on_failure"' in edit_page.text

    client.close()


def test_notification_channel_page_creates_and_tests_tg_and_feishu_channels(monkeypatch) -> None:
    client = setup_client()

    page = client.get("/notification-channels")
    assert page.status_code == 200
    assert "通知渠道" in page.text
    assert '<a class="button primary" href="/notification-channels?panel=new_channel">新建通知渠道</a>' in page.text
    assert 'data-notification-channel-form' not in page.text
    assert "创建渠道" not in page.text
    assert "Bot Token" not in page.text

    new_panel = client.get("/notification-channels?panel=new_channel")
    assert new_panel.status_code == 200
    assert "新建通知渠道" in new_panel.text
    assert 'class="panel-close"' in new_panel.text
    assert 'href="/notification-channels"' in new_panel.text
    assert 'data-notification-channel-form' in new_panel.text
    assert 'data-notification-channel-type' in new_panel.text
    assert 'data-channel-type-fields="telegram"' in new_panel.text
    assert 'data-channel-type-fields="feishu_webhook" hidden' in new_panel.text
    assert 'data-channel-type-fields="feishu_app" hidden' in new_panel.text
    scripts = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "[data-notification-channel-form]" in scripts
    assert "[data-channel-type-fields]" in scripts
    assert "group.hidden = group.dataset.channelTypeFields !== typeSelect.value" in scripts

    telegram_id = create_telegram_channel(client)
    webhook = client.post(
        "/notification-channels",
        data={
            "name": "飞书值班群",
            "channel_type": "feishu_webhook",
            "webhook_token": "hook-token",
            "signing_secret": "signing-secret",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert webhook.status_code == 303
    app_bot = client.post(
        "/notification-channels",
        data={
            "name": "飞书应用机器人",
            "channel_type": "feishu_app",
            "app_id": "cli_test",
            "app_secret": "app-secret",
            "receive_id_type": "chat_id",
            "receive_id": "oc_test",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert app_bot.status_code == 303

    with SessionLocal() as db:
        channels = list(db.scalars(select(NotificationChannel).order_by(NotificationChannel.name)))
        assert len(channels) == 3
        assert all(channel.encrypted_secret_json for channel in channels)
        assert all("secret" not in channel.encrypted_secret_json for channel in channels)
        webhook_channel = db.scalar(select(NotificationChannel).where(NotificationChannel.name == "飞书值班群"))
        app_channel = db.scalar(select(NotificationChannel).where(NotificationChannel.name == "飞书应用机器人"))
        assert webhook_channel is not None
        assert app_channel is not None
        webhook_id = webhook_channel.id
        app_id = app_channel.id

    test_calls: list[tuple[str, str]] = []

    async def fake_send_test_notification_channel(db, channel, fernet):
        test_calls.append((channel.name, channel.channel_type))

    monkeypatch.setattr(main_module, "send_test_notification_channel", fake_send_test_notification_channel)

    assert client.post(f"/notification-channels/{telegram_id}/test", follow_redirects=False).status_code == 303
    assert client.post(f"/notification-channels/{webhook_id}/test", follow_redirects=False).status_code == 303
    assert client.post(f"/notification-channels/{app_id}/test", follow_redirects=False).status_code == 303
    assert test_calls == [
        ("TG 主群", "telegram"),
        ("飞书值班群", "feishu_webhook"),
        ("飞书应用机器人", "feishu_app"),
    ]

    telegram_edit = client.get(f"/notification-channels/{telegram_id}/edit")
    assert 'data-channel-type-fields="telegram"' in telegram_edit.text
    assert 'data-channel-type-fields="feishu_webhook" hidden' in telegram_edit.text
    webhook_edit = client.get(f"/notification-channels/{webhook_id}/edit")
    assert 'data-channel-type-fields="telegram" hidden' in webhook_edit.text
    assert 'data-channel-type-fields="feishu_webhook"' in webhook_edit.text
    app_edit = client.get(f"/notification-channels/{app_id}/edit")
    assert 'data-channel-type-fields="feishu_app"' in app_edit.text
    assert 'data-channel-type-fields="feishu_webhook" hidden' in app_edit.text

    invalid = client.post(
        "/notification-channels",
        data={"name": "broken tg", "channel_type": "telegram", "enabled": "on"},
        follow_redirects=False,
    )
    assert invalid.status_code == 400
    assert "Telegram 渠道需要 Bot Token 和 Chat ID" in invalid.text
    assert 'data-notification-channel-form' in invalid.text
    assert 'data-channel-type-fields="telegram"' in invalid.text

    rendered = client.get("/notification-channels")
    assert '<table class="notification-channel-table">' in rendered.text
    assert "TG 主群" in rendered.text
    assert "Telegram" in rendered.text
    assert "启用" in rendered.text
    assert "直连" in rendered.text
    assert "已配置" in rendered.text
    assert "测试" in rendered.text
    assert "停用" in rendered.text
    assert "编辑" in rendered.text
    assert "删除" in rendered.text
    assert "123:token" not in rendered.text
    assert "hook-token" not in rendered.text
    assert "app-secret" not in rendered.text
    assert 'data-notification-channel-form' not in rendered.text
    styles = Path("app/static/styles.css").read_text(encoding="utf-8")
    assert ".notification-channel-table {\n  min-width: 0;" in styles
    assert ".notification-channel-table .schedule-actions {\n  flex-wrap: nowrap;" in styles
    assert (
        ".notification-channel-table .schedule-actions button.small,\n"
        ".notification-channel-table .schedule-actions .button.small"
    ) in styles
    assert ".schedule-run-table,\n.monitoring-check-table {\n  min-width: 1040px;" in styles
    client.close()


def test_monitoring_task_edit_updates_notification_channels_without_duplicates() -> None:
    client = setup_client()
    provider_id = create_provider(client)
    telegram_id = create_telegram_channel(client)
    webhook = client.post(
        "/notification-channels",
        data={
            "name": "飞书 Webhook",
            "channel_type": "feishu_webhook",
            "webhook_token": "hook-token",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert webhook.status_code == 303
    with SessionLocal() as db:
        webhook_channel = db.scalar(select(NotificationChannel).where(NotificationChannel.name == "飞书 Webhook"))
        assert webhook_channel is not None
        webhook_id = webhook_channel.id

    created = client.post(
        "/monitoring/tasks",
        data={
            "name": "watch channels",
            "provider_id": str(provider_id),
            "model_id": "gpt-watch",
            "client_profile": CLIENT_PROFILE_CODEX,
            "network_route": "direct",
            "interval_minutes": "5",
            "retry_attempts": "2",
            "retry_interval_seconds": "10",
            "notification_preferences_present": "1",
            "notify_on_recovery": "on",
            "notification_channel_ids": str(telegram_id),
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    with SessionLocal() as db:
        task = db.scalar(select(MonitoringTask).where(MonitoringTask.name == "watch channels"))
        assert task is not None
        task_id = task.id
        links = list(
            db.scalars(
                select(MonitoringTaskNotificationChannel).where(
                    MonitoringTaskNotificationChannel.task_id == task_id
                )
            )
        )
        assert [(link.channel_id, link.channel_name_snapshot) for link in links] == [(telegram_id, "TG 主群")]

    update_common = {
        "name": "watch channels",
        "provider_id": str(provider_id),
        "model_id": "gpt-watch",
        "client_profile": CLIENT_PROFILE_CODEX,
        "network_route": "direct",
        "interval_minutes": "5",
        "retry_attempts": "2",
        "retry_interval_seconds": "10",
        "notification_preferences_present": "1",
        "notify_on_recovery": "on",
    }
    expanded = client.post(
        f"/monitoring/tasks/{task_id}",
        data={**update_common, "notification_channel_ids": [str(telegram_id), str(webhook_id)]},
        follow_redirects=False,
    )
    assert expanded.status_code == 303
    with SessionLocal() as db:
        links = list(
            db.scalars(
                select(MonitoringTaskNotificationChannel)
                .where(MonitoringTaskNotificationChannel.task_id == task_id)
                .order_by(MonitoringTaskNotificationChannel.channel_name_snapshot)
            )
        )
        assert [(link.channel_id, link.channel_name_snapshot) for link in links] == [
            (telegram_id, "TG 主群"),
            (webhook_id, "飞书 Webhook"),
        ]

    reduced = client.post(
        f"/monitoring/tasks/{task_id}",
        data={**update_common, "notification_channel_ids": [str(webhook_id)]},
        follow_redirects=False,
    )
    assert reduced.status_code == 303
    with SessionLocal() as db:
        links = list(
            db.scalars(
                select(MonitoringTaskNotificationChannel).where(
                    MonitoringTaskNotificationChannel.task_id == task_id
                )
            )
        )
        assert [(link.channel_id, link.channel_name_snapshot) for link in links] == [(webhook_id, "飞书 Webhook")]
        duplicate_count = len(
            list(
                db.scalars(
                    select(MonitoringTaskNotificationChannel).where(
                        MonitoringTaskNotificationChannel.task_id == task_id,
                        MonitoringTaskNotificationChannel.channel_id == webhook_id,
                    )
                )
            )
        )
        assert duplicate_count == 1
    client.close()


def test_send_test_notification_channel_sets_checked_time(monkeypatch) -> None:
    fernet = Fernet(Fernet.generate_key())
    captured: dict[str, str | None] = {}
    with SessionLocal() as db:
        channel = NotificationChannel(
            name="TG 测试",
            channel_type="telegram",
            enabled=True,
            config_json="{}",
            encrypted_secret_json=encrypt_secret_with_fernet(
                json.dumps({"bot_token": "123:token", "chat_id": "-100"}),
                fernet,
            ),
        )
        db.add(channel)
        db.commit()
        channel_id = channel.id

    async def fake_post_telegram_message(*, bot_token: str, chat_id: str, text: str, proxy_url: str | None = None):
        captured["bot_token"] = bot_token
        captured["chat_id"] = chat_id
        captured["text"] = text
        captured["proxy_url"] = proxy_url

    monkeypatch.setattr(notifications_module, "post_telegram_message", fake_post_telegram_message)

    with SessionLocal() as db:
        channel = db.get(NotificationChannel, channel_id)
        assert channel is not None
        asyncio.run(notifications_module.send_test_notification_channel(db, channel, fernet))

    assert captured["bot_token"] == "123:token"
    assert captured["chat_id"] == "-100"
    assert captured["proxy_url"] is None
    assert captured["text"] is not None
    assert "时间：" in captured["text"]
    time_line = next(line for line in captured["text"].splitlines() if line.startswith("时间："))
    assert "None" not in time_line
    assert time_line != "时间：未知"


def test_feishu_notification_card_uses_card_layout_without_telegram_html() -> None:
    task = MonitoringTask(name="Any Router 监控", provider_name_snapshot="Any Router", model_id="gpt-5.5")
    check = MonitoringCheck(
        task_name_snapshot="Any Router 监控",
        provider_name_snapshot="Any Router",
        provider_base_url_snapshot="https://relay.example/v1",
        model_id="gpt-5.5",
        client_profile="codex",
        network_route="直连",
        status="success",
        latency_ms=4208,
        checked_at=datetime(2026, 7, 6, 10, 52, 20, tzinfo=timezone.utc),
    )

    card = notifications_module.build_feishu_notification_card(task, check, "recovery")
    rendered = json.dumps(card, ensure_ascii=False)

    assert card["config"]["wide_screen_mode"] is False
    assert card["header"]["template"] == "green"
    assert card["header"]["title"]["content"] == "中转站恢复可用"
    assert "fields" not in card["elements"][0]
    assert card["elements"][0]["text"]["tag"] == "lark_md"
    assert "**状态**：恢复可用" in card["elements"][0]["text"]["content"]
    assert "<b>" not in rendered
    assert "Any Router 监控" in rendered
    assert "4208 ms" in rendered
    assert "2026-07-06 10:52:20" in rendered


def test_post_feishu_webhook_message_sends_interactive_card(monkeypatch) -> None:
    captured: dict[str, object] = {}
    card = {"config": {"wide_screen_mode": False}, "elements": []}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, json):
            captured["url"] = url
            captured["json"] = json
            return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))

    monkeypatch.setattr(notifications_module.httpx, "AsyncClient", FakeAsyncClient)

    asyncio.run(
        notifications_module.post_feishu_webhook_message(
            webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
            card=card,
            proxy_url="http://proxy.example:1080",
        )
    )

    assert captured["client_kwargs"]["proxy"] == "http://proxy.example:1080"
    assert captured["json"] == {"msg_type": "interactive", "card": card}


def test_post_feishu_app_message_sends_interactive_card(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    card = {"config": {"wide_screen_mode": False}, "elements": []}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            if url.endswith("/tenant_access_token/internal"):
                return httpx.Response(
                    200,
                    json={"code": 0, "tenant_access_token": "tenant-token"},
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))

    monkeypatch.setattr(notifications_module.httpx, "AsyncClient", FakeAsyncClient)

    asyncio.run(
        notifications_module.post_feishu_app_message(
            app_id="cli_test",
            app_secret="app-secret",
            receive_id_type="chat_id",
            receive_id="oc_test",
            card=card,
        )
    )

    message_payload = calls[1]["json"]
    assert message_payload["receive_id"] == "oc_test"
    assert message_payload["msg_type"] == "interactive"
    assert json.loads(message_payload["content"]) == card


def test_monitoring_page_renders_24_hour_status_timeline() -> None:
    client = setup_client()
    provider_id = create_provider(client)
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        provider = db.get(Provider, provider_id)
        assert provider is not None
        task = MonitoringTask(
            name="timeline relay",
            enabled=True,
            provider_id=provider.id,
            provider_name_snapshot=provider.name,
            provider_base_url_snapshot=provider.base_url,
            model_id="gpt-watch",
            client_profile=CLIENT_PROFILE_CODEX,
            network_route="direct",
            interval_minutes=5,
            next_run_at=now + timedelta(minutes=5),
            last_status="failed",
            last_error='HTTP 500: {"error":{"message":"当前模型 gpt-5.5 负载已经达到上限，请稍后重试"}}',
        )
        db.add(task)
        db.flush()
        checks = [
            MonitoringCheck(
                task_id=task.id,
                task_name_snapshot=task.name,
                provider_id=provider.id,
                provider_name_snapshot=provider.name,
                provider_base_url_snapshot=provider.base_url,
                model_id=task.model_id,
                client_profile=task.client_profile,
                network_route=task.network_route,
                status="success",
                latency_ms=120,
                raw_response_excerpt="",
                checked_at=now - timedelta(hours=6),
            ),
            MonitoringCheck(
                task_id=task.id,
                task_name_snapshot=task.name,
                provider_id=provider.id,
                provider_name_snapshot=provider.name,
                provider_base_url_snapshot=provider.base_url,
                model_id=task.model_id,
                client_profile=task.client_profile,
                network_route=task.network_route,
                status="failed",
                error_message="upstream unavailable",
                raw_response_excerpt='{"error":"hidden detail"}',
                notification_status="failed",
                notification_error="RuntimeError: TG down",
                checked_at=now - timedelta(hours=4),
            ),
            MonitoringCheck(
                task_id=task.id,
                task_name_snapshot=task.name,
                provider_id=provider.id,
                provider_name_snapshot=provider.name,
                provider_base_url_snapshot=provider.base_url,
                model_id=task.model_id,
                client_profile=task.client_profile,
                network_route=task.network_route,
                status="skipped",
                error_message="中转站已归档。",
                checked_at=now - timedelta(hours=3),
            ),
            MonitoringCheck(
                task_id=task.id,
                task_name_snapshot=task.name,
                provider_id=provider.id,
                provider_name_snapshot=provider.name,
                provider_base_url_snapshot=provider.base_url,
                model_id=task.model_id,
                client_profile=task.client_profile,
                network_route=task.network_route,
                status="success",
                latency_ms=98,
                raw_response_excerpt='{"output":"healthy"}',
                checked_at=now - timedelta(hours=2),
            ),
        ]
        db.add_all(checks)
        db.commit()

    page = client.get("/monitoring")
    assert page.status_code == 200
    table_start = page.text.index('<table class="monitoring-table">')
    table_end = page.text.index("</table>", table_start)
    task_table = page.text[table_start:table_end]
    assert "monitoring-timeline-row" in task_table
    assert "monitoring-timeline-track" in task_table
    assert "monitoring-timeline-lane is-success" in task_table
    assert "monitoring-timeline-lane is-failed" in task_table
    assert "monitoring-timeline-segment is-success" in task_table
    assert "monitoring-timeline-segment is-failed" in task_table
    assert "monitoring-timeline-segment is-neutral is-skipped" not in task_table
    assert "monitoring-timeline-marker is-failed" in task_table
    assert "monitoring-timeline-marker is-success" in task_table
    assert 'data-response-tooltip="可用状态线 · 绿色区段表示检测成功"' in task_table
    assert 'data-response-tooltip="不可用状态线 · 红色区段表示检测失败"' in task_table
    assert 'data-response-tooltip="' in task_table
    assert 'title="upstream unavailable' not in task_table
    assert 'aria-label="不可用"></span>' in task_table
    assert 'aria-label="恢复"></span>' in task_table
    assert "monitoring-task-error" in task_table
    assert 'title="HTTP 500: {&#34;error&#34;:{&#34;message&#34;:&#34;当前模型 gpt-5.5 负载已经达到上限，请稍后重试&#34;}}"' in task_table
    assert "不可用" in task_table
    assert "恢复" in task_table
    assert "可用率 66.7% · 故障 1 次" in task_table
    recent_start = page.text.index('<table class="monitoring-check-table">')
    recent_end = page.text.index("</table>", recent_start)
    recent_table = page.text[recent_start:recent_end]
    assert "<th>响应</th>" in recent_table
    assert "<th>错误</th>" not in recent_table
    assert "response-cell" in recent_table
    assert "response-preview" in recent_table
    assert "{&#34;output&#34;:&#34;healthy&#34;}" in recent_table
    assert "成功（响应正文为空）" in recent_table
    assert "upstream unavailable" in recent_table
    assert "{&#34;error&#34;:&#34;hidden detail&#34;}" not in recent_table
    assert 'data-response-tooltip="RuntimeError: TG down"' in recent_table
    assert "monitoring-check-error" not in page.text
    assert ".monitoring-check-table" in Path("app/static/styles.css").read_text(encoding="utf-8")
    client.close()


def test_monitoring_checks_are_independent_and_recovery_notifications_are_stateful(monkeypatch) -> None:
    client = setup_client()
    provider_id = create_provider(client)
    authorize_background()
    fernet = monitoring_fernet()
    telegram_calls: list[dict[str, str | None]] = []

    async def fake_post_telegram_message(*, bot_token, chat_id, text, proxy_url=None):
        telegram_calls.append({"bot_token": bot_token, "chat_id": chat_id, "text": text, "proxy_url": proxy_url})

    results = [
        ConnectivityTestResult("success", 11, "", '{"result":"pong"}'),
        ConnectivityTestResult("success", 12, "", '{"result":"pong"}'),
        ConnectivityTestResult("failed", None, "暂不可用", ""),
        ConnectivityTestResult("success", 13, "", '{"result":"pong"}'),
    ]

    async def fake_run_connectivity_test(*args, **kwargs):
        return results.pop(0)

    monkeypatch.setattr(notifications_module, "post_telegram_message", fake_post_telegram_message)
    monkeypatch.setattr(scheduler_module, "run_connectivity_test", fake_run_connectivity_test)
    with SessionLocal() as db:
        db.add(
            MonitoringTask(
                name="watch relay",
                enabled=True,
                provider_id=provider_id,
                provider_name_snapshot="Relay",
                provider_base_url_snapshot="https://relay.example/v1",
                model_id="gpt-watch",
                client_profile=CLIENT_PROFILE_CODEX,
                network_route="direct",
                interval_minutes=5,
            )
        )
        db.flush()
        task_id = db.scalar(select(MonitoringTask.id))
        assert task_id is not None
        db.commit()
        from app.notifications import SETTING_TELEGRAM_BOT_TOKEN, SETTING_TELEGRAM_CHAT_ID, SETTING_TELEGRAM_ENABLED

        set_setting(db, SETTING_TELEGRAM_ENABLED, "true")
        set_setting(db, SETTING_TELEGRAM_BOT_TOKEN, encrypt_secret_with_fernet("123:token", fernet))
        set_setting(db, SETTING_TELEGRAM_CHAT_ID, encrypt_secret_with_fernet("-100", fernet))
        db.commit()

    for _ in range(4):
        asyncio.run(scheduler_module.execute_monitoring_task_run(task_id, fernet=fernet))

    with SessionLocal() as db:
        task = db.get(MonitoringTask, task_id)
        assert task is not None
        checks = list(
            db.scalars(select(MonitoringCheck).where(MonitoringCheck.task_id == task_id).order_by(MonitoringCheck.id))
        )
        assert [check.status for check in checks] == ["success", "success", "failed", "success"]
        assert [check.notification_status for check in checks] == ["sent", "skipped", "skipped", "sent"]
        assert task.current_success_notified is True
        assert task.last_status == "success"
        assert db.scalar(select(ConnectivityTest)) is None
    assert len(telegram_calls) == 2
    assert "中转站恢复可用" in str(telegram_calls[0]["text"])
    client.close()


def test_monitoring_retries_api_failures_and_records_final_check(monkeypatch) -> None:
    client = setup_client()
    provider_id = create_provider(client)
    authorize_background()
    fernet = monitoring_fernet()
    sleeps: list[int] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    results = [
        ConnectivityTestResult("failed", None, "first failure", ""),
        ConnectivityTestResult("failed", None, "second failure", ""),
        ConnectivityTestResult("success", 15, "", '{"result":"pong"}'),
    ]

    async def fake_run_connectivity_test(*args, **kwargs):
        return results.pop(0)

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(scheduler_module, "run_connectivity_test", fake_run_connectivity_test)
    with SessionLocal() as db:
        task = MonitoringTask(
            name="watch retry",
            enabled=True,
            provider_id=provider_id,
            provider_name_snapshot="Relay",
            provider_base_url_snapshot="https://relay.example/v1",
            model_id="gpt-watch",
            client_profile=CLIENT_PROFILE_CODEX,
            network_route="direct",
            interval_minutes=5,
            retry_attempts=3,
            retry_interval_seconds=1,
        )
        db.add(task)
        db.commit()
        task_id = task.id

    asyncio.run(scheduler_module.execute_monitoring_task_run(task_id, fernet=fernet))

    with SessionLocal() as db:
        checks = list(db.scalars(select(MonitoringCheck).where(MonitoringCheck.task_id == task_id)))
        assert len(checks) == 1
        assert checks[0].status == "success"
        assert checks[0].attempt_count == 3
        assert checks[0].latency_ms == 15
        assert db.scalar(select(ConnectivityTest)) is None
    assert sleeps == [1, 1]
    client.close()


def test_monitoring_failure_notifications_are_stateful_and_reset_after_recovery(monkeypatch) -> None:
    client = setup_client()
    provider_id = create_provider(client)
    authorize_background()
    fernet = monitoring_fernet()
    telegram_calls: list[str] = []

    async def fake_post_telegram_message(*, bot_token, chat_id, text, proxy_url=None):
        telegram_calls.append(text)

    results = [
        ConnectivityTestResult("failed", None, "down once", ""),
        ConnectivityTestResult("failed", None, "still down", ""),
        ConnectivityTestResult("success", 11, "", '{"result":"pong"}'),
        ConnectivityTestResult("failed", None, "down again", ""),
    ]

    async def fake_run_connectivity_test(*args, **kwargs):
        return results.pop(0)

    monkeypatch.setattr(notifications_module, "post_telegram_message", fake_post_telegram_message)
    monkeypatch.setattr(scheduler_module, "run_connectivity_test", fake_run_connectivity_test)
    with SessionLocal() as db:
        from app.notifications import SETTING_TELEGRAM_BOT_TOKEN, SETTING_TELEGRAM_CHAT_ID, SETTING_TELEGRAM_ENABLED

        set_setting(db, SETTING_TELEGRAM_ENABLED, "true")
        set_setting(db, SETTING_TELEGRAM_BOT_TOKEN, encrypt_secret_with_fernet("123:token", fernet))
        set_setting(db, SETTING_TELEGRAM_CHAT_ID, encrypt_secret_with_fernet("-100", fernet))
        task = MonitoringTask(
            name="watch failure",
            enabled=True,
            provider_id=provider_id,
            provider_name_snapshot="Relay",
            provider_base_url_snapshot="https://relay.example/v1",
            model_id="gpt-watch",
            client_profile=CLIENT_PROFILE_CODEX,
            network_route="direct",
            interval_minutes=5,
            notify_on_recovery=False,
            notify_on_failure=True,
        )
        db.add(task)
        db.commit()
        task_id = task.id

    for _ in range(4):
        asyncio.run(scheduler_module.execute_monitoring_task_run(task_id, fernet=fernet))

    with SessionLocal() as db:
        task = db.get(MonitoringTask, task_id)
        assert task is not None
        checks = list(
            db.scalars(select(MonitoringCheck).where(MonitoringCheck.task_id == task_id).order_by(MonitoringCheck.id))
        )
        assert [check.status for check in checks] == ["failed", "failed", "success", "failed"]
        assert [check.notification_status for check in checks] == ["sent", "skipped", "skipped", "sent"]
        assert [check.notification_event for check in checks] == ["failure", "", "", "failure"]
        assert task.current_failure_notified is True
        assert task.current_success_notified is False
    assert len(telegram_calls) == 2
    assert all("中转站变为不可用" in text for text in telegram_calls)
    client.close()


def test_monitoring_failure_notification_retries_and_records_failure_event(monkeypatch) -> None:
    client = setup_client()
    provider_id = create_provider(client)
    authorize_background()
    fernet = monitoring_fernet()
    telegram_calls = 0

    async def fake_sleep(seconds):
        return None

    async def fake_run_connectivity_test(*args, **kwargs):
        return ConnectivityTestResult("failed", None, "down", "")

    async def fake_post_telegram_message(*, bot_token, chat_id, text, proxy_url=None):
        nonlocal telegram_calls
        telegram_calls += 1
        raise RuntimeError("TG down")

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(notifications_module, "post_telegram_message", fake_post_telegram_message)
    monkeypatch.setattr(scheduler_module, "run_connectivity_test", fake_run_connectivity_test)
    with SessionLocal() as db:
        from app.notifications import SETTING_TELEGRAM_BOT_TOKEN, SETTING_TELEGRAM_CHAT_ID, SETTING_TELEGRAM_ENABLED

        set_setting(db, SETTING_TELEGRAM_ENABLED, "true")
        set_setting(db, SETTING_TELEGRAM_BOT_TOKEN, encrypt_secret_with_fernet("123:token", fernet))
        set_setting(db, SETTING_TELEGRAM_CHAT_ID, encrypt_secret_with_fernet("-100", fernet))
        task = MonitoringTask(
            name="watch failed tg",
            enabled=True,
            provider_id=provider_id,
            provider_name_snapshot="Relay",
            provider_base_url_snapshot="https://relay.example/v1",
            model_id="gpt-watch",
            client_profile=CLIENT_PROFILE_CODEX,
            network_route="direct",
            interval_minutes=5,
            retry_attempts=2,
            retry_interval_seconds=1,
            notify_on_recovery=False,
            notify_on_failure=True,
        )
        db.add(task)
        db.commit()
        task_id = task.id

    asyncio.run(scheduler_module.execute_monitoring_task_run(task_id, fernet=fernet))

    with SessionLocal() as db:
        check = db.scalar(select(MonitoringCheck).where(MonitoringCheck.task_id == task_id))
        assert check is not None
        assert check.status == "failed"
        assert check.notification_event == "failure"
        assert check.notification_status == "failed"
        assert check.notification_attempt_count == 2
        assert check.notification_error == "默认 Telegram: RuntimeError: TG down"
    assert telegram_calls == 2
    client.close()


def test_monitoring_retries_until_api_attempts_are_exhausted(monkeypatch) -> None:
    client = setup_client()
    provider_id = create_provider(client)
    authorize_background()
    fernet = monitoring_fernet()

    async def fake_sleep(seconds):
        return None

    results = [
        ConnectivityTestResult("failed", None, "first failure", ""),
        ConnectivityTestResult("failed", None, "final failure", ""),
    ]

    async def fake_run_connectivity_test(*args, **kwargs):
        return results.pop(0)

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(scheduler_module, "run_connectivity_test", fake_run_connectivity_test)
    with SessionLocal() as db:
        task = MonitoringTask(
            name="watch retry failure",
            enabled=True,
            provider_id=provider_id,
            provider_name_snapshot="Relay",
            provider_base_url_snapshot="https://relay.example/v1",
            model_id="gpt-watch",
            client_profile=CLIENT_PROFILE_CODEX,
            network_route="direct",
            interval_minutes=5,
            retry_attempts=2,
            retry_interval_seconds=1,
        )
        db.add(task)
        db.commit()
        task_id = task.id

    asyncio.run(scheduler_module.execute_monitoring_task_run(task_id, fernet=fernet))

    with SessionLocal() as db:
        check = db.scalar(select(MonitoringCheck).where(MonitoringCheck.task_id == task_id))
        assert check is not None
        assert check.status == "failed"
        assert check.error_message == "final failure"
        assert check.attempt_count == 2
    client.close()


def test_monitoring_retries_failed_telegram_notification_without_rerunning_api(monkeypatch) -> None:
    client = setup_client()
    provider_id = create_provider(client)
    authorize_background()
    fernet = monitoring_fernet()
    api_calls = 0
    telegram_calls = 0
    sleeps: list[int] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def fake_run_connectivity_test(*args, **kwargs):
        nonlocal api_calls
        api_calls += 1
        return ConnectivityTestResult("success", 12, "", '{"result":"pong"}')

    async def fake_post_telegram_message(*, bot_token, chat_id, text, proxy_url=None):
        nonlocal telegram_calls
        telegram_calls += 1
        if telegram_calls < 3:
            raise RuntimeError("TG down")

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(notifications_module, "post_telegram_message", fake_post_telegram_message)
    monkeypatch.setattr(scheduler_module, "run_connectivity_test", fake_run_connectivity_test)
    with SessionLocal() as db:
        from app.notifications import SETTING_TELEGRAM_BOT_TOKEN, SETTING_TELEGRAM_CHAT_ID, SETTING_TELEGRAM_ENABLED

        set_setting(db, SETTING_TELEGRAM_ENABLED, "true")
        set_setting(db, SETTING_TELEGRAM_BOT_TOKEN, encrypt_secret_with_fernet("123:token", fernet))
        set_setting(db, SETTING_TELEGRAM_CHAT_ID, encrypt_secret_with_fernet("-100", fernet))
        task = MonitoringTask(
            name="watch tg retry",
            enabled=True,
            provider_id=provider_id,
            provider_name_snapshot="Relay",
            provider_base_url_snapshot="https://relay.example/v1",
            model_id="gpt-watch",
            client_profile=CLIENT_PROFILE_CODEX,
            network_route="direct",
            interval_minutes=5,
            retry_attempts=3,
            retry_interval_seconds=1,
        )
        db.add(task)
        db.commit()
        task_id = task.id

    asyncio.run(scheduler_module.execute_monitoring_task_run(task_id, fernet=fernet))

    with SessionLocal() as db:
        check = db.scalar(select(MonitoringCheck).where(MonitoringCheck.task_id == task_id))
        assert check is not None
        assert check.status == "success"
        assert check.attempt_count == 1
        assert check.notification_status == "sent"
        assert check.notification_attempt_count == 3
    assert api_calls == 1
    assert telegram_calls == 3
    assert sleeps == [1, 1]
    client.close()


def test_monitoring_records_failed_telegram_after_retry_exhaustion(monkeypatch) -> None:
    client = setup_client()
    provider_id = create_provider(client)
    authorize_background()
    fernet = monitoring_fernet()

    async def fake_sleep(seconds):
        return None

    async def fake_run_connectivity_test(*args, **kwargs):
        return ConnectivityTestResult("success", 12, "", '{"result":"pong"}')

    async def fake_post_telegram_message(*, bot_token, chat_id, text, proxy_url=None):
        raise RuntimeError("TG still down")

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(notifications_module, "post_telegram_message", fake_post_telegram_message)
    monkeypatch.setattr(scheduler_module, "run_connectivity_test", fake_run_connectivity_test)
    with SessionLocal() as db:
        from app.notifications import SETTING_TELEGRAM_BOT_TOKEN, SETTING_TELEGRAM_CHAT_ID, SETTING_TELEGRAM_ENABLED

        set_setting(db, SETTING_TELEGRAM_ENABLED, "true")
        set_setting(db, SETTING_TELEGRAM_BOT_TOKEN, encrypt_secret_with_fernet("123:token", fernet))
        set_setting(db, SETTING_TELEGRAM_CHAT_ID, encrypt_secret_with_fernet("-100", fernet))
        task = MonitoringTask(
            name="watch tg failure",
            enabled=True,
            provider_id=provider_id,
            provider_name_snapshot="Relay",
            provider_base_url_snapshot="https://relay.example/v1",
            model_id="gpt-watch",
            client_profile=CLIENT_PROFILE_CODEX,
            network_route="direct",
            interval_minutes=5,
            retry_attempts=2,
            retry_interval_seconds=1,
        )
        db.add(task)
        db.commit()
        task_id = task.id

    asyncio.run(scheduler_module.execute_monitoring_task_run(task_id, fernet=fernet))

    with SessionLocal() as db:
        check = db.scalar(select(MonitoringCheck).where(MonitoringCheck.task_id == task_id))
        assert check is not None
        assert check.notification_status == "failed"
        assert check.notification_attempt_count == 2
        assert check.notification_error == "默认 Telegram: RuntimeError: TG still down"
    client.close()


def test_monitoring_multi_channel_notification_records_partial_delivery(monkeypatch) -> None:
    client = setup_client()
    provider_id = create_provider(client)
    authorize_background()
    fernet = monitoring_fernet()

    async def fake_run_connectivity_test(*args, **kwargs):
        return ConnectivityTestResult("success", 12, "", '{"result":"ok"}')

    async def fake_send_notification_channel_message(db, channel, task, check, fernet, *, event):
        if channel.name == "TG 主群":
            return True, ""
        return False, "飞书接口失败"

    monkeypatch.setattr(scheduler_module, "run_connectivity_test", fake_run_connectivity_test)
    monkeypatch.setattr(scheduler_module, "send_notification_channel_message", fake_send_notification_channel_message)
    with SessionLocal() as db:
        task = MonitoringTask(
            name="watch multi channel",
            enabled=True,
            provider_id=provider_id,
            provider_name_snapshot="Relay",
            provider_base_url_snapshot="https://relay.example/v1",
            model_id="gpt-watch",
            client_profile=CLIENT_PROFILE_CODEX,
            network_route="direct",
            interval_minutes=5,
            retry_attempts=1,
            retry_interval_seconds=1,
        )
        tg = NotificationChannel(name="TG 主群", channel_type="telegram", enabled=True)
        feishu = NotificationChannel(name="飞书值班群", channel_type="feishu_webhook", enabled=True)
        task.notification_links = [
            MonitoringTaskNotificationChannel(
                channel=tg,
                channel_name_snapshot=tg.name,
                channel_type_snapshot=tg.channel_type,
            ),
            MonitoringTaskNotificationChannel(
                channel=feishu,
                channel_name_snapshot=feishu.name,
                channel_type_snapshot=feishu.channel_type,
            ),
        ]
        db.add(task)
        db.commit()
        task_id = task.id

    asyncio.run(scheduler_module.execute_monitoring_task_run(task_id, fernet=fernet))

    with SessionLocal() as db:
        check = db.scalar(select(MonitoringCheck).where(MonitoringCheck.task_id == task_id))
        assert check is not None
        assert check.notification_status == "partial"
        assert check.notification_event == "recovery"
        assert check.notification_error == "飞书值班群: 飞书接口失败"
        deliveries = list(
            db.scalars(
                select(MonitoringNotificationDelivery)
                .where(MonitoringNotificationDelivery.check_id == check.id)
                .order_by(MonitoringNotificationDelivery.channel_name_snapshot)
            )
        )
        assert [(delivery.channel_name_snapshot, delivery.status) for delivery in deliveries] == [
            ("TG 主群", "sent"),
            ("飞书值班群", "failed"),
        ]
    client.close()


def test_monitoring_detects_disabled_provider_but_skips_archived_provider(monkeypatch) -> None:
    client = setup_client()
    provider_id = create_provider(client, enabled=False)
    authorize_background()
    fernet = monitoring_fernet()
    calls = 0
    telegram_calls = 0

    async def fake_run_connectivity_test(*args, **kwargs):
        nonlocal calls
        calls += 1
        return ConnectivityTestResult("success", 9, "", '{"result":"pong"}')

    async def fake_post_telegram_message(*, bot_token, chat_id, text, proxy_url=None):
        nonlocal telegram_calls
        telegram_calls += 1

    monkeypatch.setattr(scheduler_module, "run_connectivity_test", fake_run_connectivity_test)
    monkeypatch.setattr(notifications_module, "post_telegram_message", fake_post_telegram_message)
    with SessionLocal() as db:
        from app.notifications import SETTING_TELEGRAM_BOT_TOKEN, SETTING_TELEGRAM_CHAT_ID, SETTING_TELEGRAM_ENABLED

        set_setting(db, SETTING_TELEGRAM_ENABLED, "true")
        set_setting(db, SETTING_TELEGRAM_BOT_TOKEN, encrypt_secret_with_fernet("123:token", fernet))
        set_setting(db, SETTING_TELEGRAM_CHAT_ID, encrypt_secret_with_fernet("-100", fernet))
        task = MonitoringTask(
            name="watch disabled",
            enabled=True,
            provider_id=provider_id,
            provider_name_snapshot="Relay",
            provider_base_url_snapshot="https://relay.example/v1",
            model_id="gpt-watch",
            client_profile=CLIENT_PROFILE_CODEX,
            network_route="direct",
            interval_minutes=5,
            retry_attempts=3,
            retry_interval_seconds=1,
            notify_on_recovery=False,
            notify_on_failure=True,
        )
        db.add(task)
        db.commit()
        task_id = task.id

    asyncio.run(scheduler_module.execute_monitoring_task_run(task_id, fernet=fernet))
    with SessionLocal() as db:
        provider = db.get(Provider, provider_id)
        assert provider is not None
        provider.archived_at = datetime.now(timezone.utc)
        db.commit()
    asyncio.run(scheduler_module.execute_monitoring_task_run(task_id, fernet=fernet))

    with SessionLocal() as db:
        checks = list(
            db.scalars(select(MonitoringCheck).where(MonitoringCheck.task_id == task_id).order_by(MonitoringCheck.id))
        )
        assert [check.status for check in checks] == ["success", "skipped"]
        assert [check.attempt_count for check in checks] == [1, 1]
        assert [check.notification_event for check in checks] == ["", ""]
        assert checks[1].error_message == "中转站已归档。"
    assert calls == 1
    assert telegram_calls == 0
    client.close()


def test_telegram_config_is_encrypted_and_test_message_uses_proxy(monkeypatch) -> None:
    client = setup_client()
    proxy_response = client.post(
        "/proxies",
        data={
            "name": "TG Proxy",
            "scheme": "socks5h",
            "host": "proxy.example",
            "port": "1080",
            "username": "u",
            "password": "p",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert proxy_response.status_code == 303
    with SessionLocal() as db:
        proxy = db.scalar(select(NetworkProxy))
        assert proxy is not None
        proxy_id = proxy.id

    saved = client.post(
        "/monitoring/telegram",
        data={"bot_token": "123:token", "chat_id": "-100", "proxy_id": str(proxy_id), "enabled": "on"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/notification-channels"
    with SessionLocal() as db:
        encrypted_token = get_setting(db, "telegram_bot_token_encrypted")
        assert encrypted_token
        assert "123:token" not in encrypted_token
        channel = db.scalar(select(NotificationChannel).where(NotificationChannel.name == "默认 Telegram"))
        assert channel is not None
        assert channel.proxy_id == proxy_id
        channel_id = channel.id

    telegram_calls: list[dict[str, str | None]] = []

    async def fake_post_telegram_message(*, bot_token, chat_id, text, proxy_url=None):
        telegram_calls.append({"bot_token": bot_token, "chat_id": chat_id, "text": text, "proxy_url": proxy_url})

    monkeypatch.setattr(notifications_module, "post_telegram_message", fake_post_telegram_message)
    tested = client.post("/monitoring/telegram/test", follow_redirects=False)
    assert tested.status_code == 303
    assert tested.headers["location"] == "/notification-channels"
    assert telegram_calls == [
        {
            "bot_token": "123:token",
            "chat_id": "-100",
            "text": "<b>Keys 监控测试消息</b>\nTelegram 通知配置可用。",
            "proxy_url": "socks5h://u:p@proxy.example:1080",
        }
    ]
    page = client.get(f"/notification-channels/{channel_id}/edit")
    assert "123:token" not in page.text
    assert "已保存，留空则不修改" in page.text

    deleted = client.post(f"/notification-channels/{channel_id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/notification-channels"
    after_delete = client.get("/notification-channels")
    assert "共 0 个渠道" in after_delete.text
    with SessionLocal() as db:
        assert get_setting(db, "telegram_bot_token_encrypted")
        assert db.scalar(select(NotificationChannel).where(NotificationChannel.name == "默认 Telegram")) is None
    client.close()


def test_telegram_api_error_message_uses_safe_description() -> None:
    response = httpx.Response(
        400,
        json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
        request=httpx.Request("POST", "https://api.telegram.org/bot123:secret/sendMessage"),
    )

    message = notifications_module.telegram_api_error_message(response)

    assert message == "Telegram API HTTP 400: Bad Request: chat not found"
    assert "123:secret" not in message
    assert "sendMessage" not in message


def test_backup_v10_round_trips_monitoring_tasks_and_notification_channel_secrets(shared_db_reset) -> None:
    client = setup_client()
    provider_id = create_provider(client)
    client.post(
        "/monitoring/telegram",
        data={"bot_token": "123:token", "chat_id": "-100", "enabled": "on"},
        follow_redirects=False,
    )
    with SessionLocal() as db:
        channel = db.scalar(select(NotificationChannel).where(NotificationChannel.name == "默认 Telegram"))
        assert channel is not None
        channel_id = channel.id
    created = client.post(
        "/monitoring/tasks",
        data={
            "name": "watch relay",
            "provider_id": str(provider_id),
            "model_id": "gpt-watch",
            "client_profile": CLIENT_PROFILE_CODEX,
            "network_route": "direct",
            "interval_minutes": "5",
            "retry_attempts": "4",
            "retry_interval_seconds": "12",
            "notification_preferences_present": "1",
            "notify_on_recovery": "on",
            "notify_on_failure": "on",
            "notification_channel_ids": str(channel_id),
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    public_export = client.post("/export", data={"password": ""}).json()
    assert public_export["version"] == 10
    assert public_export["telegram"]["has_credentials"] is True
    assert "bot_token" not in public_export["telegram"]
    assert public_export["notification_channels"][0]["name"] == "默认 Telegram"
    assert public_export["notification_channels"][0]["has_credentials"] is True
    assert "secrets" not in public_export["notification_channels"][0]
    assert public_export["monitoring_tasks"][0]["name"] == "watch relay"
    assert public_export["monitoring_tasks"][0]["retry_attempts"] == 4
    assert public_export["monitoring_tasks"][0]["retry_interval_seconds"] == 12
    assert public_export["monitoring_tasks"][0]["notify_on_recovery"] is True
    assert public_export["monitoring_tasks"][0]["notify_on_failure"] is True
    assert public_export["monitoring_tasks"][0]["notification_channels"] == ["默认 Telegram"]

    secret_export = client.post(
        "/export",
        data={"include_secrets": "on", "password": "long-test-password"},
    ).json()
    assert secret_export["telegram"]["bot_token"] == "123:token"
    assert secret_export["telegram"]["chat_id"] == "-100"
    assert secret_export["notification_channels"][0]["secrets"]["bot_token"] == "123:token"
    assert secret_export["notification_channels"][0]["secrets"]["chat_id"] == "-100"
    client.close()

    shared_db_reset()
    restored_client = setup_client()
    imported = restored_client.post(
        "/import",
        files={"file": ("backup.json", json.dumps(secret_export).encode("utf-8"), "application/json")},
        follow_redirects=False,
    )
    assert imported.status_code == 303
    with SessionLocal() as db:
        task = db.scalar(select(MonitoringTask).where(MonitoringTask.name == "watch relay"))
        assert task is not None
        assert task.enabled is False
        assert task.next_run_at is None
        assert task.model_id == "gpt-watch"
        assert task.retry_attempts == 4
        assert task.retry_interval_seconds == 12
        assert task.notify_on_recovery is True
        assert task.notify_on_failure is True
        assert [link.channel_name_snapshot for link in task.notification_links] == ["默认 Telegram"]
        channel = db.scalar(select(NotificationChannel).where(NotificationChannel.name == "默认 Telegram"))
        assert channel is not None
        assert channel.encrypted_secret_json
        assert get_setting(db, "telegram_bot_token_encrypted")
    restored_client.close()


def test_old_backup_import_defaults_monitoring_retry_fields() -> None:
    client = setup_client()
    legacy_backup = {
        "version": 7,
        "providers": [
            {
                "name": "Relay",
                "base_url": "https://relay.example/v1",
                "api_key": "sk-test-secret",
                "notes": "",
                "enabled": True,
                "client_profile": "openai_chat",
            }
        ],
        "proxies": [],
        "groups": [],
        "schedules": [],
        "monitoring_tasks": [
            {
                "name": "watch relay",
                "enabled": True,
                "provider": {"name": "Relay", "base_url": "https://relay.example/v1"},
                "model_id": "gpt-watch",
                "client_profile": CLIENT_PROFILE_CODEX,
                "network_route": "direct",
                "interval_minutes": 5,
            }
        ],
        "telegram": {},
    }

    imported = client.post(
        "/import",
        files={"file": ("backup.json", json.dumps(legacy_backup).encode("utf-8"), "application/json")},
        follow_redirects=False,
    )

    assert imported.status_code == 303
    with SessionLocal() as db:
        task = db.scalar(select(MonitoringTask).where(MonitoringTask.name == "watch relay"))
        assert task is not None
        assert task.retry_attempts == 1
        assert task.retry_interval_seconds == 10
        assert task.notify_on_recovery is True
        assert task.notify_on_failure is False
    client.close()
