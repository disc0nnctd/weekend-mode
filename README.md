# weekend-mode

Autonomous weekend coding system for Claude Code. Works through your project backlog while you sleep, self-scheduling around Claude's 5h rolling windows and weekly reset deadline.

## What it does

- Runs Claude, Codex, or Gemini on pre-approved Beads issues
- Reads the Anthropic usage API to know exactly how much 5h/7d capacity is left
- Reads the local OpenCode SQLite database to summarize OpenCode Go spend against the official 5h/7d/month limits
- Sleeps until the next reset window, wakes up and continues
- Catches surprise mid-window resets (Anthropic updates) via 30-min heartbeat
- Discord bot (CasualBear) for all controls: start/stop, queue management, tmux, config, model lanes, provider health checks
- Discord bot (CasualBear) for all controls: start/stop, queue management, tmux, config, model lanes, provider health checks, and system monitoring
- Scheduler posts directly to a Discord webhook when a task starts, finishes, or a Claude/Codex/OpenCode usage window hits the configured limit threshold

## Architecture

```
Discord /weekend-on
    ↓
weekend-discord-bot.service  (always running)
    ↓ enables
weekend-mode.timer  (every 5min) → weekend_scheduler.py
    ↓
Anthropic usage API → pick engine (claude/codex/wait)
    ↓
bd ready + approved queue visibility in Discord
    ↓
timeout N <claude|codex|gemini> ...
```

## Files

| File | Purpose |
|------|---------|
| `scripts/weekend_discord_bot.py` | Discord bot — all slash commands + UI |
| `scripts/weekend_scheduler.py` | Core scheduler loop (run by systemd timer) |
| `systemd/weekend-discord-bot.service` | Bot daemon (always-on) |
| `systemd/weekend-mode.service` | Scheduler oneshot (triggered by timer) |
| `systemd/weekend-mode.timer` | 5-min poll timer (enabled by /weekend-on) |
| `.weekend-mode/config.example.json` | Config template (copy → `~/.weekend-mode/config.json`) |
| `.weekend-mode/state.template.json` | State template |

## Setup

```bash
# 1. Install deps
python3 -m venv ~/.venv-weekend
~/.venv-weekend/bin/pip install discord.py requests

# 2. Config
mkdir -p ~/.weekend-mode
cp .weekend-mode/config.example.json ~/.weekend-mode/config.json
# Fill in DISCORD_BOT_TOKEN, DISCORD_WEBHOOK_URL, DISCORD_GUILD_ID, CODEX_LB_API_KEY

cp .weekend-mode/state.template.json ~/.weekend-mode/state.json
# Edit project_queue and project_dirs for your setup

# 3. Systemd
cp systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now weekend-discord-bot.service

# 4. Discord
# In your announcement channel: /weekend-set-channel
# Approve issues: /weekend-approve home-xxx home-yyy
# Start: /weekend-on
```

## Discord commands

| Command | Action |
|---------|--------|
| `/weekend-on` | Start weekend mode (deadline from API) |
| `/weekend-off` | Stop |
| `/weekend-status` | Usage bars, current task, skip/pause buttons |
| `/weekend-opencode-usage` | Detailed OpenCode Go usage from `opencode.db` |
| `/weekend-queue` | View approved issue queue |
| `/weekend-approve <ids>` | Whitelist issues for automation |
| `/weekend-revoke <ids>` | Remove from queue |
| `/weekend-config` | Edit all settings via modals |
| `/weekend-models` | Show default/orchestration/worker/research models |
| `/weekend-model-set <lane> <engine> <model>` | Change model lane mappings |
| `/weekend-codex-budgets` | Show default and per-account Codex budget policy |
| `/weekend-codex-budget-set <account> <hourly> <weekly> <monthly>` | Set Codex budgets for `default` or an individual account |
| `/system-status` | Show current temperatures, load, memory, disk, and uptime |
| `/system-temp-config <cpu> <nvme> <wifi> <cooldown>` | Set thermal warning thresholds and cooldown |
| `/weekend-set-channel` | Set announcement channel |
| `/tmux list\|run\|tail\|kill` | tmux session control |
| `/run <cmd>` | One-shot shell command |

## Model control

Weekend mode keeps four model lanes:

- `default` — fallback model per engine
- `orchestration` — model used for the main weekend runner
- `worker` — model preference the orchestrator should use for subagents/helpers
- `research` — model preference for research/doc lookup subtasks

Default engine models:

- Claude: `sonnet` (`fable` is used for cheap smoke checks)
- Codex: `gpt-5.4` (`gpt-5.3-codex-spark` is used for cheap smoke checks)
- Gemini: `gemini-2.5-pro` (`gemini-2.5-flash` is used for cheap smoke checks)

When you run `/weekend-on`, the bot lets you toggle which engines are enabled for:

- orchestrators
- workers

Before activation, the bot runs a provider smoke test against each selected engine using the cheaper smoke-check model and a tiny `Reply with exactly: ok` prompt. Failed providers must be disabled or fixed before Weekend Mode starts. The last provider-check results are shown in `/weekend-status`.

The intended operating mode is unattended: you set the engine defaults, model lanes, and start-time engine toggles up front, then Weekend Mode uses those controls plus its own reset/provider monitoring without requiring live operator supervision.

## System monitoring

The bot now monitors host health from the same announcement channel:

- CPU package temperature
- NVMe temperature
- WiFi temperature sensor
- load average
- memory usage
- disk usage
- uptime

Every 5 minutes, the bot checks current temperatures and posts a warning if any configured sensor is above its threshold. Repeated warnings are rate-limited per sensor by `system_temp_cooldown_min`.

## OpenCode usage monitoring

Weekend Mode now reads `~/.local/share/opencode/opencode.db` directly and summarizes OpenCode provider sessions by:

- rolling 5-hour spend against the official Go `$12` limit
- rolling 7-day spend against the official Go `$30` limit
- month-to-date spend against the official Go `$60` limit

This is local usage reconstruction, not an official OpenCode usage API. The official docs say current usage is available in the OpenCode console; the Discord bot uses the local database because that is stable and scriptable for automation.

The compact view appears in:

- `/weekend-status`
- `/weekend-opencode-usage`
- scheduled announcement posts

## Codex budgets

Codex availability is now budgeted per account, not just by one global guard:

- `hourly_pct_min` — minimum short-window remaining percent (`secondaryRemainingPercent`)
- `weekly_pct_min` — minimum long-window remaining percent (`primaryRemainingPercent`)
- `monthly_credits_min` — minimum remaining monthly credits (`remainingCreditsSecondary`)

Weekend Mode keeps:

- a default Codex budget policy
- optional per-account overrides keyed by account email/display name

The scheduler will only treat Codex as available if at least one account passes its configured budget policy.

## Usage API

Queries `https://api.anthropic.com/api/oauth/usage` with token from `~/.claude/.credentials.json`.  
Response: `five_hour.utilization` (0-100), `five_hour.resets_at` (window start), `seven_day.*` same shape.  
Deadline is set from `seven_day.resets_at` directly — no hardcoded day math.

OpenCode Go does not use a documented public usage endpoint in this repo. Usage is reconstructed from the OpenCode SQLite database instead.

## Issue approval

Issues must have `[weekend-approved]` in their Beads notes field to be picked up:
```bash
bd update home-xxx --notes="[weekend-approved] safe to automate"
```
Or use `/weekend-approve home-xxx` from Discord.

`/weekend-status` also shows ready Beads tasks from the `/home/notdc` workspace so you can see claimable work without leaving Discord.
