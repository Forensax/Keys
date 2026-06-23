from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from .config import settings
from .db import SessionLocal
from .models import (
    ConnectivityTest,
    Provider,
    ScheduledRun,
    ScheduledTask,
    ScheduledTaskProvider,
    utc_now,
)
from .security import SETTING_SCHEDULER_WRAPPED_VAULT_KEY, get_scheduler_fernet, get_setting
from .test_runner import run_provider_connectivity_test


MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 10_080
SCHEDULE_TARGETS = {"all", "group", "providers"}
SCHEDULE_KINDS = {"interval", "daily"}
SCHEDULER_POLL_SECONDS = 15
SCHEDULED_HISTORY_DAYS = 180

_loop_task: asyncio.Task[None] | None = None
_worker_tasks: set[asyncio.Task[None]] = set()
_running_task_ids: set[int] = set()
_provider_locks: dict[int, asyncio.Lock] = {}
_test_semaphore: asyncio.Semaphore | None = None
_stop_event: asyncio.Event | None = None


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def scheduler_timezone(name: str | None = None) -> ZoneInfo:
    try:
        return ZoneInfo(name or settings.app_timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Shanghai")


def calculate_next_run(task: ScheduledTask, now: datetime | None = None) -> datetime:
    current = as_utc(now or utc_now()) or utc_now()
    if task.schedule_kind == "daily":
        hour, minute = (int(part) for part in (task.daily_time or "00:00").split(":"))
        zone = scheduler_timezone(task.timezone_name)
        local_now = current.astimezone(zone)
        candidate = datetime.combine(local_now.date(), time(hour, minute), tzinfo=zone)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    interval = max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, task.interval_minutes or 60))
    existing = as_utc(task.next_run_at)
    if existing is None:
        return current + timedelta(minutes=interval)
    candidate = existing
    while candidate <= current:
        candidate += timedelta(minutes=interval)
    return candidate


def validate_schedule_values(
    *,
    name: str,
    target_type: str,
    group_id: int | None,
    provider_ids: list[int],
    schedule_kind: str,
    interval_minutes: int | None,
    daily_time: str | None,
) -> list[str]:
    errors: list[str] = []
    if not name.strip():
        errors.append("任务名称不能为空。")
    elif len(name.strip()) > 160:
        errors.append("任务名称不能超过 160 个字符。")
    if target_type not in SCHEDULE_TARGETS:
        errors.append("任务目标类型无效。")
    elif target_type == "group" and group_id is None:
        errors.append("请选择一个分组。")
    elif target_type == "providers" and not provider_ids:
        errors.append("请至少选择一个中转站。")
    if schedule_kind not in SCHEDULE_KINDS:
        errors.append("调度类型无效。")
    elif schedule_kind == "interval":
        if interval_minutes is None or not MIN_INTERVAL_MINUTES <= interval_minutes <= MAX_INTERVAL_MINUTES:
            errors.append("间隔必须在 15 到 10080 分钟之间。")
    else:
        try:
            parsed = datetime.strptime(daily_time or "", "%H:%M")
            if parsed.strftime("%H:%M") != daily_time:
                raise ValueError
        except ValueError:
            errors.append("每日时间必须是 HH:MM 格式。")
    return errors


def task_target_summary(task: ScheduledTask) -> str:
    if task.target_type == "all":
        return "全部已启用中转站"
    if task.target_type == "group":
        return f"分组：{task.group.name}" if task.group else "分组已不存在"
    names = [link.provider_name_snapshot for link in task.provider_links]
    if not names:
        return "未选择中转站"
    preview = "、".join(names[:3])
    return preview if len(names) <= 3 else f"{preview} 等 {len(names)} 个"


def task_schedule_summary(task: ScheduledTask) -> str:
    if task.schedule_kind == "daily":
        return f"每天 {task.daily_time}（{task.timezone_name}）"
    return f"每 {task.interval_minutes} 分钟"


def scheduler_vault_state() -> str:
    if not settings.session_secret_configured:
        return "unconfigured"
    with SessionLocal() as db:
        if not get_setting(db, SETTING_SCHEDULER_WRAPPED_VAULT_KEY):
            return "unauthorized"
        return "ready" if get_scheduler_fernet(db, settings.session_secret) else "locked"


def set_task_provider_links(task: ScheduledTask, providers: list[Provider]) -> None:
    task.provider_links.clear()
    for provider in providers:
        task.provider_links.append(
            ScheduledTaskProvider(
                provider_id=provider.id,
                provider_name_snapshot=provider.name,
                provider_base_url_snapshot=provider.base_url,
            )
        )


def freeze_task_targets(db, task: ScheduledTask) -> tuple[list[int], int, str]:
    skipped = 0
    error = ""
    if task.target_type == "all":
        ids = list(
            db.scalars(
                select(Provider.id)
                .where(Provider.archived_at.is_(None), Provider.enabled.is_(True))
                .order_by(Provider.id)
            ).all()
        )
    elif task.target_type == "group":
        if task.group_id is None:
            return [], 0, "任务关联的分组已不存在。"
        ids = list(
            db.scalars(
                select(Provider.id)
                .where(
                    Provider.group_id == task.group_id,
                    Provider.archived_at.is_(None),
                    Provider.enabled.is_(True),
                )
                .order_by(Provider.id)
            ).all()
        )
    else:
        ids = []
        for link in task.provider_links:
            provider = db.get(Provider, link.provider_id) if link.provider_id is not None else None
            if provider is None or provider.archived_at is not None or not provider.enabled:
                skipped += 1
            else:
                ids.append(provider.id)
    return ids, skipped, error


def _default_model_id(provider: Provider) -> str | None:
    if provider.test_model_id:
        return provider.test_model_id
    if provider.models:
        return provider.models[0].model_id
    return None


async def _test_provider(provider_id: int, run_id: int, fernet: Fernet) -> str:
    global _test_semaphore
    if _test_semaphore is None:
        _test_semaphore = asyncio.Semaphore(5)
    lock = _provider_locks.setdefault(provider_id, asyncio.Lock())
    async with _test_semaphore, lock:
        with SessionLocal() as db:
            provider = db.scalar(
                select(Provider)
                .where(Provider.id == provider_id)
                .options(
                    selectinload(Provider.models),
                    selectinload(Provider.tests),
                    selectinload(Provider.default_proxy),
                )
            )
            if provider is None or provider.archived_at is not None or not provider.enabled:
                return "skipped"
            test = await run_provider_connectivity_test(
                db,
                provider,
                _default_model_id(provider),
                provider.test_client_profile or provider.client_profile,
                provider.test_network_route or "default",
                fernet,
                trigger_source="scheduled",
                scheduled_run_id=run_id,
            )
            return test.status


async def execute_task_run(
    task_id: int,
    *,
    trigger: str,
    fernet: Fernet,
    scheduled_for: datetime | None = None,
) -> None:
    try:
        with SessionLocal() as db:
            task = db.scalar(
                select(ScheduledTask)
                .where(ScheduledTask.id == task_id)
                .options(selectinload(ScheduledTask.provider_links))
            )
            if task is None:
                return
            provider_ids, initial_skipped, target_error = freeze_task_targets(db, task)
            run = ScheduledRun(
                task_id=task.id,
                task_name_snapshot=task.name,
                trigger=trigger,
                scheduled_for=scheduled_for,
                status="running",
                total_count=len(provider_ids) + initial_skipped,
                skipped_count=initial_skipped,
                error_message=target_error,
            )
            db.add(run)
            db.commit()
            db.refresh(run)
            run_id = run.id

        results = await asyncio.gather(
            *(_test_provider(provider_id, run_id, fernet) for provider_id in provider_ids),
            return_exceptions=True,
        )
        success = sum(result == "success" for result in results)
        failed = sum(result == "failed" or isinstance(result, Exception) for result in results)
        skipped = initial_skipped + sum(result == "skipped" for result in results)
        errors = [str(result) for result in results if isinstance(result, Exception)]
        with SessionLocal() as db:
            run = db.get(ScheduledRun, run_id)
            if run is None:
                return
            run.success_count = success
            run.failed_count = failed
            run.skipped_count = skipped
            run.finished_at = utc_now()
            if target_error:
                run.status = "failed"
            elif failed and success:
                run.status = "partial"
            elif failed:
                run.status = "failed"
            elif skipped and not success:
                run.status = "skipped"
            else:
                run.status = "success"
            if errors:
                run.error_message = "；".join(errors[:3])
            db.commit()

            # 检查是否需要发送 Telegram 通知
            from .notifications import send_telegram_notification, should_notify_for_task
            task = db.get(ScheduledTask, task_id)
            if task and task.enable_telegram_notification:
                for test in tests:
                    provider = db.get(Provider, test.provider_id)
                    if provider and should_notify_for_task(task, test.status):
                        await send_telegram_notification(db, provider, test, fernet)
    finally:
        _running_task_ids.discard(task_id)


def enqueue_task_run(
    task_id: int,
    *,
    trigger: str,
    fernet: Fernet,
    scheduled_for: datetime | None = None,
) -> bool:
    if task_id in _running_task_ids:
        return False
    _running_task_ids.add(task_id)
    worker = asyncio.create_task(
        execute_task_run(task_id, trigger=trigger, fernet=fernet, scheduled_for=scheduled_for)
    )
    _worker_tasks.add(worker)
    worker.add_done_callback(_worker_tasks.discard)
    return True


async def scan_due_tasks() -> None:
    if not settings.session_secret_configured:
        return
    now = utc_now()
    with SessionLocal() as db:
        fernet = get_scheduler_fernet(db, settings.session_secret)
        if fernet is None:
            return
        task_ids = list(
            db.scalars(
                select(ScheduledTask.id)
                .where(
                    ScheduledTask.enabled.is_(True),
                    ScheduledTask.next_run_at.is_not(None),
                    ScheduledTask.next_run_at <= now,
                )
                .order_by(ScheduledTask.next_run_at, ScheduledTask.id)
            ).all()
        )
        due: list[tuple[int, datetime | None]] = []
        for task_id in task_ids:
            task = db.get(ScheduledTask, task_id)
            if task is None or task.id in _running_task_ids:
                continue
            scheduled_for = as_utc(task.next_run_at)
            task.next_run_at = calculate_next_run(task, now)
            due.append((task.id, scheduled_for))
        db.commit()
    for task_id, scheduled_for in due:
        enqueue_task_run(task_id, trigger="scheduled", fernet=fernet, scheduled_for=scheduled_for)


def cleanup_scheduled_history(now: datetime | None = None) -> None:
    cutoff = (as_utc(now or utc_now()) or utc_now()) - timedelta(days=SCHEDULED_HISTORY_DAYS)
    with SessionLocal() as db:
        db.execute(
            delete(ConnectivityTest).where(
                ConnectivityTest.trigger_source == "scheduled",
                ConnectivityTest.tested_at < cutoff,
            )
        )
        db.execute(delete(ScheduledRun).where(ScheduledRun.started_at < cutoff))
        db.commit()


async def _scheduler_loop() -> None:
    global _stop_event
    cleanup_scheduled_history()
    last_cleanup = utc_now()
    while _stop_event is not None and not _stop_event.is_set():
        try:
            await scan_due_tasks()
            if utc_now() - last_cleanup >= timedelta(days=1):
                cleanup_scheduled_history()
                last_cleanup = utc_now()
        except Exception:
            # A scheduler scan must never terminate the web process.
            pass
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=SCHEDULER_POLL_SECONDS)
        except TimeoutError:
            continue


async def start_scheduler() -> None:
    global _loop_task, _stop_event, _test_semaphore
    if _loop_task is not None and not _loop_task.done():
        return
    _stop_event = asyncio.Event()
    _test_semaphore = asyncio.Semaphore(5)
    _loop_task = asyncio.create_task(_scheduler_loop())


async def stop_scheduler() -> None:
    global _loop_task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _loop_task is not None:
        await _loop_task
    if _worker_tasks:
        await asyncio.gather(*list(_worker_tasks), return_exceptions=True)
    _loop_task = None
    _stop_event = None


def task_is_running(task_id: int) -> bool:
    return task_id in _running_task_ids
