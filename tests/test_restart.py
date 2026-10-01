"""Tests for swee/restart.py's restart_palworld(). As in test_server_update.py,
asyncio.create_subprocess_exec is mocked throughout, so this verifies only the
Python-level control flow — the real sudo grant and systemctl's actual exit
codes are unverified here and can only be checked on the host.
"""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

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

import swee.restart as restart


class FakeProc:
    """Stands in for the process object asyncio.create_subprocess_exec returns."""

    def __init__(self, returncode):
        self.returncode = returncode

    async def wait(self):
        return self.returncode


class RestartPalworldTests(unittest.TestCase):
    def test_failed_restart_aborts_before_the_poll(self):
        """Mirrors test_server_update.py's failed-stop test: a `systemctl
        restart` that fails must not be followed by the online poll, which
        would otherwise report on whatever state the server already
        happened to be in rather than on the restart that was never
        issued — the original /update bug, verbatim, in /restart."""

        async def fake_exec(*args, **kwargs):
            return FakeProc(1)

        info_mock = AsyncMock()
        with patch.object(restart.rest, "info", info_mock), \
             patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with self.assertLogs("swee", level="ERROR") as logs:
                embed = asyncio.run(restart.restart_palworld())

        info_mock.assert_not_called()
        self.assertEqual(embed.title, "Restart failed")
        self.assertIn("exit 1", embed.fields[0].value)
        self.assertTrue(
            any("restart failed" in message for message in logs.output),
            f"expected a logged restart failure, got: {logs.output}",
        )


if __name__ == "__main__":
    unittest.main()
