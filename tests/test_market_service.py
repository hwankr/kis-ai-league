from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from backend.dashboard import DashboardServer
from backend.kis import KisError
from backend.market import MarketService, load_market_settings
from backend.market_history import MarketHistory
from test_collector import quote


class MarketServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.config = self.folder / "config.toml"
        self.config.write_text("[market_data]\nsymbols=['005930']\ninterval_seconds=60\n", encoding="utf-8")
        self.database = self.folder / "market.sqlite3"
        self.store = MarketHistory(self.database)
        self.names = Mock()
        self.names.lookup.return_value = {"005930": "삼성전자"}
        self.service = MarketService(self.config, self.store, symbol_names=self.names)

    def test_empty_then_saved_quote_and_failure_status_are_local_reads(self):
        empty = self.service.snapshot()
        self.assertEqual(empty["collector"]["state"], "not_started")
        self.assertEqual(empty["collector"]["interval_seconds"], 60)
        self.assertIsNone(empty["quotes"][0]["price"])
        self.assertEqual(empty["quotes"][0]["name"], "삼성전자")
        now = datetime.now(timezone.utc)
        self.store.record_quote(quote(), now.isoformat(), "first")
        self.store.record_attempt("005930", (now + timedelta(seconds=1)).isoformat(), "시세 조회 오류")
        self.store.set_collector("running", now.isoformat(), collector_id="private-control-id",
                                 interval_seconds=60, symbols=["005930"])
        result = self.service.snapshot()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["quotes"][0]["price"], "70000")
        self.assertEqual(result["quotes"][0]["name"], "삼성전자")
        self.assertEqual(result["quotes"][0]["error"], "시세 조회 오류")
        self.assertNotIn("private-control-id", json.dumps(result))

    def test_reading_market_api_never_queries_account_or_broker(self):
        account_service = Mock()
        server = DashboardServer(0, account_service, market_service=self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def get(path, headers=None):
            conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                conn.request("GET", path, headers=headers or {})
                res = conn.getresponse()
                return res.status, json.loads(res.read())
            finally:
                conn.close()
        headers = {"X-KIS-Dashboard": "1"}
        for _ in range(2):
            self.assertEqual(get("/api/market", headers)[0], 200)
        account_service.snapshot.assert_not_called()
        account_service.list_accounts.assert_not_called()
        self.assertEqual(self.store.read(["005930"])["total_count"], 0)
        self.assertEqual(get("/api/market")[0], 403)
        self.assertEqual(get("/api/market", {**headers, "Origin": "https://bad.invalid"})[0], 403)
        self.assertEqual(get("/api/market?account=paper", headers)[0], 400)
        self.assertEqual(get("/.local/market-history.sqlite3")[0], 404)
        self.config.write_text("[market_data]\nsymbols=[]", encoding="utf-8")
        status, data = get("/api/market", headers)
        self.assertEqual(status, 503)
        self.assertEqual(data["quotes"], [])

    def test_watchlist_changes_do_not_return_removed_symbols_or_erase_history(self):
        self.store.record_quote(quote(), datetime.now(timezone.utc).isoformat(), "first")
        self.config.write_text("[market_data]\nsymbols=['000660']\ninterval_seconds=30", encoding="utf-8")
        result = self.service.snapshot()
        self.assertEqual(result["symbols"], ["000660"])
        self.assertIsNone(result["quotes"][0]["price"])
        self.assertNotIn("name", result["quotes"][0])
        self.assertEqual(self.store.read(["005930"])["total_count"], 1)
        self.assertEqual(result["collector"]["interval_seconds"], 30)

    def test_name_catalog_failure_keeps_saved_quote_readable(self):
        self.store.record_quote(quote(), datetime.now(timezone.utc).isoformat(), "first")
        self.names.lookup.side_effect = OSError("private-catalog-error")
        result = self.service.snapshot()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["quotes"][0]["price"], "70000")
        self.assertNotIn("name", result["quotes"][0])
        self.assertNotIn("private-catalog-error", str(result))

    def test_corrupt_storage_returns_safe_error_not_empty_success(self):
        self.database.write_text("private-broken-data", encoding="utf-8")
        result = self.service.snapshot()
        self.assertEqual(result["status"], "error")
        self.assertNotIn("private-broken", str(result))

    def test_watchlist_defaults_and_invalid_values_are_validated_without_reading_keys(self):
        self.config.write_text("app_key='private-key'", encoding="utf-8")
        self.assertEqual(load_market_settings(self.config).symbols, ("005930",))
        for values in ("symbols=[]", "symbols=['005930','005930']", "symbols=['bad-secret']",
                       "symbols='005930'", "interval_seconds=true", "interval_seconds=9",
                       "interval_seconds=3601", "account='../private-secret'", "unsupported=true"):
            with self.subTest(values=values):
                self.config.write_text("[market_data]\n" + values, encoding="utf-8")
                with self.assertRaises(KisError) as caught:
                    load_market_settings(self.config)
                self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
