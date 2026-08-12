#!/usr/bin/env python3
import hashlib
import json
import os
import re
import threading
import time
import uuid
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from subprocess import PIPE, STDOUT, Popen
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "index.html"
LOGO_FILE = ROOT / "RESIZED_logo_fondooscuro_horizontal.png"
SCRIPT_FILE = ROOT / "moodle-clone-web.sh"
ENV_FILE = ROOT / ".env"
VERSION_FILE = ROOT / "VERSION"
LOG_DIR = ROOT / "logs"

SESSION_COOKIE_NAME = "mc_session"


def read_app_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


APP_VERSION = read_app_version()


def load_dotenv(filepath: Path) -> None:
    if not filepath.exists():
        return
    for raw_line in filepath.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_dotenv(ENV_FILE)

import db  # noqa: E402 — must load after .env
import course_routes  # noqa: E402 — depends on paramiko, must load after .env
import plugin_routes  # noqa: E402
import alexia_routes  # noqa: E402

HOST = os.getenv("APP_HOST", "0.0.0.0")
PORT = int(os.getenv("APP_PORT", "8787"))
MAX_LOG_CHARS = 250_000
MAX_PLUGIN_ZIP_SIZE = 100 * 1024 * 1024  # 100 MB
DEFAULT_REMOTE_HOST = "51.44.30.62"
REMOTE_SSH_KEY = os.path.expanduser(os.getenv("REMOTE_SSH_KEY", str(Path.home() / ".ssh" / "id_ed25519")))
DEFAULT_SOURCE_SSH_USER = "ubuntu"
TARGET_DB_HOST_ENV = os.getenv("TARGET_DB_HOST", "")
TARGET_DB_ADMIN_USER_ENV = os.getenv("TARGET_DB_ADMIN_USER", "")
TARGET_DB_ADMIN_PASS_ENV = os.getenv("TARGET_DB_ADMIN_PASS", "")


def esc_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def render_index_html() -> bytes:
    html = INDEX_FILE.read_text(encoding="utf-8")
    # A truncated index.html (interrupted upload, unquoted-heredoc paste) leaves
    # an unterminated <script> that browsers discard silently — blank page, no
    # console error, nothing in the server log. Fail loudly instead.
    if not html.rstrip().endswith("</html>"):
        raise RuntimeError(
            f"index.html looks truncated ({len(html)} chars, does not end with </html>). "
            "Re-upload it (git checkout -- index.html) — a partial file renders as a blank page."
        )
    html = html.replace("{{TARGET_DB_HOST}}", esc_html(TARGET_DB_HOST_ENV.strip()))
    html = html.replace("{{APP_VERSION}}", esc_html(APP_VERSION))
    return html.encode("utf-8")


jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def bool_to_env(value: Any) -> str:
    return "1" if bool(value) else "0"


def is_safe_path(value: str) -> bool:
    return bool(re.fullmatch(r"/[A-Za-z0-9_./-]+", value))


def sanitize_preview(payload: Dict[str, Any]) -> Dict[str, Any]:
    clean = dict(payload)
    for key in ("db_pass", "target_db_pass", "source_db_pass", "target_db_admin_pass"):
        if key in clean:
            clean[key] = "***"
    return clean


def validate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Payload inválido.")

    source_mode = str(payload.get("source_mode", "local")).strip().lower()
    source_host = str(payload.get("source_host", "")).strip()
    source_dir = str(payload.get("source_dir", "")).strip()
    source_data = str(payload.get("source_data", "")).strip()
    source_vhost = str(payload.get("source_vhost", "")).strip()
    new_key = str(payload.get("new_key", "")).strip()
    new_domain = str(payload.get("new_domain", "")).strip()
    new_url = str(payload.get("new_url", "")).strip()
    dest_dir = str(payload.get("dest_dir", "")).strip()
    dest_data = str(payload.get("dest_data", "")).strip()
    deploy_target = str(payload.get("deploy_target", "local")).strip().lower()
    remote_host = str(payload.get("remote_host", DEFAULT_REMOTE_HOST)).strip()

    target_db_host = TARGET_DB_HOST_ENV.strip()
    target_db_admin_user = TARGET_DB_ADMIN_USER_ENV.strip()
    target_db_admin_pass = TARGET_DB_ADMIN_PASS_ENV
    target_db_user = str(payload.get("target_db_user", payload.get("db_user", ""))).strip()
    target_db_pass = str(payload.get("target_db_pass", payload.get("db_pass", "")))
    dest_db = str(payload.get("dest_db", "")).strip()

    if source_mode not in ("local", "remote"):
        raise ValueError("Modo de origen inválido. Usa 'local' o 'remote'.")

    if source_mode == "local":
        try:
            source_index = int(payload.get("source_index", -1))
        except (TypeError, ValueError):
            raise ValueError("source_index inválido para modo local.")
        try:
            inv_servers = course_routes._load_servers()
        except Exception as exc:
            raise ValueError(f"No se pudo cargar el inventario de plataformas: {exc}")
        if source_index < 0 or source_index >= len(inv_servers):
            raise ValueError("Plataforma origen no encontrada en el inventario.")
        src_entry = inv_servers[source_index]
        source_dir = str(src_entry.get("moodle_path") or "").rstrip("/")
        source_data = str(src_entry.get("moodledata_path") or "")
        source_vhost = str(src_entry.get("vhost_path") or "")
        if not source_dir:
            raise ValueError("La plataforma origen no tiene ruta Moodle configurada (moodle_path).")
        if not source_data:
            raise ValueError("La plataforma origen no tiene ruta moodledata configurada (moodledata_path).")
        if not source_vhost:
            raise ValueError("La plataforma origen no tiene ruta vhost configurada (vhost_path).")
    else:
        if not re.fullmatch(r"[A-Za-z0-9.-]+", source_host):
            raise ValueError("Host origen inválido.")
        if not is_safe_path(source_dir):
            raise ValueError("Directorio Moodle origen inválido.")
        if not is_safe_path(source_data):
            raise ValueError("Directorio moodledata origen inválido.")
        if not is_safe_path(source_vhost):
            raise ValueError("Ruta de vhost origen inválida.")

    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}", new_key):
        raise ValueError("new_key inválido. Usa letras, números, '_' o '-'.")

    if not re.fullmatch(r"[A-Za-z0-9.-]+", new_domain):
        raise ValueError("Dominio inválido.")

    if not re.fullmatch(r"https?://[^\s]+", new_url):
        raise ValueError("URL base inválida.")

    if not is_safe_path(dest_dir):
        raise ValueError("Directorio destino inválido.")

    if not is_safe_path(dest_data):
        raise ValueError("Directorio moodledata inválido.")

    if deploy_target not in ("local", "remote"):
        raise ValueError("Destino inválido. Usa 'local' o 'remote'.")

    if deploy_target == "remote":
        if not remote_host:
            remote_host = DEFAULT_REMOTE_HOST

        if not re.fullmatch(r"[A-Za-z0-9.-]+", remote_host):
            raise ValueError("Host remoto inválido.")

        if not dest_dir.startswith("/var/www/html/moodle/"):
            raise ValueError("En remoto, el directorio Moodle debe iniciar con /var/www/html/moodle/.")

        if not dest_data.startswith("/var/www/data/moodle/"):
            raise ValueError("En remoto, el directorio moodledata debe iniciar con /var/www/data/moodle/.")

    if not re.fullmatch(r"[A-Za-z0-9._-]+", target_db_host):
        raise ValueError("Host DB destino inválido.")

    if not re.fullmatch(r"[A-Za-z0-9._$-]+", target_db_admin_user):
        raise ValueError("Usuario administrador BD destino inválido (TARGET_DB_ADMIN_USER).")

    if not target_db_admin_pass:
        raise ValueError("La contraseña administradora BD destino es obligatoria (TARGET_DB_ADMIN_PASS).")

    if not re.fullmatch(r"[A-Za-z0-9._$-]+", target_db_user):
        raise ValueError("Usuario DB destino inválido.")

    if not target_db_pass:
        raise ValueError("La contraseña de BD destino es obligatoria.")

    if not re.fullmatch(r"[A-Za-z0-9_]+", dest_db):
        raise ValueError("Nombre de BD destino inválido (solo letras, números y _).")

    validated = {
        "source_mode": source_mode,
        "source_host": source_host,
        "source_dir": source_dir,
        "source_data": source_data,
        "source_vhost": source_vhost,
        "new_key": new_key,
        "new_domain": new_domain,
        "new_url": new_url,
        "dest_dir": dest_dir,
        "dest_data": dest_data,
        "deploy_target": deploy_target,
        "remote_host": remote_host if deploy_target == "remote" else "",
        "target_db_host": target_db_host,
        "target_db_user": target_db_user,
        "target_db_pass": target_db_pass,
        "target_db_admin_user": target_db_admin_user,
        "target_db_admin_pass": target_db_admin_pass,
        "dest_db": dest_db,
        "maintenance_source": bool(payload.get("maintenance_source", True)),
        "opt_replace": bool(payload.get("opt_replace", True)),
        "opt_purge": bool(payload.get("opt_purge", True)),
        "opt_nginx": bool(payload.get("opt_nginx", True)),
        "opt_certbot": bool(payload.get("opt_certbot", True)),
        "opt_cron": bool(payload.get("opt_cron", True)),
        "dry_run": bool(payload.get("dry_run", False)),
    }
    return validated


def append_job_output(job_id: str, text: str) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["output"] += text
        if len(job["output"]) > MAX_LOG_CHARS:
            job["output"] = "... (output truncated) ...\n" + job["output"][-MAX_LOG_CHARS:]
        job["updated_at"] = now_iso()
        log_path = job.get("log_path")
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            pass


def update_job(job_id: str, **changes: Any) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job.update(changes)
        job["updated_at"] = now_iso()


def run_clone_job(job_id: str, payload: Dict[str, Any]) -> None:
    src_dir = payload["source_dir"]
    src_data = payload["source_data"]
    src_vhost = payload["source_vhost"]

    env = os.environ.copy()
    env.update(
        {
            "SRC_DIR": src_dir,
            "SRC_DATA": src_data,
            "SRC_VHOST": src_vhost,
            "SOURCE_MODE": payload["source_mode"],
            "SOURCE_HOST": payload["source_host"],
            "SOURCE_SSH_USER": DEFAULT_SOURCE_SSH_USER,
            "SOURCE_SSH_KEY": REMOTE_SSH_KEY,
            "NEW_KEY": payload["new_key"],
            "NEW_DOMAIN": payload["new_domain"],
            "NEW_URL": payload["new_url"],
            "DEST_DIR": payload["dest_dir"],
            "DEST_DATA": payload["dest_data"],
            "DEST_DB": payload["dest_db"],
            "DEPLOY_TARGET": payload["deploy_target"],
            "REMOTE_HOST": payload["remote_host"],
            "REMOTE_SSH_KEY": REMOTE_SSH_KEY,
            "TARGET_DB_HOST": payload["target_db_host"],
            "TARGET_DB_USER": payload["target_db_admin_user"],
            "TARGET_DB_PASS": payload["target_db_admin_pass"],
            "DB_HOST": payload["target_db_host"],
            "DB_USER": payload["target_db_user"],
            "DB_PASS": payload["target_db_pass"],
            "ENABLE_SRC_MAINT": bool_to_env(payload["maintenance_source"]),
            "ENABLE_REPLACE": bool_to_env(payload["opt_replace"]),
            "ENABLE_PURGE": bool_to_env(payload["opt_purge"]),
            "ENABLE_NGINX": bool_to_env(payload["opt_nginx"]),
            "ENABLE_CERTBOT": bool_to_env(payload["opt_certbot"]),
            "ENABLE_CRON": bool_to_env(payload["opt_cron"]),
            "DISABLE_NEW_MAINT": "1",
            "DISABLE_SRC_MAINT_AFTER": "1",
            "DRY_RUN": bool_to_env(payload["dry_run"]),
            "I_UNDERSTAND_PRODUCTION_RDS": "true" if not payload["dry_run"] else "",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
    )

    update_job(job_id, status="running")
    append_job_output(job_id, f"[{now_iso()}] Starting clone job...\n")

    try:
        process = Popen(
            [str(SCRIPT_FILE)],
            cwd=str(ROOT),
            env=env,
            stdout=PIPE,
            stderr=STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        assert process.stdout is not None
        for line in process.stdout:
            append_job_output(job_id, line)

        rc = process.wait()
        with jobs_lock:
            current_output = jobs.get(job_id, {}).get("output", "")

        fatal_markers = (
            "unbound variable",
            "[ERROR]",
            "Traceback (most recent call last)",
            "mysqldump:",
            "URL replace failed",
        )
        has_fatal_output = any(marker in current_output for marker in fatal_markers)

        if rc == 0 and not has_fatal_output:
            update_job(job_id, status="success", exit_code=0)
            append_job_output(job_id, f"[{now_iso()}] Job finished successfully (rc=0).\n")
        else:
            effective_rc = rc if rc != 0 else 1
            update_job(job_id, status="failed", exit_code=effective_rc)
            if has_fatal_output and rc == 0:
                append_job_output(
                    job_id,
                    f"[{now_iso()}] Job marked as failed due to fatal error markers in output (rc=0).\n",
                )
            append_job_output(job_id, f"[{now_iso()}] Job failed with exit code {effective_rc}.\n")
    except Exception as exc:
        update_job(job_id, status="failed", exit_code=1)
        append_job_output(job_id, f"[{now_iso()}] Unexpected error: {exc}\n")


def run_plugin_job(
    job_id: str,
    temp_dir: str,
    zip_path_str: str,
    plugin_type: str,
    plugin_folder_name: str,
    selected_servers: list,
) -> None:
    import shutil as _shutil
    zip_path = Path(zip_path_str)
    try:
        update_job(job_id, status="running")
        total = len(selected_servers)
        append_job_output(
            job_id,
            f"[{now_iso()}] Iniciando instalacion de plugin...\n"
            f"  Plugin: {plugin_folder_name} (ZIP: {zip_path.name})\n"
            f"  Tipo: {plugin_type}\n"
            f"  Plataformas: {total}\n\n",
        )

        effective_zip = zip_path
        if plugin_routes.zip_has_backslash_paths(zip_path):
            append_job_output(job_id, f"[{now_iso()}] Normalizando rutas del ZIP para Linux...\n")
            normalized = zip_path.parent / f"normalized_{zip_path.name}"
            plugin_routes.create_normalized_zip_for_linux(zip_path, normalized)
            effective_zip = normalized
            plugin_folder_name = plugin_routes.detect_plugin_folder_name(effective_zip)

        all_results = []
        for i, server in enumerate(selected_servers, 1):
            append_job_output(job_id, f"[{now_iso()}] --- {server['name']} ({i}/{total}) ---\n")
            result = plugin_routes.install_plugin_on_server(
                server, effective_zip, plugin_type, plugin_folder_name,
                on_output=lambda text, jid=job_id: append_job_output(jid, text),
            )
            status_icon = "OK" if result.success else "ERROR"
            append_job_output(
                job_id,
                f"  {result.server_name}: {status_icon}"
                + (f" - {result.error_detail.split(chr(10))[0]}" if result.error_detail else "")
                + "\n\n",
            )
            all_results.append(plugin_routes.serialize_result(result))

        successes = sum(1 for r in all_results if r["success"])
        failures = total - successes
        final_status = "success" if failures == 0 else "failed"
        update_job(
            job_id, status=final_status, exit_code=0 if failures == 0 else 1,
            results=all_results,
        )
        append_job_output(
            job_id,
            f"[{now_iso()}] Finalizado: {successes}/{total} exitosos"
            + (f", {failures} fallidos" if failures else "")
            + ".\n",
        )
    except Exception as exc:
        update_job(job_id, status="failed", exit_code=1)
        append_job_output(job_id, f"[{now_iso()}] Error inesperado: {exc}\n")
    finally:
        _shutil.rmtree(temp_dir, ignore_errors=True)


# --- Auth helpers --------------------------------------------------------

class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _parse_cookie_header(header: Optional[str]) -> Dict[str, str]:
    if not header:
        return {}
    c = cookies.SimpleCookie()
    try:
        c.load(header)
    except cookies.CookieError:
        return {}
    return {k: morsel.value for k, morsel in c.items()}


def _session_cookie_header(token: str, max_age: int) -> str:
    parts = [
        f"{SESSION_COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max_age}",
    ]
    return "; ".join(parts)


def _clear_cookie_header() -> str:
    return f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


class MoodleCloneHandler(BaseHTTPRequestHandler):
    # current authenticated user; populated per-request when needed
    current_user: Optional[Dict[str, Any]] = None
    response_cookies: Optional[str] = None

    def _send_json(self, status: int, payload: Dict[str, Any], send_body: bool = True) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if self.response_cookies:
            self.send_header("Set-Cookie", self.response_cookies)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _send_file(self, filepath: Path, content_type: str, send_body: bool = True) -> None:
        if filepath == INDEX_FILE:
            data = render_index_html()
        else:
            data = filepath.read_bytes()

        # Validators + revalidation. Without an ETag/Last-Modified and without
        # Cache-Control, the HTML shell has nothing a browser can revalidate
        # against, so a heuristically cached copy can be served indefinitely
        # (Safari is notably aggressive here). That makes "did my deploy reach
        # the browser?" unanswerable — exactly the ambiguity that cost hours on
        # 2026-08-11. `no-cache` means "revalidate every time", not "never
        # store", so the ETag still buys us cheap 304s.
        etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
        if self._if_none_match_matches(etag):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache, must-revalidate")
            self.send_header("X-App-Version", APP_VERSION)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        # Lets us confirm from curl or the Network tab which build the browser
        # actually received, without parsing the HTML.
        self.send_header("X-App-Version", APP_VERSION)
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _if_none_match_matches(self, etag: str) -> bool:
        header = self.headers.get("If-None-Match")
        if not header:
            return False
        for candidate in header.split(","):
            candidate = candidate.strip()
            if candidate == "*":
                return True
            # Tolerate weak validators ('W/"abc"') from proxies.
            if candidate.startswith("W/"):
                candidate = candidate[2:]
            if candidate == etag:
                return True
        return False

    def _read_json_body(self) -> Dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            raise ValueError("Body vacío.")
        raw = self.rfile.read(content_length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("JSON inválido.") from exc

    def _load_session_user(self) -> Optional[Dict[str, Any]]:
        cookie_map = _parse_cookie_header(self.headers.get("Cookie"))
        token = cookie_map.get(SESSION_COOKIE_NAME)
        if not token:
            return None
        return db.get_session_user(token)

    def _require_auth(self) -> Dict[str, Any]:
        user = self._load_session_user()
        if not user:
            raise AuthError(401, "No autenticado.")
        self.current_user = user
        return user

    def _require_permission(self, flag: str) -> Dict[str, Any]:
        user = self._require_auth()
        if user.get("is_superadmin"):
            return user
        if not user.get("permissions", {}).get(flag, False):
            raise AuthError(403, f"No tienes permiso para esta acción ({flag}).")
        return user

    # --- HTTP dispatch ---------------------------------------------------

    def do_HEAD(self) -> None:
        try:
            self._dispatch(send_body=False)
        except AuthError as e:
            self._send_json(e.status, {"error": e.message}, send_body=False)
        except Exception as e:
            self._send_json(500, {"error": f"Error interno: {e}"}, send_body=False)

    def do_GET(self) -> None:
        try:
            self._dispatch(send_body=True)
        except AuthError as e:
            self._send_json(e.status, {"error": e.message})
        except Exception as e:
            self._send_json(500, {"error": f"Error interno: {e}"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/auth/login":
                return self._handle_login()
            if parsed.path == "/api/auth/logout":
                return self._handle_logout()
            if parsed.path == "/api/users":
                return self._handle_create_user()
            if parsed.path == "/api/clone":
                return self._handle_clone()
            if parsed.path == "/api/cc/admin/servers":
                return self._handle_cc_create_server()
            m = re.fullmatch(r"/api/cc/servers/(\d+)/categories", parsed.path)
            if m:
                return self._handle_cc_create_category(int(m.group(1)))
            if parsed.path == "/api/cc/copy-course":
                return self._handle_cc_copy_course()
            if parsed.path == "/api/plugin/install":
                return self._handle_plugin_install()
            if parsed.path == "/api/alexia/config":
                return self._handle_alexia_save_config()
            if parsed.path == "/api/alexia/test-connection":
                return self._handle_alexia_test_connection()
            if parsed.path == "/api/alexia/search-courses":
                return self._handle_alexia_search_courses()
            if parsed.path == "/api/alexia/export":
                return self._handle_alexia_export()
            if parsed.path == "/api/alexia/upload-excel":
                return self._handle_alexia_upload_excel()
            if parsed.path == "/api/alexia/export-batch":
                return self._handle_alexia_export_batch()
            self._send_json(404, {"error": "Not found"})
        except AuthError as e:
            self._send_json(e.status, {"error": e.message})
        except course_routes.CourseRouteError as e:
            self._send_json(e.status, {"error": e.message})
        except alexia_routes.AlexiaRouteError as e:
            self._send_json(e.status, {"error": e.message})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            self._send_json(500, {"error": f"Error interno: {e}"})

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        try:
            m = re.fullmatch(r"/api/cc/admin/servers/(\d+)", parsed.path)
            if m:
                return self._handle_cc_update_server(int(m.group(1)))
            self._send_json(404, {"error": "Not found"})
        except AuthError as e:
            self._send_json(e.status, {"error": e.message})
        except course_routes.CourseRouteError as e:
            self._send_json(e.status, {"error": e.message})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            self._send_json(500, {"error": f"Error interno: {e}"})

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        try:
            m = re.fullmatch(r"/api/users/(\d+)", parsed.path)
            if m:
                return self._handle_update_user(int(m.group(1)))
            self._send_json(404, {"error": "Not found"})
        except AuthError as e:
            self._send_json(e.status, {"error": e.message})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            self._send_json(500, {"error": f"Error interno: {e}"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            m = re.fullmatch(r"/api/users/(\d+)", parsed.path)
            if m:
                return self._handle_delete_user(int(m.group(1)))
            m = re.fullmatch(r"/api/cc/admin/servers/(\d+)", parsed.path)
            if m:
                return self._handle_cc_delete_server(int(m.group(1)))
            self._send_json(404, {"error": "Not found"})
        except AuthError as e:
            self._send_json(e.status, {"error": e.message})
        except course_routes.CourseRouteError as e:
            self._send_json(e.status, {"error": e.message})
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:
            self._send_json(500, {"error": f"Error interno: {e}"})

    def _dispatch(self, send_body: bool) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        # Public: index, health, version
        if path in ("/", "/index.html"):
            self._send_file(INDEX_FILE, "text/html; charset=utf-8", send_body=send_body)
            return

        if path == "/assets/logo.png":
            self._send_file(LOGO_FILE, "image/png", send_body=send_body)
            return

        if path == "/api/health":
            self._send_json(200, {"ok": True, "time": now_iso()}, send_body=send_body)
            return

        if path == "/api/version":
            self._send_json(200, {"version": APP_VERSION}, send_body=send_body)
            return

        # Auth status
        if path == "/api/auth/me":
            user = self._load_session_user()
            if not user:
                self._send_json(200, {"authenticated": False}, send_body=send_body)
            else:
                self._send_json(200, {"authenticated": True, "user": user}, send_body=send_body)
            return

        # Protected: users
        if path == "/api/users":
            user = self._require_auth()
            if not (user.get("is_superadmin") or user.get("permissions", {}).get("can_manage_users")):
                raise AuthError(403, "Solo administradores con permiso de gestión de usuarios pueden listar usuarios.")
            self._send_json(200, {"users": db.list_users()}, send_body=send_body)
            return

        # Protected: jobs
        if path.startswith("/api/jobs/"):
            job_id = path.split("/")[-1]
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                self._require_auth()
                self._send_json(404, {"error": "Job no encontrado."}, send_body=send_body)
                return
            jtype = job.get("job_type", "clone")
            if jtype == "plugin":
                self._require_permission("can_access_plugin_cloner")
            else:
                self._require_permission("can_access_moodle_cloner")
            with jobs_lock:
                result = {
                    "job_id": job["id"],
                    "job_type": job.get("job_type", "clone"),
                    "status": job["status"],
                    "exit_code": job.get("exit_code"),
                    "created_at": job["created_at"],
                    "updated_at": job["updated_at"],
                    "request": job.get("request_preview", {}),
                    "output": job.get("output", ""),
                    "log_path": job.get("log_path", ""),
                    "results": job.get("results"),
                    "plugin_info": job.get("plugin_info"),
                }
            self._send_json(200, result, send_body=send_body)
            return

        # Protected: course-cloner platform listing (public-shaped, no secrets)
        if path == "/api/cc/servers":
            self._require_permission("can_access_course_cloner")
            try:
                self._send_json(200, course_routes.list_servers(), send_body=send_body)
            except course_routes.CourseRouteError as e:
                self._send_json(e.status, {"error": e.message}, send_body=send_body)
            return

        m = re.fullmatch(r"/api/cc/servers/(\d+)/categories", path)
        if m:
            self._require_permission("can_access_course_cloner")
            try:
                self._send_json(200, course_routes.list_categories(int(m.group(1))), send_body=send_body)
            except course_routes.CourseRouteError as e:
                self._send_json(e.status, {"error": e.message}, send_body=send_body)
            return

        # Protected: plugin-cloner
        if path == "/api/plugin/types":
            self._require_permission("can_access_plugin_cloner")
            self._send_json(200, {"types": plugin_routes.list_plugin_types()}, send_body=send_body)
            return

        if path == "/api/plugin/servers":
            self._require_permission("can_access_plugin_cloner")
            try:
                self._send_json(200, course_routes.list_servers(), send_body=send_body)
            except course_routes.CourseRouteError as e:
                self._send_json(e.status, {"error": e.message}, send_body=send_body)
            return

        # Protected: alexia cloner
        if path == "/api/alexia/config":
            self._require_permission("can_access_alexia_cloner")
            self._send_json(200, alexia_routes.get_config(), send_body=send_body)
            return

        m = re.fullmatch(r"/api/alexia/jobs/([a-f0-9]+)", path)
        if m:
            self._require_permission("can_access_alexia_cloner")
            result = alexia_routes.get_job(m.group(1))
            if not result:
                self._send_json(404, {"error": "Job no encontrado."}, send_body=send_body)
            else:
                self._send_json(200, result, send_body=send_body)
            return

        m = re.fullmatch(r"/api/alexia/jobs/batch/([a-f0-9]+)", path)
        if m:
            self._require_permission("can_access_alexia_cloner")
            result = alexia_routes.get_batch_job(m.group(1))
            if not result:
                self._send_json(404, {"error": "Batch job no encontrado."}, send_body=send_body)
            else:
                self._send_json(200, result, send_body=send_body)
            return

        # Protected: admin-shaped platform listing (superadmin only)
        if path == "/api/cc/admin/servers":
            user = self._require_auth()
            if not user.get("is_superadmin"):
                raise AuthError(403, "Solo un superadministrador puede gestionar plataformas.")
            try:
                self._send_json(200, course_routes.admin_list_servers(), send_body=send_body)
            except course_routes.CourseRouteError as e:
                self._send_json(e.status, {"error": e.message}, send_body=send_body)
            return

        self._send_json(404, {"error": "Not found"}, send_body=send_body)

    # --- Auth endpoints --------------------------------------------------

    def _handle_login(self) -> None:
        payload = self._read_json_body()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        if not username or not password:
            raise ValueError("Usuario y contraseña son obligatorios.")
        user = db.get_user_by_username(username)
        if not user or not db.verify_password(password, user["password_hash"]):
            # Avoid leaking which one failed.
            time.sleep(0.3)
            self._send_json(401, {"error": "Credenciales inválidas."})
            return
        session = db.create_session(user["id"])
        self.response_cookies = _session_cookie_header(session["token"], db.SESSION_TTL_SECONDS)
        # Strip password_hash before returning
        user.pop("password_hash", None)
        self._send_json(200, {"user": user})

    def _handle_logout(self) -> None:
        cookie_map = _parse_cookie_header(self.headers.get("Cookie"))
        token = cookie_map.get(SESSION_COOKIE_NAME)
        if token:
            db.delete_session(token)
        self.response_cookies = _clear_cookie_header()
        self._send_json(200, {"ok": True})

    # --- User CRUD -------------------------------------------------------

    def _can_manage_users(self, user: Dict[str, Any]) -> bool:
        return bool(user.get("is_superadmin") or user.get("permissions", {}).get("can_manage_users"))

    def _handle_create_user(self) -> None:
        actor = self._require_auth()
        if not self._can_manage_users(actor):
            raise AuthError(403, "No tienes permiso para crear usuarios.")
        payload = self._read_json_body()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        is_superadmin = bool(payload.get("is_superadmin", False))
        permissions = payload.get("permissions") or {}

        if not re.fullmatch(r"[A-Za-z0-9._@-]{3,64}", username):
            raise ValueError("Nombre de usuario inválido (3-64 caracteres: letras, números, . _ @ -).")
        if len(password) < 8:
            raise ValueError("La contraseña debe tener al menos 8 caracteres.")
        if is_superadmin and not actor.get("is_superadmin"):
            raise AuthError(403, "Solo un superadministrador puede crear otros superadministradores.")
        if db.get_user_by_username(username):
            raise ValueError("Ya existe un usuario con ese nombre.")

        # Only superadmins may grant permissions. Non-superadmin user-managers
        # create accounts with no permissions; a superadmin must later assign them.
        if actor.get("is_superadmin"):
            clean_perms = {k: bool(permissions.get(k, False)) for k in db.PERMISSION_FLAGS}
        else:
            clean_perms = {k: False for k in db.PERMISSION_FLAGS}

        new_user = db.create_user(
            username=username,
            password=password,
            is_superadmin=is_superadmin,
            permissions=clean_perms,
        )
        self._send_json(201, {"user": new_user})

    def _handle_update_user(self, user_id: int) -> None:
        actor = self._require_auth()
        target = db.get_user(user_id)
        if not target:
            self._send_json(404, {"error": "Usuario no encontrado."})
            return

        payload = self._read_json_body()
        is_self = target["id"] == actor["id"]
        actor_is_super = bool(actor.get("is_superadmin"))
        target_is_super = bool(target.get("is_superadmin"))
        can_manage = self._can_manage_users(actor)

        # Authorization gates.
        if not is_self and not can_manage:
            raise AuthError(403, "No tienes permiso para editar a otros usuarios.")
        # Only superadmins can change a superadmin's password / data.
        if target_is_super and not actor_is_super and not is_self:
            raise AuthError(403, "Solo un superadministrador puede modificar a otro superadministrador.")

        # Parse fields.
        password = payload.get("password")
        if password is not None:
            password = str(password)
            if password and len(password) < 8:
                raise ValueError("La contraseña debe tener al menos 8 caracteres.")
            if not password:
                password = None

        is_superadmin = payload.get("is_superadmin")
        permissions = payload.get("permissions")

        # Field-level restrictions for non-superadmin actors.
        if not actor_is_super:
            # Non-superadmin actors (whether user-manager or just self) can only
            # change their own / others' password — never permissions or role.
            if is_superadmin is not None and bool(is_superadmin) != target_is_super:
                raise AuthError(403, "Solo un superadministrador puede modificar el rol de superadministrador.")
            is_superadmin = None
            if permissions is not None:
                raise AuthError(403, "Solo un superadministrador puede modificar permisos.")
            permissions = None
        else:
            # Superadmin actor: normalize fields, but protect the last superadmin.
            if is_superadmin is not None:
                is_superadmin = bool(is_superadmin)
                if target_is_super and not is_superadmin and db.count_superadmins() <= 1:
                    raise ValueError("No puedes quitar el rol de superadministrador al último superadministrador.")
            if permissions is not None:
                permissions = {
                    k: bool(permissions.get(k, target["permissions"].get(k, False)))
                    for k in db.PERMISSION_FLAGS
                }

        if password is None and is_superadmin is None and permissions is None:
            # Nothing to do — return current state without error.
            self._send_json(200, {"user": target})
            return

        updated = db.update_user(
            user_id,
            password=password,
            is_superadmin=is_superadmin,
            permissions=permissions,
        )
        self._send_json(200, {"user": updated})

    def _handle_delete_user(self, user_id: int) -> None:
        actor = self._require_auth()
        if not self._can_manage_users(actor):
            raise AuthError(403, "No tienes permiso para eliminar usuarios.")
        target = db.get_user(user_id)
        if not target:
            self._send_json(404, {"error": "Usuario no encontrado."})
            return
        if target["id"] == actor["id"]:
            raise ValueError("No puedes eliminar tu propia cuenta.")
        if target["is_superadmin"] and db.count_superadmins() <= 1:
            raise ValueError("No puedes eliminar al último superadministrador.")
        if target["is_superadmin"] and not actor.get("is_superadmin"):
            raise AuthError(403, "Solo un superadministrador puede eliminar a otro superadministrador.")
        db.delete_user(user_id)
        self._send_json(200, {"ok": True})

    # --- Course-cloner endpoints ----------------------------------------

    def _require_superadmin(self) -> Dict[str, Any]:
        user = self._require_auth()
        if not user.get("is_superadmin"):
            raise AuthError(403, "Solo un superadministrador puede realizar esta acción.")
        return user

    def _handle_cc_create_category(self, server_index: int) -> None:
        self._require_permission("can_access_course_cloner")
        payload = self._read_json_body()
        result = course_routes.create_category(server_index, payload)
        self._send_json(200, result)

    def _handle_cc_create_server(self) -> None:
        self._require_superadmin()
        payload = self._read_json_body()
        result = course_routes.admin_create_server(payload)
        self._send_json(201, result)

    def _handle_cc_update_server(self, server_index: int) -> None:
        self._require_superadmin()
        payload = self._read_json_body()
        result = course_routes.admin_update_server(server_index, payload)
        self._send_json(200, result)

    def _handle_cc_delete_server(self, server_index: int) -> None:
        self._require_superadmin()
        result = course_routes.admin_delete_server(server_index)
        self._send_json(200, result)

    def _handle_cc_copy_course(self) -> None:
        self._require_permission("can_access_course_cloner")
        payload = self._read_json_body()
        # The copy itself does SSH/SFTP work that can take minutes; the request
        # blocks until it returns, like the original FastAPI impl. Phase 2 may
        # promote this to the same job-pattern used by /api/clone.
        result = course_routes.copy_course(payload)
        self._send_json(200, result)

    # --- Plugin-install endpoint -----------------------------------------

    def _handle_plugin_install(self) -> None:
        import tempfile as _tempfile
        self._require_permission("can_access_plugin_cloner")

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json(400, {"error": "Se espera multipart/form-data."})
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            self._send_json(400, {"error": "Body vacio."})
            return
        if content_length > MAX_PLUGIN_ZIP_SIZE:
            self._send_json(413, {"error": "El archivo excede el limite de 100 MB."})
            return

        raw_body = self.rfile.read(content_length)
        fields, files = plugin_routes.parse_multipart_form_data(content_type, raw_body)

        if "plugin_zip" not in files:
            self._send_json(400, {"error": "Falta el archivo plugin_zip."})
            return
        filename, zip_data = files["plugin_zip"]
        if not filename.lower().endswith(".zip"):
            self._send_json(400, {"error": "El archivo debe ser un .zip."})
            return

        plugin_type_path = fields.get("plugin_type", "").strip()
        try:
            server_indexes = json.loads(fields.get("server_indexes", "[]"))
            if not isinstance(server_indexes, list):
                raise ValueError
            server_indexes = [int(i) for i in server_indexes]
        except (json.JSONDecodeError, ValueError, TypeError):
            self._send_json(400, {"error": "server_indexes invalido."})
            return

        try:
            _, selected_servers = plugin_routes.validate_install_request(plugin_type_path, server_indexes)
        except course_routes.CourseRouteError as e:
            self._send_json(e.status, {"error": e.message})
            return

        temp_dir = _tempfile.mkdtemp(prefix="moodle_plugin_upload_")
        zip_path = Path(temp_dir) / filename
        zip_path.write_bytes(zip_data)

        try:
            plugin_folder_name = plugin_routes.detect_plugin_folder_name(zip_path)
        except ValueError as e:
            import shutil as _shutil
            _shutil.rmtree(temp_dir, ignore_errors=True)
            self._send_json(400, {"error": str(e)})
            return

        with jobs_lock:
            running_plugin = next(
                (jid for jid, j in jobs.items()
                 if j.get("status") in ("queued", "running")
                 and j.get("job_type") == "plugin"),
                None,
            )
        if running_plugin:
            import shutil as _shutil
            _shutil.rmtree(temp_dir, ignore_errors=True)
            self._send_json(409, {
                "error": "Ya hay una instalacion de plugin en ejecucion.",
                "running_job_id": running_plugin,
            })
            return

        job_id = str(uuid.uuid4())
        now = now_iso()
        server_names = [s["name"] for s in selected_servers]
        with jobs_lock:
            jobs[job_id] = {
                "id": job_id,
                "job_type": "plugin",
                "status": "queued",
                "exit_code": None,
                "output": "",
                "created_at": now,
                "updated_at": now,
                "request_preview": {},
                "plugin_info": {
                    "filename": filename,
                    "plugin_folder": plugin_folder_name,
                    "plugin_type": plugin_type_path,
                    "servers": server_names,
                },
                "results": None,
            }

        worker = threading.Thread(
            target=run_plugin_job,
            args=(job_id, temp_dir, str(zip_path), plugin_type_path,
                  plugin_folder_name, selected_servers),
            daemon=True,
        )
        worker.start()
        self._send_json(202, {"job_id": job_id, "status": "queued"})

    # --- Alexia endpoints -----------------------------------------------

    def _handle_alexia_save_config(self) -> None:
        self._require_permission("can_access_alexia_cloner")
        payload = self._read_json_body()
        self._send_json(200, alexia_routes.save_config(payload))

    def _handle_alexia_test_connection(self) -> None:
        self._require_permission("can_access_alexia_cloner")
        payload = self._read_json_body()
        server_name = str(payload.get("server", "catalejo")).strip()
        try:
            result = alexia_routes.test_connection(server_name)
            self._send_json(200, result)
        except Exception as e:
            self._send_json(200, {"success": False, "error": str(e)})

    def _handle_alexia_search_courses(self) -> None:
        self._require_permission("can_access_alexia_cloner")
        payload = self._read_json_body()
        query = str(payload.get("query", "")).strip()
        try:
            result = alexia_routes.search_courses(query)
            self._send_json(200, result)
        except Exception as e:
            self._send_json(200, {"success": False, "error": str(e)})

    def _handle_alexia_export(self) -> None:
        self._require_permission("can_access_alexia_cloner")
        payload = self._read_json_body()
        course_id = payload.get("course_id")
        form_data = payload.get("form_data", {})
        if not course_id:
            self._send_json(400, {"error": "course_id es requerido"})
            return
        result = alexia_routes.start_export(int(course_id), form_data)
        self._send_json(200, result)

    def _handle_alexia_upload_excel(self) -> None:
        self._require_permission("can_access_alexia_cloner")
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json(400, {"error": "Se espera multipart/form-data."})
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            self._send_json(400, {"error": "Body vacio."})
            return
        raw_body = self.rfile.read(content_length)
        boundary = content_type.split("boundary=")[-1].strip()
        boundary_bytes = f"--{boundary}".encode()
        parts = raw_body.split(boundary_bytes)
        file_data = None
        for part in parts:
            if b"filename=" in part and b".xlsx" in part.lower():
                header_end = part.find(b"\r\n\r\n")
                if header_end != -1:
                    file_data = part[header_end + 4:]
                    if file_data.endswith(b"\r\n"):
                        file_data = file_data[:-2]
                break
        if not file_data:
            self._send_json(400, {"error": "No se encontro archivo .xlsx en la solicitud."})
            return
        try:
            result = alexia_routes.upload_excel(file_data)
            self._send_json(200, result)
        except ValueError as e:
            self._send_json(400, {"error": str(e)})

    def _handle_alexia_export_batch(self) -> None:
        self._require_permission("can_access_alexia_cloner")
        payload = self._read_json_body()
        rows = payload.get("rows", [])
        result = alexia_routes.start_batch(rows)
        self._send_json(200, result)

    # --- Clone endpoint --------------------------------------------------

    def _handle_clone(self) -> None:
        self._require_permission("can_access_moodle_cloner")
        try:
            payload = self._read_json_body()
            validated = validate_payload(payload)

            if not SCRIPT_FILE.exists():
                raise ValueError("No se encontró moodle-clone-web.sh")

            with jobs_lock:
                running_job_id = next(
                    (jid for jid, j in jobs.items()
                     if j.get("status") in ("queued", "running")
                     and j.get("job_type", "clone") == "clone"),
                    None,
                )

            if running_job_id:
                self._send_json(
                    409,
                    {
                        "error": "Ya hay un proceso de clonación en ejecución.",
                        "running_job_id": running_job_id,
                    },
                )
                return

            job_id = str(uuid.uuid4())
            now = now_iso()
            log_path = ""
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                log_path = str(LOG_DIR / f"job-{job_id}.log")
                with open(log_path, "w", encoding="utf-8") as fh:
                    fh.write(f"[{now}] Job {job_id} created. App version {APP_VERSION}.\n")
            except OSError:
                log_path = ""
            with jobs_lock:
                jobs[job_id] = {
                    "id": job_id,
                    "job_type": "clone",
                    "status": "queued",
                    "exit_code": None,
                    "output": "",
                    "created_at": now,
                    "updated_at": now,
                    "request_preview": sanitize_preview(validated),
                    "log_path": log_path,
                }

            worker = threading.Thread(target=run_clone_job, args=(job_id, validated), daemon=True)
            worker.start()

            self._send_json(202, {"job_id": job_id, "status": "queued"})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        # Keep terminal output concise.
        return


def log_boot(message: str) -> None:
    # pm2 captures stdout as a pipe, so Python block-buffers it and short boot
    # messages never reach `pm2 logs`. Flush every line explicitly.
    print(message, flush=True)


def init_app_db() -> None:
    log_boot("Initializing application database...")
    try:
        added = db.init_schema()
        if added:
            log_boot(f"Schema migration: added users columns {', '.join(added)}")
        else:
            log_boot("Schema up to date.")
        seeded = db.seed_initial_admin()
        if seeded:
            log_boot(f"Seeded initial superadmin: {seeded}")
        db.get_or_create_session_secret()
        log_boot("Application database ready.")
    except Exception as exc:
        log_boot(f"WARNING: could not initialize app DB: {type(exc).__name__}: {exc}")
        log_boot("The login/user-management features will not work until this is resolved.")


def main() -> None:
    # Bind the port BEFORE touching Aurora. A slow, unreachable or lock-blocked
    # database used to stall this function before serve_forever(), so nothing
    # was listening and the browser got a blank page with no clue why. Now the
    # login screen always renders and any DB problem surfaces as an API error.
    server = ThreadingHTTPServer((HOST, PORT), MoodleCloneHandler)
    log_boot(f"Moodle Cloner UI available at http://{HOST}:{PORT} (v{APP_VERSION})")

    threading.Thread(target=init_app_db, name="db-init", daemon=True).start()

    server.serve_forever()


if __name__ == "__main__":
    main()
