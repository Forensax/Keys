from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import close_all_sessions


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
WORKER_ID = os.environ.get("PYTEST_XDIST_WORKER", "main")
TEST_DB_BASENAME = f"test-keys-{WORKER_ID}"
TEST_DB_PATH = DATA_DIR / f"{TEST_DB_BASENAME}.db"
TEST_DB_SIDECARS = [
    TEST_DB_PATH,
    DATA_DIR / f"{TEST_DB_BASENAME}.db-journal",
    DATA_DIR / f"{TEST_DB_BASENAME}.db-shm",
    DATA_DIR / f"{TEST_DB_BASENAME}.db-wal",
]

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"
os.environ.setdefault("SESSION_SECRET", "test-secret")

from app import scheduler as scheduler_module  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app import security as security_module  # noqa: E402

_ACTIVE_TEST_CONNECTION = None


def _cleanup_worker_db_files() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    engine.dispose()
    for path in TEST_DB_SIDECARS:
        path.unlink(missing_ok=True)


def _reset_scheduler_state() -> None:
    scheduler_module._provider_locks.clear()
    scheduler_module._running_task_ids.clear()
    scheduler_module._running_monitoring_task_ids.clear()
    scheduler_module._worker_tasks.clear()
    scheduler_module._monitoring_worker_tasks.clear()
    scheduler_module._loop_task = None
    scheduler_module._stop_event = None
    scheduler_module._test_semaphore = None


def _reset_security_state() -> None:
    security_module._vault_keys.clear()


def _clear_shared_tables(connection) -> None:
    inspector_tables = [
        "monitoring_checks",
        "monitoring_tasks",
        "connectivity_tests",
        "provider_models",
        "scheduled_task_providers",
        "scheduled_runs",
        "scheduled_tasks",
        "providers",
        "provider_groups",
        "network_proxies",
        "app_settings",
    ]
    for table_name in inspector_tables:
        connection.execute(text(f"DELETE FROM {table_name}"))


def reset_shared_db_state() -> None:
    if _ACTIVE_TEST_CONNECTION is None:
        raise RuntimeError("当前没有可重置的测试数据库连接。")
    close_all_sessions()
    _reset_scheduler_state()
    _reset_security_state()
    _clear_shared_tables(_ACTIVE_TEST_CONNECTION)


@pytest.fixture
def shared_db_reset():
    return reset_shared_db_state


@pytest.fixture(scope="session", autouse=True)
def worker_database() -> Iterator[None]:
    _cleanup_worker_db_files()
    init_db()
    yield
    close_all_sessions()
    SessionLocal.configure(bind=engine)
    _reset_scheduler_state()
    _reset_security_state()
    _cleanup_worker_db_files()


@pytest.fixture(autouse=True)
def isolated_database(worker_database: None) -> Iterator[None]:
    global _ACTIVE_TEST_CONNECTION
    _reset_scheduler_state()
    _reset_security_state()
    close_all_sessions()
    engine.dispose()
    connection = engine.connect()
    transaction = connection.begin()
    _ACTIVE_TEST_CONNECTION = connection
    SessionLocal.configure(bind=connection, join_transaction_mode="create_savepoint")
    _clear_shared_tables(connection)
    try:
        yield
    finally:
        close_all_sessions()
        SessionLocal.configure(bind=engine)
        _ACTIVE_TEST_CONNECTION = None
        transaction.rollback()
        connection.close()
        _reset_scheduler_state()
        _reset_security_state()
