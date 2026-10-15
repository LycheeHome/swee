import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone

import swee.assistant as assistant
import swee.restart as restart_module
from swee.cause_detection import check_palworld_settings_change, detect_unplanned_restart_cause, save_last_palworld_settings
from swee.config import ALERTS_CHANNEL_ID, COLOR_JOIN, COLOR_LEAVE, COLOR_READY, COLOR_SHUTDOWN, PACIFIC, PALWORLD_EVENTS_FILE
from swee.embeds import broadcast_embed
from swee.player_history import online_players, record_join, record_leave
from swee.stats import update_stats_message

log = logging.getLogger("swee")

JOIN_RE     = re.compile(r'\[LOG\]\s*(.+?) joined the server')
LEAVE_RE    = re.compile(r'\[LOG\]\s*(.+?) left the server')
CONNECT_RE  = re.compile(r'\[LOG\]\s*(.+?) [\d.]+ connected the server')
USER_ID_RE  = re.compile(r'User id:\s*([^\s,)]+)')
TS_RE       = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s*(.*)')
SHUTDOWN_RE = re.compile(r'Shutdown handler: initialize\.')
VERSION_RE  = re.compile(r'Game version is (v[\d.]+)')
CHAT_RE     = re.compile(r'\[CHAT\]\s*<(.+?)>\s*(.*)')

FALLBACK_JOIN_DELAY_SEC = 30

# Palworld doesn't always log "X joined the server" for a connection that
# clearly succeeded (player ends up chatting/playing) — some sessions only
# ever get a "connected" line. This tracks a per-player timer, started on
# "connected", that fires a fallback join notification unless a real
# "joined" or "left" line cancels it first.
#
# Keyed by the "(User id: steam_...)" suffix rather than display name: the name in a
# "connected" line is the player's Steam persona name, while "joined"/"left" lines use
# their in-game player name, and those two names can differ for the same player. Keying
# by name meant a real "joined" line couldn't cancel the pending fallback started by
# "connected" if the two names didn't match, producing a duplicate join notification
# under the wrong (persona) name alongside the real one.
pending_connects = {}  # user id (or display name, if no user id found) -> asyncio.Task
_assistant_tasks = set()


async def _fallback_join(key, name, dt):
    await asyncio.sleep(FALLBACK_JOIN_DELAY_SEC)
    # Self-pop after the await is safe here (unlike a plain dict mutation elsewhere in
    # the codebase, which must never cross an await) because nothing else can run
    # between the sleep resolving and this pop in asyncio's single-threaded loop; a
    # caller-side cancel-and-pop racing this is a no-op since pop() defaults to None.
    pending_connects.pop(key, None)
    try:
        await broadcast_embed(f"{name} joined the server", None, COLOR_JOIN, dt)
        await record_join(name, dt)
        await update_stats_message()
    except Exception:
        log.exception("fallback join broadcast failed for player %s", name)


POLL_INTERVAL_S = 0.5


class FileFollower:
    """Follows the forwarder's file the way `tail -F` would, one poll() at a
    time, so the cases that matter are testable without timing:

    - it starts at the END of a file that already exists: what was there
      before swee started is history, and replaying it would re-announce old
      joins;
    - it reopens when the path names a different file (the forwarder's
      file replaced or removed and recreated), after draining what was left
      unread in the old one, and reads the new file from its start;
    - it rewinds when the file shrinks under it (logrotate's copytruncate,
      the rule lychee-ops installs);
    - it waits, quietly, for a file that does not exist yet;
    - after an open that failed for any reason but ENOENT (a permission
      error, say), it opens at the END again, as on first start: the file
      existed all along and may hold a day of lines, so reading it from the
      top would re-announce them. The exception is the file it was already
      reading (same device and inode, not shorter than where it stopped),
      which it resumes from where it left off, so nothing is lost or repeated;
    - it returns only complete lines and holds a partial last line until its
      newline arrives.

    Polled, not inotify: no dependency, and a half-second poll costs one
    stat(). The one case it cannot see is a truncation followed by appends
    that outgrow the old read position before the next poll; at the forwarder's
    rate (a few lines a minute) and one rotation a day, that is not a
    realistic window.
    """

    def __init__(self, path):
        self.path = path
        self._file = None
        self._id = None
        self._buf = b""
        self._open_failed = None
        # True from an open failure other than ENOENT until the next open
        # succeeds: what is in the file by then is of unknown age.
        self._blind = False
        # (device and inode, offset) of the file poll() last let go of, so a
        # reopen after a failure can tell "the same file" from "a new one".
        self._last = None
        self._open(at_end=True)

    def _open(self, at_end):
        try:
            f = open(self.path, "rb")
        except OSError as error:
            if not isinstance(error, FileNotFoundError):
                self._blind = True
            # Logged once per kind of failure, not every half second.
            kind = type(error).__name__
            if self._open_failed != kind:
                if isinstance(error, FileNotFoundError):
                    log.info("log tailer: waiting for %s to exist", self.path)
                else:
                    log.warning("log tailer: cannot open %s (%s)", self.path, error)
                self._open_failed = kind
            return False
        st = os.fstat(f.fileno())
        file_id = (st.st_dev, st.st_ino)
        if at_end:
            f.seek(0, os.SEEK_END)
        elif self._blind:
            last_id, last_offset = self._last or (None, 0)
            if last_id == file_id and st.st_size >= last_offset:
                f.seek(last_offset)
            else:
                f.seek(0, os.SEEK_END)
        if self._open_failed is not None:
            log.info("log tailer: following %s", self.path)
        self._open_failed = None
        self._blind = False
        self._last = None
        self._file, self._id, self._buf = f, file_id, b""
        return True

    def _read(self):
        self._buf += self._file.read()
        *complete, self._buf = self._buf.split(b"\n")
        return [line.decode("utf-8", errors="replace") for line in complete]

    def poll(self):
        lines = []
        if self._file is not None:
            lines += self._read()
            try:
                st = os.stat(self.path)
            except FileNotFoundError:
                st = None
            if st is None or (st.st_dev, st.st_ino) != self._id:
                # Replaced or removed: the old file is drained (above); a
                # partial line left in it will never be finished -- unless the
                # same file comes back after a failed open, which resumes at
                # the start of that partial line.
                last = (self._id, self._file.tell() - len(self._buf))
                self.close()
                self._last = last
            elif st.st_size < self._file.tell():
                # Truncated in place: start again from the top.
                self._file.seek(0)
                self._buf = b""
                return lines + self._read()
            else:
                return lines
        if self._open(at_end=False):
            lines += self._read()
        return lines

    def close(self):
        if self._file is not None:
            self._file.close()
        self._file, self._id, self._buf = None, None, b""


async def handle_line(line):
    """One forwarder line: a `journalctl -o json` object, parsed exactly as
    the journalctl subprocess's stdout used to be."""
    line = line.strip()
    if not line:
        return
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return

    msg = entry.get("MESSAGE", "")
    if isinstance(msg, list):
        msg = " ".join(str(m) for m in msg)
    if not isinstance(msg, str):
        return

    micros = int(entry.get("__REALTIME_TIMESTAMP", 0))
    dt = datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc).astimezone(PACIFIC)

    ts_match = TS_RE.match(msg)
    if ts_match:
        _, rest_msg = ts_match.groups()
        if m := CONNECT_RE.search(rest_msg):
            name = m.group(1)
            uid_m = USER_ID_RE.search(rest_msg)
            key = uid_m.group(1) if uid_m else name
            if pending := pending_connects.pop(key, None):
                pending.cancel()
            pending_connects[key] = asyncio.create_task(_fallback_join(key, name, dt))
        elif m := JOIN_RE.search(rest_msg):
            name = m.group(1)
            uid_m = USER_ID_RE.search(rest_msg)
            key = uid_m.group(1) if uid_m else name
            if pending := pending_connects.pop(key, None):
                pending.cancel()
            await broadcast_embed(f"{name} joined the server", None, COLOR_JOIN, dt)
            await record_join(name, dt)
            await update_stats_message()
        elif m := LEAVE_RE.search(rest_msg):
            name = m.group(1)
            uid_m = USER_ID_RE.search(rest_msg)
            key = uid_m.group(1) if uid_m else name
            if pending := pending_connects.pop(key, None):
                pending.cancel()
            # must resolve before record_leave() pops `name` out of online_players below
            assistant.clear_session(assistant.resolve_player_id(name, online_players))
            await broadcast_embed(f"{name} left the server", None, COLOR_LEAVE, dt)
            await record_leave(name, dt)
            await update_stats_message()
        elif m := CHAT_RE.search(rest_msg):
            name, text = m.groups()
            question = assistant.parse_mention(text)
            if question:
                task = asyncio.create_task(assistant.handle_mention(name, question))
                _assistant_tasks.add(task)
                task.add_done_callback(_assistant_tasks.discard)
    else:
        if SHUTDOWN_RE.search(msg):
            if restart_module._bot_restart_in_progress:
                await broadcast_embed("Server shutting down", None, COLOR_SHUTDOWN, dt, channel_id=ALERTS_CHANNEL_ID)
            else:
                cause_result = await detect_unplanned_restart_cause(dt)
                cause_text, pending_settings = cause_result or (None, None)
                sent = await broadcast_embed(
                    "Server restarted unexpectedly",
                    None,
                    COLOR_SHUTDOWN,
                    dt,
                    channel_id=ALERTS_CHANNEL_ID,
                    fields=[("Likely cause", cause_text or "Unknown — an admin will need to check the server logs.")],
                )
                if sent and pending_settings is not None:
                    save_last_palworld_settings(pending_settings)
        elif m := VERSION_RE.search(msg):
            await broadcast_embed("Server is online", f"Game version: `{m.group(1)}`", COLOR_READY, dt, channel_id=ALERTS_CHANNEL_ID)
            await check_palworld_settings_change()


async def log_tailer():
    # One follower for the life of the bot: a failure handling one line is
    # logged and the next line still arrives, and nothing re-seeks to the end
    # of the file (which would drop whatever was appended meanwhile).
    follower = FileFollower(PALWORLD_EVENTS_FILE)
    try:
        while True:
            try:
                lines = follower.poll()
            except OSError:
                log.exception("log tailer: reading %s failed", PALWORLD_EVENTS_FILE)
                lines = []
            for line in lines:
                try:
                    await handle_line(line)
                except Exception:
                    log.exception("log tailer: handling a line failed")
            await asyncio.sleep(POLL_INTERVAL_S)
    finally:
        follower.close()
