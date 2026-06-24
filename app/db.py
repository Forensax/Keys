from __future__ import annotations

from pathlib import Path
from typing import Generator

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import DATA_DIR, settings


class Base(DeclarativeBase):
    pass


def _ensure_sqlite_dir() -> None:
    if settings.database_url.startswith("sqlite:///"):
        raw_path = settings.database_url.removeprefix("sqlite:///")
        db_path = Path(raw_path)
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_dir()

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def ensure_provider_archive_column(bind: Engine = engine) -> None:
    inspector = inspect(bind)
    if "providers" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("providers")}
    if "archived_at" in columns:
        return
    with bind.begin() as connection:
        connection.execute(text("ALTER TABLE providers ADD COLUMN archived_at DATETIME"))


def ensure_client_profile_columns(bind: Engine = engine) -> None:
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    migrations = {
        "providers": "ALTER TABLE providers ADD COLUMN client_profile VARCHAR(32) NOT NULL DEFAULT 'openai_chat'",
        "connectivity_tests": (
            "ALTER TABLE connectivity_tests ADD COLUMN client_profile VARCHAR(32) NOT NULL DEFAULT 'openai_chat'"
        ),
    }
    for table_name, statement in migrations.items():
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspect(bind).get_columns(table_name)}
        if "client_profile" in columns:
            continue
        with bind.begin() as connection:
            connection.execute(text(statement))


def ensure_proxy_columns(bind: Engine = engine) -> None:
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    migrations = {
        "providers": "ALTER TABLE providers ADD COLUMN default_proxy_id INTEGER",
        "connectivity_tests": (
            "ALTER TABLE connectivity_tests ADD COLUMN network_route VARCHAR(160) NOT NULL DEFAULT '直连'"
        ),
    }
    for table_name, statement in migrations.items():
        if table_name not in tables:
            continue
        column_name = "default_proxy_id" if table_name == "providers" else "network_route"
        columns = {column["name"] for column in inspect(bind).get_columns(table_name)}
        if column_name in columns:
            continue
        with bind.begin() as connection:
            connection.execute(text(statement))


def ensure_test_preference_columns(bind: Engine = engine) -> None:
    inspector = inspect(bind)
    if "providers" not in inspector.get_table_names():
        return
    migrations = {
        "test_model_id": "ALTER TABLE providers ADD COLUMN test_model_id VARCHAR(260)",
        "test_client_profile": "ALTER TABLE providers ADD COLUMN test_client_profile VARCHAR(32)",
        "test_network_route": (
            "ALTER TABLE providers ADD COLUMN test_network_route VARCHAR(180) NOT NULL DEFAULT 'default'"
        ),
    }
    columns = {column["name"] for column in inspector.get_columns("providers")}
    for column_name, statement in migrations.items():
        if column_name in columns:
            continue
        with bind.begin() as connection:
            connection.execute(text(statement))


def ensure_model_source_column(bind: Engine = engine) -> None:
    inspector = inspect(bind)
    if "provider_models" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("provider_models")}
    if "is_manual" in columns:
        return
    with bind.begin() as connection:
        connection.execute(
            text("ALTER TABLE provider_models ADD COLUMN is_manual BOOLEAN NOT NULL DEFAULT 0")
        )


def ensure_provider_group_column(bind: Engine = engine) -> None:
    inspector = inspect(bind)
    if "providers" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("providers")}
    if "group_id" in columns:
        return
    with bind.begin() as connection:
        connection.execute(text("ALTER TABLE providers ADD COLUMN group_id INTEGER"))


def ensure_scheduling_columns(bind: Engine = engine) -> None:
    inspector = inspect(bind)
    if "connectivity_tests" not in inspector.get_table_names():
        return
    migrations = {
        "trigger_source": (
            "ALTER TABLE connectivity_tests ADD COLUMN trigger_source VARCHAR(32) NOT NULL DEFAULT 'manual'"
        ),
        "scheduled_run_id": "ALTER TABLE connectivity_tests ADD COLUMN scheduled_run_id INTEGER",
    }
    columns = {column["name"] for column in inspector.get_columns("connectivity_tests")}
    for column_name, statement in migrations.items():
        if column_name in columns:
            continue
        with bind.begin() as connection:
            connection.execute(text(statement))


def ensure_monitoring_columns(bind: Engine = engine) -> None:
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    task_migrations = {
        "name": "ALTER TABLE monitoring_tasks ADD COLUMN name VARCHAR(160) NOT NULL DEFAULT ''",
        "enabled": "ALTER TABLE monitoring_tasks ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT 0",
        "provider_id": "ALTER TABLE monitoring_tasks ADD COLUMN provider_id INTEGER",
        "provider_name_snapshot": (
            "ALTER TABLE monitoring_tasks ADD COLUMN provider_name_snapshot VARCHAR(160) NOT NULL DEFAULT ''"
        ),
        "provider_base_url_snapshot": (
            "ALTER TABLE monitoring_tasks ADD COLUMN provider_base_url_snapshot VARCHAR(600) NOT NULL DEFAULT ''"
        ),
        "model_id": "ALTER TABLE monitoring_tasks ADD COLUMN model_id VARCHAR(260) NOT NULL DEFAULT ''",
        "client_profile": (
            "ALTER TABLE monitoring_tasks ADD COLUMN client_profile VARCHAR(32) NOT NULL DEFAULT 'openai_chat'"
        ),
        "network_route": (
            "ALTER TABLE monitoring_tasks ADD COLUMN network_route VARCHAR(180) NOT NULL DEFAULT 'default'"
        ),
        "interval_minutes": "ALTER TABLE monitoring_tasks ADD COLUMN interval_minutes INTEGER NOT NULL DEFAULT 5",
        "retry_attempts": "ALTER TABLE monitoring_tasks ADD COLUMN retry_attempts INTEGER NOT NULL DEFAULT 1",
        "retry_interval_seconds": (
            "ALTER TABLE monitoring_tasks ADD COLUMN retry_interval_seconds INTEGER NOT NULL DEFAULT 10"
        ),
        "next_run_at": "ALTER TABLE monitoring_tasks ADD COLUMN next_run_at DATETIME",
        "last_status": "ALTER TABLE monitoring_tasks ADD COLUMN last_status VARCHAR(32) NOT NULL DEFAULT ''",
        "last_error": "ALTER TABLE monitoring_tasks ADD COLUMN last_error TEXT NOT NULL DEFAULT ''",
        "last_latency_ms": "ALTER TABLE monitoring_tasks ADD COLUMN last_latency_ms INTEGER",
        "last_checked_at": "ALTER TABLE monitoring_tasks ADD COLUMN last_checked_at DATETIME",
        "last_notified_at": "ALTER TABLE monitoring_tasks ADD COLUMN last_notified_at DATETIME",
        "current_success_notified": (
            "ALTER TABLE monitoring_tasks ADD COLUMN current_success_notified BOOLEAN NOT NULL DEFAULT 0"
        ),
        "created_at": "ALTER TABLE monitoring_tasks ADD COLUMN created_at DATETIME",
        "updated_at": "ALTER TABLE monitoring_tasks ADD COLUMN updated_at DATETIME",
    }
    check_migrations = {
        "task_id": "ALTER TABLE monitoring_checks ADD COLUMN task_id INTEGER",
        "task_name_snapshot": (
            "ALTER TABLE monitoring_checks ADD COLUMN task_name_snapshot VARCHAR(160) NOT NULL DEFAULT ''"
        ),
        "provider_id": "ALTER TABLE monitoring_checks ADD COLUMN provider_id INTEGER",
        "provider_name_snapshot": (
            "ALTER TABLE monitoring_checks ADD COLUMN provider_name_snapshot VARCHAR(160) NOT NULL DEFAULT ''"
        ),
        "provider_base_url_snapshot": (
            "ALTER TABLE monitoring_checks ADD COLUMN provider_base_url_snapshot VARCHAR(600) NOT NULL DEFAULT ''"
        ),
        "model_id": "ALTER TABLE monitoring_checks ADD COLUMN model_id VARCHAR(260) NOT NULL DEFAULT ''",
        "client_profile": (
            "ALTER TABLE monitoring_checks ADD COLUMN client_profile VARCHAR(32) NOT NULL DEFAULT 'openai_chat'"
        ),
        "network_route": (
            "ALTER TABLE monitoring_checks ADD COLUMN network_route VARCHAR(160) NOT NULL DEFAULT '直连'"
        ),
        "status": "ALTER TABLE monitoring_checks ADD COLUMN status VARCHAR(32) NOT NULL DEFAULT 'failed'",
        "latency_ms": "ALTER TABLE monitoring_checks ADD COLUMN latency_ms INTEGER",
        "attempt_count": "ALTER TABLE monitoring_checks ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 1",
        "error_message": "ALTER TABLE monitoring_checks ADD COLUMN error_message TEXT NOT NULL DEFAULT ''",
        "raw_response_excerpt": (
            "ALTER TABLE monitoring_checks ADD COLUMN raw_response_excerpt TEXT NOT NULL DEFAULT ''"
        ),
        "notification_status": (
            "ALTER TABLE monitoring_checks ADD COLUMN notification_status VARCHAR(32) NOT NULL DEFAULT 'not_sent'"
        ),
        "notification_attempt_count": (
            "ALTER TABLE monitoring_checks ADD COLUMN notification_attempt_count INTEGER NOT NULL DEFAULT 0"
        ),
        "notification_error": (
            "ALTER TABLE monitoring_checks ADD COLUMN notification_error TEXT NOT NULL DEFAULT ''"
        ),
        "checked_at": "ALTER TABLE monitoring_checks ADD COLUMN checked_at DATETIME",
    }
    for table_name, migrations in (
        ("monitoring_tasks", task_migrations),
        ("monitoring_checks", check_migrations),
    ):
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspect(bind).get_columns(table_name)}
        for column_name, statement in migrations.items():
            if column_name in columns:
                continue
            with bind.begin() as connection:
                connection.execute(text(statement))


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_provider_archive_column()
    ensure_client_profile_columns()
    ensure_proxy_columns()
    ensure_test_preference_columns()
    ensure_model_source_column()
    ensure_provider_group_column()
    ensure_scheduling_columns()
    ensure_monitoring_columns()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
