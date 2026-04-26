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
import struct
import sys
from pathlib import Path

CACHE_DIR = Path.home() / ".claude" / "cache"
LOG_FILE = Path.home() / ".claude" / "cache" / "quota-receiver.log"


def log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


import datetime
log(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] INVOKED pid={os.getpid()} ppid={os.getppid()} python={sys.executable}")


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


def handle(msg: dict) -> dict:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    msg_type = msg.get("type")
    email = msg.get("email") or "unknown"
    safe = email_safe(email)
    cache = CACHE_DIR / f"quota-{safe}.json"
    status = CACHE_DIR / f"quota-{safe}.status"

    if msg_type == "quota":
        body = msg.get("body")
        if not isinstance(body, dict):
            return {"ok": False, "reason": "missing body"}
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

    return {"ok": False, "reason": f"unknown type {msg_type!r}"}


def main() -> int:
    try:
        msg = read_message()
        if msg is None:
            log("empty stdin")
            return 0
        log(f"recv: {msg.get('type')} email={msg.get('email')}")
        resp = handle(msg)
        write_message(resp)
        return 0
    except Exception as e:
        log(f"error: {e!r}")
        try:
            write_message({"ok": False, "reason": str(e)})
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
