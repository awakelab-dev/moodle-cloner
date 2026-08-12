# Changelog

All notable changes to the Moodle Cloner web app are tracked here.
Every code iteration must bump the version in `VERSION` and add an entry below.

Format: `YYYY-MM-DD - vX.Y.Z - Short description` followed by a bulleted list.

## v0.17.0 - 2026-08-12

Feature: boton de verificacion en Administrar plataformas. Comprueba, plataforma por plataforma, que el sitio responda, que el SSH funcione y que las rutas existan de verdad en el servidor.

- **Nuevo modulo `platform_check.py`** con 7 comprobaciones por plataforma, cada una con estado propio `ok` / `fail` / `skip`:
  1. **Campos obligatorios** — `name`, `host`, `ssh_user`, `moodle_path`, `web_user`, credencial SSH (llave o contrasena) y, si esta marcado "sudo requiere contrasena", que la contrasena sudo este.
  2. **Sitio en linea** — GET a la `url` del inventario con timeout de 10s. Distingue los casos que se confunden entre si: HTTP >= 400 (y avisa que un 503 suele ser el modo mantenimiento de Moodle), certificado TLS invalido (apunta a Certbot), dominio que no resuelve en DNS, conexion rechazada y sin respuesta. Informa la redireccion cuando la URL final no es la configurada.
  3. **Acceso SSH** — conexion real con las credenciales guardadas y `hostname` en el remoto para confirmar contra que maquina se hablo. Mensajes separados para credenciales rechazadas, puerto cerrado (nombra el firewall), timeout de red, host que no resuelve y llave que no existe en el host del clonador.
  4-6. **`moodle_path`, `moodledata_path` y `vhost_path` en el servidor** — `test -d` para los directorios y `test -f` para el vhost, **solo si el valor esta configurado**; si no, queda `skip` en vez de fallar.
  7. **`config.php` de Moodle** dentro de `moodle_path`. Es lo que el clonador de instancias lee para sacar los datos de la base, y su ausencia es el clasico `config.php not found` que hoy aparece recien a mitad del job, con el origen ya en modo mantenimiento.
- **Fallback a `sudo -n` en las comprobaciones de rutas.** Los directorios de datos y los vhost de Nginx suelen no ser accesibles para el usuario SSH; sin el fallback, un problema de permisos se reporta como "no existe" y manda a buscar el error al lugar equivocado. Cuando la ruta solo aparece con sudo, el detalle lo dice.
- **Nada de estados globales inventados**: si una comprobacion no aplica (URL sin configurar, ruta vacia) o no se pudo hacer (rutas sin acceso SSH), queda `skip` y **no** cuenta como fallo. El estado de la plataforma es `ok` si ninguna comprobacion aplicable fallo, y `review` si alguna fallo.
- **Nuevo endpoint `POST /api/cc/admin/servers/<i>/verify`** (solo superadmin, igual que el resto de la gestion de plataformas). Verifica **una** plataforma por request: la UI dispara una llamada por plataforma y resuelve hasta 6 en paralelo. Un unico request para las 35 tardaria minutos, se arriesgaria al timeout de nginx y no mostraria nada hasta el final. Es POST, no GET, para que quede claro que dispara trabajo y para que ningun intermediario lo cachee.
- **UI**: boton **"Verificar todas"** con barra de progreso y contadores en vivo (`N paso el test | N requieren revision | N sin poder verificar`), y un boton **"Verificar"** por plataforma. Ademas aparece **"Reverificar las N que requieren revision"**: era el caso que planteo Leonardo — si de 35 solo una falla, corregirla y no tener que volver a pasar por las 34 que ya estaban bien.
- **El detalle se abre solo cuando requiere revision** y queda plegado cuando paso, con un toggle "ver detalle (7)". Con 35 plataformas correctas, 245 lineas de checks desplegadas no dejarian ver nada.
- **Los resultados se guardan por firma de la plataforma, no por indice.** Al borrar una plataforma todas las de abajo corren un lugar, y con clave por indice el resultado quedaba pegado a la plataforma vecina — un falso "paso el test" sobre una plataforma que nunca se verifico. Con la firma (nombre, url, host, puerto, usuario, llave y las tres rutas), borrar una no toca los resultados de las demas y **editar una descarta el suyo**, que es lo correcto: el resultado anterior ya no describe los datos nuevos.
- Verificado con un sshd y servidores HTTP/HTTPS reales levantados como fixtures (usuario sin privilegios, sudo NOPASSWD, un directorio en modo 700 para el fallback de sudo, certificado autofirmado, un socket que acepta y nunca responde): los **23 casos** de `verify_server` (todo correcto, 8 variantes de fallo de URL, 5 de SSH, 6 de rutas y 3 de campos obligatorios). Y en el navegador: verificacion individual con spinner, corrida masiva de las 35 con progreso y contadores, boton deshabilitado mientras corre, auto-despliegue del detalle solo en las que fallan, toggle, una plataforma que devuelve HTTP 500 contada como "sin poder verificar", y la invalidacion por firma al editar y al borrar. Cero errores de consola.

## v0.16.0 - 2026-08-12

UI: el Clonador Alexia queda con una sola vista, el menu pasa a sidebar derecho, y Administrar plataformas muestra URL + que puede hacer cada plataforma en vez de un volcado de rutas.

- **Clonador Alexia sin sub-tabs**: se elimino "Exportacion Individual" (marcado y JS: busqueda de cursos en Catalejo, formulario de 11 campos, vista previa del arbol, `pollAxJob`/`renderAxJobStatus`) y con ella el tab, que con una sola opcion no aportaba nada. La carga del Excel es ahora el contenido directo de la seccion y se llama **"Cargar datos Alexia"**. El modal del arbol de categorias **se mantiene**: lo usa la vista masiva desde la tabla de filas. Los endpoints `/api/alexia/export`, `/api/alexia/search-courses` y `/api/alexia/jobs/<id>` siguen en el backend sin uso desde la UI.
- **Logo y version**: `LOGO_FILE` pasa a `aulacloner_logo_blank.png` (el logo es blanco, va sobre el header oscuro). Se elimino el `<footer>` fijo con la version y la etiqueta `v{{APP_VERSION}}` se movio al lado derecho del logo, en la cabecera. De paso desaparece el problema de fondo del footer fijo: ya no hay nada superpuesto al contenido que scrollea.
- **Menu como sidebar derecho superpuesto**: el boton hamburguesa se posiciona contra el borde derecho del header (`absolute right-3`, fuera del contenedor `max-w-6xl`, que reserva el hueco con `pr-16`); antes quedaba con el margen del contenedor centrado y en pantallas anchas se veia despegado del borde. El panel dejo de ser un desplegable dentro del `<header>` y pasa a ser un `<aside>` fijo de 288px que entra desde la derecha con `translate-x-full` -> `translate-x-0` y un velo que cubre el fondo. Vive al final del `<body>` para no depender del contexto de apilamiento del header sticky. Cierra por el boton, por el velo y con Escape.
- **El velo usa `bg-slate-950/70`, sin `backdrop-filter`** — a proposito: WebKit dejaba esa capa de composicion sin pintar y el sintoma era "pantalla oscura sin nada" (ver v0.11.x).
- **Nuevo campo `url` en el inventario**: se agrega al formulario de plataformas ("URL de la plataforma"), a `_server_from_payload`, a `inventario.example.json` y como `setdefault("url", "")` en `load_inventory`, para que las entradas viejas sigan cargando. Se valida que empiece con `http://` o `https://` en el cliente y en el servidor (rechaza `javascript:`), y se normaliza sin `/` final. **Las 28 entradas del inventario en produccion no la traen**: hay que completarla plataforma por plataforma desde Administrar plataformas.
- **`GET /api/cc/servers` y `/api/cc/admin/servers` ahora devuelven `capabilities`**, con `plugin_install`, `course_clone` y `platform_clone`, cada uno `{ok, missing}`. Se **derivan** de los campos del inventario en `_server_capabilities`, no se guardan como flags sueltos: una entrada sin `vhost_path` no puede servir de origen al clonador de instancias, y un booleano que diga lo contrario solo mueve el error a la mitad del job. Instalar plugins y clonar cursos comparten requisitos (paramiko contra `moodle_path` con `web_user`, y paramiko acepta llave *o* contrasena); clonar la plataforma exige ademas `ssh_key_path` (autentica con `BatchMode`, no acepta contrasena), `moodledata_path` y `vhost_path`.
- **Detalle de plataforma reescrito**: nombre, URL como enlace (`target=_blank`, `rel=noopener`), "Direccion IP", la etiqueta "Llave SSH" solo cuando existe `ssh_key_path`, y las tres capacidades con tilde/cruz. Cuando una capacidad no se cumple se lista lo que falta (`— falta moodledata_path, vhost_path`), que es justamente lo que hay que completar en el formulario de al lado. Se dejaron de mostrar `Moodle:`, `Web:`, `Moodledata:`, `Vhost:`, `Sudo con contrasena:` y `Contrasena SSH:`.
- **"Instancia Moodle Origen" solo lista las plataformas que aceptan clonacion de plataforma.** Con el inventario actual eso deja **0 de 28** tal como esta hoy (ninguna tiene `moodledata_path` ni `vhost_path`); el hint debajo del selector dice cuantas se excluyeron y que campos completar. El modulo ya fallaba con esas entradas, pero recien al lanzar el job — despues de haber puesto el origen en modo mantenimiento.
- Verificado con Playwright sobre un servidor stub que sirve el inventario real: sidebar (posicion, z-index, apertura y cierre por boton, velo y Escape), ausencia del footer y del tab individual, titulo "Cargar datos Alexia", carga masiva completa (2 filas, contadores, modal del arbol), los tres cards de plataforma (con llave, sin URL, y solo con contrasena SSH), precarga de `url` al editar, y el selector de origen filtrado. Cero errores de consola. `_server_capabilities` y la validacion de `url` probadas aparte contra las 28 entradas del inventario.

## v0.15.0 - 2026-08-12

Feature: si la base destino ya tiene datos, el clonado se detiene y pide permiso explicito para eliminarla. Y el error de SSH al destino ahora nombra el firewall de Lightsail.

- **Que pasaba antes con una base destino preexistente**: `CREATE DATABASE IF NOT EXISTS` + import **no falla**, pero tampoco limpia. El dump trae `DROP TABLE`/`CREATE TABLE` por tabla, asi que sobreescribe las que trae y **deja intactas las que no**. Una base de una clonacion anterior a medias quedaba mezclada con restos desactualizados, en silencio.
- **Nueva verificacion en el preflight del script**: cuenta las tablas de `DEST_DB` en `information_schema`. Corre **despues** de las guardas de seguridad (que ya garantizaron que `DEST_DB` no es la base de origen, que no esta en `PROTECTED_DATABASES` y que el host esta en la allowlist) y **antes** de habilitar el modo mantenimiento y del dump: si hay que abortar, se aborta sin haber tocado el origen. Esto importa porque origen y destino viven en el mismo cluster Aurora.
- **`DROP_DEST_DB`** (default `0`): con tablas preexistentes y sin confirmacion, el script aborta con `Nada fue modificado` y sugiere confirmar el borrado o cambiar el nombre de la base. Con `DROP_DEST_DB=1` hace `DROP DATABASE` y la recrea vacia. En dry-run solo informa que la eliminaria.
- **Nuevo endpoint `GET /api/clone/dest-db?name=<db>`** (permiso `can_access_moodle_cloner`): devuelve `{name, exists, tables}` consultando `information_schema` en el cluster destino. No modifica nada.
- **Confirmacion en la UI antes de lanzar el job**: al pulsar Clonar (o Simular) se consulta ese endpoint y, si la base tiene tablas, se abre un modal que dice cuantas son, explica que importar encima deja restos, avisa que se va a eliminar la base completa de forma irreversible, y ofrece la alternativa de cambiar el nombre. Para habilitar el boton hay que **escribir el nombre exacto de la base**. Solo entonces se manda `drop_dest_db: true`.
- Si el endpoint falla, no bloquea: se avisa por toaster y se sigue, porque el script hace la misma verificacion y aborta por su cuenta.
- El Summary del script ahora imprime `Dest DB tables` y `Drop dest DB`.
- **Mensaje de SSH al destino reescrito**: lista en orden el firewall IPv4 de Lightsail (con la nota de que la lista IPv6 es aparte), el security group de EC2, y el estado/IP de la instancia. Incluye el comando de metadata para obtener la IP publica del clonador, que es la que hay que habilitar.
- Verificado: los 4 caminos del script (base con datos sin confirmar → aborta con exit 1; con datos confirmado → elimina; dry-run con datos → solo informa; base vacia → sigue) y los 5 del modal, extrayendo el codigo real del archivo (oculto al cargar, se abre con nombre y conteo correctos, boton deshabilitado con nombre parcial, habilitado con el exacto, y aceptar/cancelar/Escape/Enter devolviendo el valor esperado).

## v0.14.0 - 2026-08-12

Fix: la conectividad SSH a origen y destino se prueba en el preflight, antes de tocar nada. Antes se probaba el destino al 74% del job, despues del dump y del import.

- **El problema, con un caso real**: un job fallo con `exit 255` tras 10 minutos. El log mostraba `Testing SSH connectivity to ubuntu@51.44.30.62 ... Connection timed out`, pero recien **despues** de haber puesto el sitio de origen en modo mantenimiento, dumpeado la base, saneado el dump, creado la base destino e importado el dump completo. Consecuencias: el sitio de origen estuvo en mantenimiento todo ese tiempo por nada, y quedo una base destino importada sin ningun archivo al lado.
- **Preflight de SSH al destino** apenas se calculan `REMOTE_SSH_TARGET`/`SSH_OPTS`, antes de habilitar el modo mantenimiento (que ocurre ~100 lineas despues) y antes de cualquier operacion de base. El error explica que un `Connection timed out` es de red y no de credenciales, y que hay que revisar que la instancia este encendida, que la IP sea la correcta (una EC2 sin IP elastica la cambia al reiniciarse) y que el security group permita el 22 desde el host del clonador, cuyo hostname e IP se imprimen. Cierra con "Nada fue modificado en el origen ni en el destino".
- **Preflight de SSH al origen** cuando `SOURCE_MODE=remote`, con un mensaje que apunta a los campos `host`, `ssh_user` y `ssh_key_path` de la entrada del inventario e imprime la clave usada.
- **Corre tambien en dry-run**, a proposito: un dry-run que pasa sin poder alcanzar el destino da confianza falsa. En este caso el dry-run habia pasado limpio y el job real fallo por red.
- **`-o ConnectTimeout=15`** agregado a `SSH_OPTS`, `SOURCE_SSH_OPTS` y a las cadenas de rsync. El default de ssh puede tardar ~2 minutos en rendirse ante un host que no responde.
- **La etiqueta de version al pie ahora tiene fondo opaco**: es un `<footer>` fijo, asi que se superponia al contenido que scrollea por detras y las letras se mezclaban. Pasa a ser una pastilla `rounded-full` con `bg-slate-900` opaco, borde y sombra.

## v0.13.0 - 2026-08-12

Fix: elegir una plataforma del inventario ahora clona **desde el host de esa plataforma**. Antes tomaba sus rutas pero ejecutaba los comandos en el host del clonador.

- **El bug de fondo**: `validate_payload` en modo "local" leia `moodle_path`, `moodledata_path` y `vhost_path` de la entrada del inventario, pero **ignoraba el campo `host`** de esa misma entrada, y despues corria todo en la maquina del clonador. Como las entradas del inventario describen servidores remotos (traen `host`, `ssh_user`, `ssh_key_path`), la combinacion era imposible: rutas de una maquina, comandos en otra. El resultado era `config.php not found` con una ruta que si existe — en el otro servidor.
- **Ahora se deriva el host**: si el `host` de la entrada no es esta misma maquina, se pasa a ejecucion por SSH contra ese host, con `SOURCE_SSH_USER` y `SOURCE_SSH_KEY` tomados de `ssh_user` y `ssh_key_path` de la entrada. Si el host **es** esta maquina, se sigue ejecutando localmente. La comparacion usa `local_host_identifiers()`: `localhost`, `127.0.0.1`, `::1`, el hostname corto y largo, y las IPs propias resueltas por `getaddrinfo` (cacheado, se calcula una vez).
- **Terminologia corregida.** Historicamente el script corria en la propia maquina de origen y "local" era exacto; con el clonador en su propio host dejo de serlo. Se separan los dos conceptos que estaban colapsados en uno:
  - `source_origin` (`inventory` | `manual`) — de donde salen los datos del origen. Es lo que elige el usuario.
  - `source_mode` (`local` | `remote`) — donde corren los comandos. Se **deriva**, ya no se pide. Dentro del script "local" vuelve a significar literalmente "en esta maquina", que es correcto y ahora es un caso borde.
  Los valores viejos siguen aceptados como alias (`local`→`inventory`, `remote`→`manual`) para no romper una pagina cacheada durante el deploy.
- **UI**: las opciones pasan de "Instancia local (preconfigurada)" / "Servidor remoto por SSH" a **"Plataforma del inventario"** / **"Servidor manual por SSH"**, y el campo del formulario es `source_origin`.
- **Errores utiles cuando el inventario esta incompleto**: si la plataforma esta en otro host y no tiene `ssh_key_path`, el error lo dice y aclara que este modulo autentica por clave (`ssh -o BatchMode=yes -i`) y no soporta `ssh_password`. Si el `host` esta malformado, tambien. Los mensajes de rutas faltantes ahora nombran la plataforma.
- Las rutas que vienen del inventario se validan con `is_safe_path()`, igual que las cargadas a mano.
- El mensaje del script para el caso local se reescribio acorde: ahora explica que `SOURCE_MODE=local` significa que busco en la maquina del clonador, y sugiere revisar el campo `host` de la entrada del inventario.
- Verificado: plataforma en otra maquina → `script_mode=remote` con host y usuario del inventario; plataforma en esta maquina → `script_mode=local`; los dos alias legacy resuelven igual que los nuevos; y `ssh_key_path` vacio u `host` malformado fallan con mensaje explicito.

## v0.12.1 - 2026-08-12

Fix: restaurado el bit de ejecucion de `moodle-clone-web.sh`, que se perdio en v0.12.0, y el script ahora se invoca via `bash` para que ese permiso no pueda volver a romper el clonado.

- **Causa**: el commit de v0.12.0 reescribio `moodle-clone-web.sh` desde fuera de git y el modo quedo en `100644` (antes era `100755`). Git versiona el bit de ejecucion, asi que el `git pull` en el servidor dejo el archivo sin permiso de ejecucion y los jobs morian con `[Errno 13] Permission denied: '/projects/moodle-cloner/moodle-clone-web.sh'`.
- **`git update-index --chmod=+x moodle-clone-web.sh`**: el modo vuelve a `100755`.
- **`app.py` invoca `["bash", str(SCRIPT_FILE)]`** en vez de `[str(SCRIPT_FILE)]`. El bit de ejecucion deja de ser un punto unico de fallo: el mensaje que producia era `[Errno 13] Permission denied` a secas, que no dice que el problema es el permiso del script y no algo del proceso de clonado.
- **Chequeo previo mas claro**: antes de lanzar el job se valida `os.access(SCRIPT_FILE, os.R_OK)` y el error de "no encontrado" ahora incluye la ruta completa.
- Verificado quitando el bit de ejecucion: la invocacion directa reproduce el `PermissionError` exacto de produccion, y via `bash` el script arranca normalmente.

## v0.12.0 - 2026-08-12

Fix: `moodle-clone-web.sh` ahora dice en que maquina busco `config.php`, y puede leerlo cuando el usuario SSH no tiene permiso directo.

- **Mensajes de error que nombran el host.** `config.php not found at <ruta>` no decia donde habia buscado. Con `SOURCE_MODE=local` la busqueda ocurre en el **host del clonador**, no en el servidor de origen, asi que el mensaje se leia como "el archivo no existe" cuando el usuario lo estaba viendo con `ls` en la otra maquina. Ahora el error incluye `en este servidor ($(hostname))`, aclara que el modo de origen es local, y sugiere elegir 'Servidor remoto por SSH'. La rama remota nombra `${SOURCE_SSH_TARGET}`.
- **`parse_cfg` en modo remoto reintenta con `sudo -n`.** La rama local ya usaba `sudo awk`; la remota corria `awk` pelado, asi que con permisos tipo `-r--rw---- www-data:support` (que no dan nada a "others") el usuario SSH no podia leer el archivo. Y como `parse_cfg` se invoca dentro de `$(...)`, el codigo de salida se descartaba: las variables quedaban vacias en silencio y el fallo aparecia despues, como un error confuso de dbname. Ahora es `awk ... 2>/dev/null || sudo -n awk ...` (`-n` para no colgarse esperando password).
- **El chequeo de existencia no detectaba esto**: `test -f` solo necesita traspasar el directorio, no leer el archivo, asi que pasaba igual. Verificado con el modo exacto `-r--rw----`: `test -f` pasa, `awk` directo falla con exit 2 y stdout vacio, y el fallback con `sudo -n` recupera el valor.
- **Mensaje de dbname vacio mas util**: en modo remoto ahora aclara que el archivo existe pero no se pudo leer como `${SOURCE_SSH_TARGET}` ni con `sudo -n`, y que hay que revisar permisos y el sudo sin password en el origen.
- El programa awk se extrajo a `CFG_AWK_PROG` para que las dos ramas usen exactamente el mismo, en vez de mantener dos copias con escapes distintos. Verificado que ambas ramas extraen identico `dbhost`/`dbname`/`dbuser`/`dbpass`/`wwwroot`, incluyendo un password con espacios, `&` y `$`.

## v0.11.3 - 2026-08-11

Fix: `/` ahora manda `ETag`, `Cache-Control` y `X-App-Version`, para poder distinguir un problema de render de un HTML cacheado.

- **El problema**: `_send_file()` respondia con `Content-Type` y `Content-Length` y nada mas. Sin `ETag`, sin `Last-Modified` y sin `Cache-Control`, el navegador no tiene contra que revalidar el HTML y puede servir una copia cacheada heuristicamente por tiempo indefinido (Safari es especialmente agresivo). Consecuencia practica: ante cualquier sintoma visual era imposible responder "¿el navegador esta viendo el deploy nuevo o uno viejo?", que es justo la ambiguedad que costo horas el 2026-08-11.
- **`ETag`**: `sha256` de los bytes ya renderizados (despues de sustituir `{{APP_VERSION}}` y `{{TARGET_DB_HOST}}`), primeros 32 hex. Cambia con cualquier cambio de contenido o de version.
- **`Cache-Control: no-cache, must-revalidate`**: `no-cache` significa "revalida siempre", no "no guardes", asi que el `ETag` sigue dando 304 baratos en vez de retransmitir 222 KB en cada carga.
- **`If-None-Match` → 304**: implementado en `_if_none_match_matches()`, tolerante a validadores debiles (`W/"..."`), a listas separadas por coma y a `*`.
- **`X-App-Version`**: permite confirmar desde `curl -I` o desde la pestaña Network que build recibio el navegador, sin parsear el HTML.
- Verificado con 5 casos: sin `If-None-Match` → 200 + `ETag`; con el `ETag` correcto → 304 sin cuerpo; con `W/` → 304; con un `ETag` viejo → 200 completo; con lista de `ETag`s → 304.

## v0.11.2 - 2026-08-11

Limpieza de repo: quitados de git 6 archivos vacios que se colaron en el commit `4fad552`.

- **`_to_delete/git-stale-locks/*` destrackeado** (`git rm -r --cached`). Eran `.lock` vacios que dejo git al operar sobre la carpeta montada desde una sesion de Cowork, donde `rm` esta bloqueado: en vez de borrarse se movieron a `_to_delete/` y de ahi entraron al commit del merge. No afectan a la app, pero ensucian el arbol y aparecen en cualquier `git diff` entre ramas.
- **`.gitignore`: agregado `_to_delete/`** para que no vuelva a pasar.
- Los archivos siguen en disco (destrackear no los borra); la carpeta `_to_delete/` se elimina a mano.
- Nota de deploy: el checkout del servidor habia divergido de `origin/main` por un merge hecho ahi mismo (`4c9f2a0`). Un checkout de deploy no debe tener historia propia — alinearlo con `git reset --hard origin/main` y fijar `git config pull.ff only` para que un pull divergente falle en vez de mergear.

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
