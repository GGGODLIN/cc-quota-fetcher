#!/usr/bin/env python3
"""Poll the Anthropic OAuth usage API using local keychain tokens.

Replaces the chrome-extension path as the source for Anthropic quota data.
Writes the exact cache contract quota-receiver.py produces, so usage-color.sh
consumes it unchanged:

  ~/.claude/cache/quota-<email>.json   = {fetched_at, email, org_id, data}
  ~/.claude/cache/quota-<email>.status = "<HH:MM:SS>\\t<reason>"

Success removes the .status sentinel; failure leaves the .json in place so the
statusline can render its own staleness warning.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.utils import parsedate_to_datetime
from datetime import timezone
from pathlib import Path
from typing import Any

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
HTTP_TIMEOUT_SECONDS = 10
DEFAULT_RETRY_AFTER_SECONDS = 300
MIN_RATE_LIMIT_BACKOFF_SECONDS = 1800
MAX_BACKOFF_SECONDS = 86400
# extension 正常時 cache age 最多約 60s（30s 一輪 + alarm jitter 實測 < 5s），
# 90s 足以讓位。刻意壓在 StartInterval=180 的一半以下：門檻越靠近 180，越容易
# 把 poller 自己上一輪的寫入誤判成「別人剛寫過」而跳過自己的排程。
# extension 斷線後最壞 90 + 180 = 270s 接手，早於 usage-color.sh 的
# OLD_AFTER_SECONDS=400，使用者不會看到 [N old]。
DEFER_TO_CACHE_SECONDS = 90
KEYCHAIN_SERVICE = "Claude Code-credentials"
CACHE_DIR = Path.home() / ".claude" / "cache"
STATE_PATH = CACHE_DIR / "anthropic-quota-poller-state.json"
LOCK_PATH = CACHE_DIR / "anthropic-quota-poller.lock"


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid json constant: {value}")


class PollError(RuntimeError):
    def __init__(self, code: str, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    fd_closed = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            fd_closed = True
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        if not fd_closed:
            try:
                os.close(descriptor)
            except OSError:
                pass
        temporary_path.unlink(missing_ok=True)
        raise


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(descriptor, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def load_state() -> dict[str, Any]:
    try:
        payload = json.loads(
            STATE_PATH.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {"accounts": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), dict):
        return {"accounts": {}}
    return payload


def save_state(state: dict[str, Any]) -> None:
    atomic_write_text(STATE_PATH, json.dumps(state, allow_nan=False))


def is_backed_off(state: dict[str, Any], key: str, now: int) -> bool:
    if not isinstance(state, dict):
        return False
    accounts = state.get("accounts")
    if not isinstance(accounts, dict):
        return False
    entry = accounts.get(key)
    if not isinstance(entry, dict):
        return False
    not_before = entry.get("not_before")
    return (
        isinstance(not_before, (int, float))
        and not isinstance(not_before, bool)
        and math.isfinite(not_before)
        and now < not_before
    )


def set_not_before(state: dict[str, Any], key: str, not_before: int) -> None:
    accounts = state.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
        state["accounts"] = accounts
    accounts[key] = {"not_before": int(not_before)}


def clear_not_before(state: dict[str, Any], key: str) -> None:
    accounts = state.get("accounts")
    if isinstance(accounts, dict):
        accounts.pop(key, None)


def parse_retry_after(value: str | None, now: int) -> int:
    if value is not None:
        stripped = value.strip()
        if stripped.isascii() and stripped.isdigit():
            trimmed = stripped.lstrip("0") or "0"
            if len(trimmed) > 6:
                return MAX_BACKOFF_SECONDS
            return min(MAX_BACKOFF_SECONDS, int(trimmed))
        try:
            parsed = parsedate_to_datetime(stripped)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delta = math.ceil(parsed.timestamp() - now)
            return min(MAX_BACKOFF_SECONDS, max(0, delta))
        except (TypeError, ValueError, OverflowError, OSError, IndexError):
            pass
    return DEFAULT_RETRY_AFTER_SECONDS


def email_safe(email: str) -> str:
    return email.replace("@", "_at_").replace("/", "_")


def keychain_service_for(config_dir: Path) -> str:
    if config_dir == Path.home() / ".claude":
        return KEYCHAIN_SERVICE
    digest = hashlib.sha256(str(config_dir).encode("utf-8")).hexdigest()[:8]
    return f"{KEYCHAIN_SERVICE}-{digest}"


def identity_path_for(config_dir: Path) -> Path:
    # default 帳號的身分檔在 $HOME/.claude.json，不是 ~/.claude/.claude.json
    # （後者是殘留檔、會滯留舊帳號）。兩段式慣例見 scripts/lib/resolve-account.sh
    if config_dir == Path.home() / ".claude":
        return Path.home() / ".claude.json"
    return config_dir / ".claude.json"


def read_keychain_oauth(service: str) -> dict[str, Any] | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    if result.returncode != 0 or not result.stdout or not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    oauth = payload.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else None


def read_account_identity(config_dir: Path) -> tuple[str | None, str | None]:
    try:
        payload = json.loads(
            identity_path_for(config_dir).read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    account = payload.get("oauthAccount")
    if not isinstance(account, dict):
        return None, None
    email = account.get("emailAddress")
    org_id = account.get("organizationUuid")
    return (
        email if isinstance(email, str) and email and "\x00" not in email else None,
        org_id if isinstance(org_id, str) and org_id else None,
    )


def discover_config_dirs() -> list[Path]:
    home = Path.home()
    found = []
    default = home / ".claude"
    if identity_path_for(default).is_file():
        found.append(default)
    for entry in sorted(home.glob(".claude-*")):
        if entry.is_dir() and (entry / ".claude.json").is_file():
            found.append(entry)
    return found


def fetch_usage(access_token: str, now: int) -> dict[str, Any]:
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "cc-anthropic-quota-poller",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read()
            status = response.status
            headers = dict(response.headers.items())
    except urllib.error.HTTPError as error:
        status = error.code
        headers = dict(error.headers.items()) if error.headers else {}
        error.close()
        if status == 429:
            retry_after = None
            for key, value in headers.items():
                if key.lower() == "retry-after":
                    retry_after = value
                    break
            raise PollError(
                "rate-limited",
                max(parse_retry_after(retry_after, now), MIN_RATE_LIMIT_BACKOFF_SECONDS),
            )
        if status == 401:
            raise PollError("unauthorized")
        if status == 403:
            raise PollError("forbidden")
        raise PollError("http-error")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PollError("network-error")
    if not 200 <= status < 300:
        raise PollError("http-error")
    try:
        body = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise PollError("invalid-json")
    if not isinstance(body, dict):
        raise PollError("invalid-schema")
    return body


def write_success(email: str, org_id: str | None, body: dict[str, Any], stamp: str) -> None:
    safe = email_safe(email)
    envelope = {"fetched_at": stamp, "email": email, "org_id": org_id, "data": body}
    atomic_write_text(CACHE_DIR / f"quota-{safe}.json", json.dumps(envelope, allow_nan=False))
    (CACHE_DIR / f"quota-{safe}.status").unlink(missing_ok=True)


def write_failure(email: str, reason: str, stamp: str) -> None:
    safe = email_safe(email)
    atomic_write_text(CACHE_DIR / f"quota-{safe}.status", f"{stamp}\t{reason}")


def cache_covers_account(email: str, now: int) -> bool:
    # 另一個 writer（cc-quota-fetcher chrome extension，30s 一輪）剛寫過就讓位，
    # 這輪不打 API。要求帶 limits 才算完整——per-model（weekly_scoped）只存在於
    # 這個陣列，缺了就得由本 poller 自己抓，否則 Fable 那格會斷更。
    path = CACHE_DIR / f"quota-{email_safe(email)}.json"
    try:
        age = now - int(path.stat().st_mtime)
    except OSError:
        return False
    if age < 0 or age >= DEFER_TO_CACHE_SECONDS:
        return False
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    return isinstance(data.get("limits"), list)


def poll_account(config_dir: Path, state: dict[str, Any], now: int, stamp: str) -> bool:
    email, org_id = read_account_identity(config_dir)
    if email is None:
        return False
    if cache_covers_account(email, now):
        # 讓位時清掉本 poller 先前留下的哨兵，否則 usage-color.sh 會一直畫 ⚠
        # 蓋掉那份其實新鮮的資料。瀏覽器寫入成功不會解除本 poller 的退避期限。
        (CACHE_DIR / f"quota-{email_safe(email)}.status").unlink(missing_ok=True)
        return True
    oauth = read_keychain_oauth(keychain_service_for(config_dir))
    if oauth is None:
        return False
    access_token = oauth.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        return False
    expires_at = oauth.get("expiresAt")
    if (
        isinstance(expires_at, (int, float))
        and not isinstance(expires_at, bool)
        and math.isfinite(expires_at)
    ):
        if expires_at <= now * 1000:
            return False
    if is_backed_off(state, email, now):
        return True
    try:
        body = fetch_usage(access_token, now)
    except PollError as error:
        if error.retry_after is not None:
            set_not_before(state, email, now + error.retry_after)
        write_failure(email, error.code, stamp)
        return True
    except Exception:
        write_failure(email, "internal-error", stamp)
        return True
    clear_not_before(state, email)
    write_success(email, org_id, body, stamp)
    return True


def run_once() -> int:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(LOCK_PATH)
    if lock is None:
        return 0
    try:
        now = int(time.time())
        stamp = datetime.now().strftime("%H:%M:%S")
        state = load_state()
        seen: set[str] = set()
        try:
            for config_dir in discover_config_dirs():
                try:
                    email, _ = read_account_identity(config_dir)
                    if email is None or email in seen:
                        continue
                    if poll_account(config_dir, state, now, stamp):
                        seen.add(email)
                except Exception:
                    continue
        finally:
            save_state(state)
    finally:
        lock.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.once:
        parser.error("--once is required")
    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
