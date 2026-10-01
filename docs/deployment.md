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
passwordless-sudo rule `/restart` needs, and installs `swee.service`. It sets up a host; it does
not wire up continuous deployment, and there is no longer anything in this repo that does.
`/update`'s three additional grants (`systemctl stop`/`start` plus the host-side update wrapper)
are not part of that install — the wrapper in particular is `lychee-ops`' artifact — so see the
README's "Running" section for what a host needs beyond this script before `/update` can do
anything.

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
