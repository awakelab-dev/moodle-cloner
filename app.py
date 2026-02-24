#!/usr/bin/env python3
import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from subprocess import PIPE, STDOUT, Popen
from typing import Dict, Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "index.html"
SCRIPT_FILE = ROOT / "moodle-clone-web.sh"

HOST = os.getenv("APP_HOST", "0.0.0.0")
PORT = int(os.getenv("APP_PORT", "8787"))
MAX_LOG_CHARS = 250_000

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


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
    def _send_json(self, status: int, payload: Dict[str, Any], send_body: bool = True) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _send_file(self, filepath: Path, content_type: str, send_body: bool = True) -> None:
        data = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            self._send_file(INDEX_FILE, "text/html; charset=utf-8", send_body=False)
            return

        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path.split("/")[-1]
            with jobs_lock:
                job = jobs.get(job_id)
                if not job:
                    self._send_json(404, {"error": "Job no encontrado."}, send_body=False)
                    return
                result = {
                    "job_id": job["id"],
                    "status": job["status"],
                    "exit_code": job.get("exit_code"),
                    "created_at": job["created_at"],
                    "updated_at": job["updated_at"],
                    "request": job["request_preview"],
                    "output": job.get("output", ""),
                }
            self._send_json(200, result, send_body=False)
            return

        if parsed.path == "/api/health":
            self._send_json(200, {"ok": True, "time": now_iso()}, send_body=False)
            return

        self._send_json(404, {"error": "Not found"}, send_body=False)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            self._send_file(INDEX_FILE, "text/html; charset=utf-8")
            return

        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path.split("/")[-1]
            with jobs_lock:
                job = jobs.get(job_id)
                if not job:
                    self._send_json(404, {"error": "Job no encontrado."})
                    return
                result = {
                    "job_id": job["id"],
                    "status": job["status"],
                    "exit_code": job.get("exit_code"),
                    "created_at": job["created_at"],
                    "updated_at": job["updated_at"],
                    "request": job["request_preview"],
                    "output": job.get("output", ""),
                }
            self._send_json(200, result)
            return

        if parsed.path == "/api/health":
            self._send_json(200, {"ok": True, "time": now_iso()})
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/clone":
            self._send_json(404, {"error": "Not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0:
                raise ValueError("Body vacío.")

            raw = self.rfile.read(content_length)
            payload = json.loads(raw.decode("utf-8"))
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
        except json.JSONDecodeError:
            self._send_json(400, {"error": "JSON inválido."})
        except Exception as exc:
            self._send_json(500, {"error": f"Error interno: {exc}"})

    def log_message(self, format: str, *args: Any) -> None:
        # Keep terminal output concise.
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), MoodleCloneHandler)
    print(f"Moodle Cloner UI available at http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
