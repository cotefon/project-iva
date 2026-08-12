import { supabase } from "./supabase";
import type { ExtractResponse, Profile, RutsResponse } from "../types";

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

/** The rendered PDF report for the same file, as a blob plus its filename.
 *
 *  The filename comes from the Content-Disposition header, which the browser
 *  only exposes to JavaScript because the API lists it in the CORS
 *  `expose_headers`. */
export async function extractPdf(
  file: File,
): Promise<{ blob: Blob; filename: string }> {
  const body = new FormData();
  body.append("file", file);
  const res = await fetch(`${BASE}/extract.pdf`, {
    method: "POST",
    body,
    headers: await authHeader(),
  });
  if (!res.ok) await fail(res);

  const disposition = res.headers.get("Content-Disposition") ?? "";
  const match = /filename="?([^"]+)"?/.exec(disposition);
  return { blob: await res.blob(), filename: match?.[1] ?? "ventas_por_mes.pdf" };
}

/** RUTs this user has uploaded before. */
export const listRuts = () => request<RutsResponse>("/ruts");

/** The user's latest stored extraction for a RUT. */
export const history = (rut: string) =>
  request<ExtractResponse>(`/history/${encodeURIComponent(rut)}`);

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
