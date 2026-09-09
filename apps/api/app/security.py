from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from app.config import get_settings


def require_api_key(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency, registered app-wide (SPEC-M5 §A) so every route
    requires it uniformly, including any added later -- no per-route
    boilerplate, no allowlist of "exempt" routes to maintain.

    A no-op when ``api_shared_secret`` is unset, preserving today's fully
    open local-development default (the same opt-in pattern as
    ``database_url``/``otel_exporter_connection_string``).
    """
    settings = get_settings()
    if not settings.api_shared_secret:
        return
    expected = f"Bearer {settings.api_shared_secret}"
    # secrets.compare_digest, not ==: a naive string comparison short-
    # circuits on the first mismatched byte, which leaks how many leading
    # characters of the guess were correct through response-time
    # differences -- exactly the kind of thing worth doing right the first
    # time on a check whose entire job is keeping guessers out.
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")
