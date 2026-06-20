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


class NetworkProxy(Base):
    __tablename__ = "network_proxies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True, index=True)
    scheme: Mapped[str] = mapped_column(String(16), nullable=False)
    host: Mapped[str] = mapped_column(String(500), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    encrypted_username: Mapped[str] = mapped_column(Text, nullable=False, default="")
    encrypted_password: Mapped[str] = mapped_column(Text, nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_test_status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    last_exit_ip: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    @property
    def endpoint(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def has_auth(self) -> bool:
        return bool(self.encrypted_username or self.encrypted_password)


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
    client_profile: Mapped[str] = mapped_column(String(32), nullable=False, default="openai_chat")
    test_model_id: Mapped[str | None] = mapped_column(String(260), nullable=True)
    test_client_profile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    test_network_route: Mapped[str] = mapped_column(String(180), nullable=False, default="default")
    default_proxy_id: Mapped[int | None] = mapped_column(
        ForeignKey("network_proxies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    models: Mapped[list["ProviderModel"]] = relationship(
        back_populates="provider",
        cascade="all, delete-orphan",
        order_by=lambda: (ProviderModel.is_manual.desc(), ProviderModel.model_id.asc()),
    )
    tests: Mapped[list["ConnectivityTest"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan", order_by="desc(ConnectivityTest.tested_at)"
    )
    default_proxy: Mapped[NetworkProxy | None] = relationship(foreign_keys=[default_proxy_id])


class ProviderModel(Base):
    __tablename__ = "provider_models"
    __table_args__ = (UniqueConstraint("provider_id", "model_id", name="uq_provider_model"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(260), nullable=False, index=True)
    is_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    owned_by: Mapped[str] = mapped_column(String(260), nullable=False, default="")
    raw_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    provider: Mapped[Provider] = relationship(back_populates="models")


class ConnectivityTest(Base):
    __tablename__ = "connectivity_tests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(260), nullable=False)
    client_profile: Mapped[str] = mapped_column(String(32), nullable=False, default="openai_chat")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_response_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    network_route: Mapped[str] = mapped_column(String(160), nullable=False, default="直连")
    tested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    provider: Mapped[Provider] = relationship(back_populates="tests")
