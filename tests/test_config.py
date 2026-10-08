import unittest

from bridge.config import ConfigError, load_settings

TOKEN = "a" * 32


class LoadSettingsTest(unittest.TestCase):
    def test_valid(self):
        settings = load_settings({"PUBLIC_URL": "https://media.example.com/", "STREAM_TOKEN": TOKEN})
        self.assertEqual(settings.public_url, "https://media.example.com")
        self.assertEqual(settings.stream_base, f"https://media.example.com/s/{TOKEN}")
        self.assertEqual(settings.torrserver_url, "http://torrserver:8090")

    def test_ip_with_port(self):
        settings = load_settings({"PUBLIC_URL": "http://1.2.3.4:51234", "STREAM_TOKEN": TOKEN})
        self.assertEqual(settings.public_url, "http://1.2.3.4:51234")

    def test_invalid_public_url(self):
        for url in ("", "1.2.3.4", "ftp://x", "https://x/path", "https://u:p@x"):
            with self.subTest(url=url), self.assertRaises(ConfigError):
                load_settings({"PUBLIC_URL": url, "STREAM_TOKEN": TOKEN})

    def test_invalid_token(self):
        for token in ("", "short", "a" * 15, "has/slash" * 3):
            with self.subTest(token=token), self.assertRaises(ConfigError):
                load_settings({"PUBLIC_URL": "https://x", "STREAM_TOKEN": token})

    def test_invalid_numbers(self):
        with self.assertRaises(ConfigError):
            load_settings({"PUBLIC_URL": "https://x", "STREAM_TOKEN": TOKEN, "REFRESH_INTERVAL": "1"})
        with self.assertRaises(ConfigError):
            load_settings({"PUBLIC_URL": "https://x", "STREAM_TOKEN": TOKEN, "LISTEN_PORT": "abc"})


if __name__ == "__main__":
    unittest.main()
