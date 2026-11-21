#!/usr/bin/python3
"""Receive native messaging payload from cc-quota-fetcher extension.

Reads one length-prefixed JSON message from stdin, writes the result
to ~/.claude/cache/quota-<email-safe>.json (success) or .status (failure).

Native messaging wire format (Chrome):
  4-byte little-endian uint32 length, then UTF-8 JSON.

Output contract:
  ~/.claude/cache/quota-<email>.json   = full /api/.../usage response
  ~/.claude/cache/quota-<email>.status = "<HH:MM:SS>\\t<reason>"
  Success removes the .status sentinel; failure leaves the .json (cache may go stale).
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
import tempfile
from pathlib import Path

import keychain

CACHE_DIR = Path.home() / ".claude" / "cache"
LOG_FILE = Path.home() / ".claude" / "cache" / "quota-receiver.log"
MAX_MESSAGE_BYTES = 1_048_576
_EOF = object()


class FrameError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid json constant: {value}")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
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


def log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def email_safe(email: str) -> str:
    return email.replace("@", "_at_").replace("/", "_")


def read_message() -> object:
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) == 0:
        return _EOF
    if len(raw_len) != 4:
        raise FrameError("invalid-frame")
    msg_len = struct.unpack("<I", raw_len)[0]
    if msg_len > MAX_MESSAGE_BYTES:
        raise FrameError("message-too-large")
    if msg_len == 0:
        raise FrameError("invalid-message")
    raw = sys.stdin.buffer.read(msg_len)
    if len(raw) != msg_len:
        raise FrameError("truncated-message")
    try:
        text = raw.decode("utf-8")
        return json.loads(text, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise FrameError("invalid-message")


def write_message(payload: dict) -> None:
    encoded = json.dumps(payload, allow_nan=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


PID_RE = re.compile(r'^[0-9a-f]{8}$')
WRITABLE_VENDORS = frozenset({'mimo', 'glm'})
CLEARABLE_VENDORS = frozenset({'deepseek', 'mimo', 'glm'})


def valid_pid(pid: object) -> str | None:
    if isinstance(pid, str) and PID_RE.match(pid):
        return pid
    return None


def valid_vendor(vendor: object, allowed: frozenset[str]) -> str | None:
    if isinstance(vendor, str) and vendor in allowed:
        return vendor
    return None


def update_active_profile(pid: str | None) -> None:
    if not pid:
        return
    try:
        atomic_write_text(CACHE_DIR / "vendor-active-profile", pid)
    except Exception:
        pass


def valid_secret(secret: object) -> str | None:
    if not isinstance(secret, str) or not 8 <= len(secret) <= 4096:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in secret):
        return None
    return secret


def migrate_deepseek_key(msg: dict) -> dict:
    if not valid_pid(msg.get("profile_id")):
        return {"ok": False, "reason": "invalid-profile"}
    secret = valid_secret(msg.get("secret"))
    if secret is None:
        return {"ok": False, "reason": "invalid-secret"}
    try:
        accepted = keychain.create_secret_if_absent(secret)
    except keychain.KeychainError as error:
        reason = str(error)
        if reason not in {"keychain-read-failed", "keychain-write-failed"}:
            reason = "keychain-write-failed"
        return {"ok": False, "reason": reason}
    if not accepted:
        return {"ok": False, "reason": "profile-conflict"}
    return {"ok": True}


def handle(msg: dict) -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    msg_type = msg.get("type")
    if msg_type == "migrate-deepseek-key-v1":
        return migrate_deepseek_key(msg)
    raw_email = msg.get("email")
    if raw_email is not None and (not isinstance(raw_email, str) or "\x00" in raw_email):
        return {"ok": False, "reason": "invalid-message"}
    email = raw_email if raw_email else "unknown"
    safe = email_safe(email)
    cache = CACHE_DIR / f"quota-{safe}.json"
    status = CACHE_DIR / f"quota-{safe}.status"
    pid = msg.get("profile_id")

    if msg_type == "quota":
        body = msg.get("body")
        if not isinstance(body, dict):
            return {"ok": False, "reason": "missing-body"}
        envelope = {
            "fetched_at": msg.get("fetched_at"),
            "email": email,
            "org_id": msg.get("org_id"),
            "data": body,
        }
        try:
            encoded = json.dumps(envelope, allow_nan=False)
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid-message"}
        atomic_write_text(cache, encoded)
        status.unlink(missing_ok=True)
        return {"ok": True, "wrote": str(cache)}

    if msg_type == "status":
        raw_ts = msg.get("fetched_at")
        ts = "" if raw_ts is None else str(raw_ts)
        reason = msg.get("reason") or "unknown"
        atomic_write_text(status, f"{ts}\t{reason}")
        return {"ok": True, "wrote": str(status)}

    if msg_type in ("vendor-balance", "vendor-plan", "vendor-status"):
        if not valid_pid(pid):
            return {"ok": False, "reason": "invalid-profile"}
        vendor = valid_vendor(msg.get("vendor"), WRITABLE_VENDORS)
        if not vendor:
            return {"ok": False, "reason": "invalid-vendor"}
        update_active_profile(pid)

    if msg_type == "vendor-balance":
        data = msg.get("data")
        if not isinstance(data, dict):
            return {"ok": False, "reason": "missing-data"}
        vcache = CACHE_DIR / f"vendor-{vendor}-{pid}.json"
        vstatus = CACHE_DIR / f"vendor-{vendor}-{pid}.status"
        try:
            encoded = json.dumps(
                {"vendor": vendor, "profile_id": pid, "data": data},
                allow_nan=False,
            )
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid-message"}
        atomic_write_text(vcache, encoded)
        vstatus.unlink(missing_ok=True)
        return {"ok": True, "wrote": str(vcache)}

    if msg_type == "vendor-plan":
        data = msg.get("data")
        if not isinstance(data, dict):
            return {"ok": False, "reason": "missing-data"}
        pcache = CACHE_DIR / f"vendor-{vendor}-plan-{pid}.json"
        try:
            encoded = json.dumps(
                {"vendor": vendor, "profile_id": pid, "data": data},
                allow_nan=False,
            )
        except (TypeError, ValueError):
            return {"ok": False, "reason": "invalid-message"}
        atomic_write_text(pcache, encoded)
        return {"ok": True, "wrote": str(pcache)}

    if msg_type == "vendor-status":
        raw_ts = msg.get("fetched_at")
        ts = "" if raw_ts is None else str(raw_ts)
        reason = msg.get("reason") or "unknown"
        vstatus = CACHE_DIR / f"vendor-{vendor}-{pid}.status"
        atomic_write_text(vstatus, f"{ts}\t{reason}")
        return {"ok": True, "wrote": str(vstatus)}

    if msg_type == "vendor-clear":
        vendor = valid_vendor(msg.get("vendor"), CLEARABLE_VENDORS)
        if not vendor:
            return {"ok": False, "reason": "invalid-vendor"}
        if not valid_pid(pid):
            return {"ok": False, "reason": "invalid-profile"}
        removed = []
        for name in [
            f"vendor-{vendor}-{pid}.json",
            f"vendor-{vendor}-{pid}.status",
            f"vendor-{vendor}-plan-{pid}.json",
        ]:
            p = CACHE_DIR / name
            try:
                p.unlink()
                removed.append(name)
            except FileNotFoundError:
                pass
        return {"ok": True, "removed": removed}

    return {"ok": False, "reason": "unknown-type"}


def main() -> int:
    try:
        try:
            msg = read_message()
        except FrameError as error:
            write_message({"ok": False, "reason": error.reason})
            return 1
        if msg is _EOF:
            return 0
        if not isinstance(msg, dict):
            write_message({"ok": False, "reason": "invalid-message"})
            return 1
        resp = handle(msg)
        write_message(resp)
        return 0
    except Exception:
        log("internal-error")
        try:
            write_message({"ok": False, "reason": "internal-error"})
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
