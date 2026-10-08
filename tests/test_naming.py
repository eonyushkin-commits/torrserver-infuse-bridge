import unittest

from bridge.naming import is_video, library_path, sanitize


class LibraryPathTest(unittest.TestCase):
    def assertPath(self, file_path, expected):
        self.assertEqual(library_path(file_path), expected, file_path)

    def test_movie_with_year(self):
        self.assertPath(
            "Interstellar.2014.1080p.BluRay.mkv",
            ("Movies", "Interstellar (2014)", "Interstellar (2014)"),
        )

    def test_year_from_folder(self):
        self.assertPath(
            "Interstellar.2014.1080p/interstellar.mkv",
            ("Movies", "Interstellar (2014)", "Interstellar (2014)"),
        )

    def test_movie_parts(self):
        self.assertPath("Movie.2010.CD1.avi", ("Movies", "Movie (2010)", "Movie (2010) - Part 1"))
        self.assertPath("Movie.2010.CD2.avi", ("Movies", "Movie (2010)", "Movie (2010) - Part 2"))

    def test_episode(self):
        self.assertPath(
            "Сериал.S01E03.WEB-DL.mkv",
            ("TV", "Сериал", "Season 01", "Сериал S01E03"),
        )

    def test_multi_episode(self):
        self.assertPath(
            "Breaking.Bad.S01E01E02.mkv",
            ("TV", "Breaking Bad", "Season 01", "Breaking Bad S01E01-E02"),
        )

    def test_episode_without_season(self):
        self.assertPath("Show - 05.mkv", ("TV", "Show", "Show E05"))

    def test_season_from_folder(self):
        self.assertPath("Friends/Season 1/01.mkv", ("TV", "Friends", "Season 01", "Friends S01E01"))

    def test_date_episode(self):
        self.assertPath("The.Office.2005.09.22.mkv", ("TV", "The Office", "The Office 2005-09-22"))

    def test_samples_are_skipped(self):
        self.assertIsNone(library_path("Movie.2010.sample.mkv"))
        self.assertIsNone(library_path("The.Matrix.1999/Sample/sample.mkv"))

    def test_forbidden_chars(self):
        folders_and_name = library_path("x/a:b?.mkv")
        for part in folders_and_name:
            self.assertNotRegex(part, r'[/\\:*?"<>|]')


class HelpersTest(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(sanitize(' a/b  c. '), "a b c")
        self.assertEqual(sanitize("..."), "Unknown")

    def test_is_video(self):
        self.assertTrue(is_video("A/B.MKV"))
        self.assertFalse(is_video("A/B.srt"))


if __name__ == "__main__":
    unittest.main()
