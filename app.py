#!/usr/bin/env python3
import json
import hashlib
import hmac
import mimetypes
import os
import re
import secrets
import threading
import time
import uuid
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from subprocess import PIPE, STDOUT, Popen
from typing import Dict, Any, Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "index.html"
LOGIN_FILE = ROOT / "login.html"
SCRIPT_FILE = ROOT / "moodle-clone-web.sh"
ASSETS_DIR = ROOT / "assets"
ENV_FILE = ROOT / ".env"


def load_env_file(env_path: Path) -> None:
    if not env_path.exists() or not env_path.is_file():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ENV_FILE)

HOST = os.getenv("APP_HOST", "0.0.0.0")
PORT = int(os.getenv("APP_PORT", "8787"))
APP_LOGIN_USER = os.getenv("APP_LOGIN_USER", "").strip()
APP_LOGIN_PASSWORD = os.getenv("APP_LOGIN_PASSWORD", "")
APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "")
SESSION_COOKIE_NAME = "moodle_cloner_session"
SESSION_COOKIE_SECURE = os.getenv("APP_SESSION_SECURE", "0").lower() in ("1", "true", "yes", "on")
MAX_LOG_CHARS = 250_000
try:
    SESSION_TTL_SECONDS = max(300, int(os.getenv("APP_SESSION_TTL_SECONDS", "28800")))
except ValueError:
    SESSION_TTL_SECONDS = 28800

SOURCE_INSTANCES: Dict[str, Dict[str, str]] = {
    "base_limpia": {
        "src_dir": "/var/www/moodle_dev",
        "src_data": "/var/moodledata_dev",
        "src_vhost": "/etc/nginx/sites-available/moodle-dev.awakelab.world",
    },
    "digit_institute": {
        "src_dir": "/var/www/moodle_digitinstitute",
        "src_data": "/var/moodledata_digitinstitute",
        "src_vhost": "/etc/nginx/sites-available/moodle_digitinstitute",
    },
    "hoppers": {
        "src_dir": "/var/www/moodle_hoppers",
        "src_data": "/var/moodledata_hoppers",
        "src_vhost": "/etc/nginx/sites-available/campus.hoppers.academy",
    },
    "refactika": {
        "src_dir": "/var/www/moodle_refactika",
        "src_data": "/var/moodledata_refactika",
        "src_vhost": "/etc/nginx/sites-available/campus.refactika.com",
    },
}

jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()
sessions: Dict[str, Dict[str, Any]] = {}
sessions_lock = threading.Lock()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sign_session_id(session_id: str) -> str:
    if not APP_SECRET_KEY:
        return ""
    return hmac.new(APP_SECRET_KEY.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256).hexdigest()


def encode_session_cookie(session_id: str) -> str:
    signature = sign_session_id(session_id)
    if not signature:
        return ""
    return f"{session_id}.{signature}"


def decode_session_cookie(cookie_value: str) -> str:
    if not cookie_value or "." not in cookie_value:
        return ""
    session_id, provided_signature = cookie_value.rsplit(".", 1)
    if not session_id or not provided_signature:
        return ""
    expected_signature = sign_session_id(session_id)
    if not expected_signature or not secrets.compare_digest(provided_signature, expected_signature):
        return ""
    return session_id


def parse_cookie_header(cookie_header: Optional[str]) -> Dict[str, str]:
    if not cookie_header:
        return {}
    jar = cookies.SimpleCookie()
    try:
        jar.load(cookie_header)
    except Exception:
        return {}
    return {k: v.value for k, v in jar.items()}


def extract_session_id_from_headers(headers: Any) -> str:
    cookie_map = parse_cookie_header(headers.get("Cookie"))
    raw_cookie = cookie_map.get(SESSION_COOKIE_NAME, "")
    return decode_session_cookie(raw_cookie)


def create_session(username: str) -> str:
    session_id = secrets.token_urlsafe(32)
    now_ts = time.time()
    with sessions_lock:
        sessions[session_id] = {
            "username": username,
            "created_at": now_ts,
            "last_seen": now_ts,
        }
    return session_id


def delete_session(session_id: str) -> None:
    if not session_id:
        return
    with sessions_lock:
        sessions.pop(session_id, None)


def get_authenticated_user(headers: Any) -> str:
    session_id = extract_session_id_from_headers(headers)
    if not session_id:
        return ""

    now_ts = time.time()
    with sessions_lock:
        session = sessions.get(session_id)
        if not session:
            return ""

        last_seen = float(session.get("last_seen", now_ts))
        if now_ts - last_seen > SESSION_TTL_SECONDS:
            sessions.pop(session_id, None)
            return ""

        session["last_seen"] = now_ts
        return str(session.get("username", ""))


def build_set_session_cookie_header(session_id: str) -> str:
    value = encode_session_cookie(session_id)
    jar = cookies.SimpleCookie()
    jar[SESSION_COOKIE_NAME] = value
    morsel = jar[SESSION_COOKIE_NAME]
    morsel["path"] = "/"
    morsel["httponly"] = True
    morsel["samesite"] = "Lax"
    morsel["max-age"] = str(SESSION_TTL_SECONDS)
    if SESSION_COOKIE_SECURE:
        morsel["secure"] = True
    return morsel.OutputString()


def build_clear_session_cookie_header() -> str:
    jar = cookies.SimpleCookie()
    jar[SESSION_COOKIE_NAME] = ""
    morsel = jar[SESSION_COOKIE_NAME]
    morsel["path"] = "/"
    morsel["httponly"] = True
    morsel["samesite"] = "Lax"
    morsel["max-age"] = "0"
    if SESSION_COOKIE_SECURE:
        morsel["secure"] = True
    return morsel.OutputString()


def bool_to_env(value: Any) -> str:
    return "1" if bool(value) else "0"


def is_safe_path(value: str) -> bool:
    return bool(re.fullmatch(r"/[A-Za-z0-9_./-]+", value))


def sanitize_preview(payload: Dict[str, Any]) -> Dict[str, Any]:
    clean = dict(payload)
    if "db_pass" in clean:
        clean["db_pass"] = "***"
    return clean


def validate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Payload inválido.")

    source_instance = str(payload.get("source_instance", "")).strip()
    new_key = str(payload.get("new_key", "")).strip()
    new_domain = str(payload.get("new_domain", "")).strip()
    new_url = str(payload.get("new_url", "")).strip()
    dest_dir = str(payload.get("dest_dir", "")).strip()
    dest_data = str(payload.get("dest_data", "")).strip()
    db_host = str(payload.get("db_host", "")).strip()
    db_user = str(payload.get("db_user", "")).strip()
    db_pass = str(payload.get("db_pass", ""))
    dest_db = str(payload.get("dest_db", "")).strip()

    if source_instance not in SOURCE_INSTANCES:
        raise ValueError("Instancia origen no válida.")

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

    if not re.fullmatch(r"[A-Za-z0-9._-]+", db_host):
        raise ValueError("Host DB inválido.")

    if not re.fullmatch(r"[A-Za-z0-9._$-]+", db_user):
        raise ValueError("Usuario DB inválido.")

    if not db_pass:
        raise ValueError("La contraseña de BD es obligatoria.")

    if not re.fullmatch(r"[A-Za-z0-9_]+", dest_db):
        raise ValueError("Nombre de BD destino inválido (solo letras, números y _).")

    validated = {
        "source_instance": source_instance,
        "new_key": new_key,
        "new_domain": new_domain,
        "new_url": new_url,
        "dest_dir": dest_dir,
        "dest_data": dest_data,
        "db_host": db_host,
        "db_user": db_user,
        "db_pass": db_pass,
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


def update_job(job_id: str, **changes: Any) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job.update(changes)
        job["updated_at"] = now_iso()


def run_clone_job(job_id: str, payload: Dict[str, Any]) -> None:
    source_cfg = SOURCE_INSTANCES[payload["source_instance"]]

    env = os.environ.copy()
    env.update(
        {
            "SRC_DIR": source_cfg["src_dir"],
            "SRC_DATA": source_cfg["src_data"],
            "SRC_VHOST": source_cfg["src_vhost"],
            "NEW_KEY": payload["new_key"],
            "NEW_DOMAIN": payload["new_domain"],
            "NEW_URL": payload["new_url"],
            "DEST_DIR": payload["dest_dir"],
            "DEST_DATA": payload["dest_data"],
            "DEST_DB": payload["dest_db"],
            "DB_HOST": payload["db_host"],
            "DB_USER": payload["db_user"],
            "DB_PASS": payload["db_pass"],
            "ENABLE_SRC_MAINT": bool_to_env(payload["maintenance_source"]),
            "ENABLE_REPLACE": bool_to_env(payload["opt_replace"]),
            "ENABLE_PURGE": bool_to_env(payload["opt_purge"]),
            "ENABLE_NGINX": bool_to_env(payload["opt_nginx"]),
            "ENABLE_CERTBOT": bool_to_env(payload["opt_certbot"]),
            "ENABLE_CRON": bool_to_env(payload["opt_cron"]),
            "DISABLE_NEW_MAINT": "1",
            "DISABLE_SRC_MAINT_AFTER": "1",
            "DRY_RUN": bool_to_env(payload["dry_run"]),
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
            bufsize=1,
        )

        assert process.stdout is not None
        for line in process.stdout:
            append_job_output(job_id, line)

        rc = process.wait()
        if rc == 0:
            update_job(job_id, status="success", exit_code=0)
            append_job_output(job_id, f"[{now_iso()}] Job finished successfully.\n")
        else:
            update_job(job_id, status="failed", exit_code=rc)
            append_job_output(job_id, f"[{now_iso()}] Job failed with exit code {rc}.\n")
    except Exception as exc:
        update_job(job_id, status="failed", exit_code=1)
        append_job_output(job_id, f"[{now_iso()}] Unexpected error: {exc}\n")


class MoodleCloneHandler(BaseHTTPRequestHandler):
    def _send_json(
        self,
        status: int,
        payload: Dict[str, Any],
        send_body: bool = True,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _send_file(
        self,
        filepath: Path,
        content_type: str,
        send_body: bool = True,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        data = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _redirect(self, location: str, extra_headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()

    def _serve_static_asset(self, request_path: str, send_body: bool = True) -> bool:
        if not request_path.startswith("/assets/"):
            return False

        rel = request_path[len("/assets/") :].strip("/")
        if not rel:
            self._send_json(404, {"error": "Not found"}, send_body=send_body)
            return True

        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            self._send_json(404, {"error": "Not found"}, send_body=send_body)
            return True

        assets_root = ASSETS_DIR.resolve()
        target = (ASSETS_DIR / rel_path).resolve()
        if assets_root not in target.parents and target != assets_root:
            self._send_json(404, {"error": "Not found"}, send_body=send_body)
            return True

        if not target.exists() or not target.is_file():
            self._send_json(404, {"error": "Not found"}, send_body=send_body)
            return True

        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send_file(target, content_type, send_body=send_body)
        return True

    def _current_user(self) -> str:
        return get_authenticated_user(self.headers)

    def _require_auth(self, send_body: bool = True) -> str:
        user = self._current_user()
        if user:
            return user
        self._send_json(401, {"error": "No autenticado."}, send_body=send_body)
        return ""

    def _read_json_body(self) -> Dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0:
            raise ValueError("Body vacío.")
        raw = self.rfile.read(content_length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("JSON inválido.") from exc

    def _build_job_response(self, job_id: str) -> Dict[str, Any]:
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                raise ValueError("Job no encontrado.")
            return {
                "job_id": job["id"],
                "status": job["status"],
                "exit_code": job.get("exit_code"),
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
                "request": job["request_preview"],
                "output": job.get("output", ""),
            }

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        current_user = self._current_user()

        if parsed.path in ("/login", "/login.html"):
            if current_user:
                self._redirect("/")
                return
            self._send_file(LOGIN_FILE, "text/html; charset=utf-8", send_body=False)
            return

        if parsed.path in ("/", "/index.html"):
            if not current_user:
                self._redirect("/login")
                return
            self._send_file(INDEX_FILE, "text/html; charset=utf-8", send_body=False)
            return

        if self._serve_static_asset(parsed.path, send_body=False):
            return

        if parsed.path == "/api/session":
            if current_user:
                self._send_json(
                    200,
                    {"authenticated": True, "username": current_user},
                    send_body=False,
                )
            else:
                self._send_json(401, {"authenticated": False}, send_body=False)
            return

        if parsed.path.startswith("/api/jobs/"):
            if not self._require_auth(send_body=False):
                return
            job_id = parsed.path.split("/")[-1]
            try:
                self._send_json(200, self._build_job_response(job_id), send_body=False)
            except ValueError as exc:
                self._send_json(404, {"error": str(exc)}, send_body=False)
            return

        if parsed.path == "/api/health":
            self._send_json(200, {"ok": True, "time": now_iso()}, send_body=False)
            return

        self._send_json(404, {"error": "Not found"}, send_body=False)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        current_user = self._current_user()

        if parsed.path in ("/login", "/login.html"):
            if current_user:
                self._redirect("/")
                return
            self._send_file(LOGIN_FILE, "text/html; charset=utf-8")
            return

        if parsed.path in ("/", "/index.html"):
            if not current_user:
                self._redirect("/login")
                return
            self._send_file(INDEX_FILE, "text/html; charset=utf-8")
            return

        if self._serve_static_asset(parsed.path):
            return

        if parsed.path == "/api/session":
            if current_user:
                self._send_json(200, {"authenticated": True, "username": current_user})
            else:
                self._send_json(401, {"authenticated": False})
            return

        if parsed.path.startswith("/api/jobs/"):
            if not self._require_auth():
                return
            job_id = parsed.path.split("/")[-1]
            try:
                self._send_json(200, self._build_job_response(job_id))
            except ValueError as exc:
                self._send_json(404, {"error": str(exc)})
            return

        if parsed.path == "/api/health":
            self._send_json(200, {"ok": True, "time": now_iso()})
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/login":
            try:
                if not APP_LOGIN_USER or not APP_LOGIN_PASSWORD:
                    raise RuntimeError("No hay credenciales configuradas en .env.")
                payload = self._read_json_body()
                username = str(payload.get("username", "")).strip()
                password = str(payload.get("password", ""))

                if not username or not password:
                    raise ValueError("Usuario y contraseña son obligatorios.")

                if not (
                    secrets.compare_digest(username, APP_LOGIN_USER)
                    and secrets.compare_digest(password, APP_LOGIN_PASSWORD)
                ):
                    time.sleep(0.2)
                    self._send_json(401, {"error": "Credenciales inválidas."})
                    return

                session_id = create_session(username)
                self._send_json(
                    200,
                    {"ok": True, "username": username},
                    extra_headers={"Set-Cookie": build_set_session_cookie_header(session_id)},
                )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except RuntimeError as exc:
                self._send_json(500, {"error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"error": f"Error interno: {exc}"})
            return

        if parsed.path == "/api/logout":
            session_id = extract_session_id_from_headers(self.headers)
            delete_session(session_id)
            self._send_json(
                200,
                {"ok": True},
                extra_headers={"Set-Cookie": build_clear_session_cookie_header()},
            )
            return

        if parsed.path != "/api/clone":
            self._send_json(404, {"error": "Not found"})
            return

        try:
            if not self._require_auth():
                return

            payload = self._read_json_body()
            validated = validate_payload(payload)

            if not SCRIPT_FILE.exists():
                raise ValueError("No se encontró moodle-clone-web.sh")

            with jobs_lock:
                running_job_id = next(
                    (jid for jid, j in jobs.items() if j.get("status") in ("queued", "running")),
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
            with jobs_lock:
                jobs[job_id] = {
                    "id": job_id,
                    "status": "queued",
                    "exit_code": None,
                    "output": "",
                    "created_at": now,
                    "updated_at": now,
                    "request_preview": sanitize_preview(validated),
                }

            worker = threading.Thread(target=run_clone_job, args=(job_id, validated), daemon=True)
            worker.start()
            self._send_json(202, {"job_id": job_id, "status": "queued"})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": f"Error interno: {exc}"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    if not APP_LOGIN_USER or not APP_LOGIN_PASSWORD or not APP_SECRET_KEY:
        raise RuntimeError("Debes configurar APP_LOGIN_USER, APP_LOGIN_PASSWORD y APP_SECRET_KEY en .env")
    server = ThreadingHTTPServer((HOST, PORT), MoodleCloneHandler)
    print(f"Moodle Cloner UI available at http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
