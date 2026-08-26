"""Portal 签名身份校验（与 infinite-canvas/portal_identity.py 保持一致）。

Portal 代理时用 HMAC-SHA256(INTERNAL_TOKEN, f"{ts}:{is_admin}:{username}")
注入签名头，username 为 percent-encoded 后的值。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse

PORTAL_SIG_WINDOW = 60


def portal_token() -> str:
    return os.environ.get("PORTAL_INTERNAL_TOKEN", "")


def verify_portal_identity(headers) -> dict | None:
    token = portal_token()
    if not token:
        return None

    raw_username = headers.get("X-Username") or ""
    user_id = (headers.get("X-Portal-User-Id") or "").strip()
    is_admin = headers.get("X-Is-Admin") == "1"
    ts_raw = (headers.get("X-Portal-Ts") or "").strip()
    signature = (headers.get("X-Portal-Sig") or "").strip()

    if not raw_username or not user_id or not ts_raw.isdigit() or not signature:
        return None
    try:
        if abs(time.time() - int(ts_raw)) > PORTAL_SIG_WINDOW:
            return None
    except (TypeError, ValueError, OverflowError):
        return None

    message = f"{ts_raw}:{'1' if is_admin else '0'}:{raw_username}".encode("utf-8")
    expected = hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        username = urllib.parse.unquote(raw_username)
    except Exception:
        username = raw_username
    return {"user_id": user_id, "username": username, "role": "admin" if is_admin else "user"}
