import logging
import os
import re
import signal
import threading
import time
import urllib.parse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

#------------------------------------------------------------------------------
# Конфигурация: параметры запуска, читаемые из переменных окружения.
#------------------------------------------------------------------------------
TORR_PORT = os.getenv("TORR_PORT", "8090")
TORR_INTERNAL_PORT = os.getenv("TORR_INTERNAL_PORT", "8090")
TORRSERVER_INTERNAL = f"http://torrserver:{TORR_INTERNAL_PORT}"

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

#------------------------------------------------------------------------------
# HTTP-клиент: общая сессия с ретраями для временных ошибок сети.
#------------------------------------------------------------------------------
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

logger = logging.getLogger(__name__)


def clean_title(filename: str) -> str:
    """Нормализует имя медиафайла и возвращает стабильный заголовок."""
    name = os.path.splitext(filename)[0]
    name = name.replace(".", " ").replace("_", " ")

    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", name)
    season_match = re.search(r"\bS\d{2}E\d{2}\b", name, re.IGNORECASE)

    if season_match:
        clean_name = name[:season_match.end()].strip()
    elif year_match:
        clean_name = name[:year_match.end()].strip()
    else:
        trash_words = [
            r"1080p",
            r"720p",
            r"2160p",
            r"4K",
            r"WEB-DL",
            r"BDRip",
            r"HDR",
            r"DUB",
            r"HEVC",
            r"H\.264",
        ]
        pattern = re.compile(r"\b(" + "|".join(trash_words) + r")\b", re.IGNORECASE)
        match = pattern.search(name)
        clean_name = name[:match.start()].strip() if match else name.strip()

    clean_name = re.sub(r"\s+", " ", clean_name).strip()
    return clean_name or "unknown_title"


def get_torrents():
    """Запрашивает список торрентов из TorrServer или возвращает None при ошибке."""
    try:
        response = session.post(
            f"{TORRSERVER_INTERNAL}/torrents",
            json={"action": "list"},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.error("Ошибка получения списка торрентов: %s", exc)
        return None


def main() -> None:
    """Синхронизирует .strm-файлы с активными торрентами TorrServer."""
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

    torrents = get_torrents()
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

    pending_hashes = list(active_hashes_set)
    ready_files = {}

    # Ожидаем метаданные файлов, пока торрент не станет готов или не закончатся попытки.
    for attempt in range(MAX_RETRIES):
        still_pending = []

        for t_hash in pending_hashes:
            try:
                t_resp = session.post(
                    f"{TORRSERVER_INTERNAL}/torrents",
                    json={"action": "get", "hash": t_hash},
                    timeout=10,
                )
                t_resp.raise_for_status()

                t_data = t_resp.json()
                files = t_data.get("file_stats", [])

                if files:
                    ready_files[t_hash] = files
                    logger.info(
                        "%s...: получено %d файлов",
                        t_hash[:8],
                        len(files),
                    )
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

    # Создаём или обновляем .strm-файлы для найденных видеофайлов.
    for t_hash, files in ready_files.items():
        for idx, file_info in enumerate(files):
            file_path = file_info.get("path", "")
            if not file_path.lower().endswith(VIDEO_EXTENSIONS):
                continue

            filename = os.path.basename(file_path)
            encoded_filename = urllib.parse.quote(filename)

            file_id = file_info.get("id", idx + 1)
            stream_url = (
                f"{TORRSERVER_PUBLIC}/stream/{encoded_filename}"
                f"?link={t_hash}&index={file_id}&play"
            )

            strm_filepath = os.path.join(
                OUTPUT_DIR,
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

    # Удаляем устаревшие .strm-файлы, которые больше не связаны с активными торрентами.
    for file in os.listdir(OUTPUT_DIR):
        if not file.endswith(".strm"):
            continue

        filepath = os.path.join(OUTPUT_DIR, file)
        try:
            with open(filepath, "r", encoding="utf-8") as f_obj:
                content = f_obj.read()

            match = re.search(r"link=([a-fA-F0-9]{40})", content)
            if match and match.group(1) not in active_hashes_set:
                os.remove(filepath)
                logger.info("Удалён устаревший файл: %s", file)
        except Exception as exc:
            logger.error("Ошибка при чтении/удалении файла %s: %s", filepath, exc)


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

    while not shutdown_event.is_set():
        try:
            main()
        except Exception as exc:
            logger.error("Критическая ошибка в главном цикле: %s", exc)

        shutdown_event.wait(timeout=INTERVAL)
