"""Persistence layer for app users, sessions and permissions.

Uses the same Aurora MySQL cluster as the Moodle clone target (TARGET_DB_HOST)
but a dedicated database (APP_DB_NAME, default `moodle_cloner_app`).
"""
import hashlib
import os
import secrets
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

import pymysql
from pymysql.cursors import DictCursor

PERMISSION_FLAGS = (
    "can_access_moodle_cloner",
    "can_access_course_cloner",
    "can_access_plugin_cloner",
    "can_access_alexia_cloner",
    "can_manage_users",
)

SESSION_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days
PBKDF2_ITERATIONS = 200_000


def _cfg() -> Dict[str, Any]:
    host = os.getenv("TARGET_DB_HOST", "").strip()
    user = os.getenv("TARGET_DB_ADMIN_USER", "").strip()
    password = os.getenv("TARGET_DB_ADMIN_PASS", "")
    db_name = os.getenv("APP_DB_NAME", "moodle_cloner_app").strip() or "moodle_cloner_app"
    if not host or not user:
        raise RuntimeError(
            "TARGET_DB_HOST / TARGET_DB_ADMIN_USER must be set in environment to use the app database."
        )
    return {"host": host, "user": user, "password": password, "db_name": db_name}


_NO_DB = object()


def _connect(database: Any = _NO_DB) -> pymysql.connections.Connection:
    """Connect to the Aurora cluster.

    By default (database not passed) uses the app database from cfg. Pass
    ``database=None`` to connect without selecting any database (needed to
    bootstrap CREATE DATABASE on first run).
    """
    cfg = _cfg()
    kwargs: Dict[str, Any] = {
        "host": cfg["host"],
        "user": cfg["user"],
        "password": cfg["password"],
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "autocommit": True,
        "connect_timeout": 10,
        # Without these a stalled Aurora endpoint (or a DDL waiting on a
        # metadata lock) blocks the caller forever. At boot that means the
        # process never reaches serve_forever() and nothing is served at all.
        "read_timeout": 30,
        "write_timeout": 30,
    }
    if database is _NO_DB:
        kwargs["database"] = cfg["db_name"]
    elif database is not None:
        kwargs["database"] = database
    # if database is None, omit it entirely (connect with no DB selected)
    return pymysql.connect(**kwargs)


@contextmanager
def conn():
    c = _connect()
    try:
        yield c
    finally:
        c.close()


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """Return a string of the form `pbkdf2_sha256$iterations$salt_hex$hash_hex`."""
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return secrets.compare_digest(dk, expected)


def _ensure_database() -> None:
    cfg = _cfg()
    c = _connect(database=None)
    try:
        with c.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{cfg['db_name']}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        c.close()


SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id INT UNSIGNED NOT NULL AUTO_INCREMENT,
        username VARCHAR(64) NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        is_superadmin TINYINT(1) NOT NULL DEFAULT 0,
        can_access_moodle_cloner TINYINT(1) NOT NULL DEFAULT 0,
        can_access_course_cloner TINYINT(1) NOT NULL DEFAULT 0,
        can_access_plugin_cloner TINYINT(1) NOT NULL DEFAULT 0,
        can_access_alexia_cloner TINYINT(1) NOT NULL DEFAULT 0,
        can_manage_users TINYINT(1) NOT NULL DEFAULT 0,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        UNIQUE KEY uniq_username (username)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token CHAR(64) NOT NULL,
        user_id INT UNSIGNED NOT NULL,
        expires_at DATETIME NOT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (token),
        KEY idx_user_id (user_id),
        CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        `key` VARCHAR(64) NOT NULL,
        `value` TEXT NOT NULL,
        PRIMARY KEY (`key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
]


# Columns that must exist on `users`, in order. `CREATE TABLE IF NOT EXISTS` is
# a no-op on an already-deployed database, so every column added after the first
# production deploy has to be reconciled here on boot. Declared as
# (column, definition, after_column) — keep in sync with PERMISSION_FLAGS.
USERS_COLUMNS = [
    ("is_superadmin", "TINYINT(1) NOT NULL DEFAULT 0", "password_hash"),
    ("can_access_moodle_cloner", "TINYINT(1) NOT NULL DEFAULT 0", "is_superadmin"),
    ("can_access_course_cloner", "TINYINT(1) NOT NULL DEFAULT 0", "can_access_moodle_cloner"),
    ("can_access_plugin_cloner", "TINYINT(1) NOT NULL DEFAULT 0", "can_access_course_cloner"),
    ("can_access_alexia_cloner", "TINYINT(1) NOT NULL DEFAULT 0", "can_access_plugin_cloner"),
    ("can_manage_users", "TINYINT(1) NOT NULL DEFAULT 0", "can_access_alexia_cloner"),
]


def _existing_columns(cur, table: str) -> set:
    cur.execute(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (table,),
    )
    return {row["COLUMN_NAME"] for row in cur.fetchall()}


def _migrate_users_columns(cur) -> List[str]:
    """Add any missing `users` columns. Returns the names of columns added.

    Driven by information_schema rather than by catching duplicate-column
    errors, so it is idempotent by construction and a genuine failure (no ALTER
    privilege, table locked) surfaces instead of being swallowed.
    """
    present = _existing_columns(cur, "users")
    if not present:
        # Table does not exist — SCHEMA just created it, nothing to migrate.
        return []
    added: List[str] = []
    for column, definition, after in USERS_COLUMNS:
        if column in present:
            continue
        clause = f"ADD COLUMN `{column}` {definition}"
        if after and after in present:
            clause += f" AFTER `{after}`"
        cur.execute(f"ALTER TABLE users {clause}")
        present.add(column)
        added.append(column)
    return added


def init_schema() -> List[str]:
    """Create the database/tables if needed and reconcile added columns.

    Returns the list of columns added by this run (empty when already current).
    """
    _ensure_database()
    with conn() as c, c.cursor() as cur:
        # Never let DDL block the boot indefinitely: the server default for
        # lock_wait_timeout is a year, so an ALTER waiting on a metadata lock
        # would hang the process before it binds the HTTP port.
        try:
            cur.execute("SET SESSION lock_wait_timeout = 15")
        except pymysql.err.MySQLError:
            pass
        for stmt in SCHEMA:
            cur.execute(stmt)
        return _migrate_users_columns(cur)


def _get_setting(key: str) -> Optional[str]:
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT `value` FROM app_settings WHERE `key`=%s", (key,))
        row = cur.fetchone()
        return row["value"] if row else None


def _set_setting(key: str, value: str) -> None:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (`key`, `value`) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE `value`=VALUES(`value`)",
            (key, value),
        )


def get_or_create_session_secret() -> str:
    env_secret = os.getenv("APP_SESSION_SECRET", "").strip()
    if env_secret:
        return env_secret
    existing = _get_setting("session_secret")
    if existing:
        return existing
    new_secret = secrets.token_urlsafe(48)
    _set_setting("session_secret", new_secret)
    return new_secret


def seed_initial_admin() -> Optional[str]:
    """Create the initial superadmin from env if no users exist. Returns the username if created."""
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM users")
        if cur.fetchone()["n"] > 0:
            return None
    username = os.getenv("INITIAL_ADMIN_USER", "admin").strip() or "admin"
    password = os.getenv("INITIAL_ADMIN_PASS", "").strip()
    if not password:
        password = "change_me_on_first_login"
    create_user(
        username=username,
        password=password,
        is_superadmin=True,
        permissions={k: True for k in PERMISSION_FLAGS},
    )
    return username


# --- Users ---------------------------------------------------------------

def _row_to_user(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not row:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "is_superadmin": bool(row["is_superadmin"]),
        "permissions": {k: bool(row[k]) for k in PERMISSION_FLAGS},
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def list_users() -> List[Dict[str, Any]]:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, username, is_superadmin, "
            "can_access_moodle_cloner, can_access_course_cloner, can_access_plugin_cloner, can_access_alexia_cloner, can_manage_users, "
            "created_at, updated_at FROM users ORDER BY id ASC"
        )
        return [_row_to_user(r) for r in cur.fetchall()]


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, username, is_superadmin, "
            "can_access_moodle_cloner, can_access_course_cloner, can_access_plugin_cloner, can_access_alexia_cloner, can_manage_users, "
            "created_at, updated_at FROM users WHERE id=%s",
            (user_id,),
        )
        return _row_to_user(cur.fetchone())


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, username, password_hash, is_superadmin, "
            "can_access_moodle_cloner, can_access_course_cloner, can_access_plugin_cloner, can_access_alexia_cloner, can_manage_users, "
            "created_at, updated_at FROM users WHERE username=%s",
            (username,),
        )
        row = cur.fetchone()
        if not row:
            return None
        user = _row_to_user(row)
        user["password_hash"] = row["password_hash"]
        return user


def create_user(
    *,
    username: str,
    password: str,
    is_superadmin: bool = False,
    permissions: Optional[Dict[str, bool]] = None,
) -> Dict[str, Any]:
    perms = {k: False for k in PERMISSION_FLAGS}
    if permissions:
        for k in PERMISSION_FLAGS:
            if k in permissions:
                perms[k] = bool(permissions[k])
    if is_superadmin:
        # Superadmin implicitly gets everything.
        perms = {k: True for k in PERMISSION_FLAGS}

    password_hash = hash_password(password)
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash, is_superadmin, "
            "can_access_moodle_cloner, can_access_course_cloner, can_access_plugin_cloner, can_access_alexia_cloner, can_manage_users) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                username,
                password_hash,
                1 if is_superadmin else 0,
                1 if perms["can_access_moodle_cloner"] else 0,
                1 if perms["can_access_course_cloner"] else 0,
                1 if perms["can_access_plugin_cloner"] else 0,
                1 if perms["can_access_alexia_cloner"] else 0,
                1 if perms["can_manage_users"] else 0,
            ),
        )
        new_id = cur.lastrowid
    return get_user(new_id)


def update_user(
    user_id: int,
    *,
    password: Optional[str] = None,
    is_superadmin: Optional[bool] = None,
    permissions: Optional[Dict[str, bool]] = None,
) -> Optional[Dict[str, Any]]:
    sets = []
    params: List[Any] = []
    if password is not None and password != "":
        sets.append("password_hash=%s")
        params.append(hash_password(password))
    if is_superadmin is not None:
        sets.append("is_superadmin=%s")
        params.append(1 if is_superadmin else 0)
        if is_superadmin:
            for k in PERMISSION_FLAGS:
                sets.append(f"{k}=1")
    if permissions:
        for k in PERMISSION_FLAGS:
            if k in permissions:
                sets.append(f"{k}=%s")
                params.append(1 if permissions[k] else 0)
    if not sets:
        return get_user(user_id)
    params.append(user_id)
    with conn() as c, c.cursor() as cur:
        cur.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=%s", params)
    return get_user(user_id)


def delete_user(user_id: int) -> bool:
    with conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
        return cur.rowcount > 0


def count_superadmins() -> int:
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM users WHERE is_superadmin=1")
        return int(cur.fetchone()["n"])


# --- Sessions ------------------------------------------------------------

def create_session(user_id: int) -> Dict[str, Any]:
    token = secrets.token_hex(32)  # 64 chars
    expires_ts = int(time.time()) + SESSION_TTL_SECONDS
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, FROM_UNIXTIME(%s))",
            (token, user_id, expires_ts),
        )
    return {"token": token, "expires_at": expires_ts}


def get_session_user(token: str) -> Optional[Dict[str, Any]]:
    if not token or len(token) != 64:
        return None
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT u.id, u.username, u.is_superadmin, "
            "u.can_access_moodle_cloner, u.can_access_course_cloner, u.can_access_plugin_cloner, u.can_access_alexia_cloner, u.can_manage_users, "
            "u.created_at, u.updated_at "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token=%s AND s.expires_at > NOW()",
            (token,),
        )
        return _row_to_user(cur.fetchone())


def delete_session(token: str) -> None:
    if not token:
        return
    with conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE token=%s", (token,))


def purge_expired_sessions() -> None:
    with conn() as c, c.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE expires_at <= NOW()")


# --- Manual migration entry point ---------------------------------------
# `python3 db.py` reconciles the schema and reports what it did, without
# restarting the API. Loads .env the same way app.py does.

if __name__ == "__main__":
    import sys
    from pathlib import Path

    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            os.environ.setdefault(k.strip(), v)

    try:
        added_cols = init_schema()
    except Exception as exc:  # noqa: BLE001 — CLI: report and exit non-zero
        print(f"FAILED: {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
    if added_cols:
        print("Added users columns: " + ", ".join(added_cols), flush=True)
    else:
        print("Schema already up to date.", flush=True)
