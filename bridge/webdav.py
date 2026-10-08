"""Минимальный WebDAV-сервер только для чтения (RFC 4918, класс 1).

Поддерживает то, что нужно Infuse и обычным клиентам: OPTIONS, PROPFIND (Depth 0/1),
GET и HEAD. Изменяющие методы отклоняются. Авторизацию выполняет шлюз перед сервисом.
"""

import logging
import posixpath
import time
import urllib.parse
from email.utils import formatdate
from html import escape as html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape as xml_escape

logger = logging.getLogger(__name__)

HEALTH_PATH = "/healthz"
READ_METHODS = "OPTIONS, PROPFIND, GET, HEAD"
_MAX_BODY = 64 * 1024


def _http_date(timestamp: float) -> str:
    return formatdate(timestamp, usegmt=True)


def _iso_date(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _href(path: str, is_dir: bool) -> str:
    href = urllib.parse.quote(path, safe="/")
    if is_dir and not href.endswith("/"):
        href += "/"
    return href


class DavHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "torrserver-infuse-bridge"
    sys_version = ""

    # Устанавливается при создании сервера: функция, возвращающая текущий Snapshot.
    get_snapshot = None

    #--------------------------------------------------------------------------
    # Вспомогательные методы.
    #--------------------------------------------------------------------------
    def _path(self) -> str:
        raw = urllib.parse.urlsplit(self.path).path
        path = posixpath.normpath("/" + urllib.parse.unquote(raw))
        # normpath сохраняет ведущий "//" — приводим к одному слэшу.
        return "/" + path.lstrip("/")

    def _discard_body(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            self.close_connection = True
            return
        if length > 0:
            self.rfile.read(length)

    def _send(self, status: int, body: bytes = b"", content_type: str = None, headers=None):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args) -> None:
        logger.debug("%s - %s", self.address_string(), fmt % args)

    #--------------------------------------------------------------------------
    # Методы WebDAV.
    #--------------------------------------------------------------------------
    def do_OPTIONS(self) -> None:
        self._discard_body()
        self._send(200, headers={"DAV": "1", "Allow": READ_METHODS, "MS-Author-Via": "DAV"})

    def do_PROPFIND(self) -> None:
        self._discard_body()
        snapshot = self.get_snapshot()
        path = self._path()
        depth = self.headers.get("Depth", "1").strip()

        if path in snapshot.dirs:
            responses = [self._dir_response(path, snapshot.dirs[path])]
            if depth != "0":
                for name in snapshot.dirs[path].children:
                    child = posixpath.join(path, name)
                    if child in snapshot.dirs:
                        responses.append(self._dir_response(child, snapshot.dirs[child]))
                    else:
                        responses.append(self._file_response(child, snapshot.files[child]))
        elif path in snapshot.files:
            responses = [self._file_response(path, snapshot.files[path])]
        else:
            self._send(404, b"Not Found", "text/plain; charset=utf-8")
            return

        body = (
            '<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">'
            + "".join(responses)
            + "</D:multistatus>"
        ).encode("utf-8")
        self._send(207, body, 'application/xml; charset="utf-8"')

    def do_GET(self) -> None:
        path = self._path()
        if path == HEALTH_PATH:
            self._send(200, b"ok", "text/plain; charset=utf-8")
            return

        snapshot = self.get_snapshot()
        entry = snapshot.files.get(path)
        if entry is not None:
            etag = f'"{entry.etag}"'
            headers = {"ETag": etag, "Last-Modified": _http_date(entry.mtime)}
            if self.headers.get("If-None-Match") == etag:
                self._send(304, headers=headers)
                return
            self._send(200, entry.content, "text/plain; charset=utf-8", headers)
            return

        directory = snapshot.dirs.get(path)
        if directory is not None:
            page = self._dir_listing(path, directory.children, snapshot.dirs)
            self._send(200, page, "text/html; charset=utf-8")
            return

        self._send(404, b"Not Found", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:
        self.do_GET()

    def _read_only(self) -> None:
        self._discard_body()
        self._send(403, b"Read-only library", "text/plain; charset=utf-8", {"Allow": READ_METHODS})

    do_PUT = do_DELETE = do_MKCOL = do_COPY = do_MOVE = _read_only
    do_PROPPATCH = do_LOCK = do_UNLOCK = do_POST = _read_only

    #--------------------------------------------------------------------------
    # Формирование ответов.
    #--------------------------------------------------------------------------
    @staticmethod
    def _dir_response(path: str, entry) -> str:
        name = posixpath.basename(path) or "/"
        return (
            f"<D:response><D:href>{xml_escape(_href(path, True))}</D:href>"
            "<D:propstat><D:prop>"
            f"<D:displayname>{xml_escape(name)}</D:displayname>"
            "<D:resourcetype><D:collection/></D:resourcetype>"
            f"<D:creationdate>{_iso_date(entry.mtime)}</D:creationdate>"
            f"<D:getlastmodified>{_http_date(entry.mtime)}</D:getlastmodified>"
            "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
        )

    @staticmethod
    def _file_response(path: str, entry) -> str:
        return (
            f"<D:response><D:href>{xml_escape(_href(path, False))}</D:href>"
            "<D:propstat><D:prop>"
            f"<D:displayname>{xml_escape(posixpath.basename(path))}</D:displayname>"
            "<D:resourcetype/>"
            f"<D:getcontentlength>{len(entry.content)}</D:getcontentlength>"
            "<D:getcontenttype>text/plain</D:getcontenttype>"
            f'<D:getetag>"{entry.etag}"</D:getetag>'
            f"<D:creationdate>{_iso_date(entry.mtime)}</D:creationdate>"
            f"<D:getlastmodified>{_http_date(entry.mtime)}</D:getlastmodified>"
            "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
        )

    @staticmethod
    def _dir_listing(path: str, children, dirs) -> bytes:
        links = []
        if path != "/":
            parent = posixpath.dirname(path)
            links.append(f'<li><a href="{_href(parent, True)}">../</a></li>')
        for name in children:
            child = posixpath.join(path, name)
            is_dir = child in dirs
            label = html_escape(name + ("/" if is_dir else ""))
            links.append(f'<li><a href="{html_escape(_href(child, is_dir))}">{label}</a></li>')
        title = html_escape(path)
        page = (
            f"<!doctype html><meta charset=utf-8><title>{title}</title>"
            f"<h1>{title}</h1><ul>{''.join(links)}</ul>"
        )
        return page.encode("utf-8")


def create_server(get_snapshot, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    handler = type("BoundDavHandler", (DavHandler,), {"get_snapshot": staticmethod(get_snapshot)})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
