"""Authentication helpers for the bookshelf API."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from typing import Any

from fastapi import HTTPException, Request


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_auth(request: Request, conn: sqlite3.Connection | None = None) -> None:
    """Verify bearer token from the Authorization header.

    When *conn* is provided (SQLite mode), the token hash is checked against
    the ``auth_tokens`` table.  Otherwise falls back to comparing against the
    ``BOOKSHELF_AUTH_TOKEN`` environment variable.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token.")

    token_hash = _hash_token(token)

    # SQLite path — check auth_tokens table, then fall back to env var
    if conn is not None:
        row = conn.execute(
            "SELECT token_hash FROM auth_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE auth_tokens SET last_used = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
                "WHERE token_hash = ?",
                (token_hash,),
            )
            conn.commit()
            return

    # Fallback — compare against env var
    env_token = os.getenv("BOOKSHELF_AUTH_TOKEN", "").strip()
    if not env_token:
        if conn is not None:
            raise HTTPException(status_code=401, detail="Invalid token.")
        raise HTTPException(status_code=503, detail="Auth token not configured on server.")
    if token_hash != _hash_token(env_token):
        raise HTTPException(status_code=401, detail="Invalid token.")
