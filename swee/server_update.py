import asyncio
import logging
import time

import discord

import swee.restart as restart_module
from swee.config import COLOR_LEAVE, COLOR_READY, PALWORLD_SERVICE_NAME, RAM_RESTART_WARNING_SEC, SWEE_UPDATE_WRAPPER
from swee.rest_client import rest
from swee.restart import warn_and_wait

log = logging.getLogger("swee")

# The three sudo grants /update depends on, checked in the order they're
# used. Each entry is the exact argument list `sudo -n -l` is asked about —
# it must match the command /update actually runs later, which is why the
# wrapper entry carries no arguments: the sudoers drop-in pins it to zero.
_REQUIRED_SUDO_GRANTS = (
    ("systemctl", "stop", PALWORLD_SERVICE_NAME),
    (SWEE_UPDATE_WRAPPER,),
    ("systemctl", "start", PALWORLD_SERVICE_NAME),
)


async def _missing_sudo_grant():
    """The first of _REQUIRED_SUDO_GRANTS that isn't configured NOPASSWD
    for this user, or None if all three are. Mirrors restart.py's
    check_palworld_service, but async rather than subprocess.run: this
    runs inside update_palworld(), which runs on the bot's event loop, and
    a blocking call here would stall every other command while sudo is
    consulted. Checked before anything else in update_palworld() — Phase 4
    of the identity-separation migration narrows this exact grant from two
    principals to one, and /update is how that narrowing gets verified, so
    a mis-narrowed grant must be caught here, before a warning has gone out
    to players and the world has been saved, not discovered only after.

    Captures stderr from each `sudo -n -l` check. Per `man sudo` EXIT
    VALUE, a non-zero return means an authentication failure, OR a
    configuration/permission problem, OR that the given command can't be
    executed at all — and only stderr says which. Discarding it (as this
    used to) left the caller unable to tell a missing sudoers grant apart
    from a missing/mis-pathed command.
    """
    for cmd in _REQUIRED_SUDO_GRANTS:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "-l", *cmd,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.error(
                "server update: 'sudo -n -l %s' failed (rc=%s): %s",
                " ".join(cmd), proc.returncode,
                stderr.decode(errors="replace").strip() or "(no stderr captured)",
            )
            return " ".join(cmd)
    return None


async def update_palworld(on_progress=None):
    missing_grant = await _missing_sudo_grant()
    if missing_grant is not None:
        log.error("server update: no usable sudo grant for '%s', aborting", missing_grant)
        embed = discord.Embed(title="Update failed", color=COLOR_LEAVE)
        embed.add_field(
            name="Status",
            value=f"No usable sudo grant for `{missing_grant}`. Nothing was touched — the "
                  "grant may be missing, or the command itself may not be installed; check "
                  "the sudoers drop-in and the command's path.",
            inline=False,
        )
        return embed

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
        if on_progress:
            await on_progress("Stopping server…")
        proc = await asyncio.create_subprocess_exec("sudo", "systemctl", "stop", PALWORLD_SERVICE_NAME)
        stop_rc = await proc.wait()
        if stop_rc != 0:
            # Abort rather than continue. Running the update wrapper's
            # steamcmd +app_update validate against a LIVE server is the
            # dangerous half of this flow — until this check existed, a
            # silently-failed stop led straight into it, then polled a
            # server that had never gone down, found it up, and reported
            # success.
            log.error("server update: stop failed with rc=%s, aborting", stop_rc)
            embed = discord.Embed(title="Update failed", color=COLOR_LEAVE)
            embed.add_field(
                name="Status",
                value=f"Could not stop {PALWORLD_SERVICE_NAME} (exit {stop_rc}). Nothing was "
                      f"updated and the server was not restarted — check "
                      f"`systemctl status {PALWORLD_SERVICE_NAME}`.",
                inline=False,
            )
            return embed

        if on_progress:
            await on_progress("Updating via steamcmd… this can take a few minutes")
        steamcmd_ok = False
        steamcmd_output = ""
        try:
            steamcmd_proc = await asyncio.create_subprocess_exec(
                "sudo", SWEE_UPDATE_WRAPPER,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await steamcmd_proc.communicate()
            steamcmd_ok = steamcmd_proc.returncode == 0
            steamcmd_output = stdout.decode(errors="replace").strip()
        except Exception as e:
            log.exception("server update: failed to run the update wrapper")
            steamcmd_output = str(e)

        if on_progress:
            await on_progress("Starting server…")
        start_proc = await asyncio.create_subprocess_exec("sudo", "systemctl", "start", PALWORLD_SERVICE_NAME)
        start_rc = await start_proc.wait()
        if start_rc != 0:
            # Not fatal — the liveness poll below already reports a server
            # that doesn't come back. This just turns that into one
            # identifiable line instead of a 120-second mystery.
            log.error("server update: start failed with rc=%s", start_rc)

        start = time.monotonic()
        timeout = 120
        online = False
        while time.monotonic() - start < timeout:
            try:
                await rest.info()
                online = True
                break
            except Exception:
                await asyncio.sleep(5)
    finally:
        restart_module._bot_restart_in_progress = False

    if not steamcmd_ok:
        embed = discord.Embed(title="Update failed", color=COLOR_LEAVE)
        tail = steamcmd_output[-500:]
        if len(steamcmd_output) > 500:
            tail = "…" + tail
        embed.add_field(name="steamcmd output", value=f"```{tail}```" if tail else "(no output)", inline=False)
        if start_rc == 0:
            embed.add_field(name="Status", value="Server was still restarted with the existing install.", inline=False)
        else:
            embed.add_field(
                name="Status",
                value=f"The restart afterward also failed (exit {start_rc}) — check "
                      f"`systemctl status {PALWORLD_SERVICE_NAME}`.",
                inline=False,
            )
        return embed

    if not online:
        embed = discord.Embed(title="Update timed out", color=COLOR_LEAVE)
        embed.add_field(
            name="Status",
            value=f"steamcmd succeeded but no response after {timeout}s — check `journalctl -u {PALWORLD_SERVICE_NAME}`",
        )
        return embed

    embed = discord.Embed(title="Server updated", color=COLOR_READY)
    embed.add_field(name="Status", value="steamcmd completed and the server is back online.")
    return embed
