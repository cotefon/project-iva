import { createClient } from "@supabase/supabase-js";

const url = import.meta.env.VITE_SUPABASE_URL;
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY;

if (!url || !anonKey) {
  // Fail loudly at startup rather than with an opaque network error on the first
  // login attempt.
  throw new Error(
    "Faltan VITE_SUPABASE_URL o VITE_SUPABASE_ANON_KEY. Copia web/.env.example a web/.env.local.",
  );
}

/** Supabase Auth is the only identity system: it owns sign-up, sign-in, password
 *  hashing and the session refresh loop. The FastAPI service never sees a
 *  password — it only verifies the access token this client issues (see auth.py).
 *
 *  `persistSession` keeps the session in localStorage so a page reload stays
 *  signed in, and `autoRefreshToken` renews the access token before it expires,
 *  which is what stops long sessions from suddenly 401-ing mid-upload. */
export const supabase = createClient(url, anonKey, {
  auth: { persistSession: true, autoRefreshToken: true },
});
