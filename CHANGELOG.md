# Changelog

All notable changes to the Moodle Cloner web app are tracked here.
Every code iteration must bump the version in `VERSION` and add an entry below.

Format: `YYYY-MM-DD - vX.Y.Z - Short description` followed by a bulleted list.

## v0.6.1 - 2026-06-01

Cosmetic: header alignment.

- Vertically centered the header logo, the "Plataforma de clonado" label, and the version badge by flattening the leftover nested wrapper into a single `flex items-center` row. No behavior change.

## v0.6.0 - 2026-06-01

Branding: header logo.

- Replaced the header "A" badge and "Awake Lab" text with the horizontal AWAKELAB logo (`RESIZED_logo_fondooscuro_horizontal.png`), served from the new public route `GET /assets/logo.png`. The "Plataforma de clonado" subtitle and version chip are unchanged.
- Deploy note: the logo PNG must be present in the project root on the server. It is committed to the repo, so a normal `git pull` ships it — no manual upload needed.

## v0.5.1 - 2026-05-29

Cosmetic tweaks.

- Page title changed from "Awake Lab - Plataforma de Clonado" to "Clonador Awakelab".
- Added an inline SVG favicon (indigo rounded square with a white "A", matching the header badge). Encoded as a `data:image/svg+xml` URI so it ships in `index.html` without needing a static-file route.

## v0.5.0 - 2026-05-29

User-management UI and authorization refinements.

- `PATCH /api/users/:id` now allows self-edit: any logged-in user can change their own password without needing `can_manage_users`. Self-edits and edits performed by a non-superadmin user-manager are restricted to the password field — attempts to change `permissions` or `is_superadmin` are rejected with 403.
- Non-superadmin actors can no longer touch a superadmin's record (including its password). Last-superadmin protection is unchanged.
- `POST /api/users` creates accounts with zero permissions when the actor is not a superadmin, regardless of what permissions the request payload contains. A superadmin must grant permissions afterwards.
- New "Mi cuenta" header button visible to every logged-in user, opening a small modal to change their own password (with confirmation field). Wraps `PATCH /api/users/<own-id>` with `{password}`.
- User-management modal now hides the permissions block entirely for non-superadmin viewers, and the users list hides the Edit/Eliminar buttons on superadmin rows for non-superadmin actors.
- Reusable password show/hide eye-icon toggle is decorated onto every `<input type=password>` at boot and after each modal opens. Covers login, user modal, account modal, platforms admin form, and the Moodle clone form's DB password.
- Toaster backgrounds switched from `bg-*/10` (translucent, blurring into content) to solid `bg-*-950` with a colored border and `shadow-xl`.

## v0.4.0 - 2026-05-28

Course cloner port from `awk-copia-de-cursos-` (Phase 1 backend + Phase 2 UI + UI tweaks).

- New module `course_copier.py` (verbatim from the source project): paramiko-based SSH/SFTP that backs up courses on a source Moodle and restores them in parallel to one or more destination Moodles, plus category get/create.
- New module `course_routes.py`: stdlib HTTP wrappers around `course_copier` (validation, JSON (de)serialization, `CourseRouteError` → HTTP status mapping). Public-shaped vs admin-shaped server views, identical semantics to the original FastAPI service.
- `app.py`: eight new routes under `/api/cc/*`, all auth-gated. User-facing reads and the copy itself require `can_access_course_cloner`; platform admin CRUD requires `is_superadmin`. `POST /api/cc/copy-course` accepts JSON (the original FastAPI multipart form had no file uploads).
- `requirements.txt`: `+ paramiko>=3.4.0`.
- `.gitignore`: `+ inventario.json` (plaintext SSH and sudo passwords — same pattern as `.env`). Added `inventario.example.json` as the schema template for fresh environments.
- Full course-copy UI rebuilt in Tailwind inside the existing "Clonador de cursos" tab: source server + course ID, destination cards (multi-select + select-all), two category-resolution modes (reference platform with existing/new category, and by-name search with per-server fallback + optional base route), submit, per-server result cards with course links.
- New superadmin-only "Plataformas" section reached via a header button alongside "Usuarios". Side-by-side form + list, CRUD with secret-preserving edit semantics (blank password fields keep the stored value).
- 401 from any `/api/cc/*` call boots the user to the login view; 403 surfaces in the toaster. The course-cloner inventory and platforms list are lazy-loaded on first entry via `window.__ccLoad` / `window.__pfLoad` sentinels.
- Destination platform grid expands to three columns on `lg+` screens. The platform list is sorted alphabetically by name on load, so both the source dropdown and the destination cards come out in the same order.

## v0.3.0 - 2026-05-27

Persistent user system, login, sessions, and per-section permissions. The Aurora cluster (already used as the Moodle clone target via `TARGET_DB_HOST`) now also hosts a dedicated `APP_DB_NAME` (default `moodle_cloner_app`) for the app's own metadata.

- New module `db.py`: PyMySQL-backed `users` / `sessions` / `app_settings` tables (schema bootstrapped on first boot). PBKDF2-SHA256 password hashing with per-user salt. Session tokens are `secrets.token_hex(32)`, stored server-side, exchanged via an HttpOnly `mc_session` cookie with `SameSite=Lax` and a 7-day TTL.
- Initial superadmin seeded from `INITIAL_ADMIN_USER` / `INITIAL_ADMIN_PASS` env vars on first run if no users exist; nothing is seeded thereafter.
- `app.py` gains auth middleware and endpoints: `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`, and CRUD over `/api/users` (`GET`, `POST`, `PATCH`, `DELETE`). `POST /api/clone` and `GET /api/jobs/*` now require auth + `can_access_moodle_cloner`.
- Permission model: per-section booleans (`can_access_moodle_cloner`, `can_access_course_cloner`, `can_access_plugin_cloner`), separate `can_manage_users`, plus an `is_superadmin` flag that implicitly grants everything.
- Guardrails: cannot demote or delete the last superadmin; cannot delete your own account; only superadmins can grant the superadmin role.
- Frontend rewrite of `index.html`: login view, sticky three-tab header (Moodle / Cursos / Plugins) shown to every authenticated user regardless of permission, with a toaster on click for the ones they can't reach. New superadmin/can_manage_users-only `section-users` with a CRUD modal handling all four permission flags. Toaster system added.
- `.env.example` / `.env` extended with `APP_DB_NAME`, `INITIAL_ADMIN_USER`, `INITIAL_ADMIN_PASS`, and optional `APP_SESSION_SECRET`. `PyMySQL>=1.1.0` added to a new `requirements.txt`.
- Fix in `db.py`: `_ensure_database()` could not bootstrap the app database on first boot — `_connect(database=None)` was falling through to the default name, so the `CREATE DATABASE IF NOT EXISTS` statement was issued from a connection scoped to the not-yet-existing DB. Replaced the default with a sentinel so `database=None` now means "connect with no DB selected".

## v0.2.0 - 2026-05-26

Fix silent broken-clone bug surfaced by v0.1.0 logs.

- Switched remote URL-replace and cache-purge in `moodle-clone-web.sh` from `remote_bash` to `remote_sudo_bash`. The remote `$DEST_DIR` is chowned to `www-data` right after rsync and inherits source-side perms (often `750`), so SSH'ing in as `ubuntu` could not `cd` into it. The post-deploy `admin/tool/replace/cli/replace.php` and `admin/cli/purge_caches.php` therefore never ran, leaving the cloned DB with the source's `wwwroot` and the cloned site effectively broken even though the job reported `rc=0`. We now escalate to root for the `cd` and drop to `www-data` for the PHP CLI itself.
- Added `"URL replace failed"` to `fatal_markers` in `app.py`. Even though the bash side intentionally warns-and-continues on URL replace failure (so the rest of the clone can finish), the web app now marks the job as failed when that warning appears, so a broken clone can no longer surface as success in the UI.
- **Manual cleanup needed for existing broken clones** (e.g. `sanase-test`): either re-run the clone with v0.2.0, or on the destination box run `sudo -u www-data php /var/www/html/moodle/<key>/admin/tool/replace/cli/replace.php --non-interactive --search='<old_url>' --replace='<new_url>'` followed by `sudo -u www-data php /var/www/html/moodle/<key>/admin/cli/purge_caches.php`.

## v0.1.0 - 2026-05-26

Diagnostics groundwork to unblock clone-failure debugging.

- Added `VERSION` file and this `CHANGELOG.md` as the iteration tracker; version is now surfaced in `app.py` and rendered in the web UI header.
- Fixed cleanup trap in `moodle-clone-web.sh` to use `sudo rm -rf` for `$TMP_DIR`. Files in `$TMP_DIR/source_data/` are written by `sudo rsync --rsync-path="sudo rsync"` and end up root-owned on the cloner host, so plain `rm -rf` was producing hundreds of "Permission denied" lines that filled the in-memory log ring buffer (250K chars) and masked the real upstream error. Note: this only touches `/tmp/tmp.XXXX` on the cloner box; no change to the source instance.
- Persisted full job output to disk under `./logs/job-<id>.log` in `app.py`. The web UI still shows the truncated tail, but the on-disk file is complete so we can see what actually failed before the cleanup spam.
- Surfaced effective `rc` and fatal-marker detection unchanged; this iteration is diagnostic only and does not attempt to fix the underlying deployment bug.
