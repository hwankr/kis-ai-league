from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from backend.chart import (ChartService, aggregate_minutes, daily_bars, minute_bars,
                           validate_chart_request)
from backend.kis import KST, KisError, PaperClient, Settings


NOW = datetime(2026, 10, 3, 16, tzinfo=KST)


def row(day="20261002", hour=None, **changes):
    result = {"stck_bsop_date": day, "stck_oprc": "100", "stck_hgpr": "110",
              "stck_lwpr": "90", "stck_clpr": "105", "acml_vol": "1000"}
    if hour is not None:
        result.update(stck_cntg_hour=hour, stck_prpr="105", cntg_vol="10")
    result.update(changes)
    return result


def response(*rows):
    return {"output1": {"stck_shrn_iscd": "005930"}, "output2": list(rows)}


class ChartParserTests(unittest.TestCase):
    def test_daily_sorts_deduplicates_preserves_strings_and_marks_open_session(self):
        now = datetime(2026, 10, 2, 12, tzinfo=KST)
        bars = daily_bars(response(row(), row("20261001"), row()), "005930", date(2026, 9, 1), now)
        self.assertEqual([bar["time"] for bar in bars], ["2026-10-01", "2026-10-02"])
        self.assertEqual(bars[1]["volume"], "1000")
        self.assertEqual([bar["partial"] for bar in bars], [False, True])

    def test_daily_invalid_date_ohlc_volume_and_duplicate_conflict_fail_closed(self):
        invalid = [row("20261004"), row("20260230"), row(stck_hgpr="101"),
                   row(stck_oprc="0"), row(stck_clpr="NaN"), row(acml_vol="-1"),
                   row(acml_vol="1.5"), row(acml_vol=None), row(stck_lwpr="secret")]
        for broken in invalid:
            with self.subTest(row=broken), self.assertRaises(KisError) as caught:
                daily_bars(response(broken), "005930", date(2026, 9, 1), NOW)
            self.assertNotIn("secret", str(caught.exception))
        with self.assertRaises(KisError):
            daily_bars(response(row(), row(stck_clpr="106")), "005930", date(2026, 9, 1), NOW)

    def test_raw_response_shape_and_symbol_mismatch_fail(self):
        for payload in (None, {}, {"output2": [None]}, {"output2": [], "output1": []},
                        {"output2": [row()], "output1": {"stck_shrn_iscd": "000660"}}):
            with self.subTest(payload=payload), self.assertRaises(KisError):
                daily_bars(payload, "005930", date(2026, 9, 1), NOW)

    def test_minutes_page_back_to_open_and_exclude_zero_volume_and_outside_session(self):
        client = Mock()
        client.chart_minutes.side_effect = [
            response(row(hour="153000"), row(hour="150000"), row(hour="145900", cntg_vol="0")),
            response(row(hour="145900", cntg_vol="0"), row(hour="090100"),
                     row(hour="090000"), row(hour="085900"))]
        bars = minute_bars(client, "005930", NOW)
        self.assertEqual([bar["time"][11:19] for bar in bars], ["09:00:00", "09:01:00", "15:00:00", "15:30:00"])
        self.assertEqual([call.args[1] for call in client.chart_minutes.call_args_list], ["153000", "145859"])
        self.assertTrue(all(bar["time"].endswith("+09:00") for bar in bars))

    def test_intraday_request_caps_at_current_time_and_ignores_future_rows(self):
        client = Mock()
        client.chart_minutes.return_value = response(row(hour="090400"), row(hour="090300"), row(hour="090000"))
        bars = minute_bars(client, "005930", datetime(2026, 10, 2, 9, 3, 45, tzinfo=KST))
        self.assertEqual(client.chart_minutes.call_args.args[1], "090345")
        self.assertEqual(bars[-1]["time"][11:19], "09:03:00")

    def test_holiday_morning_restarts_previous_session_at_close(self):
        client = Mock()
        client.chart_minutes.side_effect = [response(row(hour="100000"), row(hour="093100")),
                                             response(row(hour="153000"), row(hour="090000"))]
        bars = minute_bars(client, "005930", datetime(2026, 10, 3, 10, tzinfo=KST))
        self.assertEqual([call.args[1] for call in client.chart_minutes.call_args_list], ["100000", "153000"])
        self.assertEqual(bars[-1]["time"], "2026-10-02T15:30:00+09:00")
        client.chart_daily.assert_not_called()

    def test_premarket_empty_page_uses_verified_daily_session_before_requesting_close(self):
        client = Mock()
        client.chart_daily.return_value = response(row("20261001"))
        client.chart_minutes.side_effect = [response(), response(row("20261001", "153000"),
                                                               row("20261001", "090000"))]
        bars = minute_bars(client, "005930", datetime(2026, 10, 2, 7, tzinfo=KST))
        self.assertEqual([call.args[1] for call in client.chart_minutes.call_args_list], ["070000", "153000"])
        self.assertEqual(bars[-1]["time"], "2026-10-01T15:30:00+09:00")
        client.chart_daily.assert_called_once()

    def test_empty_page_does_not_request_future_today_when_daily_is_today(self):
        client = Mock()
        client.chart_minutes.return_value = response()
        client.chart_daily.return_value = response(row())
        self.assertEqual(minute_bars(client, "005930", datetime(2026, 10, 2, 10, tzinfo=KST)), [])
        client.chart_minutes.assert_called_once_with("005930", "100000")

    def test_premarket_fallback_rejects_fabricated_today_rows(self):
        client = Mock()
        client.chart_daily.return_value = response(row("20261001"))
        client.chart_minutes.side_effect = [response(), response(row("20261002", "153000"))]
        with self.assertRaisesRegex(KisError, "거래일이 변경"):
            minute_bars(client, "005930", datetime(2026, 10, 2, 7, tzinfo=KST))

    def test_latest_api_session_only_and_prior_day_ends_pagination(self):
        client = Mock()
        client.chart_minutes.return_value = response(row(hour="091000"), row("20261001", "153000"))
        bars = minute_bars(client, "005930", NOW)
        self.assertEqual(len(bars), 1)
        self.assertTrue(bars[0]["time"].startswith("2026-10-02"))
        self.assertEqual(client.chart_minutes.call_count, 1)

    def test_repeated_page_partial_failure_empty_midway_and_future_date_fail(self):
        for pages in (
            [response(row(hour="150000")), response(row(hour="150000"))],
            [response(row(hour="150000")), KisError("조회 실패")],
            [response(row(hour="150000")), response()],
            [response(row("20261004", "150000"))],
            [response(row(hour="150000")), response(row("20261003", "140000"))],
        ):
            client = Mock()
            client.chart_minutes.side_effect = pages
            with self.subTest(pages=pages), self.assertRaises(KisError):
                minute_bars(client, "005930", NOW)

    def test_pagination_limit_rejects_partial_result(self):
        client = Mock()
        client.chart_minutes.side_effect = [response(row(hour=f"15{minute:02d}00"))
                                            for minute in range(30, 14, -1)]
        with self.assertRaisesRegex(KisError, "한도"):
            minute_bars(client, "005930", NOW)
        self.assertEqual(client.chart_minutes.call_count, 16)

    def test_zero_volume_rows_can_page_without_becoming_bars(self):
        client = Mock()
        client.chart_minutes.return_value = response(row(hour="090100", cntg_vol="0", stck_prpr="0"),
                                                    row(hour="090000", cntg_vol="0"))
        self.assertEqual(minute_bars(client, "005930", NOW), [])

    def test_aggregation_ohlcv_missing_buckets_partial_and_closing_auction(self):
        client = Mock()
        client.chart_minutes.return_value = response(
            row(hour="153000"), row(hour="091700", stck_prpr="101", cntg_vol="7"),
            row(hour="091500", stck_oprc="99", stck_hgpr="120", cntg_vol="3"),
            row(hour="090000"))
        bars = minute_bars(client, "005930", NOW)
        five = aggregate_minutes(bars, "5m", datetime(2026, 10, 2, 9, 18, tzinfo=KST))
        self.assertEqual([bar["time"][11:16] for bar in five], ["09:00", "09:15", "15:30"])
        self.assertEqual({key: five[1][key] for key in ("open", "high", "low", "close", "volume", "partial")},
                         {"open": "99", "high": "120", "low": "90", "close": "101", "volume": "10", "partial": True})
        fifteen = aggregate_minutes(bars, "15m", NOW)
        self.assertTrue(all(not bar["partial"] for bar in fifteen))
        at_close = aggregate_minutes(bars, "15m", datetime(2026, 10, 2, 15, 30, 30, tzinfo=KST))
        self.assertTrue(at_close[-1]["partial"])

    def test_all_invalid_input_fails_before_network(self):
        for symbol, interval in (("5930", "day"), ("００５９３０", "day"), ("005930", "1m"),
                                 ("005930&x=1", "day"), (None, "day"), ("005930", [])):
            with self.subTest(symbol=symbol, interval=interval), self.assertRaises(KisError):
                validate_chart_request(symbol, interval)


class ChartClientTests(unittest.TestCase):
    def test_official_read_only_endpoints_and_adjusted_daily_flag(self):
        client = PaperClient(Settings("test-key", "test-secret"))
        with patch.object(client, "_get", return_value=(response(row()), {})) as get:
            client.chart_daily("005930", date(2026, 4, 6), date(2026, 10, 3))
            args = get.call_args.args
            self.assertEqual(args[:2], ("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100"))
            self.assertEqual(args[2]["FID_ORG_ADJ_PRC"], "0")
            self.assertEqual(args[2]["FID_INPUT_DATE_1"], "20260406")
            client.chart_minutes("005930", "113000")
            args = get.call_args.args
            self.assertEqual(args[:2], ("/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice", "FHKST03010200"))
            self.assertEqual(args[2]["FID_INPUT_HOUR_1"], "113000")
            self.assertEqual(args[2]["FID_PW_DATA_INCU_YN"], "Y")
            self.assertEqual(args[2]["FID_COND_MRKT_DIV_CODE"], "J")


class ChartServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name) / "config.toml"
        self.config.write_text("app_key='test-key'\napp_secret='test-secret'\naccount='12345678'\n", encoding="utf-8")
        self.client = Mock()
        self.client.chart_daily.return_value = response(row())
        self.client.chart_minutes.return_value = response(row(hour="090000"))
        self.factory = Mock(return_value=self.client)
        self.clock = [0]
        self.names = Mock()
        self.names.lookup.return_value = {"005930": "삼성전자"}
        self.service = ChartService(self.config, self.factory, lambda: self.clock[0],
                                    lambda: datetime(2026, 10, 2, 12, tzinfo=KST) + timedelta(seconds=self.clock[0]), self.names)

    def test_contract_ttl_and_returned_copy(self):
        result = self.service.snapshot("005930")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["as_of"], "2026-10-02")
        self.assertEqual(result["updated_at"], "2026-10-02T03:00:00+00:00")
        self.assertEqual(result["name"], "삼성전자")
        self.assertTrue(result["adjusted"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["source"], "KIS")
        result["bars"].clear()
        self.clock[0] = 59
        self.assertEqual(len(self.service.snapshot("005930")["bars"]), 1)
        self.client.chart_daily.assert_called_once()
        self.clock[0] = 60
        self.service.snapshot("005930")
        self.assertEqual(self.client.chart_daily.call_count, 2)

    def test_intraday_has_separate_cache_and_thirty_second_ttl(self):
        self.service.snapshot("005930")
        self.assertFalse(self.service.snapshot("005930", "5m")["adjusted"])
        self.service.snapshot("005930", "5m")
        fifteen = self.service.snapshot("005930", "15m")
        self.assertEqual(fifteen["interval"], "15m")
        self.assertEqual(self.client.chart_minutes.call_count, 1)
        self.clock[0] = 30
        self.service.snapshot("005930", "5m")
        self.assertEqual(self.client.chart_minutes.call_count, 2)

    def test_failure_preserves_previous_bars_timestamp_and_sanitizes_exception(self):
        first = self.service.snapshot("005930")
        self.clock[0] = 60
        self.client.chart_daily.side_effect = RuntimeError("private-secret")
        result = self.service.snapshot("005930")
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["stale"])
        self.assertEqual(result["bars"], first["bars"])
        self.assertEqual(result["updated_at"], first["updated_at"])
        self.assertNotIn("private-secret", json.dumps(result))
        self.clock[0] = 70
        self.client.chart_daily.side_effect = None
        self.assertFalse(self.service.snapshot("005930")["stale"])

    def test_first_failure_or_empty_data_never_invents_bars(self):
        self.client.chart_daily.return_value = response()
        result = self.service.snapshot("005930")
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["stale"])
        self.assertIsNone(result["updated_at"])
        self.assertEqual(result["bars"], [])

    def test_config_change_never_leaks_previous_accounts_cache(self):
        self.service.snapshot("005930")
        self.config.write_text("app_key='changed-key'\napp_secret='changed-secret'\naccount='12345678'\n", encoding="utf-8")
        self.client.chart_daily.side_effect = RuntimeError("changed-secret")
        result = self.service.snapshot("005930")
        self.assertFalse(result["stale"])
        self.assertEqual(result["bars"], [])
        self.assertEqual(self.factory.call_count, 2)
        self.assertNotIn("changed-secret", json.dumps(result))

    def test_market_account_overrides_default_and_name_error_is_nonfatal(self):
        self.config.write_text("default_account='paper'\n[accounts.paper]\napp_key='paper-key'\napp_secret='p'\n"
                               "[accounts.competition]\napp_key='competition-key'\napp_secret='c'\n"
                               "[market_data]\naccount='competition'\n", encoding="utf-8")
        self.names.lookup.side_effect = OSError("secret")
        result = self.service.snapshot("005930")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.factory.call_args.args[0].id, "competition")
        self.assertNotIn("name", result)

    def test_bad_input_or_config_never_reaches_client(self):
        self.assertEqual(self.service.snapshot("oops")["status"], "error")
        self.assertEqual(self.service.snapshot("005930", "bad")["status"], "error")
        self.config.unlink()
        self.assertEqual(self.service.snapshot("005930")["status"], "error")
        self.factory.assert_not_called()

    def test_simultaneous_identical_requests_share_one_fetch(self):
        entered, release = threading.Event(), threading.Event()
        def read(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            return response(row())
        self.client.chart_daily.side_effect = read
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.service.snapshot, "005930")
            self.assertTrue(entered.wait(3))
            second = pool.submit(self.service.snapshot, "005930")
            release.set()
            self.assertEqual(first.result(), second.result())
        self.client.chart_daily.assert_called_once()

    def test_lru_cache_is_bounded_even_for_failed_symbols(self):
        for number in range(33):
            self.service.snapshot(f"{number:06d}")
        self.assertEqual(len(self.service.cache), 32)
        self.assertEqual(self.factory.call_count, 33)
        self.service.snapshot("000001")
        self.assertEqual(self.factory.call_count, 33)
        self.service.snapshot("000000")
        self.assertEqual(self.factory.call_count, 34)


if __name__ == "__main__":
    unittest.main()
