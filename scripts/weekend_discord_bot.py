#!/usr/bin/env python3
"""Weekend mode Discord bot.

Standalone discord.py bot providing rich interactive UI for weekend autonomous mode.
Uses a SEPARATE bot token from cc-connect (two connections with the same token conflict).

Setup:
  1. Create a new Discord Application at https://discord.com/developers/applications
  2. Create a bot under the application, copy the token
  3. Paste the token into ~/.weekend-mode/config.json under DISCORD_BOT_TOKEN
  4. Invite the bot to your server with scopes: bot, applications.commands
     Permissions: Send Messages, Read Messages, Use Slash Commands

Run: ~/.venv-weekend/bin/python3 ~/scripts/weekend_discord_bot.py
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks
from codex_budgeting import account_key, evaluate_account_budget, normalize_codex_budget_settings
from opencode_usage import summarize_opencode_usage
from reset_detection import detect_resets

# ── paths ──────────────────────────────────────────────────────────────────

WEEKEND_DIR = Path.home() / ".weekend-mode"
STATE_FILE = WEEKEND_DIR / "state.json"
CONFIG_FILE = WEEKEND_DIR / "config.json"
HANDOFF_FILE = WEEKEND_DIR / "handoff.md"
SESSION_LOG = WEEKEND_DIR / "session.log"
STOP_FLAG = WEEKEND_DIR / "stop.flag"
SKIP_FLAG = WEEKEND_DIR / "skip.flag"
CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
USAGE_SNAPSHOT = WEEKEND_DIR / "usage_snapshot.json"
ANNOUNCEMENT_LOG = WEEKEND_DIR / "last_announcement.json"
CODEX_LB_URL = "http://127.0.0.1:2455"
ENGINES = ("claude", "claude-go", "codex", "opencode", "gemini")
MODEL_LANES = ("default", "orchestration", "worker", "research")
DEFAULT_MODELS = {
    "claude": "sonnet",
    "claude-go": "default",
    "codex": "gpt-5.4",
    "opencode": "opencode/deepseek-v4-flash-free",
    "gemini": "gemini-2.5-pro",
}
DEFAULT_LANE_MODELS = {
    "orchestration": {"claude": "sonnet", "claude-go": "default", "codex": "gpt-5.4", "opencode": "opencode/deepseek-v4-flash-free", "gemini": "gemini-2.5-pro"},
    "worker": {"claude": "sonnet", "claude-go": "default", "codex": "gpt-5.4", "opencode": "opencode/north-mini-code-free", "gemini": "gemini-2.5-flash"},
    "research": {"claude": "sonnet", "claude-go": "default", "codex": "gpt-5.4", "opencode": "opencode/deepseek-v4-flash-free", "gemini": "gemini-2.5-pro"},
}
SMOKE_TEST_MODELS = {
    "claude": "fable",
    "claude-go": "default",
    "codex": "gpt-5.3-codex-spark",
    "opencode": "opencode/north-mini-code-free",
    "gemini": "gemini-2.5-flash",
}
SMOKE_TEST_PROMPT = "Reply with exactly: ok"
DEFAULT_ENGINE_TOGGLES = {
    "claude": True,
    "claude-go": True,
    "codex": True,
    "opencode": False,
    "gemini": False,
}
DEFAULT_WORKER_TOGGLES = {
    "claude": True,
    "claude-go": True,
    "codex": True,
    "opencode": True,
    "gemini": True,
}
DEFAULT_SETTINGS = {
    "claude_5h_threshold_pct": 90,
    "claude_7d_threshold_pct": 95,
    "codex_guard_pct": 50,
    "codex_default_budget": {
        "hourly_pct_min": 50,
        "weekly_pct_min": 0,
        "monthly_credits_min": 0,
    },
    "codex_account_budgets": {},
    "opencode_go_alert_pct": 100,
    "opencode_guard_pct_5h": 50,
    "opencode_guard_pct_month": 50,
    "opencode_guard_relax_hours": 2,
    "system_temp_warn_c": {
        "cpu_package": 85,
        "nvme": 70,
        "wifi": 75,
    },
    "system_temp_cooldown_min": 30,
    "max_task_budget_min": 120,
    "deadline_override_utc": None,
    "project_queue": [
        "hermes-trading",
        "unified-expense-os",
        "tokeneyes",
    ],
    "default_models": DEFAULT_MODELS,
    "lane_models": DEFAULT_LANE_MODELS,
    "start_allowed_orchestrators": DEFAULT_ENGINE_TOGGLES,
    "start_allowed_workers": DEFAULT_WORKER_TOGGLES,
}

# ── helpers ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    return normalize_state(json.loads(STATE_FILE.read_text()))


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


def _deep_fill_missing(target: dict, defaults: dict) -> bool:
    changed = False
    for key, value in defaults.items():
        if key not in target:
            target[key] = json.loads(json.dumps(value))
            changed = True
            continue
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            changed = _deep_fill_missing(target[key], value) or changed
    return changed


def normalize_state(state: dict) -> dict:
    state.setdefault("settings", {})
    _deep_fill_missing(state["settings"], DEFAULT_SETTINGS)
    normalize_codex_budget_settings(state["settings"])
    state["settings"]["codex_default_budget"]["hourly_pct_min"] = state["settings"].get("codex_guard_pct", 50)
    for lane_name, models in DEFAULT_LANE_MODELS.items():
        lane_models = state["settings"]["lane_models"].setdefault(lane_name, {})
        _deep_fill_missing(lane_models, models)
    _deep_fill_missing(state["settings"]["default_models"], DEFAULT_MODELS)
    _deep_fill_missing(state["settings"]["start_allowed_orchestrators"], DEFAULT_ENGINE_TOGGLES)
    _deep_fill_missing(state["settings"]["start_allowed_workers"], DEFAULT_WORKER_TOGGLES)
    state.setdefault(
        "session_allowed_orchestrators",
        json.loads(json.dumps(state["settings"]["start_allowed_orchestrators"])),
    )
    state.setdefault(
        "session_allowed_workers",
        json.loads(json.dumps(state["settings"]["start_allowed_workers"])),
    )
    return state


def resolve_lane_model(state: dict, lane: str, engine: str) -> str:
    settings = state["settings"]
    if lane == "default":
        return settings.get("default_models", {}).get(engine, DEFAULT_MODELS[engine])
    lane_models = settings.get("lane_models", {})
    model = lane_models.get(lane, {}).get(engine)
    if model:
        return model
    default_models = settings.get("default_models", {})
    return default_models.get(engine, DEFAULT_MODELS[engine])


def enabled_engines(toggle_map: dict) -> list[str]:
    return [engine for engine in ENGINES if toggle_map.get(engine)]


def format_toggle_summary(toggle_map: dict) -> str:
    active = enabled_engines(toggle_map)
    return ", ".join(active) if active else "none"


def engine_available(engine: str) -> bool:
    if engine == "claude":
        return CREDS_PATH.exists() or shutil.which("claude") is not None
    if engine == "claude-go":
        return shutil.which("claude-go") is not None or Path.home().joinpath("bin/claude-go").exists()
    if engine == "opencode":
        return shutil.which("opencode") is not None or Path.home().joinpath(".opencode/bin/opencode").exists()
    return shutil.which(engine) is not None


def run_provider_smoke_test(engine: str, timeout_sec: int = 45) -> dict:
    model = SMOKE_TEST_MODELS[engine]
    if not engine_available(engine):
        return {"ok": False, "model": model, "detail": "CLI not installed or unavailable"}
    try:
        if engine == "claude":
            cmd = [
                "timeout", str(timeout_sec),
                "claude", "--dangerously-skip-permissions",
                "--model", model,
                "-p", SMOKE_TEST_PROMPT,
            ]
            env = os.environ.copy()
        elif engine == "codex":
            cmd = [
                "timeout", str(timeout_sec),
                "codex", "exec",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "--model", model,
                SMOKE_TEST_PROMPT,
            ]
            env = {**os.environ, "CODEX_HOME": "/home/notdc/.codex-cc-connect"}
        else:
            cmd = [
                "timeout", str(timeout_sec),
                "gemini",
                "--model", model,
                "--prompt", SMOKE_TEST_PROMPT,
                "--yolo",
                "--skip-trust",
            ]
            env = os.environ.copy()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec + 5, env=env)
        output = (result.stdout + "\n" + result.stderr).strip()
        ok = result.returncode == 0 and "ok" in output.lower()
        detail = output.splitlines()[-1][:160] if output else f"exit={result.returncode}"
        return {"ok": ok, "model": model, "detail": detail}
    except subprocess.TimeoutExpired:
        return {"ok": False, "model": model, "detail": "timed out"}
    except Exception as exc:
        return {"ok": False, "model": model, "detail": str(exc)[:160]}


def summarize_test_results(results: dict[str, dict]) -> list[str]:
    lines = ["**Provider Smoke Tests**"]
    for engine in ENGINES:
        result = results.get(engine)
        if not result:
            continue
        status = "PASS" if result["ok"] else "FAIL"
        lines.append(f"`{engine}` {status} via `{result['model']}` — {result['detail']}")
    return lines


def render_model_settings(state: dict) -> list[str]:
    settings = state["settings"]
    lines = [
        "**Model Controls**",
        f"Start-time orchestrators: **{format_toggle_summary(state.get('session_allowed_orchestrators', {}))}**",
        f"Start-time workers: **{format_toggle_summary(state.get('session_allowed_workers', {}))}**",
        "",
        "**Defaults**",
    ]
    for engine in ENGINES:
        suffix = "" if engine_available(engine) else " _(CLI missing)_"
        lines.append(f"`{engine}` → `{resolve_lane_model(state, 'default', engine)}`{suffix}")
    for lane in ("orchestration", "worker", "research"):
        lines.append("")
        lines.append(f"**{lane.title()} Models**")
        for engine in ENGINES:
            lines.append(f"`{engine}` → `{resolve_lane_model(state, lane, engine)}`")
    return lines


def usage_bar(pct: float, width: int = 10) -> str:
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


def render_opencode_compact(summary: dict | None) -> list[str]:
    if not summary:
        return ["_(opencode usage unavailable)_"]
    lines = ["**OpenCode Go Usage**"]
    labels = {
        "5h": "5h",
        "7d": "7d",
        "month": "month",
    }
    for window_name in ("5h", "7d", "month"):
        window = summary["windows"][window_name]
        lines.append(
            f"{labels[window_name]:<5} {usage_bar(min(float(window['pct_used']), 100.0))}  "
            f"${window['cost_usd']:.2f}/${window['limit_usd']:.2f}  "
            f"{window['pct_used']:.0f}%  resets {fmt_reset(window['reset_at'])}"
        )
    top_models = summary.get("models", [])[:3]
    if top_models:
        lines.append("Top models: " + ", ".join(f"{item['model_id']} (${item['cost_usd']:.2f})" for item in top_models))
    return lines


def render_opencode_detailed(summary: dict | None) -> list[str]:
    if not summary:
        return ["OpenCode usage unavailable. No `opencode.db` data found."]
    lines = render_opencode_compact(summary)
    lines.append(f"DB: `{summary['db_path']}`")
    lines.append(f"Sessions tracked: {summary['session_count']}")
    lines.append(f"Last session: {fmt_reset(summary['last_session_at'])}")
    if summary.get("models"):
        lines.append("")
        lines.append("**Model breakdown**")
        for item in summary["models"][:8]:
            lines.append(
                f"`{item['model_id']}` — {item['sessions']} session(s), "
                f"${item['cost_usd']:.2f}, in={item['tokens_input']}, out={item['tokens_output']}"
            )
    return lines


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except Exception:
        return None


def _fmt_bytes(num: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def _safe_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def get_system_stats() -> dict:
    stats: dict[str, object] = {"temps": {}, "warnings": []}

    try:
        uptime_s = float(_read_text("/proc/uptime").split()[0])
        h = int(uptime_s // 3600)
        m = int((uptime_s % 3600) // 60)
        stats["uptime"] = f"{h}h {m}m"
    except Exception:
        stats["uptime"] = "unknown"

    try:
        load1, load5, load15 = os.getloadavg()
        stats["load"] = f"{load1:.2f} {load5:.2f} {load15:.2f}"
    except Exception:
        stats["load"] = "unknown"

    try:
        meminfo = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            meminfo[key] = int(value.strip().split()[0]) * 1024
        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", 0)
        used = max(0, total - available)
        used_pct = (used / total * 100) if total else 0
        stats["memory"] = f"{_fmt_bytes(used)}/{_fmt_bytes(total)} ({used_pct:.0f}%)"
    except Exception:
        stats["memory"] = "unknown"

    try:
        disk = shutil.disk_usage(str(Path.home()))
        used_pct = (disk.used / disk.total * 100) if disk.total else 0
        stats["disk"] = f"{_fmt_bytes(disk.used)}/{_fmt_bytes(disk.total)} ({used_pct:.0f}%)"
    except Exception:
        stats["disk"] = "unknown"

    sensors_output = ""
    try:
        result = subprocess.run(["sensors"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            sensors_output = result.stdout
    except Exception:
        pass

    cpu_package = None
    nvme = None
    wifi = None
    for line in sensors_output.splitlines():
        if "Package id 0:" in line and cpu_package is None:
            match = re.search(r"([+-]?\d+(?:\.\d+)?)°C", line)
            if match:
                cpu_package = float(match.group(1))
        elif "Composite:" in line and nvme is None:
            match = re.search(r"([+-]?\d+(?:\.\d+)?)°C", line)
            if match:
                nvme = float(match.group(1))
        elif "iwlwifi" in sensors_output and "temp1:" in line and wifi is None:
            match = re.search(r"([+-]?\d+(?:\.\d+)?)°C", line)
            if match:
                wifi = float(match.group(1))

    temps = stats["temps"]
    if cpu_package is not None:
        temps["cpu_package"] = cpu_package
    if nvme is not None:
        temps["nvme"] = nvme
    if wifi is not None:
        temps["wifi"] = wifi

    return stats


def render_system_stats(stats: dict) -> list[str]:
    temps = stats.get("temps", {})
    temp_bits = []
    for key in ("cpu_package", "nvme", "wifi"):
        value = temps.get(key)
        if value is not None:
            temp_bits.append(f"{key}={value:.1f}C")
    return [
        "**System**",
        f"Uptime: {stats.get('uptime', 'unknown')}",
        f"Load: {stats.get('load', 'unknown')}",
        f"Memory: {stats.get('memory', 'unknown')}",
        f"Disk: {stats.get('disk', 'unknown')}",
        "Temps: " + (", ".join(temp_bits) if temp_bits else "unavailable"),
    ]


def detect_temp_warnings(state: dict, stats: dict) -> list[dict]:
    settings = state["settings"]
    thresholds = settings.get("system_temp_warn_c", {})
    temps = stats.get("temps", {})
    alerts = []
    for key, threshold in thresholds.items():
        value = temps.get(key)
        if value is None:
            continue
        if float(value) >= float(threshold):
            alerts.append({"sensor": key, "value": float(value), "threshold": float(threshold)})
    return alerts


def fmt_reset(iso: str, window_hours: int = 0) -> str:
    """Format time until reset. If iso is in the past and window_hours > 0,
    assume it's the window-start timestamp and add window_hours to get next reset."""
    try:
        from datetime import timedelta
        dt = datetime.fromisoformat(iso)
        now = datetime.now(timezone.utc)
        if window_hours > 0 and dt < now:
            dt = dt + timedelta(hours=window_hours)
        delta = dt - now
        total = int(delta.total_seconds())
        if total <= 0:
            return "imminent"
        h, m = divmod(total // 60, 60)
        return f"{h}h {m}m" if h else f"{m}m"
    except Exception:
        return iso


def get_claude_usage() -> dict | None:
    try:
        import requests
        creds = json.loads(CREDS_PATH.read_text())
        token = creds["claudeAiOauth"]["accessToken"]
        resp = requests.get(USAGE_ENDPOINT, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {
            "5h_pct": float(data["five_hour"]["utilization"]),
            "5h_resets_at": data["five_hour"]["resets_at"],
            "7d_pct": float(data["seven_day"]["utilization"]),
            "7d_resets_at": data["seven_day"]["resets_at"],
        }
    except Exception as e:
        return None


def get_approved_issues() -> list[dict]:
    try:
        result = subprocess.run(["bd", "list", "--status=open"],
                                capture_output=True, text=True, timeout=15)
        issues = []
        for line in result.stdout.splitlines():
            if "[weekend-approved]" not in line.lower():
                continue
            match = re.search(r'\b(home-\w+)\b', line)
            if match:
                issues.append({"id": match.group(1), "line": line.strip()})
        return issues
    except Exception:
        return []


def approve_issue(issue_id: str) -> bool:
    try:
        # Read current notes, append tag if not present
        detail = subprocess.run(["bd", "show", issue_id],
                                capture_output=True, text=True, timeout=10)
        notes_match = re.search(r'Notes:\s*(.+)', detail.stdout)
        current_notes = notes_match.group(1).strip() if notes_match else ""
        if "[weekend-approved]" in current_notes.lower():
            return True
        new_notes = (current_notes + " [weekend-approved]").strip()
        r = subprocess.run(["bd", "update", issue_id, "--notes", new_notes],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def revoke_issue(issue_id: str) -> bool:
    try:
        detail = subprocess.run(["bd", "show", issue_id],
                                capture_output=True, text=True, timeout=10)
        notes_match = re.search(r'Notes:\s*(.+)', detail.stdout)
        current_notes = notes_match.group(1).strip() if notes_match else ""
        new_notes = re.sub(r'\[weekend-approved\]', '', current_notes, flags=re.IGNORECASE).strip()
        r = subprocess.run(["bd", "update", issue_id, "--notes", new_notes],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def run_tmux_cmd(session: str, cmd: str, capture_lines: int = 20) -> str:
    """Run a command in a tmux session (creates session if needed), return output."""
    try:
        # Create session if not exists
        check = subprocess.run(["tmux", "has-session", "-t", session],
                               capture_output=True)
        if check.returncode != 0:
            subprocess.run(["tmux", "new-session", "-d", "-s", session],
                           check=True, timeout=5)
        subprocess.run(["tmux", "send-keys", "-t", session, cmd, "Enter"],
                       check=True, timeout=5)
        await_proc = subprocess.run(["sleep", "3"])
        output = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session, "-S", f"-{capture_lines}"],
            capture_output=True, text=True, timeout=5
        )
        return output.stdout[-1800:] if output.stdout else "(no output)"
    except Exception as e:
        return f"Error: {e}"


# ── Codex-lb helpers ──────────────────────────────────────────────────────

def get_codex_accounts() -> list:
    """Query codex-lb /api/accounts. Returns [] if unavailable or unconfigured."""
    import urllib.request, urllib.error
    config = load_config()
    key = config.get("CODEX_LB_API_KEY", "").strip()
    if not key:
        return []
    try:
        req = urllib.request.Request(
            f"{CODEX_LB_URL}/api/accounts",
            headers={"Authorization": f"Bearer {key}"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("accounts", data) if isinstance(data, dict) else data
    except Exception:
        return []


def fmt_codex_account(a: dict) -> str:
    icon = {"active": "●", "rate_limited": "⏳", "quota_exceeded": "⛔", "paused": "⏸"}.get(
        (a.get("status") or "").lower(), "?"
    )
    name = a.get("email") or a.get("displayName") or "unknown"
    usage = a.get("usage") or {}
    sec_rem = usage.get("secondaryRemainingPercent")
    reset_at = a.get("resetAtSecondary", "")
    parts = [f"{icon} `{name}`"]
    if sec_rem is not None:
        used = 100 - sec_rem
        parts.append(f"{usage_bar(used)} {used:.0f}% used  resets {fmt_reset(reset_at)}")
    cred_rem = a.get("remainingCreditsSecondary")
    cred_cap = a.get("capacityCreditsSecondary")
    if cred_rem is not None and cred_cap:
        parts.append(f"credits: {cred_rem:.0f}/{cred_cap:.0f}")
    return "  ".join(parts)


def render_codex_budget_settings(state: dict, accounts: list[dict] | None = None) -> list[str]:
    settings = state["settings"]
    normalize_codex_budget_settings(settings)
    default_budget = settings["codex_default_budget"]
    lines = [
        "**Codex Budgets**",
        f"Default hourly minimum remaining: **{default_budget['hourly_pct_min']}%**",
        f"Default weekly minimum remaining: **{default_budget['weekly_pct_min']}%**",
        f"Default monthly minimum credits: **{default_budget['monthly_credits_min']}**",
    ]
    account_budgets = settings.get("codex_account_budgets", {})
    if account_budgets:
        lines.append("")
        lines.append("**Per-account overrides**")
        for key in sorted(account_budgets):
            budget = account_budgets[key]
            lines.append(
                f"`{key}` → hourly>={budget.get('hourly_pct_min', default_budget['hourly_pct_min'])}% "
                f"weekly>={budget.get('weekly_pct_min', default_budget['weekly_pct_min'])}% "
                f"monthly>={budget.get('monthly_credits_min', default_budget['monthly_credits_min'])}"
            )
    if accounts:
        lines.append("")
        lines.append("**Live account evaluation**")
        for account in accounts:
            evaluation = evaluate_account_budget(settings, account)
            verdict = "PASS" if evaluation["ok"] else "BLOCK"
            suffix = "" if evaluation["ok"] else " — " + "; ".join(evaluation["reasons"])
            lines.append(f"`{evaluation['key']}` {verdict}{suffix}")
    return lines


# ── Usage snapshot (for reset detection) ──────────────────────────────────

def load_snapshot() -> dict:
    if USAGE_SNAPSHOT.exists():
        try:
            return json.loads(USAGE_SNAPSHOT.read_text())
        except Exception:
            pass
    return {}


def save_snapshot(snap: dict):
    USAGE_SNAPSHOT.write_text(json.dumps(snap, indent=2))


# ── Work suggestions ───────────────────────────────────────────────────────

def get_ready_beads_tasks(limit: int = 5) -> list[dict]:
    try:
        result = subprocess.run(
            ["bd", "-C", "/home/notdc", "ready", "--json", "--limit", str(limit)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []
        payload = json.loads(result.stdout)
        return payload if isinstance(payload, list) else []
    except Exception:
        return []


def suggest_weekend_work() -> str:
    """Query beads for ready issues and format as a weekend suggestion."""
    try:
        tasks = get_ready_beads_tasks(limit=8)
        if not tasks:
            return "_(no ready Beads work found)_"
        lines = ["**📋 Ready Beads work:**"]
        all_ids = []
        for item in tasks:
            issue_id = item.get("id", "unknown")
            title = (item.get("title") or "").strip()
            issue_type = item.get("issue_type") or "task"
            priority = item.get("priority")
            lines.append(f"  • `{issue_id}` P{priority} [{issue_type}] {title[:80]}")
            all_ids.append(issue_id)
        if all_ids:
            lines.append(f"\n`/weekend-approve {' '.join(all_ids[:8])}`")
        return "\n".join(lines)
    except Exception as e:
        return f"_(could not fetch suggestions: {e})_"


# ── Announcement builder ───────────────────────────────────────────────────

def build_announcement(trigger: str, usage: dict | None, codex_accounts: list) -> str:
    parts = [f"## 🤖 Weekend Mode — {trigger}"]
    if usage:
        parts += [
            "",
            "**Claude Usage**",
            f"5h  {usage_bar(usage['5h_pct'])}  {usage['5h_pct']:.0f}%  resets in {fmt_reset(usage['5h_resets_at'], window_hours=5)}",
            f"7d  {usage_bar(usage['7d_pct'])}  {usage['7d_pct']:.0f}%  resets {fmt_reset(usage['7d_resets_at'])}",
        ]
    else:
        parts.append("_(claude usage unavailable)_")

    if codex_accounts:
        parts.append("\n**Codex Accounts**")
        for acc in codex_accounts[:4]:
            if (acc.get("status") or "").lower() not in ("quota_exceeded", "deactivated"):
                parts.append(fmt_codex_account(acc))

    parts.append("")
    parts.extend(render_opencode_compact(summarize_opencode_usage()))

    parts.append("")
    parts.extend(render_system_stats(get_system_stats()))

    parts.append("")
    parts.append(suggest_weekend_work())
    parts.append("\nRun `/weekend-on` to start, `/weekend-config` to adjust thresholds.")
    return "\n".join(parts)


# ── Announcement log helpers ───────────────────────────────────────────────

def load_announcement_log() -> dict:
    if ANNOUNCEMENT_LOG.exists():
        try:
            return json.loads(ANNOUNCEMENT_LOG.read_text())
        except Exception:
            pass
    return {}


def save_announcement_log(log: dict):
    ANNOUNCEMENT_LOG.write_text(json.dumps(log, indent=2))


# ── Discord bot setup ──────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = False
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


# ── Views ──────────────────────────────────────────────────────────────────

def build_weekend_on_message(state: dict, usage: dict | None) -> str:
    settings = state["settings"]
    queue = settings["project_queue"]
    usage_line = ""
    if usage:
        usage_line = (f"\nCurrent usage: 5h={usage['5h_pct']:.0f}% "
                      f"({usage_bar(usage['5h_pct'])})  "
                      f"7d={usage['7d_pct']:.0f}% ({usage_bar(usage['7d_pct'])})")
    orchestrators = state.get("session_allowed_orchestrators", {})
    workers = state.get("session_allowed_workers", {})
    lines = [
        "Start weekend autonomous mode?",
        f"Projects: {' → '.join(queue)}",
        f"Deadline: Mon 07:30 IST  |  Stop threshold: 5h≥{settings['claude_5h_threshold_pct']}%",
        f"Orchestrators enabled: {format_toggle_summary(orchestrators)}",
        f"Workers enabled: {format_toggle_summary(workers)}",
        "",
        "**Models**",
    ]
    for lane in ("default", "orchestration", "worker", "research"):
        parts = [f"{engine}={resolve_lane_model(state, lane, engine)}" for engine in ENGINES]
        lines.append(f"{lane}: " + " | ".join(parts))
    return "\n".join(lines) + usage_line


class ToggleEngineButton(discord.ui.Button):
    def __init__(self, scope: str, engine: str):
        self.scope = scope
        self.engine = engine
        super().__init__(style=discord.ButtonStyle.secondary, row=0 if scope == "orchestrators" else 1)
        self.refresh({})

    def refresh(self, toggles: dict):
        enabled = toggles.get(self.engine, False)
        prefix = "Orch" if self.scope == "orchestrators" else "Work"
        marker = "ON" if enabled else "OFF"
        self.label = f"{prefix}: {self.engine} {marker}"
        self.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, WeekendOnView):
            return
        target_key = "session_allowed_orchestrators" if self.scope == "orchestrators" else "session_allowed_workers"
        toggles = view.draft_state[target_key]
        toggles[self.engine] = not toggles.get(self.engine, False)
        if not enabled_engines(toggles):
            toggles[self.engine] = True
            await interaction.response.send_message(
                f"{self.scope[:-1].title()} list cannot be empty.",
                ephemeral=True,
            )
            return
        view.test_results = {}
        view.refresh_buttons()
        await interaction.response.edit_message(
            content=build_weekend_on_message(view.draft_state, view.usage),
            view=view,
        )


class WeekendOnView(discord.ui.View):
    def __init__(self, state: dict, usage: dict | None):
        super().__init__(timeout=60)
        self.draft_state = json.loads(json.dumps(state))
        self.usage = usage
        self.test_results: dict[str, dict] = {}
        for engine in ENGINES:
            self.add_item(ToggleEngineButton("orchestrators", engine))
        for engine in ENGINES:
            self.add_item(ToggleEngineButton("workers", engine))
        self.refresh_buttons()

    def refresh_buttons(self):
        for item in self.children:
            if isinstance(item, ToggleEngineButton):
                key = "session_allowed_orchestrators" if item.scope == "orchestrators" else "session_allowed_workers"
                item.refresh(self.draft_state[key])

    def selected_test_engines(self) -> list[str]:
        selected = set(enabled_engines(self.draft_state["session_allowed_orchestrators"]))
        selected.update(enabled_engines(self.draft_state["session_allowed_workers"]))
        return [engine for engine in ENGINES if engine in selected]

    async def run_selected_provider_tests(self) -> dict[str, dict]:
        results = {}
        for engine in self.selected_test_engines():
            results[engine] = await asyncio.to_thread(run_provider_smoke_test, engine)
        self.test_results = results
        return results

    def test_failures(self) -> list[str]:
        failures = []
        for engine in self.selected_test_engines():
            result = self.test_results.get(engine)
            if result and not result["ok"]:
                failures.append(engine)
        return failures

    @discord.ui.button(label="✅ Confirm Start", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.test_results:
            await interaction.response.defer()
            await self.run_selected_provider_tests()
            failures = self.test_failures()
            if failures:
                content = build_weekend_on_message(self.draft_state, self.usage)
                content += "\n\n" + "\n".join(summarize_test_results(self.test_results))
                content += f"\n\nDisable or fix failing providers before starting: {', '.join(failures)}"
                await interaction.edit_original_response(content=content, view=self)
                return
        elif self.test_failures():
            content = build_weekend_on_message(self.draft_state, self.usage)
            content += "\n\n" + "\n".join(summarize_test_results(self.test_results))
            content += f"\n\nDisable or fix failing providers before starting: {', '.join(self.test_failures())}"
            await interaction.response.edit_message(content=content, view=self)
            return
        state = load_state()
        state["last_provider_checks"] = json.loads(json.dumps(self.test_results))
        state["last_provider_check_utc"] = datetime.now(timezone.utc).isoformat()
        state["settings"]["start_allowed_orchestrators"] = json.loads(
            json.dumps(self.draft_state["session_allowed_orchestrators"])
        )
        state["settings"]["start_allowed_workers"] = json.loads(
            json.dumps(self.draft_state["session_allowed_workers"])
        )
        state["session_allowed_orchestrators"] = json.loads(
            json.dumps(self.draft_state["session_allowed_orchestrators"])
        )
        state["session_allowed_workers"] = json.loads(
            json.dumps(self.draft_state["session_allowed_workers"])
        )
        state["active"] = True
        # Set deadline to next Monday 02:00 UTC (= 7:30 AM IST)
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        days_to_monday = (7 - now.weekday()) % 7 or 7
        candidate = now + timedelta(days=days_to_monday)
        deadline = candidate.replace(hour=2, minute=0, second=0, microsecond=0)
        state["weekly_deadline_utc"] = deadline.isoformat()
        state["current_project_index"] = 0
        state["sessions_run"] = 0
        state["tasks_completed"] = []
        save_state(state)
        # Enable systemd timer
        subprocess.run(["systemctl", "--user", "enable", "--now", "weekend-mode.timer"],
                       capture_output=True)
        deadline_ist = deadline.replace(tzinfo=timezone.utc)
        queue = state["settings"]["project_queue"]
        await interaction.response.edit_message(
            content=f"**Weekend mode activated** ✅\n"
                    f"Deadline: Mon 07:30 IST\n"
                    f"Projects: {' → '.join(queue)}\n"
                    f"Orchestrators: {format_toggle_summary(state['session_allowed_orchestrators'])}\n"
                    f"Workers: {format_toggle_summary(state['session_allowed_workers'])}\n"
                    f"Run `/weekend-approve home-xxx ...` to queue issues for automation.",
            view=None
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)

    @discord.ui.button(label="🧪 Test Providers", style=discord.ButtonStyle.primary, row=2)
    async def test_providers(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.run_selected_provider_tests()
        content = build_weekend_on_message(self.draft_state, self.usage)
        content += "\n\n" + "\n".join(summarize_test_results(self.test_results))
        await interaction.edit_original_response(content=content, view=self)


class WeekendStatusView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="⏭ Skip task", style=discord.ButtonStyle.secondary)
    async def skip_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        SKIP_FLAG.touch()
        await interaction.response.send_message("Skip flag set — current task will stop after this tool call.", ephemeral=True)

    @discord.ui.button(label="⏸ Pause", style=discord.ButtonStyle.danger)
    async def pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        STOP_FLAG.touch()
        state = load_state()
        state["active"] = False
        save_state(state)
        subprocess.run(["systemctl", "--user", "stop", "weekend-mode.timer"], capture_output=True)
        await interaction.response.send_message("Stop flag set. Weekend mode paused.", ephemeral=True)

    @discord.ui.button(label="▶ Resume", style=discord.ButtonStyle.green)
    async def resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        STOP_FLAG.unlink(missing_ok=True)
        state = load_state()
        state["active"] = True
        save_state(state)
        subprocess.run(["systemctl", "--user", "enable", "--now", "weekend-mode.timer"], capture_output=True)
        await interaction.response.send_message("Weekend mode resumed.", ephemeral=True)


class QueueIssueView(discord.ui.View):
    def __init__(self, issue_id: str, approved: bool):
        super().__init__(timeout=300)
        self.issue_id = issue_id
        if not approved:
            self.add_item(ApproveButton(issue_id))
        else:
            self.add_item(RevokeButton(issue_id))


class ApproveButton(discord.ui.Button):
    def __init__(self, issue_id: str):
        super().__init__(label=f"✅ Approve {issue_id}", style=discord.ButtonStyle.green)
        self.issue_id = issue_id

    async def callback(self, interaction: discord.Interaction):
        ok = approve_issue(self.issue_id)
        msg = f"Approved `{self.issue_id}` ✅" if ok else f"Failed to approve `{self.issue_id}`"
        await interaction.response.send_message(msg, ephemeral=True)


class RevokeButton(discord.ui.Button):
    def __init__(self, issue_id: str):
        super().__init__(label=f"❌ Revoke {issue_id}", style=discord.ButtonStyle.danger)
        self.issue_id = issue_id

    async def callback(self, interaction: discord.Interaction):
        ok = revoke_issue(self.issue_id)
        msg = f"Revoked `{self.issue_id}`" if ok else f"Failed to revoke `{self.issue_id}`"
        await interaction.response.send_message(msg, ephemeral=True)


# ── Config UI ─────────────────────────────────────────────────────────

def _get_nested(d: dict, key_path: str):
    for part in key_path.split("."):
        d = d[part]
    return d


def _set_nested(d: dict, key_path: str, value):
    parts = key_path.split(".")
    for part in parts[:-1]:
        d = d.setdefault(part, {})
    d[parts[-1]] = value


def _parse_bool(raw: str) -> bool:
    low = raw.strip().lower()
    if low in ("true", "on", "1"):
        return True
    if low in ("false", "off", "0"):
        return False
    raise ValueError("Must be true/false/on/off/1/0")


# 9 config sections: (title, list of (key_path, label, type))
# type: "int", "bool", "str", "str_or_none", "list"
CONFIG_SECTIONS = [
    ("Claude Thresholds + Budget", [
        ("claude_5h_threshold_pct", "Claude 5h threshold (%)", "int"),
        ("claude_7d_threshold_pct", "Claude 7d threshold (%)", "int"),
        ("codex_guard_pct", "Codex guard % (min remaining)", "int"),
        ("max_task_budget_min", "Max task budget (minutes)", "int"),
    ]),
    ("OpenCode Guards", [
        ("opencode_guard_pct_5h", "OpenCode guard 5h (%)", "int"),
        ("opencode_guard_pct_month", "OpenCode guard month (%)", "int"),
        ("opencode_guard_relax_hours", "OpenCode relax (hours)", "int"),
        ("opencode_go_alert_pct", "OpenCode Go alert threshold (%)", "int"),
    ]),
    ("Engine Toggles — Orchestrators", [
        ("start_allowed_orchestrators.claude", "Claude as orchestrator", "bool"),
        ("start_allowed_orchestrators.codex", "Codex as orchestrator", "bool"),
        ("start_allowed_orchestrators.opencode", "OpenCode as orchestrator", "bool"),
        ("start_allowed_orchestrators.gemini", "Gemini as orchestrator", "bool"),
    ]),
    ("Engine Toggles — Workers", [
        ("start_allowed_workers.claude", "Claude as worker", "bool"),
        ("start_allowed_workers.codex", "Codex as worker", "bool"),
        ("start_allowed_workers.opencode", "OpenCode as worker", "bool"),
        ("start_allowed_workers.gemini", "Gemini as worker", "bool"),
    ]),
    ("Default Models", [
        ("default_models.claude", "Claude default model", "str"),
        ("default_models.codex", "Codex default model", "str"),
        ("default_models.opencode", "OpenCode default model", "str"),
        ("default_models.gemini", "Gemini default model", "str"),
    ]),
    ("Lane Models — Orchestration", [
        ("lane_models.orchestration.claude", "Orchestration Claude model", "str"),
        ("lane_models.orchestration.codex", "Orchestration Codex model", "str"),
        ("lane_models.orchestration.opencode", "Orchestration OpenCode model", "str"),
        ("lane_models.orchestration.gemini", "Orchestration Gemini model", "str"),
    ]),
    ("Lane Models — Worker", [
        ("lane_models.worker.claude", "Worker Claude model", "str"),
        ("lane_models.worker.codex", "Worker Codex model", "str"),
        ("lane_models.worker.opencode", "Worker OpenCode model", "str"),
        ("lane_models.worker.gemini", "Worker Gemini model", "str"),
    ]),
    ("Lane Models — Research", [
        ("lane_models.research.claude", "Research Claude model", "str"),
        ("lane_models.research.codex", "Research Codex model", "str"),
        ("lane_models.research.opencode", "Research OpenCode model", "str"),
        ("lane_models.research.gemini", "Research Gemini model", "str"),
    ]),
    ("Deadline + Project Queue", [
        ("deadline_override_utc", "Deadline override (ISO UTC or blank)", "str_or_none"),
        ("project_queue", "Project queue (comma-separated)", "list"),
    ]),
]


def _extract_current(settings: dict, key_path: str, field_type: str):
    try:
        val = _get_nested(settings, key_path)
    except (KeyError, TypeError):
        return ""
    if field_type == "bool":
        return "true" if val else "false"
    if val is None:
        return ""
    if isinstance(val, list):
        return ", ".join(val)
    return str(val)


def _label_short(label: str) -> str:
    return label if len(label) <= 45 else label[:42] + "..."


class SectionModal(discord.ui.Modal):
    def __init__(self, title: str, field_defs: list, state: dict):
        super().__init__(title=title)
        self.field_defs = field_defs
        settings = state["settings"]
        for key_path, label, field_type in field_defs:
            current = _extract_current(settings, key_path, field_type)
            max_len = 500 if field_type == "list" else 200
            self.add_item(discord.ui.TextInput(
                label=_label_short(label),
                default=current,
                required=True,
                max_length=max_len,
            ))

    async def on_submit(self, interaction: discord.Interaction):
        state = load_state()
        settings = state["settings"]
        changes = []
        errors = []

        for i, item in enumerate(self.children):
            key_path, label, field_type = self.field_defs[i]
            raw = item.value.strip()

            try:
                if field_type == "int":
                    val = int(raw)
                elif field_type == "bool":
                    val = _parse_bool(raw)
                elif field_type == "str_or_none":
                    val = raw if raw else None
                elif field_type == "list":
                    val = [x.strip() for x in raw.split(",") if x.strip()]
                else:
                    val = raw
            except ValueError as e:
                errors.append(f"`{label}`: {e}")
                continue

            old = _get_nested(settings, key_path)

            if val == old:
                continue

            _set_nested(settings, key_path, val)
            changes.append((label, _extract_current({"v": old}, "v", field_type), _extract_current({"v": val}, "v", field_type)))

        if "codex_guard_pct" in [fd[0] for fd in self.field_defs]:
            normalize_codex_budget_settings(settings)
            settings["codex_default_budget"]["hourly_pct_min"] = int(settings.get("codex_guard_pct", 50))

        if errors:
            await interaction.response.send_message(
                "**Validation errors:**\n" + "\n".join(errors), ephemeral=True
            )
            return

        if not changes:
            await interaction.response.send_message("No changes were made.", ephemeral=True)
            return

        save_state(state)

        lines = ["**Settings Updated**", ""]
        for label, old_str, new_str in changes:
            lines.append(f"• {label}: `{old_str}` → `{new_str}`")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


class ConfigSectionSelect(discord.ui.Select):
    def __init__(self, state: dict):
        self.state = state
        options = []
        for idx, (title, _) in enumerate(CONFIG_SECTIONS, 1):
            options.append(discord.SelectOption(
                label=f"{idx}. {title[:80]}",
                value=str(idx),
            ))
        super().__init__(
            placeholder="Choose a settings section to edit...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0]) - 1
        title, field_defs = CONFIG_SECTIONS[idx]
        self.state = load_state()
        modal = SectionModal(title, field_defs, self.state)
        await interaction.response.send_modal(modal)


class ConfigView(discord.ui.View):
    def __init__(self, state: dict):
        super().__init__(timeout=300)
        self.add_item(ConfigSectionSelect(state))


# ── Slash Commands ─────────────────────────────────────────────────────────

@tree.command(name="weekend-on", description="Start weekend autonomous mode")
async def weekend_on(interaction: discord.Interaction):
    state = load_state()
    settings = state["settings"]
    usage = get_claude_usage()

    if state.get("active"):
        await interaction.response.send_message(
            f"Weekend mode is already active.\n"
            f"Orchestrators: {format_toggle_summary(state.get('session_allowed_orchestrators', {}))}\n"
            f"Workers: {format_toggle_summary(state.get('session_allowed_workers', {}))}",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        build_weekend_on_message(state, usage),
        view=WeekendOnView(state, usage),
        ephemeral=True
    )


@tree.command(name="weekend-off", description="Stop weekend autonomous mode")
async def weekend_off(interaction: discord.Interaction):
    STOP_FLAG.touch()
    state = load_state()
    state["active"] = False
    save_state(state)
    subprocess.run(["systemctl", "--user", "stop", "weekend-mode.timer"], capture_output=True)
    done = len(state.get("tasks_completed", []))
    await interaction.response.send_message(
        f"Weekend mode stopped. Tasks completed this session: {done}", ephemeral=True
    )


@tree.command(name="weekend-status", description="Live usage + current task status")
async def weekend_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    state = load_state()
    usage = get_claude_usage()
    opencode_summary = summarize_opencode_usage()
    active = state.get("active", False)
    last = state.get("last_issue_worked") or "none"
    done = len(state.get("tasks_completed", []))

    lines = [f"**Weekend Mode: {'🟢 Active' if active else '🔴 Inactive'}**"]

    if usage:
        lines += [
            f"",
            f"**Claude Usage**",
            f"5h  {usage_bar(usage['5h_pct'])}  {usage['5h_pct']:.0f}%  resets in {fmt_reset(usage['5h_resets_at'], window_hours=5)}",
            f"7d  {usage_bar(usage['7d_pct'])}  {usage['7d_pct']:.0f}%  resets {fmt_reset(usage['7d_resets_at'])}",
        ]
    else:
        lines.append("_(claude usage unavailable)_")

    if state.get("weekly_deadline_utc"):
        lines.append(f"\nDeadline: {fmt_reset(state['weekly_deadline_utc'])} remaining")

    lines += [
        f"Sessions run: {state.get('sessions_run', 0)}",
        f"Tasks completed: {done}",
        f"Last issue: `{last}`",
        f"Orchestrators: {format_toggle_summary(state.get('session_allowed_orchestrators', {}))}",
        f"Workers: {format_toggle_summary(state.get('session_allowed_workers', {}))}",
    ]

    approved = get_approved_issues()
    lines.append(f"Approved queue: {len(approved)} issues")
    ready_tasks = get_ready_beads_tasks(limit=5)
    if ready_tasks:
        lines.append(f"Ready Beads tasks: {len(ready_tasks)} shown below")
        for item in ready_tasks:
            lines.append(f"• `{item.get('id', 'unknown')}` P{item.get('priority', '?')} {item.get('title', '')[:72]}")
    else:
        lines.append("Ready Beads tasks: 0")
    lines.extend([""] + render_system_stats(get_system_stats()))
    lines.extend([""] + render_opencode_compact(opencode_summary))
    codex_accounts = get_codex_accounts()
    if codex_accounts:
        lines.extend(["", "**Codex budgets**"])
        for account in codex_accounts[:5]:
            evaluation = evaluate_account_budget(state["settings"], account)
            lines.append(
                f"{evaluation['key']}: {'PASS' if evaluation['ok'] else 'BLOCK'}"
                + ("" if evaluation["ok"] else f" ({'; '.join(evaluation['reasons'])})")
            )
    if state.get("last_provider_check_utc"):
        lines.append(f"Last provider check: {state['last_provider_check_utc']}")
        checks = state.get("last_provider_checks", {})
        for engine in ENGINES:
            result = checks.get(engine)
            if result:
                lines.append(
                    f"{engine}: {'PASS' if result.get('ok') else 'FAIL'} via {result.get('model')} ({result.get('detail')})"
                )

    await interaction.followup.send("\n".join(lines), view=WeekendStatusView(), ephemeral=True)


@tree.command(name="weekend-opencode-usage", description="Show OpenCode Go usage from the local OpenCode database")
async def weekend_opencode_usage(interaction: discord.Interaction):
    summary = summarize_opencode_usage()
    await interaction.response.send_message("\n".join(render_opencode_detailed(summary)), ephemeral=True)


@tree.command(name="system-status", description="Show current temperatures and system stats")
async def system_status(interaction: discord.Interaction):
    stats = get_system_stats()
    state = load_state()
    lines = render_system_stats(stats)
    warnings = detect_temp_warnings(state, stats)
    if warnings:
        lines.append("")
        lines.append("**Warnings**")
        for item in warnings:
            lines.append(
                f"{item['sensor']}: {item['value']:.1f}C >= {item['threshold']:.1f}C"
            )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@tree.command(name="weekend-queue", description="Show and manage the approved issue queue")
async def weekend_queue(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    approved = get_approved_issues()
    if not approved:
        await interaction.followup.send(
            "No approved issues. Use `/weekend-approve home-xxx` to add some.",
            ephemeral=True
        )
        return

    # Show up to 5 issues with individual approve/revoke buttons
    lines = [f"**Approved queue ({len(approved)} issues)**"]
    views = []
    for item in approved[:5]:
        lines.append(f"• `{item['id']}` {item['line'][:80]}")
        views.append((item["id"], True))

    content = "\n".join(lines)
    # Discord only allows one View per message; show first 5 with a combined view
    view = discord.ui.View(timeout=300)
    for issue_id, _ in views[:5]:
        view.add_item(RevokeButton(issue_id))

    await interaction.followup.send(content, view=view, ephemeral=True)


@tree.command(name="weekend-approve", description="Approve issues for automation: home-xxx home-yyy ...")
@app_commands.describe(issue_ids="Space-separated beads issue IDs (e.g. home-3tf home-rso2)")
async def weekend_approve(interaction: discord.Interaction, issue_ids: str):
    ids = issue_ids.strip().split()
    if not ids:
        await interaction.response.send_message("No IDs provided.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    results = []
    for issue_id in ids:
        ok = approve_issue(issue_id.strip())
        results.append(f"{'✅' if ok else '❌'} `{issue_id}`")
    await interaction.followup.send("\n".join(results), ephemeral=True)


@tree.command(name="weekend-revoke", description="Remove automation approval from an issue")
@app_commands.describe(issue_id="Beads issue ID (e.g. home-3tf)")
async def weekend_revoke(interaction: discord.Interaction, issue_id: str):
    ok = revoke_issue(issue_id.strip())
    await interaction.response.send_message(
        f"{'Revoked' if ok else 'Failed to revoke'} `{issue_id}`", ephemeral=True
    )


@tree.command(name="weekend-set-channel", description="Set this channel as the weekend mode announcement channel")
async def weekend_set_channel(interaction: discord.Interaction):
    state = load_state()
    state["announcement_channel_id"] = interaction.channel_id
    save_state(state)
    await interaction.response.send_message(
        f"✅ Announcement channel set to <#{interaction.channel_id}>. "
        f"Friday 11 PM IST, Saturday 11 AM IST, and reset notifications will post here.",
        ephemeral=False
    )


@tree.command(name="weekend-config", description="View and edit weekend mode settings")
async def weekend_config(interaction: discord.Interaction):
    state = load_state()
    s = state["settings"]
    queue_str = ", ".join(s.get("project_queue", []))
    temp_warn = s.get("system_temp_warn_c", {})
    lines = [
        "**Weekend Mode Settings** (click to edit)",
        f"Claude 5h threshold: **{s['claude_5h_threshold_pct']}%**",
        f"Claude 7d threshold: **{s['claude_7d_threshold_pct']}%**",
        f"Codex guard: **{s['codex_guard_pct']}%** minimum remaining",
        f"Codex default budget: **hourly>={s['codex_default_budget']['hourly_pct_min']}% weekly>={s['codex_default_budget']['weekly_pct_min']}% monthly>={s['codex_default_budget']['monthly_credits_min']}**",
        f"OpenCode Go alert threshold: **{s.get('opencode_go_alert_pct', 100)}%** of official window limits",
        f"Temp warn: **cpu>={temp_warn.get('cpu_package', 'n/a')}C nvme>={temp_warn.get('nvme', 'n/a')}C wifi>={temp_warn.get('wifi', 'n/a')}C**",
        f"Temp cooldown: **{s.get('system_temp_cooldown_min', 30)} min**",
        f"Max task budget: **{s['max_task_budget_min']} min**",
        f"Deadline override: **{s.get('deadline_override_utc') or 'auto (Mon 07:30 IST)'}**",
        f"Project queue: **{queue_str}**",
        "Use `/weekend-models` and `/weekend-model-set` for model lanes.",
        "Use `/weekend-codex-budgets` and `/weekend-codex-budget-set` for per-account Codex policy.",
        "Use `/system-temp-config` for temperature warning thresholds.",
    ]
    await interaction.response.send_message(
        "\n".join(lines), view=ConfigView(state), ephemeral=True
    )


@tree.command(name="weekend-models", description="Show default, orchestration, worker, and research models")
async def weekend_models(interaction: discord.Interaction):
    state = load_state()
    await interaction.response.send_message("\n".join(render_model_settings(state)), ephemeral=True)


@tree.command(name="weekend-model-set", description="Set a model for a lane and engine")
@app_commands.describe(
    lane="default, orchestration, worker, or research",
    engine="claude, codex, or gemini",
    model="Model name. Use 'inherit' to fall back to the engine default for non-default lanes.",
)
async def weekend_model_set(interaction: discord.Interaction, lane: str, engine: str, model: str):
    lane = lane.strip().lower()
    engine = engine.strip().lower()
    model = model.strip()
    if lane not in MODEL_LANES:
        await interaction.response.send_message(f"Invalid lane `{lane}`.", ephemeral=True)
        return
    if engine not in ENGINES:
        await interaction.response.send_message(f"Invalid engine `{engine}`.", ephemeral=True)
        return
    state = load_state()
    if lane == "default":
        if model.lower() == "inherit":
            await interaction.response.send_message("`inherit` is not valid for the default lane.", ephemeral=True)
            return
        state["settings"]["default_models"][engine] = model
    else:
        if model.lower() == "inherit":
            state["settings"]["lane_models"].setdefault(lane, {}).pop(engine, None)
            resolved = resolve_lane_model(state, lane, engine)
            save_state(state)
            await interaction.response.send_message(
                f"Reset `{lane}` `{engine}` to inherit default → `{resolved}`",
                ephemeral=True,
            )
            return
        state["settings"]["lane_models"].setdefault(lane, {})[engine] = model
    save_state(state)
    await interaction.response.send_message(
        f"Updated `{lane}` `{engine}` → `{resolve_lane_model(state, lane, engine)}`",
        ephemeral=True,
    )


@tree.command(name="weekend-codex-budgets", description="Show Codex default and per-account budget policy")
async def weekend_codex_budgets(interaction: discord.Interaction):
    state = load_state()
    accounts = get_codex_accounts()
    await interaction.response.send_message(
        "\n".join(render_codex_budget_settings(state, accounts)),
        ephemeral=True,
    )


@tree.command(name="weekend-codex-budget-set", description="Set Codex budget limits for default policy or one account")
@app_commands.describe(
    account="Account email/display name, or 'default' for the global policy",
    hourly_pct_min="Minimum secondary-window remaining percent",
    weekly_pct_min="Minimum primary-window remaining percent",
    monthly_credits_min="Minimum remaining monthly credits",
)
async def weekend_codex_budget_set(
    interaction: discord.Interaction,
    account: str,
    hourly_pct_min: app_commands.Range[int, 0, 100],
    weekly_pct_min: app_commands.Range[int, 0, 100],
    monthly_credits_min: app_commands.Range[int, 0, 100000],
):
    state = load_state()
    settings = state["settings"]
    normalize_codex_budget_settings(settings)
    payload = {
        "hourly_pct_min": int(hourly_pct_min),
        "weekly_pct_min": int(weekly_pct_min),
        "monthly_credits_min": int(monthly_credits_min),
    }
    key = account.strip()
    if key.lower() == "default":
        settings["codex_default_budget"] = payload
        settings["codex_guard_pct"] = payload["hourly_pct_min"]
        save_state(state)
        await interaction.response.send_message(
            f"Updated default Codex budget → hourly>={payload['hourly_pct_min']}% weekly>={payload['weekly_pct_min']}% monthly>={payload['monthly_credits_min']}",
            ephemeral=True,
        )
        return
    settings["codex_account_budgets"][key] = payload
    save_state(state)
    await interaction.response.send_message(
        f"Updated Codex budget for `{key}` → hourly>={payload['hourly_pct_min']}% weekly>={payload['weekly_pct_min']}% monthly>={payload['monthly_credits_min']}",
        ephemeral=True,
    )


@tree.command(name="system-temp-config", description="Set temperature warning thresholds and cooldown")
@app_commands.describe(
    cpu_package_c="Warn when CPU package reaches this temperature",
    nvme_c="Warn when NVMe reaches this temperature",
    wifi_c="Warn when WiFi sensor reaches this temperature",
    cooldown_min="Minimum minutes between repeated warnings per sensor",
)
async def system_temp_config(
    interaction: discord.Interaction,
    cpu_package_c: app_commands.Range[int, 40, 120],
    nvme_c: app_commands.Range[int, 30, 120],
    wifi_c: app_commands.Range[int, 30, 120],
    cooldown_min: app_commands.Range[int, 1, 360],
):
    state = load_state()
    state["settings"]["system_temp_warn_c"] = {
        "cpu_package": int(cpu_package_c),
        "nvme": int(nvme_c),
        "wifi": int(wifi_c),
    }
    state["settings"]["system_temp_cooldown_min"] = int(cooldown_min)
    save_state(state)
    await interaction.response.send_message(
        f"Updated temp warnings → cpu>={cpu_package_c}C nvme>={nvme_c}C wifi>={wifi_c}C, cooldown={cooldown_min} min",
        ephemeral=True,
    )


# ── tmux commands ──────────────────────────────────────────────────────────

tmux_group = app_commands.Group(name="tmux", description="tmux session management")


@tmux_group.command(name="list", description="List active tmux sessions")
async def tmux_list(interaction: discord.Interaction):
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}  windows=#{session_windows}  created=#{session_created_string}"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        await interaction.response.send_message("No active tmux sessions.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"**tmux sessions**\n```\n{result.stdout[:1800]}\n```", ephemeral=True
    )


@tmux_group.command(name="run", description="Run a command in a tmux session")
@app_commands.describe(session="Session name (created if not exists)", cmd="Command to run")
async def tmux_run(interaction: discord.Interaction, session: str, cmd: str):
    await interaction.response.defer(ephemeral=True)
    output = run_tmux_cmd(session, cmd)
    await interaction.followup.send(
        f"**`{session}` ← `{cmd[:60]}`**\n```\n{output[:1800]}\n```", ephemeral=True
    )


@tmux_group.command(name="tail", description="Show last 30 lines from a tmux pane")
@app_commands.describe(session="Session name")
async def tmux_tail(interaction: discord.Interaction, session: str):
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", session, "-S", "-30"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        await interaction.response.send_message(f"Session `{session}` not found.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"**`{session}` tail**\n```\n{result.stdout[-1800:]}\n```", ephemeral=True
    )


@tmux_group.command(name="kill", description="Kill a tmux session")
@app_commands.describe(session="Session name")
async def tmux_kill(interaction: discord.Interaction, session: str):
    result = subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    if result.returncode == 0:
        await interaction.response.send_message(f"Killed session `{session}`.", ephemeral=True)
    else:
        await interaction.response.send_message(f"Session `{session}` not found.", ephemeral=True)


tree.add_command(tmux_group)


@tree.command(name="run", description="One-shot: run a shell command and return output")
@app_commands.describe(cmd="Shell command to run")
async def run_cmd(interaction: discord.Interaction, cmd: str):
    await interaction.response.defer(ephemeral=True)
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=30
        )
        output = (result.stdout + result.stderr)[:1800] or "(no output)"
        exit_label = f"exit {result.returncode}"
        await interaction.followup.send(
            f"**`{cmd[:60]}`** ({exit_label})\n```\n{output}\n```", ephemeral=True
        )
    except subprocess.TimeoutExpired:
        await interaction.followup.send(f"Command timed out after 30s.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)


# ── Background tasks ──────────────────────────────────────────────────────

async def get_announcement_channel() -> discord.TextChannel | None:
    state = load_state()
    ch_id = state.get("announcement_channel_id")
    if not ch_id:
        return None
    return bot.get_channel(int(ch_id))


@tasks.loop(minutes=1)
async def scheduled_announcements():
    """Fire Friday 11 PM IST and Saturday 11 AM IST announcements once per occurrence."""
    IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST)
    weekday = now_ist.weekday()   # 0=Mon … 4=Fri, 5=Sat
    hour, minute = now_ist.hour, now_ist.minute

    # Friday 23:00 IST or Saturday 11:00 IST (trigger within the :00 minute)
    is_trigger = (
        (weekday == 4 and hour == 23 and minute == 0) or
        (weekday == 5 and hour == 11 and minute == 0)
    )
    if not is_trigger:
        return

    log_key = f"{now_ist.date()}-{hour}"
    ann_log = load_announcement_log()
    if ann_log.get(log_key):
        return  # already sent this occurrence
    ann_log[log_key] = now_ist.isoformat()
    save_announcement_log(ann_log)

    channel = await get_announcement_channel()
    if not channel:
        return

    label = "Friday Night" if weekday == 4 else "Saturday Morning"
    usage = get_claude_usage()
    codex = get_codex_accounts()
    content = build_announcement(f"{label} Check-in", usage, codex)
    # Split if too long for one message
    for chunk in [content[i:i+1900] for i in range(0, len(content), 1900)]:
        await channel.send(chunk)


@tasks.loop(minutes=5)
async def temp_monitor():
    state = load_state()
    channel = await get_announcement_channel()
    if not channel:
        return
    stats = get_system_stats()
    warnings = detect_temp_warnings(state, stats)
    if not warnings:
        return

    cooldown_min = int(state["settings"].get("system_temp_cooldown_min", 30))
    now = datetime.now(timezone.utc)
    last_sent = state.setdefault("last_temp_warning_utc", {})
    pending = []
    for item in warnings:
        key = item["sensor"]
        previous = _parse_iso_dt(last_sent.get(key))
        if previous is not None and (now - previous).total_seconds() < cooldown_min * 60:
            continue
        pending.append(item)

    if not pending:
        return

    lines = ["**🌡️ System Temperature Warning**", ""]
    for item in pending:
        lines.append(
            f"{item['sensor']}: {item['value']:.1f}C >= {item['threshold']:.1f}C"
        )
        last_sent[item["sensor"]] = now.isoformat()
    lines.append("")
    lines.extend(render_system_stats(stats))
    save_state(state)
    await channel.send("\n".join(lines))


def _next_reset_sleep(usage: dict | None, codex_accounts: list) -> float:
    """Calculate seconds to sleep until the earliest upcoming reset across Claude + Codex."""
    now = datetime.now(timezone.utc)
    candidates: list[float] = []

    if usage:
        # 5h window: resets_at is window START if in past; window ends at start + 5hr
        resets_at_5h = datetime.fromisoformat(usage["5h_resets_at"])
        window_end = resets_at_5h + timedelta(hours=5) if resets_at_5h < now else resets_at_5h
        candidates.append((window_end - now).total_seconds())

        # 7d window resets at the given future timestamp
        resets_at_7d = datetime.fromisoformat(usage["7d_resets_at"])
        if resets_at_7d > now:
            candidates.append((resets_at_7d - now).total_seconds())

    for acc in codex_accounts:
        reset_str = acc.get("resetAtSecondary", "")
        if reset_str:
            try:
                reset_dt = datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
                if reset_dt > now:
                    candidates.append((reset_dt - now).total_seconds())
            except Exception:
                pass

    if not candidates:
        return 5 * 3600  # fallback: check every 5 hours

    soonest = min(candidates)
    return max(60, soonest + 300)  # wake 5 min after the earliest reset


async def _do_reset_check():
    """Query, compare to snapshot, save new snapshot, notify on resets."""
    usage = get_claude_usage()
    codex = get_codex_accounts()
    old_snap = load_snapshot()
    events = detect_resets(old_snap, usage, codex)

    save_snapshot({
        "claude_5h_pct": usage["5h_pct"] if usage else None,
        "claude_5h_resets_at": usage["5h_resets_at"] if usage else None,
        "claude_7d_pct": usage["7d_pct"] if usage else None,
        "claude_7d_resets_at": usage["7d_resets_at"] if usage else None,
        "codex_accounts": [
            {
                "email": a.get("email", ""),
                "usage": a.get("usage"),
                "resetAtSecondary": a.get("resetAtSecondary"),
                "remainingCreditsSecondary": a.get("remainingCreditsSecondary"),
                "capacityCreditsSecondary": a.get("capacityCreditsSecondary"),
            }
            for a in codex
        ],
        "checked_utc": datetime.now(timezone.utc).isoformat(),
    })

    if not events:
        return

    channel = await get_announcement_channel()
    if not channel:
        return

    lines = ["**🔔 Usage Reset Detected**", ""]
    lines += events
    if usage:
        lines.append(f"\nClaude now: 5h={usage['5h_pct']:.0f}%  7d={usage['7d_pct']:.0f}%")
    state = load_state()
    msg = "_Weekend mode is active — scheduler picks this up on the next tick._" \
          if state.get("active") else "_Run `/weekend-on` to start using the fresh capacity._"
    lines.append(msg)
    await channel.send("\n".join(lines))


HEARTBEAT_INTERVAL = 1800  # 30 min — catches surprise mid-window resets from Anthropic updates


async def smart_reset_monitor():
    """Sleep until just after the next expected reset, with a 30-min heartbeat cap
    so surprise early resets (e.g. Anthropic service updates) are still caught."""
    import logging
    logger = logging.getLogger("weekend_bot")
    await bot.wait_until_ready()

    # Baseline snapshot on startup — no notification, just anchors the diff
    usage = get_claude_usage()
    codex = get_codex_accounts()
    if not USAGE_SNAPSHOT.exists():
        save_snapshot({
            "claude_5h_resets_at": usage["5h_resets_at"] if usage else None,
            "claude_7d_resets_at": usage["7d_resets_at"] if usage else None,
            "codex_accounts": [],
            "checked_utc": datetime.now(timezone.utc).isoformat(),
        })

    while True:
        usage = get_claude_usage()
        codex = get_codex_accounts()
        until_reset = _next_reset_sleep(usage, codex)
        # Cap at heartbeat interval — detects surprise resets from Anthropic updates
        sleep_secs = min(until_reset, HEARTBEAT_INTERVAL)
        if sleep_secs < until_reset:
            logger.warning(f"Reset monitor: next reset in {until_reset/3600:.1f}h, heartbeat-checking in 30min")
        else:
            logger.warning(f"Reset monitor sleeping {sleep_secs/3600:.1f}h until next reset")
        await asyncio.sleep(sleep_secs)
        await _do_reset_check()


# ── Bot events ─────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    import logging
    logger = logging.getLogger("weekend_bot")
    try:
        config = load_config()
        guild_id = config.get("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            logger.warning(f"Synced {len(synced)} commands to guild {guild_id}")
        else:
            synced = await tree.sync()
            logger.warning(f"Synced {len(synced)} commands globally")
        logger.warning(f"Bot ready as {bot.user}")
        started = []
        if not scheduled_announcements.is_running():
            scheduled_announcements.start()
            started.append("scheduled_announcements")
        if not temp_monitor.is_running():
            temp_monitor.start()
            started.append("temp_monitor")
        reset_task = getattr(bot, "_weekend_reset_monitor_task", None)
        if reset_task is None or reset_task.done():
            bot._weekend_reset_monitor_task = asyncio.create_task(smart_reset_monitor())
            started.append("smart_reset_monitor")
        if started:
            logger.warning(f"Started background tasks: {', '.join(started)}")
        else:
            logger.warning("Background tasks already running")
    except Exception as e:
        logger.error(f"on_ready failed: {e}", exc_info=True)


@bot.event
async def on_error(event_method: str, *args, **kwargs):
    import logging, traceback
    logging.getLogger("weekend_bot").error(
        f"Unhandled error in {event_method}:\n{traceback.format_exc()}"
    )


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    config = load_config()
    token = config.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print(
            "ERROR: DISCORD_BOT_TOKEN is not set in ~/.weekend-mode/config.json\n"
            "\n"
            "To set up the Discord bot:\n"
            "  1. Go to https://discord.com/developers/applications\n"
            "  2. Create a New Application\n"
            "  3. Go to Bot → Reset Token → copy the token\n"
            "  4. Enable: Server Members Intent, Message Content Intent (optional)\n"
            "  5. Go to OAuth2 → URL Generator → scopes: bot + applications.commands\n"
            "     permissions: Send Messages, Use Slash Commands\n"
            "  6. Open the generated URL to invite the bot to your server\n"
            "  7. Paste the token in ~/.weekend-mode/config.json DISCORD_BOT_TOKEN\n"
            "\n"
            "NOTE: Do NOT reuse the cc-connect bot token — two simultaneous connections\n"
            "with the same token will conflict."
        )
        sys.exit(1)
    bot.run(token)


if __name__ == "__main__":
    main()
