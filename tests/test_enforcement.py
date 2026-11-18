import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import main


class EnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = main.CamEnforcer()
        self.bot._connection.user = SimpleNamespace(id=999)
        self.guild = SimpleNamespace(id=1)
        self.watched = SimpleNamespace(id=10, guild=self.guild, name="Camera", members=[])
        self.other = SimpleNamespace(id=20, guild=self.guild, name="Other", members=[])
        self.guild.voice_channels = [self.watched, self.other]
        self.member = SimpleNamespace(
            id=42,
            guild=self.guild,
            bot=False,
            roles=[],
            mention="<@42>",
            voice=SimpleNamespace(channel=self.watched, self_video=False),
            move_to=AsyncMock(),
            send=AsyncMock(),
        )
        self.watched.members = [self.member]
        self.guild.get_member = Mock(return_value=self.member)
        self.bot._log = AsyncMock()
        self.interaction = SimpleNamespace(
            guild_id=1,
            guild=None,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        self.tasks = []
        self.settings = {
            1: {"watched_channel_ids": [10], "grace_seconds": 3600, "warning_seconds": 0}
        }
        settings_patch = patch.object(main, "guild_settings", self.settings)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        config_patch = patch.multiple(
            main.config, WATCHED_CHANNEL_IDS=[], EXEMPT_ROLE_IDS=[]
        )
        config_patch.start()
        self.addCleanup(config_patch.stop)
        save_patch = patch.object(main, "_save_settings")
        self.save_settings = save_patch.start()
        self.addCleanup(save_patch.stop)

    async def asyncTearDown(self):
        for pending in self.bot._pending_kicks.values():
            self.tasks.extend(pending.values())
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.bot.close()

    async def schedule(self):
        await self.bot._schedule_kick(self.member)
        task = self.bot._pending_kicks[1][42]
        self.tasks.append(task)
        return task

    async def finish(self, task):
        with patch.object(main.asyncio, "sleep", new_callable=AsyncMock):
            await asyncio.wait_for(task, timeout=1)

    async def voice_event(self, channel, video):
        before = self.member.voice
        after = SimpleNamespace(channel=channel, self_video=video)
        self.member.voice = after
        await self.bot.on_voice_state_update(self.member, before, after)
        self.tasks.extend(self.bot._pending_kicks.get(1, {}).values())

    async def ready(self):
        with patch.object(
            main.CamEnforcer, "guilds", new_callable=PropertyMock,
            return_value=[self.guild],
        ), patch("builtins.print"):
            await self.bot.on_ready()
        self.tasks.extend(self.bot._pending_kicks.get(1, {}).values())

    async def test_camera_off_disconnects_and_cleans_up(self):
        task = await self.schedule()
        await self.finish(task)
        self.member.move_to.assert_awaited_once_with(
            None, reason="Camera required — grace period expired"
        )
        self.assertNotIn(42, self.bot._pending_kicks[1])

    async def test_join_starts_countdown(self):
        self.member.voice = SimpleNamespace(channel=None, self_video=False)
        await self.voice_event(self.watched, False)
        self.assertIn(42, self.bot._pending_kicks[1])

    async def test_camera_on_cancels_disconnect(self):
        task = await self.schedule()
        await asyncio.sleep(0)
        await self.voice_event(self.watched, True)
        await task
        self.member.move_to.assert_not_awaited()
        self.assertNotIn(42, self.bot._pending_kicks[1])

    async def test_camera_off_restarts_countdown(self):
        self.member.voice.self_video = True
        await self.voice_event(self.watched, False)
        self.assertIn(42, self.bot._pending_kicks[1])

    async def test_move_to_unwatched_channel_cancels_disconnect(self):
        task = await self.schedule()
        await asyncio.sleep(0)
        await self.voice_event(self.other, False)
        await task
        self.member.move_to.assert_not_awaited()
        self.assertNotIn(42, self.bot._pending_kicks[1])

    async def test_leaving_voice_cancels_disconnect(self):
        task = await self.schedule()
        await asyncio.sleep(0)
        await self.voice_event(None, False)
        await task
        self.member.move_to.assert_not_awaited()

    async def test_move_between_watched_channels_replaces_timer(self):
        self.settings[1]["watched_channel_ids"].append(20)
        old = await self.schedule()
        await asyncio.sleep(0)
        await self.voice_event(self.other, False)
        replacement = self.bot._pending_kicks[1][42]
        await old
        self.assertIsNot(old, replacement)
        self.assertIs(self.bot._pending_kicks[1][42], replacement)

    async def test_unrelated_voice_update_does_not_reset_timer(self):
        task = await self.schedule()
        await self.voice_event(self.watched, False)
        self.assertIs(self.bot._pending_kicks[1][42], task)

    async def test_replacement_timer_remains_tracked_and_cancellable(self):
        old = await self.schedule()
        await asyncio.sleep(0)
        replacement = await self.schedule()
        await old
        self.assertIs(self.bot._pending_kicks[1].get(42), replacement)
        self.bot._cancel_pending(1, 42)
        await asyncio.gather(replacement, return_exceptions=True)
        self.member.move_to.assert_not_awaited()

    async def test_current_eligibility_checked_without_voice_event(self):
        cases = ("camera_on", "unwatched", "disconnected", "exempt", "bot")
        for case in cases:
            with self.subTest(case=case):
                self.member.voice = SimpleNamespace(channel=self.watched, self_video=False)
                self.member.roles = []
                self.member.bot = False
                self.settings[1]["exempt_role_ids"] = [77]
                task = await self.schedule()
                if case == "camera_on":
                    self.member.voice.self_video = True
                elif case == "unwatched":
                    self.member.voice.channel = self.other
                elif case == "disconnected":
                    self.member.voice = None
                elif case == "exempt":
                    self.member.roles = [SimpleNamespace(id=77)]
                else:
                    self.member.bot = True
                await self.finish(task)
                self.member.move_to.assert_not_awaited()

    async def test_countdown_refreshes_cached_member(self):
        for phase in ("warning", "final_with_warning", "final_without_warning"):
            for state in ("exempt", "missing", "eligible"):
                with self.subTest(phase=phase, state=state):
                    self.settings[1].update(
                        grace_seconds=30,
                        warning_seconds=0 if phase == "final_without_warning" else 10,
                        exempt_role_ids=[77],
                    )
                    refreshed = SimpleNamespace(
                        **{
                            **vars(self.member),
                            "roles": [SimpleNamespace(id=77)] if state == "exempt" else [],
                            "send": AsyncMock(),
                            "move_to": AsyncMock(),
                        }
                    )
                    self.member.send.reset_mock()
                    self.member.move_to.reset_mock()
                    self.guild.get_member.reset_mock()
                    self.guild.get_member.return_value = self.member
                    refresh_after = {
                        "warning": 20, "final_with_warning": 10, "final_without_warning": 30,
                    }[phase]

                    async def sleep(seconds):
                        if seconds == refresh_after:
                            self.guild.get_member.return_value = (
                                None if state == "missing" else refreshed
                            )

                    task = await self.schedule()
                    with patch.object(main.asyncio, "sleep", side_effect=sleep):
                        await asyncio.wait_for(task, timeout=1)

                    self.guild.get_member.assert_called_with(self.member.id)
                    self.member.move_to.assert_not_awaited()
                    if state == "eligible":
                        refreshed.move_to.assert_awaited_once_with(
                            None, reason="Camera required — grace period expired"
                        )
                    else:
                        refreshed.move_to.assert_not_awaited()
                    if phase == "warning" and state == "eligible":
                        refreshed.send.assert_awaited_once()
                    else:
                        refreshed.send.assert_not_awaited()
                    self.assertEqual(
                        self.member.send.await_count, int(phase == "final_with_warning")
                    )
                    self.assertNotIn(42, self.bot._pending_kicks[1])

    async def test_unwatch_prevents_pending_disconnect(self):
        task = await self.schedule()
        await main.camset_unwatch.callback(self.interaction, self.watched)
        await self.finish(task)
        self.member.move_to.assert_not_awaited()
        self.save_settings.assert_called_once_with(self.settings)

    async def test_exempt_command_prevents_pending_disconnect(self):
        self.member.roles = [SimpleNamespace(id=77)]
        task = await self.schedule()
        await main.camset_exempt.callback(
            self.interaction, SimpleNamespace(id=77, mention="<@&77>")
        )
        await self.finish(task)
        self.member.move_to.assert_not_awaited()

    async def test_settings_reconciliation_starts_fresh_countdown(self):
        self.interaction.guild = self.guild
        role = SimpleNamespace(id=77, mention="<@&77>")
        self.member.roles = [role]
        for disable, enable, argument in (
            (main.camset_unwatch, main.camset_watch, self.watched),
            (main.camset_exempt, main.camset_unexempt, role),
        ):
            with self.subTest(command=disable.name), patch.object(main, "bot", self.bot):
                self.settings[1].update(watched_channel_ids=[10], exempt_role_ids=[])
                self.member.move_to.reset_mock()
                old = await self.schedule()
                await asyncio.sleep(0)

                await disable.callback(self.interaction, argument)
                self.assertNotIn(42, self.bot._pending_kicks[1])
                self.assertTrue(old.cancelling())
                self.assertFalse(self.bot._should_enforce(self.member))
                await old

                await enable.callback(self.interaction, argument)
                replacement = self.bot._pending_kicks[1][42]
                self.tasks.append(replacement)
                self.assertIsNot(old, replacement)
                self.assertIs(self.bot._pending_kicks[1][42], replacement)
                self.member.move_to.assert_not_awaited()

                with patch.object(main.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                    await asyncio.wait_for(replacement, timeout=1)
                sleep.assert_awaited_once_with(self.settings[1]["grace_seconds"])
                self.member.move_to.assert_awaited_once()

    async def test_existing_member_enforced_on_ready(self):
        await self.ready()
        self.assertIn(42, self.bot._pending_kicks[1])

    async def test_repeated_ready_preserves_existing_timer(self):
        task = await self.schedule()
        await self.ready()
        await self.ready()
        self.assertIs(self.bot._pending_kicks[1][42], task)

    async def test_ready_ignores_ineligible_members(self):
        for case in ("camera_on", "exempt", "bot", "unwatched"):
            with self.subTest(case=case):
                self.member.voice.self_video = case == "camera_on"
                self.member.bot = case == "bot"
                self.member.roles = [SimpleNamespace(id=77)] if case == "exempt" else []
                self.settings[1]["exempt_role_ids"] = [77]
                self.settings[1]["watched_channel_ids"] = [] if case == "unwatched" else [10]
                await self.ready()
                self.assertNotIn(42, self.bot._pending_kicks.get(1, {}))

    async def test_warning_failures_do_not_disable_enforcement(self):
        self.settings[1].update(grace_seconds=30, warning_seconds=10)
        for error_type, status in ((main.discord.Forbidden, 403), (main.discord.HTTPException, 500)):
            with self.subTest(status=status):
                self.member.send.side_effect = error_type(
                    SimpleNamespace(status=status, reason="Delivery failed"), "Unavailable"
                )
                self.member.move_to.reset_mock()
                task = await self.schedule()
                await self.finish(task)
                self.member.move_to.assert_awaited_once()

    async def test_no_warning_sent_when_member_no_longer_eligible(self):
        self.settings[1].update(grace_seconds=30, warning_seconds=10)
        task = await self.schedule()
        self.member.voice = None
        await self.finish(task)
        self.member.send.assert_not_awaited()
        self.member.move_to.assert_not_awaited()

    async def test_successful_warning_preserves_countdown_phases(self):
        self.settings[1].update(grace_seconds=30, warning_seconds=10)
        task = await self.schedule()
        with patch.object(main.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            await asyncio.wait_for(task, timeout=1)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [20, 10])
        self.member.send.assert_awaited_once()
        self.member.move_to.assert_awaited_once()

    async def test_env_unwatch_preserves_other_defaults(self):
        self.settings[1].pop("watched_channel_ids")
        main.config.WATCHED_CHANNEL_IDS = [10, 20]
        await main.camset_unwatch.callback(self.interaction, self.watched)
        self.assertFalse(self.bot._is_watched(self.watched))
        self.assertTrue(self.bot._is_watched(self.other))
        self.assertEqual(main.config.WATCHED_CHANNEL_IDS, [10, 20])

    async def test_env_unexempt_preserves_other_defaults(self):
        main.config.EXEMPT_ROLE_IDS = [77, 88]
        await main.camset_unexempt.callback(
            self.interaction, SimpleNamespace(id=77, mention="<@&77>")
        )
        self.member.roles = [SimpleNamespace(id=77)]
        self.assertFalse(self.bot._is_exempt(self.member))
        self.member.roles = [SimpleNamespace(id=88)]
        self.assertTrue(self.bot._is_exempt(self.member))
        self.assertEqual(main.config.EXEMPT_ROLE_IDS, [77, 88])

    async def test_watch_preserves_env_defaults(self):
        self.settings[1].pop("watched_channel_ids")
        main.config.WATCHED_CHANNEL_IDS = [10]
        await main.camset_watch.callback(self.interaction, self.other)
        self.assertEqual(set(self.settings[1]["watched_channel_ids"]), {10, 20})

    async def test_exempt_preserves_env_defaults(self):
        main.config.EXEMPT_ROLE_IDS = [77]
        await main.camset_exempt.callback(
            self.interaction, SimpleNamespace(id=88, mention="<@&88>")
        )
        self.assertEqual(set(self.settings[1]["exempt_role_ids"]), {77, 88})

    async def test_show_includes_env_defaults(self):
        self.settings[1].pop("watched_channel_ids")
        main.config.WATCHED_CHANNEL_IDS = [10]
        main.config.EXEMPT_ROLE_IDS = [77]
        await main.camset_show.callback(self.interaction)
        message = self.interaction.response.send_message.await_args.args[0]
        self.assertIn("<#10>", message)
        self.assertIn("<@&77>", message)

    async def test_saved_empty_lists_override_env_defaults(self):
        main.config.WATCHED_CHANNEL_IDS = [10]
        main.config.EXEMPT_ROLE_IDS = [77]
        self.settings[1].update(watched_channel_ids=[], exempt_role_ids=[])
        self.member.roles = [SimpleNamespace(id=77)]
        self.assertFalse(self.bot._is_watched(self.watched))
        self.assertFalse(self.bot._is_exempt(self.member))

    async def test_server_override_does_not_change_another_server(self):
        main.config.WATCHED_CHANNEL_IDS = [10, 20]
        main.config.EXEMPT_ROLE_IDS = [77]
        self.settings[1].update(watched_channel_ids=[], exempt_role_ids=[])
        other_guild = SimpleNamespace(id=2)
        other_channel = SimpleNamespace(id=20, guild=other_guild)
        other_member = SimpleNamespace(
            guild=other_guild, bot=False, roles=[SimpleNamespace(id=77)]
        )
        self.assertTrue(self.bot._is_watched(other_channel))
        self.assertTrue(self.bot._is_exempt(other_member))


class SettingsTests(unittest.TestCase):
    def test_empty_overrides_survive_reload(self):
        with TemporaryDirectory() as directory, patch.object(
            main, "SETTINGS_PATH", Path(directory) / "settings.json"
        ), patch.object(main, "guild_settings", {}):
            main.set_guild_setting(1, "watched_channel_ids", [])
            main.set_guild_setting(1, "exempt_role_ids", [])
            self.assertEqual(
                main._load_settings(),
                {1: {"watched_channel_ids": [], "exempt_role_ids": []}},
            )


if __name__ == "__main__":
    unittest.main()
