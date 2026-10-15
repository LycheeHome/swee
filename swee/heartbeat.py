"""A liveness file for the container's HEALTHCHECK.

main.py starts heartbeat_loop() from on_ready, so the file first appears once
swee has logged in to Discord; after that it is rewritten every
HEARTBEAT_INTERVAL_S. The Dockerfile's HEALTHCHECK calls the container
unhealthy when the file is older than 90 s: three missed beats, which means
the event loop has stopped turning (or swee never logged in at all).

What it does not prove: that Discord is still connected. discord.py
reconnects on its own and on_ready refires; a heartbeat that kept beating
through a long disconnect is the price of not inventing a second health model.
"""
import asyncio
import logging
import time

log = logging.getLogger("swee")

HEARTBEAT_INTERVAL_S = 30


def write_heartbeat(path):
    # The HEALTHCHECK reads only the mtime; the timestamp inside is for a human.
    with open(path, "w") as f:
        f.write(f"{int(time.time())}\n")


async def heartbeat_loop(path, interval_s=HEARTBEAT_INTERVAL_S):
    failing = False
    while True:
        try:
            write_heartbeat(path)
            if failing:
                log.info("heartbeat: writing %s again", path)
            failing = False
        except OSError as error:
            # Never fatal: a host install with no /state must still run. Logged
            # once per run of failures, not every 30 s.
            if not failing:
                log.warning("heartbeat: cannot write %s (%s); set HEARTBEAT_FILE, or blank it to disable", path, error)
            failing = True
        await asyncio.sleep(interval_s)
