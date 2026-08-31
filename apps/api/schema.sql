-- Supabase / Postgres schema for extracted F29 / IVA sales (normalized).
-- Run once in the Supabase SQL editor (Dashboard -> SQL -> New query).
--
-- Grain, from finest to coarsest:
--   taxpayer     one row per RUT      (identity)
--   document     one row per upload   (provenance: file + timestamp)
--   declaration  one row per month    (the F29 figures)
--
-- Values are stored in FULL PESOS (the extraction unit); the round-to-thousand
-- presentation stays in api.py. A missing code is NULL, so monthly_rows()'s
-- `codes[c] is None` distinction survives the round-trip.

create table if not exists taxpayer (
    rut    text primary key,          -- "79.527.050-7" (or 'SIN-RUT' when unparsed)
    nombre text
);

create table if not exists document (
    id           bigint generated always as identity primary key,
    rut          text not null references taxpayer (rut),
    source_file  text,
    extracted_at timestamptz not null default now()
);

create table if not exists declaration (
    document_id bigint not null references document (id) on delete cascade,
    year        int  not null,
    month       int  not null check (month between 1 and 12),
    folio       text,                 -- F29 declaration folio number (NULL = absent)
    -- Venta del mes codes
    code_020    bigint,               -- NULL = code missing in the PDF
    code_142    bigint,
    code_538    bigint,
    code_587    bigint,
    -- Compras codes
    code_535    bigint,
    code_520    bigint,
    code_528    bigint,
    code_521    bigint,
    code_532    bigint,
    code_560    bigint,
    code_562    bigint,
    invoices int,                       -- sales invoice count (code 503; NULL = absent)
    sales   bigint not null,            -- month_sales(), full pesos
    compras bigint not null default 0,  -- month_compras(), full pesos
    primary key (document_id, year, month)
);

-- Migrate an already-deployed declaration table (idempotent): add the Compras
-- columns when an earlier schema without them exists. No-ops on a fresh table
-- created by the statement above.
alter table declaration add column if not exists code_535 bigint;
alter table declaration add column if not exists code_520 bigint;
alter table declaration add column if not exists code_528 bigint;
alter table declaration add column if not exists code_521 bigint;
alter table declaration add column if not exists code_532 bigint;
alter table declaration add column if not exists code_560 bigint;
alter table declaration add column if not exists code_562 bigint;
alter table declaration
    add column if not exists compras bigint not null default 0;
alter table declaration add column if not exists folio text;
alter table declaration add column if not exists invoices int;

-- Ownership: which signed-in user uploaded this document. Matches auth.users.id
-- (the `sub` claim of the Supabase access token; see auth.py). Nullable because
-- documents extracted before login existed have no owner — those rows stay in
-- the table but stop being visible, since every read now filters by user_id.
alter table document add column if not exists user_id uuid;

-- One row per signed-in user for the profile fields Supabase Auth does not
-- store. Email and password live in auth.users and are changed through the
-- Auth admin API (storage.update_auth_user), not here.
create table if not exists profile (
    id         uuid primary key,   -- matches auth.users.id
    nombre     text,
    updated_at timestamptz not null default now()
);

create index if not exists idx_declaration_period on declaration (year, month);
create index if not exists idx_document_rut on document (rut);
create index if not exists idx_document_user on document (user_id);

-- Every report starts the same way: "this user's documents for this RUT, newest
-- first". The single-column indexes above each cover half of that, so the
-- planner filters on one and re-checks the other. This composite covers the
-- whole lookup, and its trailing `id desc` means the newest-document ordering is
-- read straight off the index instead of sorted.
create index if not exists idx_document_user_rut
    on document (user_id, rut, id desc);

-- declaration_timeline() joins back to document by id, and reads each document's
-- months in period order.
create index if not exists idx_declaration_doc_period
    on declaration (document_id, year, month);


-- One RUT's continuous timeline, merged across every document the user uploaded
-- for it.
--
-- A carpeta tributaria covers a window (24, 36 months), so a user who uploads
-- several over time holds more periods for a RUT than any single document has.
-- Reporting the newest document alone hides the rest: with a 2021-2024 carpeta
-- and a 2023-2026 one, the older 18 periods become unreachable even though they
-- are stored.
--
-- `distinct on (year, month) ... order by year, month, d.id desc` keeps, for
-- each period, the row from the most recently uploaded document that declares
-- it. So a re-uploaded or corrected carpeta supersedes only the months it
-- actually contains, and uploads accumulate instead of shadowing each other.
--
-- p_desde / p_hasta are inclusive AAAA*100+MM bounds (202303 = March 2023), or
-- NULL for "no bound on this side". Filtering here rather than in Python means a
-- three-month report reads three months.
--
-- SECURITY: p_user_id is applied inside the function, and the API always passes
-- the authenticated caller's id — never a value from the request.
create or replace function declaration_timeline(
    p_rut     text,
    p_user_id uuid,
    p_desde   int default null,
    p_hasta   int default null
)
returns table (
    document_id bigint,
    year        int,
    month       int,
    folio       text,
    invoices    int,
    code_020 bigint, code_142 bigint, code_538 bigint, code_587 bigint,
    code_535 bigint, code_520 bigint, code_528 bigint, code_521 bigint,
    code_532 bigint, code_560 bigint, code_562 bigint,
    sales   bigint,
    compras bigint
)
language sql
stable
as $$
    select distinct on (dc.year, dc.month)
           dc.document_id, dc.year, dc.month, dc.folio, dc.invoices,
           dc.code_020, dc.code_142, dc.code_538, dc.code_587,
           dc.code_535, dc.code_520, dc.code_528, dc.code_521,
           dc.code_532, dc.code_560, dc.code_562,
           dc.sales, dc.compras
      from declaration dc
      join document d on d.id = dc.document_id
     where d.rut = p_rut
       and d.user_id = p_user_id
       and (p_desde is null or dc.year * 100 + dc.month >= p_desde)
       and (p_hasta is null or dc.year * 100 + dc.month <= p_hasta)
     order by dc.year, dc.month, d.id desc;
$$;


-- The RUTs a user has uploaded, with their names — one row per RUT.
--
-- Replaces selecting every one of the user's document rows and reducing them to
-- a set in Python, which transfers one row per upload forever while the answer
-- stays the size of the RUT list. This is one row per distinct RUT, straight off
-- idx_document_user_rut.
create or replace function user_taxpayers(p_user_id uuid)
returns table (rut text, nombre text)
language sql
stable
as $$
    select distinct d.rut, t.nombre
      from document d
      left join taxpayer t on t.rut = d.rut
     where d.user_id = p_user_id
     order by d.rut;
$$;

-- Search the caller's registered companies by RUT.
--
-- The RUT is matched, the name is NOT: a query is always read as a RUT
-- fragment, so searching a company name deliberately returns nothing. Don't
-- "fix" that by adding `t.nombre ilike ...` — see CLAUDE.md.
--
-- RUTs are stored formatted ("79.527.050-7"), but nobody types them that way
-- consistently. Both sides are folded to bare alphanumerics before comparing,
-- so 79527050, 795270507, 79.527.050 and 79527050-7 all find the same company.
-- The match is a substring, not a prefix, so a half-remembered middle section
-- still finds it. upper() covers the 'K' verifier digit, which is the only
-- letter a RUT can contain.
--
-- A query that folds to nothing (blank, or punctuation only) matches
-- everything, so the search degrades to the unfiltered list rather than to an
-- empty one. Note this also makes LIKE wildcards inert: '%' and '_' are not
-- alphanumeric, so they are stripped before the comparison rather than
-- interpreted.
--
-- SIN-RUT documents (uploads whose header could not be parsed) are searchable
-- like any other, matching the unfiltered list they already appear in. Search
-- filters that list, it does not change what is in it.
--
-- Like user_taxpayers, this returns one row per distinct RUT and does the work
-- in Postgres: filtering in Python would transfer every taxpayer on every
-- keystroke.
--
-- SECURITY: p_user_id is applied inside the function, and the API always passes
-- the authenticated caller's id — never a value from the request.
create or replace function search_taxpayers(
    p_user_id uuid,
    p_rut     text,
    p_limit   int default 50
)
returns table (rut text, nombre text)
language sql
stable
as $$
    select distinct d.rut, t.nombre
      from document d
      left join taxpayer t on t.rut = d.rut
     where d.user_id = p_user_id
       and (
             p_rut is null
          or btrim(p_rut) = ''
          or upper(regexp_replace(d.rut, '[^0-9a-zA-Z]', '', 'g'))
             like '%' || upper(regexp_replace(p_rut, '[^0-9a-zA-Z]', '', 'g')) || '%'
           )
     order by d.rut
     limit coalesce(p_limit, 50);
$$;


-- The periods a caller actually holds for a RUT — (year, month) only, no
-- figures.
--
-- The period picker needs to know which months exist before it can offer them.
-- Reading declaration_timeline() to find out would transfer a full timeline
-- (every code, every total) to answer a question about calendar coverage; this
-- returns two integers per month instead.
--
-- Merged across every document the user uploaded for the RUT, exactly like
-- declaration_timeline: a carpeta covers a fixed window, so the periods
-- available for a RUT are the union of its uploads, not the newest one's span.
-- No `distinct on ... order by d.id desc` is needed here because the question is
-- only whether a period exists at all, not which document's figures win.
--
-- SECURITY: p_user_id is applied inside the function, as above.
create or replace function declaration_periods(
    p_rut     text,
    p_user_id uuid
)
returns table (year int, month int)
language sql
stable
as $$
    select distinct dc.year, dc.month
      from declaration dc
      join document d on d.id = dc.document_id
     where d.rut = p_rut
       and d.user_id = p_user_id
     order by dc.year, dc.month;
$$;


-- Row Level Security: these tables are written server-side with the
-- service_role key, which bypasses RLS. Enabling RLS with no policy therefore
-- keeps the data private from the anon/public API while the backend still works.
alter table taxpayer    enable row level security;
alter table document    enable row level security;
alter table declaration enable row level security;
alter table profile     enable row level security;
