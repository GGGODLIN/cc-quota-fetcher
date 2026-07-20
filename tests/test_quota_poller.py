import base64
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

NATIVE_HOST = Path(__file__).resolve().parents[1] / "native-host"
sys.path.insert(0, str(NATIVE_HOST))

import quota_poller


def make_jwt(expiration):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode({'exp': expiration})}.x"


def auth_payload(access_token, refresh_token, account_id="synthetic-account"):
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": make_jwt(4_000_000_000),
            "account_id": account_id,
            "preserved_token_field": "keep"
        },
        "last_refresh": "2026-01-01T00:00:00.000Z",
        "preserved_root_field": {"keep": True}
    }


class AtomicFileTests(unittest.TestCase):
    def test_atomic_write_json_replaces_content_with_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_text("old")
            os.chmod(path, 0o644)

            quota_poller.atomic_write_json(path, {"value": 1})

            self.assertEqual(json.loads(path.read_text()), {"value": 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_nonblocking_lock_skips_second_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "poller.lock"
            first = quota_poller.acquire_lock(path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(quota_poller.acquire_lock(path))
            finally:
                first.close()


class HttpTests(unittest.TestCase):
    def test_http_json_does_not_forward_credentials_across_redirects(self):
        requests = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(dict(self.headers.items()))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, format, *args):
                return

        target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/target")
                self.end_headers()

            def log_message(self, format, *args):
                return

        source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        source_thread = threading.Thread(target=source.serve_forever, daemon=True)
        source_thread.start()
        try:
            result = quota_poller.http_json(
                f"http://127.0.0.1:{source.server_port}/source",
                method="GET",
                headers={
                    "Authorization": "Bearer synthetic-secret",
                    "ChatGPT-Account-Id": "synthetic-account"
                },
                body=None,
                timeout=2
            )
        finally:
            source.shutdown()
            target.shutdown()
            source.server_close()
            target.server_close()
            source_thread.join()
            target_thread.join()

        self.assertEqual(result.status, 302)
        self.assertEqual(requests, [])

    def test_http_error_preserves_retry_headers_without_reading_body(self):
        class SlowHttpError(urllib.error.HTTPError):
            def read(self, *args, **kwargs):
                raise TimeoutError("synthetic timeout")

            def close(self):
                return

        error = SlowHttpError(
            "https://example.invalid",
            429,
            "rate limited",
            {"Retry-After": "60"},
            None
        )

        class Opener:
            def open(self, request, timeout):
                raise error

        with mock.patch.object(
            quota_poller.urllib.request,
            "build_opener",
            return_value=Opener()
        ):
            result = quota_poller.http_json(
                "https://example.invalid",
                method="GET",
                headers={"Authorization": "Bearer synthetic-secret"},
                body=None,
                timeout=2
            )

        self.assertEqual(result.status, 429)
        self.assertEqual(result.headers["Retry-After"], "60")
        self.assertIsNone(result.body)


class RetryAfterTests(unittest.TestCase):
    def test_retry_after_accepts_seconds_and_http_date(self):
        now = 1_700_000_000
        date_value = datetime.fromtimestamp(now + 90, timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

        self.assertEqual(quota_poller.parse_retry_after("45", now), 45)
        self.assertEqual(quota_poller.parse_retry_after(date_value, now), 90)
        self.assertEqual(quota_poller.parse_retry_after("invalid", now), 300)
        self.assertEqual(quota_poller.parse_retry_after(None, now), 300)

    def test_backoff_state_is_persistent_and_vendor_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            quota_poller.set_not_before(path, "codex", 200)
            quota_poller.set_not_before(path, "deepseek", 300)

            state = json.loads(path.read_text())
            self.assertEqual(state["vendors"]["codex"]["not_before"], 200)
            self.assertEqual(state["vendors"]["deepseek"]["not_before"], 300)
            self.assertTrue(quota_poller.is_backed_off(state, "codex", 199))
            self.assertFalse(quota_poller.is_backed_off(state, "codex", 200))


class NormalizationTests(unittest.TestCase):
    def test_codex_windows_are_classified_by_duration_when_order_is_reversed(self):
        data = quota_poller.normalize_codex_usage({
            "plan_type": "synthetic-plan",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 35,
                    "reset_at": 1_700_172_800,
                    "limit_window_seconds": 604_800
                },
                "secondary_window": {
                    "used_percent": 8,
                    "reset_at": 1_700_005_400,
                    "limit_window_seconds": 18_000
                }
            },
            "additional_rate_limits": [{
                "limit_name": "synthetic-limit",
                "metered_feature": "synthetic-feature",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 41,
                        "reset_at": None,
                        "limit_window_seconds": 18_000
                    }
                }
            }],
            "credits": {
                "has_credits": True,
                "unlimited": False,
                "overage_limit_reached": False,
                "balance": "12.5"
            }
        })

        self.assertEqual(data["session"]["used_percent"], 8)
        self.assertEqual(data["weekly"]["used_percent"], 35)
        self.assertEqual(data["weekly"]["remaining_percent"], 65)
        self.assertIsNone(data["additional_rate_limits"][0]["rate_limit"]["primary_window"]["reset_at"])
        self.assertEqual(data["credits"]["balance"], "12.5")
        self.assertNotIn("primary", data)

    def test_empty_credits_do_not_make_missing_windows_valid(self):
        with self.assertRaisesRegex(quota_poller.PollError, "invalid-schema"):
            quota_poller.normalize_codex_usage({"credits": {}})

    def test_deepseek_normalization_keeps_balance_infos_contract_only(self):
        data = quota_poller.normalize_deepseek_balance({
            "is_available": True,
            "balance_infos": [
                {"currency": "USD", "total_balance": "1.25", "granted_balance": "ignored"},
                {"currency": "CNY", "total_balance": 2}
            ],
            "unrelated": "ignored"
        })

        self.assertEqual(data, {
            "balance_infos": [
                {"currency": "USD", "total_balance": "1.25"},
                {"currency": "CNY", "total_balance": 2}
            ]
        })


class CodexAuthTests(unittest.TestCase):
    def usage_body(self):
        return {
            "plan_type": "synthetic-plan",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 10,
                    "reset_at": 1_700_005_400,
                    "limit_window_seconds": 18_000
                },
                "secondary_window": {
                    "used_percent": 20,
                    "reset_at": 1_700_172_800,
                    "limit_window_seconds": 604_800
                }
            }
        }

    def test_success_keeps_near_expiry_auth_file_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            original = json.dumps(
                auth_payload(make_jwt(1_700_000_200), "refresh-before"),
                indent=2
            ).encode()
            path.write_bytes(original)
            methods = []

            def transport(url, *, method, headers, body, timeout):
                methods.append(method)
                if method == "POST":
                    return quota_poller.HttpResult(200, {}, {
                        "access_token": make_jwt(1_700_010_000),
                        "refresh_token": "refresh-after"
                    })
                return quota_poller.HttpResult(200, {}, self.usage_body())

            data = quota_poller.poll_codex(path, 1_700_000_000, transport)

            self.assertEqual(data["weekly"]["remaining_percent"], 80)
            self.assertEqual(methods, ["GET"])
            self.assertEqual(path.read_bytes(), original)

    def test_401_returns_fixed_status_without_refresh_or_auth_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            original = json.dumps(auth_payload(make_jwt(1_700_010_000), "refresh-before")).encode()
            path.write_bytes(original)
            methods = []

            def transport(url, *, method, headers, body, timeout):
                methods.append(method)
                if method == "POST":
                    return quota_poller.HttpResult(200, {}, {
                        "access_token": make_jwt(1_700_020_000),
                        "refresh_token": "refresh-after"
                    })
                return quota_poller.HttpResult(401, {}, None)

            with self.assertRaisesRegex(quota_poller.PollError, "unauthorized"):
                quota_poller.poll_codex(path, 1_700_000_000, transport)

            self.assertEqual(methods, ["GET"])
            self.assertEqual(path.read_bytes(), original)

    def test_403_returns_fixed_status_without_auth_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            original = json.dumps(auth_payload(make_jwt(1_700_010_000), "refresh-before")).encode()
            path.write_bytes(original)

            def transport(url, *, method, headers, body, timeout):
                return quota_poller.HttpResult(403, {}, None)

            with self.assertRaisesRegex(quota_poller.PollError, "forbidden"):
                quota_poller.poll_codex(path, 1_700_000_000, transport)

            self.assertEqual(path.read_bytes(), original)

    def test_invalid_access_token_keeps_auth_file_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            original = json.dumps(auth_payload("", "refresh-before")).encode()
            path.write_bytes(original)

            def unexpected_transport(*args, **kwargs):
                self.fail("transport must not run for invalid auth")

            with self.assertRaisesRegex(quota_poller.PollError, "access-token-missing"):
                quota_poller.poll_codex(path, 1_700_000_000, unexpected_transport)

            self.assertEqual(path.read_bytes(), original)

    def test_concurrent_external_auth_change_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text(json.dumps(auth_payload(make_jwt(1_700_010_000), "refresh-shared")))
            external = json.dumps(
                auth_payload("external-access-token", "refresh-shared"),
                indent=2
            ).encode()
            methods = []
            get_count = 0

            def transport(url, *, method, headers, body, timeout):
                nonlocal get_count
                methods.append(method)
                if method == "POST":
                    return quota_poller.HttpResult(200, {}, {
                        "access_token": "poller-access-token",
                        "refresh_token": "refresh-shared"
                    })
                get_count += 1
                if get_count == 1:
                    path.write_bytes(external)
                return quota_poller.HttpResult(401, {}, None)

            with self.assertRaisesRegex(quota_poller.PollError, "unauthorized"):
                quota_poller.poll_codex(path, 1_700_000_000, transport)

            self.assertEqual(methods, ["GET"])
            self.assertEqual(path.read_bytes(), external)

    def test_auth_file_is_reread_on_every_poll_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            authorizations = []

            def transport(url, *, method, headers, body, timeout):
                authorizations.append(headers["Authorization"])
                return quota_poller.HttpResult(200, {}, self.usage_body())

            path.write_text(json.dumps(auth_payload("first-access-token", "refresh-one")))
            quota_poller.poll_codex(path, 1_700_000_000, transport)
            path.write_text(json.dumps(auth_payload("second-access-token", "refresh-two")))
            quota_poller.poll_codex(path, 1_700_000_001, transport)

            self.assertEqual(authorizations, [
                "Bearer first-access-token",
                "Bearer second-access-token"
            ])


class PollingTests(unittest.TestCase):
    def test_run_once_keeps_provider_failures_independent_and_persists_retry_after(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_dir = root / "cache"
            auth_path = root / "auth.json"
            auth_path.write_text(json.dumps(auth_payload(make_jwt(1_700_010_000), "refresh-before")))

            def transport(url, *, method, headers, body, timeout):
                if url == quota_poller.CODEX_USAGE_URL:
                    return quota_poller.HttpResult(429, {"Retry-After": "45"}, None)
                return quota_poller.HttpResult(200, {}, {
                    "balance_infos": [{"currency": "USD", "total_balance": "3"}]
                })

            result = quota_poller.run_once(
                cache_dir=cache_dir,
                auth_path=auth_path,
                now=1_700_000_000,
                transport=transport,
                key_reader=lambda: "synthetic-value"
            )

            self.assertEqual(result, 0)
            codex_status = json.loads((cache_dir / "vendor-codex-local.status").read_text())
            deepseek_cache = json.loads((cache_dir / "vendor-deepseek-local.json").read_text())
            state = json.loads((cache_dir / "cc-quota-poller-state.json").read_text())
            self.assertEqual(codex_status, {"failed_at": 1_700_000_000, "reason": "rate-limited"})
            self.assertEqual(deepseek_cache["data"]["balance_infos"][0]["currency"], "USD")
            self.assertEqual(state["vendors"]["codex"]["not_before"], 1_700_000_045)
            self.assertNotIn("deepseek", state["vendors"])


if __name__ == "__main__":
    unittest.main()
