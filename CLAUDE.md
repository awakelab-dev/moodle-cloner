# Working notes for Claude

Project-specific guidance that overrides defaults. Read before making changes.

## Versioning is mandatory

Every code iteration MUST bump the version in `VERSION` and add a matching entry at the top of `CHANGELOG.md`. This is non-negotiable — `CHANGELOG.md`'s intro line states the rule, and missing it has happened before.

Apply this even for small refinements. If unsure whether a change qualifies:

- **Patch bump (`v0.X.Y` → `v0.X.Y+1`)**: bugfixes, copy tweaks, dependency bumps, internal refactors with no behavior change.
- **Minor bump (`v0.X.Y` → `v0.X+1.0`)**: any user-visible new feature, new endpoint, new UI surface, new permission, schema change, deploy-step change (new env var / new file to upload / new system package), or anything that changes operational behavior.
- **Major bump (`v0.X.Y` → `v1.0.0`)**: incompatible API change once the project is past pre-1.0.

`CHANGELOG.md` entries follow the existing format: `## vX.Y.Z - YYYY-MM-DD` header, a one-line summary, then a bulleted list of concrete changes. Mention any deploy steps the user needs to take (new env vars, files to copy outside git, dependencies to install, schema migrations). Use absolute dates, not "today" / "yesterday".

The order to land a change:
1. Code edits.
2. Bump `VERSION`.
3. Add the `CHANGELOG.md` entry.
4. Run syntax/smoke checks.
5. Commit (the version bump and changelog entry go in the same commit as the code).

## Stack quirks

- Pure stdlib `http.server` (no Flask/FastAPI). All routes dispatch through `MoodleCloneHandler` in `app.py`. New routes go in `_dispatch` (GETs/HEADs) or in the relevant `do_POST` / `do_PUT` / `do_PATCH` / `do_DELETE`. Auth helpers: `_require_auth`, `_require_permission(flag)`, `_require_superadmin`.
- Database is Aurora MySQL via PyMySQL. The app reuses `TARGET_DB_HOST` (the Moodle clone target cluster) and stores its own metadata in a separate database, default `moodle_cloner_app`. `db.py` bootstraps the schema on every boot — schema changes go in `SCHEMA` there.
- Frontend is a single `index.html` served as-is (with `{{TARGET_DB_HOST}}` and `{{APP_VERSION}}` string-replaced). Tailwind CDN, no build step. All JS is inline; new features get an IIFE with its own `window.__xLoad` lazy entry point.
- SSH out to Moodle targets is via paramiko, called from `course_copier.py` and `plugin_routes.py`. `inventario.json` holds plaintext SSH/sudo passwords and is gitignored — `inventario.example.json` is the committed template.

## What's gitignored on purpose

- `.env` — has Aurora admin credentials and the initial-admin password.
- `inventario.json` — has plaintext SSH and sudo passwords for the Moodle targets.
- `alexia_config.json` — has SSH/sudo passwords for the Catalejo and Alexia servers.

Don't ask to commit either. If the server is missing them after `git pull`, the user uploads them manually (SCP / heredoc paste). This is the standard pattern, same as how `.env` is handled.

## Deploy

The server is at `13.38.161.213`, managed via pm2 as `moodle-cloner-api`. The user's deploy loop is:

```
cd /projects/moodle-cloner && git pull && pm2 restart moodle-cloner-api
pm2 logs moodle-cloner-api --lines 20 --nostream
```

If a change adds a Python dependency, the user installs it via `sudo apt-get install -y python3-<pkg>` first (Ubuntu 24.04's pip is externally-managed; the apt route is what works). Mention this in the CHANGELOG entry and the deploy reply.

## Permissions cheat sheet

- `is_superadmin`: implicit access to everything, including the Plataformas admin and user-management. Last superadmin cannot be demoted or deleted.
- `can_manage_users`: list/create users, change their passwords. Cannot grant permissions or edit superadmins (only superadmins can).
- `can_access_moodle_cloner` / `can_access_course_cloner` / `can_access_plugin_cloner` / `can_access_alexia_cloner`: tab access. Tabs are visible to everyone; clicking one you don't have permission for shows a toaster.

The plugin cloner section (`plugin_routes.py`, IIFE `__pluginLoad`) ports `moodle_plugin_installer.py` — file upload, type auto-detection, multi-server SSH install with job polling.

The Alexia cloner section (`alexia_routes.py`, IIFE `__alexiaLoad`) ports `alexia-exportar-curso` — Catalejo→Alexia course migration via SSH/SFTP, with batch Excel import and individual export. Config in `alexia_config.json` (gitignored, same pattern as `inventario.json`).
