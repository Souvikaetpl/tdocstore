import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

# Only used to sign the transient OAuth handshake state (Starlette's
# SessionMiddleware) — unrelated to our own login sessions in the
# database, which are opaque random tokens, not signed cookies. Falls
# back to an obviously-insecure default so a missing .env entry fails
# loud in a real deployment rather than silently working with a
# guessable key, while still letting local dev start without one.
SESSION_MIDDLEWARE_SECRET = os.environ.get("SESSION_MIDDLEWARE_SECRET", "dev-insecure-change-me")

# The single, shared MCP endpoint every user connects to — not secret,
# not per-user (see /account: what's per-user is the token or the
# OAuth login, not this URL). One place to change when real hosting
# replaces localhost; imported by both api/main.py (to show it on
# /account) and mcp_server.py (as its own resource_server_url), so
# they can't drift apart.
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8001/mcp")
