"""Plugin installer web adapter.

Ports the SSH-based plugin installation flow from
moodle_plugin_installer.py for web use within the clonador platform.
"""
from __future__ import annotations

import posixpath
import re
import shlex
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import paramiko

from course_routes import CourseRouteError, _load_servers, _select_server

REMOTE_TMP_DIR = "/tmp"

OFFICIAL_PLUGIN_TYPES: List[Tuple[str, str]] = [
    ("mod", "mod"),
    ("antivirus", "lib/antivirus"),
    ("assignsubmission", "mod/assign/submission"),
    ("assignfeedback", "mod/assign/feedback"),
    ("booktool", "mod/book/tool"),
    ("customfield", "customfield/field"),
    ("datafield", "mod/data/field"),
    ("datapreset", "mod/data/preset"),
    ("ltisource", "mod/lti/source"),
    ("fileconverter", "files/converter"),
    ("ltiservice", "mod/lti/service"),
    ("mlbackend", "lib/mlbackend"),
    ("forumreport", "mod/forum/report"),
    ("quiz", "mod/quiz/report"),
    ("quizaccess", "mod/quiz/accessrule"),
    ("scormreport", "mod/scorm/report"),
    ("workshopform", "mod/workshop/form"),
    ("workshopallocation", "mod/workshop/allocation"),
    ("workshopeval", "mod/workshop/eval"),
    ("block", "blocks"),
    ("qtype", "question/type"),
    ("qbehaviour", "question/behaviour"),
    ("qformat", "question/format"),
    ("filter", "filter"),
    ("editor", "lib/editor"),
    ("atto", "lib/editor/atto/plugins"),
    ("enrol", "enrol"),
    ("auth", "auth"),
    ("tool", "admin/tool"),
    ("logstore", "admin/tool/log/store"),
    ("availability", "availability/condition"),
    ("calendartype", "calendar/type"),
    ("message", "message/output"),
    ("format", "course/format"),
    ("dataformat", "dataformat"),
    ("profilefield", "user/profile/field"),
    ("report", "report"),
    ("coursereport", "course/report"),
    ("gradeexport", "grade/export"),
    ("gradeimport", "grade/import"),
    ("gradereport", "grade/report"),
    ("gradingform", "grade/grading/form"),
    ("mnetservice", "mnet/service"),
    ("webservice", "webservice"),
    ("repository", "repository"),
    ("portfolio", "portfolio"),
    ("search", "search/engine"),
    ("media", "media/player"),
    ("plagiarism", "plagiarism"),
    ("cachestore", "cache/stores"),
    ("cachelock", "cache/locks"),
    ("theme", "theme"),
    ("local", "local"),
    ("contenttype", "contentbank/contenttype"),
    ("h5plib", "h5p/h5plib"),
    ("qbank", "question/bank"),
]
PLUGIN_TYPE_TO_PATH: Dict[str, str] = dict(OFFICIAL_PLUGIN_TYPES)


@dataclass
class CommandLog:
    step: str
    command: str
    exit_status: int
    stdout: str
    stderr: str


@dataclass
class PluginServerResult:
    server_name: str
    success: bool = False
    error_detail: str = ""
    command_logs: List[CommandLog] = field(default_factory=list)


class RemoteCommandError(RuntimeError):
    def __init__(self, log: CommandLog):
        self.log = log
        stderr_text = log.stderr.rstrip("\n")
        stdout_text = log.stdout.rstrip("\n")
        detail = (
            stderr_text
            if stderr_text
            else (stdout_text if stdout_text else f"exit status {log.exit_status}")
        )
        super().__init__(f"{log.step}: {detail}")


def _q(value: str) -> str:
    return shlex.quote(value)


# --- ZIP helpers ---------------------------------------------------------

def detect_plugin_folder_name(zip_path: Path) -> str:
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            top_level_items: set[str] = set()
            for member in archive.namelist():
                clean = member.lstrip("/").strip()
                if not clean or clean.startswith("__MACOSX/"):
                    continue
                top_level_items.add(clean.split("/", 1)[0])
            if len(top_level_items) == 1:
                return top_level_items.pop()
    except zipfile.BadZipFile as exc:
        raise ValueError("El archivo no es un ZIP valido.") from exc
    return zip_path.stem


def infer_plugin_type(plugin_folder: str, zip_stem: str) -> Optional[str]:
    prefix_to_type = {f"{pt}_": pt for pt in PLUGIN_TYPE_TO_PATH}
    for token in (plugin_folder.lower(), zip_stem.lower()):
        for prefix, pt in prefix_to_type.items():
            if token.startswith(prefix):
                return PLUGIN_TYPE_TO_PATH[pt]
    return None


def zip_has_backslash_paths(zip_path: Path) -> bool:
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            return any(
                "\\" in getattr(item, "orig_filename", item.filename)
                for item in archive.infolist()
            )
    except zipfile.BadZipFile:
        return False


def _normalize_zip_member_name(name: str) -> str:
    normalized = name.replace("\\", "/").strip().lstrip("/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def create_normalized_zip_for_linux(source_zip: Path, destination_zip: Path) -> None:
    destination_zip.parent.mkdir(parents=True, exist_ok=True)
    seen_names: set[str] = set()
    with zipfile.ZipFile(source_zip, "r") as src:
        with zipfile.ZipFile(destination_zip, "w") as dst:
            for item in src.infolist():
                original_name = getattr(item, "orig_filename", item.filename)
                normalized_name = _normalize_zip_member_name(original_name)
                if not normalized_name:
                    continue
                is_dir = item.is_dir() or normalized_name.endswith("/")
                if is_dir and not normalized_name.endswith("/"):
                    normalized_name += "/"
                if normalized_name in seen_names:
                    raise ValueError(
                        f"ZIP con rutas duplicadas tras normalizar: {normalized_name}"
                    )
                seen_names.add(normalized_name)
                new_item = zipfile.ZipInfo(normalized_name, date_time=item.date_time)
                new_item.comment = item.comment
                new_item.extra = item.extra
                new_item.internal_attr = item.internal_attr
                new_item.compress_type = item.compress_type
                original_mode = (item.external_attr >> 16) & 0o7777
                mode = original_mode if original_mode else (0o755 if is_dir else 0o644)
                if is_dir and not (mode & 0o111):
                    mode |= (mode & 0o444) >> 2
                file_type = 0o040000 if is_dir else 0o100000
                new_item.create_system = 3  # UNIX
                new_item.external_attr = (file_type | mode) << 16
                if is_dir:
                    dst.writestr(new_item, b"", compress_type=item.compress_type)
                    continue
                with src.open(item, "r") as sf, dst.open(new_item, "w") as df:
                    shutil.copyfileobj(sf, df)


# --- SSH helpers ---------------------------------------------------------

def _connect_ssh(server: Dict[str, Any]) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: Dict[str, Any] = {
        "hostname": server["host"],
        "port": int(server.get("port", 22)),
        "username": server["ssh_user"],
        "timeout": 20,
        "banner_timeout": 20,
        "auth_timeout": 20,
    }
    if server.get("ssh_key_path"):
        kwargs["key_filename"] = server["ssh_key_path"]
    if server.get("ssh_key_passphrase"):
        kwargs["passphrase"] = server["ssh_key_passphrase"]
    if server.get("ssh_password"):
        kwargs["password"] = server["ssh_password"]
    client.connect(**kwargs)
    return client


def _run_remote(
    ssh: paramiko.SSHClient,
    command: str,
    *,
    sudo_password: Optional[str] = None,
    timeout: int = 1800,
) -> Tuple[int, str, str]:
    prepared = command
    use_sudo_pw = bool(sudo_password) and command.lstrip().startswith("sudo ")
    if use_sudo_pw:
        prepared = command.replace("sudo ", "sudo -S -p '' ", 1)
    stdin, stdout, stderr = ssh.exec_command(
        prepared, get_pty=use_sudo_pw, timeout=timeout,
    )
    if use_sudo_pw:
        stdin.write(f"{sudo_password}\n")
        stdin.flush()
    stdout_text = stdout.read().decode("utf-8", errors="replace")
    stderr_text = stderr.read().decode("utf-8", errors="replace")
    exit_status = stdout.channel.recv_exit_status()
    return exit_status, stdout_text, stderr_text


def _exec(
    result: PluginServerResult,
    ssh: paramiko.SSHClient,
    *,
    step: str,
    command: str,
    sudo_password: Optional[str] = None,
    timeout: int = 1800,
    fail_on_error: bool = True,
) -> CommandLog:
    exit_status, stdout_text, stderr_text = _run_remote(
        ssh, command, sudo_password=sudo_password, timeout=timeout,
    )
    log = CommandLog(
        step=step, command=command, exit_status=exit_status,
        stdout=stdout_text, stderr=stderr_text,
    )
    result.command_logs.append(log)
    if fail_on_error and exit_status != 0:
        raise RemoteCommandError(log)
    return log


def _safe_cleanup(
    result: PluginServerResult,
    ssh: paramiko.SSHClient,
    *,
    step: str,
    command: str,
    cleanup_errors: List[str],
    sudo_password: Optional[str] = None,
) -> None:
    try:
        log = _exec(
            result, ssh, step=step, command=command,
            sudo_password=sudo_password, fail_on_error=False,
        )
        if log.exit_status != 0:
            detail = (
                log.stderr.rstrip("\n") or log.stdout.rstrip("\n")
                or f"exit status {log.exit_status}"
            )
            cleanup_errors.append(f"{step}: {detail}")
    except Exception as exc:
        cleanup_errors.append(f"{step}: {exc}")


# --- Core install flow ---------------------------------------------------

def install_plugin_on_server(
    server: Dict[str, Any],
    local_zip_path: Path,
    plugin_type: str,
    plugin_folder_name: str,
    *,
    on_output: Optional[Callable[[str], None]] = None,
) -> PluginServerResult:
    result = PluginServerResult(server_name=server["name"])
    emit = on_output or (lambda _: None)

    moodle_path = str(server["moodle_path"]).rstrip("/")
    web_user = str(server["web_user"])
    web_group = str(server.get("web_group") or web_user)
    sudo_password = (
        server.get("sudo_password") if server.get("sudo_requires_password") else None
    )

    target_dir = posixpath.join(moodle_path, plugin_type)
    plugin_target_dir = posixpath.join(target_dir, plugin_folder_name)
    remote_zip = posixpath.join(
        REMOTE_TMP_DIR, f"{uuid.uuid4().hex}_{local_zip_path.name}",
    )
    upgrade_script = posixpath.join(moodle_path, "admin", "cli", "upgrade.php")
    purge_script = posixpath.join(moodle_path, "admin", "cli", "purge_caches.php")

    ssh: Optional[paramiko.SSHClient] = None
    sftp: Optional[paramiko.SFTPClient] = None
    original_uid_gid: Optional[str] = None
    original_mode: Optional[str] = None
    cleanup_errors: List[str] = []

    try:
        emit(f"  {result.server_name}: conectando por SSH...\n")
        ssh = _connect_ssh(server)
        sftp = ssh.open_sftp()

        emit(f"  {result.server_name}: Paso 1/5 (capturar permisos + abrir escritura)\n")
        stat_log = _exec(
            result, ssh,
            step="Paso 1 - Capturar owner/permisos originales",
            command=f"sudo stat -c '%u:%g %a' {_q(target_dir)}",
            sudo_password=sudo_password,
        )
        stat_parts = stat_log.stdout.strip().split()
        if len(stat_parts) != 2:
            raise RuntimeError(
                f"No se pudo interpretar permisos de {target_dir}: "
                f"{stat_log.stdout.strip() or '[sin salida]'}"
            )
        original_uid_gid, original_mode = stat_parts
        _exec(
            result, ssh,
            step="Paso 1 - Dar escritura temporal",
            command=f"sudo chmod u+w {_q(target_dir)}",
            sudo_password=sudo_password,
        )

        emit(f"  {result.server_name}: Paso 2/5 (subir ZIP + descomprimir)\n")
        sftp.put(str(local_zip_path), remote_zip)
        _exec(
            result, ssh,
            step="Paso 2 - Descomprimir ZIP",
            command=f"sudo unzip -o {_q(remote_zip)} -d {_q(target_dir)}",
            sudo_password=sudo_password,
        )
        _exec(
            result, ssh,
            step="Paso 2 - Verificar carpeta plugin",
            command=f"sudo test -d {_q(plugin_target_dir)}",
            sudo_password=sudo_password,
        )
        _exec(
            result, ssh,
            step="Paso 2 - Ajustar owner plugin",
            command=f"sudo chown -R {_q(web_user)}:{_q(web_group)} {_q(plugin_target_dir)}",
            sudo_password=sudo_password,
        )
        _exec(
            result, ssh,
            step="Paso 2 - Permisos carpetas (755)",
            command=f"sudo find {_q(plugin_target_dir)} -type d -exec chmod 755 {{}} \\;",
            sudo_password=sudo_password,
        )
        _exec(
            result, ssh,
            step="Paso 2 - Permisos archivos (644)",
            command=f"sudo find {_q(plugin_target_dir)} -type f -exec chmod 644 {{}} \\;",
            sudo_password=sudo_password,
        )

        emit(f"  {result.server_name}: Paso 3/5 (upgrade.php)\n")
        _exec(
            result, ssh,
            step="Paso 3 - Ejecutar upgrade.php",
            command=f"sudo -u {_q(web_user)} php {_q(upgrade_script)} --non-interactive",
            sudo_password=sudo_password,
            timeout=3600,
        )

        emit(f"  {result.server_name}: Paso 4/5 (purge_caches.php)\n")
        _exec(
            result, ssh,
            step="Paso 4 - Ejecutar purge_caches.php",
            command=f"sudo -u {_q(web_user)} php {_q(purge_script)}",
            sudo_password=sudo_password,
            timeout=1800,
        )

    except RemoteCommandError as exc:
        exact_stderr = exc.log.stderr.rstrip("\n")
        fallback = exc.log.stdout.rstrip("\n")
        result.error_detail = exact_stderr if exact_stderr else (fallback or str(exc))
    except Exception as exc:
        result.error_detail = str(exc)
    finally:
        if ssh is not None:
            emit(f"  {result.server_name}: Paso 5/5 (restaurar permisos)\n")

            check = _exec(
                result, ssh,
                step="Paso 5 - Verificar carpeta plugin",
                command=f"sudo test -d {_q(plugin_target_dir)}",
                sudo_password=sudo_password,
                fail_on_error=False,
            )
            if check.exit_status == 0:
                _safe_cleanup(
                    result, ssh,
                    step="Paso 5 - Restaurar owner plugin",
                    command=f"sudo chown -R {_q(web_user)}:{_q(web_group)} {_q(plugin_target_dir)}",
                    cleanup_errors=cleanup_errors,
                    sudo_password=sudo_password,
                )
                _safe_cleanup(
                    result, ssh,
                    step="Paso 5 - Permisos carpetas plugin (755)",
                    command=f"sudo find {_q(plugin_target_dir)} -type d -exec chmod 755 {{}} \\;",
                    cleanup_errors=cleanup_errors,
                    sudo_password=sudo_password,
                )
                _safe_cleanup(
                    result, ssh,
                    step="Paso 5 - Permisos archivos plugin (644)",
                    command=f"sudo find {_q(plugin_target_dir)} -type f -exec chmod 644 {{}} \\;",
                    cleanup_errors=cleanup_errors,
                    sudo_password=sudo_password,
                )

            if original_uid_gid and original_mode:
                _safe_cleanup(
                    result, ssh,
                    step="Paso 5 - Restaurar owner carpeta destino",
                    command=f"sudo chown {original_uid_gid} {_q(target_dir)}",
                    cleanup_errors=cleanup_errors,
                    sudo_password=sudo_password,
                )
                _safe_cleanup(
                    result, ssh,
                    step="Paso 5 - Restaurar permisos carpeta destino",
                    command=f"sudo chmod {original_mode} {_q(target_dir)}",
                    cleanup_errors=cleanup_errors,
                    sudo_password=sudo_password,
                )

            _safe_cleanup(
                result, ssh,
                step="Paso 5 - Limpiar ZIP temporal",
                command=f"rm -f {_q(remote_zip)}",
                cleanup_errors=cleanup_errors,
                sudo_password=None,
            )

        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass

        if cleanup_errors:
            cleanup_detail = " | ".join(cleanup_errors)
            if result.error_detail:
                result.error_detail = f"{result.error_detail}\n{cleanup_detail}"
            else:
                result.error_detail = cleanup_detail

    result.success = not bool(result.error_detail)
    return result


# --- Route helpers -------------------------------------------------------

def list_plugin_types() -> List[Dict[str, str]]:
    return [
        {"key": key, "path": path}
        for key, path in OFFICIAL_PLUGIN_TYPES
    ]


def validate_install_request(
    plugin_type_path: str,
    server_indexes: List[int],
) -> Tuple[str, List[Dict[str, Any]]]:
    valid_paths = set(PLUGIN_TYPE_TO_PATH.values())
    if plugin_type_path not in valid_paths:
        raise CourseRouteError(400, f"Tipo de plugin invalido: {plugin_type_path}")
    if not server_indexes:
        raise CourseRouteError(400, "Selecciona al menos una plataforma destino.")
    all_servers = _load_servers()
    selected: List[Dict[str, Any]] = []
    for idx in server_indexes:
        selected.append(_select_server(all_servers, idx))
    return plugin_type_path, selected


def serialize_result(result: PluginServerResult) -> Dict[str, Any]:
    return {
        "server_name": result.server_name,
        "success": result.success,
        "error_detail": result.error_detail,
        "command_logs": [
            {
                "step": log.step,
                "command": log.command,
                "exit_status": log.exit_status,
                "stdout": log.stdout,
                "stderr": log.stderr,
            }
            for log in result.command_logs
        ],
    }


# --- Multipart parsing ---------------------------------------------------

def parse_multipart_form_data(
    content_type: str,
    body: bytes,
) -> Tuple[Dict[str, str], Dict[str, Tuple[str, bytes]]]:
    m = re.search(r"boundary=([^\s;]+)", content_type)
    if not m:
        raise ValueError("No se encontro boundary en Content-Type.")
    boundary = m.group(1).strip('"').encode("utf-8")
    delimiter = b"--" + boundary
    parts = body.split(delimiter)
    fields: Dict[str, str] = {}
    files: Dict[str, Tuple[str, bytes]] = {}
    for part in parts[1:]:
        if part.startswith(b"--"):
            break
        if b"\r\n\r\n" not in part:
            continue
        header_block, part_body = part.split(b"\r\n\r\n", 1)
        if part_body.endswith(b"\r\n"):
            part_body = part_body[:-2]
        headers_str = header_block.decode("utf-8", errors="replace")
        name_match = re.search(r'name="([^"]*)"', headers_str)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', headers_str)
        if filename_match:
            files[name] = (filename_match.group(1), part_body)
        else:
            fields[name] = part_body.decode("utf-8", errors="replace")
    return fields, files
