"""Tests for swee/server_update.py's update_palworld(). asyncio.create_
subprocess_exec is mocked throughout via FakeProc below, so what's verified
here is the Python-level control flow only — the real sudo grants, the
update wrapper, and steamcmd's actual exit codes are unverified by this
suite and can only be checked on the host.
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

import swee.server_update as server_update


class FakeProc:
    """Stands in for the process object asyncio.create_subprocess_exec returns."""

    def __init__(self, returncode, output=b"", stderr=b""):
        self.returncode = returncode
        self._output = output
        self._stderr = stderr

    async def wait(self):
        return self.returncode

    async def communicate(self):
        return self._output, self._stderr


def _is_preflight_call(args):
    """True for one of _missing_sudo_grant()'s `sudo -n -l <cmd>` checks,
    as opposed to a real `sudo <cmd>` invocation. Needed because the real
    wrapper call (`sudo`, SWEE_UPDATE_WRAPPER) and the preflight's check of
    that same grant (`sudo`, `-n`, `-l`, SWEE_UPDATE_WRAPPER) both contain
    the wrapper path — only the argument shape tells them apart."""
    return "-n" in args and "-l" in args


class UpdatePalworldTests(unittest.TestCase):
    def setUp(self):
        # Every path through update_palworld() broadcasts a warning and
        # saves the world before it ever touches the service — neither is
        # under test here, so both are stubbed out.
        self.warn_and_wait_mock = AsyncMock()
        self._patch(server_update, "warn_and_wait", self.warn_and_wait_mock)
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
            if _is_preflight_call(args):
                return FakeProc(0)  # all three grants present
            if "stop" in args:
                return FakeProc(1)
            return FakeProc(0, b"ok")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            embed = asyncio.run(server_update.update_palworld())

        wrapper_run = ("sudo", server_update.SWEE_UPDATE_WRAPPER)
        self.assertNotIn(wrapper_run, calls, "the update wrapper ran after a failed stop")
        self.assertEqual(embed.title, "Update failed")
        self.assertIn("not restarted", embed.fields[0].value)
        # A leaked True here would make log_tailer.py treat every future
        # unplanned shutdown as a planned one, permanently, with no test
        # failing — see swee/log_tailer.py:126.
        self.assertFalse(server_update.restart_module._bot_restart_in_progress)

    def test_successful_flow_invokes_the_wrapper_not_steamcmd_directly(self):
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            if _is_preflight_call(args):
                return FakeProc(0)
            return FakeProc(0, b"Success! App '2394010' fully installed.")

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            embed = asyncio.run(server_update.update_palworld())

        # Exact-tuple, not substring: the sudoers entry pins the wrapper to
        # zero arguments, so a call that appended one would still contain
        # the wrapper path as a substring but would break /update for real.
        self.assertIn(("sudo", server_update.SWEE_UPDATE_WRAPPER), calls)
        self.assertFalse(
            any("steamcmd" in " ".join(c) for c in calls),
            "steamcmd was invoked directly instead of through the wrapper",
        )
        self.assertEqual(embed.title, "Server updated")

    def test_failed_start_is_logged_but_does_not_abort(self):
        async def fake_exec(*args, **kwargs):
            if _is_preflight_call(args):
                return FakeProc(0)
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

    def test_missing_grant_aborts_before_warn_and_wait(self):
        """A missing grant is a configuration error, not a live-server
        emergency — it must be caught before the bot ever broadcasts an
        update warning or saves the world, not merely before the wrapper
        runs."""

        async def fake_exec(*args, **kwargs):
            # The preflight checks systemctl-stop's grant first; fail it
            # to simulate that grant being missing (e.g. mid-migration,
            # the window Phase 4's narrowing is designed to be caught in).
            return FakeProc(1)

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            embed = asyncio.run(server_update.update_palworld())

        self.warn_and_wait_mock.assert_not_called()
        self.assertEqual(embed.title, "Update failed")
        self.assertIn("systemctl stop", embed.fields[0].value)

    def test_failed_wrapper_with_successful_restart_reports_old_install_is_back(self):
        """start_rc == 0 arm of the steamcmd_ok split (c2a824b). Neither
        arm was reachable before: the failed-stop test above returns
        before the wrapper ever runs, and the other two tests have the
        wrapper exiting 0. Here the wrapper fails but the restart
        afterward succeeds, so the embed must say the previously-installed
        files are back up rather than leaving that ambiguous."""

        async def fake_exec(*args, **kwargs):
            if _is_preflight_call(args):
                return FakeProc(0)
            if args == ("sudo", server_update.SWEE_UPDATE_WRAPPER):
                return FakeProc(8, b"Error! App '2394010' state is 0x202 after update job.")
            return FakeProc(0)  # systemctl stop and start both succeed

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            embed = asyncio.run(server_update.update_palworld())

        self.assertEqual(embed.title, "Update failed")
        self.assertIn("state is 0x202", embed.fields[0].value)
        self.assertIn("still restarted", embed.fields[1].value)

    def test_failed_wrapper_and_failed_start_reports_compound_failure(self):
        """The compound case named in review: the wrapper fails AND the
        restart afterward also fails. This is the arm where the wording
        matters most — the operator needs to know the server is down, not
        just that the update itself failed."""

        async def fake_exec(*args, **kwargs):
            if _is_preflight_call(args):
                return FakeProc(0)
            if args == ("sudo", server_update.SWEE_UPDATE_WRAPPER):
                return FakeProc(8, b"Error! App '2394010' state is 0x202 after update job.")
            if "start" in args:
                return FakeProc(1)
            return FakeProc(0)  # stop succeeds

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
            with self.assertLogs("swee", level="ERROR") as logs:
                embed = asyncio.run(server_update.update_palworld())

        self.assertEqual(embed.title, "Update failed")
        self.assertIn("state is 0x202", embed.fields[0].value)
        self.assertIn("restart afterward also failed", embed.fields[1].value)
        self.assertIn("exit 1", embed.fields[1].value)
        self.assertTrue(
            any("start failed" in message for message in logs.output),
            f"expected a logged start failure, got: {logs.output}",
        )


if __name__ == "__main__":
    unittest.main()
