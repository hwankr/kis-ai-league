import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock
from zipfile import ZipFile

from backend.symbol_names import CACHE_SECONDS, RETRY_SECONDS, SymbolNames


def master(board, symbol, name):
    # 실제 KIS CP949 고정폭. 한글명 뒤의 필드는 문자열 글자 수가 아닌 바이트 수다.
    tail_size = 227 if board == "kospi" else 221
    row = (symbol.encode("ascii").ljust(9) + b"KR7005930003"
           + name.encode("cp949").ljust(40) + b"0" * tail_size + b"\r\n")
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(f"{board}_code.mst", row)
    return output.getvalue()


def fetch_master(url):
    if "kospi_" in url:
        return master("kospi", "005930", "삼성전자")
    return master("kosdaq", "035900", "JYP Ent.")


class SymbolNamesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "symbol-names.json"
        self.now = 1_000_000.0

    def cache(self, **kwargs):
        return SymbolNames(self.path, now=lambda: self.now, **kwargs)

    def test_parses_both_cp949_masters_and_persists_names_only(self):
        fetcher = Mock(side_effect=fetch_master)
        cache = self.cache(fetcher=fetcher)
        self.assertTrue(cache.refresh())
        self.assertEqual(cache.lookup(["005930", "035900", "999999"]),
                         {"005930": "삼성전자", "035900": "JYP Ent."})
        self.assertEqual(fetcher.call_count, 2)
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(saved), {"updated_at", "names"})
        self.assertEqual(saved["names"]["005930"], "삼성전자")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_fresh_cache_and_unknown_symbols_do_not_fetch(self):
        self.path.write_text(json.dumps({"updated_at": self.now, "names": {"005930": "삼성전자"}}), encoding="utf-8")
        fetcher = Mock(side_effect=AssertionError("unexpected network"))
        cache = self.cache(fetcher=fetcher)
        self.now += CACHE_SECONDS - 1
        self.assertEqual(cache.lookup(["005930", "999999"]), {"005930": "삼성전자"})
        self.assertEqual(cache.lookup(["999999"]), {})
        fetcher.assert_not_called()

    def test_download_or_bad_archive_failure_preserves_cache_and_backs_off(self):
        cache = self.cache(fetcher=fetch_master)
        self.assertTrue(cache.refresh())
        saved = self.path.read_bytes()
        for failure in (OSError("offline"), b"not a zip"):
            fetcher = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
            cache = self.cache(fetcher=fetcher)
            self.now += CACHE_SECONDS
            self.assertFalse(cache.refresh())
            self.now += RETRY_SECONDS - 1
            self.assertEqual(cache.lookup(["005930"]), {"005930": "삼성전자"})
            self.assertEqual(fetcher.call_count, 1)
            self.assertEqual(self.path.read_bytes(), saved)

    def test_initial_lookup_returns_before_download_and_only_starts_one_refresh(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def blocking_fetch(url):
            entered.set()
            release.wait(3)
            return fetch_master(url)
        fetcher = Mock(side_effect=blocking_fetch)
        cache = self.cache(fetcher=fetcher)
        original_update = cache._update
        def update():
            try:
                return original_update()
            finally:
                finished.set()
        cache._update = update
        self.addCleanup(release.set)
        self.assertEqual(cache.lookup(["005930"]), {})
        self.assertTrue(entered.wait(1))
        self.assertEqual(cache.lookup(["005930"]), {})
        self.assertEqual(fetcher.call_count, 1)
        release.set()
        self.assertTrue(finished.wait(3))
        self.assertEqual(cache.lookup(["005930"]), {"005930": "삼성전자"})
        self.assertEqual(fetcher.call_count, 2)

    def test_changed_format_is_rejected_without_publishing_partial_names(self):
        cache = self.cache(fetcher=fetch_master)
        self.assertTrue(cache.refresh())
        saved = self.path.read_bytes()
        broken = io.BytesIO()
        with ZipFile(broken, "w") as archive:
            archive.writestr("kosdaq_code.mst", b"035900 malformed row\n")
        cache._fetcher = lambda url: fetch_master(url) if "kospi_" in url else broken.getvalue()
        self.assertFalse(cache.refresh())
        self.assertEqual(cache.lookup(["005930", "035900"]), {"005930": "삼성전자", "035900": "JYP Ent."})
        self.assertEqual(self.path.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
