"""/update: update the game server through lychee-admin's host-operations
socket (swee/host_ops.py). swee runs no sudo: lychee-ops' update script, run
by lychee-admin as the unit's `update` action, stops the server, runs
steamcmd and starts it again; swee warns players, saves the world, starts
the action and reports on it."""
import asyncio
import logging
import re
import time
from datetime import datetime, timezone

import discord

import swee.restart as restart_module
from swee.config import ALERTS_CHANNEL_ID, COLOR_LEAVE, COLOR_READY, PALWORLD_SERVICE_NAME, RAM_RESTART_WARNING_SEC
from swee.embeds import broadcast_embed
from swee.host_ops import SENTENCE_UNAVAILABLE, HostOpsError, host_ops
from swee.rest_client import rest
from swee.restart import LOG_TAIL_CHARS, REST_WAIT_S, warn_and_wait

log = logging.getLogger("swee")

# How long to poll lychee-admin for the update's result: the update service's
# TimeoutStartSec=1800 plus slack.
UPDATE_WAIT_S = 35 * 60

# lychee-ops' update script ends its log with exactly one outcome line:
#   lychee-op-update: stop failed (exit N)
#   lychee-op-update: update failed (exit N); start exit M
#   lychee-op-update: done
_MARKER = "lychee-op-update: "
_STOP_FAILED = re.compile(r"stop failed \(exit (\d+)\)")
_UPDATE_FAILED = re.compile(r"update failed \(exit (\d+)\); start exit (\d+)")

# Indirections the tests patch, so they never touch the asyncio or time
# modules every other caller shares.
_sleep = asyncio.sleep
_now = time.monotonic


def _failed_embed(value, name="Status"):
    embed = discord.Embed(title="Update failed", color=COLOR_LEAVE)
    embed.add_field(name=name, value=value, inline=False)
    return embed


def _shown_tail(log_tail):
    tail = log_tail.strip()
    if len(tail) > LOG_TAIL_CHARS:
        return "…" + tail[-LOG_TAIL_CHARS:]
    return tail


async def _retract():
    """Players were told the server is restarting for an update, and it is
    not: retract the warning in Discord and in-game. Both are best-effort, so
    a failed retraction cannot mask the failure being reported."""
    await broadcast_embed(
        "Update aborted",
        f"Could not stop {PALWORLD_SERVICE_NAME} — the update was cancelled and "
        "nothing was changed. The server was never taken down.",
        COLOR_LEAVE,
        channel_id=ALERTS_CHANNEL_ID,
    )
    # warn_and_wait announced in-game as well as in Discord, so a
    # Discord-only retraction would leave the players who were actually told
    # still expecting a restart. The server is up on this path.
    try:
        await rest.announce(
            f"Update cancelled — {PALWORLD_SERVICE_NAME} could not be stopped. "
            "The server is staying up."
        )
    except Exception:
        log.warning("server update: in-game retraction failed", exc_info=True)


def _outcome(log_tail):
    """The last `lychee-op-update: ` line lychee-ops' update script wrote,
    parsed: ("stop", N), ("update", N, M), ("done",), or None when there is
    none (a killed or timed-out run, or a host without that script yet).
    The last such line, not the last line: the recorder and the restart
    guard can append output after it."""
    for line in reversed(log_tail.splitlines()):
        line = line.strip()
        if not line.startswith(_MARKER):
            continue
        rest_of_line = line[len(_MARKER):]
        if match := _STOP_FAILED.fullmatch(rest_of_line):
            return ("stop", int(match[1]))
        if match := _UPDATE_FAILED.fullmatch(rest_of_line):
            return ("update", int(match[1]), int(match[2]))
        if rest_of_line == "done":
            return ("done",)
        return None
    return None


async def _wait_for_rest():
    start = _now()
    while _now() - start < REST_WAIT_S:
        try:
            await rest.info()
            return True
        except Exception:
            await _sleep(5)
    return False


async def _report_failure(log_tail, on_progress):
    log.error("server update: the update action failed: %s", log_tail)
    tail = _shown_tail(log_tail)
    shown = f"```{tail}```" if tail else "(no output)"
    outcome = _outcome(log_tail)

    if outcome is not None and outcome[0] == "stop":
        # The stop failed, so the server was never taken down and nothing
        # was updated: retract the warning players saw.
        await _retract()
        embed = _failed_embed(
            f"Could not stop {PALWORLD_SERVICE_NAME} (exit {outcome[1]}). Nothing was updated "
            "and the server was never taken down, so it was not restarted."
        )
        if tail:
            embed.add_field(name="Log", value=shown, inline=False)
        return embed

    if outcome is not None and outcome[0] == "update":
        embed = _failed_embed(shown, name="Update log")
        start_exit = outcome[2]
        if start_exit == 0:
            status = "Server was still restarted with the existing install."
        else:
            status = (
                f"The update failed (exit {outcome[1]}) and the restart afterward also failed "
                f"(exit {start_exit}) — the server is down. Check `systemctl status {PALWORLD_SERVICE_NAME}`."
            )
        embed.add_field(name="Status", value=status, inline=False)
        return embed

    # No outcome line: the run was killed, timed out, or refused before the
    # script ran. Make no claim about why and retract nothing; say only
    # whether the server is answering.
    embed = _failed_embed(shown, name="Update log")
    if on_progress:
        await on_progress("Update did not finish; waiting for the server to answer…")
    if await _wait_for_rest():
        status = "The update did not finish. The server is back online."
    else:
        status = (
            f"The update did not finish, and the server is not responding after {REST_WAIT_S}s — "
            f"check `systemctl status {PALWORLD_SERVICE_NAME}`."
        )
    embed.add_field(name="Status", value=status, inline=False)
    return embed


async def update_palworld(on_progress=None):
    # A refusal broadcasts nothing to players: check that lychee-admin would
    # accept the update (reachable, allowed, nothing running, swee's own
    # restart not in flight) before warning anyone.
    refusal = await restart_module.host_control_refusal("update")
    if refusal is not None:
        return refusal

    warning_sec = int(RAM_RESTART_WARNING_SEC)
    if on_progress:
        await on_progress("Broadcasting update warning…")
    await warn_and_wait(
        "Updating server",
        f"Updating server — restarting in {warning_sec}s for an update.",
        f"Server restarting in {warning_sec}s for an update",
    )

    if on_progress:
        await on_progress("Saving world…")
    try:
        await rest.save()
    except Exception:
        log.exception("server update: pre-update save failed")

    restart_module._bot_restart_in_progress = True
    try:
        return await _run_update(on_progress)
    finally:
        restart_module._bot_restart_in_progress = False


async def _run_update(on_progress):
    posted_at = datetime.now(timezone.utc)
    try:
        await host_ops.start("update")
    except HostOpsError as error:
        log.error("server update: %s (%s)", error.sentence, error.detail)
        if error.status is not None:
            # lychee-admin answered and refused (e.g. an action started
            # between the preflight and this POST): nothing ran, so the
            # warning players already saw is retracted. An unreachable socket
            # is not retracted: whether the POST landed is unknown.
            await _retract()
        return _failed_embed(error.sentence)

    if on_progress:
        await on_progress("Updating via steamcmd… this can take a few minutes")

    async def on_line(line):
        if on_progress:
            await on_progress(f"running · {line}")

    try:
        result = await host_ops.wait("update", posted_at, timeout_s=UPDATE_WAIT_S, on_line=on_line)
    except HostOpsError as error:
        log.error("server update: %s (%s)", error.sentence, error.detail)
        if error.sentence == SENTENCE_UNAVAILABLE:
            # wait() tolerates a briefly unreachable socket and raises this
            # sentence only once UPDATE_WAIT_S has passed.
            embed = discord.Embed(title="Update timed out", color=COLOR_LEAVE)
            embed.add_field(
                name="Status",
                value=f"No result from lychee-admin after {UPDATE_WAIT_S // 60} minutes — check "
                      f"`systemctl status {PALWORLD_SERVICE_NAME}` and `journalctl -u lychee-admin`.",
                inline=False,
            )
            return embed
        return _failed_embed(error.sentence)

    if result.state != "succeeded":
        return await _report_failure(result.log_tail, on_progress)

    if on_progress:
        await on_progress("Waiting for server to come back online…")
    online = await _wait_for_rest()

    if not online:
        embed = discord.Embed(title="Update timed out", color=COLOR_LEAVE)
        embed.add_field(
            name="Status",
            value=f"steamcmd succeeded but no response after {REST_WAIT_S}s — check "
                  f"`journalctl -u {PALWORLD_SERVICE_NAME}`",
        )
        return embed

    embed = discord.Embed(title="Server updated", color=COLOR_READY)
    embed.add_field(name="Status", value="steamcmd completed and the server is back online.")
    return embed
