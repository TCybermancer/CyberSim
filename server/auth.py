"""
Auth for the two separate trust boundaries this server has:

  1. Dashboard <-> browser: per-user accounts (see db.py's users table),
     each with a role of 'admin' or 'viewer' (session cookie, see
     SESSION_COOKIE_NAME). Viewers can reach every read-only route;
     mutating routes (launching runs, writing scenarios/schedules,
     minting agent credentials) additionally require the 'admin' role --
     see app.py's require_admin dependency. A built-in 'admin' account is
     bootstrapped at startup (see app.py's _ensure_admin_user); more
     accounts are created via POST /users by an existing admin.
  2. Agent <-> server: each host gets its own bearer token, minted the
     first time its install bundle is downloaded (see app.py's
     /install/agent-bundle) and required on every subsequent
     register/poll/ledger call from that host. Chosen over mTLS for this
     pass as a much smaller, still-real improvement over the previous
     zero-auth state -- see docs/README.md's "Still stubbed" list for the
     mTLS tradeoff this defers.

No new dependencies: password hashing uses stdlib hashlib.pbkdf2_hmac
rather than pulling in bcrypt/passlib, consistent with this project's
existing zero-external-dependency persistence choices (see db.py).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

SESSION_COOKIE_NAME = "cybersim_session"
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> tuple[str, str]:
    """Returns (password_hash_hex, salt_hex)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return digest.hex(), salt.hex()


def verify_password(password: str, password_hash_hex: str, salt_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return hmac.compare_digest(digest.hex(), password_hash_hex)


def new_token() -> str:
    """Used for both agent bearer tokens and session ids -- same
    randomness requirement, no reason for two generators."""
    return secrets.token_urlsafe(32)


def extract_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    prefix = "Bearer "
    if not authorization_header.startswith(prefix):
        return None
    return authorization_header[len(prefix) :].strip() or None
