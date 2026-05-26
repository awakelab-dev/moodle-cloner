# Changelog

All notable changes to the Moodle Cloner web app are tracked here.
Every code iteration must bump the version in `VERSION` and add an entry below.

Format: `YYYY-MM-DD - vX.Y.Z - Short description` followed by a bulleted list.

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
