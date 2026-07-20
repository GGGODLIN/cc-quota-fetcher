#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

import keychain

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
HTTP_TIMEOUT_SECONDS = 10
DEFAULT_RETRY_AFTER_SECONDS = 300
SESSION_WINDOW_SECONDS = 18_000
WEEKLY_WINDOW_SECONDS = 604_800


class PollError(RuntimeError):
    def __init__(self, code: str, retry_after: int | None = None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: Any


@dataclass(frozen=True)
class CodexCredentials:
    access_token: str
    account_id: str


def atomic_write_json(path: Path, payload: Any, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2 if pretty else None,
                separators=None if pretty else (",", ":")
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(descriptor, 0o600)
    handle = os.fdopen(descriptor, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def parse_retry_after(value: str | None, now: int) -> int:
    if value is not None:
        stripped = value.strip()
        if stripped.isdigit():
            return max(0, int(stripped))
        try:
            parsed = parsedate_to_datetime(stripped)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0, math.ceil(parsed.timestamp() - now))
        except (TypeError, ValueError, OverflowError):
            pass
    return DEFAULT_RETRY_AFTER_SECONDS


def empty_state() -> dict[str, Any]:
    return {"vendors": {}}


def load_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return empty_state()
    if not isinstance(payload, dict) or not isinstance(payload.get("vendors"), dict):
        return empty_state()
    return payload


def set_not_before(path: Path, vendor: str, not_before: int) -> None:
    state = load_state(path)
    state["vendors"][vendor] = {"not_before": int(not_before)}
    atomic_write_json(path, state)


def clear_not_before(path: Path, vendor: str) -> None:
    state = load_state(path)
    if vendor not in state["vendors"]:
        return
    del state["vendors"][vendor]
    atomic_write_json(path, state)


def is_backed_off(state: dict[str, Any], vendor: str, now: int) -> bool:
    entry = state.get("vendors", {}).get(vendor, {})
    not_before = entry.get("not_before")
    return isinstance(not_before, (int, float)) and not isinstance(not_before, bool) and now < not_before


def header_value(headers: dict[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def decode_json_bytes(raw: bytes, required: bool) -> Any:
    if not raw:
        if required:
            raise PollError("invalid-json")
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if required:
            raise PollError("invalid-json")
        return None


def http_json(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
    timeout: int
) -> HttpResult:
    request_headers = dict(headers)
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method
    )
    try:
        opener = urllib.request.build_opener(NoRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as error:
        raw = b""
        status = error.code
        response_headers = dict(error.headers.items()) if error.headers else {}
        error.close()
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PollError("network-error")
    return HttpResult(
        status,
        response_headers,
        decode_json_bytes(raw, 200 <= status < 300)
    )


def finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalize_window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    used_percent = value.get("used_percent")
    window_seconds = value.get("limit_window_seconds")
    if not finite_number(used_percent) or not 0 <= used_percent <= 100:
        return None
    if not finite_number(window_seconds) or window_seconds <= 0:
        return None
    reset_at = value.get("reset_at")
    if reset_at is not None and (not finite_number(reset_at) or reset_at <= 0):
        return None
    result = {
        "used_percent": used_percent,
        "remaining_percent": max(0, 100 - used_percent),
        "limit_window_seconds": int(window_seconds),
        "reset_at": int(reset_at) if reset_at is not None else None
    }
    reset_after = value.get("reset_after_seconds")
    if finite_number(reset_after) and reset_after >= 0:
        result["reset_after_seconds"] = reset_after
    return result


def normalize_rate_limit(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for name in ("primary_window", "secondary_window"):
        window = normalize_window(value.get(name))
        if window is not None:
            result[name] = window
    return result


def normalize_credits(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result = {}
    for name in ("has_credits", "unlimited", "overage_limit_reached"):
        item = value.get(name)
        if isinstance(item, bool):
            result[name] = item
    if "balance" in value:
        balance = value.get("balance")
        if isinstance(balance, str) or finite_number(balance) or balance is None:
            result["balance"] = balance
    return result or None


def normalize_codex_usage(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise PollError("invalid-schema")
    session = None
    weekly = None
    rate_limit = normalize_rate_limit(body.get("rate_limit"))
    for window in rate_limit.values():
        duration = window["limit_window_seconds"]
        if duration == SESSION_WINDOW_SECONDS and session is None:
            session = window
        if duration == WEEKLY_WINDOW_SECONDS and weekly is None:
            weekly = window
    additional = []
    raw_additional = body.get("additional_rate_limits")
    if isinstance(raw_additional, list):
        for item in raw_additional:
            if not isinstance(item, dict):
                continue
            normalized_limit = normalize_rate_limit(item.get("rate_limit"))
            if not normalized_limit:
                continue
            normalized_item = {"rate_limit": normalized_limit}
            for name in ("limit_name", "metered_feature"):
                value = item.get(name)
                if isinstance(value, str):
                    normalized_item[name] = value
            additional.append(normalized_item)
    plan_type = body.get("plan_type")
    data = {
        "plan_type": plan_type if isinstance(plan_type, str) else None,
        "session": session,
        "weekly": weekly,
        "additional_rate_limits": additional,
        "credits": normalize_credits(body.get("credits"))
    }
    if session is None and weekly is None and not additional and data["credits"] is None:
        raise PollError("invalid-schema")
    return data


def normalize_deepseek_balance(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or not isinstance(body.get("balance_infos"), list):
        raise PollError("invalid-schema")
    balances = []
    for item in body["balance_infos"]:
        if not isinstance(item, dict):
            continue
        currency = item.get("currency")
        total_balance = item.get("total_balance")
        if not isinstance(currency, str) or not currency:
            continue
        if not isinstance(total_balance, str) and not finite_number(total_balance):
            continue
        balances.append({"currency": currency, "total_balance": total_balance})
    return {"balance_infos": balances}


def jwt_payload(token: str | None) -> dict[str, Any] | None:
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return None
    padding = "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def token_value(tokens: dict[str, Any], snake_name: str, camel_name: str) -> str | None:
    for name in (snake_name, camel_name):
        value = tokens.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def read_auth_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        raise PollError("auth-not-found")
    except (OSError, json.JSONDecodeError):
        raise PollError("auth-invalid")
    if not isinstance(payload, dict):
        raise PollError("auth-invalid")
    return payload


def account_id_from_id_token(id_token: str | None) -> str | None:
    payload = jwt_payload(id_token)
    auth = payload.get("https://api.openai.com/auth") if payload else None
    if not isinstance(auth, dict):
        return None
    value = auth.get("chatgpt_account_id")
    return value if isinstance(value, str) and value else None


def credentials_from_auth(path: Path, payload: dict[str, Any]) -> CodexCredentials:
    api_key = payload.get("OPENAI_API_KEY")
    if isinstance(api_key, str) and api_key.strip():
        raise PollError("api-key-auth")
    auth_mode = payload.get("auth_mode")
    if isinstance(auth_mode, str) and auth_mode.lower() not in {"chatgpt", "chat_gpt"}:
        raise PollError("api-key-auth")
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        raise PollError("oauth-tokens-missing")
    access_token = token_value(tokens, "access_token", "accessToken")
    if access_token is None:
        raise PollError("access-token-missing")
    id_token = token_value(tokens, "id_token", "idToken")
    account_id = token_value(tokens, "account_id", "accountId") or account_id_from_id_token(id_token)
    if account_id is None:
        raise PollError("account-id-missing")
    return CodexCredentials(access_token, account_id)


def load_codex_auth(path: Path) -> CodexCredentials:
    return credentials_from_auth(path, read_auth_json(path))


def codex_usage_request(credentials: CodexCredentials, transport: Callable[..., HttpResult]) -> HttpResult:
    return transport(
        CODEX_USAGE_URL,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {credentials.access_token}",
            "ChatGPT-Account-Id": credentials.account_id,
            "User-Agent": "cc-quota-poller"
        },
        body=None,
        timeout=HTTP_TIMEOUT_SECONDS
    )


def poll_codex(
    auth_path: Path,
    now: int,
    transport: Callable[..., HttpResult] = http_json
) -> dict[str, Any]:
    credentials = load_codex_auth(auth_path)
    result = codex_usage_request(credentials, transport)
    if result.status == 429:
        raise PollError(
            "rate-limited",
            parse_retry_after(header_value(result.headers, "Retry-After"), now)
        )
    if result.status == 401:
        raise PollError("unauthorized")
    if result.status == 403:
        raise PollError("forbidden")
    if not 200 <= result.status < 300:
        raise PollError("http-error")
    return normalize_codex_usage(result.body)


def poll_deepseek(
    now: int,
    transport: Callable[..., HttpResult] = http_json,
    key_reader: Callable[[], str | None] = keychain.read_secret
) -> dict[str, Any]:
    try:
        secret = key_reader()
    except keychain.KeychainError:
        raise PollError("keychain-read-failed")
    if not secret:
        raise PollError("key-not-found")
    result = transport(
        DEEPSEEK_BALANCE_URL,
        method="GET",
        headers={"Accept": "application/json", "Authorization": f"Bearer {secret}"},
        body=None,
        timeout=HTTP_TIMEOUT_SECONDS
    )
    if result.status == 429:
        raise PollError(
            "rate-limited",
            parse_retry_after(header_value(result.headers, "Retry-After"), now)
        )
    if result.status == 401:
        raise PollError("unauthorized")
    if result.status == 403:
        raise PollError("forbidden")
    if not 200 <= result.status < 300:
        raise PollError("http-error")
    return normalize_deepseek_balance(result.body)


def cache_paths(cache_dir: Path, vendor: str) -> tuple[Path, Path]:
    base = cache_dir / f"vendor-{vendor}-local"
    return Path(f"{base}.json"), Path(f"{base}.status")


def write_success(cache_dir: Path, vendor: str, now: int, data: dict[str, Any]) -> None:
    cache_path, status_path = cache_paths(cache_dir, vendor)
    atomic_write_json(cache_path, {"fetched_at": now, "data": data})
    status_path.unlink(missing_ok=True)


def write_failure(cache_dir: Path, vendor: str, now: int, reason: str) -> None:
    _, status_path = cache_paths(cache_dir, vendor)
    atomic_write_json(status_path, {"failed_at": now, "reason": reason})


def run_provider(
    vendor: str,
    cache_dir: Path,
    state_path: Path,
    now: int,
    fetcher: Callable[[], dict[str, Any]]
) -> None:
    if is_backed_off(load_state(state_path), vendor, now):
        return
    try:
        data = fetcher()
    except PollError as error:
        if error.retry_after is not None:
            set_not_before(state_path, vendor, now + error.retry_after)
        write_failure(cache_dir, vendor, now, error.code)
        return
    except Exception:
        write_failure(cache_dir, vendor, now, "internal-error")
        return
    clear_not_before(state_path, vendor)
    write_success(cache_dir, vendor, now, data)


def run_once(
    *,
    cache_dir: Path,
    auth_path: Path,
    now: int | None = None,
    transport: Callable[..., HttpResult] = http_json,
    key_reader: Callable[[], str | None] = keychain.read_secret
) -> int:
    current_time = int(time.time()) if now is None else int(now)
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(cache_dir / "cc-quota-poller.lock")
    if lock is None:
        return 0
    try:
        state_path = cache_dir / "cc-quota-poller-state.json"
        run_provider(
            "codex",
            cache_dir,
            state_path,
            current_time,
            lambda: poll_codex(auth_path, current_time, transport)
        )
        run_provider(
            "deepseek",
            cache_dir,
            state_path,
            current_time,
            lambda: poll_deepseek(current_time, transport, key_reader)
        )
    finally:
        lock.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.once:
        parser.error("--once is required")
    home = Path.home()
    return run_once(
        cache_dir=home / ".claude" / "cache",
        auth_path=home / ".codex" / "auth.json"
    )


if __name__ == "__main__":
    raise SystemExit(main())
