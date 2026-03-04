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

## Modo WEB
- El archivo index.html puede ser desplegado en un webserver para correr el script `moodle-clone-web.sh` 
- Login protegido por sesión:
  - Configura credenciales fijas en `.env` con `APP_LOGIN_USER` y `APP_LOGIN_PASSWORD`
  - Define `APP_SECRET_KEY` para firmar la cookie de sesión
  - Sin sesión activa, la UI principal redirige a `/login`
