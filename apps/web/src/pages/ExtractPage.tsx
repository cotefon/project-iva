import { useCallback, useEffect, useState } from "react";
import { Dropzone } from "../components/Dropzone";
import { YearTable } from "../components/YearTable";
import { ApiError, extract, extractPdf, history, listRuts } from "../lib/api";
import { yearTables } from "../lib/table";
import type { ExtractResponse, Taxpayer } from "../types";

/** What produced the table on screen: a file the user just dropped (which can
 *  therefore be re-sent to /extract.pdf) or a stored document loaded from the
 *  history (which cannot — the original upload is not kept). */
type Source =
  | { kind: "upload"; file: File }
  | { kind: "stored"; rut: string };

export function ExtractPage() {
  const [result, setResult] = useState<ExtractResponse | null>(null);
  const [source, setSource] = useState<Source | null>(null);
  const [busy, setBusy] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [taxpayers, setTaxpayers] = useState<Taxpayer[]>([]);

  const refreshHistory = useCallback(() => {
    // A missing history is not worth an error banner over the results the user
    // actually asked for; it just leaves the list empty.
    listRuts()
      .then((r) => setTaxpayers(r.taxpayers))
      .catch(() => setTaxpayers([]));
  }, []);

  useEffect(refreshHistory, [refreshHistory]);

  async function onFile(file: File) {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const data = await extract(file);
      setResult(data);
      setSource({ kind: "upload", file });
      refreshHistory(); // the upload was just persisted, so a new RUT may exist
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function openStored(rut: string) {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const data = await history(rut);
      setResult(data);
      setSource({ kind: "stored", rut });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function download() {
    if (source?.kind !== "upload") return;
    setDownloading(true);
    setError(null);
    try {
      const { blob, filename } = await extractPdf(source.file);
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
        <strong>miles de pesos</strong>: los códigos se redondean al millar antes
        de aplicar la fórmula, y la Venta del mes también se redondea al millar.
      </p>

      <Dropzone onFile={onFile} onError={setError} busy={busy} />

      {error && <p className="error">{error}</p>}

      {taxpayers.length > 0 && (
        <>
          <h2>Documentos anteriores</h2>
          <ul className="history-list">
            {taxpayers.map((t) => (
              <li key={t.rut}>
                <button onClick={() => openStored(t.rut)} disabled={busy}>
                  {t.nombre ?? "Sin nombre"} · {t.rut}
                </button>
              </li>
            ))}
          </ul>
        </>
      )}

      {result && (
        <>
          <div className="toolbar">
            {result.taxpayer && (
              <span className="muted">
                <strong>{result.taxpayer.nombre ?? "—"}</strong> (RUT{" "}
                {result.taxpayer.rut}) · archivo: {result.source_file ?? "—"} ·{" "}
                {result.extracted_at}
              </span>
            )}
            {source?.kind === "upload" ? (
              <button
                className="primary"
                onClick={download}
                disabled={downloading}
              >
                {downloading ? "Generando…" : "Descargar PDF"}
              </button>
            ) : (
              <span className="muted">
                Vuelve a subir el PDF para generar el informe descargable.
              </span>
            )}
          </div>

          <p className="muted">
            Venta del mes: <code>{result.formula}</code>. Compras:{" "}
            <code>{result.formula_compras}</code>. Facturas Emitidas: cantidad de
            facturas del mes (<code>503</code>), un conteo sin redondeo. Promedio
            de monto por factura: Venta del mes / Facturas Emitidas del mismo mes.
            * = mes con códigos faltantes (contados como 0).
          </p>

          {yearTables(result.months).map((table) => (
            <YearTable key={table.year} table={table} />
          ))}
        </>
      )}
    </>
  );
}
