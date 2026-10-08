import logging
import os
import re
import signal
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

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

# safe="" — иначе "/" в логине или пароле остаётся как есть и ломает разбор URL.
AUTH_USER = urllib.parse.quote(os.getenv("WEBDAV_USER", "admin"), safe="")
AUTH_PASS = urllib.parse.quote(os.getenv("WEBDAV_PASSWORD", ""), safe="")
AUTH_PREFIX = f"{AUTH_USER}:{AUTH_PASS}@" if AUTH_PASS else ""

TORRSERVER_PUBLIC = f"http://{AUTH_PREFIX}{HOST_IP}:{TORR_PORT}"
# Версия адреса для логов: пароль заменён на "***".
TORRSERVER_PUBLIC_MASKED = (
    f"http://{AUTH_USER}:***@{HOST_IP}:{TORR_PORT}" if AUTH_PASS else TORRSERVER_PUBLIC
)
OUTPUT_DIR = "/app/strm_library"

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".m4v")

WAKEUP_DELAY = 10
MAX_RETRIES = 3
INTERVAL = 300
FETCH_WORKERS = 8

# Хэш в ссылке потока: v1 (SHA-1, 40 символов) или v2 (SHA-256, 64 символа).
STRM_HASH_RE = re.compile(r"link=([0-9a-fA-F]{64}|[0-9a-fA-F]{40})(?![0-9a-fA-F])")

logger = logging.getLogger(__name__)

# Сигнал остановки: прерывает паузы и опрос торрентов, чтобы docker stop не доходил до SIGKILL.
shutdown_event = threading.Event()


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


def _as_int_list(value) -> list:
    """Приводит значение guessit (число или список чисел) к списку int."""
    values = value if isinstance(value, list) else [value]
    return [int(item) for item in values]


def clean_title(filename: str) -> str:
    """Нормализует имя медиафайла через guessit и возвращает стабильный заголовок
    для именования .strm-файла. Корректно обрабатывает год, сезон/эпизод
    (включая мультиэпизоды и эпизоды без сезона), части фильма, теги качества,
    кириллицу и прочие edge-кейсы.
    """
    if not filename:
        return "unknown_title"

    try:
        info = guessit(filename)
        title: str = str(info.get("title", "")).strip()

        season = info.get("season")
        episode = info.get("episode")
        part = info.get("part", info.get("cd"))

        if episode is not None:
            episodes = _as_int_list(episode)
            episode_tag = f"E{episodes[0]:02d}"
            if len(episodes) > 1:
                episode_tag += f"-E{episodes[-1]:02d}"

            if season is not None:
                title = f"{title} S{_as_int_list(season)[0]:02d}{episode_tag}"
            else:
                title = f"{title} {episode_tag}"
        elif part is not None:
            title = f"{title} Part {_as_int_list(part)[0]}"

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


def _fetch_file_stats(session: requests.Session, internal_url: str, t_hash: str):
    """Запрашивает список файлов торрента. Возвращает непустой список или None,
    если метаданные ещё не готовы или запрос не удался.
    """
    if shutdown_event.is_set():
        return None

    try:
        t_resp = session.post(
            f"{internal_url}/torrents",
            json={"action": "get", "hash": t_hash},
            timeout=10,
        )
        t_resp.raise_for_status()

        files = t_resp.json().get("file_stats", [])
        if files:
            logger.info("%s...: получено %d файлов", t_hash[:8], len(files))
            return files

        logger.info("%s...: file_stats пуст, торрент ещё загружается", t_hash[:8])
    except Exception as exc:
        logger.error("%s...: ошибка запроса — %s", t_hash[:8], exc)

    return None


def _fetch_ready_files(
    session: requests.Session,
    internal_url: str,
    active_hashes: set,
) -> dict:
    """Ожидает метаданные файлов по активным торрентам с ретраями.
    Запросы к торрентам выполняются параллельно.
    Возвращает словарь {hash: [file_stats]} для торрентов, по которым метаданные стали доступны.
    """
    pending_hashes = list(active_hashes)
    ready_files = {}

    for attempt in range(MAX_RETRIES):
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            results = list(
                pool.map(
                    lambda t_hash: _fetch_file_stats(session, internal_url, t_hash),
                    pending_hashes,
                )
            )

        still_pending = []
        for t_hash, files in zip(pending_hashes, results):
            if files:
                ready_files[t_hash] = files
            else:
                still_pending.append(t_hash)

        pending_hashes = still_pending

        if not pending_hashes or shutdown_event.is_set():
            break

        if attempt < MAX_RETRIES - 1:
            logger.info(
                "Ожидают: %d торрентов. Пауза %d сек (попытка %d/%d завершена)...",
                len(pending_hashes),
                WAKEUP_DELAY,
                attempt + 1,
                MAX_RETRIES,
            )
            if shutdown_event.wait(WAKEUP_DELAY):
                break

    if pending_hashes and not shutdown_event.is_set():
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
) -> set:
    """Создаёт или атомарно обновляет .strm-файлы для найденных видеофайлов.
    Неизменённые файлы (с актуальным URL) не перезаписываются.
    Возвращает множество путей .strm, которые должны существовать для этих торрентов.
    """
    expected_paths = set()

    for t_hash, files in ready_files.items():
        entries = []
        for idx, file_info in enumerate(files):
            file_path = file_info.get("path", "")
            if not file_path.lower().endswith(VIDEO_EXTENSIONS):
                continue

            filename = os.path.basename(file_path)
            file_id = file_info.get("id", idx + 1)
            entries.append((filename, file_id, clean_title(filename)))

        # Файлы одного торрента с одинаковым заголовком получают id файла в имени,
        # иначе они перезаписывали бы друг друга.
        title_counts = {}
        for _, _, title in entries:
            title_counts[title] = title_counts.get(title, 0) + 1

        for filename, file_id, title in entries:
            encoded_filename = urllib.parse.quote(filename)
            stream_url = (
                f"{public_url}/stream/{encoded_filename}"
                f"?link={t_hash}&index={file_id}&play"
            )

            suffix = t_hash[:8]
            if title_counts[title] > 1:
                suffix = f"{suffix}-{file_id}"

            strm_filepath = os.path.join(output_dir, f"{title}.{suffix}.strm")
            expected_paths.add(strm_filepath)

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

    return expected_paths


def _cleanup_stale_strm(
    active_hashes_set: set,
    synced_hashes: set,
    expected_paths: set,
    output_dir: str,
) -> None:
    """Удаляет устаревшие .strm-файлы: для торрентов, которых больше нет в TorrServer,
    и для активных торрентов, если файл не входит в актуальный набор
    (сменилась схема именования или файл исчез из раздачи).
    Файлы торрентов, чьи метаданные в этот проход не получены, не трогаются.
    """
    active_lower = {h.lower() for h in active_hashes_set}
    synced_lower = {h.lower() for h in synced_hashes}

    for file in os.listdir(output_dir):
        if not file.endswith(".strm"):
            continue

        filepath = os.path.join(output_dir, file)
        try:
            with open(filepath, "r", encoding="utf-8") as f_obj:
                content = f_obj.read()

            match = STRM_HASH_RE.search(content)
            if not match:
                continue

            t_hash = match.group(1).lower()
            if t_hash not in active_lower:
                os.remove(filepath)
                logger.info("Удалён устаревший файл: %s", file)
            elif t_hash in synced_lower and filepath not in expected_paths:
                os.remove(filepath)
                logger.info("Удалён неактуальный файл активного торрента: %s", file)
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

    # Пустой список — не повод выходить: .strm удалённых торрентов тоже нужно убрать.
    ready_files = {}
    if active_hashes_set:
        ready_files = _fetch_ready_files(session, TORRSERVER_INTERNAL, active_hashes_set)

    if shutdown_event.is_set():
        return

    expected_paths = _sync_strm_files(ready_files, TORRSERVER_PUBLIC, OUTPUT_DIR)
    _cleanup_stale_strm(active_hashes_set, set(ready_files), expected_paths, OUTPUT_DIR)


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
        TORRSERVER_PUBLIC_MASKED,
        INTERVAL // 60,
    )

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
