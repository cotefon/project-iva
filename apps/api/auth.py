"""Supabase Auth for the F29 / IVA API: verify the caller's access token.

The React app in `web/` signs in with supabase-js and sends the resulting JWT as
`Authorization: Bearer <token>`. This module turns that header into a `User`, or
raises 401. It never issues tokens and never sees a password — Supabase Auth
owns those.

Two signing schemes are supported because Supabase projects differ:

    ES256/RS256   asymmetric signing keys, verified against the project's JWKS
                  endpoint ({url}/auth/v1/.well-known/jwks.json). This is what
                  the configured project uses.
    HS256         the legacy shared secret, from SUPABASE_JWT_SECRET.

Config (env / .env):
    SUPABASE_URL          project URL (shared with storage.py)
    SUPABASE_JWT_SECRET   only needed by projects still on legacy HS256

`jwt` (PyJWT) is imported lazily inside the functions that use it, matching the
pdfplumber / supabase convention elsewhere in this codebase, so api.py stays
importable and testable without the package installed.
"""

from __future__ import annotations

import os
from typing import NamedTuple

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from storage import _load_env, supabase_url

# Every Supabase access token for a signed-in end user carries this audience.
AUDIENCE = "authenticated"

# Seconds of clock difference tolerated when checking the time-based claims.
CLOCK_SKEW_LEEWAY = 60

# auto_error=False so a missing header produces our own Spanish 401 body rather
# than FastAPI's default "Not authenticated", which the React error handler
# would render to the user as-is.
_bearer = HTTPBearer(auto_error=False)


class User(NamedTuple):
    """The authenticated caller, as asserted by a verified Supabase token.

    `id` is the `sub` claim — the uuid of the row in Supabase's `auth.users`,
    and the value stored in `document.user_id` / `profile.id`.
    """

    id: str
    email: str | None


_jwks = None  # PyJWKClient, cached across requests (it caches the fetched keys)


def _jwks_client():
    """The JWKS client for this project, created once and reused.

    PyJWKClient caches the keys it fetches, so reusing the instance keeps token
    verification a local operation instead of an HTTP round trip per request.
    """
    global _jwks
    if _jwks is None:
        from jwt import PyJWKClient

        _jwks = PyJWKClient(
            f"{supabase_url()}/auth/v1/.well-known/jwks.json", cache_keys=True
        )
    return _jwks


def _decode(token: str) -> dict:
    """Verify `token` and return its claims, or raise jwt exceptions.

    The signing scheme is chosen from the token's own `alg` header. That is
    normally how algorithm-confusion attacks start, but each branch below pins
    both its algorithm list *and* its key source: an HS256 token is only ever
    checked against the shared secret, and an ES/RS token only ever against a
    JWKS public key. Neither key can be fed to the other branch, which is the
    substitution the attack depends on.

    Audience and issuer are verified too — a valid signature alone would accept
    a token minted by a different Supabase project.
    """
    import jwt

    base = supabase_url()
    alg = jwt.get_unverified_header(token).get("alg", "")

    if alg.startswith("HS"):
        _load_env()
        secret = os.getenv("SUPABASE_JWT_SECRET")
        if not secret:
            raise RuntimeError(
                "The access token is HS256-signed but SUPABASE_JWT_SECRET is "
                "not set. Copy it from the Supabase dashboard "
                "(Settings -> API -> JWT Settings)."
            )
        key, algorithms = secret, ["HS256"]
    else:
        key = _jwks_client().get_signing_key_from_jwt(token).key
        algorithms = ["ES256", "RS256"]

    return jwt.decode(
        token,
        key,
        algorithms=algorithms,
        audience=AUDIENCE,
        issuer=f"{base}/auth/v1",
        # Clock drift between Supabase and this host is not hypothetical: a host
        # running a few seconds slow receives tokens whose `iat` is in its own
        # future, and PyJWT rejects those outright (ImmatureSignatureError) —
        # every request 401s while the token is perfectly valid. A minute is the
        # usual tolerance; it also applies to `exp`, which at worst accepts a
        # token a minute past expiry, long before the client's refresh loop
        # would have let one get that stale anyway.
        leeway=CLOCK_SKEW_LEEWAY,
    )


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    """FastAPI dependency: the verified caller, or 401.

    Add to a route as `user: User = Depends(current_user)`. A 401 (rather than
    403) is what tells the React client its session has expired and it should
    send the user back to the login screen.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Falta el token de sesión.")

    try:
        claims = _decode(credentials.credentials)
    except RuntimeError:
        # Misconfiguration on our side, not a bad token: let it 500 so the cause
        # is visible in the server log instead of being reported to the user as
        # an authentication failure they cannot fix.
        raise
    except Exception:
        raise HTTPException(
            status_code=401, detail="Sesión inválida o expirada. Inicia sesión de nuevo."
        )

    subject = claims.get("sub")
    if not subject:
        raise HTTPException(status_code=401, detail="El token no identifica a un usuario.")
    return User(id=subject, email=claims.get("email"))
