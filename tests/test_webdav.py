import http.client
import threading
import unittest
import xml.etree.ElementTree as ET

from bridge.library import Library
from bridge.webdav import create_server
from tests.test_library import H1, FakeClient

NS = {"D": "DAV:"}


class WebDavTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        client = FakeClient()
        client.torrents = [{"hash": H1, "timestamp": 100}]
        client.files = {H1: [{"path": "Show/Show.S01E01.mkv", "id": 1}]}
        library = Library(client, "https://x/s/token")
        library.refresh()

        cls.server = create_server(lambda: library.snapshot, 0, host="127.0.0.1")
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response, data

    def hrefs(self, data):
        return [el.text for el in ET.fromstring(data).findall("D:response/D:href", NS)]

    def test_options(self):
        response, _ = self.request("OPTIONS", "/")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("DAV"), "1")

    def test_propfind_root_depth_1(self):
        response, data = self.request("PROPFIND", "/", {"Depth": "1"}, b"<propfind/>")
        self.assertEqual(response.status, 207)
        self.assertEqual(self.hrefs(data), ["/", "/TV/"])

    def test_propfind_depth_0(self):
        _, data = self.request("PROPFIND", "/TV", {"Depth": "0"})
        self.assertEqual(self.hrefs(data), ["/TV/"])

    def test_propfind_encoded_path(self):
        response, data = self.request("PROPFIND", "/TV/Show/Season%2001/", {"Depth": "1"})
        self.assertEqual(response.status, 207)
        self.assertEqual(
            self.hrefs(data),
            ["/TV/Show/Season%2001/", "/TV/Show/Season%2001/Show%20S01E01.strm"],
        )
        root = ET.fromstring(data)
        length = root.find("D:response[2]/D:propstat/D:prop/D:getcontentlength", NS).text
        self.assertGreater(int(length), 0)

    def test_get_file(self):
        response, data = self.request("GET", "/TV/Show/Season%2001/Show%20S01E01.strm")
        self.assertEqual(response.status, 200)
        self.assertEqual(data.decode(), f"https://x/s/token/stream/Show.S01E01.mkv?link={H1}&index=1&play")
        self.assertEqual(int(response.getheader("Content-Length")), len(data))

    def test_head_file(self):
        response, data = self.request("HEAD", "/TV/Show/Season%2001/Show%20S01E01.strm")
        self.assertEqual(response.status, 200)
        self.assertEqual(data, b"")
        self.assertGreater(int(response.getheader("Content-Length")), 0)

    def test_not_found_and_traversal(self):
        for path in ("/nope", "/../etc/passwd", "/TV/../../x"):
            response, _ = self.request("PROPFIND", path)
            self.assertEqual(response.status, 404, path)

    def test_read_only(self):
        for method in ("PUT", "DELETE", "MKCOL", "MOVE", "LOCK"):
            response, _ = self.request(method, "/TV/x", body=b"data")
            self.assertEqual(response.status, 403, method)

    def test_healthz(self):
        response, data = self.request("GET", "/healthz")
        self.assertEqual((response.status, data), (200, b"ok"))


if __name__ == "__main__":
    unittest.main()
