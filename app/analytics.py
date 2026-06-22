from __future__ import annotations

import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import ConnectivityTest, Provider, utc_now


VALID_RANGES = {"24h", "7d", "30d", "90d", "all"}
VALID_SOURCES = {"all", "manual", "scheduled"}


@dataclass(frozen=True)
class StatRecord:
    tested_at: datetime
    provider_id: int
    provider_name: str
    status: str
    latency_ms: int | None
    error_message: str
    source: str


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def range_start(range_key: str, now: datetime) -> datetime | None:
    durations = {
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
        "90d": timedelta(days=90),
    }
    duration = durations.get(range_key)
    return now - duration if duration else None


def percentile_95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, int(len(ordered) * 0.95 + 0.999999) - 1)
    return ordered[index]


def load_real_records(
    db: Session,
    *,
    range_key: str,
    provider_id: int | None,
    source: str,
    now: datetime | None = None,
) -> list[StatRecord]:
    current = _aware_utc(now or utc_now())
    query = (
        select(
            ConnectivityTest.tested_at,
            ConnectivityTest.provider_id,
            Provider.name,
            ConnectivityTest.status,
            ConnectivityTest.latency_ms,
            ConnectivityTest.error_message,
            ConnectivityTest.trigger_source,
        )
        .join(Provider, Provider.id == ConnectivityTest.provider_id)
        .order_by(ConnectivityTest.tested_at)
    )
    start = range_start(range_key, current)
    if start is not None:
        query = query.where(ConnectivityTest.tested_at >= start)
    if provider_id is not None:
        query = query.where(ConnectivityTest.provider_id == provider_id)
    if source != "all":
        query = query.where(ConnectivityTest.trigger_source == source)
    return [
        StatRecord(
            tested_at=_aware_utc(row.tested_at),
            provider_id=row.provider_id,
            provider_name=row.name,
            status=row.status,
            latency_ms=row.latency_ms,
            error_message=row.error_message or "",
            source=row.trigger_source or "manual",
        )
        for row in db.execute(query).all()
    ]


def generate_demo_records(now: datetime | None = None) -> list[StatRecord]:
    rng = random.Random(20260622)
    current = _aware_utc(now or utc_now()).replace(minute=0, second=0, microsecond=0)
    providers = [
        (-1, "示例·星云", 820, 0.96),
        (-2, "示例·极光", 1250, 0.90),
        (-3, "示例·风帆", 560, 0.98),
        (-4, "示例·远山", 2100, 0.83),
        (-5, "示例·灯塔", 980, 0.93),
        (-6, "示例·潮汐", 1550, 0.88),
    ]
    errors = ["上游请求超时", "HTTP 429: 请求过于频繁", "连接被拒绝", "模型暂时不可用"]
    records: list[StatRecord] = []
    for day in range(30):
        for hour in (1, 7, 13, 19):
            tested_at = (current - timedelta(days=29 - day)).replace(hour=hour)
            if tested_at > current:
                continue
            for provider_id, name, baseline, success_rate in providers:
                success = rng.random() <= success_rate
                latency = max(120, int(rng.gauss(baseline + day * 3, baseline * 0.18)))
                records.append(
                    StatRecord(
                        tested_at=tested_at,
                        provider_id=provider_id,
                        provider_name=name,
                        status="success" if success else "failed",
                        latency_ms=latency,
                        error_message="" if success else rng.choice(errors),
                        source="scheduled" if hour != 13 else "manual",
                    )
                )
    return records


def filter_records(
    records: list[StatRecord],
    *,
    range_key: str,
    provider_id: int | None,
    source: str,
    now: datetime | None = None,
) -> list[StatRecord]:
    current = _aware_utc(now or utc_now())
    start = range_start(range_key, current)
    return [
        record
        for record in records
        if (start is None or record.tested_at >= start)
        and (provider_id is None or record.provider_id == provider_id)
        and (source == "all" or record.source == source)
    ]


def build_statistics(records: list[StatRecord], *, range_key: str) -> dict:
    zone = ZoneInfo(settings.app_timezone)
    hourly = range_key == "24h"

    def bucket_label(value: datetime) -> str:
        local = value.astimezone(zone)
        return local.strftime("%m-%d %H:00" if hourly else "%Y-%m-%d")

    total = len(records)
    successes = [record for record in records if record.status == "success"]
    failures = [record for record in records if record.status != "success"]
    latencies = [record.latency_ms for record in records if record.latency_ms is not None]
    bucket_counts: dict[str, Counter] = defaultdict(Counter)
    provider_records: dict[tuple[int, str], list[StatRecord]] = defaultdict(list)
    latency_buckets: dict[tuple[int, str], dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    failure_reasons: Counter[str] = Counter()
    source_counts: Counter[str] = Counter(record.source for record in records)

    for record in records:
        label = bucket_label(record.tested_at)
        bucket_counts[label]["success" if record.status == "success" else "failed"] += 1
        key = (record.provider_id, record.provider_name)
        provider_records[key].append(record)
        if record.latency_ms is not None:
            latency_buckets[key][label].append(record.latency_ms)
        if record.status != "success":
            reason = (record.error_message.splitlines()[0].strip() or "未知错误")[:120]
            failure_reasons[reason] += 1

    trend = [
        {"label": label, "success": values["success"], "failed": values["failed"]}
        for label, values in sorted(bucket_counts.items())
    ]
    provider_rows = []
    for (provider_id, name), items in provider_records.items():
        item_latencies = [item.latency_ms for item in items if item.latency_ms is not None]
        item_successes = sum(item.status == "success" for item in items)
        latest = max(items, key=lambda item: item.tested_at)
        provider_rows.append(
            {
                "provider_id": provider_id,
                "name": name,
                "total": len(items),
                "success_rate": round(item_successes * 100 / len(items), 1),
                "average_latency": round(mean(item_latencies)) if item_latencies else None,
                "p95_latency": percentile_95(item_latencies),
                "latest_status": latest.status,
                "latest_at": latest.tested_at,
            }
        )
    provider_rows.sort(key=lambda item: (-item["total"], item["name"]))
    top_keys = [
        (row["provider_id"], row["name"])
        for row in provider_rows[:5]
    ]
    latency_series = []
    all_labels = [item["label"] for item in trend]
    for key in top_keys:
        values_by_label = latency_buckets[key]
        latency_series.append(
            {
                "name": key[1],
                "points": [
                    {
                        "label": label,
                        "value": round(mean(values_by_label[label])) if values_by_label.get(label) else None,
                    }
                    for label in all_labels
                ],
            }
        )

    return {
        "summary": {
            "total": total,
            "success_rate": round(len(successes) * 100 / total, 1) if total else None,
            "failed": len(failures),
            "average_latency": round(mean(latencies)) if latencies else None,
            "p95_latency": percentile_95(latencies),
            "providers": len(provider_records),
        },
        "trend": trend,
        "latency_series": latency_series,
        "providers": provider_rows,
        "failures": [
            {"reason": reason, "count": count}
            for reason, count in failure_reasons.most_common(8)
        ],
        "sources": {
            "manual": source_counts["manual"],
            "scheduled": source_counts["scheduled"],
        },
        "chart_data": {
            "trend": trend,
            "latencySeries": latency_series,
            "failures": [
                {"reason": reason, "count": count}
                for reason, count in failure_reasons.most_common(8)
            ],
            "sources": {
                "manual": source_counts["manual"],
                "scheduled": source_counts["scheduled"],
            },
        },
    }
