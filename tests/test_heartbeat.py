"""Tests for swee/heartbeat.py and where main.py starts it.

The image's HEALTHCHECK reads the heartbeat file's age, so the file must mean
"this process logged in to Discord and its event loop is still turning": it is
written only after on_ready, then every HEARTBEAT_INTERVAL_S.
"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_BOT_TOKEN", "x")
os.environ.setdefault("GUILD_ID", "1")
os.environ.setdefault("ADMIN_ROLE_ID", "1")
os.environ.setdefault("RELAY_CHANNEL_ID", "1")
os.environ.setdefault("STATS_CHANNEL_ID", "1")
os.environ.setdefault("ACTIVITY_CHANNEL_ID", "1")
os.environ.setdefault("ALERTS_CHANNEL_ID", "1")
os.environ.setdefault("ADMIN_CHANNEL_ID", "1")
os.environ.setdefault("COMMANDS_CHANNEL_ID", "1")
os.environ.setdefault("BOT_UPDATES_CHANNEL_ID", "1")
os.environ.setdefault("REST_HOST", "x")
os.environ.setdefault("REST_PORT", "1")
os.environ.setdefault("REST_USER", "x")
os.environ.setdefault("REST_PASSWORD", "x")
os.environ.setdefault("PALWORLD_SETTINGS_INI_PATH", "/tmp/x")

import main
import swee.config as config
import swee.heartbeat as heartbeat


class HeartbeatSettingTests(unittest.TestCase):
    def test_default_path(self):
        self.assertEqual(config.HEARTBEAT_FILE, "/state/heartbeat")


class HeartbeatLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_writes_immediately_then_every_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "heartbeat")
            writes = []
            real_write = heartbeat.write_heartbeat

            def counting_write(p):
                writes.append(p)
                return real_write(p)

            with patch.object(heartbeat, "write_heartbeat", counting_write):
                task = asyncio.create_task(heartbeat.heartbeat_loop(path, interval_s=0.01))
                await asyncio.sleep(0.055)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self.assertTrue(os.path.exists(path))
            self.assertGreaterEqual(len(writes), 3)

    async def test_unwritable_path_is_logged_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "missing-dir", "heartbeat")
            with self.assertLogs("swee", level="WARNING"):
                task = asyncio.create_task(heartbeat.heartbeat_loop(path, interval_s=0.01))
                await asyncio.sleep(0.035)
            self.assertFalse(task.done())  # still running despite failing writes
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class HeartbeatAfterLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_written_only_after_login(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "heartbeat")
            fake_bot = MagicMock()
            fake_bot.tree.sync = AsyncMock()
            start = AsyncMock()
            with patch.object(main, "HEARTBEAT_FILE", path), \
                 patch.object(main, "_startup_done", False), \
                 patch.object(main, "_heartbeat_task", None), \
                 patch.object(main, "log_tailer", AsyncMock()), \
                 patch.object(main, "stats_ticker"), \
                 patch.object(main, "release_ticker"), \
                 patch.object(main, "palfeed_ticker"), \
                 patch.object(main.host_ops, "ping", AsyncMock(return_value=True)), \
                 patch.object(main, "load_player_history"), \
                 patch.object(main, "load_session_state"), \
                 patch.object(main, "load_last_release"), \
                 patch.object(main, "load_last_event_id"), \
                 patch.object(main, "load_last_palworld_settings"), \
                 patch.object(main.discord.utils, "setup_logging"), \
                 patch.object(main.bot, "start", start), \
                 patch.object(main.rest.client, "aclose", AsyncMock()):
                # Everything main() does before and around bot.start(), with
                # the login itself stubbed out, writes no heartbeat.
                await main.main()
                await asyncio.sleep(0.01)
                start.assert_awaited_once()
                self.assertFalse(os.path.exists(path))

                with patch.object(main, "bot", fake_bot):
                    await main.on_ready()
                    for _ in range(50):
                        if os.path.exists(path):
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(os.path.exists(path))
                    task = main._heartbeat_task
                    self.assertIsNotNone(task)

                    # on_ready fires again after a reconnect: still one loop.
                    await main.on_ready()
                    self.assertIs(main._heartbeat_task, task)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


if __name__ == "__main__":
    unittest.main()
