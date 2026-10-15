import os
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()

GUILD_ID            = int(os.environ["GUILD_ID"])
RELAY_CHANNEL_ID    = int(os.environ["RELAY_CHANNEL_ID"]) if os.environ.get("RELAY_CHANNEL_ID") else None
PALFEED_SERVICE_URL = os.environ.get("PALFEED_SERVICE_URL") or None
PALFEED_CHANNEL_ID  = int(os.environ["PALFEED_CHANNEL_ID"]) if os.environ.get("PALFEED_CHANNEL_ID") else None
STATS_CHANNEL_ID    = int(os.environ["STATS_CHANNEL_ID"])
ADMIN_ROLE_ID       = int(os.environ["ADMIN_ROLE_ID"])
ADMIN_CHANNEL_ID    = int(os.environ["ADMIN_CHANNEL_ID"])
COMMANDS_CHANNEL_ID = int(os.environ["COMMANDS_CHANNEL_ID"])
BOT_TOKEN           = os.environ["DISCORD_BOT_TOKEN"]

REST_BASE = f"http://{os.environ['REST_HOST']}:{os.environ['REST_PORT']}/v1/api"
REST_AUTH = httpx.BasicAuth(os.environ["REST_USER"], os.environ["REST_PASSWORD"])

ACTIVITY_CHANNEL_ID = int(os.environ["ACTIVITY_CHANNEL_ID"])
ALERTS_CHANNEL_ID   = int(os.environ["ALERTS_CHANNEL_ID"])
BOT_UPDATES_CHANNEL_ID = int(os.environ["BOT_UPDATES_CHANNEL_ID"])
GITHUB_REPO            = os.environ.get("GITHUB_REPO") or None
GITHUB_TOKEN           = os.environ.get("GITHUB_TOKEN")
ANTHROPIC_API_KEY      = os.environ.get("ANTHROPIC_API_KEY")
PALWORLD_SETTINGS_INI_PATH = os.environ["PALWORLD_SETTINGS_INI_PATH"]
PALWORLD_SERVICE_NAME = os.environ.get("PALWORLD_SERVICE_NAME", "palworld")

# The game unit's journal lines, as lychee-ops' root forwarder
# (palworld-events.service) appends them: one `journalctl -o json` object per
# line. swee tails this file rather than running journalctl, which a container
# does not have and which needed swee in `adm`. The default is the
# container's mount point; a host install points it at
# /var/log/palworld-events/palworld.jsonl.
PALWORLD_EVENTS_FILE = os.environ.get("PALWORLD_EVENTS_FILE", "/palworld-events/palworld.jsonl")
# Read by the unplanned-restart cause detector (swee/cause_detection.py).
UNATTENDED_UPGRADES_LOG = os.environ.get(
    "UNATTENDED_UPGRADES_LOG", "/var/log/unattended-upgrades/unattended-upgrades.log"
)
# Rewritten every 30 s once logged in to Discord; the image's HEALTHCHECK
# reads its age (swee/heartbeat.py). Blank disables it.
HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "/state/heartbeat")

# lychee-admin's per-client host-operations socket (A-OPS-1), and the
# inventory unit name swee operates through it. See swee/host_ops.py.
HOST_OPS_SOCKET = os.environ.get("HOST_OPS_SOCKET", "/run/lychee-admin/clients/swee/api.sock")
HOST_OPS_UNIT   = os.environ.get("HOST_OPS_UNIT", "palworld-palchuds")

_ram_restart_threshold_env = os.environ.get("RAM_RESTART_THRESHOLD_PCT")
RAM_RESTART_THRESHOLD_PCT = float(_ram_restart_threshold_env) if _ram_restart_threshold_env else None
RAM_RESTART_COOLDOWN_MIN = float(os.environ.get("RAM_RESTART_COOLDOWN_MIN", "15"))
RAM_RESTART_WARNING_SEC = float(os.environ.get("RAM_RESTART_WARNING_SEC", "60"))

OFFLINE_PLAYERS_LIMIT = int(os.environ.get("OFFLINE_PLAYERS_LIMIT", "10"))

PACIFIC = ZoneInfo("America/Los_Angeles")

COLOR_CHAT, COLOR_JOIN, COLOR_LEAVE = 0x5865F2, 0x57F287, 0xED4245
COLOR_SHUTDOWN, COLOR_READY = 0xFEE75C, 0x57F287

ASSISTANT_LOG_CHANNEL_ID = int(os.environ["ASSISTANT_LOG_CHANNEL_ID"]) if os.environ.get("ASSISTANT_LOG_CHANNEL_ID") else None
ASK_COOLDOWN_SEC = float(os.environ.get("ASK_COOLDOWN_SEC", "30"))
