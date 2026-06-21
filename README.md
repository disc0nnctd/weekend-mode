# weekend-mode

Autonomous weekend coding system for Claude Code. Works through your project backlog while you sleep, self-scheduling around Claude's 5h rolling windows and weekly reset deadline.

## What it does

- Runs Claude, Codex, or Gemini on pre-approved Beads issues
- Reads the Anthropic usage API to know exactly how much 5h/7d capacity is left
- Sleeps until the next reset window, wakes up and continues
- Catches surprise mid-window resets (Anthropic updates) via 30-min heartbeat
- Discord bot (CasualBear) for all controls: start/stop, queue management, tmux, config, model lanes, provider health checks

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
bd list --status=open | grep [weekend-approved]
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
# Fill in DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, CODEX_LB_API_KEY

cp .weekend-mode/state.template.json ~/.weekend-mode/state.json
# Edit project_queue and project_dirs for your setup

# 3. Scripts
cp scripts/weekend_discord_bot.py ~/scripts/
cp scripts/weekend_scheduler.py ~/scripts/

# 4. Systemd
cp systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now weekend-discord-bot.service

# 5. Discord
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
| `/weekend-queue` | View approved issue queue |
| `/weekend-approve <ids>` | Whitelist issues for automation |
| `/weekend-revoke <ids>` | Remove from queue |
| `/weekend-config` | Edit all settings via modals |
| `/weekend-models` | Show default/orchestration/worker/research models |
| `/weekend-model-set <lane> <engine> <model>` | Change model lane mappings |
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

## Usage API

Queries `https://api.anthropic.com/api/oauth/usage` with token from `~/.claude/.credentials.json`.  
Response: `five_hour.utilization` (0-100), `five_hour.resets_at` (window start), `seven_day.*` same shape.  
Deadline is set from `seven_day.resets_at` directly — no hardcoded day math.

## Issue approval

Issues must have `[weekend-approved]` in their Beads notes field to be picked up:
```bash
bd update home-xxx --notes="[weekend-approved] safe to automate"
```
Or use `/weekend-approve home-xxx` from Discord.
