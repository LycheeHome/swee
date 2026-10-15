"""Tests for swee/log_tailer.py's file follower.

In the container swee cannot run journalctl: lychee-ops' root forwarder
(palworld-events.service) appends the game unit's journal lines as JSON to a
file, logrotate rotates it with copytruncate, and swee tails that file. These
pin what the follower must do across a forwarder restart or a rotation: never
replay lines that were there before it started, never stop relaying, never
emit half a line.

FileFollower.poll() is synchronous and does one read, so every case here is
deterministic: no sleeping, no timing.
"""
import asyncio
import json
import os
import tempfile
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

import swee.config as config
import swee.log_tailer as log_tailer
from swee.log_tailer import FileFollower


class FollowerTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "palworld.jsonl")

    def write(self, text, mode="a"):
        with open(self.path, mode) as f:
            f.write(text)

    def follower(self):
        follower = FileFollower(self.path)
        self.addCleanup(follower.close)
        return follower


class FileFollowerTests(FollowerTestCase):
    def test_tailer_starts_at_end(self):
        self.write("old 1\nold 2\n")
        follower = self.follower()
        self.assertEqual(follower.poll(), [])
        self.write("new\n")
        self.assertEqual(follower.poll(), ["new"])

    def test_tailer_follows_appends(self):
        self.write("")
        follower = self.follower()
        self.write("a\nb\n")
        self.assertEqual(follower.poll(), ["a", "b"])
        self.assertEqual(follower.poll(), [])
        self.write("c\n")
        self.assertEqual(follower.poll(), ["c"])

    def test_partial_line_is_buffered(self):
        self.write("")
        follower = self.follower()
        self.write('{"MESSAGE": "hal')
        self.assertEqual(follower.poll(), [])
        self.write('f"}\nnext\n')
        self.assertEqual(follower.poll(), ['{"MESSAGE": "half"}', "next"])

    def test_tailer_reopens_after_copytruncate(self):
        self.write("old 1\nold 2\nold 3\n")
        follower = self.follower()
        self.write("before rotation\n")
        self.assertEqual(follower.poll(), ["before rotation"])
        # logrotate's copytruncate: same inode, size drops to zero, then the
        # forwarder's next append lands at the start of the file.
        os.truncate(self.path, 0)
        self.write("after\n")
        self.assertEqual(follower.poll(), ["after"])
        self.assertEqual(follower.poll(), [])
        self.write("again\n")
        self.assertEqual(follower.poll(), ["again"])

    def test_truncation_alone_emits_nothing(self):
        self.write("old\n")
        follower = self.follower()
        os.truncate(self.path, 0)
        self.assertEqual(follower.poll(), [])
        self.write("fresh\n")
        self.assertEqual(follower.poll(), ["fresh"])

    def test_tailer_reopens_after_replace(self):
        self.write("old\n")
        follower = self.follower()
        old_inode = os.stat(self.path).st_ino
        self.write("late line in the old file\n")
        replacement = self.path + ".new"
        with open(replacement, "w") as f:
            f.write("first in the new file\n")
        os.replace(replacement, self.path)
        self.assertNotEqual(os.stat(self.path).st_ino, old_inode)
        # The old file's unread tail is drained before switching, and the new
        # file is read from its start: everything in it was written after the
        # follower started.
        self.assertEqual(follower.poll(), ["late line in the old file", "first in the new file"])
        self.write("second\n")
        self.assertEqual(follower.poll(), ["second"])

    def test_missing_file_is_waited_for(self):
        follower = self.follower()
        self.assertEqual(follower.poll(), [])
        self.assertEqual(follower.poll(), [])
        # A file that appears after the follower started holds only new lines.
        self.write("created\n")
        self.assertEqual(follower.poll(), ["created"])

    def test_file_removed_then_recreated(self):
        self.write("old\n")
        follower = self.follower()
        os.unlink(self.path)
        self.assertEqual(follower.poll(), [])
        self.write("recreated\n")
        self.assertEqual(follower.poll(), ["recreated"])


def denied(*args, **kwargs):
    raise PermissionError(13, "Permission denied")


class OpenFailureTests(FollowerTestCase):
    """An open that fails with anything but ENOENT says nothing about how old
    the file's contents are -- it existed, and may hold a day of lines -- so
    when it clears the follower must not read it from the top. Patched rather
    than chmod'ed, because a suite run as root would open a 000 file anyway."""

    def test_permission_error_that_clears_does_not_replay(self):
        self.write("old join\nold leave\n")
        with patch("swee.log_tailer.open", side_effect=denied, create=True):
            follower = self.follower()
            self.assertEqual(follower.poll(), [])
        self.assertEqual(follower.poll(), [])
        self.write("new\n")
        self.assertEqual(follower.poll(), ["new"])

    def test_replaced_file_unreadable_then_readable_does_not_replay(self):
        self.write("")
        follower = self.follower()
        self.write("a\n")
        self.assertEqual(follower.poll(), ["a"])
        replacement = self.path + ".new"
        with open(replacement, "w") as f:
            f.write("written while unreadable\n")
        os.replace(replacement, self.path)
        with patch("swee.log_tailer.open", side_effect=denied, create=True):
            self.assertEqual(follower.poll(), [])
            self.assertEqual(follower.poll(), [])
        self.assertEqual(follower.poll(), [])
        self.write("fresh\n")
        self.assertEqual(follower.poll(), ["fresh"])

    def test_same_file_back_after_a_failure_resumes_where_it_left_off(self):
        self.write("")
        follower = self.follower()
        self.write("a\n")
        self.assertEqual(follower.poll(), ["a"])
        held = self.path + ".held"
        os.rename(self.path, held)
        self.assertEqual(follower.poll(), [])
        with open(held, "a") as f:
            f.write("while away\n")
        os.rename(held, self.path)
        with patch("swee.log_tailer.open", side_effect=denied, create=True):
            self.assertEqual(follower.poll(), [])
        # The file it was reading: nothing before the remembered offset again,
        # and nothing after it lost.
        self.assertEqual(follower.poll(), ["while away"])

    def test_enoent_alone_still_reads_a_new_file_from_its_start(self):
        follower = self.follower()
        self.assertEqual(follower.poll(), [])
        self.write("created\n")
        self.assertEqual(follower.poll(), ["created"])


class SettingsTests(unittest.TestCase):
    def test_events_file_default(self):
        self.assertEqual(config.PALWORLD_EVENTS_FILE, "/palworld-events/palworld.jsonl")

    def test_unattended_upgrades_log_default(self):
        import swee.cause_detection as cause_detection
        self.assertEqual(config.UNATTENDED_UPGRADES_LOG, "/var/log/unattended-upgrades/unattended-upgrades.log")
        self.assertEqual(cause_detection.UNATTENDED_UPGRADES_LOG, config.UNATTENDED_UPGRADES_LOG)


class RelayFromFileTests(unittest.IsolatedAsyncioTestCase):
    """End to end: a forwarder line appended to the file reaches the same
    parser journalctl's stdout used to, and is relayed."""

    async def test_join_line_in_the_file_is_relayed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "palworld.jsonl")
            with open(path, "w") as f:
                # A pre-existing join must not be replayed.
                f.write(json.dumps({"MESSAGE": "[2026-10-14 10:00:00] [LOG] Old joined the server",
                                    "__REALTIME_TIMESTAMP": "1760000000000000"}) + "\n")
            broadcast = AsyncMock(return_value=True)
            with patch.object(log_tailer, "PALWORLD_EVENTS_FILE", path), \
                 patch.object(log_tailer, "POLL_INTERVAL_S", 0.01), \
                 patch.object(log_tailer, "broadcast_embed", broadcast), \
                 patch.object(log_tailer, "record_join", AsyncMock()), \
                 patch.object(log_tailer, "update_stats_message", AsyncMock()):
                task = asyncio.create_task(log_tailer.log_tailer())
                try:
                    await asyncio.sleep(0.05)
                    with open(path, "a") as f:
                        f.write(json.dumps({
                            "MESSAGE": "[2026-10-14 10:00:01] [LOG] Lamball joined the server (User id: steam_1)",
                            "__REALTIME_TIMESTAMP": "1760000001000000",
                        }) + "\n")
                    for _ in range(100):
                        if broadcast.await_count:
                            break
                        await asyncio.sleep(0.01)
                finally:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            broadcast.assert_awaited_once()
            self.assertEqual(broadcast.await_args.args[0], "Lamball joined the server")


if __name__ == "__main__":
    unittest.main()
