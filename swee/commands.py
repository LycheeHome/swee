import logging

import discord
import httpx
from discord import app_commands

import swee.restart as restart_module
from swee.bot import bot, in_commands_channel, is_admin
from swee.config import COLOR_CHAT, COLOR_SHUTDOWN, OFFLINE_PLAYERS_LIMIT, RAM_RESTART_WARNING_SEC
from swee.embeds import add_status_fields, format_offline_field, format_online_field, offline_entries_from_history
from swee.player_history import online_players, player_history, refresh_online_players, session_started
from swee.rest_client import rest
from swee.restart import restart_palworld, warn_and_wait
from swee.server_update import update_palworld

log = logging.getLogger("swee")


async def _try_edit(interaction, embed):
    """Best-effort edit of the interaction's original response, for
    in-flight progress updates. A Discord interaction token is only valid
    for 15 minutes; /update's flow (a warning sleep, a world save, a
    systemctl stop, and a full steamcmd validate against a ~9 GB install)
    can plausibly exceed that on a real content update — the first /update
    after months of broken ones is the likely case. If the token has
    expired (edit_original_response raises discord.NotFound) or Discord
    hiccups with a transient 5xx (discord.HTTPException), letting that
    propagate out of a progress callback would abort whatever mutating
    work is in flight at that point (e.g. skip straight past `systemctl
    start`) — the exact failure class this code exists to prevent,
    arriving through a different door. A dropped progress update is
    harmless: the next one, or the final result, will still show up (via
    _deliver_result) whenever the token/connection recovers.
    """
    try:
        await interaction.edit_original_response(embed=embed)
    except discord.HTTPException:
        log.warning("progress update dropped (interaction token likely expired)", exc_info=True)


async def _deliver_result(interaction, embed):
    """Deliver a command's final result embed, falling back to a plain
    channel message if editing the original response fails. Same 15-minute
    token risk as _try_edit, but here it's the final outcome — if we let it
    silently vanish, the admin has no way to know whether /update or
    /restart succeeded, failed, or is still running some other error.
    """
    try:
        await interaction.edit_original_response(embed=embed)
        return
    except discord.HTTPException:
        log.warning("final result edit failed (interaction token likely expired); falling back to channel send", exc_info=True)
    channel = interaction.channel
    if channel is None:
        log.error("no channel available for fallback send; result embed lost")
        return
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        log.exception("fallback channel send also failed; result embed lost")


@bot.tree.command(description="Show server status")
@in_commands_channel()
async def status(interaction: discord.Interaction):
    info, metrics = await rest.info(), await rest.metrics()
    try:
        players_list = (await rest.players()).get("players", [])
        refresh_online_players(players_list)
    except Exception:
        log.exception("player history: failed to fetch players for /status")
        players_list = []
    offline_entries = offline_entries_from_history(player_history, set(online_players.values()))
    embed = discord.Embed(title=info["servername"], color=COLOR_CHAT)
    add_status_fields(embed, info, metrics, players_list, offline_entries)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(description="List online and offline players")
@in_commands_channel()
async def players(interaction: discord.Interaction):
    plist = (await rest.players()).get("players", [])
    refresh_online_players(plist)
    offline_entries = offline_entries_from_history(player_history, set(online_players.values()))
    embed = discord.Embed(title="Players", color=COLOR_CHAT)
    embed.add_field(name="Online", value=format_online_field(plist, session_started), inline=False)
    embed.add_field(name="Offline", value=format_offline_field(offline_entries, OFFLINE_PLAYERS_LIMIT), inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(description="Force-save the world")
@is_admin()
async def save(interaction: discord.Interaction):
    await rest.save()
    await interaction.response.send_message("World saved.")


@bot.tree.command(description="Kick a player by SteamID")
@is_admin()
async def kick(interaction: discord.Interaction, steamid: str, reason: str = ""):
    await rest.kick(steamid, reason)
    await interaction.response.send_message(f"Kicked `{steamid}`.")


@bot.tree.command(description="Ban a player by SteamID")
@is_admin()
async def ban(interaction: discord.Interaction, steamid: str, reason: str = ""):
    await rest.ban(steamid, reason)
    await interaction.response.send_message(f"Banned `{steamid}`.")


@bot.tree.command(description="Send an in-game announcement")
@is_admin()
async def broadcast(interaction: discord.Interaction, message: str):
    await rest.announce(message)
    await interaction.response.send_message("Sent.")


@bot.tree.command(description="Restart the Palworld service")
@is_admin()
async def restart(interaction: discord.Interaction):
    warning_sec = int(RAM_RESTART_WARNING_SEC)
    embed = discord.Embed(
        title="Restarting Palworld server",
        color=COLOR_SHUTDOWN,
    )
    embed.add_field(name="Status", value="Broadcasting restart warning…")
    await interaction.response.send_message(embed=embed)

    await warn_and_wait(
        "Restarting server",
        f"Restarting server in {warning_sec}s (requested by admin).",
        f"Server restarting in {warning_sec}s",
    )

    embed.set_field_at(0, name="Status", value="Sending restart command…")
    await _try_edit(interaction, embed)

    async def on_progress(status):
        embed.set_field_at(0, name="Status", value=status)
        await _try_edit(interaction, embed)

    restart_module._bot_restart_in_progress = True
    try:
        result_embed = await restart_palworld(on_progress)
    finally:
        restart_module._bot_restart_in_progress = False
    await _deliver_result(interaction, result_embed)


@bot.tree.command(description="Update the Palworld server via steamcmd")
@is_admin()
async def update(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Updating Palworld server",
        color=COLOR_SHUTDOWN,
    )
    embed.add_field(name="Status", value="Broadcasting update warning…")
    await interaction.response.send_message(embed=embed)

    async def on_progress(status):
        embed.set_field_at(0, name="Status", value=status)
        await _try_edit(interaction, embed)

    result_embed = await update_palworld(on_progress)
    await _deliver_result(interaction, result_embed)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return  # predicate (is_admin/in_commands_channel) already sent its own response

    command_name = interaction.command.name if interaction.command else "?"
    if isinstance(getattr(error, "original", error), httpx.ConnectError):
        log.warning("command error in /%s: Palworld REST API unreachable", command_name)
    else:
        log.exception("command error in /%s", command_name, exc_info=error)

    message = "Something went wrong talking to the server."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
