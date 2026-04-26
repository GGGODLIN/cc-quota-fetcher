# cc-quota-fetcher

Push claude.ai usage quota into local cache files for statusline / external consumption.

## Why

The goal: a statusline showing live usage percentages for two Claude accounts side by side. The path to get there ruled out three simpler approaches before landing on a Chrome extension.

**Approach 1 — Poll `/api/oauth/usage` from the shell.** Claude Code stores its OAuth bearer token in macOS keychain, so a shell script can read it and `curl` the endpoint. This works for one account at low frequency, but the endpoint enforces a per-token sliding-window rate limit (5–30 min cooling). Two accounts polling every 30s hits 429 quickly, and once you hit the limit your numbers go stale until the cooling window slides.

**Approach 2 — Hit the web endpoint from the shell.** `claude.ai/api/organizations/<org>/usage` returns the same JSON schema with cookie auth, and lives in a separate, much looser throttle pool. Reading the session cookie out of Chrome's cookies database is a well-known recipe (PBKDF2 + AES-CBC with a key in the macOS keychain), so I tried that. Two problems killed it:
- Chrome 147 (and likely earlier) on macOS no longer encrypts cookies with the old PBKDF2 + AES-CBC scheme. The `v10` prefix is still present but the inner format is different; existing decrypters output binary garbage. Reverse-engineering the new scheme to keep up with Chrome releases isn't worth the maintenance.
- Even with the cookie in hand, Cloudflare blocks plain `curl` to `claude.ai`. It checks TLS JA3/JA4 fingerprints and HTTP/2 frame ordering, not just cookies. `cf_clearance` cookies are bound to the originating client fingerprint, so copying them out of Chrome doesn't transfer. `curl-impersonate` partially works but is in an arms race with Cloudflare's fingerprint database.

**Approach 3 — Read what Chrome already fetched.** Chrome itself retrieves usage data when you visit `claude.ai/settings/usage`. If we let Chrome do the fetching (real client, real cookies, real fingerprints) and just consume the result locally, both problems disappear. This is what a Chrome extension plus native messaging is for.

So: a small extension runs in each authed Chrome profile, fetches usage on a timer, and pipes the JSON through a native messaging host into `~/.claude/cache/`. A shell script reads the cache files and renders the statusline. Chrome handles auth and Cloudflare; the extension is ~80 lines; the receiver is stdlib Python. The main caveat is that Chrome must be running for the cache to stay fresh.

## Architecture

```
chrome (profile A)                   chrome (profile B)
  └─ extension                         └─ extension
       │ alarm 1 min                        │
       ▼                                    ▼
  fetch /api/bootstrap (email + org_id)  same
       │                                    │
       ▼                                    ▼
  fetch /api/organizations/<org>/usage   same
       │ result                              │
       ▼                                    ▼
  chrome.runtime.sendNativeMessage ────► native-host/quota-receiver.py
                                              └─ stdin JSON → write
                                                  ~/.claude/cache/quota-<email>.json
                                                  ~/.claude/cache/quota-<email>.status (on failure)
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

## Install

### 1. Load the extension in each Chrome profile

For each profile that should be tracked:

1. Open `chrome://extensions/`
2. Toggle **Developer mode** on
3. Click **Load unpacked**, select the `extension/` directory
4. Note the **extension ID** that Chrome assigns (32-char hex)

### 2. Install the native messaging host

```bash
./native-host/install.sh <extension-id-1> [<extension-id-2>]
```

This:
1. Copies the receiver to `~/.claude/scripts/cc-quota-fetcher-host/quota-receiver.py` (deploy path).
2. Writes `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.cc.quota_writer.json` pointing at the deployed receiver, with allowed extension IDs.

**Why deploy outside the project directory?** macOS TCC (Sonoma+) blocks Chrome from spawning processes located under `~/Desktop/`, `~/Documents/`, `~/Downloads/`. Chrome silently fails to exec the host (no log entry) and the service worker reports `Native host has exited`. Deploying to `~/.claude/scripts/` (not TCC-protected) sidesteps this. Re-run `install.sh` after editing the receiver to redeploy.

### 3. Reload the extension

In `chrome://extensions/`, hit the reload button on each loaded extension so it picks up the new native host.

### 4. Verify

After ~1 minute, check `~/.claude/cache/`:

```bash
ls -la ~/.claude/cache/
cat ~/.claude/cache/quota-*.json | jq '.fetched_at, .data.five_hour.utilization'
```

If nothing appears, check `~/.claude/cache/quota-receiver.log` for receiver errors.

## Caveats

- **Chrome must be running.** Service worker only fires alarms while Chrome is alive. Closed Chrome → cache goes stale.
- **MV3 service worker is ephemeral.** `chrome.alarms` wakes the worker, but there is 30-60s jitter on the period.
- **Dev-mode unpacked extension.** Chrome shows a "Disable developer mode extensions" warning bar occasionally.
- **Native messaging is Chrome-specific.** Switching to Arc/Brave/Firefox requires re-implementing the host registration.
- **Per-profile install.** Each Chrome profile that should fetch needs the extension loaded separately; each gets its own extension ID.
