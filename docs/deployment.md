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
a private Ansible repo that runs `ansible-pull` on the host every five minutes. Each tick it:

1. reads the release tag pinned in its own `group_vars` as `swee_version`;
2. checks this repository's Actions API for a **completed workflow run containing a job named
   `test` that concluded `success`** for that tag's commit;
3. only if that passes, checks out the tag, reinstalls dependencies, restarts `swee.service`,
   and waits for the bot to reach Discord before calling it deployed.

### What this changes for you

**Merging the Release PR no longer deploys anything.** It cuts the tag and publishes the GitHub
Release, which is all. Deploying that release is a separate, deliberate act: someone bumps
`swee_version` in `lychee-ops` and merges it. That is the rollback lever too — pointing the pin
at an older tag reverts the bot within one tick, with no access to the host required.

**Do not rename the `test` job.** The gate matches a *job* named exactly `test` in
`.github/workflows/ci.yml`. Renaming the job key, or adding a `name:` override (which replaces
the name the API reports), breaks nothing loudly — it silently stops every future deploy, and
the status file on the host then reports the tag as having no `test` job, which reads like CI
never ran rather than like a rename.

**A release cut before the `test` job existed can never be deployed.** The gate requires a green
`test` for the pinned tag's commit, and that job was added in September 2026, so older releases
block permanently rather than eventually succeeding. This is accepted behaviour: old versions
age out of rollback range.

### Deploying this bot somewhere else

`deploy/setup.sh` remains the standalone install path and is unaffected by any of the above — it
creates the venv, installs dependencies, checks the Palworld service exists, installs the
passwordless-sudo rule the bot needs to restart it, and installs `swee.service`. It sets up a
host; it does not wire up continuous deployment, and there is no longer anything in this repo
that does.

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

**No release exists until you merge that Release PR.** Merging it tags the release and publishes
the GitHub Release — and stops there. Ordinary feature PRs merging to `main` only update the
Release PR's diff. Neither deploys anything; see Deployment above for what does.

Reserve `!`/`BREAKING CHANGE:` for changes that break an existing deployment on upgrade — e.g. a
new required `.env` var, a removed/renamed slash command, a changed REST config shape.
