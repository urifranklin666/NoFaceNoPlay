# Cam Enforcer Bot

A Discord bot that kicks users out of voice channels when they refuse to turn
their camera on. Built for the deadplug crew — no tracking, no metrics, no
bullshit. Just enforcement.

## What it does

- Watches voice channels (configured per-server or via env).
- When a non-exempt user joins a watched channel **without** their camera on,
  a grace-period countdown starts.
- If they don't turn their camera on in time, they get disconnected.
- Optionally sends a warning DM a few seconds before the kick.
- Logs every kick to a configurable text channel.

## Features

- Per-guild settings persisted to `settings.json` (survive restarts).
- Slash commands for live configuration (no editing files needed).
- Exempt roles that are never kicked (and bots are always exempt).
- Grace period and warning timing fully adjustable.
- `.env` support for zero-config startup.

## Requirements

- Python 3.10+
- `discord.py` 2.3+
- `python-dotenv` (optional, for `.env` support)

```bash
pip install -r requirements.txt
```

## Discord setup

1. Create a bot at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable **Privileged Intents**:
   - `SERVER MEMBERS INTENT`
   - `MESSAGE CONTENT INTENT` (not strictly required, but harmless)
3. Under **OAuth2 → URL Generator**, select scopes `bot` and `applications.commands`.
4. Grant bot permissions: **Move Members**, **View Channels**, **Send Messages**, **Embed Links**.
5. Invite the bot to your server.

> ⚠️ The `members` intent is privileged. If your bot is in 100+ servers it must
> be verified, otherwise Discord won't deliver member data and kicks will fail
> silently.

## Configuration

Everything is optional. The bot can start with **zero** configuration and be
set up entirely through slash commands.

### Via `.env` (recommended for startup defaults)

Copy the example and fill in what you need:

```bash
# .env
DISCORD_TOKEN=your_token_here

# Comma-separated channel IDs to watch at startup (leave empty to set via /camset watch)
WATCHED_CHANNEL_IDS=

# Timeouts in seconds (adjust freely)
GRACE_SECONDS=30
WARNING_SECONDS=10

# Comma-separated role IDs that are never kicked
EXEMPT_ROLE_IDS=

# Text channel ID for kick logs (0 or empty = disabled)
LOG_CHANNEL_ID=0
```

Leave any var **empty** and it simply won't be applied at startup — you can
configure it live instead.

### Via slash commands (live, per-server)

| Command | Description |
|---|---|
| `/camset watch <channel>` | Add a voice channel to the watch list |
| `/camset unwatch <channel>` | Remove a voice channel from the watch list |
| `/camset grace <seconds>` | Set grace period (5–300s) |
| `/camset warning <seconds>` | Set warning DM timing (0–120s before kick) |
| `/camset exempt <role>` | Add an exempt role |
| `/camset unexempt <role>` | Remove an exempt role |
| `/camset logchannel <channel>` | Set the log channel |
| `/camset show` | Show current settings for this server |

All `camset` commands require the **Administrator** permission.

## Running

```bash
python main.py
```

If `DISCORD_TOKEN` is empty, the bot prints a warning and exits — set the
token in `.env` or the environment and run again.

## How enforcement works

1. User joins a watched channel with camera off → countdown starts.
2. (Optional) `WARNING_SECONDS` before the end, a DM is sent.
3. If they turn the camera on at any point, the countdown is cancelled.
4. If they turn it **off** again, the countdown restarts.
5. At the end of the grace period, they're disconnected.

Bots and members with an exempt role are never touched.

## Files

```
cam_enforcer_bot/
├── main.py           # bot logic, events, slash commands
├── config.py         # env/.env loader with defaults
├── settings.json     # per-guild runtime settings (auto-managed)
├── requirements.txt
└── README.md
```

## License

Do whatever you want. It's for the people.
