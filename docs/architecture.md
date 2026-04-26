# Architecture

## Components

### Extension (`extension/`)

- `manifest.json`: Manifest V3, requests `alarms`, `storage`, `nativeMessaging`, host `claude.ai`.
- `background.js`: service worker. On install / startup creates an alarm with `periodInMinutes: 1`. On each tick:
  1. `fetch /api/bootstrap` → email + memberships (org_id list).
  2. Pick primary org (prefer team-type, fallback first non-empty UUID).
  3. `fetch /api/organizations/<org>/usage` with `credentials: 'include'`.
  4. Send result via `chrome.runtime.sendNativeMessage` to host `com.cc.quota_writer`.

Both fetches rely on the browser's session cookie + Cloudflare clearance, so they only work inside an authenticated Chrome profile. There is no token handling in the extension code.

### Native Host (`native-host/`)

- `manifest.template.json`: schema for Chrome's native messaging host registration. Placeholders for receiver path and allowed extension IDs.
- `install.sh`: substitutes placeholders and writes the manifest into `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.cc.quota_writer.json`.
- `quota-receiver.py`: stdio loop. Reads one length-prefixed JSON message (Chrome native messaging wire format: 4-byte little-endian length + UTF-8 JSON), writes the quota envelope or status sentinel to `~/.claude/cache/`, replies with `{ok: bool, ...}`.

The receiver runs as a fresh process per message (Chrome spawns a new process for each `sendNativeMessage` call). State is filesystem-only.

## Cache contract

```
~/.claude/cache/
├── quota-<email-safe>.json     successful fetch (envelope: fetched_at, email, org_id, data)
├── quota-<email-safe>.status   failure sentinel: "<HH:MM:SS>\t<reason>"
└── quota-receiver.log          append-only log of receiver invocations
```

`<email-safe>` = email with `@` replaced by `_at_`.

Success removes the `.status` sentinel. Failure writes only the sentinel; the previous `.json` is left untouched (it is now stale).

## Threat model

- **Extension permissions**: only `host_permissions: claude.ai`. Cannot read other sites' cookies, cannot exfiltrate data outside the session cookie scope.
- **Native messaging allowlist**: `manifest.template.json` lists explicit extension IDs. Other extensions cannot invoke this host.
- **Receiver**: only writes to `~/.claude/cache/`, never executes arbitrary code. JSON parsing is the only attack surface.
- **No secrets in repo**: `.gitignore` excludes session-key / cookie / pem files. The receiver does not handle any secrets.

## Failure modes & detection

| Failure | Effect | How to detect |
|---|---|---|
| Chrome closed | No new cache writes | Stale `quota-*.json` mtime |
| Cookie expired | extension gets 401 | `.status` reason: `auth fail` |
| Extension disabled | No invocations | No new log entries in `quota-receiver.log` |
| Native host misregistered | `chrome.runtime.lastError` in service worker | Service worker console (chrome://extensions DevTools) |
| Receiver crash | No reply, but msg still written if before crash | `quota-receiver.log` error entries |

## Future extensions

- Track `seven_day_sonnet` separately for sonnet-only quota.
- Push to other UIs (e.g., menu bar app) via additional cache files.
- Refresh frequency tuning: `chrome.alarms` minimum is 30s (1min for packed extensions).
