# Deployment details

Deep-dive reference for `swee`'s CI/CD and release process. See the [README](../README.md#deployment)
for the quick-start version.

## Deployment

**This repository does not deploy itself.** `.github/workflows/ci.yml` runs tests and
release-please, both on GitHub-hosted runners, and stops there. Nothing in CI touches the
host — deliberately: a self-hosted runner reachable from a public repository is a remote code
execution path onto the machine it runs on, because a fork supplies its own workflow file and
therefore its own `runs-on:`.

Deployment to the live bot is done by [`lychee-ops`](https://github.com/LycheeHome/lychee-ops),
a private Ansible repo whose reconciler runs on the host, and swee is a **container** there, run
from a declaration in `lychee-resources` (`swee.yml`) exactly as palsave-api is.

### The image

Every release publishes `ghcr.io/lycheehome/swee:X.Y.Z` — the version without its `v`, the only
tag form the reconciler recognises, and never `latest`. The `publish-image` job does it, chained
off release-please's `release_created` output (a release made with `GITHUB_TOKEN` triggers no
other workflow, so a `release:` event would never fire) and gated on both `test` and `image`, so a
commit whose tests or smoke run failed publishes no image. The image carries
`org.opencontainers.image.source=https://github.com/LycheeHome/swee`, so the package inherits this
repository's access.

The `image` job builds the image on every pull request and smoke-runs it the way the reconciler
runs it: read-only root, every capability dropped, `no-new-privileges`, uid `993:980`, a fresh
named volume at `/state` and **no** writable `/tmp`. With a dummy token the bot must reach
Discord's login and fail there and only there; a write outside `/state` fails the job. It is
deliberately not named `test` (see below): it gates the image, not the deploy gate.

What the container expects of its host, all declared by lychee-ops:

- **`/state`** — a volume, and the working directory. The five state files
  (`player_history.json`, `session_state.json`, `last_release.json`, `palfeed_state.json`,
  `last_palworld_settings.json`) keep their relative paths, so they land there. The image creates
  `/state` owned by `993:980` so a fresh volume starts with that owner.
- **`PALWORLD_EVENTS_FILE`** (default `/palworld-events/palworld.jsonl`) — the game unit's journal,
  as lychee-ops' root forwarder `palworld-events.service` appends it (`journalctl -o json`, one
  object per line, rotated daily with `copytruncate`). swee follows it from its end, so a restart
  never replays old joins, and reopens it when it shrinks or is replaced. swee needs no `adm` to
  read it, only the `palworld-events` group.
- **`UNATTENDED_UPGRADES_LOG`** (default `/var/log/unattended-upgrades/unattended-upgrades.log`)
  and **`PALWORLD_SETTINGS_INI_PATH`**, mounted read-only and read-write respectively.
- **`HOST_OPS_SOCKET`** — lychee-admin's host-operations socket, the only way `/restart`,
  `/update` and the RAM auto-restart reach the game unit. There is no `systemctl` in the image and
  no startup check of the unit: a missing unit surfaces through that socket.
- **The host network** (`network: host`), because the Palworld REST API and palsave-api answer on
  the host's loopback.
- **Secrets** in `/etc/lychee-secrets/swee.env` (`0600 root:root`); the reconciler refuses to start
  the container without it.

**Health.** Once logged in to Discord, swee rewrites `HEARTBEAT_FILE` (default
`/state/heartbeat`) every 30 s, and the image's `HEALTHCHECK` calls the container unhealthy when
that file is older than 90 s (or missing after a 120 s start period). That verdict is what
lychee-admin's services board shows.

### What this changes for you

**Merging the Release PR no longer deploys anything.** It cuts the tag, publishes the GitHub
Release and pushes the image, which is all. Deploying that release is a separate, deliberate act:
**Deploy** of the offered tag on lychee-admin's services board, which writes the tag into
`swee.yml` in `lychee-resources`; the reconciler pulls and starts it on its next run. Rolling back
is deploying an older tag the same way.

**Do not rename the `test` job.** lychee-ops' gate matches a *job* named exactly `test` in
`.github/workflows/ci.yml`. Renaming the job key, or adding a `name:` override (which replaces
the name the API reports), breaks nothing loudly — it silently stops every future deploy, and
the status file on the host then reports the tag as having no `test` job, which reads like CI
never ran rather than like a rename.

**Releases before 2.13.0 have no image** and cannot be deployed as a container. Until the cutover
in lychee-ops, swee also still runs as a host service pinned by `swee_version`; that install must
set `PALWORLD_EVENTS_FILE=/var/log/palworld-events/palworld.jsonl` in `/opt/swee/.env`, since
from 2.13.0 swee reads the forwarder's file instead of running `journalctl`.

### Deploying this bot somewhere else

`deploy/setup.sh` remains the standalone, non-container install path — it
creates the venv, installs dependencies, checks the Palworld service exists, and installs
`swee.service`. It sets up a host; it does not wire up continuous deployment, and there is no
longer anything in this repo that does. It installs no sudo grant, because swee needs none:
`/restart`, `/update` and the RAM auto-restart reach the game server's unit through lychee-admin's
host-operations socket (`HOST_OPS_SOCKET`), which requires the bot's user to be in the `swee-api`
group. On `lychee`, `lychee-ops` declares that group membership and the socket's grant. A host install also
needs the events file: lychee-ops' forwarder, or any root process appending `journalctl -u <unit> -f
-n 0 -o json --output-fields=MESSAGE,__REALTIME_TIMESTAMP` to a file the bot's user can read, named by
`PALWORLD_EVENTS_FILE`.

## Versioning

Releases are managed by [`release-please`](https://github.com/googleapis/release-please) via
the `release-please` job in `.github/workflows/ci.yml`, using Conventional Commits
(`feat: ...`, `fix: ...`, `chore: ...`, etc.) parsed from commits on `main`.

On every push to `main`, release-please updates a standing **Release PR** (title like
`chore(main): release X.Y.Z`) with the accumulated version bump and `CHANGELOG.md` entries:
`feat` bumps MINOR, `fix`/`perf` bump PATCH, and a `!` after the type/scope (or a
`BREAKING CHANGE:` footer starting its own line in the commit body) bumps MAJOR. Any other type
(`docs`, `chore`, `ci`, `style`, `test`, `refactor`, `build`, `revert`) doesn't contribute a
version bump, though it may still appear in the changelog depending on release-please's default
section mapping.

**No release exists until you merge that Release PR.** Merging it tags the release, publishes
the GitHub Release and pushes the image — and stops there. Ordinary feature PRs merging to `main` only update the
Release PR's diff. Neither deploys anything; see Deployment above for what does.

### Cutting a release when nothing releasable has landed

Only `feat`, `fix` and `perf` (and `!`/`BREAKING CHANGE:`) produce a version bump. A run of
`ci:`, `docs:`, `chore:`, `style:`, `test:`, `refactor:`, `build:` or `revert:` commits
accumulates on `main` without release-please opening a Release PR at all — there is nothing for
it to propose.

That matters more than it used to. Deployment installs a **release tag**, so a repository with
no new release has nothing new to deploy, however many commits have landed. A CI-only fix that
needs to reach the host will sit on `main` indefinitely unless a release is cut for it.

To force one, add a `Release-As:` footer:

```
Release-As: 2.11.3
```

**Put it in the pull request body.** This repository squash-merges with the PR body as the
commit message (`squash_merge_commit_message: PR_BODY`), so the footer lands in the commit on
`main`, which is where release-please looks. Keep it as the last line.

If that repository setting is ever changed to `COMMIT_MESSAGES`, the PR body stops reaching the
commit and the footer is silently dropped — no release, no error. Put it in the branch's own
commit message in that case. release-please reads commits, not pull requests; the PR body only
works here because of how this repo happens to squash.

Reserve `!`/`BREAKING CHANGE:` for changes that break an existing deployment on upgrade — e.g. a
new required `.env` var, a removed/renamed slash command, a changed REST config shape.
