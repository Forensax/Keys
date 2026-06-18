from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class Provider(Base):
    __tablename__ = "providers"
    __table_args__ = (UniqueConstraint("name", "base_url", name="uq_provider_name_base_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    base_url: Mapped[str] = mapped_column(String(600), nullable=False, index=True)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    key_hint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    models: Mapped[list["ProviderModel"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan", order_by="ProviderModel.model_id"
    )
    tests: Mapped[list["ConnectivityTest"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan", order_by="desc(ConnectivityTest.tested_at)"
    )


class ProviderModel(Base):
    __tablename__ = "provider_models"
    __table_args__ = (UniqueConstraint("provider_id", "model_id", name="uq_provider_model"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(260), nullable=False, index=True)
    owned_by: Mapped[str] = mapped_column(String(260), nullable=False, default="")
    raw_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    provider: Mapped[Provider] = relationship(back_populates="models")


class ConnectivityTest(Base):
    __tablename__ = "connectivity_tests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(260), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_response_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    provider: Mapped[Provider] = relationship(back_populates="tests")
