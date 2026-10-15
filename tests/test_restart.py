"""Tests for swee/restart.py: /restart and the RAM auto-restart go through
lychee-admin's host-operations socket (swee/host_ops.py), never sudo. The
host_ops singleton is patched throughout, so this verifies swee's control
flow; the socket itself is covered by tests/test_host_ops.py and the real
grant can only be checked on the host.
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

import main
import swee.commands as commands
import swee.restart as restart
from swee.host_ops import ActionState, HostOpsError

BUSY = "an action is already running on the server"
UNAVAILABLE = "host control is unavailable right now"
NOT_ALLOWED = "swee is not allowed to do that"
SOCKET = "/run/lychee-admin/clients/swee/api.sock"
NOW = datetime(2026, 10, 14, 18, 0, 0, tzinfo=timezone.utc)


def state(name, log_tail=""):
    return ActionState(state=name, started_at=NOW, finished_at=None, log_tail=log_tail)


class FakeClock:
    """Stands in for restart._now / restart._sleep: sleeping advances time."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.t += seconds


def fake_host_ops(start=None, wait=None, ping=True, preflight=None):
    ops = MagicMock()
    ops.preflight = AsyncMock(side_effect=preflight)
    ops.socket_path = SOCKET
    if isinstance(start, Exception):
        ops.start = AsyncMock(side_effect=start)
    else:
        ops.start = AsyncMock(return_value=start or state("running"))
    ops.wait = AsyncMock(return_value=wait or state("succeeded"))
    ops.ping = AsyncMock(return_value=ping)
    return ops


class RestartPalworldTests(unittest.TestCase):
    def run_restart(self, ops, info=None):
        clock = FakeClock()
        info = info or AsyncMock()
        broadcast = AsyncMock()
        announce = AsyncMock()
        with patch.object(restart, "host_ops", ops), \
             patch.object(restart.rest, "info", info), \
             patch.object(restart.rest, "announce", announce), \
             patch.object(restart, "broadcast_embed", broadcast), \
             patch.object(restart, "_now", clock.now), \
             patch.object(restart, "_sleep", clock.sleep):
            embed = asyncio.run(restart.restart_palworld())
        return embed, broadcast, announce, info

    def test_restart_succeeds_then_waits_for_rest(self):
        ops = fake_host_ops()
        embed, _, _, info = self.run_restart(ops)

        self.assertEqual(embed.title, "Server restarted")
        ops.start.assert_awaited_once_with("restart")
        args, kwargs = ops.wait.call_args
        self.assertEqual(args[0], "restart")
        posted_at = args[1] if len(args) > 1 else kwargs["posted_at"]
        self.assertIsNotNone(posted_at.tzinfo, "posted_at must be timezone-aware")
        timeout = args[2] if len(args) > 2 else kwargs["timeout_s"]
        self.assertEqual(timeout, 300)
        info.assert_awaited()

    def test_restart_failed_shows_log_tail(self):
        ops = fake_host_ops(wait=state("failed", "Job for palworld-palchuds.service failed\nboom"))
        with self.assertLogs("swee", level="ERROR"):
            embed, _, _, info = self.run_restart(ops)

        self.assertEqual(embed.title, "Restart failed")
        self.assertTrue(any("boom" in field.value for field in embed.fields), embed.fields)
        # A failed restart is never followed by the online poll, which would
        # report on whatever state the server was already in ("back online
        # after 0s" having restarted nothing).
        info.assert_not_called()

    def test_restart_busy_broadcasts_nothing(self):
        ops = fake_host_ops(start=HostOpsError(BUSY, 'HTTP 409: {"error": "busy"}', 409))
        with self.assertLogs("swee", level="ERROR") as logs:
            embed, broadcast, announce, info = self.run_restart(ops)

        self.assertEqual(embed.title, "Restart failed")
        self.assertEqual(embed.fields[0].value, BUSY)
        broadcast.assert_not_called()
        announce.assert_not_called()
        info.assert_not_called()
        ops.wait.assert_not_called()
        self.assertTrue(any("HTTP 409" in line for line in logs.output), logs.output)

    def test_restart_unreachable(self):
        ops = fake_host_ops(start=HostOpsError(UNAVAILABLE, "ConnectError: refused"))
        with self.assertLogs("swee", level="ERROR"):
            embed, broadcast, announce, _ = self.run_restart(ops)

        self.assertEqual(embed.title, "Restart failed")
        self.assertEqual(embed.fields[0].value, UNAVAILABLE)
        broadcast.assert_not_called()
        announce.assert_not_called()

    def test_wait_timeout_is_reported_with_its_sentence(self):
        ops = fake_host_ops()
        ops.wait = AsyncMock(side_effect=HostOpsError(UNAVAILABLE, "no result for restart after 300s"))
        with self.assertLogs("swee", level="ERROR"):
            embed, _, _, info = self.run_restart(ops)

        self.assertEqual(embed.title, "Restart failed")
        self.assertEqual(embed.fields[0].value, UNAVAILABLE)
        info.assert_not_called()

    def test_succeeded_but_rest_silent_times_out(self):
        ops = fake_host_ops()
        info = AsyncMock(side_effect=ConnectionError("down"))
        embed, _, _, _ = self.run_restart(ops, info=info)

        self.assertEqual(embed.title, "Restart timed out")
        self.assertIn("120s", embed.fields[0].value)


class NoUnitCheckTests(unittest.TestCase):
    """swee runs in a container now, which has no systemctl: the startup
    LoadState check is gone, and the host-ops socket read in main() is what
    covers a missing unit (lychee-admin answers for the unit it operates)."""

    def test_startup_unit_check_is_gone(self):
        self.assertFalse(hasattr(restart, "check_palworld_service"))
        self.assertFalse(hasattr(main, "check_palworld_service"))
        self.assertFalse(hasattr(restart, "subprocess"))


class RefusedBeforeWarningTests(unittest.TestCase):
    """A refusal broadcasts nothing to players: neither /restart nor the RAM
    auto-restart warns anyone until preflight() has found nothing in the way,
    and the refusal shows its own sentence."""

    def run_command(self, ops):
        interaction = MagicMock()
        interaction.response.send_message = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        warn = AsyncMock()
        broadcast = AsyncMock()
        announce = AsyncMock()
        with patch.object(restart, "host_ops", ops), \
             patch.object(commands, "warn_and_wait", warn), \
             patch.object(restart, "broadcast_embed", broadcast), \
             patch.object(restart.rest, "announce", announce):
            with self.assertLogs("swee", level="ERROR") as logs:
                asyncio.run(commands.restart.callback(interaction))
        for mock in (warn, broadcast, announce, ops.start):
            mock.assert_not_called()
        final = interaction.edit_original_response.call_args.kwargs["embed"]
        self.assertEqual(final.title, "Restart failed")
        return final, logs

    def test_restart_command_refused_before_anything_is_announced(self):
        ops = fake_host_ops(preflight=HostOpsError(UNAVAILABLE, "ConnectError: refused"))
        final, _ = self.run_command(ops)
        self.assertEqual(final.fields[0].value, UNAVAILABLE)
        self.assertFalse(restart._bot_restart_in_progress)

    def test_restart_command_not_allowed_shows_its_sentence(self):
        ops = fake_host_ops(preflight=HostOpsError(NOT_ALLOWED, 'HTTP 403: {"error": "not granted"}', 403))
        final, logs = self.run_command(ops)
        self.assertEqual(final.fields[0].value, NOT_ALLOWED)
        self.assertTrue(any("not granted" in line for line in logs.output), logs.output)

    def test_restart_command_while_own_action_in_flight(self):
        ops = fake_host_ops()
        with patch.object(restart, "_bot_restart_in_progress", True):
            final, _ = self.run_command(ops)
        self.assertEqual(final.fields[0].value, BUSY)
        self.assertEqual(ops.mock_calls, [], "no host_ops call while swee's own action runs")

    def test_auto_restart_busy_broadcasts_nothing(self):
        ops = fake_host_ops(preflight=HostOpsError(BUSY, "update is running", 409))
        warn = AsyncMock()
        broadcast = AsyncMock()
        announce = AsyncMock()
        channel = MagicMock(spec=restart.discord.TextChannel)
        channel.send = AsyncMock()
        with patch.object(restart, "host_ops", ops), \
             patch.object(restart, "warn_and_wait", warn), \
             patch.object(restart, "broadcast_embed", broadcast), \
             patch.object(restart.rest, "announce", announce), \
             patch.object(restart.bot, "get_channel", return_value=channel):
            with self.assertLogs("swee", level="ERROR"):
                asyncio.run(restart.auto_restart_sequence(95))

        for mock in (warn, broadcast, announce, ops.start):
            mock.assert_not_called()
        sent = channel.send.call_args.kwargs["embed"]
        self.assertEqual(sent.title, "Restart failed")
        self.assertEqual(sent.fields[0].value, BUSY)


class StartupTests(unittest.TestCase):
    def test_startup_does_not_exit_when_socket_down(self):
        ops = fake_host_ops(ping=False)
        start = AsyncMock()
        with patch.object(main, "host_ops", ops), \
             patch.object(main, "load_player_history"), \
             patch.object(main, "load_session_state"), \
             patch.object(main, "load_last_release"), \
             patch.object(main, "load_last_event_id"), \
             patch.object(main, "load_last_palworld_settings"), \
             patch.object(main.discord.utils, "setup_logging"), \
             patch.object(main.bot, "start", start), \
             patch.object(main.rest.client, "aclose", AsyncMock()):
            with self.assertLogs("swee", level="INFO") as logs:
                asyncio.run(main.main())  # no SystemExit

        ops.ping.assert_awaited_once()
        start.assert_awaited_once()
        self.assertTrue(any(SOCKET in line for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
