#!/usr/bin/env bash
set -Eeuo pipefail

log()  { printf "[INFO] %s\n" "$*"; }
warn() { printf "[WARN] %s\n" "$*"; }
err()  { printf "[ERROR] %s\n" "$*"; }

confirm() {
  local prompt="${1:-Are you sure?}" default="${2:-Y}" yn
  if [[ "$default" =~ ^[Yy]$ ]]; then
    read -r -p "$prompt [Y/n]: " yn || true
    [[ -z "${yn:-}" || "$yn" =~ ^[Yy]$ ]]
  else
    read -r -p "$prompt [y/N]: " yn || true
    [[ "${yn:-}" =~ ^[Yy]$ ]]
  fi
}

prompt_def() {
  local varname="$1" message="$2" def="$3" val
  read -r -p "$message [$def]: " val || true
  [[ -z "${val:-}" ]] && val="$def"
  printf -v "$varname" '%s' "$val"
}

require_cmd() {
  local missing=()
  for c in "$@"; do command -v "$c" >/dev/null 2>&1 || missing+=("$c"); done
  if ((${#missing[@]})); then
    err "Missing required commands: ${missing[*]}"; exit 1
  fi
}

cleanup() {
  if [[ "${SRC_MAINT_ENABLED:-0}" == "1" && "${REVERT_SRC_MAINT:-0}" == "1" ]]; then
    log "Disabling maintenance mode on source (cleanup)…"
    sudo -u www-data /usr/bin/php "$SRC_DIR/admin/cli/maintenance.php" --disable || true
  fi
}
trap cleanup EXIT

# Defaults
SRC_DIR_DEF="/var/www/moodle_digitinstitute"
SRC_DATA_DEF="/var/moodledata_digitinstitute"
SRC_VHOST_DEF="/etc/nginx/sites-available/moodle_digitinstitute"
PHP_CLI="/usr/bin/php"

require_cmd rsync sed grep awk mysql mysqldump "$PHP_CLI" nginx
if command -v certbot >/dev/null 2>&1; then HAS_CERTBOT=1; else HAS_CERTBOT=0; fi

log "Collecting inputs…"
prompt_def SRC_DIR   "Source Moodle dir"   "$SRC_DIR_DEF"
prompt_def SRC_DATA  "Source MoodleData"   "$SRC_DATA_DEF"
prompt_def SRC_VHOST "Source Nginx vhost"  "$SRC_VHOST_DEF"

prompt_def NEW_KEY    "New instance key (used for dirs/db), e.g. nuevaplataforma" "nuevaplataforma"
NEW_DOMAIN_DEF="${NEW_KEY}.awakelab.world"
prompt_def NEW_DOMAIN "New domain (FQDN)" "$NEW_DOMAIN_DEF"

DEST_DIR="/var/www/moodle_${NEW_KEY}"
DEST_DATA="/var/moodledata_${NEW_KEY}"
DEST_DB="moodle_${NEW_KEY}"

NEW_URL_DEF="https://${NEW_DOMAIN}"
prompt_def NEW_URL "New base URL (wwwroot)" "$NEW_URL_DEF"

CONFIG_FILE="$SRC_DIR/config.php"
[[ -f "$CONFIG_FILE" ]] || { err "config.php not found at $CONFIG_FILE"; exit 1; }

parse_cfg() {
  local key="$1"
  awk -v k="$key" -F"'" '
    $0 ~ "\\$CFG->" k "[[:space:]]*=" { print $2; exit }
  ' "$CONFIG_FILE"
}

SRC_DBHOST="$(parse_cfg dbhost)"
SRC_DBNAME="$(parse_cfg dbname)"
SRC_DBUSER="$(parse_cfg dbuser)"
SRC_WWWROOT="$(parse_cfg wwwroot)"

if [[ -z "${SRC_DBNAME:-}" ]]; then
  echo "[ERROR] No pude leer \$CFG->dbname desde $CONFIG_FILE"
  read -r -p "Ingresa el nombre de la BD origen (ej: moodle_digitinstitute): " SRC_DBNAME
fi

if [[ -z "${SRC_WWWROOT:-}" ]]; then
  echo "[WARN] No pude leer \$CFG->wwwroot desde $CONFIG_FILE (replace.php podría omitirse)"
fi


if confirm "Enable maintenance mode on source ($SRC_DIR)?" Y; then
  log "Enabling maintenance mode on source…"
  sudo -u www-data "$PHP_CLI" "$SRC_DIR/admin/cli/maintenance.php" --enable
  SRC_MAINT_ENABLED=1
else
  SRC_MAINT_ENABLED=0
fi

DB_HOST_DEF="${SRC_DBHOST:-localhost}"
DB_USER_DEF="${SRC_DBUSER:-admin_moodle}"
prompt_def DB_HOST "RDS host" "$DB_HOST_DEF"
prompt_def DB_USER "DB user for dump/import and (try) create DB" "$DB_USER_DEF"

read -rs -p "Password for DB user $DB_USER: " DB_PASS; echo
export DB_PASS

cat <<SUM
--- Summary ---
Source:           $SRC_DIR
Source data:      $SRC_DATA
Source vhost:     $SRC_VHOST
Source wwwroot:   ${SRC_WWWROOT:-unknown}
New key:          $NEW_KEY
New domain:       $NEW_DOMAIN
New URL:          $NEW_URL
Dest dir:         $DEST_DIR
Dest moodledata:  $DEST_DATA
Dest database:    $DEST_DB
RDS host:         $DB_HOST
DB user:          $DB_USER
Maintenance on source now: $SRC_MAINT_ENABLED
----------------
SUM

confirm "Proceed with cloning?" Y || { warn "Aborted by user"; exit 1; }

log "Copying code to $DEST_DIR …"
sudo rsync -a "$SRC_DIR/" "$DEST_DIR/"
sudo chown -R www-data:www-data "$DEST_DIR"

log "Copying moodledata to $DEST_DATA … (may take time)"
sudo mkdir -p "$DEST_DATA"
sudo rsync -a "$SRC_DATA/" "$DEST_DATA/"
sudo chown -R www-data:www-data "$DEST_DATA"
sudo find "$DEST_DATA" -type d -exec chmod 770 {} \;
sudo find "$DEST_DATA" -type f -exec chmod 660 {} \;

TMP_DIR="$(mktemp -d)"
DUMP_ORIG="$TMP_DIR/${SRC_DBNAME}.sql"
DUMP_SAN="$TMP_DIR/${SRC_DBNAME}.sanitized.sql"

log "Dumping database $SRC_DBNAME from $DB_HOST …"
(
  export MYSQL_PWD="$DB_PASS"
  mysqldump -h "$DB_HOST" -u "$DB_USER" \
    --single-transaction --quick --set-gtid-purged=OFF \
    "$SRC_DBNAME" > "$DUMP_ORIG"
)

log "Sanitizing dump to remove privileged statements…"
sed '/SQL_LOG_BIN/d; /GTID_PURGED/d' "$DUMP_ORIG" > "$DUMP_SAN"

log "Ensuring destination database $DEST_DB exists…"
set +e
(
  export MYSQL_PWD="$DB_PASS"
  mysql -h "$DB_HOST" -u "$DB_USER" \
    -e "CREATE DATABASE IF NOT EXISTS ${DEST_DB} DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
)
CREATE_RC=$?
set -e
if [[ $CREATE_RC -ne 0 ]]; then
  warn "Could not create database with user $DB_USER. Create it manually, then press Enter to continue."
  read -r -p "Press Enter to continue… " _
fi

log "Importing dump into $DEST_DB …"
(
  export MYSQL_PWD="$DB_PASS"
  mysql -h "$DB_HOST" -u "$DB_USER" "$DEST_DB" < "$DUMP_SAN"
)

log "Patching $DEST_DIR/config.php …"
PATCH_PHP="$TMP_DIR/config_patch.php"
cat > "$PATCH_PHP" <<'PHP'
<?php
$conf = $argv[1] ?? null;
$orig = $argv[2] ?? null;
if (!$conf) { fwrite(STDERR, "Usage: php config_patch.php <dest_config.php> [source_config.php]\n"); exit(2); }

$keys = ['wwwroot','dataroot','dirroot','dbhost','dbname','dbuser','dbpass'];
$map = [];
foreach ($keys as $k) { $map[$k] = getenv(strtoupper($k)) ?: ''; }

$c = file_get_contents($conf);
if ($c === false) { fwrite(STDERR, "Cannot read $conf\n"); exit(1); }

foreach ($map as $k => $v) {
    if ($v === '') continue;
    $pattern = "~(\\$CFG->" . preg_quote($k, "~") . "\\s*=\\s*)'[^'\\\\]*';~m";
    $replace = "\$1'".addslashes($v)."';";
    $count = 0;
    $c2 = preg_replace($pattern, $replace, $c, 1, $count);
    if ($count === 0) {
        $ins = "\n".'$CFG->'.$k." = '".addslashes($v)."';\n";
        $icount = 0;
        $c2 = preg_replace('~\n\s*require_once\(.*lib/setup\.php.*\);~m', $ins.'$0', $c, 1, $icount);
        if ($icount === 0) { $c2 = $c . $ins; }
    }
    $c = $c2;
}

if (file_put_contents($conf, $c) === false) { fwrite(STDERR, "Cannot write $conf\n"); exit(1); }
PHP

sudo env \
  WWWROOT="$NEW_URL" \
  DATAROOT="$DEST_DATA" \
  DIRROOT="$DEST_DIR" \
  DBHOST="$DB_HOST" \
  DBNAME="$DEST_DB" \
  DBUSER="$DB_USER" \
  DBPASS="$DB_PASS" \
  "$PHP_CLI" "$PATCH_PHP" "$DEST_DIR/config.php" "$SRC_DIR/config.php"

# -------- Nginx vhost (clean HTTP template, no legacy Certbot lines) --------
NEW_VHOST="/etc/nginx/sites-available/${NEW_DOMAIN}"
log "Creating Nginx vhost at $NEW_VHOST …"

sudo tee "$NEW_VHOST" > /dev/null <<NGINX
server {
    listen 80;
    server_name ${NEW_DOMAIN};

    root ${DEST_DIR};
    index index.php index.html index.htm;
    client_max_body_size 4G;

    location / {
        try_files \$uri \$uri/ /index.php?\$query_string;
    }

    location ~ [^/]\.php(/|$) {
        fastcgi_split_path_info ^(.+\.php)(/.+)\$;
        fastcgi_index index.php;
        fastcgi_pass unix:/run/php/php8.1-fpm.sock;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        fastcgi_param PATH_INFO \$fastcgi_path_info;
    }

    location ~* \.(jpg|jpeg|gif|png|svg|css|js|ico|webp)\$ {
        expires 30d;
        access_log off;
    }

    access_log /var/log/nginx/${NEW_DOMAIN}.access.log;
    error_log  /var/log/nginx/${NEW_DOMAIN}.error.log;
}
NGINX

sudo ln -sfn "$NEW_VHOST" "/etc/nginx/sites-enabled/${NEW_DOMAIN}"
sudo nginx -t
sudo systemctl reload nginx

if [[ "$HAS_CERTBOT" == "1" ]] && confirm "Obtain/renew Let's Encrypt certificate for ${NEW_DOMAIN} now?" Y; then
  sudo certbot --nginx -d "$NEW_DOMAIN" || warn "Certbot failed; run it later."
else
  warn "Skipping Certbot (not installed or user chose No)."
fi

if [[ -n "${SRC_WWWROOT:-}" && "$SRC_WWWROOT" != "$NEW_URL" ]]; then
  log "Replacing URLs in DB: $SRC_WWWROOT -> $NEW_URL …"
  pushd "$DEST_DIR" >/dev/null
  sudo -u www-data "$PHP_CLI" admin/tool/replace/cli/replace.php --search="$SRC_WWWROOT" --replace="$NEW_URL"
  popd >/dev/null
fi

log "Purging caches …"
pushd "$DEST_DIR" >/dev/null
sudo -u www-data "$PHP_CLI" admin/cli/purge_caches.php || true
popd >/dev/null

log "Ensuring cron entry for www-data …"
CRON_LINE="*/1 * * * * /usr/bin/php ${DEST_DIR}/admin/cli/cron.php >/dev/null 2>&1"
set +e
if sudo crontab -u www-data -l 2>/dev/null | grep -Fq "${DEST_DIR}/admin/cli/cron.php"; then
  log "Cron entry already present for this instance."
else
  ( sudo crontab -u www-data -l 2>/dev/null; echo "$CRON_LINE" ) | sudo crontab -u www-data -
  log "Cron entry added."
fi
set -e

if confirm "Disable maintenance mode on NEW instance now?" Y; then
  sudo -u www-data "$PHP_CLI" "$DEST_DIR/admin/cli/maintenance.php" --disable || true
fi
if [[ "$SRC_MAINT_ENABLED" == "1" ]] && confirm "Disable maintenance mode on SOURCE instance now?" Y; then
  REVERT_SRC_MAINT=1
else
  REVERT_SRC_MAINT=0
fi

log "Clone completed. New site should be available at: $NEW_URL"
log "Nginx vhost: $NEW_VHOST"
log "Moodle code: $DEST_DIR"
log "Moodledata:  $DEST_DATA"
log "Database:    $DEST_DB on $DB_HOST (user $DB_USER)"

rm -rf "$TMP_DIR"