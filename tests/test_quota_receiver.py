import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
NATIVE_HOST = ROOT / "native-host"
sys.path.insert(0, str(NATIVE_HOST))
SPEC = importlib.util.spec_from_file_location("quota_receiver", NATIVE_HOST / "quota-receiver.py")
quota_receiver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quota_receiver)


class ReceiverMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.cache_dir = Path(self.tempdir.name)
        self.cache_patch = mock.patch.object(quota_receiver, "CACHE_DIR", self.cache_dir)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def message(self, secret="synthetic-value"):
        return {
            "type": "migrate-deepseek-key-v1",
            "profile_id": "1234abcd",
            "secret": secret
        }

    def test_migration_uses_create_only_operation_and_returns_no_secret_or_path(self):
        with mock.patch.object(
            quota_receiver.keychain,
            "create_secret_if_absent",
            return_value=True,
            create=True
        ) as create_secret, mock.patch.object(
            quota_receiver.keychain,
            "read_secret",
            return_value=None
        ), mock.patch.object(quota_receiver.keychain, "write_secret"):
            response = quota_receiver.handle(self.message())

        self.assertEqual(response, {"ok": True})
        create_secret.assert_called_once_with("synthetic-value")
        self.assertNotIn("synthetic-value", json.dumps(response))

    def test_migration_is_idempotent_for_same_key(self):
        with mock.patch.object(
            quota_receiver.keychain,
            "create_secret_if_absent",
            return_value=True,
            create=True
        ) as create_secret, mock.patch.object(
            quota_receiver.keychain,
            "read_secret",
            return_value="synthetic-value"
        ), mock.patch.object(quota_receiver.keychain, "write_secret"):
            response = quota_receiver.handle(self.message())

        self.assertEqual(response, {"ok": True})
        create_secret.assert_called_once_with("synthetic-value")

    def test_migration_rejects_different_existing_key_with_fixed_code(self):
        with mock.patch.object(
            quota_receiver.keychain,
            "create_secret_if_absent",
            return_value=False,
            create=True
        ) as create_secret, mock.patch.object(
            quota_receiver.keychain,
            "read_secret",
            return_value="different-synthetic-value"
        ), mock.patch.object(quota_receiver.keychain, "write_secret"):
            response = quota_receiver.handle(self.message())

        self.assertEqual(response, {"ok": False, "reason": "profile-conflict"})
        create_secret.assert_called_once_with("synthetic-value")

    def test_concurrent_different_profile_migrations_cannot_both_succeed(self):
        barrier = threading.Barrier(2)
        store = {}
        store_lock = threading.Lock()

        def legacy_read():
            barrier.wait()
            return None

        def legacy_write(secret):
            with store_lock:
                store["secret"] = secret

        def atomic_create(secret):
            barrier.wait()
            with store_lock:
                if "secret" not in store:
                    store["secret"] = secret
                    return True
                return store["secret"] == secret

        messages = [self.message("synthetic-value-a"), self.message("synthetic-value-b")]
        with mock.patch.object(
            quota_receiver.keychain,
            "create_secret_if_absent",
            side_effect=atomic_create,
            create=True
        ), mock.patch.object(
            quota_receiver.keychain,
            "read_secret",
            side_effect=legacy_read
        ), mock.patch.object(
            quota_receiver.keychain,
            "write_secret",
            side_effect=legacy_write
        ), ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(quota_receiver.handle, messages))

        self.assertEqual(responses.count({"ok": True}), 1)
        self.assertEqual(
            responses.count({"ok": False, "reason": "profile-conflict"}),
            1
        )

    def test_migration_rejects_invalid_profile_and_control_characters(self):
        invalid_profile = self.message()
        invalid_profile["profile_id"] = "not-valid"
        invalid_secret = self.message("synthetic\nvalue")

        self.assertEqual(
            quota_receiver.handle(invalid_profile),
            {"ok": False, "reason": "invalid-profile"}
        )
        self.assertEqual(
            quota_receiver.handle(invalid_secret),
            {"ok": False, "reason": "invalid-secret"}
        )

    def test_deepseek_legacy_balance_plan_and_status_writes_are_rejected(self):
        messages = [
            {
                "type": "vendor-balance",
                "profile_id": "1234abcd",
                "vendor": "deepseek",
                "data": {}
            },
            {
                "type": "vendor-plan",
                "profile_id": "1234abcd",
                "vendor": "deepseek",
                "data": {}
            },
            {
                "type": "vendor-status",
                "profile_id": "1234abcd",
                "vendor": "deepseek",
                "reason": "synthetic"
            }
        ]

        for message in messages:
            self.assertEqual(
                quota_receiver.handle(message),
                {"ok": False, "reason": "invalid-vendor"}
            )

        self.assertEqual(list(self.cache_dir.iterdir()), [])

    def test_deepseek_legacy_cache_can_still_be_cleared(self):
        names = [
            "vendor-deepseek-1234abcd.json",
            "vendor-deepseek-1234abcd.status",
            "vendor-deepseek-plan-1234abcd.json"
        ]
        for name in names:
            (self.cache_dir / name).write_text("synthetic")

        response = quota_receiver.handle({
            "type": "vendor-clear",
            "profile_id": "1234abcd",
            "vendor": "deepseek"
        })

        self.assertTrue(response["ok"])
        self.assertEqual(set(response["removed"]), set(names))
        self.assertEqual(list(self.cache_dir.iterdir()), [])

    def test_success_path_has_no_invocation_logging_statements(self):
        source = (NATIVE_HOST / "quota-receiver.py").read_text()

        self.assertNotIn("INVOKED", source)
        self.assertNotIn("recv:", source)


if __name__ == "__main__":
    unittest.main()
