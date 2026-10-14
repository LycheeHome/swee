import asyncio
import logging
import subprocess
import time
from datetime import datetime, timezone

import discord

from swee.bot import bot
from swee.config import ALERTS_CHANNEL_ID, COLOR_LEAVE, COLOR_READY, COLOR_SHUTDOWN, PALWORLD_SERVICE_NAME, RAM_RESTART_WARNING_SEC
from swee.embeds import broadcast_embed
from swee.host_ops import SENTENCE_UNAVAILABLE, HostOpsError, host_ops
from swee.rest_client import rest

log = logging.getLogger("swee")

_bot_restart_in_progress = False  # true while a bot-initiated restart (/restart or auto) is in flight

# How long to poll lychee-admin for the restart's result: the restart
# service's TimeoutStartSec, after which systemd itself gives up on it.
RESTART_WAIT_S = 300
# How long to wait for the game's REST API once the restart has succeeded.
REST_WAIT_S = 120
LOG_TAIL_CHARS = 500

# Indirections the tests patch, so they never touch the asyncio or time
# modules every other caller shares.
_sleep = asyncio.sleep
_now = time.monotonic


def check_palworld_service():
    # Unprivileged: restarting the unit goes through lychee-admin's
    # host-operations socket, whose availability is checked (and only logged)
    # in main(), since lychee-admin restarts on every one of its deploys.
    load_state = subprocess.run(
        ["systemctl", "show", "-p", "LoadState", "--value", PALWORLD_SERVICE_NAME],
        capture_output=True, text=True,
    ).stdout.strip()
    if load_state != "loaded":
        log.error("%s.service not found (LoadState=%s) — check the unit is installed", PALWORLD_SERVICE_NAME, load_state or "unknown")
        return False
    return True


def _failed_embed(sentence):
    embed = discord.Embed(title="Restart failed", color=COLOR_LEAVE)
    embed.add_field(name="Status", value=sentence, inline=False)
    return embed


async def host_control_refusal():
    """The "Restart failed" embed to show instead of warning anyone, when
    lychee-admin's socket does not answer; None when it does. Checked before
    any warning so that a refusal broadcasts nothing to players. It cannot see
    a 409 coming (a GET answers while an action runs), so restart_palworld
    still reports one, after the warning."""
    if await host_ops.ping():
        return None
    log.error("restart: host control unavailable, not warning players")
    return _failed_embed(SENTENCE_UNAVAILABLE)


async def restart_palworld(on_progress=None):
    posted_at = datetime.now(timezone.utc)
    try:
        await host_ops.start("restart")
        if on_progress:
            await on_progress("Restarting the server…")
        result = await host_ops.wait("restart", posted_at, timeout_s=RESTART_WAIT_S)
    except HostOpsError as error:
        log.error("restart: %s (%s)", error.sentence, error.detail)
        return _failed_embed(error.sentence)

    if result.state != "succeeded":
        # Success requires `succeeded`: polling REST after a failed restart
        # would report on whatever state the server was already in — a server
        # that was already up reads "back online after 0s" having restarted
        # nothing.
        log.error("restart: the restart action ended %s: %s", result.state, result.log_tail)
        embed = _failed_embed(
            f"Could not restart {PALWORLD_SERVICE_NAME} — check "
            f"`journalctl -u {PALWORLD_SERVICE_NAME}`."
        )
        tail = result.log_tail.strip()
        if tail:
            shown = tail if len(tail) <= LOG_TAIL_CHARS else "…" + tail[-LOG_TAIL_CHARS:]
            embed.add_field(name="Log", value=f"```\n{shown}\n```", inline=False)
        return embed

    if on_progress:
        await on_progress("Waiting for server to come back online…")

    start = _now()
    timeout = REST_WAIT_S
    online = False
    while _now() - start < timeout:
        try:
            await rest.info()
            online = True
            break
        except Exception:
            await _sleep(5)

    elapsed = int(_now() - start)
    embed = discord.Embed(color=COLOR_READY if online else COLOR_LEAVE)
    if online:
        embed.title = "Server restarted"
        embed.add_field(name="Status", value=f"Back online after {elapsed}s")
    else:
        embed.title = "Restart timed out"
        embed.add_field(
            name="Status",
            value=f"No response after {timeout}s — check `journalctl -u {PALWORLD_SERVICE_NAME}`",
        )
    return embed


async def warn_and_wait(discord_title, discord_description, ingame_message):
    warning_sec = int(RAM_RESTART_WARNING_SEC)
    await broadcast_embed(
        discord_title,
        discord_description,
        COLOR_SHUTDOWN,
        channel_id=ALERTS_CHANNEL_ID,
    )
    try:
        await rest.announce(ingame_message)
    except Exception:
        log.exception("in-game restart announce failed")
    await asyncio.sleep(warning_sec)


async def auto_restart_sequence(pct):
    global _bot_restart_in_progress
    warning_sec = int(RAM_RESTART_WARNING_SEC)
    embed = await host_control_refusal()
    if embed is None:
        await warn_and_wait(
            "High RAM usage detected",
            f"RAM usage at {pct}% — restarting server in {warning_sec}s.",
            f"Server restarting in {warning_sec}s due to high memory usage",
        )

        _bot_restart_in_progress = True
        try:
            embed = await restart_palworld()
        finally:
            _bot_restart_in_progress = False

    channel = bot.get_channel(ALERTS_CHANNEL_ID)
    if isinstance(channel, discord.TextChannel):
        await channel.send(embed=embed)
    else:
        log.warning("auto-restart result broadcast failed: channel %s not found or not a text channel", ALERTS_CHANNEL_ID)


def _log_auto_restart_failure(task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("auto-restart sequence failed", exc_info=exc)
