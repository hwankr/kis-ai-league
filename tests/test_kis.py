import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from backend.kis import KisError, PaperClient, Settings


class KisTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = Settings("sample-key", "sample-secret", "12345678", "01")
        self.client = PaperClient(self.settings, self.root / "token.json")

    def test_config_errors_hide_contents_and_preserve_leading_zeroes(self):
        path = self.root / "config.toml"
        path.write_text("app_key = 'sample-key'\napp_secret = 'sample-secret'\n"
                        "account = '01234567'\nproduct_code = '01'", encoding="utf-8")
        settings = Settings.load(path)
        settings.validate_account()
        self.assertEqual(settings.account, "01234567")
        self.assertNotIn("sample-secret", repr(settings))
        path.write_text("app_secret = 'sample-secret", encoding="utf-8")
        with self.assertRaises(KisError) as caught:
            Settings.load(path)
        self.assertNotIn("sample-secret", str(caught.exception))

    def test_missing_account_fails_before_network(self):
        self.client.settings.account = ""
        with patch.object(self.client, "_request") as request:
            with self.assertRaises(KisError):
                self.client.balance()
            request.assert_not_called()

    def test_invalid_symbol_fails_before_auth(self):
        with patch.object(self.client, "token") as token:
            with self.assertRaises(KisError):
                self.client.quote("005930&other=value")
            token.assert_not_called()

    def test_token_reused_across_clients_and_refreshed_for_changed_credentials(self):
        with patch.object(PaperClient, "_request", return_value=(
                {"access_token": "test-token", "expires_in": 86400}, {})) as request:
            self.assertEqual(self.client.token(), "test-token")
            another = PaperClient(self.settings, self.client.cache_path)
            self.assertEqual(another.token(), "test-token")
            self.assertEqual(request.call_count, 1)
            another.settings = Settings("other-key", "other-secret")
            another.token()
            self.assertEqual(request.call_count, 2)

    def test_expired_cache_refreshes(self):
        with patch.object(self.client, "_request", return_value=(
                {"access_token": "first", "expires_in": 86400}, {})):
            self.client.token()
        saved = json.loads(self.client.cache_path.read_text())
        saved["expires_at"] = 0
        self.client.cache_path.write_text(json.dumps(saved))
        with patch.object(self.client, "_request", return_value=(
                {"access_token": "second", "expires_in": 86400}, {})) as request:
            self.assertEqual(self.client.token(), "second")
            request.assert_called_once()

    def test_absolute_expiration_prevents_caching_expired_token(self):
        with patch.object(self.client, "_request", return_value=(
                {"access_token": "old-token", "expires_in": 86400,
                 "access_token_token_expired": "2000-01-01 00:00:00"}, {})):
            with self.assertRaises(KisError):
                self.client.token()
        self.assertFalse(self.client.cache_path.exists())

    def test_quote_uses_paper_endpoint_and_omits_unneeded_response_fields(self):
        captured = []

        class Response(io.BytesIO):
            headers = {"Content-Type": "application/json"}

        def open_request(request, timeout):
            captured.append(request)
            return Response(json.dumps({"rt_cd": "0", "output": {
                "stck_prpr": "75000", "prdy_ctrt": "1.5", "private": "hidden"
            }}).encode())

        with patch.object(self.client, "token", return_value="test-token"), \
                patch("backend.kis.urlopen", side_effect=open_request):
            result = self.client.quote("005930")
        self.assertEqual(result["price"], "75000")
        self.assertNotIn("private", result)
        self.assertTrue(captured[0].full_url.startswith(
            "https://openapivts.koreainvestment.com:29443/"))
        self.assertEqual(captured[0].get_header("Tr_id"), "FHKST01010100")
        self.assertIn("FID_INPUT_ISCD=005930", captured[0].full_url)

    def test_balance_joins_pages_without_repeating_summary(self):
        pages = [
            ({"output1": [{"pdno": "005930", "hldg_qty": "1"}],
              "output2": [{"tot_evlu_amt": "100000000"}],
              "ctx_area_fk100": "first ", "ctx_area_nk100": "next "}, {"tr_cont": "F"}),
            ({"output1": [{"pdno": "000660", "hldg_qty": "2"}],
              "output2": [{"tot_evlu_amt": "100000000"}]}, {"tr_cont": "D"}),
        ]
        calls = []

        def get(path, tr_id, params, continuation):
            calls.append((tr_id, dict(params), continuation))
            return pages[len(calls) - 1]

        with patch.object(self.client, "_get", side_effect=get):
            result = self.client.balance()
        self.assertEqual(len(result["holdings"]), 2)
        self.assertEqual(result["summary"]["tot_evlu_amt"], "100000000")
        self.assertEqual(calls[1][2], "N")
        self.assertEqual(calls[1][1]["CTX_AREA_NK100"], "next")
        self.assertEqual(calls[0][0], "VTTC8434R")

    def test_repeated_cursor_fails_instead_of_returning_partial_balance(self):
        page = ({"output1": [], "output2": [{}], "ctx_area_fk100": "same",
                 "ctx_area_nk100": "same"}, {"tr_cont": "M"})
        with patch.object(self.client, "_get", return_value=page):
            with self.assertRaisesRegex(KisError, "연속조회"):
                self.client.balance()

    def test_http_error_does_not_expose_response_body_or_credentials(self):
        error = HTTPError("https://example.invalid", 403, "sample-secret", {},
                          io.BytesIO(b'sample-secret'))
        with patch("backend.kis.urlopen", side_effect=error):
            with self.assertRaises(KisError) as caught:
                self.client._request("/oauth2/tokenP", body={"appsecret": "sample-secret"})
        self.assertNotIn("sample-secret", str(caught.exception))
        self.assertIn("403", str(caught.exception))

    def test_business_error_is_not_treated_as_success(self):
        class Response(io.BytesIO):
            headers = {}

        response = Response(b'{"rt_cd":"1","msg_cd":"EGW00123","msg1":"sample-secret"}')
        with patch("backend.kis.urlopen", return_value=response):
            with self.assertRaises(KisError) as caught:
                self.client._request("/example")
        self.assertIn("EGW00123", str(caught.exception))
        self.assertNotIn("sample-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
