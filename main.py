"""Cam Enforcer Bot — kicks users from voice channels if their camera is off.

Requires discord.py 2.x and the following privileged intents:
  - voice_states
  - guilds
  - members  (privileged; bot must be under 100 servers or verified)

OAuth2 scopes: bot, applications.commands
Bot permissions: Move Members, View Channels, Send Messages, Embed Links
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config

SETTINGS_PATH = Path(__file__).with_name("settings.json")

# ---------------------------------------------------------------------------
# Persistent per-guild settings
# ---------------------------------------------------------------------------

def _load_settings() -> Dict[int, Dict[str, Any]]:
    """Load settings.json and return a dict keyed by guild ID."""
    if not SETTINGS_PATH.exists():
        return {}
    raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    # keys are strings in JSON; convert to int
    return {int(k): v for k, v in raw.items()}


def _save_settings(data: Dict[int, Dict[str, Any]]) -> None:
    """Persist settings to disk."""
    serializable = {str(k): v for k, v in data.items()}
    SETTINGS_PATH.write_text(json.dumps(serializable, indent=2), encoding="utf-8")


# In-memory cache of per-guild settings
guild_settings: Dict[int, Dict[str, Any]] = {}


def get_guild_setting(guild_id: int, key: str, default: Any = None) -> Any:
    return guild_settings.get(guild_id, {}).get(key, default)


def set_guild_setting(guild_id: int, key: str, value: Any) -> None:
    if guild_id not in guild_settings:
        guild_settings[guild_id] = {}
    guild_settings[guild_id][key] = value
    _save_settings(guild_settings)


# ---------------------------------------------------------------------------
# Bot class
# ---------------------------------------------------------------------------

class CamEnforcer(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.members = True  # privileged intent
        intents.guilds = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

        # guild_id -> {member_id: asyncio.Task}
        self._pending_kicks: Dict[int, Dict[int, asyncio.Task]] = {}

    async def setup_hook(self) -> None:
        global guild_settings
        guild_settings = _load_settings()
        await self.tree.sync()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_exempt(self, member: discord.Member) -> bool:
        """Return True if the member should never be kicked."""
        if member.bot:
            return True
        exempt_ids = set(config.EXEMPT_ROLE_IDS)
        # merge per-guild exempt roles
        guild_exempt = get_guild_setting(member.guild.id, "exempt_role_ids", [])
        exempt_ids.update(guild_exempt)
        for role in member.roles:
            if role.id in exempt_ids:
                return True
        return False

    def _is_watched(self, channel: discord.VoiceChannel) -> bool:
        """Return True if this voice channel is under enforcement."""
        watched = set(config.WATCHED_CHANNEL_IDS)
        watched.update(get_guild_setting(channel.guild.id, "watched_channel_ids", []))
        return channel.id in watched

    def _grace_seconds(self, guild_id: int) -> int:
        return get_guild_setting(guild_id, "grace_seconds", config.GRACE_SECONDS)

    def _warning_seconds(self, guild_id: int) -> int:
        return get_guild_setting(guild_id, "warning_seconds", config.WARNING_SECONDS)

    async def _log(self, guild: discord.Guild, message: str) -> None:
        """Send a message to the configured log channel, if any."""
        # prefer per-guild log channel override, fallback to env
        log_id = get_guild_setting(guild.id, "log_channel_id", config.LOG_CHANNEL_ID)
        if not log_id:
            return
        channel = guild.get_channel(log_id)
        if isinstance(channel, discord.TextChannel):
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            await channel.send(f"`{ts}` {message}")

    async def _dm_warn(self, member: discord.Member, seconds_left: int) -> None:
        """Send a warning DM to the member."""
        try:
            await member.send(
                f"⚠️ **Camera Required**\n"
                f"You have **{seconds_left}s** left to enable your camera in **{member.voice.channel.name}** "
                f"or you will be disconnected."
            )
        except discord.Forbidden:
            pass  # DMs closed

    async def _kick_for_no_cam(self, member: discord.Member) -> None:
        """Disconnect a member for failing to enable their camera."""
        if not member.voice or not member.voice.channel:
            return
        channel = member.voice.channel
        try:
            await member.move_to(None, reason="Camera required — grace period expired")
        except discord.Forbidden:
            await self._log(
                member.guild,
                f"❌ Failed to kick {member.mention} from <#{channel.id}> (missing permissions).",
            )
            return
        except discord.HTTPException:
            return

        await self._log(
            member.guild,
            f"🥾 Kicked {member.mention} from <#{channel.id}> — camera off after grace period.",
        )

    def _cancel_pending(self, guild_id: int, member_id: int) -> None:
        """Cancel any pending kick task for a member."""
        task = self._pending_kicks.get(guild_id, {}).pop(member_id, None)
        if task:
            task.cancel()

    async def _schedule_kick(self, member: discord.Member) -> None:
        """Start the grace-period countdown for a member."""
        guild_id = member.guild.id
        member_id = member.id

        self._cancel_pending(guild_id, member_id)

        grace = self._grace_seconds(guild_id)
        warn = self._warning_seconds(guild_id)

        async def _countdown():
            try:
                # warning phase
                if warn > 0 and grace > warn:
                    await asyncio.sleep(grace - warn)
                    # re-check before warning
                    if member.voice and member.voice.self_video:
                        return
                    await self._dm_warn(member, warn)
                    await asyncio.sleep(warn)
                else:
                    await asyncio.sleep(grace)

                # final check
                if member.voice and member.voice.self_video:
                    return
                await self._kick_for_no_cam(member)
            except asyncio.CancelledError:
                pass
            finally:
                self._pending_kicks.get(guild_id, {}).pop(member_id, None)

        task = asyncio.create_task(_countdown())
        self._pending_kicks.setdefault(guild_id, {})[member_id] = task

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def on_ready(self):
        print(f"Bot logged in as {self.user} ({self.user.id})")
        print(f"Watching {len(config.WATCHED_CHANNEL_IDS)} channel(s) from env.")
        print("------")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        # ignore self
        if member.id == self.user.id:
            return

        # --- user joined a watched channel ---
        if after.channel and after.channel != before.channel:
            if self._is_watched(after.channel):
                if self._is_exempt(member):
                    return
                if after.self_video:
                    return  # already on cam
                await self._schedule_kick(member)
            return

        # --- user turned camera on while in a watched channel ---
        if after.channel and after.channel == before.channel:
            if not self._is_watched(after.channel):
                return
            if after.self_video and not before.self_video:
                # camera turned ON — cancel any pending kick
                self._cancel_pending(member.guild.id, member.id)
                return
            if not after.self_video and before.self_video:
                # camera turned OFF — start grace again
                if self._is_exempt(member):
                    return
                await self._schedule_kick(member)
                return

        # --- user left the voice channel ---
        if before.channel and not after.channel:
            self._cancel_pending(member.guild.id, member.id)


# ---------------------------------------------------------------------------
# Slash commands (app_commands)
# ---------------------------------------------------------------------------

bot = CamEnforcer()


camset_group = app_commands.Group(name="camset", description="Configure camera enforcement settings.")


@camset_group.command(name="grace", description="Set grace period in seconds for this server.")
@app_commands.describe(seconds="Seconds a user has to enable their camera before being kicked.")
@app_commands.checks.has_permissions(administrator=True)
async def camset_grace(interaction: discord.Interaction, seconds: app_commands.Range[int, 5, 300]):
    set_guild_setting(interaction.guild_id, "grace_seconds", seconds)
    await interaction.response.send_message(f"✅ Grace period set to **{seconds}s**.", ephemeral=True)


@camset_group.command(name="warning", description="Set warning DM timing (seconds before kick).")
@app_commands.describe(seconds="Seconds before kick to send a warning DM.")
@app_commands.checks.has_permissions(administrator=True)
async def camset_warning(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 120]):
    set_guild_setting(interaction.guild_id, "warning_seconds", seconds)
    await interaction.response.send_message(f"✅ Warning DM set to **{seconds}s** before kick.", ephemeral=True)


@camset_group.command(name="watch", description="Add a voice channel to the watch list.")
@app_commands.describe(channel="Voice channel to enforce camera in.")
@app_commands.checks.has_permissions(administrator=True)
async def camset_watch(interaction: discord.Interaction, channel: discord.VoiceChannel):
    watched = set(get_guild_setting(interaction.guild_id, "watched_channel_ids", []))
    watched.add(channel.id)
    set_guild_setting(interaction.guild_id, "watched_channel_ids", list(watched))
    await interaction.response.send_message(f"✅ Now watching <#{channel.id}>.", ephemeral=True)


@camset_group.command(name="unwatch", description="Remove a voice channel from the watch list.")
@app_commands.describe(channel="Voice channel to stop enforcing.")
@app_commands.checks.has_permissions(administrator=True)
async def camset_unwatch(interaction: discord.Interaction, channel: discord.VoiceChannel):
    watched = set(get_guild_setting(interaction.guild_id, "watched_channel_ids", []))
    watched.discard(channel.id)
    set_guild_setting(interaction.guild_id, "watched_channel_ids", list(watched))
    await interaction.response.send_message(f"✅ Stopped watching <#{channel.id}>.", ephemeral=True)


@camset_group.command(name="exempt", description="Add an exempt role.")
@app_commands.describe(role="Role whose members will never be kicked.")
@app_commands.checks.has_permissions(administrator=True)
async def camset_exempt(interaction: discord.Interaction, role: discord.Role):
    exempt = set(get_guild_setting(interaction.guild_id, "exempt_role_ids", []))
    exempt.add(role.id)
    set_guild_setting(interaction.guild_id, "exempt_role_ids", list(exempt))
    await interaction.response.send_message(f"✅ Exempted role {role.mention}.", ephemeral=True)


@camset_group.command(name="unexempt", description="Remove an exempt role.")
@app_commands.describe(role="Role to remove exemption from.")
@app_commands.checks.has_permissions(administrator=True)
async def camset_unexempt(interaction: discord.Interaction, role: discord.Role):
    exempt = set(get_guild_setting(interaction.guild_id, "exempt_role_ids", []))
    exempt.discard(role.id)
    set_guild_setting(interaction.guild_id, "exempt_role_ids", list(exempt))
    await interaction.response.send_message(f"✅ Removed exemption for role {role.mention}.", ephemeral=True)


@camset_group.command(name="logchannel", description="Set the log channel for kick events.")
@app_commands.describe(channel="Text channel to post kick logs to.")
@app_commands.checks.has_permissions(administrator=True)
async def camset_logchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    set_guild_setting(interaction.guild_id, "log_channel_id", channel.id)
    await interaction.response.send_message(f"✅ Log channel set to <#{channel.id}>.", ephemeral=True)


@camset_group.command(name="show", description="Show current settings for this server.")
@app_commands.checks.has_permissions(administrator=True)
async def camset_show(interaction: discord.Interaction):
    gid = interaction.guild_id
    grace = get_guild_setting(gid, "grace_seconds", config.GRACE_SECONDS)
    warn = get_guild_setting(gid, "warning_seconds", config.WARNING_SECONDS)
    watched = get_guild_setting(gid, "watched_channel_ids", [])
    exempt = get_guild_setting(gid, "exempt_role_ids", [])
    log_id = get_guild_setting(gid, "log_channel_id", config.LOG_CHANNEL_ID)

    lines = [
        f"**Grace:** {grace}s",
        f"**Warning:** {warn}s",
        f"**Watched channels:** {', '.join(f'<#{c}>' for c in watched) or '*(none)*'}",
        f"**Exempt roles:** {', '.join(f'<@&{r}>' for r in exempt) or '*(none)*'}",
        f"**Log channel:** <#{log_id}>" if log_id else "**Log channel:** *(not set)*",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


bot.tree.add_command(camset_group)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is empty. Set it in your .env or environment and "
            "run again — see README.md."
        )
    bot.run(config.DISCORD_TOKEN)
