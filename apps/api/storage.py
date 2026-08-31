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


def _taxpayer_name(client, rut: str) -> str | None:
    """The registered name for a RUT, or None when it has none.

    A plain lookup rather than a PostgREST embed: the embed needs the foreign-key
    relationship to be introspectable and can fail with PGRST125.
    """
    res = client.table("taxpayer").select("nombre").eq("rut", rut).limit(1).execute()
    return res.data[0]["nombre"] if res.data else None


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

    nombre = _taxpayer_name(client, doc["rut"])

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
    several users — so the caller's RUTs come from their own `document` rows.

    One row per distinct RUT comes back, because the DISTINCT and the name join
    both happen in Postgres (see `user_taxpayers` in schema.sql). Reducing the
    document rows to a set here instead would transfer one row per upload, which
    grows without bound while the answer stays the same size.
    """
    client = _client()
    res = client.rpc("user_taxpayers", {"p_user_id": user_id}).execute()
    return res.data or []


def search_taxpayers(
    user_id: str, rut_query: str | None, limit: int = 50
) -> list[dict]:
    """The caller's taxpayers whose RUT matches `rut_query`, as [{"rut", "nombre"}].

    Matches the **RUT only** — never the name. A query is always read as a RUT
    fragment, so searching "Frutam" deliberately returns nothing while "79527050"
    finds 79.527.050-7. See `search_taxpayers` in schema.sql for the folding
    rules (dots and hyphens stripped on both sides, substring not prefix, upper()
    for the 'K' verifier digit).

    An empty or blank query returns everything, so `/ruts?q=` degrades to the
    unfiltered list rather than to no results.

    Like list_taxpayers, the work happens in Postgres: filtering here would
    transfer every taxpayer the user owns on every keystroke.
    """
    client = _client()
    res = client.rpc(
        "search_taxpayers",
        {"p_user_id": user_id, "p_rut": rut_query, "p_limit": limit},
    ).execute()
    return res.data or []


def declaration_periods(rut: str, user_id: str) -> list[dict]:
    """The (year, month) periods the caller holds for a RUT, in order.

    Returns [{"year": 2024, "month": 3}, ...] — calendar coverage, no figures.
    The period picker needs to know which months exist before it can offer them,
    and answering that with rut_timeline() would transfer every code and total
    of a whole timeline to count months.

    Merged across all the user's uploads for the RUT, like rut_timeline: the
    periods available are the union of the uploads, not the newest one's window.

    An empty list means the caller has nothing stored for the RUT — the same
    "nothing stored" answer the other reads give, and for the same reason: a RUT
    another user uploaded must not be readable here.
    """
    client = _client()
    res = client.rpc(
        "declaration_periods", {"p_rut": rut, "p_user_id": user_id}
    ).execute()
    return res.data or []


def _period_bound(bound: tuple[int, int] | None) -> int | None:
    """(2023, 3) -> 202303, the AAAA*100+MM form declaration_timeline() compares.

    None passes through as "no bound on this side".
    """
    return None if bound is None else bound[0] * 100 + bound[1]


def rut_timeline(
    rut: str,
    user_id: str,
    desde: tuple[int, int] | None = None,
    hasta: tuple[int, int] | None = None,
) -> dict | None:
    """The caller's whole timeline for a RUT, merged across all their uploads.

    Where latest_document() reports one upload, this reports the RUT: for each
    period it keeps the figures from the most recently uploaded document that
    declares it (see `declaration_timeline` in schema.sql). A user holding a
    2021-2024 carpeta and a 2023-2026 one can therefore report 2021-2026, which
    no single document covers.

    `desde`/`hasta` are inclusive (year, month) bounds applied in SQL, so a
    narrow range transfers only the months it asks for.

    Returns None when this user has no documents for the RUT — the same "nothing
    stored" answer latest_document() gives, and for the same reason: a RUT
    another user uploaded must not be readable here.

    Shape matches latest_document()'s, plus `documents` (how many uploads the
    timeline draws on), so the API renders both without branching:
        {"document_id", "rut", "nombre", "source_file", "extracted_at",
         "documents", "rows": [{"year", "month", "folio", "invoices",
                                "codes": {...}, "sales", "compras"}, ...]}
    """
    client = _client()
    # Provenance comes from the newest document: it is what the report is dated
    # by, and its absence is what "nothing stored for this RUT" means.
    docs = (
        client.table("document")
        .select("id, rut, source_file, extracted_at")
        .eq("rut", rut)
        .eq("user_id", user_id)
        .order("id", desc=True)
        .execute()
    )
    if not docs.data:
        return None
    newest = docs.data[0]

    rows = (
        client.rpc(
            "declaration_timeline",
            {
                "p_rut": rut,
                "p_user_id": user_id,
                "p_desde": _period_bound(desde),
                "p_hasta": _period_bound(hasta),
            },
        )
        .execute()
        .data
        or []
    )

    return {
        "document_id": newest["id"],
        "rut": newest["rut"],
        "nombre": _taxpayer_name(client, newest["rut"]),
        "source_file": newest["source_file"],
        "extracted_at": newest["extracted_at"],
        "documents": len(docs.data),
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
            for d in rows
        ],
    }


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
