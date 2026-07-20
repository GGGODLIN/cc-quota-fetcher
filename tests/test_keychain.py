import io
import sys
import unittest
from pathlib import Path
from unittest import mock

NATIVE_HOST = Path(__file__).resolve().parents[1] / "native-host"
sys.path.insert(0, str(NATIVE_HOST))

import keychain


class KeychainTests(unittest.TestCase):
    def create_secret_if_absent(self, secret):
        operation = getattr(keychain, "create_secret_if_absent", None)
        self.assertIsNotNone(operation)
        return operation(secret)

    @mock.patch("keychain._release_item")
    @mock.patch("keychain._find_secret", return_value=(0, "synthetic-value", 123))
    def test_read_secret_uses_security_framework(self, find_secret, release_item):
        self.assertEqual(keychain.read_secret(), "synthetic-value")
        find_secret.assert_called_once_with(include_data=True)
        release_item.assert_called_once_with(123)

    @mock.patch("keychain._release_item")
    @mock.patch("keychain._find_secret", return_value=(keychain.ERR_SEC_ITEM_NOT_FOUND, None, None))
    def test_read_secret_returns_none_when_item_is_missing(self, find_secret, release_item):
        self.assertIsNone(keychain.read_secret())
        release_item.assert_not_called()

    @mock.patch("keychain._release_item")
    @mock.patch("keychain._modify_item", return_value=0)
    @mock.patch("keychain._find_secret", return_value=(0, None, 123))
    def test_write_secret_updates_existing_item_without_subprocess(self, find_secret, modify_item, release_item):
        keychain.write_secret("synthetic-value")

        find_secret.assert_called_once_with(include_data=False)
        modify_item.assert_called_once_with(123, "synthetic-value")
        release_item.assert_called_once_with(123)
        self.assertNotIn("subprocess", (NATIVE_HOST / "keychain.py").read_text())

    @mock.patch("keychain._add_item", return_value=0)
    @mock.patch("keychain._find_secret", return_value=(keychain.ERR_SEC_ITEM_NOT_FOUND, None, None))
    def test_write_secret_adds_missing_item(self, find_secret, add_item):
        keychain.write_secret("synthetic-value")

        add_item.assert_called_once_with("synthetic-value")

    @mock.patch("keychain._add_item", return_value=keychain.ERR_SEC_SUCCESS)
    @mock.patch("keychain.read_secret")
    def test_create_secret_if_absent_adds_without_precheck(self, read_secret, add_item):
        self.assertTrue(self.create_secret_if_absent("synthetic-value"))
        add_item.assert_called_once_with("synthetic-value")
        read_secret.assert_not_called()

    @mock.patch("keychain.read_secret", return_value="synthetic-value")
    @mock.patch("keychain._add_item", return_value=-25299)
    def test_create_secret_if_absent_accepts_matching_duplicate(self, add_item, read_secret):
        self.assertTrue(self.create_secret_if_absent("synthetic-value"))
        read_secret.assert_called_once_with()

    @mock.patch("keychain.read_secret", return_value="different-synthetic-value")
    @mock.patch("keychain._add_item", return_value=-25299)
    def test_create_secret_if_absent_rejects_different_duplicate(self, add_item, read_secret):
        self.assertFalse(self.create_secret_if_absent("synthetic-value"))
        read_secret.assert_called_once_with()

    @mock.patch("keychain.read_secret", return_value="合成密鑰-value")
    @mock.patch("keychain._add_item", return_value=-25299)
    def test_create_secret_if_absent_accepts_matching_unicode_duplicate(self, add_item, read_secret):
        self.assertTrue(self.create_secret_if_absent("合成密鑰-value"))
        read_secret.assert_called_once_with()

    @mock.patch("keychain._add_item", return_value=-1)
    def test_create_secret_if_absent_uses_fixed_write_error(self, add_item):
        with self.assertRaisesRegex(keychain.KeychainError, "keychain-write-failed"):
            self.create_secret_if_absent("synthetic-value")

    @mock.patch("keychain._find_secret", return_value=(-1, None, None))
    def test_keychain_errors_use_fixed_code(self, find_secret):
        with self.assertRaisesRegex(keychain.KeychainError, "keychain-write-failed"):
            keychain.write_secret("synthetic-value")

    @mock.patch("keychain.read_secret", return_value="synthetic-value")
    def test_check_cli_returns_success_without_output(self, read_secret):
        stdout = io.StringIO()
        with mock.patch.object(keychain.sys, "argv", ["keychain.py", "--check"]), \
             mock.patch.object(keychain.sys, "stdout", stdout):
            result = keychain.main()

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "")

    @mock.patch("keychain.write_secret")
    def test_set_stdin_cli_passes_secret_without_output(self, write_secret):
        stdout = io.StringIO()
        with mock.patch.object(keychain.sys, "argv", ["keychain.py", "--set-stdin"]), \
             mock.patch.object(keychain.sys, "stdin", io.StringIO("synthetic-value\n")), \
             mock.patch.object(keychain.sys, "stdout", stdout):
            result = keychain.main()

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "")
        write_secret.assert_called_once_with("synthetic-value")


if __name__ == "__main__":
    unittest.main()
