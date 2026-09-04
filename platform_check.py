"""Verificación de una plataforma del inventario.

Comprueba, para una entrada de ``inventario.json``, que los datos estén
completos y que además *funcionen*: que el sitio responda por HTTP, que el
servidor acepte la conexión SSH con las credenciales guardadas, y que las rutas
configuradas existan en ese servidor.

Diseño: cada verificación devuelve ``ok`` / ``fail`` / ``skip`` por separado, y
el estado global es ``review`` si alguna falla. ``skip`` es para lo que no
aplica — una ruta que no está configurada no es un error, y no se puede
comprobar una ruta si antes falló el SSH.

Se ejecuta una plataforma por request: la UI dispara una llamada por plataforma
y las va resolviendo en paralelo. Así el resultado de cada una aparece en
cuanto está lista y ningún request queda colgado esperando a las 35.
"""
from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

import paramiko

# Timeouts cortos a proposito: esto es un chequeo de salud, no una operacion de
# clonado. El default de ssh/urllib puede tardar minutos en rendirse ante un
# host que no responde, y aca lo que interesa es justamente saber rapido que no
# responde.
URL_TIMEOUT = 10
SSH_CONNECT_TIMEOUT = 10
SSH_COMMAND_TIMEOUT = 15

# Lo que exige `load_inventory` para poder cargar la entrada, mas la credencial
# SSH (sin ella no se puede hacer nada con la plataforma).
REQUIRED_FIELDS = (
    ("name", "Nombre"),
    ("host", "Direccion IP"),
    ("ssh_user", "Usuario SSH"),
    ("moodle_path", "Ruta Moodle"),
    ("web_user", "Usuario web"),
)

USER_AGENT = "AulaCloner/verificacion"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _check(check_id: str, label: str, status: str, detail: str) -> dict[str, Any]:
    return {"id": check_id, "label": label, "status": status, "detail": detail}


def _text(server: dict[str, Any], field: str) -> str:
    return str(server.get(field) or "").strip()


# --- Verificaciones individuales ---------------------------------------

def _check_required_fields(server: dict[str, Any]) -> dict[str, Any]:
    missing = [label for field, label in REQUIRED_FIELDS if not _text(server, field)]
    if not (_text(server, "ssh_key_path") or _text(server, "ssh_password")):
        missing.append("Ruta llave SSH o Contrasena SSH")
    if server.get("sudo_requires_password") and not _text(server, "sudo_password"):
        missing.append("Contrasena sudo (esta marcado 'sudo requiere contrasena')")
    if missing:
        return _check(
            "required_fields", "Campos obligatorios", "fail",
            "Faltan: " + ", ".join(missing) + ".",
        )
    return _check(
        "required_fields", "Campos obligatorios", "ok",
        "Todos los campos obligatorios estan completos.",
    )


def _check_url(url: str) -> dict[str, Any]:
    label = "Sitio en linea"
    if not url:
        return _check("url", label, "skip", "Sin URL configurada en el inventario.")

    request = urllib.request.Request(
        url, method="GET", headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=URL_TIMEOUT) as response:
            code = response.getcode()
            final_url = response.geturl()
            detail = f"HTTP {code}."
            if final_url.rstrip("/") != url.rstrip("/"):
                detail += f" Redirige a {final_url}"
            return _check("url", label, "ok", detail)
    except urllib.error.HTTPError as exc:
        # El servidor contesto: esta vivo, pero devolvio un error. Un 503 suele
        # ser el modo mantenimiento de Moodle, no una caida.
        extra = " Puede ser el modo mantenimiento de Moodle." if exc.code == 503 else ""
        return _check(
            "url", label, "fail",
            f"HTTP {exc.code} ({exc.reason}). El servidor respondio con error.{extra}",
        )
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return _check(
                "url", label, "fail",
                f"Certificado TLS invalido: {reason.verify_message or reason}. "
                "Revisar Certbot para este dominio.",
            )
        if isinstance(reason, ssl.SSLError):
            return _check("url", label, "fail", f"Error TLS: {reason}.")
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return _check(
                "url", label, "fail",
                f"Sin respuesta en {URL_TIMEOUT}s. Revisar que el sitio este arriba "
                "y que el dominio apunte a la IP correcta.",
            )
        if isinstance(reason, socket.gaierror):
            return _check(
                "url", label, "fail",
                f"El dominio no resuelve en DNS ({reason}).",
            )
        return _check("url", label, "fail", f"No se pudo conectar: {reason}.")
    except (socket.timeout, TimeoutError):
        return _check("url", label, "fail", f"Sin respuesta en {URL_TIMEOUT}s.")
    except Exception as exc:  # noqa: BLE001 - un chequeo no debe tumbar el request
        return _check("url", label, "fail", f"Error inesperado: {exc}.")


def _connect_ssh(server: dict[str, Any]) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict[str, Any] = {
        "hostname": server["host"],
        "port": int(server.get("port", 22)),
        "username": server["ssh_user"],
        "timeout": SSH_CONNECT_TIMEOUT,
        "banner_timeout": SSH_CONNECT_TIMEOUT,
        "auth_timeout": SSH_CONNECT_TIMEOUT,
    }
    if server.get("ssh_key_path"):
        kwargs["key_filename"] = server["ssh_key_path"]
    if server.get("ssh_key_passphrase"):
        kwargs["passphrase"] = server["ssh_key_passphrase"]
    if server.get("ssh_password"):
        kwargs["password"] = server["ssh_password"]
    client.connect(**kwargs)
    return client


def _run(ssh: paramiko.SSHClient, command: str) -> tuple[int, str, str]:
    _stdin, stdout, stderr = ssh.exec_command(command, timeout=SSH_COMMAND_TIMEOUT)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    return stdout.channel.recv_exit_status(), out, err


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _check_remote_path(
    ssh: paramiko.SSHClient,
    check_id: str,
    label: str,
    path: str,
    *,
    kind: str,
) -> dict[str, Any]:
    """Existencia de una ruta en el servidor. ``kind`` es 'd' (directorio) o 'f'.

    Con fallback a ``sudo -n``: los directorios de datos y los vhost de Nginx
    suelen no ser accesibles para el usuario SSH, y un 'no existe' por permisos
    manda a buscar el problema al lugar equivocado.
    """
    if not path:
        return _check(check_id, label, "skip", "No esta configurada en el inventario.")

    quoted = _quote(path)
    status, _out, _err = _run(ssh, f"test -{kind} {quoted}")
    if status == 0:
        return _check(check_id, label, "ok", f"Existe: {path}")

    sudo_status, _out, _err = _run(ssh, f"sudo -n test -{kind} {quoted}")
    if sudo_status == 0:
        return _check(
            check_id, label, "ok",
            f"Existe: {path} (solo visible con sudo; el usuario SSH no la alcanza).",
        )

    esperado = "El directorio" if kind == "d" else "El archivo"
    return _check(
        check_id, label, "fail",
        f"{esperado} no existe en el servidor, o no es del tipo esperado: {path}",
    )


def _check_config_php(ssh: paramiko.SSHClient, moodle_path: str) -> dict[str, Any]:
    """`config.php` dentro de moodle_path.

    Es lo que el clonador de instancias lee para sacar los datos de la base. Su
    ausencia es el fallo clasico ("config.php not found") y aparece recien a
    mitad del job, con el origen ya en modo mantenimiento.
    """
    label = "config.php de Moodle"
    if not moodle_path:
        return _check("config_php", label, "skip", "Sin ruta Moodle configurada.")
    path = moodle_path.rstrip("/") + "/config.php"
    quoted = _quote(path)
    status, _out, _err = _run(ssh, f"test -f {quoted}")
    if status != 0:
        status, _out, _err = _run(ssh, f"sudo -n test -f {quoted}")
    if status == 0:
        return _check("config_php", label, "ok", f"Existe: {path}")
    return _check(
        "config_php", label, "fail",
        f"No se encontro {path}. La ruta Moodle puede estar apuntando al lugar "
        "equivocado.",
    )


def _ssh_failure_detail(exc: Exception, server: dict[str, Any]) -> str:
    host = _text(server, "host")
    port = int(server.get("port", 22))
    user = _text(server, "ssh_user")
    key = _text(server, "ssh_key_path")
    if isinstance(exc, paramiko.AuthenticationException):
        if key:
            # paramiko no distingue "llave cifrada sin passphrase" de "llave
            # rechazada": en los dos casos termina en AuthenticationException,
            # asi que se nombran las dos causas.
            return (
                f"El servidor rechazo las credenciales de {user}@{host} (llave {key}). "
                "Revisar que el usuario sea el correcto, que la llave este en "
                "authorized_keys del servidor, y que no este cifrada sin passphrase "
                "guardada."
            )
        return (
            f"El servidor rechazo las credenciales de {user}@{host} (contrasena SSH). "
            "Revisar el usuario y la contrasena guardada."
        )
    if isinstance(exc, paramiko.ssh_exception.NoValidConnectionsError):
        return (
            f"No se pudo abrir el puerto {port} en {host}. La instancia puede estar "
            "apagada, o el firewall no permite el puerto 22 desde el host del clonador."
        )
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return (
            f"Sin respuesta de {host}:{port} en {SSH_CONNECT_TIMEOUT}s. Es un problema "
            "de red, no de credenciales: revisar que la instancia este encendida, que "
            "la IP sea la correcta y el firewall del puerto 22."
        )
    if isinstance(exc, socket.gaierror):
        return f"No se pudo resolver el host '{host}' ({exc})."
    if isinstance(exc, FileNotFoundError):
        return f"No existe el archivo de llave SSH en el host del clonador: {key}"
    if isinstance(exc, paramiko.PasswordRequiredException):
        return f"La llave {key} esta cifrada y no hay passphrase guardada."
    return f"{type(exc).__name__}: {exc}"


# --- Entrada publica ---------------------------------------------------

def verify_server(server: dict[str, Any], index: int) -> dict[str, Any]:
    checks = [
        _check_required_fields(server),
        _check_url(_text(server, "url")),
    ]

    path_specs = (
        ("path_moodle", "Ruta Moodle en el servidor", _text(server, "moodle_path"), "d"),
        ("path_moodledata", "Ruta moodledata en el servidor", _text(server, "moodledata_path"), "d"),
        ("path_vhost", "Ruta vhost Nginx en el servidor", _text(server, "vhost_path"), "f"),
    )

    ssh: Optional[paramiko.SSHClient] = None
    if not _text(server, "host") or not _text(server, "ssh_user"):
        checks.append(_check(
            "ssh", "Acceso SSH", "skip",
            "Faltan datos de conexion (direccion IP o usuario SSH).",
        ))
    elif not (_text(server, "ssh_key_path") or _text(server, "ssh_password")):
        # Sin credencial no hay nada que intentar; paramiko diria "No
        # authentication methods available", que no le dice nada a nadie.
        checks.append(_check(
            "ssh", "Acceso SSH", "skip",
            "Sin credencial configurada: falta la ruta de la llave SSH o la contrasena.",
        ))
    else:
        try:
            ssh = _connect_ssh(server)
            status, out, _err = _run(ssh, "hostname")
            remoto = f" Responde como '{out}'." if status == 0 and out else ""
            checks.append(_check(
                "ssh", "Acceso SSH", "ok",
                f"Conexion establecida con {server['ssh_user']}@{server['host']}:"
                f"{int(server.get('port', 22))}.{remoto}",
            ))
        except Exception as exc:  # noqa: BLE001 - se reporta, no se propaga
            checks.append(_check(
                "ssh", "Acceso SSH", "fail", _ssh_failure_detail(exc, server),
            ))
            ssh = None

    if ssh is None:
        for check_id, label, path, _kind in path_specs:
            checks.append(_check(
                check_id, label, "skip",
                "No se pudo comprobar: sin acceso SSH al servidor."
                if path else "No esta configurada en el inventario.",
            ))
        checks.append(_check(
            "config_php", "config.php de Moodle", "skip",
            "No se pudo comprobar: sin acceso SSH al servidor.",
        ))
    else:
        try:
            for check_id, label, path, kind in path_specs:
                checks.append(
                    _check_remote_path(ssh, check_id, label, path, kind=kind)
                )
            checks.append(_check_config_php(ssh, _text(server, "moodle_path")))
        finally:
            try:
                ssh.close()
            except Exception:  # noqa: BLE001
                pass

    failed = [c for c in checks if c["status"] == "fail"]
    return {
        "index": index,
        "name": _text(server, "name") or f"#{index}",
        "status": "review" if failed else "ok",
        "failed_count": len(failed),
        "skipped_count": sum(1 for c in checks if c["status"] == "skip"),
        "checked_at": _now_iso(),
        "checks": checks,
    }
