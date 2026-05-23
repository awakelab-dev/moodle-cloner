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

require_safe_db_identifier() {
  local name="$1"
  local label="$2"
  if [[ ! "$name" =~ ^[A-Za-z0-9_]+$ ]]; then
    err "Invalid ${label} '$name'. Only letters, numbers and underscore are allowed."
    exit 1
  fi
}

is_in_csv_list() {
  local needle="$1"
  local csv="$2"
  local item
  IFS=',' read -r -a __items <<< "$csv"
  for item in "${__items[@]}"; do
    item="${item//[[:space:]]/}"
    [[ -z "$item" ]] && continue
    if [[ "$needle" == "$item" ]]; then
      return 0
    fi
  done
  return 1
}

enforce_db_host_allowlist() {
  local host="$1"
  local allowlist_csv="$2"
  if [[ -z "$allowlist_csv" ]]; then
    return 0
  fi
  if ! is_in_csv_list "$host" "$allowlist_csv"; then
    err "TARGET_DB_HOST '$host' is not in RDS_HOST_ALLOWLIST. Aborting for safety."
    exit 1
  fi
}

enforce_protected_db_blocklist() {
  local dbname="$1"
  local protected_csv="$2"
  if [[ -z "$protected_csv" ]]; then
    return 0
  fi
  if is_in_csv_list "$dbname" "$protected_csv"; then
    err "DEST_DB '$dbname' is listed in PROTECTED_DATABASES. Aborting for safety."
    exit 1
  fi
}

require_production_ack_if_needed() {
  local safe_mode="$1"
  local dry_run="$2"
  local ack="${I_UNDERSTAND_PRODUCTION_RDS:-}"
  if bool_true "$safe_mode" && ! bool_true "$dry_run"; then
    if ! bool_true "$ack"; then
      err "SAFE_MODE is enabled and this is not a dry-run. Set I_UNDERSTAND_PRODUCTION_RDS=true to proceed."
      exit 1
    fi
  fi
}

quote_shell() {
  printf '%q' "$1"
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

remote_exec() {
  local cmd="$1"
  if [[ -z "${REMOTE_SSH_TARGET:-}" ]]; then
    err "Remote host not configured."
    exit 1
  fi
  ssh "${SSH_OPTS[@]}" "$REMOTE_SSH_TARGET" "$cmd"
}

source_exec() {
  local cmd="$1"
  if [[ "${SOURCE_MODE:-local}" == "remote" ]]; then
    ssh "${SOURCE_SSH_OPTS[@]}" "$SOURCE_SSH_TARGET" "$cmd"
  else
    bash -lc "$cmd"
  fi
}

remote_bash() {
  local script="$1"
  remote_exec "bash -lc $(quote_shell "$script")"
}

remote_sudo_bash() {
  local script="$1"
  remote_exec "sudo bash -lc $(quote_shell "$script")"
}

PHP_CLI="${PHP_CLI:-php}"
require_cmd rsync sed grep awk mysql mysqldump "$PHP_CLI"
if command -v certbot >/dev/null 2>&1; then HAS_CERTBOT=1; else HAS_CERTBOT=0; fi

# Required inputs
SRC_DIR="${SRC_DIR:-}"
SRC_DATA="${SRC_DATA:-}"
SRC_VHOST="${SRC_VHOST:-}"
NEW_KEY="${NEW_KEY:-}"
SOURCE_MODE="${SOURCE_MODE:-local}"
SOURCE_HOST="${SOURCE_HOST:-}"
SOURCE_SSH_USER="${SOURCE_SSH_USER:-ubuntu}"
SOURCE_SSH_KEY="${SOURCE_SSH_KEY:-$HOME/.ssh/id_ed25519}"

require_non_empty SRC_DIR
require_non_empty SRC_DATA
require_non_empty SRC_VHOST
require_non_empty NEW_KEY

if [[ "$SOURCE_MODE" != "local" && "$SOURCE_MODE" != "remote" ]]; then
  err "SOURCE_MODE must be 'local' or 'remote'."
  exit 1
fi

DEPLOY_TARGET="${DEPLOY_TARGET:-local}"
DEPLOY_TARGET="$(printf '%s' "$DEPLOY_TARGET" | tr '[:upper:]' '[:lower:]')"
REMOTE_HOST="${REMOTE_HOST:-51.44.30.62}"
REMOTE_SSH_KEY="${REMOTE_SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_SSH_USER="${REMOTE_SSH_USER:-ubuntu}"

if [[ "$DEPLOY_TARGET" != "local" && "$DEPLOY_TARGET" != "remote" ]]; then
  err "DEPLOY_TARGET must be 'local' or 'remote'."
  exit 1
fi

# Derived / overridable inputs
NEW_DOMAIN="${NEW_DOMAIN:-${NEW_KEY}.awakelab.world}"
NEW_URL="${NEW_URL:-https://${NEW_DOMAIN}}"
if [[ "$DEPLOY_TARGET" == "remote" ]]; then
  DEST_DIR="${DEST_DIR:-/var/www/html/moodle/${NEW_KEY}}"
  DEST_DATA="${DEST_DATA:-/var/www/data/moodle/${NEW_KEY}}"
else
  DEST_DIR="${DEST_DIR:-/var/www/moodle_${NEW_KEY}}"
  DEST_DATA="${DEST_DATA:-/var/moodledata_${NEW_KEY}}"
fi
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
SAFE_MODE="${SAFE_MODE:-1}"
RDS_HOST_ALLOWLIST="${RDS_HOST_ALLOWLIST:-}"
PROTECTED_DATABASES="${PROTECTED_DATABASES:-}"

SSH_OPTS=()
REMOTE_SSH_TARGET=""
RSYNC_SSH=""
SOURCE_SSH_OPTS=()
SOURCE_SSH_TARGET=""
SOURCE_RSYNC_SSH=""
if [[ "$DEPLOY_TARGET" == "remote" ]]; then
  require_cmd ssh
  [[ -f "$REMOTE_SSH_KEY" ]] || { err "SSH key not found at $REMOTE_SSH_KEY"; exit 1; }

  if [[ ! "$REMOTE_HOST" =~ ^[A-Za-z0-9.-]+$ ]]; then
    err "Invalid REMOTE_HOST '$REMOTE_HOST'."
    exit 1
  fi

  if [[ "$DEST_DIR" != /var/www/html/moodle/* ]]; then
    err "For remote deploy, DEST_DIR must start with /var/www/html/moodle/"
    exit 1
  fi

  if [[ "$DEST_DATA" != /var/www/data/moodle/* ]]; then
    err "For remote deploy, DEST_DATA must start with /var/www/data/moodle/"
    exit 1
  fi

  REMOTE_SSH_TARGET="${REMOTE_SSH_USER}@${REMOTE_HOST}"
  SSH_OPTS=(-i "$REMOTE_SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
  RSYNC_SSH="ssh -i $REMOTE_SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
fi

if [[ "$SOURCE_MODE" == "remote" ]]; then
  require_cmd ssh
  [[ -f "$SOURCE_SSH_KEY" ]] || { err "Source SSH key not found at $SOURCE_SSH_KEY"; exit 1; }
  if [[ ! "$SOURCE_HOST" =~ ^[A-Za-z0-9.-]+$ ]]; then
    err "Invalid SOURCE_HOST '$SOURCE_HOST'."
    exit 1
  fi
  SOURCE_SSH_TARGET="${SOURCE_SSH_USER}@${SOURCE_HOST}"
  SOURCE_SSH_OPTS=(-i "$SOURCE_SSH_KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new)
  SOURCE_RSYNC_SSH="ssh -i $SOURCE_SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
fi

if bool_true "$ENABLE_NGINX" && [[ "$DEPLOY_TARGET" == "local" ]]; then
  require_cmd nginx
fi

CONFIG_FILE="$SRC_DIR/config.php"
if [[ "$SOURCE_MODE" == "local" ]]; then
  [[ -f "$CONFIG_FILE" ]] || { err "config.php not found at $CONFIG_FILE"; exit 1; }
else
  source_exec "test -f $(quote_shell "$CONFIG_FILE")" || { err "Remote config.php not found at $CONFIG_FILE"; exit 1; }
fi

parse_cfg() {
  local key="$1"
  if [[ "$SOURCE_MODE" == "local" ]]; then
    sudo awk -v k="$key" -F"'" '
    $0 ~ "\\$CFG->" k "[[:space:]]*=" { print $2; exit }
  ' "$CONFIG_FILE"
  else
    source_exec "awk -v k=$(quote_shell "$key") -F\' '\$0 ~ \"\\\\\$CFG->\" k \"[[:space:]]*=\" { print \$2; exit }' $(quote_shell "$CONFIG_FILE")"
  fi
}

SRC_DBHOST_CFG="$(parse_cfg dbhost)"
SRC_DBNAME="$(parse_cfg dbname)"
SRC_DBUSER_CFG="$(parse_cfg dbuser)"
SRC_DBPASS_CFG="$(parse_cfg dbpass)"
SRC_WWWROOT="$(parse_cfg wwwroot)"

if [[ -z "${SRC_DBNAME:-}" ]]; then
  err "Could not read source dbname from $CONFIG_FILE."
  exit 1
fi

if [[ -z "${SRC_WWWROOT:-}" ]]; then
  warn "Could not read source wwwroot from $CONFIG_FILE (URL replace may be skipped)."
fi

SOURCE_DB_HOST="${SRC_DBHOST_CFG:-localhost}"
SOURCE_DB_USER="${SRC_DBUSER_CFG:-admin_moodle}"
TARGET_DB_HOST="${TARGET_DB_HOST:-${DB_HOST:-${SRC_DBHOST_CFG:-localhost}}}"
TARGET_DB_USER="${TARGET_DB_USER:-${DB_USER:-${SRC_DBUSER_CFG:-admin_moodle}}}"
TARGET_DB_PASS="${TARGET_DB_PASS:-${DB_PASS:-}}"
SOURCE_DB_PASS="${SRC_DBPASS_CFG:-}"

require_non_empty SOURCE_DB_HOST
require_non_empty SOURCE_DB_USER
require_non_empty SOURCE_DB_PASS
require_non_empty TARGET_DB_HOST
require_non_empty TARGET_DB_USER
require_non_empty TARGET_DB_PASS
require_safe_db_name "$DEST_DB"
require_safe_db_identifier "$SRC_DBNAME" "source database name"
require_safe_db_identifier "$DEST_DB" "destination database name"
enforce_db_host_allowlist "$TARGET_DB_HOST" "$RDS_HOST_ALLOWLIST"
enforce_protected_db_blocklist "$DEST_DB" "$PROTECTED_DATABASES"

if [[ "$DEST_DB" == "$SRC_DBNAME" ]]; then
  err "DEST_DB must be different from source DB ($SRC_DBNAME). Aborting for safety."
  exit 1
fi

require_production_ack_if_needed "$SAFE_MODE" "$DRY_RUN"

if bool_true "$ENABLE_SRC_MAINT"; then
  log "Enabling maintenance mode on source…"
  if [[ "$SOURCE_MODE" == "local" ]]; then
    sudo -u www-data "$PHP_CLI" "$SRC_DIR/admin/cli/maintenance.php" --enable
  else
    source_exec "sudo -u www-data $(quote_shell "$PHP_CLI") $(quote_shell "$SRC_DIR/admin/cli/maintenance.php") --enable"
  fi
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
Deploy target:    $DEPLOY_TARGET
Remote host:      ${REMOTE_HOST:-n/a}
Dest dir:         $DEST_DIR
Dest moodledata:  $DEST_DATA
Dest database:    $DEST_DB
Source DB host:    $SOURCE_DB_HOST
Source DB user:    $SOURCE_DB_USER
Target DB host:    $TARGET_DB_HOST
Target DB user:    $TARGET_DB_USER
Dry-run:          $DRY_RUN
Safe mode:        $SAFE_MODE
RDS allowlist:    ${RDS_HOST_ALLOWLIST:-<not set>}
Protected DBs:    ${PROTECTED_DATABASES:-<not set>}
Maintenance on source now: $SRC_MAINT_ENABLED
----------------
SUM

if bool_true "$DRY_RUN"; then
  log "Dry-run enabled. No changes were applied."
  REVERT_SRC_MAINT=1
  exit 0
fi

TMP_DIR="$(mktemp -d)"
DUMP_ORIG="$TMP_DIR/${SRC_DBNAME}.sql"
DUMP_SAN="$TMP_DIR/${SRC_DBNAME}.sanitized.sql"
PATCH_PHP="$TMP_DIR/config_patch.php"

log "Dumping database $SRC_DBNAME from $SOURCE_DB_HOST …"
if [[ "$SOURCE_MODE" == "local" ]]; then
  (
    export MYSQL_PWD="$SOURCE_DB_PASS"
    mysqldump -h "$SOURCE_DB_HOST" -u "$SOURCE_DB_USER" \
      --single-transaction --quick --set-gtid-purged=OFF \
      "$SRC_DBNAME" > "$DUMP_ORIG"
  )
else
  source_exec "export MYSQL_PWD=$(quote_shell "$SOURCE_DB_PASS"); mysqldump -h $(quote_shell "$SOURCE_DB_HOST") -u $(quote_shell "$SOURCE_DB_USER") --single-transaction --quick --set-gtid-purged=OFF $(quote_shell "$SRC_DBNAME")" > "$DUMP_ORIG"
fi

log "Sanitizing dump to remove privileged statements…"
sed '/SQL_LOG_BIN/d; /GTID_PURGED/d' "$DUMP_ORIG" > "$DUMP_SAN"

log "Ensuring destination database $DEST_DB exists on $TARGET_DB_HOST…"
log "Running target DB preflight safety checks on $TARGET_DB_HOST …"
(
  export MYSQL_PWD="$TARGET_DB_PASS"
  mysql -N -B -h "$TARGET_DB_HOST" -u "$TARGET_DB_USER" \
    -e "SELECT 'preflight_ok', CURRENT_USER(), @@hostname;" >/dev/null
)

(
  export MYSQL_PWD="$TARGET_DB_PASS"
  mysql -h "$TARGET_DB_HOST" -u "$TARGET_DB_USER" \
    -e "CREATE DATABASE IF NOT EXISTS ${DEST_DB} DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
)

log "Importing dump into $DEST_DB on $TARGET_DB_HOST …"
(
  export MYSQL_PWD="$TARGET_DB_PASS"
  mysql -h "$TARGET_DB_HOST" -u "$TARGET_DB_USER" "$DEST_DB" < "$DUMP_SAN"
)

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

if [[ "$DEPLOY_TARGET" == "local" ]]; then
  log "Copying code to $DEST_DIR …"
  if [[ "$SOURCE_MODE" == "local" ]]; then
    sudo rsync -a "$SRC_DIR/" "$DEST_DIR/"
  else
    sudo rsync -a -e "$SOURCE_RSYNC_SSH" --rsync-path="sudo rsync" "${SOURCE_SSH_TARGET}:${SRC_DIR}/" "$DEST_DIR/"
  fi
  sudo chown -R www-data:www-data "$DEST_DIR"

  log "Copying moodledata to $DEST_DATA … (may take time)"
  sudo mkdir -p "$DEST_DATA"
  if [[ "$SOURCE_MODE" == "local" ]]; then
    sudo rsync -a "$SRC_DATA/" "$DEST_DATA/"
  else
    sudo rsync -a -e "$SOURCE_RSYNC_SSH" --rsync-path="sudo rsync" "${SOURCE_SSH_TARGET}:${SRC_DATA}/" "$DEST_DATA/"
  fi
  sudo chown -R www-data:www-data "$DEST_DATA"
  sudo find "$DEST_DATA" -type d -exec chmod 770 {} \;
  sudo find "$DEST_DATA" -type f -exec chmod 660 {} \;

  log "Patching $DEST_DIR/config.php …"
  sudo env \
    WWWROOT="$NEW_URL" \
    DATAROOT="$DEST_DATA" \
    DIRROOT="$DEST_DIR" \
    DBHOST="$TARGET_DB_HOST" \
    DBNAME="$DEST_DB" \
    DBUSER="$TARGET_DB_USER" \
    DBPASS="$TARGET_DB_PASS" \
    "$PHP_CLI" "$PATCH_PHP" "$DEST_DIR/config.php"
else
  log "Testing SSH connectivity to $REMOTE_SSH_TARGET …"
  remote_exec "true"

  log "Preparing remote directories on $REMOTE_SSH_TARGET …"
  remote_sudo_bash "mkdir -p $(quote_shell "$DEST_DIR") $(quote_shell "$DEST_DATA")"

  STAGE_DIR="$TMP_DIR/stage_moodle"
  sudo mkdir -p "$STAGE_DIR"

  log "Staging Moodle code locally before remote sync …"
  if [[ "$SOURCE_MODE" == "local" ]]; then
    sudo rsync -a "$SRC_DIR/" "$STAGE_DIR/"
  else
    sudo rsync -a -e "$SOURCE_RSYNC_SSH" --rsync-path="sudo rsync" "${SOURCE_SSH_TARGET}:${SRC_DIR}/" "$STAGE_DIR/"
  fi

  log "Patching staged config.php for remote destination …"
  sudo env \
    WWWROOT="$NEW_URL" \
    DATAROOT="$DEST_DATA" \
    DIRROOT="$DEST_DIR" \
    DBHOST="$TARGET_DB_HOST" \
    DBNAME="$DEST_DB" \
    DBUSER="$TARGET_DB_USER" \
    DBPASS="$TARGET_DB_PASS" \
    "$PHP_CLI" "$PATCH_PHP" "$STAGE_DIR/config.php"

  log "Copying code to remote $REMOTE_SSH_TARGET:$DEST_DIR …"
  sudo rsync -a -e "$RSYNC_SSH" --rsync-path="sudo rsync" "$STAGE_DIR/" "${REMOTE_SSH_TARGET}:${DEST_DIR}/"

  log "Copying moodledata to remote $REMOTE_SSH_TARGET:$DEST_DATA … (may take time)"
  if [[ "$SOURCE_MODE" == "local" ]]; then
    sudo rsync -a -e "$RSYNC_SSH" --rsync-path="sudo rsync" "$SRC_DATA/" "${REMOTE_SSH_TARGET}:${DEST_DATA}/"
  else
    sudo rsync -a -e "$SOURCE_RSYNC_SSH" --rsync-path="sudo rsync" "${SOURCE_SSH_TARGET}:${SRC_DATA}/" "$TMP_DIR/source_data/"
    sudo rsync -a -e "$RSYNC_SSH" --rsync-path="sudo rsync" "$TMP_DIR/source_data/" "${REMOTE_SSH_TARGET}:${DEST_DATA}/"
  fi

  remote_sudo_bash "chown -R www-data:www-data $(quote_shell "$DEST_DIR") $(quote_shell "$DEST_DATA")"
  remote_sudo_bash "find $(quote_shell "$DEST_DATA") -type d -exec chmod 770 {} \\;"
  remote_sudo_bash "find $(quote_shell "$DEST_DATA") -type f -exec chmod 660 {} \\;"
fi

if bool_true "$ENABLE_NGINX"; then
  NEW_VHOST="/etc/nginx/sites-available/${NEW_DOMAIN}"
  VHOST_TMP="$TMP_DIR/${NEW_DOMAIN}.nginx"
  log "Creating Nginx vhost at $NEW_VHOST …"

  cat > "$VHOST_TMP" <<NGINX
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

  if [[ "$DEPLOY_TARGET" == "local" ]]; then
    sudo install -m 644 "$VHOST_TMP" "$NEW_VHOST"
    sudo ln -sfn "$NEW_VHOST" "/etc/nginx/sites-enabled/${NEW_DOMAIN}"
    sudo nginx -t
    sudo systemctl reload nginx
  else
    REMOTE_VHOST_TMP="/tmp/${NEW_DOMAIN}.nginx.$$"
    rsync -a -e "$RSYNC_SSH" "$VHOST_TMP" "${REMOTE_SSH_TARGET}:${REMOTE_VHOST_TMP}"
    remote_sudo_bash "mv $(quote_shell "$REMOTE_VHOST_TMP") $(quote_shell "$NEW_VHOST")"
    remote_sudo_bash "ln -sfn $(quote_shell "$NEW_VHOST") $(quote_shell "/etc/nginx/sites-enabled/${NEW_DOMAIN}")"
    remote_sudo_bash "nginx -t"
    remote_sudo_bash "systemctl reload nginx"
  fi
else
  warn "Skipping Nginx setup."
fi

if bool_true "$ENABLE_CERTBOT"; then
  CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
  if [[ "$DEPLOY_TARGET" == "local" ]]; then
    if [[ "$HAS_CERTBOT" == "1" ]]; then
      if [[ -n "$CERTBOT_EMAIL" ]]; then
        sudo certbot --nginx -d "$NEW_DOMAIN" --non-interactive --agree-tos --redirect -m "$CERTBOT_EMAIL" || warn "Certbot failed; run it later."
      else
        sudo certbot --nginx -d "$NEW_DOMAIN" --non-interactive --agree-tos --redirect --register-unsafely-without-email || warn "Certbot failed; run it later."
      fi
    else
      warn "Skipping Certbot (disabled or not installed)."
    fi
  else
    if remote_bash "command -v certbot >/dev/null 2>&1"; then
      if [[ -n "$CERTBOT_EMAIL" ]]; then
        remote_sudo_bash "certbot --nginx -d $(quote_shell "$NEW_DOMAIN") --non-interactive --agree-tos --redirect -m $(quote_shell "$CERTBOT_EMAIL")" || warn "Certbot failed on remote host; run it later."
      else
        remote_sudo_bash "certbot --nginx -d $(quote_shell "$NEW_DOMAIN") --non-interactive --agree-tos --redirect --register-unsafely-without-email" || warn "Certbot failed on remote host; run it later."
      fi
    else
      warn "Skipping Certbot on remote host (not installed)."
    fi
  fi
else
  warn "Skipping Certbot (disabled)."
fi

if bool_true "$ENABLE_REPLACE"; then
  if [[ -n "${SRC_WWWROOT:-}" && "$SRC_WWWROOT" != "$NEW_URL" ]]; then
    log "Replacing URLs in DB: $SRC_WWWROOT -> $NEW_URL …"

    if [[ "$DEPLOY_TARGET" == "local" ]]; then
      pushd "$DEST_DIR" >/dev/null
      set +e
      sudo -u www-data "$PHP_CLI" admin/tool/replace/cli/replace.php --non-interactive --search="$SRC_WWWROOT" --replace="$NEW_URL"
      REPLACE_RC=$?
      set -e
      popd >/dev/null
    else
      set +e
      remote_bash "cd $(quote_shell "$DEST_DIR") && sudo -u www-data $(quote_shell "$PHP_CLI") admin/tool/replace/cli/replace.php --non-interactive --search=$(quote_shell "$SRC_WWWROOT") --replace=$(quote_shell "$NEW_URL")"
      REPLACE_RC=$?
      set -e
    fi

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
  if [[ "$DEPLOY_TARGET" == "local" ]]; then
    pushd "$DEST_DIR" >/dev/null
    sudo -u www-data "$PHP_CLI" admin/cli/purge_caches.php || true
    popd >/dev/null
  else
    remote_bash "cd $(quote_shell "$DEST_DIR") && sudo -u www-data $(quote_shell "$PHP_CLI") admin/cli/purge_caches.php || true"
  fi
else
  warn "Skipping cache purge."
fi

if bool_true "$ENABLE_CRON"; then
  log "Ensuring cron entry for www-data …"
  CRON_LINE="*/1 * * * * ${PHP_CLI} ${DEST_DIR}/admin/cli/cron.php >/dev/null 2>&1"
  if [[ "$DEPLOY_TARGET" == "local" ]]; then
    if sudo crontab -u www-data -l 2>/dev/null | grep -Fq "${DEST_DIR}/admin/cli/cron.php"; then
      log "Cron entry already present for this instance."
    else
      ( sudo crontab -u www-data -l 2>/dev/null; echo "$CRON_LINE" ) | sudo crontab -u www-data -
      log "Cron entry added."
    fi
  else
    if remote_sudo_bash "crontab -u www-data -l 2>/dev/null | grep -Fq $(quote_shell "${DEST_DIR}/admin/cli/cron.php")"; then
      log "Cron entry already present for this instance on remote host."
    else
      remote_sudo_bash "( crontab -u www-data -l 2>/dev/null; printf '%s\n' $(quote_shell "$CRON_LINE") ) | crontab -u www-data -"
      log "Cron entry added on remote host."
    fi
  fi
else
  warn "Skipping cron setup."
fi

if bool_true "$DISABLE_NEW_MAINT"; then
  if [[ "$DEPLOY_TARGET" == "local" ]]; then
    sudo -u www-data "$PHP_CLI" "$DEST_DIR/admin/cli/maintenance.php" --disable || true
  else
    remote_bash "sudo -u www-data $(quote_shell "$PHP_CLI") $(quote_shell "$DEST_DIR/admin/cli/maintenance.php") --disable || true"
  fi
fi

if [[ "${SRC_MAINT_ENABLED:-0}" == "1" ]] && bool_true "$DISABLE_SRC_MAINT_AFTER"; then
  REVERT_SRC_MAINT=1
else
  REVERT_SRC_MAINT=0
fi

log "Clone completed. New site should be available at: $NEW_URL"
log "Moodle code: $DEST_DIR"
log "Moodledata:  $DEST_DATA"
log "Database:    $DEST_DB on $TARGET_DB_HOST (user $TARGET_DB_USER)"
