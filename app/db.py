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


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_provider_archive_column()
    ensure_client_profile_columns()
    ensure_proxy_columns()
    ensure_test_preference_columns()
    ensure_model_source_column()
    ensure_provider_group_column()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
