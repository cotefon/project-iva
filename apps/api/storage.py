"""Supabase persistence for extracted F29 / IVA sales (normalized schema).

Three tables (see schema.sql), split by grain so one taxpayer can accumulate
many uploads over time with a full audit trail of which file produced which
numbers:

    taxpayer     one row per RUT (identity: RUT + name)
    document     one row per upload/extraction (provenance: file + timestamp)
    declaration  one row per monthly F29 declaration inside a document

Values are stored in **full pesos** (the extraction layer's unit); the round-to-
thousand presentation stays in api.py. A missing code is stored as NULL, so the
`codes[c] is None` distinction from monthly_rows() survives a round-trip.

Config (env / .env):
    SUPABASE_URL   project URL, e.g. https://xxxx.supabase.co
    SUPABASE_KEY   service_role key (server-side; bypasses RLS). Keep it secret.

The `supabase` client is imported lazily so the parsing/formatting helpers and
api.py stay importable and testable without the package installed — matching the
pdfplumber/dotenv convention in extract_codes.py.
"""

from __future__ import annotations

import os

from extract_codes import ALL_CODES
from paths import ENV_FILE

# Sentinel RUT for documents whose emisor RUT could not be parsed, so the
# taxpayer table's primary key is never NULL.
UNKNOWN_RUT = "SIN-RUT"

# Column name for each stored code, e.g. "020" -> "code_020". Covers both the
# Venta del mes and Compras codes.
_CODE_COL = {c: f"code_{c}" for c in ALL_CODES}


def _load_env() -> None:
    """Load the repo-root .env if python-dotenv is available.

    The path is explicit (see paths.ENV_FILE) rather than left to dotenv's
    search, which walks up from the current working directory — and uvicorn,
    Turborepo and a hand-run CLI each start somewhere different.

    Env vars may also come straight from the real environment, so a missing
    package is not fatal.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE)
    except ImportError:
        pass


def supabase_url() -> str:
    """The project's base URL, normalized to have no path suffix.

    supabase-py builds requests as "{url}/rest/v1/<table>", so a trailing slash
    yields a "//rest/v1" path that PostgREST rejects (PGRST125 "Invalid path
    specified in request URL"). Strip trailing slashes and an
    accidentally-included "/rest/v1" suffix — the configured SUPABASE_URL in
    this project carries one.

    Shared with auth.py, which appends "/auth/v1" to reach the JWKS endpoint and
    to check the token issuer, so both layers agree on what the base URL is.
    """
    _load_env()
    url = os.getenv("SUPABASE_URL")
    if not url:
        raise RuntimeError("SUPABASE_URL must be set.")
    url = url.strip().rstrip("/")
    if url.endswith("/rest/v1"):
        url = url[: -len("/rest/v1")].rstrip("/")
    return url


def _client():
    """Create a Supabase client from SUPABASE_URL / SUPABASE_KEY.

    Imported lazily; raises RuntimeError with an actionable message if the
    package or the credentials are missing, so a misconfigured deploy fails
    loudly here rather than with an opaque error deeper in the call.
    """
    _load_env()
    key = os.getenv("SUPABASE_KEY")
    if not os.getenv("SUPABASE_URL") or not key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_KEY must be set to persist extractions."
        )
    url = supabase_url()
    try:
        from supabase import create_client
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "The 'supabase' package is required for persistence "
            "(pip install -r requirements.txt)."
        ) from exc
    return create_client(url, key)


def _upsert_taxpayer(client, rut: str, nombre: str | None) -> None:
    """Insert the taxpayer, keeping any existing name when this extraction has
    none (upsert with ignore_duplicates), otherwise refreshing it.
    """
    if nombre is not None:
        client.table("taxpayer").upsert(
            {"rut": rut, "nombre": nombre}, on_conflict="rut"
        ).execute()
    else:
        client.table("taxpayer").upsert(
            {"rut": rut}, on_conflict="rut", ignore_duplicates=True
        ).execute()


def save_extraction(
    taxpayer: dict,
    rows: list[dict],
    source_file: str | None = None,
    user_id: str | None = None,
) -> int:
    """Persist one extraction and return the new document id.

    `taxpayer` is extract_taxpayer()'s {"nombre", "rut"} dict; `rows` is
    monthly_rows() output. Each call creates a fresh `document` row (full upload
    history), so re-uploading the same PDF records a new document rather than
    overwriting the previous figures.

    `user_id` is the uploader (auth.User.id). It is what every read filters on,
    so a document saved without one is retained but invisible.
    """
    rut = (taxpayer or {}).get("rut") or UNKNOWN_RUT
    nombre = (taxpayer or {}).get("nombre")

    client = _client()
    _upsert_taxpayer(client, rut, nombre)

    doc = (
        client.table("document")
        .insert({"rut": rut, "source_file": source_file, "user_id": user_id})
        .execute()
    )
    document_id = doc.data[0]["id"]

    records = [
        {
            "document_id": document_id,
            "year": r["year"],
            "month": r["month"],
            "folio": r.get("folio"),
            "invoices": r.get("invoices"),
            **{_CODE_COL[c]: r["codes"][c] for c in ALL_CODES},
            "sales": int(round(r["sales"])),
            "compras": int(round(r["compras"])),
        }
        for r in rows
    ]
    if records:
        client.table("declaration").insert(records).execute()
    return document_id


def latest_document(rut: str, user_id: str) -> dict | None:
    """Return the caller's most recent document for a RUT, or None.

    Scoped to `user_id`: one user's uploads are never readable by another, and a
    RUT the caller has not uploaded reads as "nothing stored" rather than
    returning someone else's figures.

    Shape mirrors monthly_rows() closely enough to re-render without re-parsing:
        {"document_id", "rut", "nombre", "source_file", "extracted_at",
         "rows": [{"year", "month", "codes": {...}, "sales", "compras"}, ...]}
    """
    client = _client()
    docs = (
        client.table("document")
        .select("id, rut, source_file, extracted_at")
        .eq("rut", rut)
        .eq("user_id", user_id)
        .order("id", desc=True)
        .limit(1)
        .execute()
    )
    if not docs.data:
        return None
    doc = docs.data[0]

    # Taxpayer name via a plain lookup (no PostgREST embed, which needs the FK
    # relationship to be introspectable and can fail with PGRST125).
    tp = (
        client.table("taxpayer")
        .select("nombre")
        .eq("rut", doc["rut"])
        .limit(1)
        .execute()
    )
    nombre = tp.data[0]["nombre"] if tp.data else None

    decls = (
        client.table("declaration")
        .select("*")
        .eq("document_id", doc["id"])
        .order("year")
        .order("month")
        .execute()
    )

    return {
        "document_id": doc["id"],
        "rut": doc["rut"],
        "nombre": nombre,
        "source_file": doc["source_file"],
        "extracted_at": doc["extracted_at"],
        "rows": [
            {
                "year": d["year"],
                "month": d["month"],
                "folio": d.get("folio"),
                "invoices": d.get("invoices"),
                "codes": {c: d[_CODE_COL[c]] for c in ALL_CODES},
                "sales": d["sales"],
                "compras": d["compras"],
            }
            for d in decls.data
        ],
    }


def list_taxpayers(user_id: str) -> list[dict]:
    """Return the taxpayers this user has uploaded, as [{"rut", "nombre"}, ...].

    `taxpayer` holds identities, not ownership — the same RUT can be uploaded by
    several users — so the caller's RUTs come from their own `document` rows and
    the names are looked up afterwards.
    """
    client = _client()
    docs = (
        client.table("document").select("rut").eq("user_id", user_id).execute()
    )
    ruts = sorted({d["rut"] for d in (docs.data or [])})
    if not ruts:
        return []

    res = (
        client.table("taxpayer")
        .select("rut, nombre")
        .in_("rut", ruts)
        .order("rut")
        .execute()
    )
    return res.data or []


def get_profile(user_id: str) -> dict | None:
    """Return {"id", "nombre"} for a user, or None when they have no row yet.

    A user who has never saved their profile simply has no row; callers render
    that as an empty name rather than an error.
    """
    client = _client()
    res = (
        client.table("profile")
        .select("id, nombre")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def upsert_profile(user_id: str, nombre: str | None) -> dict:
    """Create or update the user's profile row and return it.

    `updated_at` is set explicitly because the column's default only applies on
    insert, so an update would otherwise keep the original timestamp.
    """
    from datetime import datetime, timezone

    record = {
        "id": user_id,
        "nombre": nombre,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    client = _client()
    res = client.table("profile").upsert(record, on_conflict="id").execute()
    return res.data[0] if res.data else record


def update_auth_user(user_id: str, **attributes) -> None:
    """Change a user's email or password through the Supabase Auth admin API.

    Those fields live in `auth.users`, not in our schema, so they are not ours
    to write directly. This needs the service_role key (SUPABASE_KEY), which is
    why it runs here on the server and never in the browser.

    Raises whatever the Supabase client raises; the caller maps it to a 400 so a
    rejected password reads as a validation error rather than a server fault.
    """
    if not attributes:
        return
    client = _client()
    client.auth.admin.update_user_by_id(user_id, attributes)
