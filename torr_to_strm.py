import logging
import os
import re
import signal
import threading
import time
import urllib.parse

import requests
from guessit import guessit
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

#------------------------------------------------------------------------------
# Конфигурация: параметры запуска, читаемые из переменных окружения.
#------------------------------------------------------------------------------
TORR_PORT = os.getenv("TORR_PORT", "8090")
TORR_INTERNAL_PORT = os.getenv("TORR_INTERNAL_PORT", "8090")
TORR_HOST = os.getenv("TORR_HOST", "torrserver")
TORRSERVER_INTERNAL = f"http://{TORR_HOST}:{TORR_INTERNAL_PORT}"

HOST_IP = os.getenv("HOST_IP", "127.0.0.1")

AUTH_USER = urllib.parse.quote(os.getenv("WEBDAV_USER", "admin"))
AUTH_PASS = urllib.parse.quote(os.getenv("WEBDAV_PASSWORD", ""))
AUTH_PREFIX = f"{AUTH_USER}:{AUTH_PASS}@" if AUTH_PASS else ""

TORRSERVER_PUBLIC = f"http://{AUTH_PREFIX}{HOST_IP}:{TORR_PORT}"
OUTPUT_DIR = "/app/strm_library"

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".m4v")

WAKEUP_DELAY = 10
MAX_RETRIES = 3
INTERVAL = 300

logger = logging.getLogger(__name__)


def create_http_session() -> requests.Session:
    """Создаёт и возвращает HTTP-сессию с настроенными ретраями для временных ошибок сети."""
    session = requests.Session()

    retry_config = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("HEAD", "GET", "POST"),
    )

    adapter = HTTPAdapter(max_retries=retry_config)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


def clean_title(filename: str) -> str:
    """Нормализует имя медиафайла через guessit и возвращает стабильный заголовок
    для именования .strm-файла. Корректно обрабатывает год, сезон/эпизод,
    теги качества, кириллицу и прочие edge-кейсы.
    """
    if not filename:
        return "unknown_title"

    try:
        info = guessit(filename)
        title: str = info.get("title", "").strip()

        season = info.get("season")
        episode = info.get("episode")
        if season is not None and episode is not None:
            title = f"{title} S{int(season):02d}E{int(episode):02d}"

        return title or "unknown_title"
    except Exception as exc:
        logger.warning("guessit не смог разобрать имя файла '%s': %s", filename, exc)
        return "unknown_title"


def get_torrents(session: requests.Session, internal_url: str):
    """Запрашивает список торрентов из TorrServer или возвращает None при ошибке."""
    try:
        response = session.post(
            f"{internal_url}/torrents",
            json={"action": "list"},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.error("Ошибка получения списка торрентов: %s", exc)
        return None


def _fetch_ready_files(
    session: requests.Session,
    internal_url: str,
    active_hashes: set,
) -> dict:
    """Ожидает метаданные файлов по активным торрентам с ретраями.
    Возвращает словарь {hash: [file_stats]} для торрентов, по которым метаданные стали доступны.
    """
    pending_hashes = list(active_hashes)
    ready_files = {}

    for attempt in range(MAX_RETRIES):
        still_pending = []

        for t_hash in pending_hashes:
            try:
                t_resp = session.post(
                    f"{internal_url}/torrents",
                    json={"action": "get", "hash": t_hash},
                    timeout=10,
                )
                t_resp.raise_for_status()

                files = t_resp.json().get("file_stats", [])

                if files:
                    ready_files[t_hash] = files
                    logger.info("%s...: получено %d файлов", t_hash[:8], len(files))
                else:
                    logger.info(
                        "%s...: file_stats пуст, торрент ещё загружается",
                        t_hash[:8],
                    )
                    still_pending.append(t_hash)
            except Exception as exc:
                logger.error("%s...: ошибка запроса — %s", t_hash[:8], exc)
                still_pending.append(t_hash)

        pending_hashes = still_pending

        if not pending_hashes:
            break

        if attempt < MAX_RETRIES - 1:
            logger.info(
                "Ожидают: %d торрентов. Пауза %d сек (попытка %d/%d завершена)...",
                len(pending_hashes),
                WAKEUP_DELAY,
                attempt + 1,
                MAX_RETRIES,
            )
            time.sleep(WAKEUP_DELAY)

    if pending_hashes:
        logger.warning(
            "Пропущено %d торрентов после %d попыток: метаданные недоступны. Хэши: %s",
            len(pending_hashes),
            MAX_RETRIES,
            [h[:8] for h in pending_hashes],
        )

    return ready_files


def _sync_strm_files(
    ready_files: dict,
    public_url: str,
    output_dir: str,
) -> None:
    """Создаёт или атомарно обновляет .strm-файлы для найденных видеофайлов.
    Неизменённые файлы (с актуальным URL) не перезаписываются.
    """
    for t_hash, files in ready_files.items():
        for idx, file_info in enumerate(files):
            file_path = file_info.get("path", "")
            if not file_path.lower().endswith(VIDEO_EXTENSIONS):
                continue

            filename = os.path.basename(file_path)
            encoded_filename = urllib.parse.quote(filename)

            file_id = file_info.get("id", idx + 1)
            stream_url = (
                f"{public_url}/stream/{encoded_filename}"
                f"?link={t_hash}&index={file_id}&play"
            )

            strm_filepath = os.path.join(
                output_dir,
                f"{clean_title(filename)}.{t_hash[:8]}.strm",
            )

            tmp_filepath = None
            try:
                if os.path.exists(strm_filepath):
                    with open(strm_filepath, "r", encoding="utf-8") as f_obj:
                        if f_obj.read() == stream_url:
                            continue
                    action_msg = "Обновлён"
                else:
                    action_msg = "Создан"

                tmp_filepath = f"{strm_filepath}.tmp"
                with open(tmp_filepath, "w", encoding="utf-8") as f_obj:
                    f_obj.write(stream_url)

                os.replace(tmp_filepath, strm_filepath)
                logger.info("%s: %s", action_msg, strm_filepath)

            except Exception as exc:
                try:
                    if tmp_filepath and os.path.exists(tmp_filepath):
                        os.remove(tmp_filepath)
                except Exception:
                    pass
                logger.error("Ошибка записи файла %s: %s", strm_filepath, exc)


def _cleanup_stale_strm(
    active_hashes_set: set,
    output_dir: str,
) -> None:
    """Удаляет устаревшие .strm-файлы, которых больше нет в списке активных торрентов TorrServer."""
    for file in os.listdir(output_dir):
        if not file.endswith(".strm"):
            continue

        filepath = os.path.join(output_dir, file)
        try:
            with open(filepath, "r", encoding="utf-8") as f_obj:
                content = f_obj.read()

            match = re.search(r"link=([a-fA-F0-9]{40})", content)
            if match and match.group(1) not in active_hashes_set:
                os.remove(filepath)
                logger.info("Удалён устаревший файл: %s", file)
        except Exception as exc:
            logger.error("Ошибка при чтении/удалении файла %s: %s", filepath, exc)


def main(session: requests.Session) -> None:
    """Оркестрирует одну итерацию синхронизации: получает торренты, обновляет
    .strm-файлы и удаляет устаревшие.
    """
    if not HOST_IP or HOST_IP == "127.0.0.1":
        logger.warning(
            "HOST_IP не задан или указан localhost — Infuse не сможет воспроизвести видео!",
        )

    try:
        int(TORR_PORT)
    except ValueError:
        logger.error(
            "TORR_PORT имеет некорректное значение: %s — ссылки в .strm будут битые",
            TORR_PORT,
        )
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    torrents = get_torrents(session, TORRSERVER_INTERNAL)
    if torrents is None:
        return

    active_hashes_set = set()
    for torrent in torrents:
        t_hash = torrent.get("hash")
        if t_hash:
            active_hashes_set.add(t_hash)
        else:
            t_title = torrent.get("title", "Неизвестное_название")
            logger.warning(
                "Пропущен торрент без хэша (ожидает инициализации или ошибка). Название: %s",
                t_title,
            )

    if not active_hashes_set:
        return

    ready_files = _fetch_ready_files(session, TORRSERVER_INTERNAL, active_hashes_set)
    _sync_strm_files(ready_files, TORRSERVER_PUBLIC, OUTPUT_DIR)
    _cleanup_stale_strm(active_hashes_set, OUTPUT_DIR)


def configure_logging(level: int = logging.INFO) -> None:
    """Настраивает корневой логгер для вывода в stdout внутри контейнера."""
    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        fmt="%(asctime)s [UTC] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(handler)


if __name__ == "__main__":
    configure_logging()

    logger.info(
        "Парсер запущен. Internal: %s | Public: %s | Интервал: %d мин.",
        TORRSERVER_INTERNAL,
        TORRSERVER_PUBLIC,
        INTERVAL // 60,
    )

    shutdown_event = threading.Event()

    def handle_sigterm(signum, frame) -> None:
        """Обрабатывает сигналы завершения и останавливает цикл опроса."""
        logger.info("Получен сигнал завершения. Остановка парсера...")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGINT, handle_sigterm)

    # Сессия создаётся один раз при старте и переиспользуется между итерациями.
    http_session = create_http_session()

    while not shutdown_event.is_set():
        try:
            main(http_session)
        except Exception as exc:
            logger.error("Критическая ошибка в главном цикле: %s", exc)

        shutdown_event.wait(timeout=INTERVAL)
