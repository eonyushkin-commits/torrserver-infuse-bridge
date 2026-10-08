#!/usr/bin/env bash
#
# Установка, обновление и перенастройка TorrServer → Infuse Bridge.
#
#   curl -fsSL https://raw.githubusercontent.com/eonyushkin-commits/torrserver-infuse-bridge/main/install.sh | sudo bash
#   sudo bash install.sh --help
#
# Репозиторий на сервер не клонируется: скачиваются только docker-compose.yml,
# docker-compose.tls.yml и Caddyfile, параметры хранятся в .env.

set -euo pipefail

#------------------------------------------------------------------------------
# Константы и параметры по умолчанию.
#------------------------------------------------------------------------------
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly CYAN='\033[0;36m'
readonly NC='\033[0m'

readonly REPO="eonyushkin-commits/torrserver-infuse-bridge"
readonly RAW_BASE="https://raw.githubusercontent.com/${REPO}"
readonly PROJECT_FILES=(docker-compose.yml docker-compose.tls.yml Caddyfile)
# Контейнеры проекта: текущей версии и версии 1. Занятые ими порты — «свои».
readonly OWN_CONTAINERS=(infuse-gateway infuse-bridge torrserver torr-proxy webdav-infuse strm-parser)
readonly LEGACY_CONTAINERS=(torr-proxy webdav-infuse strm-parser torrserver)

INSTALL_DIR="/opt/torrserver-infuse-bridge"
VERSION="main"
NON_INTERACTIVE=0
RECONFIGURE=0
ROTATE_TOKEN=0
LEGACY=0

# Параметры установки: из командной строки, затем из существующего .env, затем вопросы.
CLI_DOMAIN=""
CLI_HOST_IP=""
CLI_TS_PORT=""
CLI_WEBDAV_PORT=""
CLI_AUTH_USER=""
CLI_AUTH_PASSWORD="${BRIDGE_PASSWORD:-}"
CLI_NO_DOMAIN=0

DOMAIN=""
HOST_IP=""
TS_PORT=""
WEBDAV_PORT=""
AUTH_USER=""
AUTH_PASSWORD=""
STREAM_TOKEN=""
PUBLIC_URL=""

#------------------------------------------------------------------------------
# Логирование и завершение.
#------------------------------------------------------------------------------
log_info() { echo -e "${CYAN}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err() { echo -e "${RED}[ERROR]${NC} $1" >&2; }
die() { log_err "$1"; exit 1; }

# shellcheck disable=SC2317 # вызывается через trap
cleanup() {
  local exit_code=$?
  if [ "$exit_code" -ne 0 ]; then
    log_err "Установка прервана (код: $exit_code)."
  fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

usage() {
  cat <<EOF
Использование: install.sh [параметры]

Без параметров скрипт задаёт вопросы. Повторный запуск обновляет установку.

  --domain ДОМЕН         Работать по HTTPS на домене (сертификат Let's Encrypt).
  --no-domain            Работать по IP без шифрования (сбросить ранее указанный домен).
  --ip IPV4              Внешний IPv4-адрес сервера (без домена; по умолчанию определяется сам).
  --ts-port ПОРТ         Порт TorrServer и потоков (по умолчанию 443 с доменом, иначе случайный).
  --webdav-port ПОРТ     Порт WebDAV для Infuse (по умолчанию 8443 с доменом, иначе 8080).
  --user ЛОГИН           Логин для WebDAV и TorrServer (по умолчанию admin).
  --password-file ФАЙЛ   Прочитать пароль из файла (или передайте его в BRIDGE_PASSWORD).
  --version ВЕРСИЯ       Версия: main (по умолчанию) или тег релиза, например v2.0.0.
  --dir КАТАЛОГ          Каталог установки (по умолчанию $INSTALL_DIR).
  --reconfigure          Заново задать параметры, даже если .env уже есть.
  --rotate-token         Выпустить новый токен для ссылок на потоки.
  -y, --non-interactive  Не задавать вопросов: брать значения из параметров и .env.
  -h, --help             Показать эту справку.
EOF
}

#------------------------------------------------------------------------------
# Разбор аргументов.
#------------------------------------------------------------------------------
require_value() {
  [ -n "${2:-}" ] || die "Параметр $1 требует значение."
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --domain) require_value "$@"; CLI_DOMAIN=$2; shift 2 ;;
      --no-domain) CLI_NO_DOMAIN=1; shift ;;
      --ip) require_value "$@"; CLI_HOST_IP=$2; shift 2 ;;
      --ts-port) require_value "$@"; CLI_TS_PORT=$2; shift 2 ;;
      --webdav-port) require_value "$@"; CLI_WEBDAV_PORT=$2; shift 2 ;;
      --user) require_value "$@"; CLI_AUTH_USER=$2; shift 2 ;;
      --password-file)
        require_value "$@"
        [ -r "$2" ] || die "Не удаётся прочитать файл с паролем: $2"
        CLI_AUTH_PASSWORD=$(<"$2")
        shift 2
        ;;
      --version) require_value "$@"; VERSION=$2; shift 2 ;;
      --dir) require_value "$@"; INSTALL_DIR=$2; shift 2 ;;
      --reconfigure) RECONFIGURE=1; shift ;;
      --rotate-token) ROTATE_TOKEN=1; shift ;;
      -y | --non-interactive) NON_INTERACTIVE=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) usage >&2; die "Неизвестный параметр: $1" ;;
    esac
  done

  if [ -n "$CLI_DOMAIN" ] && [ "$CLI_NO_DOMAIN" -eq 1 ]; then
    die "--domain и --no-domain нельзя указывать вместе."
  fi

  if [[ ! "$VERSION" =~ ^(main|v[0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
    die "Некорректная версия: $VERSION. Ожидается main или тег вида v2.0.0."
  fi

  if [ -n "$CLI_DOMAIN$CLI_HOST_IP$CLI_TS_PORT$CLI_WEBDAV_PORT$CLI_AUTH_USER$CLI_AUTH_PASSWORD" ] \
    || [ "$CLI_NO_DOMAIN" -eq 1 ]; then
    RECONFIGURE=1
  fi
}

#------------------------------------------------------------------------------
# Вопросы пользователю. Читаем из /dev/tty, чтобы работал запуск через curl | bash.
#------------------------------------------------------------------------------
# ask ПЕРЕМЕННАЯ "Вопрос" "значение по умолчанию"
ask() {
  local name=$1 question=$2 default=${3:-} answer=""
  if [ "$NON_INTERACTIVE" -eq 1 ]; then
    answer=$default
  else
    [ -n "$default" ] && question+=" [$default]"
    read -rp "$question: " answer </dev/tty || true
    answer=${answer:-$default}
  fi
  printf -v "$name" '%s' "$answer"
}

# ask_secret ПЕРЕМЕННАЯ "Вопрос"
ask_secret() {
  local name=$1 question=$2 answer=""
  read -rsp "$question: " answer </dev/tty || true
  echo >&2
  printf -v "$name" '%s' "$answer"
}

# confirm "Вопрос" — да/нет, по умолчанию «нет».
confirm() {
  local answer=""
  [ "$NON_INTERACTIVE" -eq 1 ] && return 1
  read -rp "$1 [y/N]: " answer </dev/tty || true
  [[ "$answer" =~ ^[yYдД]([eE][sS]|[аА])?$ ]]
}

#------------------------------------------------------------------------------
# Проверки значений.
#------------------------------------------------------------------------------
is_port() {
  [[ "$1" =~ ^[0-9]{1,5}$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

is_ipv4() {
  [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  local octet
  for octet in "${BASH_REMATCH[@]:1}"; do
    [ $((10#$octet)) -le 255 ] || return 1
  done
}

is_domain() {
  [[ "$1" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]]
}

port_owned_by_us() {
  local name
  for name in "${OWN_CONTAINERS[@]}"; do
    docker port "$name" 2>/dev/null | grep -qE ":$1\$" && return 0
  done
  return 1
}

port_busy() {
  command -v ss >/dev/null 2>&1 || return 1
  ss -Htln "sport = :$1" 2>/dev/null | grep -q .
}

# env_get ФАЙЛ КЛЮЧ — значение из .env без выполнения файла (кавычки снимаются).
env_get() {
  local line
  line=$(grep -E "^$2=" "$1" 2>/dev/null | tail -n 1) || true
  line=${line#*=}
  if [[ ${#line} -ge 2 && ( "$line" == \'*\' || "$line" == \"*\" ) ]]; then
    line=${line:1:${#line}-2}
  fi
  printf '%s' "$line"
}

# env_set ФАЙЛ КЛЮЧ ЗНАЧЕНИЕ — только для значений без спецсимволов (версия, токен).
env_set() {
  if grep -qE "^$2=" "$1"; then
    sed -i "s|^$2=.*|$2=$3|" "$1"
  else
    echo "$2=$3" >>"$1"
  fi
}

generate_token() {
  od -An -tx1 -N24 /dev/urandom | tr -d ' \n'
}

image_tag() {
  if [ "$VERSION" = "main" ]; then
    echo "latest"
  else
    echo "${VERSION#v}"
  fi
}

#------------------------------------------------------------------------------
# Этапы установки.
#------------------------------------------------------------------------------
check_requirements() {
  log_info "Проверка системных требований..."

  [ "$EUID" -eq 0 ] || die "Скрипт нужно запускать от root (sudo)."

  command -v docker >/dev/null 2>&1 || die "Docker не установлен: https://docs.docker.com/engine/install/"
  docker compose version >/dev/null 2>&1 \
    || die "Не найден Docker Compose v2 (команда 'docker compose'). Установите плагин docker-compose-plugin."
  command -v curl >/dev/null 2>&1 || die "Утилита curl не установлена."

  if [ "$NON_INTERACTIVE" -eq 0 ] && ! { : </dev/tty; } 2>/dev/null; then
    die "Нет терминала для вопросов. Запустите с -y и параметрами (см. --help)."
  fi
}

download_files() {
  local file
  if [ -n "${BRIDGE_SOURCE_DIR:-}" ]; then
    log_info "Копирование файлов проекта из $BRIDGE_SOURCE_DIR..."
    for file in "${PROJECT_FILES[@]}"; do
      cp "$BRIDGE_SOURCE_DIR/$file" "$file"
    done
    return 0
  fi

  log_info "Загрузка файлов проекта (версия $VERSION)..."
  for file in "${PROJECT_FILES[@]}"; do
    curl -fsSL --retry 3 "$RAW_BASE/$VERSION/$file" -o "$file.tmp" \
      || die "Не удалось скачать $file для версии $VERSION."
    mv "$file.tmp" "$file"
  done
}

detect_legacy() {
  # Установка версии 1: .env без STREAM_TOKEN (nginx-прокси, WebDAV, парсер .strm).
  if [ -f .env ] && ! grep -qE '^STREAM_TOKEN=' .env; then
    LEGACY=1
    RECONFIGURE=1
    log_warn "Найдена установка предыдущей версии — параметры будут перенесены."
  fi
}

load_existing() {
  [ -f .env ] || return 0

  if [ "$LEGACY" -eq 1 ]; then
    HOST_IP=$(env_get .env HOST_IP)
    TS_PORT=$(env_get .env TORR_PORT)
    WEBDAV_PORT=$(env_get .env WEBDAV_PORT)
    AUTH_USER=$(env_get .env WEBDAV_USER)
    AUTH_PASSWORD=$(env_get .env WEBDAV_PASSWORD)
    return 0
  fi

  DOMAIN=$(env_get .env DOMAIN)
  HOST_IP=$(env_get .env HOST_IP)
  TS_PORT=$(env_get .env TS_PORT)
  WEBDAV_PORT=$(env_get .env WEBDAV_PORT)
  AUTH_USER=$(env_get .env AUTH_USER)
  AUTH_PASSWORD=$(env_get .env AUTH_PASSWORD)
  STREAM_TOKEN=$(env_get .env STREAM_TOKEN)
  PUBLIC_URL=$(env_get .env PUBLIC_URL)
}

configure_address() {
  local previous_domain=$DOMAIN

  if [ -n "$CLI_DOMAIN" ]; then
    DOMAIN=$CLI_DOMAIN
  elif [ "$CLI_NO_DOMAIN" -eq 1 ]; then
    DOMAIN=""
  elif [ "$NON_INTERACTIVE" -eq 0 ]; then
    echo
    log_info "С доменом сервис работает по HTTPS (сертификат Let's Encrypt), и пароль с потоками"
    log_info "шифруются. Без домена — обычный HTTP по IP-адресу."
    ask DOMAIN "Домен (пусто — без домена${DOMAIN:+, «-» — отказаться от текущего})" "$DOMAIN"
    [ "$DOMAIN" = "-" ] && DOMAIN=""
  fi

  if [ -n "$DOMAIN" ]; then
    is_domain "$DOMAIN" || die "Некорректный домен: $DOMAIN"
    HOST_IP=""
  else
    local default_ip=${CLI_HOST_IP:-$HOST_IP}
    if [ -z "$default_ip" ]; then
      log_info "Определение внешнего IPv4-адреса..."
      default_ip=$(curl -4 -fsS --max-time 5 https://ifconfig.me 2>/dev/null || true)
      is_ipv4 "$default_ip" || default_ip=""
    fi
    if [ -n "$CLI_HOST_IP" ]; then
      HOST_IP=$CLI_HOST_IP
    else
      ask HOST_IP "Внешний IPv4-адрес сервера" "$default_ip"
    fi
    is_ipv4 "$HOST_IP" || die "Некорректный IPv4-адрес: '${HOST_IP}'. Укажите его через --ip."
  fi

  # При смене режима старые порты по умолчанию не подходят (443/8443 против 8080).
  if [ "$previous_domain" != "$DOMAIN" ] && { [ -z "$previous_domain" ] || [ -z "$DOMAIN" ]; }; then
    TS_PORT=""
    WEBDAV_PORT=""
  fi
}

configure_ports() {
  local default_ts default_dav
  if [ -n "$DOMAIN" ]; then
    default_ts=${TS_PORT:-443}
    default_dav=${WEBDAV_PORT:-8443}
  else
    default_ts=${TS_PORT:-$(((RANDOM * 32768 + RANDOM) % 50000 + 10000))}
    default_dav=${WEBDAV_PORT:-8080}
  fi

  if [ -n "$CLI_TS_PORT" ]; then TS_PORT=$CLI_TS_PORT; else ask TS_PORT "Порт TorrServer и потоков" "$default_ts"; fi
  if [ -n "$CLI_WEBDAV_PORT" ]; then WEBDAV_PORT=$CLI_WEBDAV_PORT; else ask WEBDAV_PORT "Порт WebDAV для Infuse" "$default_dav"; fi

  is_port "$TS_PORT" || die "Некорректный порт TorrServer: $TS_PORT"
  is_port "$WEBDAV_PORT" || die "Некорректный порт WebDAV: $WEBDAV_PORT"
  [ "$TS_PORT" != "$WEBDAV_PORT" ] || die "Порты TorrServer и WebDAV должны различаться."
  if [ -n "$DOMAIN" ] && { [ "$TS_PORT" = 80 ] || [ "$WEBDAV_PORT" = 80 ]; }; then
    die "Порт 80 занят выпуском сертификата — выберите другой."
  fi

  # Порты, занятые контейнерами этого же проекта (в том числе версии 1), освободятся
  # при перезапуске — проверяем только чужие.
  local port
  local ports=("$TS_PORT" "$WEBDAV_PORT")
  [ -n "$DOMAIN" ] && ports+=(80)
  for port in "${ports[@]}"; do
    if port_busy "$port" && ! port_owned_by_us "$port"; then
      die "Порт $port уже занят другим процессом. Освободите его или выберите другой."
    fi
  done
}

configure_credentials() {
  local default_user=${AUTH_USER:-admin}
  if [ -n "$CLI_AUTH_USER" ]; then AUTH_USER=$CLI_AUTH_USER; else ask AUTH_USER "Логин для WebDAV и TorrServer" "$default_user"; fi
  [[ "$AUTH_USER" =~ ^[A-Za-z0-9._-]+$ ]] \
    || die "Логин может содержать только латиницу, цифры, точку, '_' и '-'."

  local password="" repeat=""
  if [ -n "$CLI_AUTH_PASSWORD" ]; then
    password=$CLI_AUTH_PASSWORD
  elif [ "$NON_INTERACTIVE" -eq 1 ]; then
    [ -n "$AUTH_PASSWORD" ] || die "Пароль не задан: передайте BRIDGE_PASSWORD или --password-file."
  else
    if [ -n "$AUTH_PASSWORD" ]; then
      ask_secret password "Пароль (Enter — оставить текущий)"
    else
      ask_secret password "Пароль"
    fi
    if [ -n "$password" ]; then
      ask_secret repeat "Повторите пароль"
      [ "$password" = "$repeat" ] || die "Пароли не совпадают."
    fi
  fi

  if [ -n "$password" ]; then
    [ "${#password}" -ge 8 ] || die "Пароль должен быть не короче 8 символов."
    AUTH_PASSWORD=$password
  fi

  [ -n "$AUTH_PASSWORD" ] || die "Пароль не может быть пустым."
  # Значения хранятся в .env в одинарных кавычках — сама кавычка внутри недопустима.
  [[ "$AUTH_PASSWORD" != *"'"* ]] || die "Пароль не должен содержать одинарную кавычку (')."
  [[ "$AUTH_PASSWORD" != *$'\n'* ]] || die "Пароль не должен содержать перевод строки."
}

write_env() {
  local ts_site dav_site compose_file
  if [ -n "$DOMAIN" ]; then
    ts_site="https://$DOMAIN:$TS_PORT"
    dav_site="https://$DOMAIN:$WEBDAV_PORT"
    PUBLIC_URL="https://$DOMAIN"
    [ "$TS_PORT" = 443 ] || PUBLIC_URL+=":$TS_PORT"
    compose_file="docker-compose.yml:docker-compose.tls.yml"
  else
    ts_site="http://:$TS_PORT"
    dav_site="http://:$WEBDAV_PORT"
    PUBLIC_URL="http://$HOST_IP:$TS_PORT"
    compose_file="docker-compose.yml"
  fi

  if [ -z "$STREAM_TOKEN" ] || [ "$ROTATE_TOKEN" -eq 1 ]; then
    STREAM_TOKEN=$(generate_token)
  fi

  local tmp
  tmp=$(umask 077 && mktemp .env.XXXXXX)
  cat >"$tmp" <<EOF
# Создано install.sh $(date -u +%Y-%m-%d). Изменения применяются командой: docker compose up -d
COMPOSE_FILE=$compose_file
BRIDGE_VERSION=$(image_tag)

# Адрес сервера: домен (HTTPS) или IPv4 (HTTP)
DOMAIN=$DOMAIN
HOST_IP=$HOST_IP
TS_PORT=$TS_PORT
WEBDAV_PORT=$WEBDAV_PORT

# Вычисляются из значений выше
PUBLIC_URL=$PUBLIC_URL
TS_SITE=$ts_site
DAV_SITE=$dav_site

# Доступ к WebDAV и TorrServer (одинарные кавычки обязательны)
AUTH_USER='$AUTH_USER'
AUTH_PASSWORD='$AUTH_PASSWORD'

# Секрет в ссылках на потоки. Новый токен: install.sh --rotate-token
STREAM_TOKEN=$STREAM_TOKEN
EOF
  chmod 600 "$tmp"
  mv "$tmp" .env
}

configure() {
  detect_legacy
  load_existing

  if [ -f .env ] && [ "$RECONFIGURE" -eq 0 ]; then
    if ! confirm "Конфигурация уже есть. Изменить параметры?"; then
      log_info "Параметры сохранены без изменений."
      env_set .env BRIDGE_VERSION "$(image_tag)"
      if [ "$ROTATE_TOKEN" -eq 1 ]; then
        STREAM_TOKEN=$(generate_token)
        env_set .env STREAM_TOKEN "$STREAM_TOKEN"
        log_info "Выпущен новый токен для потоков."
      fi
      return 0
    fi
  fi

  configure_address
  configure_ports
  configure_credentials
  write_env
  log_success "Параметры сохранены в $INSTALL_DIR/.env"
}

cleanup_legacy() {
  [ "$LEGACY" -eq 1 ] || return 0

  # Контейнеры версии 1 могли быть созданы Compose v1 под другим именем проекта —
  # тогда --remove-orphans их не увидит, а имя torrserver вызовет конфликт.
  # Данные TorrServer лежат в ./ts и не затрагиваются.
  log_info "Остановка контейнеров предыдущей версии..."
  docker rm -f "${LEGACY_CONTAINERS[@]}" >/dev/null 2>&1 || true

  # Сгенерированные файлы версии 1 больше не используются.
  rm -f nginx.conf .htpasswd
  if [ -d strm_library ]; then
    log_warn "Каталог strm_library больше не нужен (медиатека теперь виртуальная) — его можно удалить."
  fi
  if [ -d .git ]; then
    log_warn "Клон репозитория в $INSTALL_DIR больше не нужен: оставьте .env, docker-compose*.yml, Caddyfile, ts/ и caddy/."
  fi
  log_warn "Медиатека теперь разложена по папкам Movies/ и TV/ — в Infuse обновите (пересканируйте) источник."
}

start_services() {
  log_info "Загрузка образов..."
  docker compose pull || log_warn "Не удалось скачать все образы, используются локальные."

  log_info "Запуск контейнеров..."
  docker compose up -d --remove-orphans
}

print_summary() {
  local scheme="http" address
  [ -n "$DOMAIN" ] && scheme="https"
  address=${DOMAIN:-$HOST_IP}

  echo
  log_success "Готово!"
  echo "-------------------------------------------------------"
  log_info "TorrServer UI: $PUBLIC_URL"
  log_info "Infuse → WebDAV: адрес ${address}, порт ${WEBDAV_PORT}, HTTPS: $([ "$scheme" = https ] && echo да || echo нет)"
  log_info "Логин: $AUTH_USER"
  echo "-------------------------------------------------------"
  if [ -n "$DOMAIN" ]; then
    log_info "Домен $DOMAIN должен указывать на этот сервер (A-запись), а порты 80, $TS_PORT и"
    log_info "$WEBDAV_PORT — быть открыты: при первом запуске Caddy выпустит сертификат."
  else
    log_warn "Без домена трафик (включая пароль) идёт открытым текстом. Ограничьте доступ файрволом или VPN."
  fi
  log_info "Логи: cd $INSTALL_DIR && docker compose logs -f"
}

#------------------------------------------------------------------------------
# Точка входа.
#------------------------------------------------------------------------------
main() {
  parse_args "$@"
  echo -e "${BLUE}=== TorrServer → Infuse Bridge ===${NC}"

  check_requirements
  mkdir -p "$INSTALL_DIR"
  cd "$INSTALL_DIR"

  download_files
  configure
  cleanup_legacy
  start_services
  print_summary
}

# Вызов и exit в одной группе: bash разбирает её целиком до выполнения, поэтому
# скрипт корректно отработает, даже если его файл изменится во время работы.
{ main "$@"; exit; }
