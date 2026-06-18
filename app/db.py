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


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_provider_archive_column()
    ensure_client_profile_columns()


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
