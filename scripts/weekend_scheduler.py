#!/usr/bin/env python3
"""Weekend autonomous mode scheduler.

Queries the Anthropic usage API, picks the right engine (claude/codex),
and runs one beads issue per invocation. Called every 5 min by systemd timer.

Usage:
  python3 weekend_scheduler.py          # normal run
  python3 weekend_scheduler.py --dry-run  # print what would happen, no side effects
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WEEKEND_DIR = Path.home() / ".weekend-mode"
STATE_FILE = WEEKEND_DIR / "state.json"
CONFIG_FILE = WEEKEND_DIR / "config.json"
HANDOFF_FILE = WEEKEND_DIR / "handoff.md"
STOP_FLAG = WEEKEND_DIR / "stop.flag"
SKIP_FLAG = WEEKEND_DIR / "skip.flag"
SESSION_LOG = WEEKEND_DIR / "session.log"
PID_FILE = WEEKEND_DIR / "session.pid"

CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
USAGE_ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
CODEX_LB_URL = "http://127.0.0.1:2455/api/accounts"
ENGINES = ("claude", "codex", "gemini")
DEFAULT_MODELS = {
    "claude": "sonnet",
    "codex": "gpt-5.4",
    "gemini": "gemini-2.5-pro",
}
DEFAULT_LANE_MODELS = {
    "orchestration": {"claude": "sonnet", "codex": "gpt-5.4", "gemini": "gemini-2.5-pro"},
    "worker": {"claude": "sonnet", "codex": "gpt-5.4", "gemini": "gemini-2.5-flash"},
    "research": {"claude": "sonnet", "codex": "gpt-5.4", "gemini": "gemini-2.5-pro"},
}
DEFAULT_ENGINE_TOGGLES = {
    "claude": True,
    "codex": True,
    "gemini": False,
}
DEFAULT_WORKER_TOGGLES = {
    "claude": True,
    "codex": True,
    "gemini": True,
}
DEFAULT_SETTINGS = {
    "claude_5h_threshold_pct": 90,
    "claude_7d_threshold_pct": 95,
    "codex_guard_pct": 50,
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

DRY_RUN = "--dry-run" in sys.argv


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(SESSION_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state() -> dict:
    return normalize_state(json.loads(STATE_FILE.read_text()))


def save_state(state: dict):
    if not DRY_RUN:
        STATE_FILE.write_text(json.dumps(state, indent=2))


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


def _deep_fill_missing(target: dict, defaults: dict) -> None:
    for key, value in defaults.items():
        if key not in target:
            target[key] = json.loads(json.dumps(value))
            continue
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_fill_missing(target[key], value)


def normalize_state(state: dict) -> dict:
    state.setdefault("settings", {})
    _deep_fill_missing(state["settings"], DEFAULT_SETTINGS)
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
    model = settings.get("lane_models", {}).get(lane, {}).get(engine)
    if model:
        return model
    return settings.get("default_models", {}).get(engine, DEFAULT_MODELS[engine])


def enabled_engines(toggle_map: dict) -> list[str]:
    return [engine for engine in ENGINES if toggle_map.get(engine)]


def get_claude_usage() -> dict | None:
    """Replicated from disc0nnctd/Claude-Code-Usage-Monitor poller.rs."""
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
        fh = data["five_hour"]
        sd = data["seven_day"]
        return {
            "5h_pct": float(fh["utilization"]),
            "5h_resets_at": fh["resets_at"],
            "7d_pct": float(sd["utilization"]),
            "7d_resets_at": sd["resets_at"],
        }
    except Exception as e:
        log(f"WARNING: failed to fetch claude usage: {e}")
        return None


def get_codex_health(config: dict) -> dict | None:
    """Query codex-lb pool health. Returns None if unavailable."""
    try:
        import requests
        key = config.get("CODEX_LB_KEY", "")
        resp = requests.get(CODEX_LB_URL, headers={
            "Authorization": f"Bearer {key}"
        }, timeout=5)
        if resp.status_code == 401:
            return None
        resp.raise_for_status()
        accounts = resp.json()
        if isinstance(accounts, dict):
            accounts = accounts.get("accounts", [])
        if not accounts:
            return None
        remaining_candidates = []
        for account in accounts:
            if account.get("status") in ("paused", "disabled", "deactivated", "quota_exceeded"):
                continue
            usage = account.get("usage") or {}
            if "secondaryRemainingPercent" in usage:
                remaining_candidates.append(float(usage["secondaryRemainingPercent"]))
            elif "remainingPercent" in account:
                remaining_candidates.append(float(account["remainingPercent"]))
        remaining = min(remaining_candidates, default=100)
        return {"remaining_pct": remaining}
    except Exception:
        return None


def gemini_available() -> bool:
    return shutil.which("gemini") is not None


def pick_engine(usage: dict | None, codex_health: dict | None, state: dict) -> str:
    """Returns 'claude', 'codex', 'gemini', or 'wait'."""
    settings = state["settings"]
    threshold_5h = settings["claude_5h_threshold_pct"]
    codex_guard = settings["codex_guard_pct"]
    allowed = enabled_engines(state.get("session_allowed_orchestrators", {}))

    if "claude" in allowed and usage is not None and usage["5h_pct"] < threshold_5h:
        return "claude"

    if "codex" in allowed and codex_health is not None and codex_health["remaining_pct"] > codex_guard:
        return "codex"

    if "gemini" in allowed and gemini_available():
        return "gemini"

    return "wait"


def get_next_approved_issue(state: dict) -> dict | None:
    """Return the next beads issue tagged [weekend-approved] for the current project."""
    queue = state["settings"]["project_queue"]
    idx = state.get("current_project_index", 0)

    for offset in range(len(queue)):
        project = queue[(idx + offset) % len(queue)]
        try:
            result = subprocess.run(
                ["bd", "list", "--status=open"],
                capture_output=True, text=True, timeout=15
            )
            for line in result.stdout.splitlines():
                if "[weekend-approved]" not in line.lower():
                    continue
                # Parse issue id from line like "○ home-3tf ● P1 ..."
                match = re.search(r'\b(home-\w+)\b', line)
                if not match:
                    continue
                issue_id = match.group(1)
                # Get full issue details
                detail = subprocess.run(
                    ["bd", "show", issue_id],
                    capture_output=True, text=True, timeout=10
                )
                title_match = re.search(r'Title:\s*(.+)', detail.stdout)
                desc_match = re.search(r'Description:\s*([\s\S]+?)(?:\nStatus:|\nType:|\Z)', detail.stdout)
                return {
                    "id": issue_id,
                    "title": title_match.group(1).strip() if title_match else line.strip(),
                    "description": desc_match.group(1).strip() if desc_match else "",
                    "project": project,
                    "project_dir": state["project_dirs"].get(project, "/home/notdc"),
                }
        except Exception as e:
            log(f"WARNING: failed to query beads for {project}: {e}")

    return None


def compute_budget_min(usage: dict | None, state: dict) -> int:
    max_budget = state["settings"]["max_task_budget_min"]
    if usage is None:
        return min(60, max_budget)
    remaining_window_pct = 100.0 - usage["5h_pct"]
    # 80% of remaining window time (300 min window)
    budget = int(remaining_window_pct / 100.0 * 300 * 0.80)
    return max(15, min(budget, max_budget))


def build_prompt(issue: dict, budget_min: int, state: dict, engine: str, model: str) -> str:
    allowed_workers = enabled_engines(state.get("session_allowed_workers", {}))
    worker_models = ", ".join(
        f"{worker}={resolve_lane_model(state, 'worker', worker)}" for worker in allowed_workers
    ) or "none"
    research_models = ", ".join(
        f"{worker}={resolve_lane_model(state, 'research', worker)}" for worker in allowed_workers
    ) or "none"
    return f"""You are running in weekend autonomous mode on the {issue['project']} project.

Task: [{issue['id']}] {issue['title']}
Description: {issue['description']}

Working directory: {issue['project_dir']}
Time budget: {budget_min} minutes. Work without asking for confirmation.
Current time: {datetime.now(timezone.utc).isoformat()}
Orchestrator engine: {engine}
Orchestrator model: {model}
Allowed worker engines: {", ".join(allowed_workers) or "none"}
Worker models: {worker_models}
Research models: {research_models}

Instructions:
1. Run: bd update {issue['id']} --claim
2. Read ~/.weekend-mode/handoff.md for context from the previous session (if it exists)
3. Implement the changes needed. Run tests if relevant.
4. Run: bd close {issue['id']} --reason="Weekend autonomous session"
5. Append a brief summary (3-5 lines) to ~/.weekend-mode/handoff.md
6. If you cannot finish within time, commit any partial work with a clear commit message, then document exactly what remains in ~/.weekend-mode/handoff.md

Before each file edit or bash command, check if ~/.weekend-mode/stop.flag exists — if it does, stop immediately and write your current state to ~/.weekend-mode/handoff.md.
"""


def is_session_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # signal 0 = check if process exists
        return True
    except (ProcessLookupError, ValueError):
        PID_FILE.unlink(missing_ok=True)
        return False


def run_task(issue: dict, engine: str, budget_min: int, state: dict):
    model = resolve_lane_model(state, "orchestration", engine)
    prompt = build_prompt(issue, budget_min, state, engine, model)
    log(
        f"Starting task {issue['id']} ({issue['title'][:60]}) via {engine}"
        f" model={model}, budget={budget_min}min"
    )

    if DRY_RUN:
        log(f"DRY RUN: would run {engine}/{model} with {budget_min}min budget on {issue['id']}")
        return

    SKIP_FLAG.unlink(missing_ok=True)

    if engine == "claude":
        cmd = [
            "timeout", str(budget_min * 60),
            "claude", "--dangerously-skip-permissions", "--model", model, "-p", prompt
        ]
        env = os.environ.copy()
    elif engine == "codex":
        cmd = [
            "timeout", str(budget_min * 60),
            "codex", "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model", model,
            prompt,
        ]
        env = {**os.environ, "CODEX_HOME": "/home/notdc/.codex-cc-connect"}
    else:
        cmd = [
            "timeout", str(budget_min * 60),
            "gemini",
            "--model", model,
            "--prompt", prompt,
            "--yolo",
            "--skip-trust",
        ]
        env = os.environ.copy()

    proc = subprocess.Popen(
        cmd, env=env, stdout=open(SESSION_LOG, "a"), stderr=subprocess.STDOUT
    )
    PID_FILE.write_text(str(proc.pid))
    proc.wait()
    PID_FILE.unlink(missing_ok=True)
    log(f"Task finished with exit code {proc.returncode}")
    return proc.returncode


def get_deadline(state: dict) -> datetime | None:
    override = state["settings"].get("deadline_override_utc")
    dl_str = override or state.get("weekly_deadline_utc")
    if not dl_str:
        return None
    return datetime.fromisoformat(dl_str)


def compute_weekly_deadline() -> datetime:
    """Next Monday 02:00 UTC (= Monday 7:30 AM IST)."""
    now = datetime.now(timezone.utc)
    days_to_monday = (7 - now.weekday()) % 7 or 7
    deadline = now.replace(hour=2, minute=0, second=0, microsecond=0)
    deadline = deadline.replace(day=deadline.day + days_to_monday)
    # Recompute properly using timedelta
    from datetime import timedelta
    candidate = now + timedelta(days=(7 - now.weekday()) % 7 or 7)
    return candidate.replace(hour=2, minute=0, second=0, microsecond=0)


def main():
    state = load_state()
    config = load_config()

    if not state.get("active"):
        log("Weekend mode is not active. Exiting.")
        return

    if STOP_FLAG.exists():
        log("Stop flag present. Clearing and deactivating.")
        STOP_FLAG.unlink()
        state["active"] = False
        save_state(state)
        return

    if is_session_running():
        log("Session already running. Exiting.")
        return

    # Check deadline
    deadline = get_deadline(state)
    if deadline is None:
        deadline = compute_weekly_deadline()
        state["weekly_deadline_utc"] = deadline.isoformat()
        save_state(state)

    now = datetime.now(timezone.utc)
    if now >= deadline:
        log(f"Past weekly deadline ({deadline.isoformat()}). Deactivating.")
        state["active"] = False
        save_state(state)
        return

    # Query usage
    usage = get_claude_usage()
    codex_health = get_codex_health(config)

    if usage:
        log(f"Claude usage: 5h={usage['5h_pct']:.0f}% (resets {usage['5h_resets_at']}), 7d={usage['7d_pct']:.0f}%")
    if codex_health:
        log(f"Codex pool: {codex_health['remaining_pct']:.0f}% remaining")
    if gemini_available():
        log("Gemini CLI available")

    # Check 7d hard stop
    if usage and usage["7d_pct"] >= state["settings"]["claude_7d_threshold_pct"]:
        log(f"Claude 7d usage {usage['7d_pct']:.0f}% >= threshold. Pausing weekend mode.")
        state["active"] = False
        save_state(state)
        return

    engine = pick_engine(usage, codex_health, state)

    if engine == "wait":
        resets_at = usage["5h_resets_at"] if usage else "unknown"
        log(f"No enabled orchestrator is currently usable. Claude resets at {resets_at}. Will retry next timer tick.")
        return

    issue = get_next_approved_issue(state)
    if issue is None:
        log("No approved issues in queue. Weekend mode complete.")
        state["active"] = False
        save_state(state)
        return

    budget = compute_budget_min(usage, state)
    hours_left = (deadline - now).total_seconds() / 3600
    log(f"Hours to deadline: {hours_left:.1f}h | Engine: {engine} | Issue: {issue['id']}")

    state["last_issue_worked"] = issue["id"]
    state["sessions_run"] = state.get("sessions_run", 0) + 1
    save_state(state)

    returncode = run_task(issue, engine, budget, state)

    # Mark issue completed in state if claude exited cleanly
    if returncode in (0, None):
        completed = state.get("tasks_completed", [])
        if issue["id"] not in completed:
            completed.append(issue["id"])
        state["tasks_completed"] = completed
    state["last_session_end_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
