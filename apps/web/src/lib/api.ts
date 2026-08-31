import { supabase } from "./supabase";
import type {
  ExtractResponse,
  PeriodRange,
  PeriodsResponse,
  Profile,
  RutsResponse,
} from "../types";

const BASE = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";

/** An error carrying the HTTP status, so callers can treat 401 (session gone)
 *  differently from 422 (this PDF could not be parsed). */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** The single place that attaches the bearer token.
 *
 *  getSession() returns the cached session and refreshes it when it is close to
 *  expiring, so a token is never sent stale. */
async function authHeader(): Promise<Record<string, string>> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  if (!token) throw new ApiError("No hay sesión activa.", 401);
  return { Authorization: `Bearer ${token}` };
}

/** Turn a non-2xx response into an ApiError.
 *
 *  Every failure from the API is a FastAPI HTTPException, whose body is always
 *  {"detail": "..."} — including the 400/422 upload errors from `pdf_to_rows`
 *  and the 503 raised when Supabase credentials are missing. Falling back to the
 *  status text covers a proxy or a crash that returns something else. */
async function fail(res: Response): Promise<never> {
  let detail = res.statusText;
  try {
    const body = await res.json();
    if (typeof body?.detail === "string") detail = body.detail;
  } catch {
    // Not JSON — keep the status text.
  }
  throw new ApiError(detail, res.status);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { ...(await authHeader()), ...(init.headers ?? {}) },
  });
  if (!res.ok) await fail(res);
  return (await res.json()) as T;
}

/** Parse one PDF into the monthly table. */
export async function extract(file: File): Promise<ExtractResponse> {
  const body = new FormData();
  body.append("file", file);
  // No Content-Type header: the browser must set the multipart boundary itself.
  return request<ExtractResponse>("/extract", { method: "POST", body });
}

export type Download = { blob: Blob; filename: string };

/** Fetch a binary response as a blob plus the filename the server chose.
 *
 *  The filename comes from the Content-Disposition header, which the browser
 *  only exposes to JavaScript because the API lists it in the CORS
 *  `expose_headers`. */
async function downloadRequest(
  path: string,
  init: RequestInit = {},
): Promise<Download> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { ...(await authHeader()), ...(init.headers ?? {}) },
  });
  if (!res.ok) await fail(res);

  const disposition = res.headers.get("Content-Disposition") ?? "";
  const match = /filename="?([^"]+)"?/.exec(disposition);
  return { blob: await res.blob(), filename: match?.[1] ?? "ventas_por_mes.pdf" };
}

/** The rendered PDF report for a freshly uploaded file. */
export function extractPdf(file: File): Promise<Download> {
  const body = new FormData();
  body.append("file", file);
  // No Content-Type header: the browser must set the multipart boundary itself.
  return downloadRequest("/extract.pdf", { method: "POST", body });
}

/** The same report for a stored document, rebuilt server-side from its saved
 *  rows — the original upload is not kept, so there is no file to re-send. */
export function historyPdf(rut: string): Promise<Download> {
  return downloadRequest(`/history/${encodeURIComponent(rut)}.pdf`);
}

/** `?desde=&hasta=` for a period range, omitting the bounds that are not set.
 *  An empty range produces an empty string, so /report/{rut} then means exactly
 *  what /history/{rut} means: the whole stored document. */
function rangeQuery(range?: PeriodRange): string {
  const params = new URLSearchParams();
  if (range?.desde) params.set("desde", range.desde);
  if (range?.hasta) params.set("hasta", range.hasta);
  const query = params.toString();
  return query ? `?${query}` : "";
}

/** RUTs this user has uploaded before, optionally narrowed by RUT.
 *
 *  `query` is matched against the **RUT only**, never the company name: the API
 *  reads it as a RUT fragment with dots and hyphens ignored, so "79527050"
 *  finds 79.527.050-7 and a company name finds nothing. Omitting it (or passing
 *  a blank string) returns the whole list, so the caller keeps one code path. */
export const listRuts = (query?: string) => {
  const q = query?.trim();
  return request<RutsResponse>(
    q ? `/ruts?${new URLSearchParams({ q })}` : "/ruts",
  );
};

/** The (year, month) periods stored for a RUT, ascending.
 *
 *  What the period picker offers, so it can only ever produce a span the
 *  database can answer. 404s when the caller has nothing stored for the RUT. */
export const storedPeriods = (rut: string) =>
  request<PeriodsResponse>(`/periods/${encodeURIComponent(rut)}`);

/** The user's latest stored extraction for a RUT. */
export const history = (rut: string) =>
  request<ExtractResponse>(`/history/${encodeURIComponent(rut)}`);

/** The same stored document narrowed to a period range.
 *
 *  Server-side on purpose: the accumulated columns restart at `desde`, and only
 *  the API can recompute them while still comparing each month against the same
 *  month a year earlier — which may sit outside the range the caller asked for. */
export const report = (rut: string, range?: PeriodRange) =>
  request<ExtractResponse>(
    `/report/${encodeURIComponent(rut)}${rangeQuery(range)}`,
  );

/** That narrowed report as a PDF, rendered from the same stored rows. */
export const reportPdf = (rut: string, range?: PeriodRange) =>
  downloadRequest(`/report/${encodeURIComponent(rut)}.pdf${rangeQuery(range)}`);

export const getProfile = () => request<Profile>("/me");

export const updateProfile = (changes: {
  nombre?: string;
  email?: string;
  password?: string;
}) =>
  request<Profile>("/me", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
