## Clone from any source server (SSH)

This tool can now clone Moodle from:

- **Local source instance** (preset in `app.py`)
- **Remote source server over SSH** (custom paths)

### Requirements (remote source mode)

The server running `app.py` must be able to SSH into the **source** server using the configured key:

- SSH user (example: `ubuntu`)
- SSH private key path (absolute path on the app server)
- Access to source paths:
  - Moodle dir (e.g. `/var/www/moodle_x`)
  - moodledata dir (e.g. `/var/moodledata_x`)
  - source `config.php` inside Moodle dir
- Ability to run remote commands used by the script:
  - `sudo -u www-data php ...` (maintenance/CLI actions on source)
  - `mysqldump` on source host
  - `rsync`/SSH copy from source host

### UI usage

1. Open the web UI.
2. In **1. Origen**, set **Modo de origen**:
   - `Instancia local (preconfigurada)` OR
   - `Servidor remoto por SSH`
3. If remote:
   - Fill:
     - `Host origen`
     - `Directorio Moodle origen`
     - `Directorio moodledata origen`
     - `Vhost origen (ruta)`
4. Fill destination + DB + options.
5. Run **Simular (dry-run)** first.

### API payload example (remote source)

`POST /api/clone`

```json
{
  "source_mode": "remote",
  "source_host": "10.0.0.15",
  "source_dir": "/var/www/moodle_clienta",
  "source_data": "/var/moodledata_clienta",
  "source_vhost": "/etc/nginx/sites-available/clienta.example.com",

  "source_instance": "",
  "maintenance_source": true,

  "deploy_target": "remote",
  "remote_host": "51.44.30.62",

  "new_key": "clienta_clone",
  "new_domain": "clienta-clone.example.com",
  "new_url": "https://clienta-clone.example.com",
  "dest_dir": "/var/www/html/moodle/clienta_clone",
  "dest_data": "/var/www/data/moodle/clienta_clone",

  "target_db_user": "admin_moodle",
  "target_db_pass": "********",
  "dest_db": "moodle_clienta_clone",

  "opt_replace": true,
  "opt_purge": true,
  "opt_nginx": true,
  "opt_certbot": true,
  "opt_cron": true,
  "dry_run": true
}
```

### Notes

- In **remote source mode**, `source_instance` is ignored.
- In **local source mode**, presets from `SOURCE_INSTANCES` in `app.py` are used.
- SSH key used by the app is configured via `.env` (`REMOTE_SSH_KEY`).
- Always validate with **dry-run** before real cloning.

# Moodle Cloner
Script interactivo para clonar instancias Moodle en este servidor (Ubuntu, Nginx, MySQL/Aurora en RDS):
- Copia código y `moodledata`
- Dump/restore de base de datos con saneado para evitar errores de privilegios (GTID/SQL_LOG_BIN)
- Actualiza `config.php` sin exponer contraseñas en la línea de comandos
- Crea vhost Nginx y obtiene certificados (opcional) con Certbot
- Ejecuta `replace.php` y `purge_caches.php`
- Configura `cron`

## Requisitos
- `rsync`, `mysql-client`, `mysqldump`, `php-cli`, `nginx`, (opcional) `certbot`

## Uso
```bash
/home/ubuntu/moodle-cloner/moodle-clone.sh
```
Sigue las preguntas. Recomienda habilitar mantenimiento en la instancia origen antes de clonar y lo puede desactivar al final.

Por defecto propone:
- Origen código: `/var/www/moodle_digitinstitute`
- Origen moodledata: `/var/moodledata_digitinstitute`
- Vhost base: `/etc/nginx/sites-available/moodle_digitinstitute`
- - Nueva clave: `nuevaplataforma` → crea `/var/www/moodle_nuevaplataforma`, `/var/moodledata_nuevaplataforma`, BD `moodle_nuevaplataforma`, dominio `nuevaplataforma.awakelab.world` (editable)

## Notas de seguridad
- Las contraseñas se leen en modo silencioso y se pasan a los clientes MySQL mediante la variable `MYSQL_PWD` o variables de entorno temporales; no se incluyen como argumentos visibles del proceso.
- Revisa el vhost generado y los logs de Nginx si algo falla (`sudo nginx -t`, `sudo tail -f /var/log/nginx/error.log`).

### Guardrails adicionales para RDS en producción

El script web incluye controles de seguridad adicionales para minimizar riesgos en RDS:

- `SAFE_MODE` (default: `1`): habilita barreras obligatorias antes de escribir en BD.
- `I_UNDERSTAND_PRODUCTION_RDS=true`: confirmación explícita requerida cuando `SAFE_MODE=1` y `DRY_RUN=0`.
- `RDS_HOST_ALLOWLIST` (CSV opcional): lista de hosts permitidos para `TARGET_DB_HOST`.
  - Ejemplo: `RDS_HOST_ALLOWLIST=db1.xxxxx.rds.amazonaws.com,db2.xxxxx.rds.amazonaws.com`
- `PROTECTED_DATABASES` (CSV opcional): bases prohibidas como destino.
  - Ejemplo: `PROTECTED_DATABASES=moodle_prod,moodle_clienta_prod`

Además, el script ahora valida que:

- `DEST_DB` y `SRC_DBNAME` tengan formato seguro (`[A-Za-z0-9_]+`).
- `DEST_DB` sea distinto de la base origen.
- Se ejecute un preflight de conexión al host destino antes de `CREATE DATABASE`/import.

Recomendación operativa:

1. Ejecutar primero con `DRY_RUN=1`.
2. Configurar `RDS_HOST_ALLOWLIST` y `PROTECTED_DATABASES`.
3. Solo para ejecución real, establecer `I_UNDERSTAND_PRODUCTION_RDS=true`.

## Modo WEB
- El archivo index.html puede ser desplegado en un webserver para correr el script `moodle-clone-web.sh` 
