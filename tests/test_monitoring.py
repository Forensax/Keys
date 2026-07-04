from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select

from app import notifications as notifications_module
from app import scheduler as scheduler_module
from app.config import settings
from app.db import Base, SessionLocal, ensure_monitoring_columns
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


def test_monitoring_page_create_task_and_requires_vault_for_enabled_task() -> None:
    client = setup_client()
    provider_id = create_provider(client, enabled=False)

    page = client.get("/monitoring")
    assert page.status_code == 200
    assert "监控" in page.text
    assert '<a class="button" href="/monitoring?panel=vault">锁定后台密钥</a>' in page.text
    assert '<a class="button" href="/monitoring?panel=telegram">TG通知设置</a>' in page.text
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
    assert "返回任务列表" in vault_panel.text
    assert "保存通知配置" not in vault_panel.text
    assert 'name="retry_attempts"' not in vault_panel.text

    telegram_panel = client.get("/monitoring?panel=telegram")
    assert telegram_panel.status_code == 200
    assert "Telegram 通知" in telegram_panel.text
    assert "保存通知配置" in telegram_panel.text
    assert "返回任务列表" in telegram_panel.text
    assert "后台密钥可用" not in telegram_panel.text
    assert 'name="retry_attempts"' not in telegram_panel.text

    new_task_panel = client.get("/monitoring?panel=new_task")
    assert new_task_panel.status_code == 200
    assert "新建监控任务" in new_task_panel.text
    assert "/monitoring/tasks" in new_task_panel.text
    assert "返回任务列表" in new_task_panel.text
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
    assert "每 5 分钟 · 最多 1 次" in task_table
    assert "monitoring-timeline-row" in task_table
    assert "过去 24 小时" in task_table
    assert "暂无可用率 · 故障 0 次" in task_table
    assert "monitoring-timeline-lane is-success" in task_table
    assert "monitoring-timeline-lane is-failed" in task_table
    assert "恢复" in task_table
    assert "立即检测" in task_table
    assert "停用" in task_table
    assert "编辑" in task_table
    assert "删除" in task_table

    styles = Path("app/static/styles.css").read_text(encoding="utf-8")
    assert ".monitoring-table,\n.monitoring-check-table" not in styles
    assert ".monitoring-table {\n  min-width: 0;" in styles
    assert ".monitoring-table .schedule-actions {\n  flex-wrap: nowrap;" in styles
    assert ".monitoring-period-cell span {\n  display: block;" in styles
    assert ".monitoring-timeline-track" in styles
    assert ".monitoring-timeline-plot" in styles
    assert ".monitoring-timeline-lane.is-success" in styles
    assert ".monitoring-timeline-lane.is-failed" in styles
    assert ".monitoring-timeline-segment.is-success" in styles
    assert ".monitoring-timeline-segment.is-neutral" not in styles
    assert ".monitoring-task-error" in styles
    assert ".monitoring-check-error" in styles
    assert "white-space: nowrap;" in styles
    assert ".monitoring-check-table" in styles and "min-width: 1040px" in styles

    edit_page = client.get(f"/monitoring/tasks/{task_id}/edit")
    assert edit_page.status_code == 200
    assert 'name="retry_attempts"' in edit_page.text
    assert 'name="retry_interval_seconds"' in edit_page.text
    assert 'name="notify_on_recovery"' in edit_page.text
    assert 'name="notify_on_failure"' in edit_page.text

    client.close()


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
    assert "upstream unavailable" in task_table
    assert "monitoring-check-error" in page.text
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
        assert check.notification_error == "RuntimeError: TG down"
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
        assert check.notification_error == "RuntimeError: TG still down"
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
    assert saved.headers["location"] == "/monitoring?panel=telegram"
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
    assert tested.headers["location"] == "/monitoring?panel=telegram"
    assert telegram_calls == [
        {
            "bot_token": "123:token",
            "chat_id": "-100",
            "text": "<b>Keys 监控测试消息</b>\nTelegram 通知配置可用。",
            "proxy_url": "socks5h://u:p@proxy.example:1080",
        }
    ]
    page = client.get("/monitoring?panel=telegram")
    assert "123:token" not in page.text
    assert "已保存，留空则不修改" in page.text
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


def test_backup_v9_round_trips_monitoring_tasks_and_telegram_secrets(shared_db_reset) -> None:
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
            "retry_attempts": "4",
            "retry_interval_seconds": "12",
            "notification_preferences_present": "1",
            "notify_on_recovery": "on",
            "notify_on_failure": "on",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303

    public_export = client.post("/export", data={"password": ""}).json()
    assert public_export["version"] == 9
    assert public_export["telegram"]["has_credentials"] is True
    assert "bot_token" not in public_export["telegram"]
    assert public_export["monitoring_tasks"][0]["name"] == "watch relay"
    assert public_export["monitoring_tasks"][0]["retry_attempts"] == 4
    assert public_export["monitoring_tasks"][0]["retry_interval_seconds"] == 12
    assert public_export["monitoring_tasks"][0]["notify_on_recovery"] is True
    assert public_export["monitoring_tasks"][0]["notify_on_failure"] is True

    secret_export = client.post(
        "/export",
        data={"include_secrets": "on", "password": "long-test-password"},
    ).json()
    assert secret_export["telegram"]["bot_token"] == "123:token"
    assert secret_export["telegram"]["chat_id"] == "-100"
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
