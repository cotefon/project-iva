import { BLANK, fmt, fmtPct } from "../lib/format";
import type { TableRow, YearTableData } from "../lib/table";

/** Same ten columns, in the same order, as the Markdown and HTML reports. */
const COLUMNS = [
  "Período",
  "Folio",
  "Venta del mes",
  "Venta acumulada",
  "Var. Venta",
  "Facturas Emitidas",
  "Promedio de monto por factura",
  "Compras",
  "Compras acumulada",
  "Var. Compras",
];

/** A "*" marks a month whose F29 was missing some codes; they were counted as 0. */
const flag = (value: string, incomplete: boolean) =>
  incomplete ? `${value} *` : value;

function Row({ row }: { row: TableRow }) {
  switch (row.kind) {
    case "blank":
      return (
        <tr className="blank">
          <td>{row.name}</td>
          {COLUMNS.slice(1).map((c) => (
            <td key={c}>{BLANK}</td>
          ))}
        </tr>
      );

    case "data": {
      const m = row.row;
      return (
        <tr>
          <td>{row.name}</td>
          <td>{m.folio ?? BLANK}</td>
          <td>{flag(fmt(m.venta_del_mes), m.missing.length > 0)}</td>
          <td>{fmt(m.venta_acumulada)}</td>
          <td>{fmtPct(m.venta_variacion_pct)}</td>
          <td>{fmt(m.invoices)}</td>
          <td>{fmt(m.promedio_facturas)}</td>
          <td>{flag(fmt(m.compras), m.missing_compras.length > 0)}</td>
          <td>{fmt(m.compras_acumulada)}</td>
          <td>{fmtPct(m.compras_variacion_pct)}</td>
        </tr>
      );
    }

    case "total":
      return (
        <tr className="total">
          <td>Total</td>
          <td>{BLANK}</td>
          <td>{fmt(row.sales)}</td>
          <td>{BLANK}</td>
          <td>{BLANK}</td>
          <td>{fmt(row.invoices)}</td>
          <td>{BLANK}</td>
          <td className={row.deficit ? "deficit" : undefined}>
            {fmt(row.compras)}
          </td>
          <td>{BLANK}</td>
          <td>{BLANK}</td>
        </tr>
      );

    case "average":
      return (
        <tr className="avg">
          <td>Promedio</td>
          <td>{BLANK}</td>
          <td>{fmt(row.sales)}</td>
          <td>{BLANK}</td>
          <td>{BLANK}</td>
          <td>{fmt(row.invoices)}</td>
          <td>{fmt(row.perFactura)}</td>
          <td>{fmt(row.compras)}</td>
          <td>{BLANK}</td>
          <td>{BLANK}</td>
        </tr>
      );
  }
}

export function YearTable({ table }: { table: YearTableData }) {
  return (
    <section className="year">
      <h2>{table.year}</h2>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {COLUMNS.map((c) => (
                <th key={c}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row, i) => (
              <Row key={i} row={row} />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
