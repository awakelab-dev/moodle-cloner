"""HTTP-shaped wrappers around course_copier.

Pure functions that take parsed Python inputs and return plain dicts, or raise
``CourseRouteError`` / ``ValueError`` on error. The HTTP handler in app.py is
responsible for turning these into responses.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Optional

from course_copier import (
    INVENTORY_FILE,
    copy_course_between_servers,
    create_course_category,
    get_course_categories,
    load_inventory,
)


ROOT = Path(__file__).resolve().parent
INVENTORY_PATH = ROOT / INVENTORY_FILE


class CourseRouteError(Exception):
    """Raised when an operation fails in a way that has a specific HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --- Inventory IO -------------------------------------------------------

def _load_servers() -> list[dict[str, Any]]:
    try:
        return load_inventory(INVENTORY_PATH)
    except FileNotFoundError as exc:
        raise CourseRouteError(500, str(exc)) from exc
    except Exception as exc:
        raise CourseRouteError(400, str(exc)) from exc


def _write_inventory(servers: list[dict[str, Any]]) -> None:
    INVENTORY_PATH.write_text(
        json.dumps(servers, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _select_server(servers: list[dict[str, Any]], index: int) -> dict[str, Any]:
    if index < 0 or index >= len(servers):
        raise CourseRouteError(404, f"La plataforma #{index} no existe.")
    return servers[index]


# --- Server (de)serialization ------------------------------------------

def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _public_server(server: dict[str, Any], index: int) -> dict[str, Any]:
    if server.get("ssh_key_path"):
        auth_method = "Llave SSH"
    elif server.get("ssh_password"):
        auth_method = "Contraseña SSH"
    else:
        auth_method = "Agente SSH / credenciales del sistema"
    return {
        "index": index,
        "name": server["name"],
        "host": server["host"],
        "port": int(server.get("port", 22)),
        "ssh_user": server["ssh_user"],
        "moodle_path": server["moodle_path"],
        "web_user": server["web_user"],
        "web_group": server.get("web_group") or server["web_user"],
        "sudo_requires_password": bool(server.get("sudo_requires_password", False)),
        "auth_method": auth_method,
    }


def _admin_server(server: dict[str, Any], index: int) -> dict[str, Any]:
    public = _public_server(server, index)
    public.update(
        {
            "ssh_key_path": server.get("ssh_key_path") or "",
            "has_ssh_password": bool(server.get("ssh_password")),
            "has_ssh_key_passphrase": bool(server.get("ssh_key_passphrase")),
            "has_sudo_password": bool(server.get("sudo_password")),
        }
    )
    return public


def _secret_value(
    payload: dict[str, Any],
    existing: Optional[dict[str, Any]],
    field_name: str,
) -> str:
    raw_value = payload.get(field_name)
    value = str(raw_value).strip() if raw_value is not None else ""
    if value:
        return value
    if existing is not None:
        return str(existing.get(field_name) or "")
    return ""


def _server_from_payload(
    payload: dict[str, Any],
    *,
    existing: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CourseRouteError(400, "La plataforma debe enviarse como un objeto JSON.")

    def read_text(field_name: str, *, required: bool = False, default: str = "") -> str:
        raw_value = payload.get(field_name, default)
        value = str(raw_value).strip() if raw_value is not None else ""
        if required and not value:
            raise CourseRouteError(400, f"El campo '{field_name}' es obligatorio.")
        return value

    name = read_text("name", required=True)
    host = read_text("host", required=True)
    ssh_user = read_text("ssh_user", required=True)
    moodle_path = read_text("moodle_path", required=True).rstrip("/")
    web_user = read_text("web_user", required=True)
    web_group = read_text("web_group") or web_user
    ssh_key_path = read_text("ssh_key_path")

    try:
        port = int(payload.get("port", 22))
    except (TypeError, ValueError) as exc:
        raise CourseRouteError(400, "El puerto SSH debe ser un número válido.") from exc
    if port <= 0 or port > 65535:
        raise CourseRouteError(400, "El puerto SSH debe estar entre 1 y 65535.")

    server = {
        "name": name,
        "host": host,
        "port": port,
        "ssh_user": ssh_user,
        "ssh_password": _secret_value(payload, existing, "ssh_password"),
        "ssh_key_path": ssh_key_path,
        "ssh_key_passphrase": _secret_value(payload, existing, "ssh_key_passphrase"),
        "moodle_path": moodle_path,
        "web_user": web_user,
        "web_group": web_group,
        "sudo_requires_password": _as_bool(payload.get("sudo_requires_password", False)),
        "sudo_password": _secret_value(payload, existing, "sudo_password"),
    }

    if not server["ssh_password"] and not server["ssh_key_path"]:
        raise CourseRouteError(400, "Debes indicar contraseña SSH o ruta de llave SSH.")
    if server["sudo_requires_password"] and not server["sudo_password"]:
        raise CourseRouteError(400, "Indica la contraseña sudo o desactiva 'sudo requiere contraseña'.")
    return server


# --- ServerResult serialization ----------------------------------------

def _serialize_result(result: Any) -> dict[str, Any]:
    return {
        "server_name": result.server_name,
        "success": result.success,
        "error_detail": result.error_detail,
        "data": result.data,
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


# --- Public endpoints (called from MoodleCloneHandler) ------------------

def list_servers() -> dict[str, Any]:
    servers = _load_servers()
    return {
        "inventory_file": INVENTORY_PATH.name,
        "count": len(servers),
        "servers": [_public_server(s, i) for i, s in enumerate(servers)],
    }


def list_categories(server_index: int) -> dict[str, Any]:
    servers = _load_servers()
    server = _select_server(servers, server_index)
    result = get_course_categories(server)
    if not result.success:
        raise CourseRouteError(
            502,
            result.error_detail or "No fue posible obtener las categorías del Moodle destino.",
        )
    categories = result.data.get("categories") or []
    return {
        "server": _public_server(server, server_index),
        "count": len(categories),
        "categories": categories,
    }


def create_category(server_index: int, payload: dict[str, Any]) -> dict[str, Any]:
    servers = _load_servers()
    server = _select_server(servers, server_index)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise CourseRouteError(400, "El nombre de la nueva categoría es obligatorio.")
    try:
        parent = int(payload.get("parent") or 0)
    except (TypeError, ValueError) as exc:
        raise CourseRouteError(400, "La categoría padre debe ser un ID numérico.") from exc
    if parent < 0:
        raise CourseRouteError(400, "La categoría padre no puede ser negativa.")

    result = create_course_category(server, name, parent)
    if not result.success:
        raise CourseRouteError(
            502,
            result.error_detail or "No fue posible crear la categoría en Moodle destino.",
        )
    category = result.data.get("category")
    if not category:
        raise CourseRouteError(502, "Moodle no devolvió la categoría creada.")
    return {
        "message": "Categoría creada correctamente.",
        "server": _public_server(server, server_index),
        "category": category,
    }


def admin_list_servers() -> dict[str, Any]:
    servers = _load_servers()
    return {
        "inventory_file": INVENTORY_PATH.name,
        "count": len(servers),
        "servers": [_admin_server(s, i) for i, s in enumerate(servers)],
    }


def admin_create_server(payload: dict[str, Any]) -> dict[str, Any]:
    servers = _load_servers()
    servers.append(_server_from_payload(payload))
    _write_inventory(servers)
    saved = _load_servers()
    new_index = len(saved) - 1
    return {
        "message": "Plataforma agregada correctamente.",
        "server": _admin_server(saved[new_index], new_index),
    }


def admin_update_server(server_index: int, payload: dict[str, Any]) -> dict[str, Any]:
    servers = _load_servers()
    existing = _select_server(servers, server_index)
    servers[server_index] = _server_from_payload(payload, existing=existing)
    _write_inventory(servers)
    saved = _load_servers()
    return {
        "message": "Plataforma actualizada correctamente.",
        "server": _admin_server(saved[server_index], server_index),
    }


def admin_delete_server(server_index: int) -> dict[str, Any]:
    servers = _load_servers()
    if len(servers) <= 1:
        raise CourseRouteError(400, "Debe quedar al menos una plataforma registrada.")
    removed = _select_server(servers, server_index)
    servers.pop(server_index)
    _write_inventory(servers)
    saved = _load_servers()
    return {
        "message": f"Plataforma '{removed['name']}' eliminada correctamente.",
        "count": len(saved),
        "servers": [_admin_server(s, i) for i, s in enumerate(saved)],
    }


# --- Course copy (JSON body instead of multipart) ----------------------

def _coerce_indexes(value: Any) -> list[int]:
    if not isinstance(value, list) or not value:
        raise CourseRouteError(400, "Debes seleccionar al menos una plataforma destino.")
    out: list[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise CourseRouteError(400, "La selección de plataformas contiene valores inválidos.")
        out.append(item)
    return out


def _coerce_category_path(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise CourseRouteError(400, "La ruta de categoría destino debe enviarse como un array JSON.")
    if len(value) > 50:
        raise CourseRouteError(400, "La ruta de categoría destino es demasiado profunda.")
    out: list[str] = []
    for item in value:
        name = str(item or "").strip()
        if not name:
            raise CourseRouteError(400, "La ruta de categoría destino contiene nombres vacíos.")
        if len(name) > 255:
            raise CourseRouteError(400, "La ruta de categoría destino contiene un nombre demasiado largo.")
        out.append(name)
    return out


def _coerce_category_paths(value: Any, selected: list[int]) -> dict[int, list[str]]:
    if not isinstance(value, dict):
        raise CourseRouteError(400, "Las rutas por plataforma deben enviarse como objeto JSON.")
    selected_set = set(selected)
    out: dict[int, list[str]] = {}
    for raw_key, raw_path in value.items():
        try:
            idx = int(str(raw_key))
        except (TypeError, ValueError) as exc:
            raise CourseRouteError(400, "Las rutas por plataforma contienen claves inválidas.") from exc
        if idx not in selected_set:
            raise CourseRouteError(400, f"La plataforma #{idx} no está seleccionada como destino.")
        out[idx] = _coerce_category_path(raw_path)
    return out


def _coerce_category_ids(
    value: Any,
    selected: list[int],
    required: list[int],
) -> dict[int, int]:
    if not isinstance(value, dict):
        raise CourseRouteError(400, "La selección de categorías destino debe enviarse como objeto JSON.")
    selected_set = set(selected)
    required_set = set(required)
    if not required_set.issubset(selected_set):
        raise CourseRouteError(400, "La selección requerida de categorías destino no coincide con los destinos seleccionados.")
    out: dict[int, int] = {}
    for raw_key, raw_id in value.items():
        try:
            idx = int(str(raw_key))
        except (TypeError, ValueError) as exc:
            raise CourseRouteError(400, "La selección de categorías destino contiene claves inválidas.") from exc
        if idx not in selected_set:
            raise CourseRouteError(400, f"La plataforma #{idx} no está seleccionada como destino.")
        if raw_id in (None, ""):
            if idx in required_set:
                raise CourseRouteError(400, f"Selecciona una categoría destino válida para la plataforma #{idx}.")
            continue
        try:
            cat_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise CourseRouteError(400, f"Selecciona una categoría destino válida para la plataforma #{idx}.") from exc
        if cat_id <= 0:
            raise CourseRouteError(400, f"Selecciona una categoría destino válida para la plataforma #{idx}.")
        out[idx] = cat_id
    missing = [i for i in required_set if i not in out]
    if missing:
        joined = ", ".join(f"#{i}" for i in sorted(missing))
        raise CourseRouteError(400, f"Selecciona una categoría destino válida para la(s) plataforma(s): {joined}.")
    return out


def copy_course(payload: dict[str, Any]) -> dict[str, Any]:
    """Run a course copy from source to one or more destinations.

    Expected JSON body:
        source_index: int
        course_id: int (>1)
        destination_indexes: list[int]
        destination_categories: dict[str|int, int]    # optional
        destination_category_path: list[str]           # optional, global path for all dests
        destination_category_paths: dict[str|int, list[str]]  # optional, per-dest path
        destination_use_source_path: bool              # default True
    """
    if not isinstance(payload, dict):
        raise CourseRouteError(400, "El cuerpo de la solicitud debe ser un objeto JSON.")

    try:
        source_index = int(payload.get("source_index"))
    except (TypeError, ValueError) as exc:
        raise CourseRouteError(400, "source_index inválido.") from exc
    try:
        course_id = int(payload.get("course_id"))
    except (TypeError, ValueError) as exc:
        raise CourseRouteError(400, "course_id inválido.") from exc
    if course_id <= 1:
        raise CourseRouteError(400, "Ingresa un ID de curso válido mayor a 1.")

    selected = _coerce_indexes(payload.get("destination_indexes"))
    if source_index in selected:
        raise CourseRouteError(400, "La plataforma origen no debe estar seleccionada como destino.")

    raw_global_path = payload.get("destination_category_path", [])
    global_path = _coerce_category_path(raw_global_path) if raw_global_path else []
    raw_per_dest_paths = payload.get("destination_category_paths", {})
    per_dest_paths = _coerce_category_paths(raw_per_dest_paths, selected) if raw_per_dest_paths else {}

    use_source_path = bool(payload.get("destination_use_source_path", True))
    if global_path or per_dest_paths:
        use_source_path = False
    if global_path and per_dest_paths:
        raise CourseRouteError(
            400,
            "No combines una ruta global y rutas por plataforma al mismo tiempo. Usa solo una modalidad de ruta destino.",
        )

    if use_source_path:
        category_ids: dict[int, int] = {}
        per_dest_paths = {}
    else:
        if global_path:
            per_dest_paths = {idx: list(global_path) for idx in selected}
        required = [idx for idx in selected if idx not in per_dest_paths]
        raw_cats = payload.get("destination_categories", {})
        category_ids = _coerce_category_ids(raw_cats, selected, required) if raw_cats else _coerce_category_ids({}, selected, required)
        overlap = sorted(set(category_ids).intersection(per_dest_paths))
        if overlap:
            joined = ", ".join(f"#{i}" for i in overlap)
            raise CourseRouteError(
                400,
                f"No se puede definir categoría por ID y ruta al mismo tiempo para la(s) plataforma(s): {joined}.",
            )
        unresolved = [idx for idx in selected if idx not in category_ids and idx not in per_dest_paths]
        if unresolved:
            joined = ", ".join(f"#{i}" for i in unresolved)
            raise CourseRouteError(
                400,
                f"Falta definir categoría o ruta destino para la(s) plataforma(s): {joined}.",
            )

    servers = _load_servers()
    source_server = _select_server(servers, source_index)
    destination_servers: list[dict[str, Any]] = []
    for idx in selected:
        d = dict(_select_server(servers, idx))
        if idx in per_dest_paths:
            d["destination_category_path"] = list(per_dest_paths[idx])
        elif idx in category_ids:
            d["destination_category_id"] = category_ids[idx]
        destination_servers.append(d)

    with tempfile.TemporaryDirectory(prefix="moodle_course_transfer_") as tmp_dir:
        source_result, results = copy_course_between_servers(
            source_server=source_server,
            destination_servers=destination_servers,
            course_id=course_id,
            local_work_dir=Path(tmp_dir),
        )

    return {
        "course_id": course_id,
        "source": _serialize_result(source_result),
        "summary": {
            "total": len(results),
            "success": sum(1 for r in results if r.success),
            "failed": sum(1 for r in results if not r.success),
        },
        "results": [_serialize_result(r) for r in results],
    }
