from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def effective_started_at(
    created_at: datetime,
    *,
    timezone: str = "Asia/Shanghai",
    start_hour: int = 9,
    end_hour: int = 18,
) -> datetime:
    zone = ZoneInfo(timezone)
    local = created_at.astimezone(zone)
    work_start = time(start_hour)
    work_end = time(end_hour % 24)

    if local.weekday() >= 5:
        days = 7 - local.weekday()
        return datetime.combine(local.date() + timedelta(days=days), work_start, zone)
    if local.time() < work_start:
        return datetime.combine(local.date(), work_start, zone)
    if local.time() < work_end:
        return local

    next_date = local.date() + timedelta(days=1)
    while next_date.weekday() >= 5:
        next_date += timedelta(days=1)
    return datetime.combine(next_date, work_start, zone)


def week_start_for(value: datetime, timezone: str = "Asia/Shanghai"):
    local_date = value.astimezone(ZoneInfo(timezone)).date()
    return local_date - timedelta(days=local_date.weekday())
