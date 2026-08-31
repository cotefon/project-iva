import { useEffect, useRef, useState } from "react";

type Props = {
  /** Called with the debounced query. "" means "no filter". */
  onSearch: (query: string) => void;
  disabled?: boolean;
};

/** How long the field stays quiet before it becomes a request. Long enough that
 *  typing a RUT is one search rather than eleven, short enough to feel live. */
const DEBOUNCE_MS = 275;

/** Search box over the companies registered in the database.
 *
 *  It searches the **RUT, not the name** — the API reads the query as a RUT
 *  fragment with dots and hyphens ignored. The label and placeholder say so,
 *  because a user who types "Frutam", gets nothing back and cannot see why
 *  would reasonably conclude the search is broken.
 */
export function TaxpayerSearch({ onSearch, disabled }: Props) {
  const [value, setValue] = useState("");
  // The parent has already loaded the unfiltered list by the time this mounts
  // (that is how it knows to render the box at all), so emitting "" on mount
  // would only repeat that request.
  const mounted = useRef(false);

  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    const timer = setTimeout(() => onSearch(value), DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [value, onSearch]);

  return (
    <div className="taxpayer-search">
      <label htmlFor="rut-search">Buscar por RUT</label>
      <input
        id="rut-search"
        type="search"
        value={value}
        disabled={disabled}
        placeholder=""
        autoComplete="off"
        onChange={(e) => setValue(e.target.value)}
      />
      <span className="muted">
        Los puntos y el guion se ignoran. No busca por nombre.
      </span>
    </div>
  );
}
