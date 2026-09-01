from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
DEFAULT_PROVIDER_IDS = ("opencode", "opencode-go")
GO_LIMITS_USD = {
    "5h": 12.0,
    "7d": 30.0,
    "month": 60.0,
}


@dataclass(frozen=True)
class SessionRecord:
    model_id: str
    provider_id: str
    cost_usd: float
    tokens_input: int
    tokens_output: int
    tokens_reasoning: int
    tokens_cache_read: int
    tokens_cache_write: int
    updated_at: datetime


def _coerce_model(model_raw: str | None) -> tuple[str, str]:
    if not model_raw:
        return "", ""
    try:
        parsed = json.loads(model_raw)
    except Exception:
        return str(model_raw), ""
    if isinstance(parsed, dict):
        return str(parsed.get("id") or ""), str(parsed.get("providerID") or "")
    return str(model_raw), ""


def _month_start(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month_start(dt: datetime) -> datetime:
    start = _month_start(dt)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


def _window_summary(
    records: list[SessionRecord],
    *,
    now: datetime,
    start: datetime,
    limit_usd: float,
    reset_at: datetime | None,
) -> dict:
    in_window = [record for record in records if record.updated_at >= start]
    cost_usd = sum(record.cost_usd for record in in_window)
    return {
        "cost_usd": round(cost_usd, 4),
        "limit_usd": limit_usd,
        "pct_used": round((cost_usd / limit_usd) * 100, 2) if limit_usd > 0 else 0.0,
        "session_count": len(in_window),
        "tokens_input": sum(record.tokens_input for record in in_window),
        "tokens_output": sum(record.tokens_output for record in in_window),
        "tokens_reasoning": sum(record.tokens_reasoning for record in in_window),
        "tokens_cache_read": sum(record.tokens_cache_read for record in in_window),
        "tokens_cache_write": sum(record.tokens_cache_write for record in in_window),
        "reset_at": reset_at.isoformat() if reset_at else None,
        "window_started_at": start.isoformat(),
        "window_age_hours": round((now - start).total_seconds() / 3600, 2),
    }


def load_opencode_sessions(
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
    provider_ids: tuple[str, ...] = DEFAULT_PROVIDER_IDS,
) -> list[SessionRecord]:
    path = Path(db_path).expanduser()
    if not path.exists():
        return []

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """
            SELECT model, cost, tokens_input, tokens_output, tokens_reasoning,
                   tokens_cache_read, tokens_cache_write, time_updated
            FROM session
            WHERE time_updated IS NOT NULL
            ORDER BY time_updated DESC
            """
        ).fetchall()
    finally:
        conn.close()

    records: list[SessionRecord] = []
    for row in rows:
        model_raw, cost, tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, time_updated = row
        model_id, provider_id = _coerce_model(model_raw)
        if provider_id not in provider_ids:
            continue
        try:
            updated_at = datetime.fromtimestamp(int(time_updated) / 1000, tz=timezone.utc)
        except Exception:
            continue
        records.append(
            SessionRecord(
                model_id=model_id or "unknown",
                provider_id=provider_id,
                cost_usd=float(cost or 0.0),
                tokens_input=int(tokens_input or 0),
                tokens_output=int(tokens_output or 0),
                tokens_reasoning=int(tokens_reasoning or 0),
                tokens_cache_read=int(tokens_cache_read or 0),
                tokens_cache_write=int(tokens_cache_write or 0),
                updated_at=updated_at,
            )
        )
    return records


def summarize_opencode_usage(
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
    provider_ids: tuple[str, ...] = DEFAULT_PROVIDER_IDS,
    now: datetime | None = None,
) -> dict | None:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    records = load_opencode_sessions(db_path=db_path, provider_ids=provider_ids)
    if not records:
        return None

    last_session_at = max(record.updated_at for record in records)
    five_hour_start = now - timedelta(hours=5)
    seven_day_start = now - timedelta(days=7)
    month_start = _month_start(now)
    next_month = _next_month_start(now)

    five_hour_records = [record for record in records if record.updated_at >= five_hour_start]
    seven_day_records = [record for record in records if record.updated_at >= seven_day_start]

    next_clear_5h = min((record.updated_at + timedelta(hours=5) for record in five_hour_records), default=None)
    next_clear_7d = min((record.updated_at + timedelta(days=7) for record in seven_day_records), default=None)

    model_totals: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"sessions": 0, "cost_usd": 0.0, "tokens_input": 0, "tokens_output": 0}
    )
    for record in records:
        bucket = model_totals[record.model_id]
        bucket["sessions"] += 1
        bucket["cost_usd"] += record.cost_usd
        bucket["tokens_input"] += record.tokens_input
        bucket["tokens_output"] += record.tokens_output

    models = [
        {
            "model_id": model_id,
            "sessions": int(values["sessions"]),
            "cost_usd": round(float(values["cost_usd"]), 4),
            "tokens_input": int(values["tokens_input"]),
            "tokens_output": int(values["tokens_output"]),
        }
        for model_id, values in sorted(
            model_totals.items(),
            key=lambda item: (float(item[1]["cost_usd"]), int(item[1]["sessions"])),
            reverse=True,
        )
    ]

    return {
        "db_path": str(Path(db_path).expanduser()),
        "session_count": len(records),
        "last_session_at": last_session_at.isoformat(),
        "provider_ids": list(provider_ids),
        "windows": {
            "5h": _window_summary(
                records,
                now=now,
                start=five_hour_start,
                limit_usd=GO_LIMITS_USD["5h"],
                reset_at=next_clear_5h,
            ),
            "7d": _window_summary(
                records,
                now=now,
                start=seven_day_start,
                limit_usd=GO_LIMITS_USD["7d"],
                reset_at=next_clear_7d,
            ),
            "month": _window_summary(
                records,
                now=now,
                start=month_start,
                limit_usd=GO_LIMITS_USD["month"],
                reset_at=next_month,
            ),
        },
        "models": models,
    }
