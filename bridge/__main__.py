"""Точка входа: python -m bridge."""

import logging
import signal
import sys
import threading
import time

from .config import ConfigError, load_settings
from .library import Library, TorrServerClient
from .webdav import create_server

logger = logging.getLogger("bridge")


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [UTC] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.Formatter.converter = time.gmtime


def main() -> int:
    configure_logging()

    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error("Ошибка конфигурации: %s", exc)
        return 2

    client = TorrServerClient(settings.torrserver_url, pool_size=settings.fetch_workers)
    library = Library(client, settings.stream_base, settings.fetch_workers)
    server = create_server(lambda: library.snapshot, settings.listen_port)

    stop_event = threading.Event()

    def handle_signal(signum, frame) -> None:
        logger.info("Получен сигнал завершения, останавливаюсь...")
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    refresher = threading.Thread(
        target=library.run,
        args=(settings.refresh_interval, stop_event),
        name="library-refresh",
        daemon=True,
    )
    refresher.start()

    server_thread = threading.Thread(target=server.serve_forever, name="webdav", daemon=True)
    server_thread.start()

    logger.info(
        "Сервис запущен. TorrServer: %s | Потоки: %s/s/*** | WebDAV: порт %d | Обновление: %d сек.",
        settings.torrserver_url,
        settings.public_url,
        settings.listen_port,
        settings.refresh_interval,
    )

    stop_event.wait()
    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
