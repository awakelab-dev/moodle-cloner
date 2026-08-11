# Changelog

All notable changes to the Moodle Cloner web app are tracked here.
Every code iteration must bump the version in `VERSION` and add an entry below.

Format: `YYYY-MM-DD - vX.Y.Z - Short description` followed by a bulleted list.

## v0.11.1 - 2026-08-11

Fix: eliminado `backdrop-blur` de toda la UI — en Safari 26 dejaba la pantalla completamente oscura, con el contenido presente y seleccionable pero sin pintar.

- **Sintoma**: en Safari 26.5.2 (macOS) el login no se veia. El texto estaba en el DOM y se copiaba con select-all, el cursor cambiaba a seleccion de texto sobre los campos, y los estilos computados eran todos correctos (`h1.color: rgb(248,250,252)` sobre `bodyBg: rgb(2,6,23)`, `opacity: 1`, `visibility: visible`, `getBoundingClientRect` dentro del viewport). O sea: geometria y colores bien resueltos, pero WebKit no pintaba la capa. En Chrome se veia perfecto, y en una ventana privada de Safari tambien — porque arranca con estado de composicion nuevo, no porque hubiera un cache viejo (un cache desactualizado habria dado valores computados incorrectos, no correctos).
- **Causa**: `backdrop-filter: blur(8px)` fuerza una capa de composicion propia, y Safari tiene una familia conocida de bugs donde esa capa termina sin pintarse. Se usaba en 10 lugares.
- **Cambio**: `bg-slate-900/80 backdrop-blur`, `/70` y `/95` pasan a `bg-slate-900` opaco (tarjeta de login, header sticky, menu mobile y las 4 tarjetas de seccion). En los 3 overlays de modal se conserva el velo `bg-black/60` y solo se quita `backdrop-blur-sm`. Cero `backdrop-*` en el archivo.
- Sin perdida visual: las tarjetas estaban sobre un degradado plano, no habia contenido real detras que difuminar.
- Verificado: los 2 bloques de script inline siguen parseando (`node --check`), el archivo termina en `</html>`, y la tarjeta renderiza con `backdrop-filter: none`.

Ademas:

- **Etiqueta de version movida a un pie fijo**. Estaba en el header junto al logo (y solo desde el breakpoint `sm`, asi que en mobile no se veia). Ahora es un `<footer class="fixed inset-x-0 bottom-0">` centrado, fuera de `#login-view` y `#app-view`, asi que se ve en el login y en la app. Lleva `pointer-events-none` para no tapar clicks y `z-20` para quedar debajo del header (`z-30`) y de los modales (`z-40`). El header queda con el logo solo, con mas aire para los tabs.

## v0.11.0 - 2026-08-11

Fix: el arranque ya no depende de Aurora, y la migracion de columnas de `users` es determinista en vez de basarse en atrapar el error 1060.

- **`app.py` — el puerto HTTP se bindea antes de tocar la base de datos**. Antes, `main()` hacia `db.init_schema()` / `seed_initial_admin()` de forma sincrona y solo despues creaba el `ThreadingHTTPServer`. Si esa inicializacion se colgaba (endpoint de Aurora lento, `ALTER TABLE` esperando un metadata lock — el default de `lock_wait_timeout` en MySQL es un año), el proceso nunca llegaba a `serve_forever()`: nada escuchaba en el puerto, el navegador mostraba una pantalla vacia y `pm2 logs` solo dejaba ver "Initializing application database..." sin ningun error. Ahora el servidor levanta primero y la init de la DB corre en un thread daemon (`init_app_db`), asi que el login siempre renderiza y cualquier problema de DB aparece como error de API, no como pantalla en blanco.
- **`app.py` — logs de arranque con `flush=True`** (`log_boot`). pm2 captura stdout como pipe, asi que Python lo bufferea por bloques y los mensajes cortos de arranque no llegaban nunca a `pm2 logs`. Tambien se registra el nombre de la excepcion (`type(exc).__name__`) y la version en la linea de arranque.
- **`db.py` — migracion de columnas via `information_schema`**. Se elimino el bloque `MIGRATIONS` con `ALTER TABLE ... ADD COLUMN` a ciegas + `try/except` del error 1060. En su lugar `USERS_COLUMNS` declara las columnas esperadas con su definicion y su `AFTER`, y `_migrate_users_columns()` consulta `information_schema.COLUMNS` y agrega solo las que faltan. Es idempotente por construccion, y un fallo real (falta el privilegio `ALTER`, tabla bloqueada) sale a la luz en vez de quedar tapado. Agregar un permiso nuevo ahora es una linea en `USERS_COLUMNS`.
- **`db.py` — `init_schema()` devuelve la lista de columnas agregadas**, que se loguea en el arranque (`Schema migration: added users columns ...` o `Schema up to date.`).
- **`db.py` — `SET SESSION lock_wait_timeout = 15`** antes del DDL, para que un metadata lock falle rapido en vez de colgar el arranque.
- **`db.py` — `read_timeout` / `write_timeout` de 30s** en `_connect()`. Solo habia `connect_timeout`, asi que una conexion ya establecida que se quedaba esperando respuesta bloqueaba indefinidamente.
- **`db.py` — entry point manual**: `python3 db.py` carga `.env`, reconcilia el schema e informa que columnas agrego, sin reiniciar la API. Util para aplicar la migracion y verificarla antes del `pm2 restart`.
- **`index.html` — el login se renderiza por defecto**. `#login-view` tenia la clase `hidden` y solo se mostraba cuando el IIFE de boot del final del archivo llamaba a `showLoginView()`. Si ese `<script>` no se parseaba o llegaba truncado (subida interrumpida, paste con heredoc sin comillas), el navegador lo descartaba en silencio — sin error en consola — el `hidden` nunca se quitaba y la pagina quedaba completamente vacia. Ahora arranca visible (`flex`) y `showAppView()` lo oculta cuando ya hay sesion, asi que una falla de JS degrada a "login visible" en vez de pantalla negra.
- **`app.py` — `render_index_html()` detecta un `index.html` truncado**: si el archivo no termina en `</html>` lanza un `RuntimeError` con el tamaño leido y como recuperarlo, en vez de servir un HTML partido que se ve como pantalla vacia.
- Deploy: sin dependencias nuevas. Recomendado correr `python3 db.py` una vez tras el `git pull` para confirmar que la columna `can_access_alexia_cloner` quedo creada, y despues `pm2 restart moodle-cloner-api`.

## v0.10.0 - 2026-08-04

Feature: Clonador Alexia — migrar cursos de Catalejo a Alexia via SSH, con importacion masiva por Excel y exportacion individual.

- **Nuevo modulo `alexia_routes.py`**: porta la logica completa del proyecto standalone `alexia-exportar-curso`. Conecta a Catalejo (origen) y Alexia (destino) por SSH/SFTP via paramiko. Genera y ejecuta scripts PHP remotos para buscar cursos, crear arboles de categorias (5 niveles: Ejercicio > IdCentro > Modalidad > Especialidad > PerteneceCurso), hacer backup (.mbz) en origen, transferir via SFTP, y restaurar en destino. Credenciales en `alexia_config.json` (gitignored).
- **Importacion masiva**: subida de archivo Excel (.xlsx) con openpyxl. El Excel contiene filas con ReducidoGrupo, CodigoOficial, IdCentro, NombreCentro, Ejercicio, Especialidad, PerteneceCurso, Estudio, Mat1, Mat2, Area. Se genera automaticamente el shortname (`{ReducidoGrupo}_{CodigoOficial}_{IdCentro}_{Ejercicio}`), la modalidad (VIRTUAL si ReducidoGrupo termina en V, PRESENCIAL en caso contrario), y el arbol de categorias. Tabla interactiva con seleccion, filtro, y vista previa del arbol. Exportacion por lotes con polling de progreso y estados por fila.
- **Exportacion individual**: busqueda de cursos en Catalejo, formulario con 12 campos, vista previa del arbol de categorias en tiempo real, y exportacion con barra de progreso y log de pasos.
- **`app.py`**: 9 nuevas rutas bajo `/api/alexia/*` (6 POST, 3 GET), todas protegidas por `can_access_alexia_cloner`.
- **`db.py`**: nuevo permiso `can_access_alexia_cloner` en `PERMISSION_FLAGS`, columna en schema, y en todas las queries SELECT/INSERT. Corregida la query de `get_session_user` que no incluia el nuevo campo.
- **`index.html`**: reemplazado el placeholder "Proximamente" del tab Alexia con la interfaz completa (HTML + IIFE JS). Config panel para servidores Catalejo/Alexia, sub-tabs batch/individual, drag-and-drop de Excel, tabla de resultados, modal de arbol, formulario de exportacion individual, barras de progreso, y tarjetas de resultado. Todos los IDs prefijados con `ax-` para evitar conflictos.
- **UI de usuarios**: nueva columna "Alexia" en la tabla de usuarios y checkbox en el modal de edicion para `can_access_alexia_cloner`.
- `.gitignore`: agregados `alexia_config.json` y `temp_alexia/`.
- Deploy: `sudo apt-get install -y python3-openpyxl` antes de reiniciar. Bases de datos existentes necesitan: `ALTER TABLE users ADD COLUMN can_access_alexia_cloner TINYINT(1) NOT NULL DEFAULT 0 AFTER can_access_plugin_cloner;`

## v0.9.0 - 2026-08-04

Feature: topbar reorganizado con menu hamburguesa responsive y nuevo tab Clonador Alexia.

- **Topbar reorganizado**: los 4 tabs de herramientas (Moodle, Cursos, Plugins, Alexia) estan siempre visibles en el topbar, con labels cortos para caber en mobile. Los botones de administracion (Plataformas, Usuarios, Mi cuenta, Salir) se colapsan en un menu hamburguesa en pantallas < `md` (768px).
- **Clonador Alexia**: nuevo tab en la navegacion principal. Muestra una seccion placeholder "en desarrollo". No requiere permiso especifico por ahora.
- Los botones de administracion en el menu mobile se sincronizan con la visibilidad segun permisos del usuario.

## v0.8.0 - 2026-07-28

Feature: Clonador de plugins — interfaz web para instalar plugins Moodle (.zip) en multiples plataformas por SSH.

- **Nuevo modulo `plugin_routes.py`**: porta la logica de instalacion de `moodle_plugin_installer.py` (SSH/SFTP, 5 pasos: capturar permisos, subir ZIP + descomprimir, upgrade.php, purge_caches.php, restaurar permisos). Incluye deteccion automatica de tipo de plugin por nombre (frankenstyle), normalizacion de ZIPs con rutas Windows, y parsing de multipart/form-data.
- **`app.py`**: nuevas rutas `POST /api/plugin/install` (multipart con archivo ZIP), `GET /api/plugin/types`, `GET /api/plugin/servers`. Los jobs de plugin usan el mismo dict `jobs` con `job_type: "plugin"` y corren en hilo separado. El check de job en ejecucion ahora filtra por tipo, permitiendo un job de clone y uno de plugin en paralelo. Limite de upload: 100 MB.
- **`index.html`**: reemplazado el placeholder "Clonador de plugins" con la interfaz completa: zona de drag-and-drop para subir .zip, selector de tipo de plugin (55 tipos oficiales con auto-deteccion), checkboxes de plataformas destino (con seleccionar/limpiar todo), boton de instalacion, barra de progreso, log en tiempo real, y tarjetas de resultados por servidor.
- **IIFE `window.__pluginLoad`**: carga lazy de tipos y servidores al entrar a la seccion. Patron identico a `__ccLoad` / `__moodleLoad`.
- El permiso `can_access_plugin_cloner` (ya existente en el esquema de BD y en la UI de gestion de usuarios) ahora controla el acceso real a la funcionalidad.
- El endpoint `GET /api/jobs/{id}` ahora devuelve `job_type`, `results` y `plugin_info` cuando el job es de tipo plugin, y verifica el permiso correcto segun el tipo de job.

## v0.7.0 - 2026-07-28

Feature: inventario con campos Moodle completos; Clonador Moodle carga instancias desde inventario.

- **Nuevo campo `moodledata_path`** en el inventario: ruta al directorio moodledata de cada plataforma (ej. `/var/www/data/moodle/ejemplo`).
- **Nuevo campo `vhost_path`** en el inventario: ruta al archivo vhost Nginx de cada plataforma (ej. `/etc/nginx/sites-available/ejemplo.awakelab.world`).
- Ambos campos son opcionales en el esquema (no afectan el Clonador de cursos) pero **obligatorios** para usar la plataforma como origen en el Clonador Moodle.
- **Clonador Moodle — "Instancia Moodle Origen"**: el select ahora carga dinámicamente todas las plataformas del inventario (igual que el Clonador de cursos) en lugar de opciones estáticas hardcodeadas. Se envía `source_index` al backend.
- **`app.py`**: eliminado el dict `SOURCE_INSTANCES` hardcodeado. El modo `local` ahora resuelve rutas (`moodle_path`, `moodledata_path`, `vhost_path`) leyendo la entrada del inventario por índice.
- **`course_routes.py`**: `_public_server` y `_server_from_payload` incluyen los dos nuevos campos.
- **`course_copier.py`**: `load_inventory` inicializa `moodledata_path` y `vhost_path` con string vacío si no están presentes (retrocompatible).
- **Administrar plataformas**: el formulario ahora muestra y permite editar `moodledata_path` y `vhost_path`; las tarjetas de lista los muestran cuando están configurados.
- **`inventario.example.json`**: actualizado con los dos nuevos campos.
- Deploy: actualizar `inventario.json` en el servidor agregando `moodledata_path` y `vhost_path` a cada entrada existente para poder usar esas plataformas como origen en el Clonador Moodle. Las plataformas sin esos campos siguen funcionando normalmente en el Clonador de cursos.

## v0.6.2 - 2026-07-28

UI: mostrar solo el nombre en el selector de plataforma origen del clonador de cursos.

- En el `<select>` "Plataforma origen" (paso 1 del clonador de cursos), las opciones ahora muestran únicamente el nombre de la plataforma en lugar de `nombre · ruta_moodle`.

## v0.6.1 - 2026-06-01

Cosmetic: header alignment.

- Vertically centered the header logo, the "Plataforma de clonado" label, and the version badge by flattening the leftover nested wrapper into a single `flex items-center` row. No behavior change.

## v0.6.0 - 2026-06-01

Branding: header logo.

- Replaced the header "A" badge and "Awake Lab" text with the horizontal AWAKELAB logo (`RESIZED_logo_fondooscuro_horizontal.png`), served from the new public route `GET /assets/logo.png`. The "Plataforma de clonado" subtitle and version chip are unchanged.
- Deploy note: the logo PNG must be present in the project root on the server. It is committed to the repo, so a normal `git pull` ships it — no manual upload needed.

## v0.5.1 - 2026-05-29

Cosmetic tweaks.

- Page title changed from "Awake Lab - Plataforma de Clonado" to "Clonador Awakelab".
- Added an inline SVG favicon (indigo rounded square with a white "A", matching the header badge). Encoded as a `data:image/svg+xml` URI so it ships in `index.html` without needing a static-file route.

## v0.5.0 - 2026-05-29

User-management UI and authorization refinements.

- `PATCH /api/users/:id` now allows self-edit: any logged-in user can change their own password without needing `can_manage_users`. Self-edits and edits performed by a non-superadmin user-manager are restricted to the password field — attempts to change `permissions` or `is_superadmin` are rejected with 403.
- Non-superadmin actors can no longer touch a superadmin's record (including its password). Last-superadmin protection is unchanged.
- `POST /api/users` creates accounts with zero permissions when the actor is not a superadmin, regardless of what permissions the request payload contains. A superadmin must grant permissions afterwards.
- New "Mi cuenta" header button visible to every logged-in user, opening a small modal to change their own password (with confirmation field). Wraps `PATCH /api/users/<own-id>` with `{password}`.
- User-management modal now hides the permissions block entirely for non-superadmin viewers, and the users list hides the Edit/Eliminar buttons on superadmin rows for non-superadmin actors.
- Reusable password show/hide eye-icon toggle is decorated onto every `<input type=password>` at boot and after each modal opens. Covers login, user modal, account modal, platforms admin form, and the Moodle clone form's DB password.
- Toaster backgrounds switched from `bg-*/10` (translucent, blurring into content) to solid `bg-*-950` with a colored border and `shadow-xl`.

## v0.4.0 - 2026-05-28

Course cloner port from `awk-copia-de-cursos-` (Phase 1 backend + Phase 2 UI + UI tweaks).

- New module `course_copier.py` (verbatim from the source project): paramiko-based SSH/SFTP that backs up courses on a source Moodle and restores them in parallel to one or more destination Moodles, plus category get/create.
- New module `course_routes.py`: stdlib HTTP wrappers around `course_copier` (validation, JSON (de)serialization, `CourseRouteError` → HTTP status mapping). Public-shaped vs admin-shaped server views, identical semantics to the original FastAPI service.
- `app.py`: eight new routes under `/api/cc/*`, all auth-gated. User-facing reads and the copy itself require `can_access_course_cloner`; platform admin CRUD requires `is_superadmin`. `POST /api/cc/copy-course` accepts JSON (the original FastAPI multipart form had no file uploads).
- `requirements.txt`: `+ paramiko>=3.4.0`.
- `.gitignore`: `+ inventario.json` (plaintext SSH and sudo passwords — same pattern as `.env`). Added `inventario.example.json` as the schema template for fresh environments.
- Full course-copy UI rebuilt in Tailwind inside the existing "Clonador de cursos" tab: source server + course ID, destination cards (multi-select + select-all), two category-resolution modes (reference platform with existing/new category, and by-name search with per-server fallback + optional base route), submit, per-server result cards with course links.
- New superadmin-only "Plataformas" section reached via a header button alongside "Usuarios". Side-by-side form + list, CRUD with secret-preserving edit semantics (blank password fields keep the stored value).
- 401 from any `/api/cc/*` call boots the user to the login view; 403 surfaces in the toaster. The course-cloner inventory and platforms list are lazy-loaded on first entry via `window.__ccLoad` / `window.__pfLoad` sentinels.
- Destination platform grid expands to three columns on `lg+` screens. The platform list is sorted alphabetically by name on load, so both the source dropdown and the destination cards come out in the same order.

## v0.3.0 - 2026-05-27

Persistent user system, login, sessions, and per-section permissions. The Aurora cluster (already used as the Moodle clone target via `TARGET_DB_HOST`) now also hosts a dedicated `APP_DB_NAME` (default `moodle_cloner_app`) for the app's own metadata.

- New module `db.py`: PyMySQL-backed `users` / `sessions` / `app_settings` tables (schema bootstrapped on first boot). PBKDF2-SHA256 password hashing with per-user salt. Session tokens are `secrets.token_hex(32)`, stored server-side, exchanged via an HttpOnly `mc_session` cookie with `SameSite=Lax` and a 7-day TTL.
- Initial superadmin seeded from `INITIAL_ADMIN_USER` / `INITIAL_ADMIN_PASS` env vars on first run if no users exist; nothing is seeded thereafter.
- `app.py` gains auth middleware and endpoints: `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`, and CRUD over `/api/users` (`GET`, `POST`, `PATCH`, `DELETE`). `POST /api/clone` and `GET /api/jobs/*` now require auth + `can_access_moodle_cloner`.
- Permission model: per-section booleans (`can_access_moodle_cloner`, `can_access_course_cloner`, `can_access_plugin_cloner`), separate `can_manage_users`, plus an `is_superadmin` flag that implicitly grants everything.
- Guardrails: cannot demote or delete the last superadmin; cannot delete your own account; only superadmins can grant the superadmin role.
- Frontend rewrite of `index.html`: login view, sticky three-tab header (Moodle / Cursos / Plugins) shown to every authenticated user regardless of permission, with a toaster on click for the ones they can't reach. New superadmin/can_manage_users-only `section-users` with a CRUD modal handling all four permission flags. Toaster system added.
- `.env.example` / `.env` extended with `APP_DB_NAME`, `INITIAL_ADMIN_USER`, `INITIAL_ADMIN_PASS`, and optional `APP_SESSION_SECRET`. `PyMySQL>=1.1.0` added to a new `requirements.txt`.
- Fix in `db.py`: `_ensure_database()` could not bootstrap the app database on first boot — `_connect(database=None)` was falling through to the default name, so the `CREATE DATABASE IF NOT EXISTS` statement was issued from a connection scoped to the not-yet-existing DB. Replaced the default with a sentinel so `database=None` now means "connect with no DB selected".

## v0.2.0 - 2026-05-26

Fix silent broken-clone bug surfaced by v0.1.0 logs.

- Switched remote URL-replace and cache-purge in `moodle-clone-web.sh` from `remote_bash` to `remote_sudo_bash`. The remote `$DEST_DIR` is chowned to `www-data` right after rsync and inherits source-side perms (often `750`), so SSH'ing in as `ubuntu` could not `cd` into it. The post-deploy `admin/tool/replace/cli/replace.php` and `admin/cli/purge_caches.php` therefore never ran, leaving the cloned DB with the source's `wwwroot` and the cloned site effectively broken even though the job reported `rc=0`. We now escalate to root for the `cd` and drop to `www-data` for the PHP CLI itself.
- Added `"URL replace failed"` to `fatal_markers` in `app.py`. Even though the bash side intentionally warns-and-continues on URL replace failure (so the rest of the clone can finish), the web app now marks the job as failed when that warning appears, so a broken clone can no longer surface as success in the UI.
- **Manual cleanup needed for existing broken clones** (e.g. `sanase-test`): either re-run the clone with v0.2.0, or on the destination box run `sudo -u www-data php /var/www/html/moodle/<key>/admin/tool/replace/cli/replace.php --non-interactive --search='<old_url>' --replace='<new_url>'` followed by `sudo -u www-data php /var/www/html/moodle/<key>/admin/cli/purge_caches.php`.

## v0.1.0 - 2026-05-26

Diagnostics groundwork to unblock clone-failure debugging.

- Added `VERSION` file and this `CHANGELOG.md` as the iteration tracker; version is now surfaced in `app.py` and rendered in the web UI header.
- Fixed cleanup trap in `moodle-clone-web.sh` to use `sudo rm -rf` for `$TMP_DIR`. Files in `$TMP_DIR/source_data/` are written by `sudo rsync --rsync-path="sudo rsync"` and end up root-owned on the cloner host, so plain `rm -rf` was producing hundreds of "Permission denied" lines that filled the in-memory log ring buffer (250K chars) and masked the real upstream error. Note: this only touches `/tmp/tmp.XXXX` on the cloner box; no change to the source instance.
- Persisted full job output to disk under `./logs/job-<id>.log` in `app.py`. The web UI still shows the truncated tail, but the on-disk file is complete so we can see what actually failed before the cleanup spam.
- Surfaced effective `rc` and fatal-marker detection unchanged; this iteration is diagnostic only and does not attempt to fix the underlying deployment bug.
