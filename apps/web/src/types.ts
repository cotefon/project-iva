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

export type ExtractResponse = {
  unit: string;
  formula: string;
  formula_compras: string;
  codes: string[];
  codes_compras: string[];
  months: MonthRow[];

  /** Present only on GET /history/{rut}, which returns a stored document in the
   *  same shape as a fresh upload plus its provenance. */
  taxpayer?: { nombre: string | null; rut: string };
  document_id?: number;
  source_file?: string | null;
  extracted_at?: string;
};

export type Taxpayer = { rut: string; nombre: string | null };

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
