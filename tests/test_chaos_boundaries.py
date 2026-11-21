from __future__ import annotations

import importlib.util
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
NATIVE_HOST = ROOT / "native-host"
RECEIVER_PATH = NATIVE_HOST / "quota-receiver.py"

sys.path.insert(0, str(NATIVE_HOST))

RECEIVER_SPEC = importlib.util.spec_from_file_location("quota_receiver_chaos", RECEIVER_PATH)
quota_receiver = importlib.util.module_from_spec(RECEIVER_SPEC)
RECEIVER_SPEC.loader.exec_module(quota_receiver)

import anthropic_quota_poller
import quota_poller


def frame_bytes(payload: bytes) -> bytes:
    return struct.pack("<I", len(payload)) + payload


def run_receiver_raw(raw_stdin: bytes, fake_home: Path) -> tuple[int, dict | None, bytes]:
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    result = subprocess.run(
        [sys.executable, str(RECEIVER_PATH)],
        input=raw_stdin,
        capture_output=True,
        env=env,
        timeout=5,
    )
    resp = None
    if len(result.stdout) >= 4:
        resp_len = struct.unpack("<I", result.stdout[:4])[0]
        resp = json.loads(result.stdout[4:4 + resp_len].decode("utf-8"))
    return result.returncode, resp, result.stdout


class QuotaReceiverChaosTests(unittest.TestCase):
    def test_clean_zero_byte_eof_exits_zero_silently(self):
        with tempfile.TemporaryDirectory() as directory:
            rc, resp, stdout = run_receiver_raw(b"", Path(directory))
        self.assertEqual(rc, 0)
        self.assertIsNone(resp)
        self.assertEqual(stdout, b"")

    def test_rejects_oversized_frame_without_reading_body(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            # 0xffffffff length header only (no 4 GiB body) must fail fast
            rc_hdr, resp_hdr, _ = run_receiver_raw(b"\xff\xff\xff\xff", home)
            self.assertEqual(rc_hdr, 1)
            self.assertEqual(resp_hdr, {"ok": False, "reason": "message-too-large"})

            # 1 MiB + 1 byte payload must be rejected
            too_big = json.dumps({
                "type": "status",
                "reason": "x" * quota_receiver.MAX_MESSAGE_BYTES,
            }).encode("utf-8")
            self.assertGreater(len(too_big), quota_receiver.MAX_MESSAGE_BYTES)
            rc_big, resp_big, _ = run_receiver_raw(frame_bytes(too_big), home)
            self.assertEqual(rc_big, 1)
            self.assertEqual(resp_big, {"ok": False, "reason": "message-too-large"})

    def test_rejects_zero_length_frame_with_invalid_message(self):
        with tempfile.TemporaryDirectory() as directory:
            rc, resp, _ = run_receiver_raw(struct.pack("<I", 0), Path(directory))
        self.assertEqual(rc, 1)
        self.assertEqual(resp, {"ok": False, "reason": "invalid-message"})

    def test_rejects_truncated_header_and_truncated_body(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            rc_h, resp_h, _ = run_receiver_raw(b"\x04\x00", home)
            self.assertEqual(rc_h, 1)
            self.assertEqual(resp_h, {"ok": False, "reason": "invalid-frame"})

            rc_b, resp_b, _ = run_receiver_raw(struct.pack("<I", 10) + b"abc", home)
            self.assertEqual(rc_b, 1)
            self.assertEqual(resp_b, {"ok": False, "reason": "truncated-message"})

    def test_rejects_json_null_frame_not_swallowed_as_eof(self):
        with tempfile.TemporaryDirectory() as directory:
            rc, resp, _ = run_receiver_raw(frame_bytes(b"null"), Path(directory))
        self.assertEqual(rc, 1)
        self.assertEqual(resp, {"ok": False, "reason": "invalid-message"})

    def test_rejects_non_utf8_bad_json_and_nonstandard_nan_infinity(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cases = [
                b"\xff\xfe\xfd",
                b"{not-json",
                b'{"type":"quota","email":"nan@example.com","body":{"v":NaN}}',
                b'{"type":"quota","email":"inf@example.com","body":{"v":Infinity}}',
                b'{"type":"quota","email":"ninf@example.com","body":{"v":-Infinity}}',
            ]
            for raw_payload in cases:
                with self.subTest(payload=raw_payload):
                    rc, resp, _ = run_receiver_raw(frame_bytes(raw_payload), home)
                    self.assertEqual(rc, 1)
                    self.assertEqual(resp, {"ok": False, "reason": "invalid-message"})

            cache_dir = home / ".claude" / "cache"
            if cache_dir.exists():
                self.assertEqual(list(cache_dir.iterdir()), [])

    def test_rejects_non_string_or_nul_email_and_handles_null_vendor_fetched_at(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            with mock.patch.object(quota_receiver, "CACHE_DIR", cache_dir):
                for bad_email in (123, ["a"], False, "a\x00b"):
                    with self.subTest(email=bad_email):
                        resp = quota_receiver.handle({
                            "type": "quota",
                            "email": bad_email,
                            "body": {"five_hour": {}},
                        })
                        self.assertEqual(resp, {"ok": False, "reason": "invalid-message"})

                status_resp = quota_receiver.handle({
                    "type": "vendor-status",
                    "profile_id": "1234abcd",
                    "vendor": "mimo",
                    "fetched_at": None,
                    "reason": "rate-limited",
                })
                self.assertTrue(status_resp["ok"])
                written = Path(status_resp["wrote"]).read_text(encoding="utf-8")
                self.assertEqual(written, "\trate-limited")
                self.assertNotIn("None", written)

    def test_concurrent_quota_writes_and_status_unlinks_stay_atomic_and_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            status_path = cache_dir / "quota-race_at_example.com.status"
            cache_path = cache_dir / "quota-race_at_example.com.json"

            def writer(idx: int) -> dict:
                if idx % 3 == 0:
                    return quota_receiver.handle({
                        "type": "status",
                        "email": "race@example.com",
                        "fetched_at": "12:00:00",
                        "reason": f"err-{idx}",
                    })
                return quota_receiver.handle({
                    "type": "quota",
                    "email": "race@example.com",
                    "fetched_at": "12:00:00",
                    "org_id": "org-1",
                    "body": {"limits": [{"idx": idx, "pad": "x" * 512}]},
                })

            with mock.patch.object(quota_receiver, "CACHE_DIR", cache_dir):
                with ThreadPoolExecutor(max_workers=8) as executor:
                    results = list(executor.map(writer, range(40)))

            self.assertTrue(all(item.get("ok") is True for item in results))
            parsed = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertIn("limits", parsed["data"])
            self.assertEqual(cache_path.stat().st_mode & 0o777, 0o600)
            if status_path.exists():
                self.assertEqual(status_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(cache_dir.glob("*.tmp")), [])


class QuotaPollerChaosTests(unittest.TestCase):
    def test_non_utf8_and_non_dict_files_map_to_expected_errors_or_empty_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad_utf8 = root / "bad.bin"
            bad_utf8.write_bytes(b"\xff\xfe\xfd")

            self.assertEqual(quota_poller.load_state(bad_utf8), {"vendors": {}})

            with self.assertRaisesRegex(quota_poller.PollError, "^auth-invalid$"):
                quota_poller.read_auth_json(bad_utf8)

            with self.assertRaisesRegex(quota_poller.PollError, "^auth-invalid$"):
                quota_poller.magpie_codex_auth(bad_utf8, "user@example.com")

            with self.assertRaisesRegex(quota_poller.PollError, "^source-invalid$"):
                quota_poller.load_codex_credentials(root / "absent.json", bad_utf8)

            non_dict = root / "null.json"
            non_dict.write_text("null", encoding="utf-8")
            with self.assertRaisesRegex(quota_poller.PollError, "^source-invalid$"):
                quota_poller.load_codex_credentials(root / "absent.json", non_dict)

    def test_is_backed_off_guards_non_dict_and_non_finite_values(self):
        for state in (
            {"vendors": {"codex": None}},
            {"vendors": None},
            {"vendors": {"codex": []}},
            {"vendors": {"codex": {"not_before": float("inf")}}},
            {"vendors": {"codex": {"not_before": float("nan")}}},
            {"vendors": {"codex": {"not_before": True}}},
        ):
            with self.subTest(state=state):
                self.assertFalse(quota_poller.is_backed_off(state, "codex", 1000))

    def test_parse_retry_after_rejects_unicode_digits_and_caps_huge_values(self):
        now = 1_700_000_000
        self.assertEqual(quota_poller.parse_retry_after("²³", now), quota_poller.DEFAULT_RETRY_AFTER_SECONDS)
        self.assertEqual(quota_poller.parse_retry_after("99999999999999999999", now), quota_poller.MAX_BACKOFF_SECONDS)
        self.assertEqual(quota_poller.parse_retry_after("9" * 5000, now), quota_poller.MAX_BACKOFF_SECONDS)
        self.assertEqual(
            quota_poller.parse_retry_after("Fri, 31 Dec 9999 23:59:59 GMT", now),
            quota_poller.MAX_BACKOFF_SECONDS,
        )
        self.assertEqual(
            quota_poller.parse_retry_after("01 Jan , 00:00:00 GMT", now),
            quota_poller.DEFAULT_RETRY_AFTER_SECONDS,
        )

    def test_normalize_window_rejects_sub_one_floats_truncated_to_zero(self):
        self.assertIsNone(
            quota_poller.normalize_window({
                "used_percent": 10,
                "limit_window_seconds": 0.5,
                "reset_at": 1_700_000_000,
            })
        )
        self.assertIsNone(
            quota_poller.normalize_window({
                "used_percent": 10,
                "limit_window_seconds": 18000,
                "reset_at": 0.5,
            })
        )

    def test_atomic_write_json_does_not_double_close_descriptor_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            real_close = os.close
            closed_fds = []

            def tracking_close(fd: int) -> None:
                closed_fds.append(fd)
                real_close(fd)

            with mock.patch.object(quota_poller.os, "replace", side_effect=OSError("disk full")), \
                 mock.patch.object(quota_poller.os, "close", side_effect=tracking_close):
                with self.assertRaises(OSError):
                    quota_poller.atomic_write_json(target, {"a": 1})

            self.assertEqual(closed_fds, [])
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


class AnthropicQuotaPollerChaosTests(unittest.TestCase):
    def test_non_utf8_and_non_dict_inputs_never_crash_readers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad_utf8 = root / "bad.bin"
            bad_utf8.write_bytes(b"\xff\xfe\xfd")
            null_json = root / "null.json"
            null_json.write_text("null", encoding="utf-8")

            with mock.patch.object(anthropic_quota_poller, "STATE_PATH", bad_utf8):
                self.assertEqual(anthropic_quota_poller.load_state(), {"accounts": {}})
            with mock.patch.object(anthropic_quota_poller, "STATE_PATH", null_json):
                self.assertEqual(anthropic_quota_poller.load_state(), {"accounts": {}})

            cfg_bad = root / "cfg_bad"
            cfg_bad.mkdir()
            (cfg_bad / ".claude.json").write_bytes(b"\xff\xfe\xfd")
            self.assertEqual(anthropic_quota_poller.read_account_identity(cfg_bad), (None, None))

            cfg_null = root / "cfg_null"
            cfg_null.mkdir()
            (cfg_null / ".claude.json").write_text("[]", encoding="utf-8")
            self.assertEqual(anthropic_quota_poller.read_account_identity(cfg_null), (None, None))

            with mock.patch.object(
                anthropic_quota_poller.subprocess,
                "run",
                side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
            ):
                self.assertIsNone(anthropic_quota_poller.read_keychain_oauth("svc"))

            with mock.patch.object(
                anthropic_quota_poller.subprocess,
                "run",
                return_value=mock.Mock(returncode=0, stdout="null"),
            ):
                self.assertIsNone(anthropic_quota_poller.read_keychain_oauth("svc"))

            cache_dir = root / "cache"
            cache_dir.mkdir()
            cache_file = cache_dir / "quota-user_at_example.com.json"
            cache_file.write_text("null", encoding="utf-8")
            now = int(cache_file.stat().st_mtime) + 5
            with mock.patch.object(anthropic_quota_poller, "CACHE_DIR", cache_dir):
                self.assertFalse(anthropic_quota_poller.cache_covers_account("user@example.com", now))
                cache_file.write_bytes(b"\xff\xfe")
                self.assertFalse(anthropic_quota_poller.cache_covers_account("user@example.com", now))

    def test_is_backed_off_parse_retry_after_and_nan_rejection(self):
        for state in (
            {"accounts": None},
            {"accounts": {"u@e.com": None}},
            {"accounts": {"u@e.com": []}},
            {"accounts": {"u@e.com": {"not_before": float("inf")}}},
            {"accounts": {"u@e.com": {"not_before": float("nan")}}},
        ):
            with self.subTest(state=state):
                self.assertFalse(anthropic_quota_poller.is_backed_off(state, "u@e.com", 1000))

        now = 1_700_000_000
        self.assertEqual(
            anthropic_quota_poller.parse_retry_after("²³", now),
            anthropic_quota_poller.DEFAULT_RETRY_AFTER_SECONDS,
        )
        self.assertEqual(
            anthropic_quota_poller.parse_retry_after("9" * 5000, now),
            anthropic_quota_poller.MAX_BACKOFF_SECONDS,
        )
        self.assertEqual(
            anthropic_quota_poller.parse_retry_after("01 Jan , 00:00:00 GMT", now),
            anthropic_quota_poller.DEFAULT_RETRY_AFTER_SECONDS,
        )

        class FakeResponse:
            status = 200
            headers = {}

            def read(self):
                return b'{"five_hour": NaN}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch.object(anthropic_quota_poller.urllib.request, "urlopen", return_value=FakeResponse()):
            with self.assertRaisesRegex(anthropic_quota_poller.PollError, "^invalid-json$"):
                anthropic_quota_poller.fetch_usage("tok", now)

    def test_run_once_isolates_account_crash_and_persists_prior_429_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".claude").mkdir()
            (home / ".claude-2").mkdir()
            (home / ".claude-3").mkdir()
            (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "a1@example.com"}}))
            (home / ".claude-2" / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": "a2@example.com"}})
            )
            (home / ".claude-3" / ".claude.json").write_text(
                json.dumps({"oauthAccount": {"emailAddress": "a3@example.com"}})
            )

            cache_dir = home / ".claude" / "cache"
            state_path = cache_dir / "anthropic-quota-poller-state.json"
            lock_path = cache_dir / "anthropic-quota-poller.lock"
            polled = []

            def fake_poll_account(config_dir, state, now, stamp):
                email, _ = anthropic_quota_poller.read_account_identity(config_dir)
                polled.append(email)
                if email == "a1@example.com":
                    anthropic_quota_poller.set_not_before(state, email, now + 1800)
                    return True
                if email == "a2@example.com":
                    raise RuntimeError("unexpected failure on a2")
                return True

            with mock.patch.object(Path, "home", return_value=home), \
                 mock.patch.object(anthropic_quota_poller, "CACHE_DIR", cache_dir), \
                 mock.patch.object(anthropic_quota_poller, "STATE_PATH", state_path), \
                 mock.patch.object(anthropic_quota_poller, "LOCK_PATH", lock_path), \
                 mock.patch.object(anthropic_quota_poller, "poll_account", side_effect=fake_poll_account):
                rc = anthropic_quota_poller.run_once()

            self.assertEqual(rc, 0)
            self.assertEqual(polled, ["a1@example.com", "a2@example.com", "a3@example.com"])
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("a1@example.com", saved["accounts"])

    def test_run_once_does_not_mask_later_valid_dir_when_earlier_dir_lacks_valid_token(self):
        for scenario in ("missing-keychain", "expired-token"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                (home / ".claude").mkdir()
                (home / ".claude-alt").mkdir()
                (home / ".claude.json").write_text(
                    json.dumps({"oauthAccount": {"emailAddress": "dup@example.com"}})
                )
                (home / ".claude-alt" / ".claude.json").write_text(
                    json.dumps({"oauthAccount": {"emailAddress": "dup@example.com"}})
                )
                cache_dir = home / ".claude" / "cache"
                state_path = cache_dir / "anthropic-quota-poller-state.json"
                lock_path = cache_dir / "anthropic-quota-poller.lock"
                fetched = []

                def fake_keychain(service):
                    if service == anthropic_quota_poller.KEYCHAIN_SERVICE:
                        if scenario == "missing-keychain":
                            return None
                        return {"accessToken": "expired-tok", "expiresAt": 1}
                    return {"accessToken": "valid-tok", "expiresAt": 9_999_999_999_000}

                def fake_fetch(token, now):
                    fetched.append(token)
                    return {"limits": []}

                with mock.patch.object(Path, "home", return_value=home), \
                     mock.patch.object(anthropic_quota_poller, "CACHE_DIR", cache_dir), \
                     mock.patch.object(anthropic_quota_poller, "STATE_PATH", state_path), \
                     mock.patch.object(anthropic_quota_poller, "LOCK_PATH", lock_path), \
                     mock.patch.object(anthropic_quota_poller, "read_keychain_oauth", side_effect=fake_keychain), \
                     mock.patch.object(anthropic_quota_poller, "fetch_usage", side_effect=fake_fetch):
                    rc = anthropic_quota_poller.run_once()

                self.assertEqual(rc, 0)
                self.assertEqual(fetched, ["valid-tok"])


if __name__ == "__main__":
    unittest.main()
