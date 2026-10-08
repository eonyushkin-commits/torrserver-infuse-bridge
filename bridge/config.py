"""Настройки сервиса из переменных окружения с проверкой при старте."""

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,}$")


class ConfigError(ValueError):
    """Некорректная или отсутствующая настройка."""


@dataclass(frozen=True)
class Settings:
    # Адрес TorrServer внутри Docker-сети (host:port), без авторизации.
    torrserver_addr: str
    # Внешний адрес шлюза, по которому Infuse открывает потоки: https://media.example.com
    public_url: str
    # Секрет в пути ссылок на поток: /s/<token>/stream/...
    stream_token: str
    listen_port: int = 8080
    refresh_interval: int = 30
    fetch_workers: int = 8

    @property
    def torrserver_url(self) -> str:
        return f"http://{self.torrserver_addr}"

    @property
    def stream_base(self) -> str:
        return f"{self.public_url}/s/{self.stream_token}"


def _int(env, name: str, default: int, minimum: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} должен быть целым числом, получено: {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} должен быть не меньше {minimum}, получено: {value}")
    return value


def load_settings(env=None) -> Settings:
    env = os.environ if env is None else env

    public_url = env.get("PUBLIC_URL", "").strip().rstrip("/")
    parts = urlsplit(public_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(
            "PUBLIC_URL должен быть полным адресом, например https://media.example.com "
            f"или http://1.2.3.4:51234; получено: {public_url!r}"
        )
    if parts.path or parts.query or parts.username:
        raise ConfigError("PUBLIC_URL не должен содержать путь, параметры или логин")

    token = env.get("STREAM_TOKEN", "").strip()
    if not _TOKEN_RE.match(token):
        raise ConfigError(
            "STREAM_TOKEN должен состоять из латиницы, цифр, '-' или '_' "
            "и быть не короче 16 символов"
        )

    addr = env.get("TORRSERVER_ADDR", "torrserver:8090").strip()
    if not addr or "/" in addr:
        raise ConfigError(f"TORRSERVER_ADDR должен иметь вид host:port, получено: {addr!r}")

    return Settings(
        torrserver_addr=addr,
        public_url=public_url,
        stream_token=token,
        listen_port=_int(env, "LISTEN_PORT", 8080, 1),
        refresh_interval=_int(env, "REFRESH_INTERVAL", 30, 5),
        fetch_workers=_int(env, "FETCH_WORKERS", 8, 1),
    )
