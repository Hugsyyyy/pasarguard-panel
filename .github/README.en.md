# Pengu panel: a PasarGuard fork

**English** · [فارسی](README.md)

<p align="center">
  <a href="https://github.com/Hugsyyyy/pasarguard-panel/releases/latest"><img alt="Release" src="https://img.shields.io/github/v/release/Hugsyyyy/pasarguard-panel?label=release"></a>
  <a href="https://github.com/Hugsyyyy/pasarguard-panel/actions/workflows/pengu-ci.yml"><img alt="Pengu CI" src="https://github.com/Hugsyyyy/pasarguard-panel/actions/workflows/pengu-ci.yml/badge.svg?branch=pengu"></a>
  <a href="https://github.com/Hugsyyyy/pasarguard-panel/actions/workflows/test-database-migrations.yml"><img alt="Tests on all databases" src="https://github.com/Hugsyyyy/pasarguard-panel/actions/workflows/test-database-migrations.yml/badge.svg?branch=pengu"></a>
  <a href="../LICENSE"><img alt="License" src="https://img.shields.io/github/license/Hugsyyyy/pasarguard-panel"></a>
  <a href="https://github.com/PasarGuard/panel"><img alt="Upstream" src="https://img.shields.io/badge/upstream-PasarGuard%2Fpanel-blue"></a>
</p>

## What this is

This repository is a **fork** of the [PasarGuard](https://github.com/PasarGuard/panel) panel, maintained for the Pengu service. The panel itself is built by the PasarGuard team. We take every official release, add a few features of our own on top, and test everything again.

- **Everything in the official release is here too:** the same panel, API, databases and nodes.
- **Every official release and bug fix** is merged in automatically and published once the tests pass.
- **Our changes are small and kept apart**, so merging official releases stays clean.

## What it adds

| | Official | This fork |
|---|:---:|:---:|
| All official features and bug fixes | ✓ | ✓ |
| My Hosts: server names, order and visibility for each reseller's users | ✗ | ✓ |
| My Hosts switched on or off per role | ✗ | ✓ |
| The owner can view and clear any reseller's My Hosts settings | ✗ | ✓ |
| Repeated bot logins without re-checking the password, at far less CPU | ✗ | ✓ |
| Dashboard version section that follows this repository's releases | — | ✓ |
| Updates with `pasarguard update` from this repository | — | ✓ |

## My Hosts, for resellers

Resellers get a **My Hosts** page in the dashboard sidebar. Whatever they change there applies **only to their own users' subscriptions**. The panel's shared hosts and other resellers' users are not touched.

**What a reseller can do:**

- **Rename any server**, with the same variables as host names; for example `🇩🇪 Germany {USERNAME}`, `{STATUS_EMOJI} {DATA_LEFT}` or `{DAYS_LEFT} days`. Other variables: `{DATA_USAGE}`, `{DATA_LIMIT}`, `{TIME_LEFT}`, `{EXPIRE_DATE}` and `{JALALI_EXPIRE_DATE}`.
- **Reorder** by drag and drop, or with the up and down buttons on mobile.
- **Hide servers** they do not want their users to get; each server's switch is labelled "Visible" or "Hidden".
- **Reset** one server's name, the order, or everything with one button.

**Who gets the page:**

- Admins whose role can neither create nor edit hosts and has "Use My Hosts" switched on.
- The switch is on the "Admin Roles" page, in "Edit role", under "Features". It is on for roles that existed before it.
- If the owner switches it off for a role, the page disappears for that role's admins and their changes stop reaching their users. The changes are kept and come back when the switch is turned on again.
- The panel owner and admins who edit the shared hosts do not get the page; it has no meaning for them.

**Rules:**

- Each reseller sees only the servers of **the groups their role allows**.
- Until a reseller changes the order, their users get **the panel's order**. Random ordering, if enabled, stays, and the panel manager's later changes reach them.
- Once a reseller sets their own order, **servers added to the panel later go to the end** of their list and show up on their page so they can move them.
- A server the panel manager disabled is shown to no reseller's users.
- Names follow the same limits as shared host names; invalid input is refused.

**Security:**

- API keys with custom permissions, even the owner's own key, cannot use this feature.
- A limited admin cannot save changes.
- The owner can view and clear any reseller's settings (see "My Hosts API" below).

## Repeated logins without the CPU cost

Some bots and apps log in to the panel before every request. Each login is one bcrypt password check, about half a second of CPU. One reseller's bot, at about 250 logins a minute, kept two of our server's four cores busy with that alone.

In this fork a password that has just been checked and accepted is remembered for **5 minutes**, and logging in again with the same password no longer costs that CPU. Nothing changes for bots or resellers, and they do not need to do anything.

**What is kept:**

- **A wrong password is never remembered** and always gets the full check. Guessing a password over the network is no cheaper than before.
- **A changed password stops the old one at once.**
- **A disabled or deleted admin** is refused as before, even if their login was remembered.
- **The password itself is not stored.** Only a cryptographic proof stays in the panel's memory; it is dropped after the window and is gone on restart.

**What to know:**

- While a login is remembered, someone who can read the panel process's memory can test password guesses about 50 times cheaper than bcrypt. That reader already holds the token signing key and the database password.
- `PENGU_LOGIN_CACHE_SECONDS` in `.env` sets the window (default 300 seconds, at most 3600). `0` switches the feature off. Restart the panel after changing it.
- Technical details are in [PENGU.md](../PENGU.md).

## Install

This fork only replaces the panel image in the official PasarGuard install. The database, nodes and other services stay the same.

**New panel:**

1. Install the panel with the [official PasarGuard install script](https://github.com/PasarGuard/scripts).
2. Follow the steps for an existing panel below.

**Existing panel** (running the official release):

1. Take a backup:

   ```sh
   pasarguard backup
   ```

2. In `/opt/pasarguard/docker-compose.yml`, in the `pasarguard` service, change the image line:

   ```yaml
   image: ghcr.io/hugsyyyy/pasarguard-panel:pengu-latest
   ```

3. Start only the panel with the new image. The database and other services are not touched:

   ```sh
   cd /opt/pasarguard
   docker compose pull pasarguard
   docker compose up -d --no-deps pasarguard
   ```

   Database migrations run automatically on startup. The dashboard then shows a version like `5.6.0-pengu.4`.

> Images are built for `amd64` (x86_64) servers only for now.

## Updating

After installing, **`pasarguard update` updates from this repository**: it pulls this fork's latest release (`pengu-latest`) and restarts the panel. The dashboard also tells you when a new release is out.

- `pasarguard update` pulls every image in `docker-compose.yml`, the database included, and restarts all services, just like the official release.
- To update only the panel, the two commands of install step 3 are enough.
- `pasarguard install` (a fresh install) sets the image back to the official one. Change the image line again afterwards.

## Going back to the official release

Going back is possible and was tested on a copy of a real database. Only the My Hosts settings are lost.

1. Stop the panel:

   ```sh
   cd /opt/pasarguard
   docker compose stop pasarguard
   ```

2. With this fork's image, undo the fork's database changes. The last official migration id is in [PENGU.md](../PENGU.md); for `v5.6.0` it is:

   ```sh
   docker compose run --rm --no-deps --entrypoint python pasarguard -m alembic downgrade 1a2b3c4d5e6f
   ```

3. Set the image line back to the matching official release (for example `pasarguard/panel:v5.6.0`) and start the panel:

   ```sh
   docker compose up -d --no-deps pasarguard
   ```

## Versions and images

- Every release is tagged **`vX.Y.Z-pengu.N`**: the official `vX.Y.Z` plus our changes. `N` goes up for further releases on the same official version.
- Each version has a [release page](https://github.com/Hugsyyyy/pasarguard-panel/releases) with patch notes.

Image tags:

| Tag | Use |
|---|---|
| `ghcr.io/hugsyyyy/pasarguard-panel:pengu-latest` | Always the latest release; used by `pasarguard update` |
| `ghcr.io/hugsyyyy/pasarguard-panel:vX.Y.Z-pengu.N` | One exact version, to pin the panel |

## Keeping up with the official release

```mermaid
flowchart LR
    A["New official release"] --> B["Merged automatically on a separate branch"]
    B --> C["Tests on five databases"]
    C --> D["Review and merge"]
    D --> E["Pengu version tag"]
    E --> F["Image and release page built"]
    F --> G["Update on the panel"]
```

- A workflow checks the latest official release every day.
- When there is a new one, it merges it into a `sync/...` branch and opens a pull request. All official tests and ours run on it.
- If the merge conflicts, an issue is opened so it can be merged by hand.
- Nothing is installed on any panel automatically: a release is built only after the tests pass and it is approved.

## My Hosts API

The whole official PasarGuard API is unchanged. These routes are added:

| Route | Who | What |
|---|---|---|
| `GET /api/hosts/preferences` | Reseller | The servers as their users get them, in their own order |
| `PUT /api/hosts/preferences` | Reseller | Save names, order and visibility; send every server on the page exactly once |
| `DELETE /api/hosts/preferences` | Reseller | Full reset |
| `GET /api/admin/by-username/{username}/host-preferences` | Owner | See one admin's changes |
| `DELETE /api/admin/by-username/{username}/host-preferences` | Owner | Clear one admin's settings |

Whether a role has My Hosts is its `features.can_use_my_hosts`, changed through the official `/api/admin-role` routes. It is on by default.

Example `PUT` body: server 7 renamed, server 9 hidden, and the panel's order kept (`priority: null` for all):

```json
[
  {"id": 7, "remark": "🇩🇪 Germany {USERNAME}", "priority": null, "enabled": true},
  {"id": 9, "remark": null, "priority": null, "enabled": false},
  {"id": 12, "remark": null, "priority": null, "enabled": true}
]
```

For a custom order, give every server its position in `priority` (0, 1, 2, ...).

Error responses:

- The panel's server list changed in the meantime: `409`.
- Invalid name: `422`.
- Not allowed, or My Hosts switched off for the role: `403`.

## Compatibility and tests

- **The API, databases and nodes** are the same as the official release. SQLite, PostgreSQL, TimescaleDB, MySQL and MariaDB are supported.
- **Bots and tools that use the official API** keep working unchanged. The routes our own bots call were compared one by one.
- **Users whose reseller has not used My Hosts** get exactly the official subscription. This was compared on a copy of real data, in every subscription format.
- **Before every release:**
  - all official PasarGuard tests plus ours run on all five databases;
  - lint and the dashboard build are checked;
  - the `pengu` branch is protected and changes only through pull requests with passing tests.

## FAQ

**Do new official features arrive too?**

Yes. Every official release is merged and tested automatically, then published as `vX.Y.Z-pengu.N`.

**Will my bot or app work with this version?**

If it uses the official PasarGuard API, yes. Nothing in the official API was removed or changed.

**My bot logs in before every request. Is that a problem?**

It works, and in this fork it no longer loads the server much (see "Repeated logins without the CPU cost"). It is still better to keep the token and log in again only after it expires; a panel token lasts 24 hours by default.

**How do I turn My Hosts off for some resellers?**

Edit their role on the "Admin Roles" page and switch off "Use My Hosts" under "Features". Their changes are kept and come back if you switch it on again.

**What happens if we go back to the official release?**

Everything stays except the My Hosts settings. Resellers' users get the panel's shared hosts again. See "Going back to the official release".

**Can a reseller change other resellers' users or the shared hosts?**

No. Their settings only affect their own users' subscriptions, and the shared hosts are never changed.

**Why is this not in the official release?**

The PasarGuard team did not accept the pull request, so it is maintained in a separate fork with more tests.

## Development

- The main branch is `pengu`. New work goes on a `feat/...` or `fix/...` branch and comes in through a pull request. `main` is the official branch and is left untouched.
- Tests run as in the original project: `make setup-test`, then `make test`.
- The database migration rule, the upstream sync process and other technical details are in [PENGU.md](../PENGU.md) (in Persian).
- The panel's own documentation: the [official PasarGuard docs](https://docs.pasarguard.org) and the [project's original README](../README.md).

## License and credits

- Like the original project, this fork is licensed under [AGPL-3.0](../LICENSE), and its full source is public here.
- All credit for the panel goes to the [PasarGuard team](https://github.com/PasarGuard) and its contributors.
- My Hosts is based on pull request [#1185](https://github.com/PasarGuard/panel/pull/1185) by [AsanFillter](https://github.com/AsanFillter). We reviewed it, fixed its problems and completed its tests.
