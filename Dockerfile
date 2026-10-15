# swee as a container, run by lychee-ops' reconciler from a declaration in
# lychee-resources (read-only root filesystem, host networking, state volume
# at /state). Published as ghcr.io/lycheehome/swee:X.Y.Z by ci.yml's
# publish-image job.
FROM python:3.14-slim

LABEL org.opencontainers.image.source="https://github.com/LycheeHome/swee"

# PYTHONDONTWRITEBYTECODE: the root filesystem is read-only at runtime, so
# there is nowhere to cache bytecode; the app's own is compiled below instead.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY swee ./swee
RUN python -m compileall -q /app

# Matches `id swee` on the host (uid 993, gid 980), so the state carried over
# from /var/lib/swee and the files swee writes through its mounts keep their
# owner. The host's supplementary groups (palworld-events, palworld, swee-api)
# are supplied at runtime by compose's group_add, not baked in.
# No home directory and no login shell: nothing here needs either.
RUN groupadd --gid 980 swee \
    && useradd --uid 993 --gid 980 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin swee

# State lives outside /app: player_history.json, session_state.json,
# last_release.json, palfeed_state.json and last_palworld_settings.json are all
# opened by relative path, so the working directory IS the state directory, as
# WorkingDirectory=/var/lib/swee was on the host.
# /state must exist in the image and be owned by 993:980: Docker initializes a
# fresh named volume from the image's content and ownership at the mount point
# only if that path exists; otherwise the volume is created root-owned and
# every state write fails.
RUN mkdir -p /state && chown 993:980 /state
USER 993:980
WORKDIR /state

# Healthy means "logged in to Discord, and the event loop still turning":
# swee/heartbeat.py rewrites the file every 30 s, starting only from on_ready,
# so a stale or missing file is unhealthy. 90 s is three missed beats.
# python, not curl: curl is not in python-slim. The start period covers the
# Discord login and the slash-command sync that precede on_ready; failures
# inside it do not count, and the first success ends it.
HEALTHCHECK --interval=30s --start-period=120s --start-interval=5s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import os, sys, time; p = os.environ.get('HEARTBEAT_FILE') or '/state/heartbeat'; sys.exit(0 if time.time() - os.stat(p).st_mtime < 90 else 1)"]

# The absolute path is what makes the imports work from /state: Python puts
# the script's own directory (/app) first on sys.path, so `import swee`
# resolves there whatever the working directory is. No PYTHONPATH, and no
# chdir in code, which would have moved the relative state paths with it.
ENTRYPOINT ["python", "/app/main.py"]
