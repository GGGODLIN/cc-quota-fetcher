import io
import json
import plistlib
import sys
import tempfile
import time
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

NATIVE_HOST = Path(__file__).resolve().parents[1] / "native-host"
sys.path.insert(0, str(NATIVE_HOST))

import anthropic_quota_poller as poller


def http_error(status, headers):
    message = Message()
    for key, value in headers.items():
        message[key] = value
    return urllib.error.HTTPError(
        poller.USAGE_URL, status, "error", message, io.BytesIO(b"{}")
    )


def raise_http_error(status, headers):
    def opener(request, timeout=None):
        raise http_error(status, headers)

    return opener


class RetryAfterParsingTests(unittest.TestCase):
    def test_numeric_header_is_seconds(self):
        self.assertEqual(poller.parse_retry_after("2446", 1_000_000), 2446)

    def test_whitespace_is_stripped(self):
        self.assertEqual(poller.parse_retry_after(" 2446 ", 1_000_000), 2446)

    def test_missing_header_falls_back_to_default(self):
        self.assertEqual(
            poller.parse_retry_after(None, 1_000_000), poller.DEFAULT_RETRY_AFTER_SECONDS
        )

    def test_past_http_date_clamps_to_zero(self):
        self.assertEqual(
            poller.parse_retry_after("Thu, 01 Jan 1970 00:00:01 GMT", 1_000_000), 0
        )


class RateLimitBackoffTests(unittest.TestCase):
    def fetch_and_capture_error(self, headers):
        with mock.patch.object(
            poller.urllib.request, "urlopen", raise_http_error(429, headers)
        ):
            with self.assertRaises(poller.PollError) as captured:
                poller.fetch_usage("token", 1_000_000)
        return captured.exception

    def test_zero_retry_after_still_backs_off(self):
        error = self.fetch_and_capture_error({"Retry-After": "0"})

        self.assertEqual(error.code, "rate-limited")
        self.assertEqual(error.retry_after, poller.MIN_RATE_LIMIT_BACKOFF_SECONDS)

    def test_past_date_retry_after_still_backs_off(self):
        error = self.fetch_and_capture_error(
            {"Retry-After": "Thu, 01 Jan 1970 00:00:01 GMT"}
        )

        self.assertEqual(error.retry_after, poller.MIN_RATE_LIMIT_BACKOFF_SECONDS)

    def test_long_retry_after_is_honoured_above_the_floor(self):
        error = self.fetch_and_capture_error({"Retry-After": "2446"})

        self.assertEqual(error.retry_after, 2446)

    def test_missing_retry_after_still_backs_off(self):
        error = self.fetch_and_capture_error({})

        self.assertEqual(error.retry_after, poller.MIN_RATE_LIMIT_BACKOFF_SECONDS)

    def test_backoff_floor_outlasts_one_scheduled_poll(self):
        plist_path = NATIVE_HOST / "com.cc.anthropic-quota-poller.plist"
        with plist_path.open("rb") as handle:
            interval = plistlib.load(handle)["StartInterval"]

        self.assertGreater(poller.MIN_RATE_LIMIT_BACKOFF_SECONDS, interval)


class BackoffStateTests(unittest.TestCase):
    def test_zero_retry_after_suppresses_the_next_poll(self):
        now = int(time.time())
        state = {"accounts": {}}
        poller.set_not_before(state, "user@example.com", now + poller.MIN_RATE_LIMIT_BACKOFF_SECONDS)

        self.assertTrue(poller.is_backed_off(state, "user@example.com", now))
        self.assertTrue(
            poller.is_backed_off(
                state, "user@example.com", now + poller.MIN_RATE_LIMIT_BACKOFF_SECONDS - 1
            )
        )
        self.assertFalse(
            poller.is_backed_off(
                state, "user@example.com", now + poller.MIN_RATE_LIMIT_BACKOFF_SECONDS
            )
        )

    def test_rate_limited_account_is_skipped_without_a_request(self):
        now = int(time.time())
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / ".claude"
            config_dir.mkdir()
            (config_dir / ".claude.json").write_text(
                json.dumps(
                    {
                        "oauthAccount": {
                            "emailAddress": "user@example.com",
                            "organizationUuid": "org-1",
                        }
                    }
                )
            )
            state = {"accounts": {"user@example.com": {"not_before": now + 600}}}
            oauth = {"accessToken": "token", "expiresAt": (now + 3600) * 1000}

            with mock.patch.object(poller, "read_keychain_oauth", return_value=oauth):
                with mock.patch.object(poller, "fetch_usage") as fetch:
                    with mock.patch.object(poller, "write_failure") as failure:
                        poller.poll_account(config_dir, state, now, "13:47:08")

        fetch.assert_not_called()
        failure.assert_not_called()


class PlistContractTests(unittest.TestCase):
    def test_launch_agent_matches_the_poller_contract(self):
        plist_path = NATIVE_HOST / "com.cc.anthropic-quota-poller.plist"
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)

        self.assertEqual(plist["Label"], "com.cc.anthropic-quota-poller")
        self.assertEqual(plist["ProgramArguments"][0], "__POLLER_PATH__")
        self.assertEqual(plist["ProgramArguments"][-1], "--once")
        self.assertEqual(plist["StartInterval"], 180)
        self.assertTrue(plist["RunAtLoad"])
        self.assertNotIn("KeepAlive", plist)


if __name__ == "__main__":
    unittest.main()
