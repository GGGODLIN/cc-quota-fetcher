# cc-quota-fetcher

Write Claude, Codex, and DeepSeek quota data into local cache files for statusline and external consumption.

## Why

The goal: a statusline showing live usage percentages for two Claude accounts side by side. The path to get there ruled out three simpler approaches before landing on a Chrome extension.

**Approach 1 — Poll `/api/oauth/usage` from the shell.** Claude Code stores its OAuth bearer token in macOS keychain, so a shell script can read it and `curl` the endpoint. This works for one account at low frequency, but the endpoint enforces a per-token sliding-window rate limit (5–30 min cooling). Two accounts polling every 30s hits 429 quickly, and once you hit the limit your numbers go stale until the cooling window slides.

**Approach 2 — Hit the web endpoint from the shell.** `claude.ai/api/organizations/<org>/usage` returns the same JSON schema with cookie auth, and lives in a separate, much looser throttle pool. Reading the session cookie out of Chrome's cookies database is a well-known recipe (PBKDF2 + AES-CBC with a key in the macOS keychain), so I tried that. Two problems killed it:
- Chrome 147 (and likely earlier) on macOS no longer encrypts cookies with the old PBKDF2 + AES-CBC scheme. The `v10` prefix is still present but the inner format is different; existing decrypters output binary garbage. Reverse-engineering the new scheme to keep up with Chrome releases isn't worth the maintenance.
- Even with the cookie in hand, Cloudflare blocks plain `curl` to `claude.ai`. It checks TLS JA3/JA4 fingerprints and HTTP/2 frame ordering, not just cookies. `cf_clearance` cookies are bound to the originating client fingerprint, so copying them out of Chrome doesn't transfer. `curl-impersonate` partially works but is in an arms race with Cloudflare's fingerprint database.

**Approach 3 — Let Chrome fetch Claude, but keep local-token providers native.** Chrome can call the Claude web endpoint with its real session, TLS fingerprint, and Cloudflare state. Codex already maintains OAuth credentials in `~/.codex/auth.json`, while DeepSeek uses an API key that belongs in macOS Keychain. Those providers do not need browser sessions.

The resulting split has one writer per cache. The extension fetches only Claude and uses native messaging for cache writes. A separate one-shot Python poller owns the Codex and DeepSeek local caches. A LaunchAgent starts the poller every 30 seconds, while a nonblocking file lock prevents overlapping runs. Chrome must remain open only for Claude quota updates.

## Architecture

```
Chrome profile A/B
  └─ MV3 extension alarm
       └─ Claude web usage
            └─ Native Messaging
                 └─ quota-receiver.py
                      └─ ~/.claude/cache/quota-<email>.*

launchd StartInterval=30
  └─ quota_poller.py --once
       ├─ ~/.codex/auth.json → Codex usage
       │    └─ ~/.claude/cache/vendor-codex-local.*
       └─ macOS Keychain → DeepSeek balance
            └─ ~/.claude/cache/vendor-deepseek-local.*
```

## Output contract

`~/.claude/cache/quota-<email-safe>.json` (e.g. `quota-you_at_example.com.json`):

```json
{
  "fetched_at": "12:34:56",
  "email": "you@example.com",
  "org_id": "...",
  "data": { /* full /api/organizations/<org>/usage response */ }
}
```

`~/.claude/cache/quota-<email-safe>.status` (only on failure):

```
12:34:56\trate limited
```

Schema of `.data` matches `/api/oauth/usage`: `five_hour.utilization`, `seven_day.utilization`, `extra_usage.*`, etc.

## Native Codex and DeepSeek caches

The native poller writes successful responses as `{fetched_at, data}` and fixed-code failures as `{failed_at, reason}`:

- `~/.claude/cache/vendor-codex-local.json` and `.status`
- `~/.claude/cache/vendor-deepseek-local.json` and `.status`
- `~/.claude/cache/cc-quota-poller-state.json` for per-provider `Retry-After` deadlines

Every JSON, status, and state write uses a same-directory temporary file, mode `0600`, `fsync`, and atomic replacement. A failed poll leaves the previous successful JSON in place.

Codex rereads OAuth credentials from `~/.codex/auth.json` at the start of every poll, sends the bearer token with `ChatGPT-Account-Id`, and classifies quota windows by `limit_window_seconds`: `18000` is the session window and `604800` is the weekly window. The cache keeps `plan_type`, `session`, `weekly`, `additional_rate_limits`, and `credits`, but no token or account identity. The poller treats `auth.json` as read-only: it never refreshes, merges, or writes credentials. TokenBar or the Codex CLI owns credential refresh, while `401` and `403` responses produce fixed `unauthorized` and `forbidden` status reasons.

DeepSeek reads service `com.cc.quota-fetcher.deepseek-api-key`, account `default`, from macOS Keychain and keeps the existing `balance_infos` shape with only `currency` and `total_balance`. The extension performs a one-time migration from `chrome.storage.local`. Migration uses an atomic create-only Keychain operation: a matching duplicate succeeds, a different duplicate returns `profile-conflict`, and no migration can overwrite an existing value. After native confirmation, the extension disables the retired browser writer, clears its legacy cache, and records `deepseek_key_migration_version = 1`. It intentionally retains the browser copy because the popup no longer reads or writes it, and automatic deletion could remove a newer value written after the migration snapshot.

The Codex OAuth request and window mapping follow [TokenBar v1.5.0](https://github.com/Nanako0129/TokenBar/releases/tag/v1.5.0), with tighter local safeguards for a 10-second timeout, persistent `Retry-After`, nonoverlapping runs, and read-only credential ownership. This documents source behavior only; focused tests use synthetic responses and do not verify live provider APIs.

## Native Anthropic poller

`native-host/anthropic_quota_poller.py` polls `/api/oauth/usage` directly with the Claude Code keychain token, which is Approach 1 from the *Why* section. Its own docstring states the intent as replacing the extension path as the source of Anthropic quota data, and it writes the same cache contract `quota-receiver.py` does, so `usage-color.sh` consumes it unchanged. The extension's `fetchAnthropicQuota` still runs and still writes `quota-<email>.json`, so until one of the two is retired this cache has two writers, against the one-writer-per-cache rule the *Architecture* section states. The two do not share a throttle pool — the extension calls `claude.ai/api/organizations/<org>/usage` under the browser session, this poller calls `api.anthropic.com/api/oauth/usage` under the keychain token. It discovers accounts from `~/.claude/.claude.json` plus every `~/.claude-*/.claude.json`, and keys backoff deadlines by email in `~/.claude/cache/anthropic-quota-poller-state.json`.

Approach 1's rate limit is the reason this poller runs at a 600-second interval rather than the extension poller's 30. A `429` carries `Retry-After`, but the endpoint sometimes answers `Retry-After: 0` while still refusing requests, so the honoured deadline is `max(Retry-After, MIN_RATE_LIMIT_BACKOFF_SECONDS)` with the floor at 1800 seconds. Without that floor a zero-valued header sets the deadline to the current second, the next scheduled run fires immediately, each retry keeps the limit warm, and the statusline never leaves `rate-limited`.

Deployment is manual — `native-host/install.sh` owns only the extension path and does not touch this poller or its LaunchAgent:

```bash
cp native-host/anthropic_quota_poller.py ~/.claude/scripts/cc-quota-fetcher-host/anthropic_quota_poller.py
chmod 700 ~/.claude/scripts/cc-quota-fetcher-host/anthropic_quota_poller.py
python3 -c 'import plistlib,sys; src,dst,p=sys.argv[1:]; d=plistlib.load(open(src,"rb")); d["ProgramArguments"][0]=p; plistlib.dump(d,open(dst,"wb"),sort_keys=False)' \
  native-host/com.cc.anthropic-quota-poller.plist \
  ~/Library/LaunchAgents/com.cc.anthropic-quota-poller.plist \
  ~/.claude/scripts/cc-quota-fetcher-host/anthropic_quota_poller.py
launchctl bootout "gui/$(id -u)/com.cc.anthropic-quota-poller" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.cc.anthropic-quota-poller.plist
```

## Install

### 1. Load the extension in each Chrome profile

For each profile that should be tracked:

1. Open `chrome://extensions/`
2. Toggle **Developer mode** on
3. Click **Load unpacked**, select the `extension/` directory
4. Note the **extension ID** that Chrome assigns (32 characters in the `a`–`p` range)

> Optional: edit `extension/icons/icon.svg` (any vector tool — Inkscape, Figma export, hand-edited XML — works), then regenerate the PNGs:
> `python3 extension/icons/generate.py` (requires `brew install librsvg`). The committed PNGs are sufficient for normal use.

### 2. Install the native host and poller

```bash
./native-host/install.sh <extension-id-1> [<extension-id-2>]
```

The installer rejects extension IDs outside `^[a-p]{32}$`, validates the source Python, shell, JSON, and plist files, then renders the native-host manifest and LaunchAgent plist into a temporary staging directory. It validates both staged outputs before creating or replacing any deployment destination. Only after the full preflight succeeds does it atomically install `quota-receiver.py`, `quota_poller.py`, and `keychain.py` under `~/.claude/scripts/cc-quota-fetcher-host/`; install the staged manifest and plist; and restart only the independent `com.cc.quota-poller` LaunchAgent.

The deployment directory is outside `~/Desktop`, `~/Documents`, and `~/Downloads` because macOS privacy controls can prevent Chrome from spawning a native host from those locations.

### 3. Reload the extension once

Reload each unpacked extension after deploying the new receiver. The service worker migrates a stored DeepSeek key without blocking Claude polling. After native confirmation, it disables the retired browser-side DeepSeek path, clears the profile-scoped legacy cache, records migration version 1, and preserves the browser key so a concurrently written newer value cannot be deleted.

### 4. Update or verify the DeepSeek Keychain item

To replace the Keychain value without placing it in command arguments or terminal output:

```bash
read -r -s DEEPSEEK_API_KEY
printf '\n'
printf '%s\n' "$DEEPSEEK_API_KEY" | /usr/bin/python3 "$HOME/.claude/scripts/cc-quota-fetcher-host/keychain.py" --set-stdin
unset DEEPSEEK_API_KEY
```

The helper calls macOS Security.framework directly, so the value stays on stdin and never enters subprocess arguments. To verify that the item is readable without printing it:

```bash
/usr/bin/python3 "$HOME/.claude/scripts/cc-quota-fetcher-host/keychain.py" --check
```

### 5. Restart or uninstall the poller

```bash
/bin/launchctl kickstart -k "gui/$(/usr/bin/id -u)/com.cc.quota-poller"
```

```bash
/bin/launchctl bootout "gui/$(/usr/bin/id -u)/com.cc.quota-poller"
rm -f "$HOME/Library/LaunchAgents/com.cc.quota-poller.plist"
rm -f "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.cc.quota_writer.json"
rm -rf "$HOME/.claude/scripts/cc-quota-fetcher-host"
```

### 6. Inspect nonsecret cache metadata

```bash
ls -la "$HOME/.claude/cache/vendor-codex-local.json" "$HOME/.claude/cache/vendor-deepseek-local.json"
jq '{fetched_at, keys: (.data | keys)}' "$HOME/.claude/cache/vendor-codex-local.json" "$HOME/.claude/cache/vendor-deepseek-local.json"
```

## Caveats

- **Claude still depends on Chrome.** Closing Chrome stops only the Claude cache updates; the native Codex and DeepSeek poller remains independent.
- **MV3 alarms have scheduling jitter.** The extension alarm is not a precise wall-clock timer.
- **The Codex usage endpoint is private.** Its route or response schema may change; fixed-code status files report failures without replacing last-good data.
- **DeepSeek has one local Keychain owner.** A second Chrome profile with a different stored key receives `profile-conflict` and keeps its browser copy for manual resolution.
- **Tests are synthetic.** The repository checks parsing, retries, locking, atomic writes, and read-only auth handling without claiming live API behavior.
