from __future__ import annotations

from datetime import datetime


def parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def is_real_reset(
    old_reset_at: str | None,
    new_reset_at: str | None,
    old_used_pct: float | None,
    new_used_pct: float | None,
    *,
    min_drop_pct: float,
    min_window_shift_sec: float,
    new_used_max_pct: float | None = None,
) -> bool:
    old_dt = parse_iso_dt(old_reset_at)
    new_dt = parse_iso_dt(new_reset_at)
    if old_dt is None or new_dt is None or new_dt <= old_dt:
        return False
    if (new_dt - old_dt).total_seconds() < min_window_shift_sec:
        return False
    if old_used_pct is None or new_used_pct is None:
        return False
    if old_used_pct - new_used_pct < min_drop_pct:
        return False
    if new_used_max_pct is not None and new_used_pct > new_used_max_pct:
        return False
    return True


def detect_resets(old: dict, new_usage: dict | None, new_codex: list) -> list[str]:
    events = []
    if new_usage:
        if is_real_reset(
            old.get("claude_5h_resets_at"),
            new_usage["5h_resets_at"],
            old.get("claude_5h_pct"),
            new_usage["5h_pct"],
            min_drop_pct=10,
            min_window_shift_sec=3 * 3600,
        ):
            events.append(f"🔄 **Claude 5h window reset** — now at {new_usage['5h_pct']:.0f}% used")

        if is_real_reset(
            old.get("claude_7d_resets_at"),
            new_usage["7d_resets_at"],
            old.get("claude_7d_pct"),
            new_usage["7d_pct"],
            min_drop_pct=30,
            min_window_shift_sec=5 * 24 * 3600,
            new_used_max_pct=25,
        ):
            events.append(f"🗓️ **Claude weekly (7d) reset** — now at {new_usage['7d_pct']:.0f}% used")

    old_codex = {a["email"]: a for a in old.get("codex_accounts", []) if a.get("email")}
    for acc in new_codex:
        email = acc.get("email", "")
        usage = acc.get("usage") or {}
        old_acc = old_codex.get(email)
        if not old_acc:
            continue
        old_usage = old_acc.get("usage") or {}
        if is_real_reset(
            old_acc.get("resetAtSecondary"),
            acc.get("resetAtSecondary"),
            100 - old_usage.get("secondaryRemainingPercent", 100),
            100 - usage.get("secondaryRemainingPercent", 100),
            min_drop_pct=10,
            min_window_shift_sec=3 * 3600,
        ):
            sec_rem = usage.get("secondaryRemainingPercent", 0)
            events.append(f"⏱️ **Codex window reset** `{email}` — {sec_rem:.0f}% remaining")

        new_cred = acc.get("remainingCreditsSecondary")
        old_cred = old_acc.get("remainingCreditsSecondary")
        if new_cred and old_cred and new_cred > old_cred * 1.2:
            cap = acc.get("capacityCreditsSecondary", 0)
            events.append(f"📅 **Codex monthly credits reset** `{email}` — {new_cred:.0f}/{cap:.0f} credits")
    return events
