"""Виртуальная медиатека: дерево .strm-файлов, построенное из текущего состояния TorrServer.

На диск ничего не пишется — дерево живёт в памяти и обновляется фоном. Метаданные
запрашиваются только для новых торрентов; удалённые торренты исчезают из дерева сами.
"""

import hashlib
import logging
import os
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .naming import is_video, library_path

logger = logging.getLogger(__name__)


#------------------------------------------------------------------------------
# Клиент TorrServer.
#------------------------------------------------------------------------------
class TorrServerClient:
    def __init__(self, base_url: str, pool_size: int = 8, timeout: float = 10):
        self._url = f"{base_url}/torrents"
        self._timeout = timeout

        retry = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=("POST",),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=pool_size)
        self._session = requests.Session()
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def list_torrents(self) -> list:
        """Список торрентов. Исключение при ошибке — вызывающий сохранит прежнее дерево."""
        response = self._session.post(self._url, json={"action": "list"}, timeout=self._timeout)
        response.raise_for_status()
        torrents = response.json()
        if not isinstance(torrents, list):
            raise ValueError(f"неожиданный ответ TorrServer: {type(torrents).__name__}")
        return torrents

    def get_files(self, t_hash: str):
        """Файлы торрента или None, если метаданные ещё не загружены или запрос не удался."""
        try:
            response = self._session.post(
                self._url,
                json={"action": "get", "hash": t_hash},
                timeout=self._timeout,
            )
            response.raise_for_status()
            return response.json().get("file_stats") or None
        except (requests.RequestException, ValueError, AttributeError) as exc:
            logger.warning("%s...: не удалось получить список файлов — %s", t_hash[:8], exc)
            return None


#------------------------------------------------------------------------------
# Снимок дерева медиатеки.
#------------------------------------------------------------------------------
@dataclass(frozen=True)
class FileEntry:
    content: bytes
    mtime: float
    etag: str


@dataclass(frozen=True)
class DirEntry:
    children: tuple
    mtime: float


@dataclass(frozen=True)
class Snapshot:
    dirs: dict
    files: dict

    @classmethod
    def empty(cls) -> "Snapshot":
        return cls(dirs={"/": DirEntry(children=(), mtime=time.time())}, files={})


@dataclass(frozen=True)
class LibraryItem:
    parts: tuple  # папки + имя без расширения, см. naming.library_path
    file_id: int
    filename: str


@dataclass
class Torrent:
    hash: str
    title: str
    added: float
    items: list = field(default_factory=list)


def layout_files(file_stats: list) -> list:
    """Раскладывает видеофайлы торрента по медиатеке (один раз на торрент)."""
    items = []
    for idx, info in enumerate(file_stats):
        path = str(info.get("path", ""))
        if not is_video(path):
            continue
        parts = library_path(path)
        if parts is None:
            continue
        items.append(LibraryItem(parts, info.get("id", idx + 1), os.path.basename(path)))
    return items


def stream_url(stream_base: str, t_hash: str, item: LibraryItem) -> str:
    filename = urllib.parse.quote(item.filename, safe="")
    return f"{stream_base}/stream/{filename}?link={t_hash}&index={item.file_id}&play"


def build_snapshot(torrents, stream_base: str) -> Snapshot:
    """Строит дерево. Торренты обходятся от старых к новым, поэтому при совпадении путей
    имя без суффикса остаётся за ранее добавленным — пути не «переезжают».
    """
    files = {}
    for torrent in sorted(torrents, key=lambda t: (t.added, t.hash)):
        for item in torrent.items:
            *folders, name = item.parts
            dir_path = "/" + "/".join(folders)
            path = f"{dir_path}/{name}.strm"
            if path in files:
                path = f"{dir_path}/{name} - {torrent.hash[:8]}-{item.file_id}.strm"

            content = stream_url(stream_base, torrent.hash, item).encode("utf-8")
            etag = hashlib.sha1(content).hexdigest()[:16]
            files[path] = FileEntry(content=content, mtime=torrent.added, etag=etag)

    children = {"/": set()}
    mtimes = {"/": 0.0}
    for path, entry in files.items():
        child = path
        while child != "/":
            parent = os.path.dirname(child)
            children.setdefault(parent, set()).add(os.path.basename(child))
            mtimes[parent] = max(mtimes.get(parent, 0.0), entry.mtime)
            child = parent

    now = time.time()
    dirs = {
        path: DirEntry(children=tuple(sorted(names)), mtime=mtimes.get(path) or now)
        for path, names in children.items()
    }
    return Snapshot(dirs=dirs, files=files)


#------------------------------------------------------------------------------
# Медиатека с фоновым обновлением.
#------------------------------------------------------------------------------
class Library:
    def __init__(self, client, stream_base: str, fetch_workers: int = 8):
        self._client = client
        self._stream_base = stream_base
        self._workers = fetch_workers
        self._torrents = {}
        self._waiting = set()
        self._snapshot = Snapshot.empty()
        self._refresh_lock = threading.Lock()

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    def refresh(self) -> None:
        with self._refresh_lock:
            self._refresh()

    def _refresh(self) -> None:
        active = {}
        for torrent in self._client.list_torrents():
            t_hash = torrent.get("hash")
            if t_hash:
                active[t_hash] = torrent

        for t_hash in list(self._torrents):
            if t_hash not in active:
                removed = self._torrents.pop(t_hash)
                logger.info("Удалён из медиатеки: %s", removed.title or t_hash[:8])
        self._waiting &= set(active)

        missing = [h for h in active if h not in self._torrents]
        if missing:
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                results = list(pool.map(self._client.get_files, missing))

            for t_hash, file_stats in zip(missing, results, strict=True):
                meta = active[t_hash]
                title = str(meta.get("title") or t_hash[:8])
                if not file_stats:
                    if t_hash not in self._waiting:
                        logger.info("Ожидаются метаданные: %s", title)
                        self._waiting.add(t_hash)
                    continue

                self._waiting.discard(t_hash)
                items = layout_files(file_stats)
                self._torrents[t_hash] = Torrent(
                    hash=t_hash,
                    title=title,
                    added=_timestamp(meta.get("timestamp")),
                    items=items,
                )
                logger.info("Добавлен в медиатеку: %s (видеофайлов: %d)", title, len(items))

        self._snapshot = build_snapshot(self._torrents.values(), self._stream_base)

    def run(self, interval: float, stop_event: threading.Event) -> None:
        """Цикл фонового обновления до установки stop_event."""
        while True:
            try:
                self.refresh()
            except Exception as exc:
                logger.error("Не удалось обновить медиатеку: %s", exc)
            if stop_event.wait(interval):
                return


def _timestamp(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return time.time()
    return value if value > 0 else time.time()
