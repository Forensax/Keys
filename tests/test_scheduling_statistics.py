from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from app.analytics import build_statistics, filter_records, generate_demo_records, percentile_95
from app.config import settings
from app.db import Base, SessionLocal, engine, ensure_scheduling_columns
from app.main import app
from app.models import ConnectivityTest, Provider, ScheduledRun, ScheduledTask
from app import scheduler as scheduler_module
from app.scheduler import (
    MAX_INTERVAL_MINUTES,
    MIN_INTERVAL_MINUTES,
    calculate_next_run,
    cleanup_scheduled_history,
    validate_schedule_values,
)
from app.security import (
    authorize_scheduler_vault,
    clear_scheduler_vault,
    get_scheduler_fernet,
    get_setting,
    SETTING_SCHEDULER_WRAPPED_VAULT_KEY,
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


def test_scheduling_migration_is_repeatable_and_marks_existing_rows_manual(tmp_path) -> None:
    legacy = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE connectivity_tests ("
                "id INTEGER PRIMARY KEY, provider_id INTEGER NOT NULL, model_id VARCHAR(260) NOT NULL, "
                "status VARCHAR(32) NOT NULL, error_message TEXT NOT NULL DEFAULT '', tested_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO connectivity_tests "
                "(provider_id, model_id, status, error_message, tested_at) "
                "VALUES (1, 'gpt-test', 'success', '', CURRENT_TIMESTAMP)"
            )
        )
    ensure_scheduling_columns(legacy)
    ensure_scheduling_columns(legacy)
    columns = {column["name"] for column in inspect(legacy).get_columns("connectivity_tests")}
    assert {"trigger_source", "scheduled_run_id"} <= columns
    with legacy.connect() as connection:
        assert connection.execute(text("SELECT trigger_source FROM connectivity_tests")).scalar_one() == "manual"


def test_scheduler_vault_round_trip_never_stores_plain_vault_key() -> None:
    reset_db()
    client = setup_client()
    with SessionLocal() as db:
        authorize_scheduler_vault(db, "long-test-password", settings.session_secret)
        wrapped = get_setting(db, SETTING_SCHEDULER_WRAPPED_VAULT_KEY)
        fernet = get_scheduler_fernet(db, settings.session_secret)
        assert wrapped
        assert fernet is not None
        assert wrapped.encode() != fernet._signing_key + fernet._encryption_key
        assert get_scheduler_fernet(db, settings.session_secret + "-changed") is None
        clear_scheduler_vault(db)
        assert get_scheduler_fernet(db, settings.session_secret) is None
    client.close()


def test_schedule_validation_and_next_run_cover_interval_and_daily() -> None:
    assert validate_schedule_values(
        name="too short",
        target_type="all",
        group_id=None,
        provider_ids=[],
        schedule_kind="interval",
        interval_minutes=MIN_INTERVAL_MINUTES - 1,
        daily_time=None,
    )
    assert validate_schedule_values(
        name="too long",
        target_type="all",
        group_id=None,
        provider_ids=[],
        schedule_kind="interval",
        interval_minutes=MAX_INTERVAL_MINUTES + 1,
        daily_time=None,
    )
    now = datetime(2026, 6, 22, 1, 0, tzinfo=timezone.utc)
    interval_task = ScheduledTask(schedule_kind="interval", interval_minutes=15)
    assert calculate_next_run(interval_task, now) == now + timedelta(minutes=15)
    daily_task = ScheduledTask(schedule_kind="daily", daily_time="09:30", timezone_name="Asia/Shanghai")
    assert calculate_next_run(daily_task, now) == datetime(2026, 6, 22, 1, 30, tzinfo=timezone.utc)


def test_scheduled_run_freezes_targets_and_writes_summary(monkeypatch) -> None:
    reset_db()
    with SessionLocal() as db:
        provider = Provider(
            name="P",
            base_url="https://p.example/v1",
            encrypted_api_key="encrypted",
            key_hint="...",
            enabled=True,
        )
        task = ScheduledTask(
            name="all",
            enabled=True,
            target_type="all",
            schedule_kind="interval",
            interval_minutes=15,
        )
        db.add_all([provider, task])
        db.commit()
        task_id = task.id

    async def fake_test_provider(provider_id: int, run_id: int, fernet: Fernet) -> str:
        assert provider_id > 0 and run_id > 0 and fernet is not None
        return "success"

    monkeypatch.setattr(scheduler_module, "_test_provider", fake_test_provider)
    asyncio.run(
        scheduler_module.execute_task_run(
            task_id,
            trigger="scheduled",
            fernet=Fernet(Fernet.generate_key()),
        )
    )
    with SessionLocal() as db:
        run = db.scalar(select(ScheduledRun))
        assert run is not None
        assert (run.status, run.total_count, run.success_count, run.failed_count) == ("success", 1, 1, 0)


def test_demo_statistics_are_deterministic_filtered_and_complete() -> None:
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    first = generate_demo_records(now)
    second = generate_demo_records(now)
    assert first == second
    scheduled = filter_records(first, range_key="7d", provider_id=None, source="scheduled", now=now)
    stats = build_statistics(scheduled, range_key="7d")
    assert stats["summary"]["total"] == len(scheduled)
    assert 0 <= stats["summary"]["success_rate"] <= 100
    assert len(stats["providers"]) == 6
    assert len(stats["latency_series"]) == 5
    assert stats["sources"]["manual"] == 0
    assert stats["sources"]["scheduled"] == len(scheduled)
    assert percentile_95([10, 20, 30, 40, 50]) == 50


def test_schedule_and_statistics_pages_and_demo_do_not_write_history() -> None:
    reset_db()
    client = setup_client()
    schedules = client.get("/schedules")
    assert schedules.status_code == 200
    assert "定时任务" in schedules.text
    created = client.post(
        "/schedules",
        data={
            "name": "每小时检查",
            "target_type": "all",
            "schedule_kind": "interval",
            "interval_minutes": "60",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    before = 0
    with SessionLocal() as db:
        before = len(db.scalars(select(ConnectivityTest)).all())
    demo = client.get("/statistics?range=30d&provider_id=all&source=all&mode=demo")
    assert demo.status_code == 200
    assert "演示数据" in demo.text
    assert "成功与失败趋势" in demo.text
    assert "data-stat-chart=\"latency\"" in demo.text
    with SessionLocal() as db:
        assert len(db.scalars(select(ConnectivityTest)).all()) == before
    client.close()


def test_backup_v6_round_trips_schedule_definitions_as_disabled() -> None:
    reset_db()
    client = setup_client()
    response = client.post(
        "/schedules",
        data={
            "name": "daily check",
            "target_type": "all",
            "schedule_kind": "daily",
            "daily_time": "09:30",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    exported = client.post("/export", data={"password": ""}).json()
    assert exported["version"] == 6
    assert exported["schedules"][0]["name"] == "daily check"
    assert "scheduler_wrapped_vault_key" not in json.dumps(exported)
    client.close()

    reset_db()
    restored_client = setup_client()
    imported = restored_client.post(
        "/import",
        files={"file": ("backup.json", json.dumps(exported).encode("utf-8"), "application/json")},
        follow_redirects=False,
    )
    assert imported.status_code == 303
    with SessionLocal() as db:
        task = db.scalar(select(ScheduledTask).where(ScheduledTask.name == "daily check"))
        assert task is not None
        assert task.enabled is False
        assert task.daily_time == "09:30"
        assert task.next_run_at is None
    restored_client.close()


def test_cleanup_only_removes_old_scheduled_history() -> None:
    reset_db()
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        run = ScheduledRun(task_name_snapshot="old", started_at=now - timedelta(days=181), status="success")
        db.add(run)
        db.flush()
        # ConnectivityTest requires a provider; use raw inserts so this test focuses on cleanup predicates.
        db.execute(
            text(
                "INSERT INTO providers (name, base_url, encrypted_api_key, key_hint, notes, enabled, client_profile, "
                "test_network_route, created_at, updated_at) VALUES "
                "('P', 'https://p.example/v1', 'x', 'x', '', 1, 'openai_chat', 'default', :now, :now)"
            ),
            {"now": now},
        )
        provider_id = db.execute(text("SELECT id FROM providers WHERE name='P'")).scalar_one()
        for source in ("manual", "scheduled"):
            db.add(
                ConnectivityTest(
                    provider_id=provider_id,
                    model_id="m",
                    status="success",
                    trigger_source=source,
                    tested_at=now - timedelta(days=181),
                )
            )
        db.commit()
    cleanup_scheduled_history(now)
    with SessionLocal() as db:
        remaining = list(db.scalars(select(ConnectivityTest)).all())
        assert [item.trigger_source for item in remaining] == ["manual"]
        assert db.scalar(select(ScheduledRun)) is None
