#!/usr/bin/env bash
set -Eeuo pipefail

log()  { printf "[INFO] %s\n" "$*"; }
warn() { printf "[WARN] %s\n" "$*"; }
err()  { printf "[ERROR] %s\n" "$*"; }

bool_true() {
  case "${1:-}" in
    1|true|TRUE|True|yes|YES|y|Y|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

require_cmd() {
  local missing=()
  for c in "$@"; do command -v "$c" >/dev/null 2>&1 || missing+=("$c"); done
  if ((${#missing[@]})); then
    err "Missing required commands: ${missing[*]}"
    exit 1
  fi
}

require_non_empty() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    err "Missing required environment variable: $name"
    exit 1
  fi
}

require_safe_db_name() {
  local db="$1"
  if [[ ! "$db" =~ ^[A-Za-z0-9_]+$ ]]; then
    err "Invalid DEST_DB '$db'. Only letters, numbers and underscore are allowed."
    exit 1
  fi
}

cleanup() {
  if [[ "${SRC_MAINT_ENABLED:-0}" == "1" && "${REVERT_SRC_MAINT:-0}" == "1" ]]; then
    log "Disabling maintenance mode on source (cleanup)…"
    sudo -u www-data "$PHP_CLI" "$SRC_DIR/admin/cli/maintenance.php" --disable || true
  fi

  if [[ -n "${TMP_DIR:-}" && -d "${TMP_DIR:-}" ]]; then
    rm -rf "$TMP_DIR" || true
  fi
}
trap cleanup EXIT

PHP_CLI="${PHP_CLI:-/usr/bin/php}"
require_cmd rsync sed grep awk mysql mysqldump "$PHP_CLI" nginx
if command -v certbot >/dev/null 2>&1; then HAS_CERTBOT=1; else HAS_CERTBOT=0; fi

# Required inputs
SRC_DIR="${SRC_DIR:-}"
SRC_DATA="${SRC_DATA:-}"
SRC_VHOST="${SRC_VHOST:-}"
NEW_KEY="${NEW_KEY:-}"
DB_PASS="${DB_PASS:-}"

require_non_empty SRC_DIR
require_non_empty SRC_DATA
require_non_empty SRC_VHOST
require_non_empty NEW_KEY
require_non_empty DB_PASS

# Derived / overridable inputs
NEW_DOMAIN="${NEW_DOMAIN:-${NEW_KEY}.awakelab.world}"
NEW_URL="${NEW_URL:-https://${NEW_DOMAIN}}"
DEST_DIR="${DEST_DIR:-/var/www/moodle_${NEW_KEY}}"
DEST_DATA="${DEST_DATA:-/var/moodledata_${NEW_KEY}}"
DEST_DB="${DEST_DB:-moodle_${NEW_KEY}}"

ENABLE_SRC_MAINT="${ENABLE_SRC_MAINT:-1}"
ENABLE_REPLACE="${ENABLE_REPLACE:-1}"
ENABLE_PURGE="${ENABLE_PURGE:-1}"
ENABLE_NGINX="${ENABLE_NGINX:-1}"
ENABLE_CERTBOT="${ENABLE_CERTBOT:-1}"
ENABLE_CRON="${ENABLE_CRON:-1}"
DISABLE_NEW_MAINT="${DISABLE_NEW_MAINT:-1}"
DISABLE_SRC_MAINT_AFTER="${DISABLE_SRC_MAINT_AFTER:-1}"
DRY_RUN="${DRY_RUN:-0}"

CONFIG_FILE="$SRC_DIR/config.php"
[[ -f "$CONFIG_FILE" ]] || { err "config.php not found at $CONFIG_FILE"; exit 1; }

parse_cfg() {
  local key="$1"
  sudo awk -v k="$key" -F"'" '
    $0 ~ "\\$CFG->" k "[[:space:]]*=" { print $2; exit }
  ' "$CONFIG_FILE"
}

SRC_DBHOST="$(parse_cfg dbhost)"
SRC_DBNAME="${SRC_DBNAME_OVERRIDE:-$(parse_cfg dbname)}"
SRC_DBUSER="$(parse_cfg dbuser)"
SRC_WWWROOT="$(parse_cfg wwwroot)"

if [[ -z "${SRC_DBNAME:-}" ]]; then
  err "Could not read source dbname from $CONFIG_FILE (and SRC_DBNAME_OVERRIDE was not set)."
  exit 1
fi

if [[ -z "${SRC_WWWROOT:-}" ]]; then
  warn "Could not read source wwwroot from $CONFIG_FILE (URL replace may be skipped)."
fi

DB_HOST="${DB_HOST:-${SRC_DBHOST:-localhost}}"
DB_USER="${DB_USER:-${SRC_DBUSER:-admin_moodle}}"
require_non_empty DB_HOST
require_non_empty DB_USER
require_safe_db_name "$DEST_DB"

if bool_true "$ENABLE_SRC_MAINT"; then
  log "Enabling maintenance mode on source…"
  sudo -u www-data "$PHP_CLI" "$SRC_DIR/admin/cli/maintenance.php" --enable
  SRC_MAINT_ENABLED=1
else
  SRC_MAINT_ENABLED=0
fi

# Default cleanup behavior early, so failures before the end still revert source maintenance when requested.
if [[ "${SRC_MAINT_ENABLED:-0}" == "1" ]] && bool_true "$DISABLE_SRC_MAINT_AFTER"; then
  REVERT_SRC_MAINT=1
else
  REVERT_SRC_MAINT=0
fi

cat <<SUM
--- Summary ---
Source:           $SRC_DIR
Source data:      $SRC_DATA
Source vhost:     $SRC_VHOST
Source DB:        $SRC_DBNAME
Source wwwroot:   ${SRC_WWWROOT:-unknown}
New key:          $NEW_KEY
New domain:       $NEW_DOMAIN
New URL:          $NEW_URL
Dest dir:         $DEST_DIR
Dest moodledata:  $DEST_DATA
Dest database:    $DEST_DB
RDS host:         $DB_HOST
DB user:          $DB_USER
Dry-run:          $DRY_RUN
Maintenance on source now: $SRC_MAINT_ENABLED
----------------
SUM

if bool_true "$DRY_RUN"; then
  log "Dry-run enabled. No changes were applied."
  REVERT_SRC_MAINT=1
  exit 0
fi

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
(
  export MYSQL_PWD="$DB_PASS"
  mysql -h "$DB_HOST" -u "$DB_USER" \
    -e "CREATE DATABASE IF NOT EXISTS ${DEST_DB} DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
)

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
if (!$conf) { fwrite(STDERR, "Usage: php config_patch.php <dest_config.php>\n"); exit(2); }
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
  "$PHP_CLI" "$PATCH_PHP" "$DEST_DIR/config.php"

if bool_true "$ENABLE_NGINX"; then
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
else
  warn "Skipping Nginx setup."
fi

if bool_true "$ENABLE_CERTBOT" && [[ "$HAS_CERTBOT" == "1" ]]; then
  CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
  if [[ -n "$CERTBOT_EMAIL" ]]; then
    sudo certbot --nginx -d "$NEW_DOMAIN" --non-interactive --agree-tos --redirect -m "$CERTBOT_EMAIL" || warn "Certbot failed; run it later."
  else
    sudo certbot --nginx -d "$NEW_DOMAIN" --non-interactive --agree-tos --redirect --register-unsafely-without-email || warn "Certbot failed; run it later."
  fi
else
  warn "Skipping Certbot (disabled or not installed)."
fi

if bool_true "$ENABLE_REPLACE"; then
  if [[ -n "${SRC_WWWROOT:-}" && "$SRC_WWWROOT" != "$NEW_URL" ]]; then
    log "Replacing URLs in DB: $SRC_WWWROOT -> $NEW_URL …"
    pushd "$DEST_DIR" >/dev/null
    set +e
    sudo -u www-data "$PHP_CLI" admin/tool/replace/cli/replace.php --non-interactive --search="$SRC_WWWROOT" --replace="$NEW_URL"
    REPLACE_RC=$?
    set -e
    popd >/dev/null
    if [[ $REPLACE_RC -ne 0 ]]; then
      warn "URL replace failed with exit code $REPLACE_RC. Continuing clone without blocking."
    fi
  else
    warn "Skipping URL replace (source wwwroot unavailable or equal to new URL)."
  fi
else
  warn "Skipping URL replace."
fi

if bool_true "$ENABLE_PURGE"; then
  log "Purging caches …"
  pushd "$DEST_DIR" >/dev/null
  sudo -u www-data "$PHP_CLI" admin/cli/purge_caches.php || true
  popd >/dev/null
else
  warn "Skipping cache purge."
fi

if bool_true "$ENABLE_CRON"; then
  log "Ensuring cron entry for www-data …"
  CRON_LINE="*/1 * * * * /usr/bin/php ${DEST_DIR}/admin/cli/cron.php >/dev/null 2>&1"
  if sudo crontab -u www-data -l 2>/dev/null | grep -Fq "${DEST_DIR}/admin/cli/cron.php"; then
    log "Cron entry already present for this instance."
  else
    ( sudo crontab -u www-data -l 2>/dev/null; echo "$CRON_LINE" ) | sudo crontab -u www-data -
    log "Cron entry added."
  fi
else
  warn "Skipping cron setup."
fi

if bool_true "$DISABLE_NEW_MAINT"; then
  sudo -u www-data "$PHP_CLI" "$DEST_DIR/admin/cli/maintenance.php" --disable || true
fi

if [[ "${SRC_MAINT_ENABLED:-0}" == "1" ]] && bool_true "$DISABLE_SRC_MAINT_AFTER"; then
  REVERT_SRC_MAINT=1
else
  REVERT_SRC_MAINT=0
fi

log "Clone completed. New site should be available at: $NEW_URL"
log "Moodle code: $DEST_DIR"
log "Moodledata:  $DEST_DATA"
log "Database:    $DEST_DB on $DB_HOST (user $DB_USER)"
