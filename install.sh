#!/usr/bin/env bash

set -euo pipefail

#------------------------------------------------------------------------------
# Константы: цвета терминала и параметры установки.
#------------------------------------------------------------------------------
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[1;33m'
readonly BLUE='\033[0;34m'
readonly CYAN='\033[0;36m'
readonly NC='\033[0m'

readonly INSTALL_DIR="/opt/torrserver-infuse-bridge"
readonly REPO_URL="https://github.com/eonyushkin-commits/torrserver-infuse-bridge.git"

# Захватываем абсолютный путь к скрипту до любых cd,
# чтобы realpath работал относительно исходной директории, а не INSTALL_DIR.
readonly SCRIPT_PATH="$(realpath "$0")"

#------------------------------------------------------------------------------
# Логирование: единый вывод сообщений по уровням важности.
#------------------------------------------------------------------------------
log_info() {
  echo -e "${CYAN}[INFO]${NC} $1"
}

log_success() {
  echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
  echo -e "${YELLOW}[WARN]${NC} $1"
}

log_err() {
  echo -e "${RED}[ERROR]${NC} $1" >&2
}

#------------------------------------------------------------------------------
# Жизненный цикл: очистка и обработка сигналов завершения.
#------------------------------------------------------------------------------
cleanup() {
  local exit_code=$?

  if [ "$exit_code" -ne 0 ]; then
    log_err "Установка прервана (код: $exit_code)."
  fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

#------------------------------------------------------------------------------
# Предварительные проверки: root-доступ и обязательные зависимости.
#------------------------------------------------------------------------------
check_requirements() {
  log_info "Проверка системных требований..."

  if [ "$EUID" -ne 0 ]; then
    log_err "Этот скрипт должен быть запущен с правами root (sudo)."
    exit 1
  fi

  local deps=("git" "docker" "curl")
  local dep

  for dep in "${deps[@]}"; do
    if ! command -v "$dep" >/dev/null 2>&1; then
      log_err "Утилита '$dep' не установлена. Пожалуйста, установите её."
      exit 1
    fi
  done

  if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
    log_err "Docker Compose не найден (ни v1, ни v2)."
    exit 1
  fi
}

#------------------------------------------------------------------------------
# Репозиторий: клонирование проекта или жёсткое обновление существующей копии.
#------------------------------------------------------------------------------
fetch_repository() {
  if [[ "$REPO_URL" == *"ВАШ_ЛОГИН"* ]]; then
    log_err "Замените REPO_URL в скрипте на адрес своего репозитория."
    exit 1
  fi

  if [ -d "$INSTALL_DIR/.git" ]; then
    log_info "Проект уже существует в $INSTALL_DIR. Выполняю обновление..."
    log_warn "Все локальные изменения кода в $INSTALL_DIR будут ПЕРЕЗАПИСАНЫ (кроме .env)."

    read -rp "Продолжить? [y/N]: " confirm
    [[ "$confirm" =~ ^[yY]([eE][sS])?$ ]] || {
      log_info "Обновление отменено."
      return 0
    }

    log_info "Остановка текущих сервисов перед обновлением..."

    (
      cd "$INSTALL_DIR"

      docker compose down 2>/dev/null || docker-compose down 2>/dev/null || true

      LOCAL_BRANCH=$(git rev-parse --abbrev-ref HEAD)
      git fetch --all
      git reset --hard "origin/$LOCAL_BRANCH"
    )
  else
    log_info "Клонирование репозитория в $INSTALL_DIR..."
    git clone -q "$REPO_URL" "$INSTALL_DIR"
  fi
}

#------------------------------------------------------------------------------
# Конфигурация: сбор параметров и генерация локальных конфигурационных файлов.
#------------------------------------------------------------------------------
configure_env() {
  local env_file="$INSTALL_DIR/.env"

  if [ -f "$env_file" ]; then
    log_warn "Файл .env уже существует. Пересоздать его? [y/N]"
    read -rp "Ваш выбор: " response

    if [[ ! "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
      log_info "Сохраняем текущую конфигурацию."

      grep -vE '^\s*(#|$)' "$env_file" | grep -qvE '^[A-Z_][A-Z0-9_]*=' && {
        log_err "Файл .env содержит строки неверного формата. Исправьте его или удалите для пересоздания."
        exit 1
      } || true

      set -a
      # shellcheck source=/dev/null
      source "$env_file"
      set +a

      log_info "Обновление ключей доступа для TorrServer..."
      docker run --rm httpd:alpine htpasswd -bn "$WEBDAV_USER" "$WEBDAV_PASSWORD" > "$INSTALL_DIR/.htpasswd"
      chmod 644 "$INSTALL_DIR/.htpasswd"

      cat > "$INSTALL_DIR/nginx.conf" <<'EOF'
server {
    listen 80;

    location / {
        auth_basic "Restricted Area";
        auth_basic_user_file /etc/nginx/.htpasswd;
        proxy_pass http://torrserver:8090;
        proxy_set_header Host $host;
    }
}
EOF
      return 0
    fi
  fi

  log_info "Настройка конфигурации..."

  read -rp "Укажите порт для WebDAV (по умолчанию 8080): " WEBDAV_PORT
  WEBDAV_PORT=${WEBDAV_PORT:-8080}

  if ! [[ "$WEBDAV_PORT" =~ ^[0-9]+$ ]] || [ "$WEBDAV_PORT" -lt 1 ] || [ "$WEBDAV_PORT" -gt 65535 ]; then
    log_err "Некорректный порт: $WEBDAV_PORT. Порт должен быть числом от 1 до 65535."
    exit 1
  fi

  if ss -tlun | grep -q ":${WEBDAV_PORT} "; then
    log_err "Порт $WEBDAV_PORT уже занят. Запустите установку заново или укажите другой порт."
    exit 1
  fi

  read -rp "Укажите логин для WebDAV и TorrServer (по умолчанию admin): " WEBDAV_USER
  WEBDAV_USER=${WEBDAV_USER:-admin}

  read -rsp "Укажите пароль для WebDAV и TorrServer: " WEBDAV_PASSWORD
  echo

  if [ -z "$WEBDAV_PASSWORD" ]; then
    log_err "Пароль не может быть пустым."
    exit 1
  fi

  log_info "Определение внешнего IP-адреса сервера..."
  AUTO_IP=$(curl -s --connect-timeout 5 ifconfig.me 2>/dev/null || echo "")

  if ! echo "$AUTO_IP" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    AUTO_IP="127.0.0.1"
    log_warn "Не удалось определить внешний IP автоматически. Используется $AUTO_IP."
  fi

  read -rp "Укажите внешний IP-адрес сервера (по умолчанию $AUTO_IP): " HOST_IP
  HOST_IP=${HOST_IP:-$AUTO_IP}

  if ! echo "$HOST_IP" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    log_err "Некорректный IP-адрес: $HOST_IP. Ожидается формат X.X.X.X."
    exit 1
  fi

  AUTO_TORR_PORT=$(shuf -i 10000-60000 -n 1)
  read -rp "Укажите публичный порт для TorrServer (по умолчанию $AUTO_TORR_PORT): " TORR_PORT
  TORR_PORT=${TORR_PORT:-$AUTO_TORR_PORT}

  if ! [[ "$TORR_PORT" =~ ^[0-9]+$ ]] || [ "$TORR_PORT" -lt 1 ] || [ "$TORR_PORT" -gt 65535 ]; then
    log_err "Некорректный порт: $TORR_PORT. Порт должен быть числом от 1 до 65535.\""
    exit 1
  fi

  if ss -tlun | grep -q ":${TORR_PORT} "; then
    log_err "Порт $TORR_PORT уже занят. Запустите установку заново или укажите другой порт."
    exit 1
  fi

  touch "$env_file"
  chmod 600 "$env_file"

  cat > "$env_file" <<EOF
WEBDAV_PORT=$WEBDAV_PORT
WEBDAV_USER=$WEBDAV_USER
WEBDAV_PASSWORD=$WEBDAV_PASSWORD
HOST_IP=$HOST_IP
TORR_PORT=$TORR_PORT
EOF

  log_info "Генерация ключей доступа для TorrServer proxy..."
  docker run --rm httpd:alpine htpasswd -bn "$WEBDAV_USER" "$WEBDAV_PASSWORD" > "$INSTALL_DIR/.htpasswd"
  chmod 644 "$INSTALL_DIR/.htpasswd"

  cat > "$INSTALL_DIR/nginx.conf" <<'EOF'
server {
    listen 80;

    location / {
        auth_basic "Restricted Area";
        auth_basic_user_file /etc/nginx/.htpasswd;
        proxy_pass http://torrserver:8090;
        proxy_set_header Host $host;
    }
}
EOF
}

#------------------------------------------------------------------------------
# Запуск сервисов: старт контейнеров и вывод параметров доступа.
#------------------------------------------------------------------------------
start_services() {
  log_info "Запуск Docker-контейнеров..."

  cd "$INSTALL_DIR"

  if docker compose version >/dev/null 2>&1; then
    docker compose up -d
  else
    docker-compose up -d
  fi

  echo
  log_success "Установка успешно завершена!"
  echo "-------------------------------------------------------"
  log_info "TorrServer UI: http://${HOST_IP}:${TORR_PORT}"
  log_info "WebDAV URL: http://${HOST_IP}:${WEBDAV_PORT}"
  log_info "Логин для входа (WebDAV и TorrServer): ${WEBDAV_USER}"
  echo "-------------------------------------------------------"
  log_info "Логи парсера: docker logs -f strm-parser"
}

#------------------------------------------------------------------------------
# Самоудаление: удаляет скрипт из рабочей директории после установки.
# Не удаляет, если запущен из INSTALL_DIR (штатный путь для обновлений).
#------------------------------------------------------------------------------
self_remove() {
  if [[ "$SCRIPT_PATH" != "$INSTALL_DIR/install.sh" ]]; then
    rm -f "$SCRIPT_PATH"
    log_info "Файл установщика удалён: $SCRIPT_PATH"
  fi
}

#------------------------------------------------------------------------------
# Точка входа: последовательный запуск этапов установки.
#------------------------------------------------------------------------------
main() {
  echo -e "${BLUE}=== Установка TorrServer to Infuse Media Bridge ===${NC}"

  check_requirements
  fetch_repository
  configure_env
  start_services
  self_remove
}

main
