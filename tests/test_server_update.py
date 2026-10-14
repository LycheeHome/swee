"""Tests for swee/server_update.py: /update goes through lychee-admin's
host-operations socket (swee/host_ops.py), never sudo. The host_ops singleton
is patched throughout, so this verifies swee's control flow; the socket itself
is covered by tests/test_host_ops.py, and lychee-ops' update script (stop,
steamcmd, start) can only be checked on the host.
"""
import asyncio
import os
import unittest
from datetime import datetime, timezone
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

import swee.restart as restart
import swee.server_update as server_update
from swee.host_ops import ActionState, HostOpsError

BUSY = "an action is already running on the server"
UNAVAILABLE = "host control is unavailable right now"
NOT_ALLOWED = "swee is not allowed to do that"
NOW = datetime(2026, 10, 14, 18, 0, 0, tzinfo=timezone.utc)
PROGRESS_LINE = "Update state (0x61) downloading, progress: 64.31"


def state(name, log_tail=""):
    return ActionState(state=name, started_at=NOW, finished_at=None, log_tail=log_tail)


class FakeClock:
    """Stands in for server_update._now / _sleep: sleeping advances time."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.t += seconds


def fake_host_ops(start=None, wait=None, preflight=None):
    ops = MagicMock()
    ops.preflight = AsyncMock(side_effect=preflight)
    if isinstance(start, Exception):
        ops.start = AsyncMock(side_effect=start)
    else:
        ops.start = AsyncMock(return_value=start or state("running"))
    if isinstance(wait, Exception):
        ops.wait = AsyncMock(side_effect=wait)
    else:
        ops.wait = AsyncMock(return_value=wait or state("succeeded"))
    ops.ping = AsyncMock(return_value=True)
    return ops


class UpdatePalworldTests(unittest.TestCase):
    def run_update(self, ops, info=None, on_progress=None):
        clock = FakeClock()
        self.info = info or AsyncMock()
        self.warn = AsyncMock()
        self.broadcast = AsyncMock()
        self.announce = AsyncMock()
        self.save = AsyncMock()
        # restart.host_control_refusal() reads restart's host_ops; the update
        # itself reads server_update's. Both are the same singleton in the bot.
        with patch.object(restart, "host_ops", ops), \
             patch.object(server_update, "host_ops", ops), \
             patch.object(server_update, "warn_and_wait", self.warn), \
             patch.object(server_update, "broadcast_embed", self.broadcast), \
             patch.object(server_update.rest, "info", self.info), \
             patch.object(server_update.rest, "announce", self.announce), \
             patch.object(server_update.rest, "save", self.save), \
             patch.object(server_update, "_now", clock.now), \
             patch.object(server_update, "_sleep", clock.sleep):
            embed = asyncio.run(server_update.update_palworld(on_progress))
        # A leaked True would make log_tailer treat every future unplanned
        # shutdown as a planned one, permanently.
        self.assertFalse(restart._bot_restart_in_progress)
        return embed

    def test_update_progress_shows_latest_log_line(self):
        ops = fake_host_ops()

        async def fake_wait(action, posted_at, timeout_s, on_line=None):
            await on_line(PROGRESS_LINE)
            return state("succeeded", PROGRESS_LINE)

        ops.wait = AsyncMock(side_effect=fake_wait)
        progress = AsyncMock()
        self.run_update(ops, on_progress=progress)

        messages = [call.args[0] for call in progress.await_args_list]
        self.assertTrue(any(PROGRESS_LINE in message for message in messages), messages)

    def test_update_succeeds_then_waits_for_rest(self):
        ops = fake_host_ops()
        embed = self.run_update(ops)

        self.assertEqual(embed.title, "Server updated")
        ops.start.assert_awaited_once_with("update")
        self.assertEqual(ops.wait.call_args.args[0], "update")
        posted_at = ops.wait.call_args.args[1]
        self.assertIsNotNone(posted_at.tzinfo, "posted_at must be timezone-aware")
        self.warn.assert_awaited_once()
        self.save.assert_awaited_once()
        self.info.assert_awaited()

    def test_update_succeeded_but_rest_silent_times_out(self):
        ops = fake_host_ops()
        embed = self.run_update(ops, info=AsyncMock(side_effect=ConnectionError("down")))
        self.assertEqual(embed.title, "Update timed out")
        self.assertIn("120s", embed.fields[0].value)

    def test_update_failed_shows_last_500_chars(self):
        tail = "Running steamcmd +app_update 2394010 validate\n" + "x" * 700
        ops = fake_host_ops(wait=state("failed", tail))
        with self.assertLogs("swee", level="ERROR"):
            embed = self.run_update(ops)

        self.assertEqual(embed.title, "Update failed")
        self.assertEqual(embed.fields[0].value, "```…" + tail[-500:] + "```")
        self.assertIn("restarted with the existing install", embed.fields[1].value)
        self.broadcast.assert_not_called()
        self.announce.assert_not_called()

    def test_update_stop_failure_retracts(self):
        ops = fake_host_ops(wait=state("failed", "Failed to stop palworld-palchuds.service: timed out"))
        with self.assertLogs("swee", level="ERROR"):
            embed = self.run_update(ops)

        self.assertEqual(embed.title, "Update failed")
        self.broadcast.assert_awaited_once()
        self.assertEqual(self.broadcast.call_args.args[0], "Update aborted")
        self.announce.assert_awaited_once()
        self.assertIn("Update cancelled", self.announce.call_args.args[0])
        self.assertIn("staying up", self.announce.call_args.args[0])
        self.assertIn("not restarted", embed.fields[0].value)
        self.assertTrue(any("Failed to stop" in field.value for field in embed.fields), embed.fields)
        self.info.assert_not_called()

    def test_update_poll_timeout_is_35_minutes(self):
        ops = fake_host_ops()
        self.run_update(ops)
        self.assertEqual(ops.wait.call_args.kwargs["timeout_s"], 35 * 60)

    def test_update_wait_gives_up_reports_timeout(self):
        ops = fake_host_ops(wait=HostOpsError(UNAVAILABLE, "no result for update after 2100s"))
        with self.assertLogs("swee", level="ERROR"):
            embed = self.run_update(ops)
        self.assertEqual(embed.title, "Update timed out")
        self.broadcast.assert_not_called()

    def test_update_start_refused_after_warning_retracts(self):
        """A POST refused after players were warned (e.g. an action started
        between the preflight and the POST): nothing ran, so the warning is
        retracted."""
        ops = fake_host_ops(start=HostOpsError(BUSY, 'HTTP 409: {"error": "busy"}', 409))
        with self.assertLogs("swee", level="ERROR"):
            embed = self.run_update(ops)
        self.assertEqual(embed.title, "Update failed")
        self.assertEqual(embed.fields[0].value, BUSY)
        self.assertEqual(self.broadcast.call_args.args[0], "Update aborted")
        self.announce.assert_awaited_once()
        ops.wait.assert_not_called()

    def test_update_refused_before_anything_is_announced(self):
        ops = fake_host_ops(preflight=HostOpsError(NOT_ALLOWED, 'HTTP 403: {"error": "not granted"}', 403))
        with self.assertLogs("swee", level="ERROR") as logs:
            embed = self.run_update(ops)

        self.assertEqual(embed.title, "Update failed")
        self.assertEqual(embed.fields[0].value, NOT_ALLOWED)
        self.assertTrue(any("not granted" in line for line in logs.output), logs.output)
        for mock in (self.warn, self.broadcast, self.announce, self.save, ops.start):
            mock.assert_not_called()

    def test_update_refused_while_own_action_in_flight(self):
        ops = fake_host_ops()
        warn = AsyncMock()
        with patch.object(restart, "_bot_restart_in_progress", True):
            with patch.object(restart, "host_ops", ops), \
                 patch.object(server_update, "host_ops", ops), \
                 patch.object(server_update, "warn_and_wait", warn):
                with self.assertLogs("swee", level="ERROR"):
                    embed = asyncio.run(server_update.update_palworld())
            # The refusal must not clear the flag owned by the action in flight.
            self.assertTrue(restart._bot_restart_in_progress)

        self.assertEqual(embed.title, "Update failed")
        self.assertEqual(embed.fields[0].value, BUSY)
        self.assertEqual(ops.mock_calls, [], "no host_ops call while swee's own action runs")
        warn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
