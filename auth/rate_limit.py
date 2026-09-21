"""Rate limiting for the website's own auth endpoints — a different
surface from mcp_server.py's rate limiter (that guards MCP tool calls
against a runaway/compromised token; this guards the login mechanism
itself against being hammered directly, most importantly password
guessing against a local/test account).

Same simple fixed-window-counter approach as mcp_server.py, but this
process (api/main.py) can't share state with that one, so it gets its
own in-memory dict — fine at this scale, and a restart just resets
everyone's budget rather than locking anyone out.
"""
import time
from typing import Optional

from fastapi import Depends, HTTPException, Request

_state: dict[str, tuple[int, int]] = {}


def _window_count(key: str, window_seconds: int) -> int:
    now = time.time()
    window = int(now // window_seconds)
    window_start, count = _state.get(key, (window, 0))
    return count if window_start == window else 0


def _increment(key: str, window_seconds: int) -> int:
    now = time.time()
    window = int(now // window_seconds)
    window_start, count = _state.get(key, (window, 0))
    if window_start != window:
        window_start, count = window, 0
    count += 1
    _state[key] = (window_start, count)
    return count


def rate_limit(max_requests: int, window_seconds: int, scope: str):
    """FastAPI dependency factory — `scope` namespaces the key per
    route (so /auth/local/login and /auth/admin/test-accounts don't
    share one budget), combined with the caller's source IP. This is a
    blunt, generic throttle on raw request volume; see
    is_failed_login_throttled below for the sharper, username-specific
    guard against brute-forcing one account from many IPs."""

    def dependency(request: Request):
        client_ip = request.client.host if request.client else "unknown"
        key = f"{scope}:{client_ip}"
        if _increment(key, window_seconds) > max_requests:
            raise HTTPException(status_code=429, detail="Too many requests — try again in a moment")

    return dependency


_FAILED_LOGIN_WINDOW_SECONDS = 300
_FAILED_LOGIN_MAX = 5


def is_failed_login_throttled(username: str) -> bool:
    """Checked BEFORE attempting authentication — keyed on the
    attempted username itself, not the caller's IP, so this catches a
    slow/distributed brute force against one account that rotates IPs
    to dodge the per-IP throttle above. Only failed attempts count
    (see record_failed_login) — a real user logging in correctly
    several times never eats into this budget."""
    return _window_count(f"failed_login:{username}", _FAILED_LOGIN_WINDOW_SECONDS) >= _FAILED_LOGIN_MAX


def record_failed_login(username: str) -> None:
    _increment(f"failed_login:{username}", _FAILED_LOGIN_WINDOW_SECONDS)
