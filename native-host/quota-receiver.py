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
import re
import struct
import sys
from pathlib import Path

import keychain

CACHE_DIR = Path.home() / ".claude" / "cache"
LOG_FILE = Path.home() / ".claude" / "cache" / "quota-receiver.log"


def log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def email_safe(email: str) -> str:
    return email.replace("@", "_at_").replace("/", "_")


def read_message() -> dict | None:
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) != 4:
        return None
    msg_len = struct.unpack("<I", raw_len)[0]
    raw = sys.stdin.buffer.read(msg_len)
    if len(raw) != msg_len:
        return None
    return json.loads(raw.decode("utf-8"))


def write_message(payload: dict) -> None:
    encoded = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


ACTIVE_PROFILE_FILE = CACHE_DIR / "vendor-active-profile"
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
        ACTIVE_PROFILE_FILE.write_text(pid)
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
    email = msg.get("email") or "unknown"
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
        cache.write_text(json.dumps(envelope))
        if status.exists():
            status.unlink()
        return {"ok": True, "wrote": str(cache)}

    if msg_type == "status":
        ts = msg.get("fetched_at") or ""
        reason = msg.get("reason") or "unknown"
        status.write_text(f"{ts}\t{reason}")
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
        vcache.write_text(json.dumps({"vendor": vendor, "profile_id": pid, "data": data}))
        if vstatus.exists():
            vstatus.unlink()
        return {"ok": True, "wrote": str(vcache)}

    if msg_type == "vendor-plan":
        data = msg.get("data")
        if not isinstance(data, dict):
            return {"ok": False, "reason": "missing-data"}
        pcache = CACHE_DIR / f"vendor-{vendor}-plan-{pid}.json"
        pcache.write_text(json.dumps({"vendor": vendor, "profile_id": pid, "data": data}))
        return {"ok": True, "wrote": str(pcache)}

    if msg_type == "vendor-status":
        reason = msg.get("reason") or "unknown"
        vstatus = CACHE_DIR / f"vendor-{vendor}-{pid}.status"
        vstatus.write_text(f"{msg.get('fetched_at', '')}\t{reason}")
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
            if p.exists():
                p.unlink()
                removed.append(name)
        return {"ok": True, "removed": removed}

    return {"ok": False, "reason": "unknown-type"}


def main() -> int:
    try:
        msg = read_message()
        if msg is None:
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
