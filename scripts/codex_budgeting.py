from __future__ import annotations


DEFAULT_CODEX_BUDGET = {
    "hourly_pct_min": 50,
    "weekly_pct_min": 0,
    "monthly_credits_min": 0,
}


def account_key(account: dict) -> str:
    return (
        account.get("email")
        or account.get("displayName")
        or account.get("accountId")
        or "unknown"
    )


def normalize_codex_budget_settings(settings: dict) -> None:
    default_budget = settings.setdefault("codex_default_budget", {})
    for key, value in DEFAULT_CODEX_BUDGET.items():
        default_budget.setdefault(key, value)
    settings.setdefault("codex_account_budgets", {})


def resolve_account_budget(settings: dict, account: dict) -> dict:
    normalize_codex_budget_settings(settings)
    budget = dict(settings["codex_default_budget"])
    specific = settings["codex_account_budgets"].get(account_key(account), {})
    budget.update(specific)
    return budget


def evaluate_account_budget(settings: dict, account: dict) -> dict:
    budget = resolve_account_budget(settings, account)
    usage = account.get("usage") or {}
    reasons = []

    hourly_remaining = usage.get("secondaryRemainingPercent")
    if hourly_remaining is not None and float(hourly_remaining) < float(budget["hourly_pct_min"]):
        reasons.append(f"hourly {float(hourly_remaining):.0f}% < {float(budget['hourly_pct_min']):.0f}%")

    weekly_remaining = usage.get("primaryRemainingPercent")
    if weekly_remaining is not None and float(weekly_remaining) < float(budget["weekly_pct_min"]):
        reasons.append(f"weekly {float(weekly_remaining):.0f}% < {float(budget['weekly_pct_min']):.0f}%")

    monthly_remaining = account.get("remainingCreditsSecondary")
    monthly_min = budget["monthly_credits_min"]
    if monthly_remaining is not None and monthly_min and float(monthly_remaining) < float(monthly_min):
        reasons.append(f"monthly {float(monthly_remaining):.0f} < {float(monthly_min):.0f} credits")

    status = (account.get("status") or "").lower()
    if status in ("paused", "disabled", "deactivated", "quota_exceeded", "rate_limited"):
        reasons.append(f"status={status}")

    return {
        "key": account_key(account),
        "budget": budget,
        "ok": not reasons,
        "reasons": reasons,
        "hourly_remaining_pct": hourly_remaining,
        "weekly_remaining_pct": weekly_remaining,
        "monthly_remaining_credits": monthly_remaining,
    }


def eligible_codex_accounts(settings: dict, accounts: list[dict]) -> list[dict]:
    eligible = []
    for account in accounts:
        evaluation = evaluate_account_budget(settings, account)
        if evaluation["ok"]:
            eligible.append(account)
    return eligible
