/** Turns the API's flat `months[]` into the per-year tables the report shows.
 *
 *  The payload carries only declared months, each already holding its own
 *  accumulated figures. Three things are still computed here, matching the
 *  Python renderers one-for-one so this table agrees with /extract.md and
 *  /extract.pdf for the same document:
 *
 *    - blank padding, from extract_codes.fill_year_months: every year spans
 *      Ene-Dic, and a month with no F29 declaration shows dashes rather than a
 *      computed zero — an unfiled month must never read as a month that
 *      declared nothing.
 *    - the Total row, and the deficit flag on its Compras cell.
 *    - the Promedio row, from api.py's _YearAggregator.
 */
import { monthName, roundHalfEven } from "./format";
import type { MonthRow, PeriodRange } from "../types";

export type BlankRow = { kind: "blank"; month: number; name: string };
export type DataRow = { kind: "data"; name: string; row: MonthRow };
export type TotalRow = {
  kind: "total";
  sales: number;
  compras: number;
  invoices: number | null;
  /** Year Compras above year Ventas: the one cell rendered in orange. */
  deficit: boolean;
};
export type AverageRow = {
  kind: "average";
  sales: number | null;
  compras: number | null;
  invoices: number | null;
  perFactura: number | null;
};

export type TableRow = BlankRow | DataRow | TotalRow | AverageRow;

export type YearTableData = { year: number; rows: TableRow[] };

/** Group the flat month list by year, preserving the API's chronological order. */
export function groupByYear(months: MonthRow[]): Map<number, MonthRow[]> {
  const years = new Map<number, MonthRow[]>();
  for (const m of months) {
    const bucket = years.get(m.year);
    if (bucket) bucket.push(m);
    else years.set(m.year, [m]);
  }
  return years;
}

/** Average value of one invoice, or null when there is nothing to divide by.
 *  Mirrors extract_codes.per_invoice + api.py's avg_invoice rounding. */
const perInvoice = (total: number, invoices: number | null) =>
  !invoices ? null : roundHalfEven(total / invoices);

/** The first and last month to show for `year`, mirroring PeriodRange.months_of
 *  in api.py: only the range's boundary years are clipped, so Mar 2023 - Ago
 *  2024 renders 2023 as Mar-Dic, 2024 as Ene-Ago, and anything between as a full
 *  Ene-Dic year. Without this the padding would print the months the server
 *  deliberately left out as undeclared ones. */
function monthSpan(year: number, period?: PeriodRange): [number, number] {
  const bound = (value: string | null | undefined, fallback: number) => {
    if (!value) return fallback;
    const [boundYear, boundMonth] = value.split("-").map(Number);
    return boundYear === year ? boundMonth : fallback;
  };
  return [bound(period?.desde, 1), bound(period?.hasta, 12)];
}

function buildRows(
  declared: MonthRow[],
  year: number,
  period?: PeriodRange,
): TableRow[] {
  const byMonth = new Map(declared.map((m) => [m.month, m]));
  const [first, last] = monthSpan(year, period);

  let totSales = 0;
  let totCompras = 0;
  let totInvoices = 0;
  let declaredCount = 0; // the divisor for the monthly means — not always 12

  const rows: TableRow[] = [];
  for (let month = first; month <= last; month++) {
    const row = byMonth.get(month);
    if (!row) {
      rows.push({ kind: "blank", month, name: monthName(month) });
      continue;
    }
    rows.push({ kind: "data", name: monthName(month), row });
    totSales += row.venta_del_mes;
    totCompras += row.compras;
    totInvoices += row.invoices ?? 0;
    declaredCount++;
  }

  // The deficit highlight only means something on a full year: in a partial year
  // the two totals cover different spans of months, so a gap in the document
  // would read as a deficit.
  const complete = declared.length === 12;
  rows.push({
    kind: "total",
    sales: totSales,
    compras: totCompras,
    invoices: totInvoices || null,
    deficit: complete && totCompras > totSales,
  });

  const perMonth = (total: number) =>
    declaredCount ? roundHalfEven(total / declaredCount) : null;
  rows.push({
    kind: "average",
    sales: perMonth(totSales),
    compras: perMonth(totCompras),
    invoices: perMonth(totInvoices),
    // The odd one out: the year's whole Venta over its whole invoice count, not
    // the mean of the monthly averages.
    perFactura: perInvoice(totSales, totInvoices),
  });

  return rows;
}

export function yearTables(
  months: MonthRow[],
  period?: PeriodRange,
): YearTableData[] {
  return [...groupByYear(months)].map(([year, declared]) => ({
    year,
    rows: buildRows(declared, year, period),
  }));
}
