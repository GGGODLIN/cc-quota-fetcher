import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension"
NATIVE_HOST = ROOT / "native-host"


class ExtensionContractTests(unittest.TestCase):
    def test_anthropic_fetch_function_is_unchanged(self):
        source = (EXTENSION / "background.js").read_text()
        start = source.index("const fetchAnthropicQuota = async (pid) => {")
        end = source.index("\n};", start) + 3
        digest = hashlib.sha256(source[start:end].encode()).hexdigest()

        self.assertEqual(digest, "932318012c99d72e8e5e7392db7d6ff3e8d6ca1f75139bac9571fa39ee110f0f")

    def test_tick_only_fetches_anthropic_quota(self):
        source = (EXTENSION / "background.js").read_text()
        match = re.search(r"const tick = async \(\) => \{(?P<body>.*?)\n\};", source, re.DOTALL)
        self.assertIsNotNone(match)
        body = match.group("body")

        self.assertIn("await fetchAnthropicQuota(pid);", body)
        self.assertEqual(body.count("fetch"), 1)
        self.assertNotIn("getVendorConfig", body)

    def test_active_codex_and_deepseek_network_code_is_removed(self):
        source = (EXTENSION / "background.js").read_text()

        for forbidden in [
            "CODEX_SESSION_URL",
            "CODEX_USAGE_URL",
            "fetchCodexUsage",
            "DEEPSEEK_BALANCE_URL",
            "fetchDeepSeekBalance"
        ]:
            self.assertNotIn(forbidden, source)
        self.assertIn("migrate-deepseek-key-v1", source)
        self.assertIn("deepseek_key_migration_version", source)
        self.assertNotIn("chrome.storage.local.remove('deepseek_api_key')", source)

    def test_deepseek_migration_preserves_key_changed_after_snapshot(self):
        harness = r'''
import fs from 'node:fs';
import { webcrypto } from 'node:crypto';
import vm from 'node:vm';

const source = fs.readFileSync(process.argv[1], 'utf8');
const state = {
  profile_id: '1234abcd',
  deepseek_api_key: 'old-synthetic-secret',
  vendor_enabled: { deepseek: true, mimo: false, glm: false }
};
const sent = [];
let removeCalls = 0;
const chrome = {
  storage: {
    local: {
      get: async (keys) => {
        const names = Array.isArray(keys) ? keys : [keys];
        return Object.fromEntries(names.filter((name) => name in state).map((name) => [name, state[name]]));
      },
      set: async (values) => Object.assign(state, values),
      remove: async (key) => {
        removeCalls += 1;
        delete state[key];
      }
    }
  },
  runtime: {
    lastError: null,
    sendNativeMessage: (_host, payload, callback) => {
      sent.push(payload.type);
      if (payload.type === 'vendor-clear') {
        state.deepseek_api_key = 'new-synthetic-secret';
      }
      callback({ ok: true });
    },
    onInstalled: { addListener: () => {} },
    onStartup: { addListener: () => {} }
  },
  alarms: {
    get: async () => null,
    create: () => {},
    onAlarm: { addListener: () => {} }
  }
};
const context = {
  AbortController,
  chrome,
  clearTimeout,
  console,
  TextEncoder,
  crypto: {
    randomUUID: () => '1234abcd-0000-0000-0000-000000000000',
    subtle: webcrypto.subtle
  },
  fetch: async () => { throw new Error('unexpected fetch'); },
  setTimeout
};
vm.runInNewContext(source, context);
for (let index = 0; index < 5; index += 1) {
  await new Promise((resolve) => setImmediate(resolve));
}
process.stdout.write(JSON.stringify({ state, sent, removeCalls }));
'''
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                harness,
                str(EXTENSION / "background.js")
            ],
            capture_output=True,
            text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)

        self.assertEqual(observed["state"]["deepseek_api_key"], "new-synthetic-secret")
        self.assertTrue(observed["state"]["vendor_enabled"]["deepseek"])
        self.assertNotIn("deepseek_key_migration_version", observed["state"])
        self.assertEqual(observed["removeCalls"], 0)
        self.assertEqual(observed["sent"], ["migrate-deepseek-key-v1", "vendor-clear"])

    def test_migration_fingerprint_detects_key_changed_before_marker_write(self):
        harness = r'''
import fs from 'node:fs';
import { webcrypto } from 'node:crypto';
import vm from 'node:vm';

const source = fs.readFileSync(process.argv[1], 'utf8');
const state = {
  profile_id: '1234abcd',
  deepseek_api_key: 'old-synthetic-secret',
  vendor_enabled: { deepseek: true, mimo: false, glm: false }
};
let changeKey = true;
let migrationCalls = 0;
const chrome = {
  storage: {
    local: {
      get: async (keys) => {
        if (keys === 'vendor_enabled' && changeKey) {
          changeKey = false;
          state.deepseek_api_key = 'new-synthetic-secret';
        }
        const names = Array.isArray(keys) ? keys : [keys];
        return Object.fromEntries(names.filter((name) => name in state).map((name) => [name, state[name]]));
      },
      set: async (values) => Object.assign(state, values)
    }
  },
  runtime: {
    lastError: null,
    sendNativeMessage: (_host, payload, callback) => {
      if (payload.type === 'migrate-deepseek-key-v1') migrationCalls += 1;
      callback(migrationCalls === 1 ? { ok: true } : { ok: false, reason: 'profile-conflict' });
    },
    onInstalled: { addListener: () => {} },
    onStartup: { addListener: () => {} }
  },
  alarms: {
    get: async () => null,
    create: () => {},
    onAlarm: { addListener: () => {} }
  }
};
const context = () => ({
  AbortController,
  chrome,
  clearTimeout,
  console,
  TextEncoder,
  crypto: {
    randomUUID: () => '1234abcd-0000-0000-0000-000000000000',
    subtle: webcrypto.subtle
  },
  fetch: async () => { throw new Error('unexpected fetch'); },
  setTimeout
});
vm.runInNewContext(source, context());
for (let index = 0; index < 10; index += 1) {
  await new Promise((resolve) => setImmediate(resolve));
}
vm.runInNewContext(source, context());
for (let index = 0; index < 10; index += 1) {
  await new Promise((resolve) => setImmediate(resolve));
}
process.stdout.write(JSON.stringify({
  migrationCalls,
  version: state.deepseek_key_migration_version,
  key: state.deepseek_api_key
}));
'''
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                harness,
                str(EXTENSION / "background.js")
            ],
            capture_output=True,
            text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)

        self.assertEqual(observed["migrationCalls"], 2)
        self.assertEqual(observed["version"], 1)
        self.assertEqual(observed["key"], "new-synthetic-secret")

    def test_completed_deepseek_migration_does_not_repeat_native_messages(self):
        harness = r'''
import fs from 'node:fs';
import { webcrypto } from 'node:crypto';
import vm from 'node:vm';

const source = fs.readFileSync(process.argv[1], 'utf8');
const state = {
  deepseek_api_key: 'synthetic-secret',
  deepseek_key_migration_version: 1,
  deepseek_key_migration_fingerprint: 'dfe7ed3dca6c04829b439d6eef708640eb84e7618700f5663f089926b1fb99fe'
};
const sent = [];
let setCalls = 0;
const chrome = {
  storage: {
    local: {
      get: async (keys) => {
        const names = Array.isArray(keys) ? keys : [keys];
        return Object.fromEntries(names.filter((name) => name in state).map((name) => [name, state[name]]));
      },
      set: async (values) => {
        setCalls += 1;
        Object.assign(state, values);
      }
    }
  },
  runtime: {
    lastError: null,
    sendNativeMessage: (_host, payload, callback) => {
      sent.push(payload.type);
      callback({ ok: true });
    },
    onInstalled: { addListener: () => {} },
    onStartup: { addListener: () => {} }
  },
  alarms: {
    get: async () => null,
    create: () => {},
    onAlarm: { addListener: () => {} }
  }
};
vm.runInNewContext(source, {
  AbortController,
  chrome,
  clearTimeout,
  console,
  TextEncoder,
  crypto: {
    randomUUID: () => '1234abcd-0000-0000-0000-000000000000',
    subtle: webcrypto.subtle
  },
  fetch: async () => { throw new Error('unexpected fetch'); },
  setTimeout
});
for (let index = 0; index < 5; index += 1) {
  await new Promise((resolve) => setImmediate(resolve));
}
process.stdout.write(JSON.stringify({ sent, setCalls }));
'''
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                harness,
                str(EXTENSION / "background.js")
            ],
            capture_output=True,
            text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)

        self.assertEqual(observed["sent"], [])
        self.assertEqual(observed["setCalls"], 0)

    def test_deepseek_migration_and_startup_tick_share_profile_initialization(self):
        harness = r'''
import fs from 'node:fs';
import { webcrypto } from 'node:crypto';
import vm from 'node:vm';

const source = fs.readFileSync(process.argv[1], 'utf8');
const state = { deepseek_api_key: 'synthetic-secret' };
const profileReads = [];
const sent = [];
const startupListeners = [];
let randomCalls = 0;
const getValues = (keys) => {
  const names = Array.isArray(keys) ? keys : [keys];
  return Object.fromEntries(names.filter((name) => name in state).map((name) => [name, state[name]]));
};
const chrome = {
  storage: {
    local: {
      get: async (keys) => {
        if (keys === 'profile_id') {
          return new Promise((resolve) => profileReads.push(resolve));
        }
        return getValues(keys);
      },
      set: async (values) => Object.assign(state, values)
    }
  },
  runtime: {
    lastError: null,
    sendNativeMessage: (_host, payload, callback) => {
      sent.push(payload);
      callback({ ok: true });
    },
    onInstalled: { addListener: () => {} },
    onStartup: { addListener: (listener) => startupListeners.push(listener) }
  },
  alarms: {
    get: async () => null,
    create: () => {},
    onAlarm: { addListener: () => {} }
  }
};
const context = {
  AbortController,
  chrome,
  clearTimeout,
  console,
  TextEncoder,
  crypto: {
    randomUUID: () => {
      randomCalls += 1;
      return randomCalls === 1
        ? 'aaaaaaaa-0000-0000-0000-000000000000'
        : 'bbbbbbbb-0000-0000-0000-000000000000';
    },
    subtle: webcrypto.subtle
  },
  fetch: async () => ({ ok: true, status: 200, json: async () => ({ account: {} }) }),
  setTimeout
};
vm.runInNewContext(source, context);
for (let index = 0; index < 5; index += 1) {
  await new Promise((resolve) => setImmediate(resolve));
}
startupListeners[0]();
for (let index = 0; index < 5; index += 1) {
  await new Promise((resolve) => setImmediate(resolve));
}
for (const resolve of profileReads) resolve({});
for (let index = 0; index < 10; index += 1) {
  await new Promise((resolve) => setImmediate(resolve));
}
process.stdout.write(JSON.stringify({
  randomCalls,
  storedProfile: state.profile_id,
  sentProfiles: [...new Set(sent.map((payload) => payload.profile_id).filter(Boolean))]
}));
'''
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                harness,
                str(EXTENSION / "background.js")
            ],
            capture_output=True,
            text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)

        self.assertEqual(observed["randomCalls"], 1)
        self.assertEqual(observed["storedProfile"], "aaaaaaaa")
        self.assertEqual(observed["sentProfiles"], ["aaaaaaaa"])

    def test_manifest_removes_codex_and_deepseek_hosts_but_keeps_dormant_hosts(self):
        manifest = json.loads((EXTENSION / "manifest.json").read_text())
        hosts = manifest["host_permissions"]

        self.assertNotIn("https://chatgpt.com/*", hosts)
        self.assertNotIn("https://api.deepseek.com/*", hosts)
        self.assertIn("https://platform.xiaomimimo.com/*", hosts)
        self.assertIn("https://api.z.ai/*", hosts)

    def test_popup_has_only_disabled_retired_vendor_controls(self):
        html = (EXTENSION / "popup.html").read_text()
        script = (EXTENSION / "popup.js").read_text()

        self.assertNotIn("codex", html.lower())
        self.assertNotIn("deepseek", html.lower())
        self.assertNotIn("ds-key", html)
        self.assertRegex(html, r'id="mimo"[^>]*disabled')
        self.assertRegex(html, r'id="glm"[^>]*disabled')
        self.assertGreaterEqual(html.lower().count("retired"), 2)
        self.assertNotIn("vendor_enabled", script)
        self.assertNotIn("deepseek_api_key", script)


class InstallContractTests(unittest.TestCase):
    def run_isolated_installer(
        self,
        extension_ids,
        home_name="home",
        bootstrap_exit=0,
        service_loaded=False,
        corrupt_staged_manifest=False,
        corrupt_staged_plist=False
    ):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            copied_native_host = sandbox / "project" / "native-host"
            shutil.copytree(NATIVE_HOST, copied_native_host)
            fake_home = sandbox / home_name
            fake_tmp = sandbox / "tmp"
            fake_tmp.mkdir()

            launchctl_log = sandbox / "launchctl.log"
            launchctl_shim = sandbox / "launchctl"
            launchctl_shim.write_text(
                "#!/bin/bash\n"
                "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_LOG\"\n"
                "if [[ \"$1\" == \"print\" ]]; then exit \"$LAUNCHCTL_PRINT_EXIT\"; fi\n"
                "if [[ \"$1\" == \"bootstrap\" ]]; then exit \"$LAUNCHCTL_BOOTSTRAP_EXIT\"; fi\n"
                "exit 0\n"
            )
            launchctl_shim.chmod(0o700)

            installer = copied_native_host / "install.sh"
            installer_source = installer.read_text().replace("/bin/launchctl", str(launchctl_shim))
            if corrupt_staged_manifest:
                marker = "/usr/bin/python3 -c 'import json, sys; [json.load(open(path, encoding=\"utf-8\")) for path in sys.argv[1:]]' \"$STAGED_MANIFEST\""
                installer_source = installer_source.replace(
                    marker,
                    "printf '{' > \"$STAGED_MANIFEST\"\n" + marker
                )
            if corrupt_staged_plist:
                marker = "/usr/bin/plutil -lint \"$STAGED_PLIST\" >/dev/null"
                installer_source = installer_source.replace(
                    marker,
                    "printf '<' > \"$STAGED_PLIST\"\n" + marker
                )
            installer.write_text(installer_source)

            deployed = {
                "receiver": fake_home / ".claude/scripts/cc-quota-fetcher-host/quota-receiver.py",
                "poller": fake_home / ".claude/scripts/cc-quota-fetcher-host/quota_poller.py",
                "keychain": fake_home / ".claude/scripts/cc-quota-fetcher-host/keychain.py",
                "manifest": fake_home / "Library/Application Support/Google/Chrome/NativeMessagingHosts/com.cc.quota_writer.json",
                "plist": fake_home / "Library/LaunchAgents/com.cc.quota-poller.plist"
            }
            for name, path in deployed.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"sentinel-{name}".encode())
            before = {name: path.read_bytes() for name, path in deployed.items()}

            environment = os.environ.copy()
            environment.update({
                "HOME": str(fake_home),
                "LAUNCHCTL_LOG": str(launchctl_log),
                "LAUNCHCTL_BOOTSTRAP_EXIT": str(bootstrap_exit),
                "LAUNCHCTL_PRINT_EXIT": "0" if service_loaded else "1",
                "TMPDIR": str(fake_tmp)
            })
            result = subprocess.run(
                ["/bin/bash", "-x", str(installer), *extension_ids],
                capture_output=True,
                text=True,
                env=environment
            )
            after = {
                name: path.read_bytes() if path.exists() else None
                for name, path in deployed.items()
            }
            launchctl_calls = launchctl_log.read_text() if launchctl_log.exists() else ""

        return result, before, after, launchctl_calls

    def test_native_python_is_compatible_with_system_python_3_9(self):
        paths = [str(NATIVE_HOST / name) for name in ["keychain.py", "quota-receiver.py", "quota_poller.py"]]
        result = subprocess.run(
            [
                "/usr/bin/python3",
                "-c",
                "import pathlib,sys; [compile(pathlib.Path(path).read_text(), path, 'exec') for path in sys.argv[1:]]",
                *paths
            ],
            capture_output=True,
            text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_launch_agent_runs_one_shot_every_thirty_seconds(self):
        with (NATIVE_HOST / "com.cc.quota-poller.plist").open("rb") as handle:
            plist = plistlib.load(handle)

        self.assertEqual(plist["Label"], "com.cc.quota-poller")
        self.assertEqual(plist["StartInterval"], 30)
        self.assertTrue(plist["RunAtLoad"])
        self.assertNotIn("KeepAlive", plist)
        self.assertEqual(plist["ProgramArguments"][-1], "--once")

    def test_too_many_extension_ids_leave_deployment_unchanged_without_launchctl(self):
        result, before, after, launchctl_calls = self.run_isolated_installer(
            ["a" * 32, "b" * 32, "c" * 32]
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage:", result.stderr)
        self.assertEqual(after, before)
        self.assertEqual(launchctl_calls, "")

    def test_invalid_extension_id_leaves_deployment_unchanged_without_launchctl(self):
        result, before, after, launchctl_calls = self.run_isolated_installer(
            ["a" * 32, "invalid-extension-id"]
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid extension id", result.stderr)
        self.assertEqual(after, before)
        self.assertEqual(launchctl_calls, "")

    def test_invalid_staged_manifest_leaves_deployment_unchanged_without_launchctl(self):
        result, before, after, launchctl_calls = self.run_isolated_installer(
            ["a" * 32],
            corrupt_staged_manifest=True
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(after, before)
        self.assertEqual(launchctl_calls, "")

    def test_invalid_staged_plist_leaves_deployment_unchanged_without_launchctl(self):
        result, before, after, launchctl_calls = self.run_isolated_installer(
            ["a" * 32],
            corrupt_staged_plist=True
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(after, before)
        self.assertEqual(launchctl_calls, "")

    def test_home_path_with_ampersand_renders_exact_manifest_and_plist_paths(self):
        result, _, after, launchctl_calls = self.run_isolated_installer(
            ["a" * 32],
            home_name="home&name"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(after["manifest"])
        plist = plistlib.loads(after["plist"])
        self.assertIn("home&name", manifest["path"])
        self.assertNotIn("__RECEIVER_PATH__", manifest["path"])
        self.assertIn("home&name", plist["ProgramArguments"][0])
        self.assertNotIn("__POLLER_PATH__", plist["ProgramArguments"][0])
        self.assertIn("bootstrap", launchctl_calls)

    def test_bootstrap_failure_restores_all_deployed_files(self):
        result, before, after, launchctl_calls = self.run_isolated_installer(
            ["a" * 32],
            bootstrap_exit=42,
            service_loaded=True
        )

        self.assertEqual(result.returncode, 42)
        self.assertEqual(after, before)
        self.assertIn("bootout", launchctl_calls)
        self.assertGreaterEqual(launchctl_calls.count("bootstrap"), 2)

    def test_installer_deploys_keychain_before_python_entrypoints(self):
        source = (NATIVE_HOST / "install.sh").read_text()
        destinations = re.search(r"DESTINATIONS=\((?P<items>[^\n]+)\)", source)
        self.assertIsNotNone(destinations)
        items = destinations.group("items")

        self.assertLess(items.index("$DEPLOYED_KEYCHAIN"), items.index("$DEPLOYED_RECEIVER"))
        self.assertLess(items.index("$DEPLOYED_KEYCHAIN"), items.index("$DEPLOYED_POLLER"))

    def test_installer_preflight_executes_before_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            bash_env = Path(directory) / "bash-env"
            bash_env.write_text("mkdir() { exit 97; }\n")
            environment = os.environ.copy()
            environment["BASH_ENV"] = str(bash_env)
            result = subprocess.run(
                ["/bin/bash", str(NATIVE_HOST / "install.sh"), "a" * 32],
                capture_output=True,
                text=True,
                env=environment
            )

        self.assertEqual(result.returncode, 97, result.stderr or result.stdout)

    def test_installer_deploys_native_files_without_statusline_side_effects(self):
        source = (NATIVE_HOST / "install.sh").read_text()

        for required in [
            "quota-receiver.py",
            "quota_poller.py",
            "keychain.py",
            "com.cc.quota-poller.plist",
            "launchctl bootout",
            "launchctl bootstrap",
            "launchctl kickstart"
        ]:
            self.assertIn(required, source)
        self.assertNotIn("cc-statusline", source)
        self.assertNotIn("statusline", source.lower())


if __name__ == "__main__":
    unittest.main()
