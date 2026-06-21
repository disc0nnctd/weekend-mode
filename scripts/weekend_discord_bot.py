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
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import tasks

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

# ── helpers ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text())


def usage_bar(pct: float, width: int = 10) -> str:
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


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


def detect_resets(old: dict, new_usage: dict | None, new_codex: list) -> list[str]:
    """Compare snapshots and return list of reset event descriptions."""
    events = []
    if new_usage:
        # Claude 5h reset: resets_at timestamp changed → new window
        if old.get("claude_5h_resets_at") and old["claude_5h_resets_at"] != new_usage["5h_resets_at"]:
            events.append(f"🔄 **Claude 5h window reset** — now at {new_usage['5h_pct']:.0f}% used")
        # Claude 7d reset: resets_at changed
        if old.get("claude_7d_resets_at") and old["claude_7d_resets_at"] != new_usage["7d_resets_at"]:
            events.append(f"🗓️ **Claude weekly (7d) reset** — now at {new_usage['7d_pct']:.0f}% used")

    # Codex resets
    old_codex = {a["email"]: a for a in old.get("codex_accounts", []) if a.get("email")}
    for acc in new_codex:
        email = acc.get("email", "")
        usage = acc.get("usage") or {}
        old_acc = old_codex.get(email)
        if not old_acc:
            continue
        old_usage = old_acc.get("usage") or {}
        # Secondary window reset: resetAtSecondary changed
        new_reset = acc.get("resetAtSecondary", "")
        old_reset = old_acc.get("resetAtSecondary", "")
        if old_reset and new_reset and old_reset != new_reset:
            sec_rem = usage.get("secondaryRemainingPercent", 0)
            events.append(f"⏱️ **Codex window reset** `{email}` — {sec_rem:.0f}% remaining")
        # Monthly credits reset: remainingCredits jumped up by >20%
        new_cred = acc.get("remainingCreditsSecondary")
        old_cred = old_acc.get("remainingCreditsSecondary")
        if new_cred and old_cred and new_cred > old_cred * 1.2:
            cap = acc.get("capacityCreditsSecondary", 0)
            events.append(f"📅 **Codex monthly credits reset** `{email}` — {new_cred:.0f}/{cap:.0f} credits")
    return events


# ── Work suggestions ───────────────────────────────────────────────────────

def suggest_weekend_work() -> str:
    """Query beads for top open issues and format as a weekend suggestion."""
    try:
        result = subprocess.run(["bd", "list", "--status=open"],
                                capture_output=True, text=True, timeout=15)
        state = load_state()
        queue = state["settings"].get("project_queue", [])
        project_dirs = state.get("project_dirs", {})

        # Build project → issues mapping; prefer P1 then P2
        project_issues: dict[str, list[str]] = {p: [] for p in queue}
        other: list[str] = []

        for line in result.stdout.splitlines():
            if not line.strip() or "[weekend-approved]" in line.lower():
                continue
            match = re.search(r'\b(home-\w+)\b', line)
            if not match:
                continue
            issue_id = match.group(1)
            title = re.sub(r'.*P\d\s+', '', line).strip()[:70]
            placed = False
            for p, d in project_dirs.items():
                # Heuristic: issue belongs to project if its title/notes hint at it
                # For now, just bucket by priority order (first 3 per project)
                if p in queue and len(project_issues[p]) < 3:
                    project_issues[p].append(f"`{issue_id}` {title}")
                    placed = True
                    break
            if not placed and len(other) < 3:
                other.append(f"`{issue_id}` {title}")

        lines = ["**📋 Suggested issues for this weekend:**"]
        all_ids = []
        for project in queue:
            issues = project_issues.get(project, [])
            if issues:
                lines.append(f"\n**{project}**")
                for issue in issues:
                    lines.append(f"  • {issue}")
                    all_ids.append(issue.split()[0].strip('`'))
        if other:
            lines.append("\n**other**")
            for issue in other:
                lines.append(f"  • {issue}")
                all_ids.append(issue.split()[0].strip('`'))

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

class WeekendOnView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="✅ Confirm Start", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = load_state()
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
                    f"Run `/weekend-approve home-xxx ...` to queue issues for automation.",
            view=None
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


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


# ── Modals ─────────────────────────────────────────────────────────────────

class EditSettingModal(discord.ui.Modal, title="Edit Setting"):
    def __init__(self, setting_key: str, current_value: str, label: str):
        super().__init__()
        self.setting_key = setting_key
        self.value_input = discord.ui.TextInput(
            label=label,
            default=str(current_value),
            required=True,
            max_length=100,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        state = load_state()
        new_val = self.value_input.value.strip()
        # Type coerce
        old = state["settings"].get(self.setting_key)
        try:
            if isinstance(old, int):
                new_val = int(new_val)
            elif isinstance(old, float):
                new_val = float(new_val)
            elif isinstance(old, list):
                new_val = [x.strip() for x in new_val.split(",")]
        except ValueError:
            pass
        state["settings"][self.setting_key] = new_val
        save_state(state)
        await interaction.response.send_message(
            f"Updated `{self.setting_key}` → `{new_val}`", ephemeral=True
        )


class ConfigView(discord.ui.View):
    SETTINGS = [
        ("claude_5h_threshold_pct", "Claude 5h stop threshold (%)"),
        ("claude_7d_threshold_pct", "Claude 7d stop threshold (%)"),
        ("codex_guard_pct", "Codex guard % (min remaining)"),
        ("max_task_budget_min", "Max task budget (minutes)"),
        ("deadline_override_utc", "Deadline override (ISO UTC or blank)"),
        ("project_queue", "Project queue (comma-separated)"),
    ]

    def __init__(self, state: dict):
        super().__init__(timeout=300)
        for key, label in self.SETTINGS:
            btn = discord.ui.Button(label=f"Edit: {label[:35]}", style=discord.ButtonStyle.secondary)
            btn.callback = self._make_callback(key, label, state)
            self.add_item(btn)

    def _make_callback(self, key: str, label: str, state: dict):
        async def callback(interaction: discord.Interaction):
            current = state["settings"].get(key, "")
            if isinstance(current, list):
                current = ", ".join(current)
            modal = EditSettingModal(key, current, label[:45])
            await interaction.response.send_modal(modal)
        return callback


# ── Slash Commands ─────────────────────────────────────────────────────────

@tree.command(name="weekend-on", description="Start weekend autonomous mode")
async def weekend_on(interaction: discord.Interaction):
    state = load_state()
    settings = state["settings"]
    queue = settings["project_queue"]
    usage = get_claude_usage()
    usage_line = ""
    if usage:
        usage_line = (f"\nCurrent usage: 5h={usage['5h_pct']:.0f}% "
                      f"({usage_bar(usage['5h_pct'])})  "
                      f"7d={usage['7d_pct']:.0f}% ({usage_bar(usage['7d_pct'])})")

    if state.get("active"):
        await interaction.response.send_message(
            f"Weekend mode is already active.{usage_line}", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Start weekend autonomous mode?\n"
        f"Projects: {' → '.join(queue)}\n"
        f"Deadline: Mon 07:30 IST  |  Stop threshold: 5h≥{settings['claude_5h_threshold_pct']}%"
        f"{usage_line}",
        view=WeekendOnView(),
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
    ]

    approved = get_approved_issues()
    lines.append(f"Approved queue: {len(approved)} issues")

    await interaction.followup.send("\n".join(lines), view=WeekendStatusView(), ephemeral=True)


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
    lines = [
        "**Weekend Mode Settings** (click to edit)",
        f"Claude 5h threshold: **{s['claude_5h_threshold_pct']}%**",
        f"Claude 7d threshold: **{s['claude_7d_threshold_pct']}%**",
        f"Codex guard: **{s['codex_guard_pct']}%** minimum remaining",
        f"Max task budget: **{s['max_task_budget_min']} min**",
        f"Deadline override: **{s.get('deadline_override_utc') or 'auto (Mon 07:30 IST)'}**",
        f"Project queue: **{queue_str}**",
    ]
    await interaction.response.send_message(
        "\n".join(lines), view=ConfigView(state), ephemeral=True
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
        scheduled_announcements.start()
        asyncio.create_task(smart_reset_monitor())
        logger.warning("Started background tasks: scheduled_announcements, smart_reset_monitor")
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
