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


def generate_session_id() -> str:
    """A per-tab caller-correlation token (D-016), not a new authentication
    boundary of its own -- ``/api/v1/session`` (app/main.py) that issues it
    already sits behind ``require_api_key``. Its only job is telling two
    concurrent holders of the same shared secret apart for
    ``check_request_ownership`` below, the way a per-user identity would if
    this project had one (OD-28 deliberately does not).
    """
    return secrets.token_urlsafe(32)


def check_request_ownership(record: dict, caller_session_id: str | None) -> None:
    """Raise 404 -- not 403, so a non-owner cannot even confirm the request
    exists -- if ``record`` (app.state.requests[request_id]) was created
    under a different session than the caller's (D-016, closing the
    REVIEW_REQUIRED.md gap that any shared-secret holder could query or
    cancel any other holder's requests).

    A record created with no ``session_id`` (the caller never adopted the
    ``X-Session-Id`` flow -- e.g. a direct API script) keeps its pre-D-016
    behaviour: unrestricted, exactly as every request was before this fix.
    This is a deliberate, documented compatibility choice, not an
    oversight -- see D-016.
    """
    owner_session_id = record.get("session_id")
    if owner_session_id is None:
        return
    if caller_session_id != owner_session_id:
        raise HTTPException(status_code=404, detail="Request was not found.")
