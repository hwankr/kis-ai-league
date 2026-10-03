from http.client import HTTPConnection
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from backend.dashboard import DashboardServer


class FrontendAssetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.dist = self.root / "dist"
        (self.dist / "assets").mkdir(parents=True)
        (self.dist / "fonts").mkdir()
        self.files = {
            "index.html": (b'<div id="root"></div><script src="/assets/index-test.js"></script>', "text/html"),
            "assets/index-test.js": (b"export const app = 'react';", "text/javascript"),
            "assets/index-test.css": (b"body { color: black; }", "text/css"),
            "fonts/PretendardVariable.woff2": (b"sample-font", "font/woff2"),
            "fonts/OFL.txt": (b"font-license", "text/plain"),
        }
        for name, (body, _) in self.files.items():
            (self.dist / name).write_bytes(body)
        (self.root / "config.local.toml").write_text("private-test-secret", encoding="utf-8")
        self.service = Mock()
        self.server = DashboardServer(0, self.service, frontend_path=self.dist)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            connection.close()

    def test_built_entry_assets_and_local_font_are_served_with_security_headers(self):
        for name, (expected, content_type) in self.files.items():
            with self.subTest(name=name):
                status, headers, body = self.request("/" + name + "?v=1")
                self.assertEqual(status, 200)
                self.assertEqual(body, expected)
                self.assertTrue(headers["Content-Type"].startswith(content_type))
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertNotIn("unsafe", headers["Content-Security-Policy"])
        self.assertEqual(self.request("/")[2], self.files["index.html"][0])
        self.service.snapshot.assert_not_called()

    def test_source_private_paths_and_traversal_are_not_public(self):
        for path in ("/src/App.tsx", "/dashboard.js", "/dashboard.css", "/package.json",
                     "/config.local.toml", "/.local/account-history.sqlite3",
                     "/assets/../config.local.toml", "/assets/%2e%2e/config.local.toml",
                     "/assets/..%5cconfig.local.toml", "/assets/.env.js",
                     "/assets/index-test.js.map", "/@fs/config.local.toml",
                     "/node_modules/react/index.js", "/.vite/manifest.json"):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"private-test-secret", body)

    def test_missing_build_returns_setup_error_and_missing_asset_is_not_html(self):
        (self.dist / "index.html").unlink()
        status, _, body = self.request("/")
        self.assertEqual(status, 503)
        self.assertIn(b"npm run build", body)
        status, headers, body = self.request("/assets/missing.js")
        self.assertEqual(status, 404)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertNotIn(b"<html", body)

    def test_assets_cannot_follow_links_outside_build_directory(self):
        linked = self.dist / "assets" / "linked.js"
        try:
            linked.symlink_to(self.root / "config.local.toml")
        except OSError:
            self.skipTest("Symbolic links are unavailable on this host")
        status, _, body = self.request("/assets/linked.js")
        self.assertEqual(status, 404)
        self.assertNotIn(b"private-test-secret", body)


if __name__ == "__main__":
    unittest.main()
