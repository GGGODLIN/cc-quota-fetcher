# Architecture

## Components

### Extension (`extension/`)

- `manifest.json` uses Manifest V3 with `alarms`, `storage`, and `nativeMessaging`.
- Active host access is limited to `claude.ai`. The MiMo and GLM host permissions remain only for dormant functions that are not called by `tick()`.
- `background.js` keeps the existing `fetchAnthropicQuota(pid)` implementation unchanged. Each tick resolves the profile ID and calls only that fetcher.
- Browser-side Codex and DeepSeek network code is removed.
- `migrateDeepSeekKey()` runs independently from the Claude tick. If `deepseek_api_key` exists in `chrome.storage.local` and migration version 1 is not complete, it sends one `migrate-deepseek-key-v1` native message. After a successful native reply, it disables the retired DeepSeek setting, clears the profile-scoped legacy cache, and records `deepseek_key_migration_version = 1`. It never automatically deletes the browser key, so a value changed at any point after the migration snapshot remains available for manual resolution.
- The popup exposes no Codex or DeepSeek controls. MiMo and GLM remain visible only as disabled retired entries.

### Native messaging receiver (`native-host/quota-receiver.py`)

The receiver reads one Chrome Native Messaging frame and exits. Existing Claude cache messages remain supported. The migration handler accepts only the fixed `migrate-deepseek-key-v1` message, validates the profile ID and secret shape, and calls `keychain.py` with fixed service `com.cc.quota-fetcher.deepseek-api-key` and account `default`.

`keychain.py` calls macOS Security.framework directly through stdlib `ctypes`; it does not spawn a subprocess. Its manual update mode reads the value from stdin without printing it. Migration calls `SecKeychainAddGenericPassword` directly without a read-before-write check. A duplicate item is read back and compared: a matching value is idempotent, while a different value returns `profile-conflict`. Migration replies contain only `{ok: true}` or `{ok: false, reason: <fixed-code>}`. Successful receiver invocations are no longer appended to the receiver log, and unexpected failures are reduced to `internal-error` without exception text.

The receiver uses separate vendor permissions. `deepseek` is accepted only by `vendor-clear` so migration can remove old profile-scoped files; `vendor-balance`, `vendor-plan`, and `vendor-status` reject it. `codex` is not accepted by the receiver.

### Native quota poller (`native-host/quota_poller.py`)

The poller is a one-shot process. `com.cc.quota-poller.plist` starts `quota_poller.py --once` at load and every 30 seconds without `KeepAlive`. A nonblocking `flock` on `~/.claude/cache/cc-quota-poller.lock` makes overlapping scheduled or manual runs exit successfully without writing a failure status.

Codex flow:

1. Reread `~/.codex/auth.json` at the start of every poll and reject API-key mode.
2. Call `https://chatgpt.com/backend-api/wham/usage` once with bearer authorization and `ChatGPT-Account-Id`.
3. Treat `auth.json` as read-only. The poller never refreshes, merges, or writes credentials; TokenBar or the Codex CLI owns refresh.
4. Return fixed `unauthorized` and `forbidden` status reasons for `401` and `403` without retrying.
5. Classify `18000`-second and `604800`-second windows as `session` and `weekly`, independent of primary or secondary position.
6. Preserve only quota fields: `plan_type`, `session`, `weekly`, `additional_rate_limits`, and `credits`.

DeepSeek flow:

1. Read the API key from macOS Keychain.
2. Call `https://api.deepseek.com/user/balance`.
3. Preserve the `balance_infos` array with only `currency` and `total_balance`.

Every HTTP request has a 10-second timeout. A `429` parses either delta-seconds or an HTTP-date from `Retry-After`; missing or invalid values fall back to 300 seconds. Each provider stores its own `not_before` deadline, so one provider's backoff or failure does not block the other.

The Codex OAuth request and window classification are based on [TokenBar v1.5.0](https://github.com/Nanako0129/TokenBar/releases/tag/v1.5.0). Repository tests use synthetic responses and do not establish live endpoint availability.

### Installer (`native-host/install.sh`)

The installer validates every extension ID against `^[a-p]{32}$` and validates the source scripts and templates. It renders the native-host manifest and LaunchAgent plist in a temporary staging directory and validates both generated files before creating or replacing deployment destinations. After the complete preflight succeeds, it atomically deploys the receiver, poller, Keychain module, staged native-host manifest, and staged LaunchAgent plist. It bootouts, bootstraps, and kickstarts only `com.cc.quota-poller`; it has no statusline deployment or daemon operations.

## Cache contract

```
~/.claude/cache/
├── quota-<email-safe>.json
├── quota-<email-safe>.status
├── vendor-codex-local.json
├── vendor-codex-local.status
├── vendor-deepseek-local.json
├── vendor-deepseek-local.status
├── cc-quota-poller-state.json
└── cc-quota-poller.lock
```

Claude keeps its existing envelope and tab-separated status contract. Native provider success files contain `{fetched_at: <epoch>, data: <deidentified-provider-data>}`. Native status files contain `{failed_at: <epoch>, reason: <fixed-code>}`. State contains provider-scoped `not_before` epochs.

Native JSON, status, and state writes use same-directory temporary files, mode `0600`, `fsync`, and `os.replace`. Codex `auth.json` is never written. Success removes the matching native status file. Failure leaves the last-good JSON untouched.

## Threat model

- **Single writer per cache**: the extension owns Claude files; the poller owns fixed local Codex and DeepSeek files.
- **Scoped extension access**: Codex and DeepSeek host permissions are absent. Dormant MiMo and GLM functions are not reachable from the tick or popup.
- **Native allowlist**: only configured extension IDs can invoke the receiver.
- **Secret boundary**: Codex tokens stay in `auth.json`; the DeepSeek key stays in Keychain after migration. Neither appears in cache, status, state, log, response, or subprocess arguments.
- **No overlap**: launchd scheduling and the poller lock prevent concurrent writers.
- **Bounded failures**: status and log output use fixed codes rather than remote bodies, stderr, or exception text.

## Failure modes and detection

| Failure | Effect | Detection |
|---|---|---|
| Chrome closed | Claude caches stop updating; native caches continue | Claude cache age only |
| Claude cookie expired | Claude status is updated | Matching `quota-*.status` |
| Codex auth missing or invalid | Last-good Codex JSON remains | `vendor-codex-local.status` fixed reason |
| DeepSeek Keychain item missing | Last-good DeepSeek JSON remains | `vendor-deepseek-local.status` fixed reason |
| Provider returns `429` | Only that provider pauses until `not_before` | `cc-quota-poller-state.json` |
| Previous poll still running | New process exits without network or status writes | Lock owner remains active |
| DeepSeek profiles disagree | Browser key is retained for resolution | Migration reply `profile-conflict` |
| LaunchAgent not loaded | Native cache times stop advancing | `launchctl print gui/$UID/com.cc.quota-poller` |
