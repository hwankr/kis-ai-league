import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from backend.kis import AccountProfile, KisError, PaperClient, Settings, client_for_profile, load_profiles, main


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

    def test_legacy_profile_can_coexist_with_a_new_competition_account(self):
        path = self.root / "config.toml"
        path.write_text("app_key = 'sample-key'\napp_secret = 'sample-secret'\n"
                        "account = '01234567'\nproduct_code = '01'\n"
                        "[accounts.competition]\nname = 'AI 리그 대회'\n"
                        "app_key = 'competition-key'\napp_secret = 'competition-secret'\n"
                        "account = '87654321'\nproduct_code = '02'", encoding="utf-8")
        catalog = load_profiles(path)
        self.assertEqual(catalog.default_id, "paper")
        self.assertEqual(catalog.select().name, "일반 모의투자")
        self.assertTrue(catalog.select().configured)
        self.assertEqual(Settings.load(path).account, "01234567")
        self.assertEqual(Settings.load(path, "competition").account, "87654321")
        self.assertEqual(catalog.select("competition").public_metadata(), {
            "id": "competition", "name": "AI 리그 대회", "configured": True,
        })
        self.assertNotIn("competition-secret", repr(catalog))
        self.assertNotIn("competition-secret", repr(catalog.select("competition")))
        with self.assertRaises(KisError):
            catalog.select("missing")

    def test_explicit_default_and_first_profile_fallback(self):
        path = self.root / "config.toml"
        profiles = ("[accounts.competition]\napp_key = 'competition-key'\n"
                    "app_secret = 'competition-secret'\naccount = '87654321'\n"
                    "[accounts.paper]\napp_key = 'sample-key'\napp_secret = 'sample-secret'\n"
                    "account = '12345678'\n")
        path.write_text("default_account = 'competition'\n" + profiles, encoding="utf-8")
        self.assertEqual(Settings.load(path).app_key, "competition-key")
        path.write_text(profiles, encoding="utf-8")
        self.assertEqual(load_profiles(path).default_id, "paper")
        path.write_text("[accounts.competition]\n", encoding="utf-8")
        self.assertEqual(load_profiles(path).default_id, "competition")

    def test_incomplete_profile_does_not_block_a_configured_account(self):
        path = self.root / "config.toml"
        base = ("[accounts.paper]\napp_key = 'sample-key'\napp_secret = 'sample-secret'\n"
                "account = '12345678'\n[accounts.competition]\n")
        for unfinished in ("", "app_key = ' '\n", "account = '123'\n",
                           "app_key = 'key'\napp_secret = 'secret'\naccount = ''\n",
                           "app_key = 'key'\napp_secret = 'secret'\naccount = '12345678'\n"
                           "product_code = 'bad'\n"):
            with self.subTest(unfinished=unfinished):
                path.write_text(base + unfinished, encoding="utf-8")
                catalog = load_profiles(path)
                self.assertTrue(catalog.select("paper").configured)
                self.assertFalse(catalog.select("competition").configured)
                self.assertEqual(Settings.load(path, "paper").account, "12345678")

    def test_auth_and_quote_settings_do_not_require_an_account_number(self):
        path = self.root / "config.toml"
        path.write_text("[accounts.paper]\napp_key = 'key'\napp_secret = 'secret'\n",
                        encoding="utf-8")
        self.assertEqual(Settings.load(path).account, "")
        self.assertFalse(load_profiles(path).select().configured)
        path.write_text("[accounts.paper]\napp_key = ''\napp_secret = 'secret'\n",
                        encoding="utf-8")
        with self.assertRaises(KisError):
            Settings.load(path)

    def test_profile_structure_errors_are_safe(self):
        path = self.root / "config.toml"
        invalid = (
            "accounts = 'sample-secret'", "accounts = []", "[accounts]", "accounts = {paper = 2}",
            "[accounts.'../sample-secret']", "[accounts.Paper]", "[accounts.'']",
            "[accounts.'" + "a" * 33 + "']",
            "[accounts.paper]\nname = ''", "[accounts.paper]\nname = '   '",
            "[accounts.paper]\nname = 4", "[accounts.paper]\nname = '" + "a" * 41 + "'",
            '[accounts.paper]\nname = "sample-secret\\n"',
            "[accounts.paper]\napp_secret = 1234", "[accounts.paper]\naccount = []",
            "app_key = 'sample-secret'\n[accounts.paper]",
            "default_account = 'sample-secret'\n[accounts.paper]",
            "default_account = []\n[accounts.paper]",
        )
        for body in invalid:
            with self.subTest(body=body):
                path.write_text(body, encoding="utf-8")
                with self.assertRaises(KisError) as caught:
                    load_profiles(path)
                self.assertNotIn("sample-secret", str(caught.exception))

    def test_profile_token_paths_are_isolated_by_credentials(self):
        paper = AccountProfile("paper", "일반", self.settings)
        competition = AccountProfile("competition", "대회", Settings("other-key", "other-secret", "87654321"))
        first = client_for_profile(paper, self.root)
        second = client_for_profile(competition, self.root)
        self.assertNotEqual(first.cache_path, second.cache_path)
        self.assertNotIn("sample-key", str(first.cache_path))
        shared_key = AccountProfile("shared", "공유 키", Settings("sample-key", "sample-secret", "87654321"))
        self.assertEqual(first.cache_path, client_for_profile(shared_key, self.root).cache_path)
        with patch.object(PaperClient, "_request", side_effect=[
            ({"access_token": "paper-token", "expires_in": 86400}, {}),
            ({"access_token": "competition-token", "expires_in": 86400}, {}),
        ]) as request:
            self.assertEqual(first.token(), "paper-token")
            self.assertEqual(second.token(), "competition-token")
            self.assertEqual(first.token(), "paper-token")
            self.assertEqual(second.token(), "competition-token")
            self.assertEqual(request.call_count, 2)

    def test_matching_legacy_token_is_reused_without_changing_the_cache(self):
        legacy_path = self.root / "kis-token.json"
        legacy_client = PaperClient(self.settings, legacy_path)
        with patch.object(legacy_client, "_request", return_value=(
                {"access_token": "legacy-token", "expires_in": 86400}, {})):
            legacy_client.token()
        before = legacy_path.read_bytes()
        profile_client = client_for_profile(AccountProfile("paper", "일반", self.settings), self.root)
        with patch.object(profile_client, "_request") as request:
            self.assertEqual(profile_client.token(), "legacy-token")
            request.assert_not_called()
        self.assertEqual(legacy_path.read_bytes(), before)
        self.assertFalse(profile_client.cache_path.exists())
        other = client_for_profile(AccountProfile("other", "대회", Settings("other-key", "other-secret")), self.root)
        with patch.object(other, "_request", return_value=(
                {"access_token": "new-token", "expires_in": 86400}, {})) as request:
            self.assertEqual(other.token(), "new-token")
            request.assert_called_once()
        self.assertEqual(legacy_path.read_bytes(), before)

    def test_cli_global_account_option_selects_profile(self):
        path = self.root / "config.local.toml"
        path.write_text("[accounts.paper]\napp_key = 'key'\napp_secret = 'secret'\n"
                        "[accounts.competition]\napp_key = 'other-key'\napp_secret = 'other-secret'\n"
                        "account = '87654321'\n", encoding="utf-8")
        output = io.StringIO()
        with patch("backend.kis.ROOT", self.root), patch("sys.stdout", output):
            self.assertEqual(main(["--account", "competition", "check"]), 0)
        self.assertTrue(json.loads(output.getvalue())["account_configured"])
        with patch("backend.kis.ROOT", self.root), patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["--account", "unknown", "check"]), 1)

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
                "stck_prpr": "75000", "prdy_ctrt": "1.5", "acml_vol": "1234",
                "acml_tr_pbmn": "92550000", "private": "hidden"
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
              "output2": [{"tot_evlu_amt": "99900000", "pchs_amt_smtl_amt": "50000",
                           "evlu_pfls_smtl_amt": "-100"}],
              "ctx_area_fk100": "first ", "ctx_area_nk100": "next "}, {"tr_cont": "F"}),
            ({"output1": [{"pdno": "000660", "hldg_qty": "2"}],
              "output2": [{"tot_evlu_amt": "100000000", "pchs_amt_smtl_amt": "100000",
                           "evlu_pfls_smtl_amt": "1500"}]}, {"tr_cont": "D"}),
        ]
        calls = []

        def get(path, tr_id, params, continuation):
            calls.append((tr_id, dict(params), continuation))
            return pages[len(calls) - 1]

        with patch.object(self.client, "_get", side_effect=get):
            result = self.client.balance()
        self.assertEqual(len(result["holdings"]), 2)
        self.assertEqual(result["summary"]["tot_evlu_amt"], "100000000")
        self.assertEqual(result["summary"]["pchs_amt_smtl_amt"], "100000")
        self.assertEqual(result["summary"]["evlu_pfls_smtl_amt"], "1500")
        self.assertEqual(calls[1][2], "N")
        self.assertEqual(calls[1][1]["CTX_AREA_NK100"], "next")
        self.assertEqual(calls[0][0], "VTTC8434R")

    def test_balance_preserves_dashboard_fields_and_excludes_private_fields(self):
        holding = {
            "pdno": "005930", "prdt_name": "삼성전자", "hldg_qty": "2",
            "pchs_avg_pric": "75000.0000", "pchs_amt": "150000", "prpr": "73500",
            "evlu_amt": "147000", "evlu_pfls_amt": "-3000", "evlu_pfls_rt": "-2.00",
        }
        summary = {
            "dnca_tot_amt": "9850000", "scts_evlu_amt": "147000", "tot_evlu_amt": "9997000",
            "pchs_amt_smtl_amt": "150000", "evlu_pfls_smtl_amt": "-3000",
        }
        private = {"cano": "12345678", "app_secret": "sample-secret", "private": "hidden"}
        response = {"output1": [{**holding, **private}], "output2": [{**summary, **private}]}
        with patch.object(self.client, "_get", return_value=(response, {})):
            result = self.client.balance()
        self.assertEqual(result, {"environment": "paper", "holdings": [holding], "summary": summary})
        serialized = json.dumps(result)
        for value in private.values():
            self.assertNotIn(value, serialized)

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
