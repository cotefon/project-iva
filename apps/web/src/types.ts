/** Mirrors the JSON that api.py's `_extract_payload` produces. */

/** One declared month. Every amount is already in thousands of pesos — the API
 *  rounds each F29 code to the nearest thousand before applying the formulas, so
 *  the frontend never rescales, only formats. `invoices` is a plain count and is
 *  never rounded. */
export type MonthRow = {
  Mes: string;
  year: number;
  month: number;
  folio: string | null;
  invoices: number | null;
  codes: Record<string, number | null>;
  venta_del_mes: number;
  promedio_facturas: number | null;
  venta_acumulada: number;
  venta_variacion_pct: number | null;
  compras: number;
  compras_acumulada: number;
  compras_variacion_pct: number | null;
  /** F29 codes absent from the document, counted as 0. Non-empty renders a "*". */
  missing: string[];
  missing_compras: string[];
};

/** An inclusive period range as the API takes and echoes it, each bound
 *  "AAAA-MM" or null for "no bound on this side". Mirrors api.py's PeriodRange:
 *  zero-padded, so plain string comparison orders two bounds correctly. */
export type PeriodRange = { desde: string | null; hasta: string | null };

export type ExtractResponse = {
  unit: string;
  formula: string;
  formula_compras: string;
  codes: string[];
  codes_compras: string[];
  months: MonthRow[];

  /** The span actually reported. null/null is the whole document — what every
   *  route except /report returns. The table clips each boundary year's Ene-Dic
   *  padding to it, so the screen shows the same months the PDF does. */
  period?: PeriodRange;

  /** The document's taxpayer. `rut` is null when the PDF header could not be
   *  parsed; such an upload is stored under a placeholder RUT and cannot be
   *  addressed as /report/{rut} afterwards. */
  taxpayer?: { nombre: string | null; rut: string | null };

  /** Present only on the stored-document routes (/history, /report). On /report
   *  they describe the newest upload behind the report, which merges every
   *  document held for the RUT — `documents` is how many it merged. */
  document_id?: number;
  source_file?: string | null;
  extracted_at?: string;
  documents?: number;
};

export type Taxpayer = { rut: string; nombre: string | null };

/** One period the database holds for a RUT. Figures are deliberately absent:
 *  this answers "which months exist", which is all the picker needs to offer
 *  them, and asking /report instead would transfer a whole timeline to find out. */
export type StoredPeriod = { year: number; month: number };

export type PeriodsResponse = {
  rut: string;
  count: number;
  /** Ascending by (year, month), merged across every upload for the RUT. */
  periods: StoredPeriod[];
};

export type RutsResponse = {
  count: number;
  ruts: string[];
  taxpayers: Taxpayer[];
};

export type Profile = {
  id: string;
  email: string | null;
  nombre: string | null;
  /** PATCH /me only: Supabase mails a confirmation link before an address change
   *  takes effect, so the UI must not claim the email was already updated. */
  email_confirmation_pending?: boolean;
};
