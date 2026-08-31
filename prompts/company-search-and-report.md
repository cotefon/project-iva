# Task: company search + month selection for report generation

Two features, in this order. Part 2 depends on Part 1's endpoint work, so build
and verify Part 1 first.

Read `CLAUDE.md` before starting — especially **"The RUT is the reporting unit,
not the upload"**, **"Period ranges"** and **"Keeping the frontend table
honest"**. This task touches all three.

---

## Part 1 — Search the companies registered in the database

### Problem

`GET /ruts` returns *every* taxpayer the caller has ever uploaded, and
`ExtractPage.tsx` renders them as a flat `<ul className="history-list">` of
buttons under "Documentos anteriores". There is no way to find a company except
by scrolling. A user with a few dozen RUTs cannot work with that.

### What to build

A search over the caller's registered companies, matching on the **`rut`
only** — not the name — and returning `{rut, nombre}` in the existing shape.
The name is still *displayed* in the results, it is just not searched.

**Do the matching in SQL, not in Python.** This is an established convention
here, not a preference: `list_taxpayers()` already goes through the
`user_taxpayers` RPC precisely so the database returns one row per RUT instead
of shipping one row per upload to be reduced in Python. A search that pulls
every taxpayer and filters in `storage.py` would reintroduce exactly the cost
that function was written to remove.

- **`apps/api/schema.sql`** — add a `search_taxpayers(p_user_id uuid, p_rut
  text, p_limit int default 50)` function next to `user_taxpayers`. `p_rut` is
  a RUT fragment, and matching it is the whole of the search. Follow the
  file's conventions: `create or replace`, `language sql`, `stable`, and a
  comment block explaining *why* it exists, in the voice of the existing ones.
  The file must stay idempotent and re-runnable in the Supabase SQL editor.
- **`apps/api/storage.py`** — a `search_taxpayers(user_id, rut_query, limit)`
  that calls the RPC. Keep it beside `list_taxpayers` and match its docstring
  style.
- **`apps/api/api.py`** — extend the existing `GET /ruts` with an optional
  `?q=` query parameter rather than adding a new route. `RutsResponse` already
  carries `taxpayers`, so the response shape does not change and the frontend
  keeps one code path. An absent or blank `q` must behave exactly as today.
- **`apps/web/`** — a search input above the "Documentos anteriores" list,
  whose label and placeholder make it obvious the field takes a **RUT** and
  not a company name (e.g. placeholder `Buscar por RUT (76.044.491-K)`). A
  user who types a name and gets nothing back should be able to see why from
  the field itself.
  Debounce the input (~250-300ms) so a keystroke is not a request. Show a
  distinct empty state for "no matches" versus "nothing uploaded yet" — they
  mean different things and the second one should still tell the user to upload
  a PDF.

### Requirements that are easy to get wrong

1. **Normalization is the entire feature — get it right.** RUTs are stored
   formatted, e.g. `79.527.050-7`. A user who types `79527050`, `795270507`,
   `79.527.050` or `79527050-7` expects a hit. Strip dots, the hyphen and
   surrounding whitespace from *both* the stored value and the query before
   comparing, then match the query as a substring of what remains. Do not
   require the verifier digit, and do not require the query to be a prefix — a
   user who remembers the middle of a RUT should still find it.
2. **The verifier digit can be `K`**, e.g. `76.044.491-K`. Fold case before
   comparing so `...-k` and `...-K` are the same RUT. This is the one place
   case-insensitivity still matters now that names are not searched.
3. **Scoping is a security boundary, not a filter.** `p_user_id` is applied
   *inside* the SQL function (the same rule the `declaration_timeline` comment
   states), the endpoint keeps `Depends(current_user)`, and the user id always
   comes from the verified token — never from the request. Per `CLAUDE.md`, a
   data endpoint without both of those leaks one user's tax documents to
   another.
4. **`SIN-RUT` documents.** Uploads whose header could not be parsed are stored
   under the `UNKNOWN_RUT` sentinel with no name. Decide deliberately whether
   they appear in results, and comment the decision. They cannot be addressed
   as `/report/{rut}` afterwards, so surfacing them as a searchable, clickable
   company is a dead end for the user.

---

## Part 2 — Select stored months, then generate the document

### Problem

The `PeriodPicker` works, but its options come from `full.months` — the document
already loaded into the page. That has two consequences:

- You must load a company's entire timeline before you can narrow it.
- The month dropdown offers all twelve months for the boundary years even when
  the database holds no declarations for some of them. Picking a span that
  contains no stored month returns a 404 (`_timeline_or_404`), which surfaces to
  the user as an error banner. The picker can currently produce a request that
  is guaranteed to fail.

### What to build

Month selection driven by **what the database actually holds** for the selected
company, so the flow becomes: *search a company → see its stored periods → pick
a span → generate the table and the PDF*.

- **`apps/api/schema.sql`** — a `declaration_periods(p_rut text, p_user_id
  uuid)` function returning the distinct `(year, month)` pairs the caller holds
  for a RUT, in order. It must merge across documents the same way
  `declaration_timeline` does, since a RUT's periods span every upload. This is
  deliberately cheap: it returns periods, not figures, so the picker can be
  populated without transferring a timeline.
- **`apps/api/storage.py`** + **`apps/api/api.py`** — expose it as
  `GET /periods/{rut}`, authenticated and user-scoped like every other read.
- **`apps/web/`** — populate `PeriodPicker` from that endpoint and **offer only
  months that exist**. This removes the guaranteed-404 selection described
  above. Keep the existing behaviour where an unset bound displays the
  document's own first/last period rather than a blank field.
- Generation itself needs no new backend work: `GET /report/{rut}?desde=&hasta=`
  and `GET /report/{rut}.pdf?desde=&hasta=` already do this and are already
  wired in `apps/web/src/lib/api.ts` as `report` / `reportPdf`. Reuse them.

### The one architectural decision to make explicitly

`PeriodRange`, `declaration_timeline`'s `p_desde`/`p_hasta`,
`PeriodRange.months_of` and `table.ts`'s `monthSpan` all assume a **contiguous
inclusive range**. That assumption is load-bearing.

**Default: keep the contiguous range** and simply restrict its options to real
stored periods. This reuses the entire existing pipeline, and every invariant
below continues to hold unchanged.

Only if arbitrary non-contiguous month selection is genuinely required, treat it
as a separate, additive feature and answer these first — do not let it arrive by
accident:

- What does **Venta acumulada** mean over a discontinuous set? (Sum in selection
  order? Then a gap is silently invisible on the report.)
- What happens to the **Ene-Dic padding**? `fill_year_months` renders undeclared
  months as blank rows; an unselected month and an undeclared month would then
  look identical on the page and in the PDF.
- `months_of` / `monthSpan` return a `(first, last)` pair and cannot express a
  set. Both sides would have to change together.

If you go that way, say so in `CLAUDE.md` and define the semantics there.

---

## Invariants neither part may break

These are documented in `CLAUDE.md` and verified to hold today. Re-check them
after your changes.

1. **Accumulated columns restart at `desde`.** They sum only the months on the
   report.
2. **Year-over-year still looks outside the range.** The renderers take both
   `rows` (what to draw) and `all_rows` (what to look up in). Filtering the
   lookup too blanks out the `% Var` column of the report's first year.
3. **`apps/web/src/lib/table.ts` duplicates Python rules on purpose** — the
   Ene-Dic padding, the Total row and its deficit flag, the Promedio row, and
   `monthSpan` mirroring `PeriodRange.months_of`. Change one side and you must
   change the other, or the same document reads differently in the browser and
   in the downloaded PDF. Use `roundHalfEven` from `lib/format.ts` for anything
   mirroring a Python `round()`.
4. **Every endpoint is authenticated and every read is user-scoped.**
5. **Rounding and thousands-formatting stay in the API layer**, never in
   `extract_codes.py`.
6. **`pdfplumber`, `dotenv` and `supabase` stay lazily imported** so the parsing
   helpers and `api.py` import cleanly without them.
7. **The service_role key stays server-side.** The browser only ever gets the
   anon key.
8. **`schema.sql` stays idempotent** and safe to re-run.

## Verification — required before reporting done

Run npm and Python **from Windows**, not WSL (`CLAUDE.md`, "Run npm from ONE
side"). Mixing sides corrupts `node_modules`.

```bash
npm run typecheck     # tsc -b across the workspaces
npm run build         # typecheck + vite build
```

Then exercise the behaviour, do not just assert it compiles:

- **Search:** the same RUT written four ways (with dots and hyphen, bare
  digits, digits with the verifier digit, dots but no hyphen), a fragment from
  the *middle* of a RUT, a `-k` / `-K` verifier digit in both cases, and a
  query matching nothing. Confirm that a company *name* returns no results —
  that is now the intended behaviour, not a bug. Confirm a second user's RUTs
  never appear.
- **Periods:** confirm `/periods/{rut}` returns the merged set across several
  uploads, not just the newest document's window.
- **Report:** confirm the two range rules above still hold on real data.
  `outputs/output.md` is a real text extraction of the sample PDF and is safe to
  use — parse it with `extract_codes.monthly_rows()`, then call
  `api._extract_payload(narrowed, all_rows=rows, rng=rng)` and assert that the
  first row's `venta_acumulada` equals its `venta_del_mes`, and that its
  `venta_variacion_pct` is unchanged from the unnarrowed payload.
- Confirm the picker can no longer produce a span that 404s.

Report honestly what you ran and what it printed. If part of this is blocked,
finish everything else and say plainly what you left out and why.

## Also update

- **`CLAUDE.md`** — the file table (new functions in `storage.py`/`schema.sql`),
  and a short section stating that the search matches the RUT only, and how the
  normalization works. Say it explicitly: a future reader who assumes the name
  is searched will "fix" a bug that does not exist. If you changed what a period
  range means, that section too.
- **`README.md`** if the user-facing flow changes.
