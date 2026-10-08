import unittest

from bridge.library import Library

H1 = "a" * 40
H2 = "b" * 64  # BitTorrent v2
BASE = "https://media.example.com/s/token"


class FakeClient:
    def __init__(self):
        self.torrents = []
        self.files = {}
        self.get_calls = []

    def list_torrents(self):
        return self.torrents

    def get_files(self, t_hash):
        self.get_calls.append(t_hash)
        return self.files.get(t_hash) or None


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.library = Library(self.client, BASE, fetch_workers=2)

    def add(self, t_hash, timestamp, *paths):
        self.client.torrents.append({"hash": t_hash, "title": t_hash[:4], "timestamp": timestamp})
        self.client.files[t_hash] = [{"path": p, "id": i + 1} for i, p in enumerate(paths)]

    def test_builds_tree(self):
        self.add(H1, 100, "Show/Show.S01E01.mkv", "Show/Show.S01E02.mkv", "Show/info.nfo")
        self.library.refresh()
        snap = self.library.snapshot

        self.assertEqual(snap.dirs["/"].children, ("TV",))
        self.assertEqual(snap.dirs["/TV/Show/Season 01"].children, ("Show S01E01.strm", "Show S01E02.strm"))
        content = snap.files["/TV/Show/Season 01/Show S01E02.strm"].content.decode()
        self.assertEqual(content, f"{BASE}/stream/Show.S01E02.mkv?link={H1}&index=2&play")
        self.assertEqual(snap.dirs["/TV/Show"].mtime, 100)

    def test_filename_is_url_encoded(self):
        self.add(H1, 100, "Фильм 2010/a/b#c.mkv")
        self.library.refresh()
        (entry,) = self.library.snapshot.files.values()
        self.assertIn("/stream/b%23c.mkv?", entry.content.decode())

    def test_duplicates_get_suffix_and_older_keeps_name(self):
        self.add(H2, 200, "Movie.2010.720p.mkv")
        self.add(H1, 100, "Movie.2010.1080p.mkv")
        self.library.refresh()
        names = self.library.snapshot.dirs["/Movies/Movie (2010)"].children
        self.assertEqual(names, ("Movie (2010) - bbbbbbbb-1.strm", "Movie (2010).strm"))
        older = self.library.snapshot.files["/Movies/Movie (2010)/Movie (2010).strm"]
        self.assertIn(f"link={H1}", older.content.decode())

    def test_removed_torrent_disappears(self):
        self.add(H1, 100, "Movie.2010.mkv")
        self.library.refresh()
        self.client.torrents = []
        self.library.refresh()
        self.assertEqual(self.library.snapshot.files, {})
        self.assertEqual(self.library.snapshot.dirs["/"].children, ())

    def test_metadata_fetched_only_once(self):
        self.add(H1, 100, "Movie.2010.mkv")
        self.library.refresh()
        self.library.refresh()
        self.assertEqual(self.client.get_calls, [H1])

    def test_pending_torrent_retried(self):
        self.client.torrents.append({"hash": H1, "timestamp": 100})
        self.library.refresh()
        self.assertEqual(self.library.snapshot.files, {})
        self.client.files[H1] = [{"path": "Movie.2010.mkv", "id": 1}]
        self.library.refresh()
        self.assertEqual(len(self.library.snapshot.files), 1)

    def test_list_error_keeps_previous_tree(self):
        self.add(H1, 100, "Movie.2010.mkv")
        self.library.refresh()

        def boom():
            raise ConnectionError("down")

        self.client.list_torrents = boom
        with self.assertRaises(ConnectionError):
            self.library.refresh()
        self.assertEqual(len(self.library.snapshot.files), 1)


if __name__ == "__main__":
    unittest.main()
