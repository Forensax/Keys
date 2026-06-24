from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text

from app.analytics import StatRecord, build_statistics, percentile_95
from app.config import settings
from app.db import SessionLocal, ensure_scheduling_columns
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


def test_statistics_summary_and_percentile_use_real_records() -> None:
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    records = [
        StatRecord(now - timedelta(hours=2), 1, "P1", "success", 100, "", "manual"),
        StatRecord(now - timedelta(hours=1), 1, "P1", "failed", 300, "HTTP 500\ntrace", "scheduled"),
        StatRecord(now, 2, "P2", "success", 200, "", "scheduled"),
    ]
    stats = build_statistics(records, range_key="7d")
    assert stats["summary"]["total"] == 3
    assert stats["summary"]["success_rate"] == 66.7
    assert stats["summary"]["failed"] == 1
    assert stats["summary"]["average_latency"] == 200
    assert len(stats["providers"]) == 2
    assert [row["name"] for row in stats["providers"]] == ["P2", "P1"]
    assert len(stats["latency_series"]) == 2
    assert stats["failures"] == [{"reason": "HTTP 500", "count": 1}]
    assert stats["sources"]["manual"] == 1
    assert stats["sources"]["scheduled"] == 2
    assert percentile_95([10, 20, 30, 40, 50]) == 50


def test_statistics_provider_ranking_uses_success_rate_then_volume_then_name() -> None:
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    records = [
        StatRecord(now - timedelta(minutes=12), 1, "Beta", "success", 100, "", "manual"),
        StatRecord(now - timedelta(minutes=11), 1, "Beta", "failed", 100, "boom", "manual"),
        StatRecord(now - timedelta(minutes=10), 2, "Alpha", "success", 100, "", "manual"),
        StatRecord(now - timedelta(minutes=9), 2, "Alpha", "failed", 100, "boom", "manual"),
        StatRecord(now - timedelta(minutes=8), 2, "Alpha", "success", 100, "", "manual"),
        StatRecord(now - timedelta(minutes=7), 3, "Gamma", "success", 100, "", "manual"),
        StatRecord(now - timedelta(minutes=6), 4, "Delta", "success", 100, "", "manual"),
        StatRecord(now - timedelta(minutes=5), 4, "Delta", "failed", 100, "boom", "manual"),
        StatRecord(now - timedelta(minutes=4), 4, "Delta", "success", 100, "", "manual"),
        StatRecord(now - timedelta(minutes=3), 4, "Delta", "failed", 100, "boom", "manual"),
        StatRecord(now - timedelta(minutes=2), 5, "Omega", "success", 100, "", "manual"),
        StatRecord(now - timedelta(minutes=1), 5, "Omega", "failed", 100, "boom", "manual"),
    ]

    stats = build_statistics(records, range_key="7d")

    assert [row["name"] for row in stats["providers"]] == ["Gamma", "Alpha", "Delta", "Beta", "Omega"]
    assert [row["success_rate"] for row in stats["providers"]] == [100.0, 66.7, 50.0, 50.0, 50.0]
    assert [series["name"] for series in stats["latency_series"]] == ["Delta", "Alpha", "Beta", "Omega", "Gamma"]


def test_schedule_and_statistics_pages_use_real_history_only() -> None:
    client = setup_client()
    schedules = client.get("/schedules")
    assert schedules.status_code == 200
    assert '<a href="/schedules">定时</a>' in schedules.text
    assert '<a href="/schedules">定时任务</a>' not in schedules.text
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
    legacy_demo_url = client.get("/statistics?range=30d&provider_id=all&source=all&mode=demo")
    assert legacy_demo_url.status_code == 200
    assert "data-auto-submit-filter" in legacy_demo_url.text
    assert "应用筛选" not in legacy_demo_url.text
    assert 'name="range"' in legacy_demo_url.text
    assert 'name="provider_id"' in legacy_demo_url.text
    assert 'name="source"' in legacy_demo_url.text
    assert "演示数据" not in legacy_demo_url.text
    assert "数据模式" not in legacy_demo_url.text
    assert "查看演示数据" not in legacy_demo_url.text
    assert "当前筛选范围内没有测试历史" in legacy_demo_url.text
    with SessionLocal() as db:
        assert len(db.scalars(select(ConnectivityTest)).all()) == before
    client.close()


def test_statistics_page_describes_provider_ranking_by_success_rate() -> None:
    client = setup_client()
    with SessionLocal() as db:
        provider = Provider(
            name="Relay",
            base_url="https://relay.example/v1",
            encrypted_api_key="encrypted",
            key_hint="...",
            enabled=True,
        )
        db.add(provider)
        db.flush()
        db.add(
            ConnectivityTest(
                provider_id=provider.id,
                model_id="gpt-test",
                status="success",
                latency_ms=100,
            )
        )
        db.commit()

    statistics = client.get("/statistics")

    assert statistics.status_code == 200
    assert "按成功率降序" in statistics.text
    assert "按测试次数降序" not in statistics.text
    client.close()


def test_statistics_filter_preferences_save_restore_and_repair() -> None:
    client = setup_client()
    with SessionLocal() as db:
        provider = Provider(
            name="Relay",
            base_url="https://relay.example/v1",
            encrypted_api_key="encrypted",
            key_hint="...",
            enabled=True,
        )
        db.add(provider)
        db.commit()
        provider_id = provider.id

    default_page = client.get("/statistics")
    assert default_page.status_code == 200
    assert 'value="7d" selected' in default_page.text
    assert 'value="all" selected>全部中转站' in default_page.text
    assert 'value="all" selected>全部来源' in default_page.text
    with SessionLocal() as db:
        assert get_setting(db, "statistics_filter_preferences") is None

    demo_only_page = client.get("/statistics?mode=demo")
    assert demo_only_page.status_code == 200
    assert "演示数据" not in demo_only_page.text
    with SessionLocal() as db:
        assert get_setting(db, "statistics_filter_preferences") is None

    saved_page = client.get("/statistics?range=30d&provider_id=all&source=scheduled")
    assert saved_page.status_code == 200
    assert 'value="30d" selected' in saved_page.text
    assert 'value="scheduled" selected' in saved_page.text
    with SessionLocal() as db:
        assert json.loads(get_setting(db, "statistics_filter_preferences") or "{}") == {
            "range": "30d",
            "provider_id": "all",
            "source": "scheduled",
        }

    restored_page = client.get("/statistics")
    assert restored_page.status_code == 200
    assert 'value="30d" selected' in restored_page.text
    assert 'value="scheduled" selected' in restored_page.text

    provider_page = client.get(f"/statistics?range=90d&provider_id={provider_id}&source=manual")
    assert provider_page.status_code == 200
    assert f'value="{provider_id}" selected' in provider_page.text
    with SessionLocal() as db:
        assert json.loads(get_setting(db, "statistics_filter_preferences") or "{}") == {
            "range": "90d",
            "provider_id": str(provider_id),
            "source": "manual",
        }
        db.delete(db.get(Provider, provider_id))
        db.commit()

    repaired_page = client.get("/statistics")
    assert repaired_page.status_code == 200
    assert 'value="90d" selected' in repaired_page.text
    assert 'value="all" selected>全部中转站' in repaired_page.text
    assert 'value="manual" selected' in repaired_page.text
    with SessionLocal() as db:
        assert json.loads(get_setting(db, "statistics_filter_preferences") or "{}") == {
            "range": "90d",
            "provider_id": "all",
            "source": "manual",
        }
    client.close()


def test_backup_v8_round_trips_schedule_definitions_as_disabled(shared_db_reset) -> None:
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
    assert exported["version"] == 8
    assert exported["schedules"][0]["name"] == "daily check"
    assert "scheduler_wrapped_vault_key" not in json.dumps(exported)
    client.close()

    shared_db_reset()
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
