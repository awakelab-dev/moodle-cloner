"""Persistence layer for app users, sessions and permissions.

Uses the same Aurora MySQL cluster as the Moodle clone target (TARGET_DB_HOST)
but a dedicated database (APP_DB_NAME, default `moodle_cloner_app`).
"""
import hashlib
import json
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
    # Lotes del replicador Alexia. Viven en la base y no en memoria porque una
    # corrida de 700 cursos dura horas: el navegador se recarga, el usuario se
    # va a otra seccion y el proceso se reinicia con pm2, y el progreso tiene
    # que seguir estando.
    """
    CREATE TABLE IF NOT EXISTS alexia_batches (
        id VARCHAR(32) NOT NULL,
        status VARCHAR(20) NOT NULL,
        total INT UNSIGNED NOT NULL DEFAULT 0,
        started_by VARCHAR(64) NULL,
        current_shortname VARCHAR(190) NULL,
        error TEXT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        finished_at DATETIME NULL,
        PRIMARY KEY (id),
        KEY idx_status (status),
        KEY idx_created_at (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # Copias de curso del Replicador de Cursos. Mismo motivo que los lotes de
    # Alexia: la copia tarda minutos, el request no puede quedarse esperando y
    # el resumen tiene que seguir estando si el navegador se recarga.
    """
    CREATE TABLE IF NOT EXISTS course_copy_jobs (
        id VARCHAR(32) NOT NULL,
        status VARCHAR(20) NOT NULL,
        stage VARCHAR(40) NULL,
        source_name VARCHAR(190) NULL,
        course_id INT UNSIGNED NULL,
        total INT UNSIGNED NOT NULL DEFAULT 0,
        started_by VARCHAR(64) NULL,
        message TEXT NULL,
        error TEXT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        finished_at DATETIME NULL,
        PRIMARY KEY (id),
        KEY idx_status (status),
        KEY idx_created_at (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS course_copy_targets (
        job_id VARCHAR(32) NOT NULL,
        idx INT UNSIGNED NOT NULL,
        server_index INT NULL,
        server_name VARCHAR(190) NULL,
        server_host VARCHAR(190) NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        message TEXT NULL,
        result TEXT NULL,
        error TEXT NULL,
        PRIMARY KEY (job_id, idx),
        KEY idx_job_status (job_id, status),
        CONSTRAINT fk_copy_targets_job FOREIGN KEY (job_id)
            REFERENCES course_copy_jobs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # Instalaciones de plugin. A diferencia de los otros dos, aca los destinos
    # se procesan en paralelo, asi que varias filas pueden estar en 'running'
    # a la vez.
    """
    CREATE TABLE IF NOT EXISTS plugin_install_jobs (
        id VARCHAR(64) NOT NULL,
        status VARCHAR(20) NOT NULL,
        plugin_folder VARCHAR(190) NULL,
        plugin_type VARCHAR(64) NULL,
        zip_name VARCHAR(255) NULL,
        total INT UNSIGNED NOT NULL DEFAULT 0,
        concurrency INT UNSIGNED NOT NULL DEFAULT 1,
        started_by VARCHAR(64) NULL,
        message TEXT NULL,
        error TEXT NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        finished_at DATETIME NULL,
        PRIMARY KEY (id),
        KEY idx_status (status),
        KEY idx_created_at (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS plugin_install_targets (
        job_id VARCHAR(64) NOT NULL,
        idx INT UNSIGNED NOT NULL,
        server_name VARCHAR(190) NULL,
        server_host VARCHAR(190) NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        message TEXT NULL,
        error TEXT NULL,
        PRIMARY KEY (job_id, idx),
        KEY idx_job_status (job_id, status),
        CONSTRAINT fk_plugin_targets_job FOREIGN KEY (job_id)
            REFERENCES plugin_install_jobs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """,
    # `form_data` y `category_path` se guardan porque son exactamente lo que
    # hace falta para reanudar un lote cortado sin volver a pedir el Excel.
    """
    CREATE TABLE IF NOT EXISTS alexia_batch_rows (
        batch_id VARCHAR(32) NOT NULL,
        idx INT UNSIGNED NOT NULL,
        shortname_alexia VARCHAR(190) NULL,
        codigo_oficial VARCHAR(64) NULL,
        reducido_grupo VARCHAR(64) NULL,
        form_data TEXT NULL,
        category_path TEXT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        message TEXT NULL,
        result TEXT NULL,
        error TEXT NULL,
        PRIMARY KEY (batch_id, idx),
        KEY idx_batch_status (batch_id, status),
        CONSTRAINT fk_batch_rows_batch FOREIGN KEY (batch_id)
            REFERENCES alexia_batches(id) ON DELETE CASCADE
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


# --- Lotes del replicador Alexia ----------------------------------------
# El progreso se lee con un COUNT agrupado por estado en vez de mantener
# contadores desnormalizados: con 700 filas la agregacion es instantanea y no
# hay dos fuentes de verdad que se puedan desincronizar.

ALEXIA_BATCH_ACTIVE = ("pending", "running")
ALEXIA_ROW_TERMINAL = ("completed", "error")


def alexia_batch_create(
    batch_id: str, rows: List[Dict[str, Any]], started_by: Optional[str] = None
) -> None:
    """Crea el lote y sus filas en una transaccion. `rows` viene del Excel."""
    payload = []
    for i, r in enumerate(rows):
        form = r.get("form_data") or {}
        payload.append((
            batch_id, i,
            (r.get("shortname_alexia") or "")[:190],
            str(form.get("codigo_oficial") or "")[:64],
            str(form.get("reducido_grupo") or "")[:64],
            _json_dumps(form),
            _json_dumps(r.get("category_path") or []),
        ))
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO alexia_batches (id, status, total, started_by) "
            "VALUES (%s, 'pending', %s, %s)",
            (batch_id, len(rows), (started_by or None)),
        )
        if payload:
            cur.executemany(
                "INSERT INTO alexia_batch_rows "
                "(batch_id, idx, shortname_alexia, codigo_oficial, reducido_grupo, "
                " form_data, category_path) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                payload,
            )


def alexia_batch_set_status(
    batch_id: str,
    status: str,
    *,
    error: Optional[str] = None,
    current_shortname: Optional[str] = None,
    finished: bool = False,
) -> None:
    sets = ["status=%s", "error=%s", "current_shortname=%s"]
    args: List[Any] = [status, error, current_shortname]
    if finished:
        sets.append("finished_at=NOW()")
    with conn() as c, c.cursor() as cur:
        cur.execute(
            f"UPDATE alexia_batches SET {', '.join(sets)} WHERE id=%s",
            (*args, batch_id),
        )


def alexia_batch_update_rows(batch_id: str, rows: List[Dict[str, Any]]) -> None:
    """Vuelca el estado de un puñado de filas. Solo las que cambiaron."""
    if not rows:
        return
    payload = [
        (
            r.get("status") or "pending",
            (r.get("message") or None),
            _json_dumps(r["result"]) if r.get("result") else None,
            (r.get("error") or None),
            batch_id,
            int(r["index"]),
        )
        for r in rows
    ]
    with conn() as c, c.cursor() as cur:
        cur.executemany(
            "UPDATE alexia_batch_rows SET status=%s, message=%s, result=%s, error=%s "
            "WHERE batch_id=%s AND idx=%s",
            payload,
        )


def alexia_batch_progress(batch_id: str) -> Optional[Dict[str, Any]]:
    """Progreso liviano: contadores y estado, sin las filas.

    Es lo que consulta el navegador cada pocos segundos. Devolver las 700 filas
    en cada consulta era justamente lo que dejaba sin recursos a la maquina del
    usuario.
    """
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, status, total, started_by, current_shortname, error, "
            "created_at, updated_at, finished_at FROM alexia_batches WHERE id=%s",
            (batch_id,),
        )
        batch = cur.fetchone()
        if not batch:
            return None
        cur.execute(
            "SELECT status, COUNT(*) AS n FROM alexia_batch_rows "
            "WHERE batch_id=%s GROUP BY status",
            (batch_id,),
        )
        counts = {row["status"]: int(row["n"]) for row in cur.fetchall()}
    completed = counts.get("completed", 0)
    errors = counts.get("error", 0)
    total = int(batch["total"] or 0)
    done = completed + errors
    return {
        "id": batch["id"],
        "status": batch["status"],
        "total": total,
        "completed_count": completed,
        "error_count": errors,
        "done_count": done,
        "pending_count": max(total - done, 0),
        "progress": int(done / total * 100) if total else 100,
        "current_shortname": batch["current_shortname"],
        "started_by": batch["started_by"],
        "error": batch["error"],
        "created_at": _iso(batch["created_at"]),
        "updated_at": _iso(batch["updated_at"]),
        "finished_at": _iso(batch["finished_at"]),
        "counts": counts,
    }


def alexia_batch_latest(active_only: bool = False) -> Optional[Dict[str, Any]]:
    """El lote en curso si hay uno; si no, el mas reciente.

    Es lo que permite que el navegador vuelva a enganchar el progreso despues
    de recargarse, sin que el front tenga que recordar el batch_id.
    """
    where = ""
    args: tuple = ()
    if active_only:
        where = "WHERE status IN (%s, %s)"
        args = ALEXIA_BATCH_ACTIVE
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id FROM alexia_batches "
            + where
            + " ORDER BY (status IN ('pending','running')) DESC, created_at DESC LIMIT 1",
            args,
        )
        row = cur.fetchone()
    return alexia_batch_progress(row["id"]) if row else None


def alexia_batch_rows(
    batch_id: str,
    offset: int = 0,
    limit: int = 50,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """Filas paginadas. Se piden solo cuando el usuario abre el detalle."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where = "WHERE batch_id=%s"
    args: List[Any] = [batch_id]
    if status:
        where += " AND status=%s"
        args.append(status)
    with conn() as c, c.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM alexia_batch_rows {where}", args)
        matched = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute(
            "SELECT idx, shortname_alexia, codigo_oficial, reducido_grupo, "
            "status, message, result, error, category_path "
            f"FROM alexia_batch_rows {where} ORDER BY idx LIMIT %s OFFSET %s",
            (*args, limit, offset),
        )
        rows = cur.fetchall()
    out = []
    for r in rows:
        out.append({
            "index": int(r["idx"]),
            "shortname_alexia": r["shortname_alexia"],
            "codigo_oficial": r["codigo_oficial"],
            "reducido_grupo": r["reducido_grupo"],
            "status": r["status"],
            "message": r["message"],
            "error": r["error"],
            "result": _json_loads(r["result"]),
            "category_path": _json_loads(r["category_path"]) or [],
        })
    return {"batch_id": batch_id, "matched": matched, "offset": offset,
            "limit": limit, "rows": out}


def alexia_batch_pending_rows(batch_id: str) -> List[Dict[str, Any]]:
    """Filas que faltan procesar, con lo necesario para reanudar el lote."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT idx, shortname_alexia, form_data, category_path, status "
            "FROM alexia_batch_rows WHERE batch_id=%s ORDER BY idx",
            (batch_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "index": int(r["idx"]),
            "shortname_alexia": r["shortname_alexia"],
            "form_data": _json_loads(r["form_data"]) or {},
            "category_path": _json_loads(r["category_path"]) or [],
            "status": r["status"],
        }
        for r in rows
    ]


def alexia_batches_unfinished() -> List[str]:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id FROM alexia_batches WHERE status IN (%s, %s) ORDER BY created_at",
            ALEXIA_BATCH_ACTIVE,
        )
        return [r["id"] for r in cur.fetchall()]


# --- Copias de curso (Replicador de Cursos) -----------------------------
# Mismo patron que los lotes de Alexia. La diferencia esta en la reanudacion:
# aca NO se reanuda, porque el .mbz del backup vive en un TemporaryDirectory
# local que desaparece cuando muere el proceso. Un job cortado se marca
# `interrupted` y hay que relanzarlo.

COURSE_COPY_ACTIVE = ("pending", "running")


def course_copy_create(
    job_id: str,
    *,
    course_id: int,
    source_name: str,
    targets: List[Dict[str, Any]],
    started_by: Optional[str] = None,
) -> None:
    payload = [
        (
            job_id, i,
            t.get("server_index"),
            (t.get("server_name") or "")[:190],
            (t.get("server_host") or "")[:190],
        )
        for i, t in enumerate(targets)
    ]
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO course_copy_jobs "
            "(id, status, stage, source_name, course_id, total, started_by) "
            "VALUES (%s, 'pending', 'queued', %s, %s, %s, %s)",
            (job_id, source_name[:190], int(course_id), len(targets), started_by or None),
        )
        if payload:
            cur.executemany(
                "INSERT INTO course_copy_targets "
                "(job_id, idx, server_index, server_name, server_host) "
                "VALUES (%s,%s,%s,%s,%s)",
                payload,
            )


def course_copy_set_status(
    job_id: str,
    status: str,
    *,
    stage: Optional[str] = None,
    message: Optional[str] = None,
    error: Optional[str] = None,
    finished: bool = False,
) -> None:
    sets = ["status=%s", "stage=%s", "message=%s", "error=%s"]
    args: List[Any] = [status, stage, message, error]
    if finished:
        sets.append("finished_at=NOW()")
    with conn() as c, c.cursor() as cur:
        cur.execute(
            f"UPDATE course_copy_jobs SET {', '.join(sets)} WHERE id=%s",
            (*args, job_id),
        )


def course_copy_update_target(
    job_id: str,
    index: int,
    status: str,
    *,
    message: Optional[str] = None,
    result: Any = None,
    error: Optional[str] = None,
) -> None:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE course_copy_targets SET status=%s, message=%s, result=%s, error=%s "
            "WHERE job_id=%s AND idx=%s",
            (status, message, _json_dumps(result) if result else None, error, job_id, int(index)),
        )


def course_copy_progress(job_id: str) -> Optional[Dict[str, Any]]:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, status, stage, source_name, course_id, total, started_by, "
            "message, error, created_at, updated_at, finished_at "
            "FROM course_copy_jobs WHERE id=%s",
            (job_id,),
        )
        job = cur.fetchone()
        if not job:
            return None
        cur.execute(
            "SELECT status, COUNT(*) AS n FROM course_copy_targets "
            "WHERE job_id=%s GROUP BY status",
            (job_id,),
        )
        counts = {r["status"]: int(r["n"]) for r in cur.fetchall()}
        cur.execute(
            "SELECT server_name FROM course_copy_targets "
            "WHERE job_id=%s AND status='running' ORDER BY idx LIMIT 1",
            (job_id,),
        )
        running = cur.fetchone()
    completed = counts.get("completed", 0)
    errors = counts.get("error", 0)
    total = int(job["total"] or 0)
    done = completed + errors
    return {
        "id": job["id"],
        "status": job["status"],
        "stage": job["stage"],
        "source_name": job["source_name"],
        "course_id": job["course_id"],
        "total": total,
        "completed_count": completed,
        "error_count": errors,
        "done_count": done,
        "pending_count": max(total - done, 0),
        "progress": int(done / total * 100) if total else 0,
        "current_target": running["server_name"] if running else None,
        "started_by": job["started_by"],
        "message": job["message"],
        "error": job["error"],
        "created_at": _iso(job["created_at"]),
        "updated_at": _iso(job["updated_at"]),
        "finished_at": _iso(job["finished_at"]),
        "counts": counts,
    }


def course_copy_latest(active_only: bool = False) -> Optional[Dict[str, Any]]:
    where = ""
    args: tuple = ()
    if active_only:
        where = "WHERE status IN (%s, %s)"
        args = COURSE_COPY_ACTIVE
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id FROM course_copy_jobs "
            + where
            + " ORDER BY (status IN ('pending','running')) DESC, created_at DESC LIMIT 1",
            args,
        )
        row = cur.fetchone()
    return course_copy_progress(row["id"]) if row else None


def course_copy_targets(job_id: str) -> List[Dict[str, Any]]:
    """Todos los destinos de una copia. Son pocos (hasta 35), no hace falta paginar."""
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT idx, server_index, server_name, server_host, status, message, result, error "
            "FROM course_copy_targets WHERE job_id=%s ORDER BY idx",
            (job_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "index": int(r["idx"]),
            "server_index": r["server_index"],
            "server_name": r["server_name"],
            "server_host": r["server_host"],
            "status": r["status"],
            "message": r["message"],
            "error": r["error"],
            "result": _json_loads(r["result"]),
        }
        for r in rows
    ]


def course_copy_unfinished() -> List[str]:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id FROM course_copy_jobs WHERE status IN (%s, %s) ORDER BY created_at",
            COURSE_COPY_ACTIVE,
        )
        return [r["id"] for r in cur.fetchall()]


# --- Instalaciones de plugin --------------------------------------------
# Mismo patron que los dos anteriores. Se escriben explicitas y no sobre un
# helper generico porque las tres tienen columnas distintas, y en este archivo
# la consistencia de estilo pesa mas que evitar la repeticion.
# Tampoco se reanuda: el ZIP vive en un directorio temporal que muere con el
# proceso.

PLUGIN_INSTALL_ACTIVE = ("pending", "running")


def plugin_install_create(
    job_id: str,
    *,
    plugin_folder: str,
    plugin_type: str,
    zip_name: str,
    concurrency: int,
    targets: List[Dict[str, Any]],
    started_by: Optional[str] = None,
) -> None:
    payload = [
        (job_id, i, (t.get("server_name") or "")[:190], (t.get("server_host") or "")[:190])
        for i, t in enumerate(targets)
    ]
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO plugin_install_jobs "
            "(id, status, plugin_folder, plugin_type, zip_name, total, concurrency, started_by) "
            "VALUES (%s, 'pending', %s, %s, %s, %s, %s, %s)",
            (job_id, plugin_folder[:190], plugin_type[:64], zip_name[:255],
             len(targets), int(concurrency), started_by or None),
        )
        if payload:
            cur.executemany(
                "INSERT INTO plugin_install_targets "
                "(job_id, idx, server_name, server_host) VALUES (%s,%s,%s,%s)",
                payload,
            )


def plugin_install_set_status(
    job_id: str,
    status: str,
    *,
    message: Optional[str] = None,
    error: Optional[str] = None,
    finished: bool = False,
) -> None:
    sets = ["status=%s", "message=%s", "error=%s"]
    args: List[Any] = [status, message, error]
    if finished:
        sets.append("finished_at=NOW()")
    with conn() as c, c.cursor() as cur:
        cur.execute(
            f"UPDATE plugin_install_jobs SET {', '.join(sets)} WHERE id=%s",
            (*args, job_id),
        )


def plugin_install_update_target(
    job_id: str,
    index: int,
    status: str,
    *,
    message: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE plugin_install_targets SET status=%s, message=%s, error=%s "
            "WHERE job_id=%s AND idx=%s",
            (status, message, error, job_id, int(index)),
        )


def plugin_install_progress(job_id: str) -> Optional[Dict[str, Any]]:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id, status, plugin_folder, plugin_type, zip_name, total, concurrency, "
            "started_by, message, error, created_at, updated_at, finished_at "
            "FROM plugin_install_jobs WHERE id=%s",
            (job_id,),
        )
        job = cur.fetchone()
        if not job:
            return None
        cur.execute(
            "SELECT status, COUNT(*) AS n FROM plugin_install_targets "
            "WHERE job_id=%s GROUP BY status",
            (job_id,),
        )
        counts = {r["status"]: int(r["n"]) for r in cur.fetchall()}
    completed = counts.get("completed", 0)
    errors = counts.get("error", 0)
    total = int(job["total"] or 0)
    done = completed + errors
    return {
        "id": job["id"],
        "status": job["status"],
        "plugin_folder": job["plugin_folder"],
        "plugin_type": job["plugin_type"],
        "zip_name": job["zip_name"],
        "total": total,
        "concurrency": int(job["concurrency"] or 1),
        "completed_count": completed,
        "error_count": errors,
        "done_count": done,
        "running_count": counts.get("running", 0),
        "pending_count": max(total - done - counts.get("running", 0), 0),
        "progress": int(done / total * 100) if total else 0,
        "started_by": job["started_by"],
        "message": job["message"],
        "error": job["error"],
        "created_at": _iso(job["created_at"]),
        "updated_at": _iso(job["updated_at"]),
        "finished_at": _iso(job["finished_at"]),
        "counts": counts,
    }


def plugin_install_latest(active_only: bool = False) -> Optional[Dict[str, Any]]:
    where = ""
    args: tuple = ()
    if active_only:
        where = "WHERE status IN (%s, %s)"
        args = PLUGIN_INSTALL_ACTIVE
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id FROM plugin_install_jobs "
            + where
            + " ORDER BY (status IN ('pending','running')) DESC, created_at DESC LIMIT 1",
            args,
        )
        row = cur.fetchone()
    return plugin_install_progress(row["id"]) if row else None


def plugin_install_targets(job_id: str) -> List[Dict[str, Any]]:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT idx, server_name, server_host, status, message, error "
            "FROM plugin_install_targets WHERE job_id=%s ORDER BY idx",
            (job_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "index": int(r["idx"]),
            "server_name": r["server_name"],
            "server_host": r["server_host"],
            "status": r["status"],
            "message": r["message"],
            "error": r["error"],
        }
        for r in rows
    ]


def plugin_install_unfinished() -> List[str]:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT id FROM plugin_install_jobs WHERE status IN (%s, %s) ORDER BY created_at",
            PLUGIN_INSTALL_ACTIVE,
        )
        return [r["id"] for r in cur.fetchall()]


def _json_dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: Any) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value is not None else None


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
