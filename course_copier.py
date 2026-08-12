from __future__ import annotations

import json
import posixpath
import re
import shlex
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import paramiko

INVENTORY_FILE = Path("inventario.json")
REMOTE_TMP_DIR = "/tmp"


@dataclass
class CommandLog:
    step: str
    command: str
    exit_status: int
    stdout: str
    stderr: str


@dataclass
class ServerResult:
    server_name: str
    success: bool = False
    error_detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    command_logs: list[CommandLog] = field(default_factory=list)

@dataclass
class BackupArtifact:
    course_id: int
    category_id: int
    category_name: str
    category_path: list[dict[str, Any]]
    fullname: str
    shortname: str
    local_path: Path
    remote_path: str


class RemoteCommandError(RuntimeError):
    def __init__(self, log: CommandLog):
        self.log = log
        detail = log.stderr.rstrip("\n") or log.stdout.rstrip("\n") or f"exit status {log.exit_status}"
        super().__init__(f"{log.step}: {detail}")


def load_inventory(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"No se encontró el archivo de inventario: {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "servers" in raw:
        servers = raw["servers"]
    elif isinstance(raw, list):
        servers = raw
    else:
        raise ValueError(
            "inventario.json debe ser un array de servidores o un objeto con clave 'servers'."
        )

    if not isinstance(servers, list) or not servers:
        raise ValueError("El inventario está vacío o su formato es inválido.")

    required_fields = ("name", "host", "ssh_user", "moodle_path", "web_user")
    for idx, server in enumerate(servers, start=1):
        if not isinstance(server, dict):
            raise ValueError(f"Entrada inválida en servidor #{idx}: debe ser un objeto JSON.")

        missing = [field_name for field_name in required_fields if not server.get(field_name)]
        if missing:
            raise ValueError(
                f"Servidor #{idx} incompleto. Faltan campos: {', '.join(missing)}"
            )

        server.setdefault("port", 22)
        server.setdefault("web_group", server["web_user"])
        server.setdefault("sudo_requires_password", False)
        server.setdefault("moodledata_path", "")
        server.setdefault("vhost_path", "")
        # URL publica de la plataforma. Solo informativa (la UI la muestra como
        # enlace); las entradas viejas del inventario no la traen.
        server.setdefault("url", "")

    return servers


def copy_course_between_servers(
    source_server: dict[str, Any],
    destination_servers: list[dict[str, Any]],
    course_id: int,
    local_work_dir: Path,
) -> tuple[ServerResult, list[ServerResult]]:
    source_result, backup_artifact = create_course_backup(
        source_server,
        course_id,
        local_work_dir,
    )
    if not source_result.success or backup_artifact is None:
        return source_result, []

    destination_results: list[ServerResult | None] = [None] * len(destination_servers)
    max_workers = max(1, min(len(destination_servers), 20))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(restore_course_on_server, destination_server, backup_artifact): index
            for index, destination_server in enumerate(destination_servers)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            destination_server = destination_servers[index]
            try:
                destination_results[index] = future.result()
            except Exception as exc:
                destination_results[index] = ServerResult(
                    server_name=str(destination_server.get("name") or f"Destino #{index + 1}"),
                    success=False,
                    error_detail=str(exc),
                )
    completed_results = [
        result
        if result is not None
        else ServerResult(
            server_name=str(destination_servers[index].get("name") or f"Destino #{index + 1}"),
            success=False,
            error_detail="El restore no devolvió resultado.",
        )
        for index, result in enumerate(destination_results)
    ]
    return source_result, completed_results


def create_course_backup(
    source_server: dict[str, Any],
    course_id: int,
    local_work_dir: Path,
) -> tuple[ServerResult, BackupArtifact | None]:
    result = ServerResult(server_name=source_server["name"])
    moodle_path = str(source_server["moodle_path"]).rstrip("/")
    web_user = str(source_server["web_user"])
    sudo_password = (
        source_server.get("sudo_password")
        if source_server.get("sudo_requires_password")
        else None
    )
    remote_work_dir = posixpath.join(
        REMOTE_TMP_DIR,
        f"moodle_course_transfer_source_{uuid.uuid4().hex}",
    )
    remote_script_path = posixpath.join(remote_work_dir, "create_backup.php")
    backup_artifact: BackupArtifact | None = None

    ssh: paramiko.SSHClient | None = None
    sftp: paramiko.SFTPClient | None = None
    cleanup_errors: list[str] = []

    try:
        local_work_dir.mkdir(parents=True, exist_ok=True)
        ssh = connect_ssh(source_server)
        sftp = ssh.open_sftp()

        execute_remote(
            result,
            ssh,
            step="Preparar carpeta temporal origen",
            command=f"mkdir -p {q(remote_work_dir)} && chmod 777 {q(remote_work_dir)}",
        )

        with sftp.file(remote_script_path, "w") as remote_file:
            remote_file.write(build_remote_backup_script(moodle_path))
        sftp.chmod(remote_script_path, 0o644)

        log = execute_remote(
            result,
            ssh,
            step="Generar backup del curso origen",
            command=(
                f"sudo -u {q(web_user)} env HOME=/tmp php {q(remote_script_path)} "
                f"--courseid={int(course_id)} --destination={q(remote_work_dir)}"
            ),
            sudo_password=sudo_password,
            timeout=7200,
        )
        payload = parse_json_payload(log.stdout)
        if not payload.get("success"):
            raise RuntimeError(payload.get("error") or "No se pudo generar el backup del curso origen.")

        remote_backup_path = str(payload["backup_path"])
        local_backup_path = local_work_dir / Path(remote_backup_path).name
        sftp.get(remote_backup_path, str(local_backup_path))

        course_data = payload["source_course"]
        category_data = payload.get("source_category") or {}
        category_path = payload.get("source_category_path") or []
        if not isinstance(category_path, list):
            category_path = []
        backup_artifact = BackupArtifact(
            course_id=int(course_data["id"]),
            category_id=int(course_data["category"]),
            category_name=str(category_data.get("name") or f"Categoría {course_data['category']}"),
            category_path=category_path,
            fullname=str(course_data["fullname"]),
            shortname=str(course_data["shortname"]),
            local_path=local_backup_path,
            remote_path=remote_backup_path,
        )
        result.data = {
            "source_course": course_data,
            "source_category": category_data,
            "source_category_path": category_path,
            "backup_file": Path(remote_backup_path).name,
        }
        result.success = True
    except RemoteCommandError as exc:
        result.error_detail = (
            exc.log.stderr.rstrip("\n")
            or exc.log.stdout.rstrip("\n")
            or str(exc)
        )
    except Exception as exc:
        result.error_detail = str(exc)
    finally:
        if ssh is not None:
            safe_cleanup_command(
                result,
                ssh,
                step="Limpiar temporales origen",
                command=f"rm -rf {q(remote_work_dir)}",
                cleanup_errors=cleanup_errors,
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

        result.success = result.success and not bool(result.error_detail)

    return result, backup_artifact


def restore_course_on_server(
    destination_server: dict[str, Any],
    backup_artifact: BackupArtifact,
) -> ServerResult:
    result = ServerResult(server_name=destination_server["name"])
    moodle_path = str(destination_server["moodle_path"]).rstrip("/")
    web_user = str(destination_server["web_user"])
    sudo_password = (
        destination_server.get("sudo_password")
        if destination_server.get("sudo_requires_password")
        else None
    )
    remote_work_dir = posixpath.join(
        REMOTE_TMP_DIR,
        f"moodle_course_transfer_dest_{uuid.uuid4().hex}",
    )
    remote_script_path = posixpath.join(remote_work_dir, "restore_backup.php")
    remote_backup_path = posixpath.join(remote_work_dir, backup_artifact.local_path.name)
    destination_category_id = int(destination_server.get("destination_category_id") or 0)
    raw_destination_category_path = destination_server.get("destination_category_path") or []
    if isinstance(raw_destination_category_path, list):
        destination_category_path = [
            str(item).strip()
            for item in raw_destination_category_path
            if str(item).strip()
        ]
    else:
        destination_category_path = []

    ssh: paramiko.SSHClient | None = None
    sftp: paramiko.SFTPClient | None = None
    cleanup_errors: list[str] = []

    try:
        ssh = connect_ssh(destination_server)
        sftp = ssh.open_sftp()

        execute_remote(
            result,
            ssh,
            step="Preparar carpeta temporal destino",
            command=f"mkdir -p {q(remote_work_dir)} && chmod 777 {q(remote_work_dir)}",
        )

        with sftp.file(remote_script_path, "w") as remote_file:
            remote_file.write(build_remote_restore_script(moodle_path))
        sftp.chmod(remote_script_path, 0o644)
        sftp.put(str(backup_artifact.local_path), remote_backup_path)
        sftp.chmod(remote_backup_path, 0o644)
        category_path_json = json.dumps(
            backup_artifact.category_path,
            ensure_ascii=False,
        )
        destination_category_path_json = json.dumps(
            destination_category_path,
            ensure_ascii=False,
        )

        log = execute_remote(
            result,
            ssh,
            step="Restaurar curso en Moodle destino",
            command=(
                f"sudo -u {q(web_user)} env HOME=/tmp php {q(remote_script_path)} "
                f"--backupfile={q(remote_backup_path)} "
                f"--categoryid={int(backup_artifact.category_id)} "
                f"--destinationcategoryid={destination_category_id} "
                f"--destinationcategorypathjson={q(destination_category_path_json)} "
                f"--categoryname={q(backup_artifact.category_name)} "
                f"--categorypathjson={q(category_path_json)} "
                f"--fullname={q(backup_artifact.fullname)} "
                f"--shortname={q(backup_artifact.shortname)}"
            ),
            sudo_password=sudo_password,
            timeout=7200,
        )
        payload = parse_json_payload(log.stdout)
        if not payload.get("success"):
            raise RuntimeError(payload.get("error") or "No se pudo restaurar el curso en destino.")

        result.data = payload
        result.success = True
    except RemoteCommandError as exc:
        result.error_detail = (
            exc.log.stderr.rstrip("\n")
            or exc.log.stdout.rstrip("\n")
            or str(exc)
        )
    except Exception as exc:
        result.error_detail = str(exc)
    finally:
        if ssh is not None:
            safe_cleanup_command(
                result,
                ssh,
                step="Limpiar temporales destino",
                command=f"rm -rf {q(remote_work_dir)}",
                cleanup_errors=cleanup_errors,
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

        result.success = result.success and not bool(result.error_detail)

    return result


def copy_course_on_server(server: dict[str, Any], course_id: int) -> ServerResult:
    result = ServerResult(server_name=server["name"])
    moodle_path = str(server["moodle_path"]).rstrip("/")
    web_user = str(server["web_user"])
    sudo_password = (
        server.get("sudo_password") if server.get("sudo_requires_password") else None
    )
    remote_script_path = posixpath.join(
        REMOTE_TMP_DIR,
        f"moodle_copy_course_{uuid.uuid4().hex}.php",
    )

    ssh: paramiko.SSHClient | None = None
    sftp: paramiko.SFTPClient | None = None
    cleanup_errors: list[str] = []

    try:
        ssh = connect_ssh(server)
        sftp = ssh.open_sftp()

        php_script = build_remote_php_script(moodle_path)
        with sftp.file(remote_script_path, "w") as remote_file:
            remote_file.write(php_script)
        sftp.chmod(remote_script_path, 0o644)

        log = execute_remote(
            result,
            ssh,
            step="Copiar curso en Moodle",
            command=(
                f"sudo -u {q(web_user)} env HOME=/tmp php "
                f"{q(remote_script_path)} --courseid={int(course_id)}"
            ),
            sudo_password=sudo_password,
            timeout=7200,
        )
        result.data = parse_json_payload(log.stdout)
        result.success = True
    except RemoteCommandError as exc:
        result.error_detail = (
            exc.log.stderr.rstrip("\n")
            or exc.log.stdout.rstrip("\n")
            or str(exc)
        )
    except Exception as exc:
        result.error_detail = str(exc)
    finally:
        if ssh is not None:
            safe_cleanup_command(
                result,
                ssh,
                step="Limpiar script temporal",
                command=f"rm -f {q(remote_script_path)}",
                cleanup_errors=cleanup_errors,
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

        result.success = result.success and not bool(result.error_detail)

    return result


def get_course_categories(server: dict[str, Any]) -> ServerResult:
    moodle_path = str(server["moodle_path"]).rstrip("/")
    return run_moodle_cli_script(
        server,
        build_remote_get_categories_script(moodle_path),
        step="Listar categorías Moodle destino",
        timeout=1800,
    )


def create_course_category(
    server: dict[str, Any],
    name: str,
    parent: int = 0,
) -> ServerResult:
    category_name = str(name).strip()
    if not category_name:
        result = ServerResult(server_name=server["name"])
        result.error_detail = "El nombre de la nueva categoría es obligatorio."
        return result

    parent_id = int(parent)
    if parent_id < 0:
        result = ServerResult(server_name=server["name"])
        result.error_detail = "La categoría padre no puede ser negativa."
        return result

    moodle_path = str(server["moodle_path"]).rstrip("/")
    return run_moodle_cli_script(
        server,
        build_remote_create_category_script(moodle_path),
        step="Crear categoría Moodle destino",
        command_args=f"--name={q(category_name)} --parent={parent_id}",
        timeout=1800,
    )


def run_moodle_cli_script(
    server: dict[str, Any],
    php_script: str,
    *,
    step: str,
    command_args: str = "",
    timeout: int = 1800,
) -> ServerResult:
    result = ServerResult(server_name=server["name"])
    web_user = str(server["web_user"])
    sudo_password = (
        server.get("sudo_password") if server.get("sudo_requires_password") else None
    )
    remote_script_path = posixpath.join(
        REMOTE_TMP_DIR,
        f"moodle_cli_{uuid.uuid4().hex}.php",
    )

    ssh: paramiko.SSHClient | None = None
    sftp: paramiko.SFTPClient | None = None
    cleanup_errors: list[str] = []

    try:
        ssh = connect_ssh(server)
        sftp = ssh.open_sftp()

        with sftp.file(remote_script_path, "w") as remote_file:
            remote_file.write(php_script)
        sftp.chmod(remote_script_path, 0o644)

        command = f"sudo -u {q(web_user)} env HOME=/tmp php {q(remote_script_path)}"
        if command_args:
            command = f"{command} {command_args}"

        log = execute_remote(
            result,
            ssh,
            step=step,
            command=command,
            sudo_password=sudo_password,
            timeout=timeout,
        )
        payload = parse_json_payload(log.stdout)
        if not payload.get("success"):
            raise RuntimeError(payload.get("error") or "La operación Moodle no devolvió una respuesta exitosa.")

        result.data = payload
        result.success = True
    except RemoteCommandError as exc:
        result.error_detail = (
            exc.log.stderr.rstrip("\n")
            or exc.log.stdout.rstrip("\n")
            or str(exc)
        )
    except Exception as exc:
        result.error_detail = str(exc)
    finally:
        if ssh is not None:
            safe_cleanup_command(
                result,
                ssh,
                step="Limpiar script temporal Moodle",
                command=f"rm -f {q(remote_script_path)}",
                cleanup_errors=cleanup_errors,
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

        result.success = result.success and not bool(result.error_detail)

    return result

def build_remote_get_categories_script(moodle_path: str) -> str:
    return """<?php
define('CLI_SCRIPT', true);
$moodlepath = __MOODLE_PATH__;

function normalize_course_category($category) {
    $category = (array)$category;
    return array(
        'id' => (int)($category['id'] ?? 0),
        'name' => (string)($category['name'] ?? ''),
        'parent' => (int)($category['parent'] ?? 0),
        'depth' => (int)($category['depth'] ?? 0),
        'path' => (string)($category['path'] ?? ''),
        'sortorder' => (int)($category['sortorder'] ?? 0),
        'coursecount' => (int)($category['coursecount'] ?? 0),
        'visible' => (bool)($category['visible'] ?? true),
        'idnumber' => (string)($category['idnumber'] ?? ''),
    );
}

try {
    require_once($moodlepath . '/config.php');
    require_once($CFG->dirroot . '/course/externallib.php');

    global $USER;

    $admin = get_admin();
    if (!$admin) {
        throw new Exception('No se encontró usuario administrador en Moodle destino.');
    }
    \core\session\manager::set_user($admin);
    $USER = $admin;

    $rawcategories = core_course_external::get_categories(array(), true);
    $categories = array();
    foreach ($rawcategories as $rawcategory) {
        $category = normalize_course_category($rawcategory);
        if ($category['id'] > 0) {
            $categories[] = $category;
        }
    }

    usort($categories, function($a, $b) {
        $pathcmp = strcmp((string)$a['path'], (string)$b['path']);
        if ($pathcmp !== 0) {
            return $pathcmp;
        }
        return strcmp((string)$a['name'], (string)$b['name']);
    });

    echo json_encode(
        array(
            'success' => true,
            'count' => count($categories),
            'categories' => $categories,
        ),
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    ) . PHP_EOL;
    exit(0);
} catch (Throwable $e) {
    fwrite(STDERR, $e->getMessage() . PHP_EOL);
    echo json_encode(
        array('success' => false, 'error' => $e->getMessage()),
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    ) . PHP_EOL;
    exit(1);
}
""".replace("__MOODLE_PATH__", json.dumps(moodle_path))


def build_remote_create_category_script(moodle_path: str) -> str:
    return """<?php
define('CLI_SCRIPT', true);
$moodlepath = __MOODLE_PATH__;

function normalize_course_category_record($category) {
    $category = (array)$category;
    return array(
        'id' => (int)($category['id'] ?? 0),
        'name' => (string)($category['name'] ?? ''),
        'parent' => (int)($category['parent'] ?? 0),
        'depth' => (int)($category['depth'] ?? 0),
        'path' => (string)($category['path'] ?? ''),
        'sortorder' => (int)($category['sortorder'] ?? 0),
        'coursecount' => (int)($category['coursecount'] ?? 0),
        'visible' => (bool)($category['visible'] ?? true),
        'idnumber' => (string)($category['idnumber'] ?? ''),
    );
}

try {
    require_once($moodlepath . '/config.php');
    require_once($CFG->libdir . '/clilib.php');
    require_once($CFG->dirroot . '/course/externallib.php');

    global $DB, $USER;

    list($options, $unrecognized) = cli_get_params(
        array('name' => '', 'parent' => 0),
        array('n' => 'name', 'p' => 'parent')
    );

    $name = trim((string)$options['name']);
    $parent = (int)$options['parent'];
    if ($name === '') {
        throw new Exception('El nombre de la nueva categoría es obligatorio.');
    }
    if ($parent < 0) {
        throw new Exception('La categoría padre no puede ser negativa.');
    }
    if ($parent > 0 && !$DB->record_exists('course_categories', array('id' => $parent))) {
        throw new Exception('No existe la categoría padre ID ' . $parent . '.');
    }

    $admin = get_admin();
    if (!$admin) {
        throw new Exception('No se encontró usuario administrador en Moodle destino.');
    }
    \core\session\manager::set_user($admin);
    $USER = $admin;

    $createdcategories = core_course_external::create_categories(
        array(
            array(
                'name' => $name,
                'parent' => $parent,
            ),
        )
    );
    $createdcategory = reset($createdcategories);
    $createdcategoryarray = (array)$createdcategory;
    $categoryid = (int)($createdcategoryarray['id'] ?? 0);
    if ($categoryid <= 0) {
        throw new Exception('Moodle no devolvió el ID de la categoría creada.');
    }

    $record = $DB->get_record('course_categories', array('id' => $categoryid));
    $category = $record
        ? normalize_course_category_record($record)
        : normalize_course_category_record(
            array(
                'id' => $categoryid,
                'name' => $name,
                'parent' => $parent,
            )
        );

    echo json_encode(
        array(
            'success' => true,
            'category' => $category,
        ),
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    ) . PHP_EOL;
    exit(0);
} catch (Throwable $e) {
    fwrite(STDERR, $e->getMessage() . PHP_EOL);
    echo json_encode(
        array('success' => false, 'error' => $e->getMessage()),
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    ) . PHP_EOL;
    exit(1);
}
""".replace("__MOODLE_PATH__", json.dumps(moodle_path))

def build_remote_php_script(moodle_path: str) -> str:
    encoded_moodle_path = json.dumps(moodle_path)
    return f"""<?php
define('CLI_SCRIPT', true);
$moodlepath = {encoded_moodle_path};

try {{
    require_once($moodlepath . '/config.php');
    require_once($CFG->libdir . '/clilib.php');
    require_once($CFG->dirroot . '/course/externallib.php');

    global $DB, $USER;

    list($options, $unrecognized) = cli_get_params(
        array('courseid' => 0),
        array('c' => 'courseid')
    );

    $courseid = (int)$options['courseid'];
    if ($courseid <= 1) {{
        throw new Exception('ID de curso inválido. Debe ser mayor a 1.');
    }}

    $course = $DB->get_record('course', array('id' => $courseid));
    if (!$course) {{
        throw new Exception('No existe un curso con ID ' . $courseid . ' en esta plataforma Moodle.');
    }}
    $admin = get_admin();
    \\core\\session\\manager::set_user($admin);
    $USER = $admin;

    $timestamp = date('YmdHis');
    $fullname = $course->fullname . ' (Copia ' . $timestamp . ')';
    $shortbase = preg_replace('/[^A-Za-z0-9_-]+/', '_', $course->shortname);
    $shortbase = trim($shortbase, '_');
    if ($shortbase === '') {{
        $shortbase = 'curso_' . $courseid;
    }}
    $shortname = substr($shortbase . '_copia_' . $timestamp, 0, 255);

    $copyoptions = array(
        array('name' => 'users', 'value' => 0),
    );

    $newcourse = core_course_external::duplicate_course(
        (int)$course->id,
        $fullname,
        $shortname,
        (int)$course->category,
        (int)$course->visible,
        $copyoptions
    );

    echo json_encode(
        array(
            'success' => true,
            'source_course' => array(
                'id' => (int)$course->id,
                'fullname' => $course->fullname,
                'shortname' => $course->shortname,
                'category' => (int)$course->category,
            ),
            'new_course' => $newcourse,
        ),
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    ) . PHP_EOL;
    exit(0);
}} catch (Throwable $e) {{
    fwrite(STDERR, $e->getMessage() . PHP_EOL);
    echo json_encode(
        array(
            'success' => false,
            'error' => $e->getMessage(),
        ),
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    ) . PHP_EOL;
    exit(1);
}}
"""


def build_remote_backup_script(moodle_path: str) -> str:
    return """<?php
define('CLI_SCRIPT', true);
$moodlepath = __MOODLE_PATH__;

function set_backup_setting_if_exists($plan, $name, $value) {
    try {
        $setting = $plan->get_setting($name);
        $setting->set_value($value);
    } catch (Throwable $ignored) {
    }
}

try {
    require_once($moodlepath . '/config.php');
    require_once($CFG->libdir . '/clilib.php');
    require_once($CFG->dirroot . '/backup/util/includes/backup_includes.php');

    global $DB, $USER;

    list($options, $unrecognized) = cli_get_params(
        array('courseid' => 0, 'destination' => ''),
        array('c' => 'courseid', 'd' => 'destination')
    );

    $courseid = (int)$options['courseid'];
    $destination = rtrim((string)$options['destination'], '/');
    if ($courseid <= 1) {
        throw new Exception('ID de curso inválido. Debe ser mayor a 1.');
    }
    if ($destination === '' || !is_dir($destination) || !is_writable($destination)) {
        throw new Exception('La carpeta temporal de destino del backup no existe o no tiene permisos de escritura.');
    }

    $course = $DB->get_record('course', array('id' => $courseid));
    if (!$course) {
        throw new Exception('No existe un curso con ID ' . $courseid . ' en el Moodle origen.');
    }
    $category = $DB->get_record('course_categories', array('id' => (int)$course->category));
    if (!$category) {
        throw new Exception('No existe la categoría ID ' . $course->category . ' del curso origen.');
    }
    $categorypath = array();
    $pathids = array_filter(explode('/', trim((string)$category->path, '/')));
    foreach ($pathids as $pathid) {
        $pathcategory = $DB->get_record('course_categories', array('id' => (int)$pathid));
        if ($pathcategory) {
            $categorypath[] = array(
                'id' => (int)$pathcategory->id,
                'name' => $pathcategory->name,
                'parent' => (int)$pathcategory->parent,
            );
        }
    }
    if (empty($categorypath)) {
        $categorypath[] = array(
            'id' => (int)$category->id,
            'name' => $category->name,
            'parent' => (int)$category->parent,
        );
    }

    $admin = get_admin();
    if (!$admin) {
        throw new Exception('No se encontró usuario administrador en Moodle.');
    }
    \core\session\manager::set_user($admin);
    $USER = $admin;

    $controller = null;
    $filename = 'course_' . $courseid . '_' . date('YmdHis') . '.mbz';
    try {
        $controller = new backup_controller(
            backup::TYPE_1COURSE,
            $courseid,
            backup::FORMAT_MOODLE,
            backup::INTERACTIVE_NO,
            backup::MODE_GENERAL,
            $admin->id
        );

        set_backup_setting_if_exists($controller->get_plan(), 'users', 0);
        set_backup_setting_if_exists($controller->get_plan(), 'anonymize', 0);
        set_backup_setting_if_exists($controller->get_plan(), 'role_assignments', 0);
        set_backup_setting_if_exists($controller->get_plan(), 'activities', 1);
        set_backup_setting_if_exists($controller->get_plan(), 'blocks', 1);
        set_backup_setting_if_exists($controller->get_plan(), 'filters', 1);
        set_backup_setting_if_exists($controller->get_plan(), 'comments', 1);
        set_backup_setting_if_exists($controller->get_plan(), 'badges', 1);
        set_backup_setting_if_exists($controller->get_plan(), 'calendarevents', 1);
        set_backup_setting_if_exists($controller->get_plan(), 'userscompletion', 0);
        set_backup_setting_if_exists($controller->get_plan(), 'groups', 1);
        set_backup_setting_if_exists($controller->get_plan(), 'competencies', 1);
        set_backup_setting_if_exists($controller->get_plan(), 'questionbank', 1);
        set_backup_setting_if_exists($controller->get_plan(), 'contentbank', 1);
        set_backup_setting_if_exists($controller->get_plan(), 'customfield', 1);
        set_backup_setting_if_exists($controller->get_plan(), 'legacyfiles', 1);
        set_backup_setting_if_exists($controller->get_plan(), 'logs', 0);
        set_backup_setting_if_exists($controller->get_plan(), 'grade_histories', 0);
        set_backup_setting_if_exists($controller->get_plan(), 'filename', $filename);

        $controller->set_status(backup::STATUS_AWAITING);
        $controller->execute_plan();
        $results = $controller->get_results();
        if (empty($results['backup_destination'])) {
            throw new Exception('Moodle no devolvió el archivo de backup generado.');
        }

        $file = $results['backup_destination'];
        $target = $destination . '/' . $filename;
        if (!$file->copy_content_to($target)) {
            throw new Exception('No fue posible copiar el backup al directorio temporal.');
        }
        $file->delete();
        $controller->destroy();
        $controller = null;

        echo json_encode(
            array(
                'success' => true,
                'backup_path' => $target,
                'source_course' => array(
                    'id' => (int)$course->id,
                    'fullname' => $course->fullname,
                    'shortname' => $course->shortname,
                    'category' => (int)$course->category,
                ),
                'source_category' => array(
                    'id' => (int)$category->id,
                    'name' => $category->name,
                    'parent' => (int)$category->parent,
                ),
                'source_category_path' => $categorypath,
            ),
            JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
        ) . PHP_EOL;
        exit(0);
    } finally {
        if ($controller !== null) {
            $controller->destroy();
        }
    }
} catch (Throwable $e) {
    fwrite(STDERR, $e->getMessage() . PHP_EOL);
    echo json_encode(
        array('success' => false, 'error' => $e->getMessage()),
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    ) . PHP_EOL;
    exit(1);
}
""".replace("__MOODLE_PATH__", json.dumps(moodle_path))


def build_remote_restore_script(moodle_path: str) -> str:
    return """<?php
define('CLI_SCRIPT', true);
$moodlepath = __MOODLE_PATH__;

function unique_shortname($base) {
    global $DB;
    $base = trim((string)$base);
    if ($base === '') {
        $base = 'curso_copiado';
    }
    $base = substr($base, 0, 255);
    if (!$DB->record_exists('course', array('shortname' => $base))) {
        return $base;
    }

    $safebase = preg_replace('/[^A-Za-z0-9_-]+/', '_', $base);
    $safebase = trim($safebase, '_');
    if ($safebase === '') {
        $safebase = 'curso_copiado';
    }
    $timestamp = date('YmdHis');
    $copiesuffix = '_copia_' . $timestamp;
    $base = substr($safebase, 0, 255 - strlen($copiesuffix)) . $copiesuffix;
    $candidate = $base;
    $counter = 1;
    while ($DB->record_exists('course', array('shortname' => $candidate))) {
        $suffix = '_' . $counter;
        $candidate = substr($base, 0, 255 - strlen($suffix)) . $suffix;
        $counter++;
    }
    return $candidate;
}

function unique_fullname($base) {
    global $DB;

    $base = trim((string)$base);
    if ($base === '') {
        $base = 'Curso copiado';
    }

    if (!$DB->record_exists('course', array('fullname' => $base))) {
        return $base;
    }

    $timestamp = date('YmdHis');
    $counter = 0;
    do {
        $suffix = $counter === 0
            ? ' (Copia ' . $timestamp . ')'
            : ' (Copia ' . $timestamp . ' ' . $counter . ')';
        $candidate = substr($base, 0, max(1, 254 - strlen($suffix))) . $suffix;
        $counter++;
    } while ($DB->record_exists('course', array('fullname' => $candidate)));

    return $candidate;
}

function set_restore_setting_if_exists($plan, $name, $value) {
    try {
        $setting = $plan->get_setting($name);
        $setting->set_value($value);
    } catch (Throwable $ignored) {
    }
}

function stringify_precheck_results($results) {
    $messages = array();
    foreach ($results as $type => $items) {
        if (is_array($items)) {
            foreach ($items as $item) {
                if (is_scalar($item)) {
                    $messages[] = (string)$item;
                } else {
                    $messages[] = json_encode($item, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
                }
            }
        } else if (is_scalar($items)) {
            $messages[] = (string)$items;
        }
    }
    return implode(' | ', array_filter($messages));
}

function normalize_category_path($categorypathjson, $fallbackname) {
    $path = array();
    $decoded = json_decode((string)$categorypathjson, true);
    if (is_array($decoded)) {
        foreach ($decoded as $item) {
            if (!is_array($item)) {
                continue;
            }
            $name = trim((string)($item['name'] ?? ''));
            if ($name !== '') {
                $path[] = array('name' => $name);
            }
        }
    }

    $fallbackname = trim((string)$fallbackname);
    if (empty($path) && $fallbackname !== '') {
        $path[] = array('name' => $fallbackname);
    }
    if (empty($path)) {
        $path[] = array('name' => 'Cursos copiados');
    }
    return $path;
}

function normalize_course_category_for_path($category) {
    $category = (array)$category;
    return array(
        'id' => (int)($category['id'] ?? 0),
        'name' => (string)($category['name'] ?? ''),
        'parent' => (int)($category['parent'] ?? 0),
        'depth' => (int)($category['depth'] ?? 0),
        'path' => (string)($category['path'] ?? ''),
        'sortorder' => (int)($category['sortorder'] ?? 0),
        'coursecount' => (int)($category['coursecount'] ?? 0),
        'visible' => (bool)($category['visible'] ?? true),
        'idnumber' => (string)($category['idnumber'] ?? ''),
    );
}

function normalize_requested_category_path($categorypathjson) {
    $path = array();
    $decoded = json_decode((string)$categorypathjson, true);
    if (!is_array($decoded)) {
        return $path;
    }

    foreach ($decoded as $item) {
        $name = trim((string)$item);
        if ($name !== '') {
            $path[] = $name;
        }
    }
    return $path;
}

function category_path_items_to_names($path) {
    $names = array();
    foreach ($path as $item) {
        if (!is_array($item)) {
            continue;
        }
        $name = trim((string)($item['name'] ?? ''));
        if ($name !== '') {
            $names[] = $name;
        }
    }
    return $names;
}

function load_course_categories_for_path() {
    $rawcategories = core_course_external::get_categories(array(), true);
    $categories = array();
    foreach ($rawcategories as $rawcategory) {
        $category = normalize_course_category_for_path($rawcategory);
        if ($category['id'] > 0) {
            $categories[] = $category;
        }
    }
    return $categories;
}

function find_category_by_name_and_parent($categories, $name, $parent) {
    $parent = (int)$parent;
    foreach ($categories as $category) {
        if ((int)$category['parent'] === $parent && (string)$category['name'] === (string)$name) {
            return $category;
        }
    }
    return null;
}

function create_category_for_path($name, $parent) {
    global $DB;
    $createdcategories = core_course_external::create_categories(
        array(
            array(
                'name' => $name,
                'parent' => (int)$parent,
            ),
        )
    );
    $createdcategory = reset($createdcategories);
    $createdcategoryarray = (array)$createdcategory;
    $categoryid = (int)($createdcategoryarray['id'] ?? 0);
    if ($categoryid <= 0) {
        throw new Exception('Moodle no devolvió el ID de la categoría creada para la ruta destino.');
    }

    $record = $DB->get_record('course_categories', array('id' => $categoryid));
    if ($record) {
        return normalize_course_category_for_path($record);
    }

    return normalize_course_category_for_path(
        array(
            'id' => $categoryid,
            'name' => $name,
            'parent' => (int)$parent,
        )
    );
}

function resolve_category_path_names($pathnames, $sourcecategoryid, $selectedbypath, $originlabel) {
    $parent = 0;
    $created = false;
    $creatednames = array();
    $resolvedpath = array();
    $categories = load_course_categories_for_path();

    foreach ($pathnames as $name) {
        $name = trim((string)$name);
        if ($name === '') {
            continue;
        }

        $category = find_category_by_name_and_parent($categories, $name, $parent);
        if (!$category) {
            $category = create_category_for_path($name, $parent);
            $categories[] = $category;
            $created = true;
            $creatednames[] = $name;
        }
        $parent = (int)$category['id'];
        $resolvedpath[] = array(
            'id' => (int)$category['id'],
            'name' => (string)$category['name'],
            'parent' => (int)$category['parent'],
        );
    }

    if ($parent <= 0) {
        throw new Exception('No fue posible resolver o crear la categoría destino.');
    }
    $finalcategory = end($resolvedpath);

    return array(
        'id' => $parent,
        'name' => is_array($finalcategory) ? (string)$finalcategory['name'] : '',
        'parent' => is_array($finalcategory) ? (int)$finalcategory['parent'] : 0,
        'created' => $created,
        'created_names' => $creatednames,
        'source_category_id' => (int)$sourcecategoryid,
        'requested_path' => array_values($pathnames),
        'resolved_path' => $resolvedpath,
        'selected_by_path' => (bool)$selectedbypath,
        'message' => $created
            ? 'La jerarquía de categoría destino no existía completa y fue creada usando la ruta ' . $originlabel . '.'
            : 'Se reutilizó la jerarquía de categoría destino existente usando la ruta ' . $originlabel . '.',
    );
}

function resolve_requested_restore_category_path($destinationcategorypathjson) {
    $pathnames = normalize_requested_category_path($destinationcategorypathjson);
    if (empty($pathnames)) {
        return null;
    }
    return resolve_category_path_names($pathnames, 0, true, 'seleccionada por el usuario');
}

function resolve_restore_category($sourcecategoryid, $categoryname, $categorypathjson) {
    $pathnames = category_path_items_to_names(normalize_category_path($categorypathjson, $categoryname));
    return resolve_category_path_names($pathnames, $sourcecategoryid, false, 'del curso origen');
}
function resolve_selected_restore_category($destinationcategoryid) {
    global $DB;

    $destinationcategoryid = (int)$destinationcategoryid;
    if ($destinationcategoryid <= 0) {
        return null;
    }

    $category = $DB->get_record(
        'course_categories',
        array('id' => $destinationcategoryid)
    );
    if (!$category) {
        throw new Exception('No existe la categoría destino ID ' . $destinationcategoryid . '.');
    }

    return array(
        'id' => (int)$category->id,
        'name' => $category->name,
        'parent' => (int)$category->parent,
        'created' => false,
        'source_category_id' => 0,
        'selected_by_user' => true,
        'message' => 'Se usó la categoría destino seleccionada por el usuario.',
    );
}

function enforce_course_category($courseid, $categoryid) {
    global $DB;

    $courseid = (int)$courseid;
    $categoryid = (int)$categoryid;
    $course = $DB->get_record('course', array('id' => $courseid), '*', MUST_EXIST);
    $previouscategoryid = (int)$course->category;

    if ($previouscategoryid !== $categoryid) {
        if (!function_exists('move_courses')) {
            throw new Exception('No está disponible la función Moodle move_courses para mover el curso a la categoría seleccionada.');
        }
        move_courses(array($courseid), $categoryid);
    }

    $finalcourse = $DB->get_record('course', array('id' => $courseid), '*', MUST_EXIST);
    $finalcategoryid = (int)$finalcourse->category;
    if ($finalcategoryid !== $categoryid) {
        throw new Exception(
            'El restore finalizó, pero Moodle dejó el curso en la categoría ID ' .
            $finalcategoryid .
            ' en vez de la categoría seleccionada ID ' .
            $categoryid .
            '.'
        );
    }

    return array(
        'requested_category_id' => $categoryid,
        'previous_category_id' => $previouscategoryid,
        'final_category_id' => $finalcategoryid,
        'changed_after_restore' => $previouscategoryid !== $finalcategoryid,
    );
}

try {
    require_once($moodlepath . '/config.php');
    require_once($CFG->libdir . '/clilib.php');
    require_once($CFG->dirroot . '/course/lib.php');
    require_once($CFG->dirroot . '/course/externallib.php');
    require_once($CFG->dirroot . '/backup/util/includes/restore_includes.php');

    global $DB, $USER;

    list($options, $unrecognized) = cli_get_params(
        array(
            'backupfile' => '',
            'categoryid' => 0,
            'destinationcategoryid' => 0,
            'destinationcategorypathjson' => '',
            'categoryname' => '',
            'categorypathjson' => '',
            'fullname' => '',
            'shortname' => '',
        ),
        array('f' => 'backupfile', 'c' => 'categoryid')
    );

    $backupfile = (string)$options['backupfile'];
    $categoryid = (int)$options['categoryid'];
    $destinationcategoryid = (int)$options['destinationcategoryid'];
    $destinationcategorypathjson = (string)$options['destinationcategorypathjson'];
    $categoryname = trim((string)$options['categoryname']);
    $categorypathjson = (string)$options['categorypathjson'];
    $sourcefullname = trim((string)$options['fullname']);
    $sourceshortname = trim((string)$options['shortname']);

    if ($backupfile === '' || !is_readable($backupfile)) {
        throw new Exception('No se puede leer el archivo .mbz subido al destino.');
    }

    $admin = get_admin();
    if (!$admin) {
        throw new Exception('No se encontró usuario administrador en Moodle destino.');
    }
    \core\session\manager::set_user($admin);
    $USER = $admin;
    $resolvedcategory = resolve_selected_restore_category($destinationcategoryid);
    if ($resolvedcategory === null) {
        $resolvedcategory = resolve_requested_restore_category_path($destinationcategorypathjson);
    }
    if ($resolvedcategory === null) {
        $resolvedcategory = resolve_restore_category($categoryid, $categoryname, $categorypathjson);
    }
    $categoryid = (int)$resolvedcategory['id'];

    $fullname = unique_fullname($sourcefullname !== '' ? $sourcefullname : 'Curso copiado');
    $shortname = unique_shortname($sourceshortname !== '' ? $sourceshortname : 'curso');

    $packer = get_file_packer('application/vnd.moodle.backup');
    $backupid = restore_controller::get_tempdir_name(SITEID, $admin->id);
    $temppath = $CFG->tempdir . '/backup/' . $backupid . '/';
    if (!$packer->extract_to_pathname($backupfile, $temppath)) {
        throw new Exception('El archivo .mbz no es un backup válido de Moodle.');
    }

    $controller = null;
    $transaction = null;
    try {
        $transaction = $DB->start_delegated_transaction();
        $newcourseid = restore_dbops::create_new_course($fullname, $shortname, $categoryid);
        $controller = new restore_controller(
            $backupid,
            $newcourseid,
            backup::INTERACTIVE_NO,
            backup::MODE_GENERAL,
            $admin->id,
            backup::TARGET_NEW_COURSE
        );
        set_restore_setting_if_exists($controller->get_plan(), 'course_fullname', $fullname);
        set_restore_setting_if_exists($controller->get_plan(), 'course_shortname', $shortname);
        set_restore_setting_if_exists($controller->get_plan(), 'course_category', $categoryid);
        set_restore_setting_if_exists($controller->get_plan(), 'users', 0);
        set_restore_setting_if_exists($controller->get_plan(), 'role_assignments', 0);
        set_restore_setting_if_exists($controller->get_plan(), 'activities', 1);
        set_restore_setting_if_exists($controller->get_plan(), 'blocks', 1);
        set_restore_setting_if_exists($controller->get_plan(), 'filters', 1);
        set_restore_setting_if_exists($controller->get_plan(), 'comments', 1);
        set_restore_setting_if_exists($controller->get_plan(), 'badges', 1);
        set_restore_setting_if_exists($controller->get_plan(), 'calendarevents', 1);
        set_restore_setting_if_exists($controller->get_plan(), 'userscompletion', 0);
        set_restore_setting_if_exists($controller->get_plan(), 'groups', 1);
        set_restore_setting_if_exists($controller->get_plan(), 'competencies', 1);
        set_restore_setting_if_exists($controller->get_plan(), 'questionbank', 1);
        set_restore_setting_if_exists($controller->get_plan(), 'contentbank', 1);
        set_restore_setting_if_exists($controller->get_plan(), 'customfield', 1);
        set_restore_setting_if_exists($controller->get_plan(), 'legacyfiles', 1);

        if (!$controller->execute_precheck()) {
            $detail = stringify_precheck_results($controller->get_precheck_results());
            throw new Exception('Falló la validación previa del restore.' . ($detail ? ' ' . $detail : ''));
        }

        $controller->execute_plan();
        $categoryenforcement = enforce_course_category($newcourseid, $categoryid);
        $resolvedcategory['requested_category_id'] = (int)$categoryenforcement['requested_category_id'];
        $resolvedcategory['previous_category_id'] = (int)$categoryenforcement['previous_category_id'];
        $resolvedcategory['final_category_id'] = (int)$categoryenforcement['final_category_id'];
        $resolvedcategory['changed_after_restore'] = (bool)$categoryenforcement['changed_after_restore'];
        course_change_visibility($newcourseid, 1);
        rebuild_course_cache($newcourseid, true);
        $transaction->allow_commit();
        $transaction = null;
        $controller->destroy();
        $controller = null;

        echo json_encode(
            array(
                'success' => true,
                'new_course' => array(
                    'id' => (int)$newcourseid,
                    'fullname' => $fullname,
                    'shortname' => $shortname,
                    'category' => (int)$categoryenforcement['final_category_id'],
                    'visible' => 1,
                    'course_url' => $CFG->wwwroot . '/course/view.php?id=' . (int)$newcourseid,
                ),
                'category_resolution' => $resolvedcategory,
            ),
            JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
        ) . PHP_EOL;
        exit(0);
    } catch (Throwable $e) {
        if ($transaction !== null) {
            try {
                $transaction->rollback($e);
            } catch (Throwable $ignored) {
            }
        }
        throw $e;
    } finally {
        if ($controller !== null) {
            $controller->destroy();
        }
        if (function_exists('fulldelete') && is_dir($temppath)) {
            fulldelete($temppath);
        }
    }
} catch (Throwable $e) {
    fwrite(STDERR, $e->getMessage() . PHP_EOL);
    echo json_encode(
        array('success' => false, 'error' => $e->getMessage()),
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    ) . PHP_EOL;
    exit(1);
}
""".replace("__MOODLE_PATH__", json.dumps(moodle_path))

def connect_ssh(server: dict[str, Any]) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict[str, Any] = {
        "hostname": server["host"],
        "port": int(server.get("port", 22)),
        "username": server["ssh_user"],
        "timeout": 20,
        "banner_timeout": 20,
        "auth_timeout": 20,
    }

    if server.get("ssh_key_path"):
        key_path = Path(server["ssh_key_path"])
        if not key_path.exists():
            fallback = Path.home() / ".ssh" / key_path.name
            if fallback.exists():
                key_path = fallback
        connect_kwargs["key_filename"] = str(key_path)
    if server.get("ssh_key_passphrase"):
        connect_kwargs["passphrase"] = server["ssh_key_passphrase"]
    if server.get("ssh_password"):
        connect_kwargs["password"] = server["ssh_password"]

    host = str(server["host"])
    port = int(server.get("port", 22))
    name = str(server.get("name") or host)

    try:
        client.connect(**connect_kwargs)
    except socket.timeout as exc:
        client.close()
        raise ConnectionError(
            f"No se pudo conectar por SSH a {name} ({host}:{port}): tiempo de espera agotado. "
            "Verifica que la IP/host sea correcta y que el servicio SSH esté activo."
        ) from exc
    except paramiko.AuthenticationException as exc:
        client.close()
        raise ConnectionError(
            f"No se pudo autenticar por SSH en {name} ({host}:{port}). "
            "Verifica usuario, contraseña o llave SSH."
        ) from exc
    except (paramiko.SSHException, OSError) as exc:
        client.close()
        raise ConnectionError(
            f"No se pudo abrir la conexión SSH a {name} ({host}:{port}): {exc}"
        ) from exc
    return client


def run_remote_command(
    ssh: paramiko.SSHClient,
    command: str,
    *,
    sudo_password: str | None = None,
    timeout: int = 1800,
) -> tuple[int, str, str]:
    prepared_command = command
    use_sudo_password = bool(sudo_password) and command.lstrip().startswith("sudo ")

    if use_sudo_password:
        prepared_command = command.replace("sudo ", "sudo -S -p '' ", 1)

    stdin, stdout, stderr = ssh.exec_command(
        prepared_command,
        get_pty=use_sudo_password,
        timeout=timeout,
    )
    if use_sudo_password:
        stdin.write(f"{sudo_password}\n")
        stdin.flush()

    stdout_text = stdout.read().decode("utf-8", errors="replace")
    stderr_text = stderr.read().decode("utf-8", errors="replace")
    exit_status = stdout.channel.recv_exit_status()
    return exit_status, stdout_text, stderr_text


def execute_remote(
    result: ServerResult,
    ssh: paramiko.SSHClient,
    *,
    step: str,
    command: str,
    sudo_password: str | None = None,
    timeout: int = 1800,
    fail_on_error: bool = True,
) -> CommandLog:
    exit_status, stdout_text, stderr_text = run_remote_command(
        ssh,
        command,
        sudo_password=sudo_password,
        timeout=timeout,
    )
    log = CommandLog(
        step=step,
        command=command,
        exit_status=exit_status,
        stdout=stdout_text,
        stderr=stderr_text,
    )
    result.command_logs.append(log)

    if fail_on_error and exit_status != 0:
        raise RemoteCommandError(log)
    return log


def safe_cleanup_command(
    result: ServerResult,
    ssh: paramiko.SSHClient,
    *,
    step: str,
    command: str,
    cleanup_errors: list[str],
) -> None:
    try:
        log = execute_remote(
            result,
            ssh,
            step=step,
            command=command,
            fail_on_error=False,
        )
        if log.exit_status != 0:
            detail = (
                log.stderr.rstrip("\n")
                or log.stdout.rstrip("\n")
                or f"exit status {log.exit_status}"
            )
            cleanup_errors.append(f"{step}: {detail}")
    except Exception as exc:
        cleanup_errors.append(f"{step}: {exc}")


def parse_json_payload(stdout_text: str) -> dict[str, Any]:
    match = re.search(r"(\{.*\})", stdout_text, flags=re.DOTALL)
    if not match:
        return {"raw_stdout": stdout_text}

    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {"raw_stdout": stdout_text}

    if isinstance(payload, dict):
        return payload
    return {"raw_stdout": stdout_text}


def q(value: str) -> str:
    return shlex.quote(value)
