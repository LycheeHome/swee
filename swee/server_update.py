import asyncio
import logging
import time

import discord

import swee.restart as restart_module
from swee.config import COLOR_LEAVE, COLOR_READY, PALWORLD_SERVICE_NAME, RAM_RESTART_WARNING_SEC, SWEE_UPDATE_WRAPPER
from swee.rest_client import rest
from swee.restart import warn_and_wait

log = logging.getLogger("swee")


async def update_palworld(on_progress=None):
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
        embed.add_field(name="Status", value="Server was still restarted with the existing install.", inline=False)
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
