/** Presentation helpers mirroring the ones in the Python layer, so a figure
 *  reads identically here and in the Markdown/PDF reports. */

/** Abbreviated month names — the same table as extract_codes.SPANISH_MONTHS. */
export const MONTHS = [
  "Ene",
  "Feb",
  "Mar",
  "Abr",
  "May",
  "Jun",
  "Jul",
  "Ago",
  "Sep",
  "Oct",
  "Nov",
  "Dic",
] as const;

export const monthName = (month: number) => MONTHS[month - 1] ?? String(month);

const NUMBER = new Intl.NumberFormat("es-CL");

/** Chilean dot thousands separators; a missing value renders as an em dash.
 *  Mirrors api.py's `fmt`. */
export const fmt = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : NUMBER.format(v);

/** Signed percentage with a Chilean decimal comma: 12.3 -> "+12,3%".
 *  Mirrors extract_codes.format_pct, including the em dash for undefined. */
export const fmtPct = (pct: number | null | undefined) => {
  if (pct === null || pct === undefined) return "—";
  const sign = pct < 0 ? "-" : "+";
  return `${sign}${Math.abs(pct).toFixed(1).replace(".", ",")}%`;
};

/** Round half to even — what Python's built-in `round()` does.
 *
 *  JavaScript's Math.round breaks ties upward (0.5 -> 1) while Python breaks
 *  them toward the even integer (0.5 -> 0, 1.5 -> 2). The averages below are
 *  also computed server-side for the Markdown and PDF reports, so using
 *  Math.round here would make this table disagree with those by one peso-
 *  thousand whenever a mean lands exactly on .5. */
export function roundHalfEven(x: number): number {
  const floor = Math.floor(x);
  const diff = x - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}
