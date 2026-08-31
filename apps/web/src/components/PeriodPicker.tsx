import { useMemo } from "react";
import { MONTHS } from "../lib/format";
import type { PeriodRange, StoredPeriod } from "../types";

/** "AAAA-MM" — zero-padded so two bounds compare correctly as plain strings,
 *  and so the value round-trips to the API's `?desde=`/`?hasta=` unchanged. */
const key = (year: number, month: number) =>
  `${year}-${String(month).padStart(2, "0")}`;

const partsOf = (period: string) => ({
  year: Number(period.slice(0, 4)),
  month: Number(period.slice(5, 7)),
});

type Props = {
  /** The range in force. Both bounds null means every stored period. */
  range: PeriodRange;
  /** The periods the database actually holds for this RUT, ascending. The
   *  picker offers these and nothing else, so it cannot produce a span the
   *  API would answer with a 404. */
  periods: StoredPeriod[];
  onChange: (next: PeriodRange) => void;
  disabled?: boolean;
};

/** Desde / Hasta month+year selectors bounding one continuous span.
 *
 *  Every option comes from `periods` — the months stored for the RUT, merged
 *  across the user's uploads. A year lists only its own declared months, so a
 *  carpeta running Nov 2022 - Sep 2024 offers Nov/Dic in 2022 and Ene-Sep in
 *  2024, never the twelve calendar months. Both bounds therefore always name a
 *  real declaration, and since the range is closed and ordered it always
 *  contains at least one.
 *
 *  An unset bound displays the first/last stored period, so the picker reads as
 *  the range actually on screen rather than as a blank field. Touching either
 *  side closes the range against the other end, and a bound that would cross
 *  the other one pushes it instead of producing an inverted range the API would
 *  reject with a 400.
 */
export function PeriodPicker({ range, periods, onChange, disabled }: Props) {
  /** Years in order, and the months declared within each — the option lists. */
  const stored = useMemo(() => {
    const byYear = new Map<number, number[]>();
    for (const { year, month } of periods) {
      const months = byYear.get(year);
      if (months) months.push(month);
      else byYear.set(year, [month]);
    }
    for (const months of byYear.values()) months.sort((a, b) => a - b);

    const years = [...byYear.keys()].sort((a, b) => a - b);
    const first = years[0];
    const last = years[years.length - 1];
    return {
      byYear,
      years,
      bounds: {
        first: key(first, byYear.get(first)![0]),
        last: key(last, byYear.get(last)!.slice(-1)[0]),
      },
    };
  }, [periods]);

  const ranged = range.desde !== null || range.hasta !== null;

  const effective = {
    desde: partsOf(range.desde ?? stored.bounds.first),
    hasta: partsOf(range.hasta ?? stored.bounds.last),
  };

  /** The stored month of `year` closest to `month`.
   *
   *  Switching year is the case that needs it: the month in hand may not be
   *  declared in the year just picked (Ene exists in 2024 but not in a 2022 that
   *  starts in Nov). Snapping to the nearest declared month keeps the selection
   *  meaningful instead of silently resetting it to January. */
  function nearestMonth(year: number, month: number): number {
    const months = stored.byYear.get(year) ?? [];
    if (months.includes(month)) return month;
    return months.reduce(
      (best, m) => (Math.abs(m - month) < Math.abs(best - month) ? m : best),
      months[0],
    );
  }

  function change(
    side: "desde" | "hasta",
    patch: Partial<typeof effective.desde>,
  ) {
    const current = effective[side];
    const year = patch.year ?? current.year;
    const value = key(year, nearestMonth(year, patch.month ?? current.month));
    const next: PeriodRange = {
      desde: side === "desde" ? value : (range.desde ?? stored.bounds.first),
      hasta: side === "hasta" ? value : (range.hasta ?? stored.bounds.last),
    };
    // Keep desde <= hasta by moving the *other* bound to meet the one just set,
    // which reads as "at least this month" instead of silently rejecting it.
    // Both bounds are stored periods, so the closed range still holds one.
    if (next.desde! > next.hasta!) {
      if (side === "desde") next.hasta = next.desde;
      else next.desde = next.hasta;
    }
    onChange(next);
  }

  return (
    <div className="period-picker">
      {(["desde", "hasta"] as const).map((side) => (
        <label key={side} className="period-side">
          <span>{side === "desde" ? "Desde" : "Hasta"}</span>
          <select
            value={effective[side].month}
            disabled={disabled}
            aria-label={`Mes ${side}`}
            onChange={(e) => change(side, { month: Number(e.target.value) })}
          >
            {(stored.byYear.get(effective[side].year) ?? []).map((month) => (
              <option key={month} value={month}>
                {MONTHS[month - 1]}
              </option>
            ))}
          </select>
          <select
            value={effective[side].year}
            disabled={disabled}
            aria-label={`Año ${side}`}
            onChange={(e) => change(side, { year: Number(e.target.value) })}
          >
            {stored.years.map((year) => (
              <option key={year} value={year}>
                {year}
              </option>
            ))}
          </select>
        </label>
      ))}

      {ranged && (
        <button
          className="link"
          disabled={disabled}
          onClick={() => onChange({ desde: null, hasta: null })}
        >
          Todos los períodos
        </button>
      )}
    </div>
  );
}
