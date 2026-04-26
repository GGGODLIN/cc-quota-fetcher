# cc-quota-fetcher

Push claude.ai usage quota into local cache files for statusline / external consumption.

## Why

`api.anthropic.com/api/oauth/usage` rate limits per-token (sliding window 5-30 min). For multi-account quota visibility on a statusline, polling oauth/usage hits 429 frequently. The web UI uses a different endpoint with a different throttle pool, but Cloudflare blocks plain CLI curl (TLS fingerprint). Solution: a Chrome extension fetches inside an already-authed browser, then a native messaging host writes the result to local cache files.

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
