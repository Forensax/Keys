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


class ProviderGroup(Base):
    __tablename__ = "provider_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    providers: Mapped[list["Provider"]] = relationship(back_populates="group")


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
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_groups.id", ondelete="SET NULL"), nullable=True, index=True
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
    group: Mapped[ProviderGroup | None] = relationship(back_populates="providers", foreign_keys=[group_id])


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


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, default="all")
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    schedule_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="interval")
    interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    group: Mapped[ProviderGroup | None] = relationship(foreign_keys=[group_id])
    provider_links: Mapped[list["ScheduledTaskProvider"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="ScheduledTaskProvider.id"
    )
    runs: Mapped[list["ScheduledRun"]] = relationship(
        back_populates="task", order_by="desc(ScheduledRun.started_at)", passive_deletes=True
    )


class ScheduledTaskProvider(Base):
    __tablename__ = "scheduled_task_providers"
    __table_args__ = (UniqueConstraint("task_id", "provider_id", name="uq_scheduled_task_provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("scheduled_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("providers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider_name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_base_url_snapshot: Mapped[str] = mapped_column(String(600), nullable=False)

    task: Mapped[ScheduledTask] = relationship(back_populates="provider_links")
    provider: Mapped[Provider | None] = relationship(foreign_keys=[provider_id])


class ScheduledRun(Base):
    __tablename__ = "scheduled_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("scheduled_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running", index=True)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    task: Mapped[ScheduledTask | None] = relationship(back_populates="runs", foreign_keys=[task_id])
    tests: Mapped[list["ConnectivityTest"]] = relationship(
        back_populates="scheduled_run", passive_deletes=True
    )


class MonitoringTask(Base):
    __tablename__ = "monitoring_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("providers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider_name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_base_url_snapshot: Mapped[str] = mapped_column(String(600), nullable=False)
    model_id: Mapped[str] = mapped_column(String(260), nullable=False)
    client_profile: Mapped[str] = mapped_column(String(32), nullable=False, default="openai_chat")
    network_route: Mapped[str] = mapped_column(String(180), nullable=False, default="default")
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    retry_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    retry_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_success_notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_failure_notified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notify_on_recovery: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_on_failure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    provider: Mapped[Provider | None] = relationship(foreign_keys=[provider_id])
    checks: Mapped[list["MonitoringCheck"]] = relationship(
        back_populates="task", order_by="desc(MonitoringCheck.checked_at)", passive_deletes=True
    )


class MonitoringCheck(Base):
    __tablename__ = "monitoring_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("monitoring_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task_name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_id: Mapped[int | None] = mapped_column(
        ForeignKey("providers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider_name_snapshot: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_base_url_snapshot: Mapped[str] = mapped_column(String(600), nullable=False)
    model_id: Mapped[str] = mapped_column(String(260), nullable=False)
    client_profile: Mapped[str] = mapped_column(String(32), nullable=False, default="openai_chat")
    network_route: Mapped[str] = mapped_column(String(160), nullable=False, default="直连")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_response_excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    notification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_sent")
    notification_event: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    notification_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notification_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    task: Mapped[MonitoringTask | None] = relationship(back_populates="checks", foreign_keys=[task_id])
    provider: Mapped[Provider | None] = relationship(foreign_keys=[provider_id])


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
    trigger_source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual", index=True)
    scheduled_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("scheduled_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

    provider: Mapped[Provider] = relationship(back_populates="tests")
    scheduled_run: Mapped[ScheduledRun | None] = relationship(back_populates="tests", foreign_keys=[scheduled_run_id])
