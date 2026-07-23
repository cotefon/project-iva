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

create index if not exists idx_declaration_period on declaration (year, month);
create index if not exists idx_document_rut on document (rut);

-- Row Level Security: these tables are written server-side with the
-- service_role key, which bypasses RLS. Enabling RLS with no policy therefore
-- keeps the data private from the anon/public API while the backend still works.
alter table taxpayer    enable row level security;
alter table document    enable row level security;
alter table declaration enable row level security;
