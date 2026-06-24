from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select

from app import notifications as notifications_module
from app import scheduler as scheduler_module
from app.config import settings
from app.db import Base, SessionLocal, engine, ensure_monitoring_columns
from app.main import app
from app.models import ConnectivityTest, MonitoringCheck, MonitoringTask, NetworkProxy, Provider
from app.openai_compat import CLIENT_PROFILE_CODEX, ConnectivityTestResult
from app.security import (
    authorize_scheduler_vault,
    encrypt_secret_with_fernet,
    get_scheduler_fernet,
    get_setting,
    set_setting,
)


def reset_db() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def setup_client() -> TestClient:
    client = TestClient(app)
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


def monitoring_fernet():
    with SessionLocal() as db:
        fernet = get_scheduler_fernet(db, settings.session_secret)
        assert fernet is not None
        return fernet


def test_monitoring_tables_are_created_and_migration_is_repeatable(tmp_path) -> None:
    legacy = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    Base.metadata.create_all(bind=legacy)
    ensure_monitoring_columns(legacy)
    ensure_monitoring_columns(legacy)

    tables = set(inspect(legacy).get_table_names())
    assert {"monitoring_tasks", "monitoring_checks"} <= tables
    task_columns = {column["name"] for column in inspect(legacy).get_columns("monitoring_tasks")}
    check_columns = {column["name"] for column in inspect(legacy).get_columns("monitoring_checks")}
    assert {"model_id", "client_profile", "network_route", "current_success_notified"} <= task_columns
    assert {"notification_status", "notification_error", "checked_at"} <= check_columns


def test_monitoring_page_create_task_and_requires_vault_for_enabled_task() -> None:
    reset_db()
    client = setup_client()
    provider_id = create_provider(client, enabled=False)

    page = client.get("/monitoring")
    assert page.status_code == 200
    assert "监控" in page.text
    assert "Telegram 通知" in page.text
    assert "新建监控任务" in page.text
    assert "/monitoring/tasks" in page.text

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

    client.close()


def test_monitoring_checks_are_independent_and_recovery_notifications_are_stateful(monkeypatch) -> None:
    reset_db()
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


def test_monitoring_detects_disabled_provider_but_skips_archived_provider(monkeypatch) -> None:
    reset_db()
    client = setup_client()
    provider_id = create_provider(client, enabled=False)
    authorize_background()
    fernet = monitoring_fernet()
    calls = 0

    async def fake_run_connectivity_test(*args, **kwargs):
        nonlocal calls
        calls += 1
        return ConnectivityTestResult("success", 9, "", '{"result":"pong"}')

    monkeypatch.setattr(scheduler_module, "run_connectivity_test", fake_run_connectivity_test)
    with SessionLocal() as db:
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
        assert checks[1].error_message == "中转站已归档。"
    assert calls == 1
    client.close()


def test_telegram_config_is_encrypted_and_test_message_uses_proxy(monkeypatch) -> None:
    reset_db()
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
    with SessionLocal() as db:
        encrypted_token = get_setting(db, "telegram_bot_token_encrypted")
        assert encrypted_token
        assert "123:token" not in encrypted_token

    telegram_calls: list[dict[str, str | None]] = []

    async def fake_post_telegram_message(*, bot_token, chat_id, text, proxy_url=None):
        telegram_calls.append({"bot_token": bot_token, "chat_id": chat_id, "text": text, "proxy_url": proxy_url})

    monkeypatch.setattr(notifications_module, "post_telegram_message", fake_post_telegram_message)
    tested = client.post("/monitoring/telegram/test", follow_redirects=False)
    assert tested.status_code == 303
    assert telegram_calls == [
        {
            "bot_token": "123:token",
            "chat_id": "-100",
            "text": "<b>Keys 监控测试消息</b>\nTelegram 通知配置可用。",
            "proxy_url": "socks5h://u:p@proxy.example:1080",
        }
    ]
    page = client.get("/monitoring")
    assert "123:token" not in page.text
    assert "已保存，留空则不修改" in page.text
    client.close()


def test_backup_v7_round_trips_monitoring_tasks_and_telegram_secrets() -> None:
    reset_db()
    client = setup_client()
    provider_id = create_provider(client)
    client.post(
        "/monitoring/telegram",
        data={"bot_token": "123:token", "chat_id": "-100", "enabled": "on"},
        follow_redirects=False,
    )
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

    public_export = client.post("/export", data={"password": ""}).json()
    assert public_export["version"] == 7
    assert public_export["telegram"]["has_credentials"] is True
    assert "bot_token" not in public_export["telegram"]
    assert public_export["monitoring_tasks"][0]["name"] == "watch relay"

    secret_export = client.post(
        "/export",
        data={"include_secrets": "on", "password": "long-test-password"},
    ).json()
    assert secret_export["telegram"]["bot_token"] == "123:token"
    assert secret_export["telegram"]["chat_id"] == "-100"
    client.close()

    reset_db()
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
        assert get_setting(db, "telegram_bot_token_encrypted")
    restored_client.close()
