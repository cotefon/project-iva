import { useCallback, useEffect, useState } from "react";
import { Dropzone } from "../components/Dropzone";
import { PeriodPicker } from "../components/PeriodPicker";
import { TaxpayerSearch } from "../components/TaxpayerSearch";
import { YearTable } from "../components/YearTable";
import {
  ApiError,
  extract,
  extractPdf,
  historyPdf,
  listRuts,
  report,
  reportPdf,
  storedPeriods,
} from "../lib/api";
import { yearTables } from "../lib/table";
import type {
  ExtractResponse,
  PeriodRange,
  StoredPeriod,
  Taxpayer,
} from "../types";

/** What produced the table on screen: a file the user just dropped (which can
 *  therefore be re-sent to /extract.pdf) or a stored document loaded from the
 *  history (which cannot — the original upload is not kept). */
type Source = { kind: "upload"; file: File } | { kind: "stored"; rut: string };

const WHOLE_DOCUMENT: PeriodRange = { desde: null, hasta: null };

export function ExtractPage() {
  /** The document as first loaded, never narrowed: the period picker's options
   *  come from here, so narrowing the report cannot shrink the range the user
   *  is still allowed to pick. */
  const [full, setFull] = useState<ExtractResponse | null>(null);
  /** What the tables render — `full`, or the API's answer for a narrower span. */
  const [result, setResult] = useState<ExtractResponse | null>(null);
  const [range, setRange] = useState<PeriodRange>(WHOLE_DOCUMENT);
  const [source, setSource] = useState<Source | null>(null);
  const [busy, setBusy] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [taxpayers, setTaxpayers] = useState<Taxpayer[]>([]);
  /** The RUT search in force. "" is the unfiltered list. */
  const [query, setQuery] = useState("");
  /** Whether this account holds any upload at all, learned from an unfiltered
   *  load. It is what separates "no matches" from "nothing uploaded yet" — the
   *  same empty list means different things and deserves different advice. */
  const [hasUploads, setHasUploads] = useState(false);
  /** The months the database holds for the open RUT: the picker's options. */
  const [periods, setPeriods] = useState<StoredPeriod[]>([]);

  const runSearch = useCallback((q: string) => {
    // A missing history is not worth an error banner over the results the user
    // actually asked for; it just leaves the list empty.
    listRuts(q)
      .then((r) => {
        setTaxpayers(r.taxpayers);
        // Only an unfiltered answer can tell us whether the account is empty;
        // a search that matched nothing says nothing about that.
        if (!q.trim()) setHasUploads(r.taxpayers.length > 0);
      })
      .catch(() => setTaxpayers([]));
  }, []);

  const search = useCallback(
    (q: string) => {
      setQuery(q);
      runSearch(q);
    },
    [runSearch],
  );

  // The unfiltered load. It has to happen here rather than from the search box,
  // because the box only renders once we know the account has uploads — and
  // this is what establishes that.
  useEffect(() => runSearch(""), [runSearch]);

  /** The RUT the narrowing routes address this document by.
   *
   *  Null when a fresh upload's header could not be parsed: that document is
   *  stored under a placeholder RUT, so there is nothing to ask /report about
   *  and the picker stays hidden rather than 404-ing on every change. */
  const rut = full?.taxpayer?.rut ?? null;

  /** Load the picker's options for a RUT: the months the database holds.
   *
   *  Asked of /periods rather than derived from the document on screen, because
   *  the two differ — a RUT uploaded more than once holds more months than any
   *  single carpeta covers, and offering only the loaded document's months would
   *  hide the rest.
   *
   *  Falls back to the document's own months when /periods cannot answer
   *  (persistence unconfigured, or an upload that was not stored), so the picker
   *  degrades to the old behaviour instead of vanishing. */
  const loadPeriods = useCallback(
    async (rutToLoad: string | null, data: ExtractResponse) => {
      const fallback = data.months.map((m) => ({
        year: m.year,
        month: m.month,
      }));
      if (!rutToLoad) {
        setPeriods(fallback);
        return;
      }
      try {
        setPeriods((await storedPeriods(rutToLoad)).periods);
      } catch {
        setPeriods(fallback);
      }
    },
    [],
  );

  /** Load a document, resetting any range from the previously shown one. */
  async function show(data: ExtractResponse, from: Source) {
    setFull(data);
    setResult(data);
    setSource(from);
    setRange(WHOLE_DOCUMENT);
    await loadPeriods(data.taxpayer?.rut ?? null, data);
  }

  async function onFile(file: File) {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      await show(await extract(file), { kind: "upload", file });
      // The upload was just persisted, so a new RUT may exist. Re-run whatever
      // search is in force rather than dropping the user's filter.
      setHasUploads(true);
      runSearch(query);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  /** Open a RUT's whole stored timeline.
   *
   *  /report rather than /history on purpose: a carpeta covers a fixed window,
   *  so a RUT uploaded more than once holds more periods than any one document.
   *  /history would show the newest upload alone and the picker would then only
   *  offer that document's months. */
  async function openStored(rutToOpen: string) {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      await show(await report(rutToOpen), { kind: "stored", rut: rutToOpen });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  /** Re-fetch the report for a new span.
   *
   *  Server-side rather than filtered here: the accumulated columns restart at
   *  `desde`, and only the API can recompute them while still comparing each
   *  month against the same month a year earlier — which usually falls outside
   *  the requested span. On failure (a span the document has no months for) the
   *  previous table stays on screen behind the error.
   */
  async function applyRange(next: PeriodRange) {
    if (!rut) return;
    setRange(next);
    setBusy(true);
    setError(null);
    try {
      setResult(await report(rut, next));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  /** Download the PDF report for whatever is on screen.
   *
   *  A narrowed report comes from /report/{rut}.pdf, which re-renders the stored
   *  rows for that span. Unnarrowed, the original routes are kept: a fresh
   *  upload re-sends its file, and a stored document is rebuilt from the
   *  database — so a document whose figures were never persisted still
   *  downloads. */
  async function download() {
    if (!source || !result) return;
    setDownloading(true);
    setError(null);
    try {
      const ranged = range.desde !== null || range.hasta !== null;
      // A stored document downloads through /report, so the PDF covers the same
      // merged timeline the screen shows — and so does a narrowed upload. An
      // unnarrowed fresh upload re-sends its file instead, which still works
      // when persistence failed and there is nothing stored to report on.
      const pending =
        rut && (source.kind === "stored" || ranged)
          ? reportPdf(rut, range)
          : source.kind === "upload"
            ? extractPdf(source.file)
            : historyPdf(source.rut);
      const { blob, filename } = await pending;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setDownloading(false);
    }
  }

  return (
    <>
      <h1>Extractor de ventas IVA / Formulario 29</h1>
      <p className="muted">
        Sube una carpeta tributaria en PDF. Todos los montos se muestran en{" "}
        <strong>miles de pesos</strong>: los códigos se redondean al millar
        antes de aplicar la fórmula, y la Venta del mes también se redondea al
        millar.
      </p>

      <Dropzone onFile={onFile} onError={setError} busy={busy} />

      {error && <p className="error">{error}</p>}

      {(hasUploads || taxpayers.length > 0) && (
        <>
          <h2>Documentos anteriores</h2>
          <TaxpayerSearch onSearch={search} disabled={busy} />
          {taxpayers.length > 0 ? (
            <ul className="history-list">
              {taxpayers.map((t) => (
                <li key={t.rut}>
                  <button onClick={() => openStored(t.rut)} disabled={busy}>
                    {t.nombre ?? "Sin nombre"} · {t.rut}
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">
              Ningún RUT coincide con <code>{query}</code>. La búsqueda es por
              RUT, no por nombre.
            </p>
          )}
        </>
      )}

      {result && (
        <>
          <div className="toolbar">
            {result.taxpayer && (
              <span className="muted">
                <strong>{result.taxpayer.nombre ?? "—"}</strong> (RUT{" "}
                {result.taxpayer.rut ?? "—"}) · archivo:{" "}
                {result.source_file ?? "—"} · {result.extracted_at}
                {result.documents && result.documents > 1
                  ? ` · ${result.documents} documentos combinados`
                  : ""}
              </span>
            )}
            <button
              className="primary"
              onClick={download}
              disabled={downloading}
            >
              {downloading ? "Generando…" : "Descargar PDF"}
            </button>
          </div>

          {rut && periods.length > 0 && (
            <PeriodPicker
              range={range}
              periods={periods}
              disabled={busy}
              onChange={applyRange}
            />
          )}

          {yearTables(result.months).map((table) => (
            <YearTable key={table.year} table={table} />
          ))}
        </>
      )}
    </>
  );
}
