from contextlib import contextmanager, nullcontext
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from backend.kis import AccountProfile, ERROR_BODY_LIMIT, KisError, KisRequestError, PaperClient, Settings
from backend.paper_broker import BrokerUnknown, PaperBroker


QUOTE = "/uapi/domestic-stock/v1/quotations/inquire-price"


class Response(io.BytesIO):
    headers = {}


def http_error(status=500, payload=None):
    body = json.dumps(payload or {"msg_cd": "EGW00201", "msg1": "private-secret"}).encode()
    return HTTPError("https://example.invalid/private-account", status, "private-secret",
                     {"authorization": "private-token"}, io.BytesIO(body))


class KisReadRetryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.client = PaperClient(Settings("private-key", "private-secret", "12345678", "01"),
                                  Path(directory.name) / "token.json")
        spacing = patch("backend.kis.request_spacing", side_effect=lambda path: nullcontext())
        spacing.start()
        self.addCleanup(spacing.stop)
        logger = patch("backend.kis.LOGGER")
        self.logger = logger.start()
        self.addCleanup(logger.stop)

    def get(self):
        return self.client._get(QUOTE, "FHKST01010100", {"private-parameter": "private-account"})

    def assert_safe(self, error):
        rendered = str(error) + repr(vars(error))
        for value in ("private-key", "private-secret", "private-account", "private-token",
                      "private-parameter", "12345678", "example.invalid", "authorization", "msg1"):
            self.assertNotIn(value, rendered)

    def test_transient_http_read_retries_once_then_succeeds_with_one_authentication(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status), patch.object(self.client, "token", return_value="private-token") as token, \
                    patch("backend.kis.urlopen", side_effect=[http_error(status), Response(b'{"rt_cd":"0"}')]) as send:
                self.assertEqual(self.get()[0], {"rt_cd": "0"})
                self.assertEqual(send.call_count, 2)
                token.assert_called_once()
                self.assertEqual([call.args[0].get_method() for call in send.call_args_list], ["GET", "GET"])

    def test_final_failure_retains_safe_last_attempt_diagnostics_and_stops(self):
        with patch.object(self.client, "token", return_value="private-token"), \
                patch("backend.kis.urlopen", side_effect=[http_error(), http_error(503, {"msg_cd": "EGW00123"})]) as send:
            with self.assertRaises(KisRequestError) as caught:
                self.get()
        error = caught.exception
        self.assertEqual(send.call_count, 2)
        self.assertEqual((error.request_kind, error.http_status, error.message_code, error.attempts),
                         ("quote", 503, "EGW00123", 2))
        self.assertEqual(datetime.fromisoformat(error.occurred_at).utcoffset().total_seconds(), 0)
        for value in ("quote", "HTTP 503", "EGW00123", error.occurred_at, "2회 시도"):
            self.assertIn(value, str(error))
        self.assert_safe(error)

    def test_nontransient_http_read_does_not_retry(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status), patch.object(self.client, "token", return_value="private-token"), \
                    patch("backend.kis.urlopen", side_effect=http_error(status, {"msg_cd": "EGW00123"})) as send:
                with self.assertRaises(KisRequestError):
                    self.get()
                send.assert_called_once()

    def test_recovery_log_preserves_only_safe_first_failure_and_retry_outcome(self):
        with patch.object(self.client, "token", return_value="private-token"), \
                patch("backend.kis.urlopen", side_effect=[http_error(), Response(b'{"rt_cd":"0"}')]):
            self.get()
        logs = [call.args[0] % call.args[1:] for call in self.logger.warning.call_args_list]
        self.assertEqual(len(logs), 2)
        self.assertIn("EGW00201", logs[0])
        self.assertIn("HTTP 500", logs[0])
        self.assertIn("quote", logs[0])
        self.assertIn("복구 2/2", logs[1])
        self.assertNotIn("private-", " ".join(logs))
        self.logger.reset_mock()
        with patch.object(self.client, "token", return_value="private-token"), \
                patch("backend.kis.urlopen", return_value=Response(b'{"rt_cd":"0"}')):
            self.get()
        self.logger.warning.assert_not_called()

    def test_retry_failure_is_logged_without_original_exception_or_traceback(self):
        for outcome in (http_error(503), Response(b'{"msg1":"private-secret"}')):
            self.logger.reset_mock()
            with patch.object(self.client, "token", return_value="private-token"), \
                    patch("backend.kis.urlopen", side_effect=[http_error(), outcome]):
                with self.assertRaises(KisError):
                    self.get()
            logs = [call.args[0] % call.args[1:] for call in self.logger.warning.call_args_list]
            self.assertEqual(len(logs), 2)
            self.assertIn("실패 2/2", logs[1])
            self.assertNotIn("private-", " ".join(logs))
            for call in self.logger.warning.call_args_list:
                self.assertEqual(call.kwargs, {})
                self.assertTrue(all(isinstance(value, str) for value in call.args))

    def test_only_allowlisted_business_error_is_retried(self):
        for code, expected in (("EGW00201", 2), ("EGW00123", 1)):
            responses = [Response(json.dumps({"rt_cd": "1", "msg_cd": code, "msg1": "private-secret"}).encode()),
                         Response(b'{"rt_cd":"0"}')]
            with self.subTest(code=code), patch.object(self.client, "token", return_value="private-token"), \
                    patch("backend.kis.urlopen", side_effect=responses) as send:
                if expected == 2:
                    self.assertEqual(self.get()[0], {"rt_cd": "0"})
                else:
                    with self.assertRaises(KisRequestError) as caught:
                        self.get()
                    self.assert_safe(caught.exception)
                self.assertEqual(send.call_count, expected)

    def test_retry_releases_and_reenters_outer_guard_and_shared_gate(self):
        events = []

        @contextmanager
        def guard(name):
            events.append(name + "+")
            try:
                yield
            finally:
                events.append(name + "-")

        self.client.request_guard = lambda: guard("outer")
        with patch.object(self.client, "token", return_value="private-token"), \
                patch("backend.kis.request_spacing", side_effect=lambda path: guard("gate")), \
                patch("backend.kis.urlopen", side_effect=[http_error(), Response(b'{"rt_cd":"0"}')]):
            self.get()
        self.assertEqual(events, ["outer+", "gate+", "gate-", "outer-"] * 2)

    def test_token_http_failure_is_not_retried_by_read(self):
        with patch("backend.kis.urlopen", side_effect=http_error()) as send:
            with self.assertRaises(KisRequestError) as caught:
                self.get()
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0].get_method(), "POST")
        self.assertEqual(caught.exception.request_kind, "token")
        self.assert_safe(caught.exception)

    def test_post_http_failure_with_business_code_stays_unknown_without_retry(self):
        broker = PaperBroker(AccountProfile("paper", "test", self.client.settings), client=self.client)
        for path in ("/uapi/domestic-stock/v1/trading/order-cash", "/uapi/domestic-stock/v1/trading/order-rvsecncl"):
            with self.subTest(path=path), patch.object(self.client, "token", return_value="private-token"), \
                    patch("backend.kis.urlopen", side_effect=http_error(500, {"msg_cd": "EGW00123"})) as send:
                with self.assertRaises(BrokerUnknown) as caught:
                    broker._mutate(path, "VTTC0012U", {"CANO": "12345678"})
                send.assert_called_once()
                self.assertEqual(send.call_args.args[0].get_method(), "POST")
                self.assert_safe(caught.exception)

    def test_post_business_error_preserves_exact_mutation_classification_contract(self):
        with patch("backend.kis.urlopen", return_value=Response(b'{"rt_cd":"1","msg_cd":"EGW00123"}')) as send:
            with self.assertRaises(KisError) as caught:
                self.client._request("/uapi/domestic-stock/v1/trading/order-cash", body={"CANO": "12345678"})
        self.assertEqual(str(caught.exception), "KIS 업무 응답 오류 (EGW00123)")
        send.assert_called_once()

    def test_error_body_read_is_bounded_and_oversize_body_is_discarded(self):
        class BoundedBody(io.BytesIO):
            requested = []

            def read(self, size=-1):
                self.requested.append(size)
                return super().read(size)

        body = BoundedBody(b'{"msg_cd":"EGW00201","msg1":"' + b'x' * (ERROR_BODY_LIMIT * 2) + b'"}')
        error = HTTPError("https://example.invalid/private-account", 500, "private-secret", {}, body)
        with patch("backend.kis.urlopen", side_effect=error):
            with self.assertRaises(KisRequestError) as caught:
                self.client._request(QUOTE)
        self.assertEqual(body.requested, [ERROR_BODY_LIMIT + 1])
        self.assertTrue(body.closed)
        self.assertIsNone(caught.exception.message_code)
        self.assert_safe(caught.exception)

    def test_malformed_body_or_code_and_unknown_path_never_expose_private_values(self):
        for payload in (b'private-secret', b'[]', b'{"msg_cd":"EGW00201 private-secret"}',
                        b'{"msg_cd":{"private-secret":1}}', b'[' * 2000):
            error = HTTPError("https://example.invalid", 500, "private-secret", {}, io.BytesIO(payload))
            with self.subTest(payload=payload[:25]), patch("backend.kis.urlopen", side_effect=error):
                with self.assertRaises(KisRequestError) as caught:
                    self.client._request("/private-account")
            self.assertEqual(caught.exception.request_kind, "unknown")
            self.assertIsNone(caught.exception.message_code)
            self.assert_safe(caught.exception)


if __name__ == "__main__":
    unittest.main()
