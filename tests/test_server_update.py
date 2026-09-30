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

import swee.server_update as server_update


class FakeProc:
    """Stands in for the process object asyncio.create_subprocess_exec returns."""

    def __init__(self, returncode, output=b""):
        self.returncode = returncode
        self._output = output

    async def wait(self):
        return self.returncode

    async def communicate(self):
        return self._output, None


class UpdatePalworldTests(unittest.TestCase):
    def setUp(self):
        # Every path through update_palworld() broadcasts a warning and
        # saves the world before it ever touches the service — neither is
        # under test here, so both are stubbed out.
        self._patch(server_update, "warn_and_wait", AsyncMock())
        self._patch(server_update.rest, "save", AsyncMock())
        self._patch(server_update.rest, "info", AsyncMock())

    def _patch(self, target, attr, value):
        patcher = patch.object(target, attr, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_failed_stop_aborts_before_the_wrapper_runs(self):
        """This is the whole point of the task: a stop that fails must not
        be followed by the update wrapper running its `steamcmd +app_update
        validate` against a server that never went down."""
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            if "stop" in args:
                return FakeProc(1)
            return FakeProc(0, b"ok")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            embed = asyncio.run(server_update.update_palworld())

        wrapper_calls = [c for c in calls if server_update.SWEE_UPDATE_WRAPPER in c]
        self.assertEqual(wrapper_calls, [], "the update wrapper ran after a failed stop")
        self.assertEqual(embed.title, "Update failed")
        self.assertIn("left running", embed.fields[0].value)

    def test_successful_flow_invokes_the_wrapper_not_steamcmd_directly(self):
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            return FakeProc(0, b"Success! App '2394010' fully installed.")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            embed = asyncio.run(server_update.update_palworld())

        flat = [" ".join(c) for c in calls]
        self.assertTrue(
            any(server_update.SWEE_UPDATE_WRAPPER in c for c in flat),
            "the update wrapper was never invoked",
        )
        self.assertFalse(
            any("steamcmd" in c for c in flat),
            "steamcmd was invoked directly instead of through the wrapper",
        )
        self.assertEqual(embed.title, "Server updated")

    def test_failed_start_is_logged_but_does_not_abort(self):
        async def fake_exec(*args, **kwargs):
            if "start" in args:
                return FakeProc(1)
            return FakeProc(0, b"Success! App '2394010' fully installed.")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with self.assertLogs("swee", level="ERROR") as logs:
                embed = asyncio.run(server_update.update_palworld())

        self.assertTrue(
            any("start failed" in message for message in logs.output),
            f"expected a logged start failure, got: {logs.output}",
        )
        # Not fatal: the flow still completes and reports on the update
        # itself rather than aborting because start's return code was bad.
        self.assertEqual(embed.title, "Server updated")


if __name__ == "__main__":
    unittest.main()
