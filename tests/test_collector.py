from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from backend.collector import Collector
from backend.kis import KisError
from backend.market_history import MarketHistory


def quote(symbol="005930", **changes):
    return {"environment": "paper", "market": "KRX", "symbol": symbol, "price": "70000",
            "change_percent": "-0.5", "volume": "120", "cumulative_turnover": "8400000", **changes}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.config = self.folder / "config.toml"
        self.base = "app_key='test-key'\napp_secret='test-secret'\n"
        self.config.write_text(self.base + "[market_data]\nsymbols=['005930','000660']\ninterval_seconds=60\n", encoding="utf-8")
        self.database = self.folder / "quotes.sqlite3"
        self.store = MarketHistory(self.database)
        self.now = datetime(2026, 10, 3, 9, tzinfo=timezone.utc)
        self.client = Mock()
        self.client.quote.side_effect = lambda symbol: quote(symbol)
        self.factory = Mock(return_value=self.client)
        self.collector = Collector(self.config, self.database, self.factory, self.store, now=lambda: self.now)

    def test_each_successful_poll_is_saved_even_if_unchanged_and_no_account_number_is_needed(self):
        self.assertEqual(self.collector.run(once=True), 0)
        self.now += timedelta(minutes=1)
        self.assertTrue(self.collector.collect_cycle())
        rows = self.store.read(["005930", "000660"])["quotes"]
        self.assertEqual([row["total_count"] for row in rows], [2, 2])
        self.assertTrue(all(row["price"] == "70000" for row in rows))
        self.factory.assert_called_once()

    def test_one_symbol_failure_preserves_last_quote_and_other_symbol_continues(self):
        self.collector.collect_cycle()
        self.now += timedelta(minutes=1)
        def partial(symbol):
            if symbol == "005930":
                raise KisError("KIS 업무 응답 오류")
            return quote(symbol, price="80000")
        self.client.quote.side_effect = partial
        self.assertFalse(self.collector.collect_cycle())
        first, second = self.store.read(["005930", "000660"])["quotes"]
        self.assertEqual(first["total_count"], 1)
        self.assertEqual(first["price"], "70000")
        self.assertIsNotNone(first["error"])
        self.assertEqual(second["total_count"], 2)
        self.assertIsNone(second["error"])
        self.now += timedelta(minutes=1)
        self.client.quote.side_effect = lambda symbol: quote(symbol)
        self.collector.collect_cycle()
        self.assertIsNone(self.store.read(["005930"])["quotes"][0]["error"])

    def test_bad_symbol_and_numeric_responses_are_errors_without_saving_or_leaking(self):
        self.client.quote.side_effect = [quote("000660"), quote("000660", price="NaN")]
        self.assertFalse(self.collector.collect_cycle())
        rows = self.store.read(["005930", "000660"])["quotes"]
        self.assertTrue(all(row["total_count"] == 0 and row["error"] for row in rows))
        self.now += timedelta(minutes=1)
        self.client.quote.side_effect = RuntimeError("secret-key-and-raw-response")
        self.collector.collect_cycle()
        self.assertNotIn("secret-key", str(self.store.read(["005930"])))

    def test_watchlist_interval_and_credentials_reload_without_mixing_symbols(self):
        self.collector.collect_cycle()
        self.config.write_text(self.base.replace("test-key", "new-key") +
                               "[market_data]\nsymbols=['035420']\ninterval_seconds=30\n", encoding="utf-8")
        self.now += timedelta(minutes=1)
        self.collector.collect_cycle()
        self.assertEqual(self.collector.settings.symbols, ("035420",))
        self.assertEqual(self.collector.settings.interval_seconds, 30)
        self.assertEqual(self.factory.call_count, 2)
        self.assertEqual(self.store.read(["035420"])["quotes"][0]["total_count"], 1)
        self.assertEqual(self.store.read(["005930"])["quotes"][0]["total_count"], 1)

    def test_invalid_config_stops_requests_and_is_reported_without_secrets(self):
        self.config.write_text("app_key='private-invalid", encoding="utf-8")
        self.assertEqual(self.collector.run(once=True), 1)
        self.factory.assert_not_called()
        state = self.store.get_collector(now=self.now.isoformat())
        self.assertEqual(state["state"], "stopped")
        self.assertIsNotNone(state["error"])
        self.assertNotIn("private-invalid", state["error"])

    def test_stop_during_a_response_prevents_saving_and_next_symbol(self):
        def stop(symbol):
            self.collector.stop_event.set()
            return quote(symbol)
        self.client.quote.side_effect = stop
        self.collector.run(once=True)
        self.client.quote.assert_called_once_with("005930")
        self.assertEqual(self.store.read(["005930", "000660"])["total_count"], 0)
        self.assertEqual(self.store.get_collector(now=self.now.isoformat())["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
