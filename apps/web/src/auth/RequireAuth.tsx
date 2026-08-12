import { Navigate, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import { useAuth } from "./AuthProvider";

/** Gate for the signed-in routes.
 *
 *  The redirect remembers where the user was headed, so signing in lands them on
 *  the page they asked for instead of always on the home screen. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { session, loading } = useAuth();
  const location = useLocation();

  if (loading) return <p className="muted centered">Cargando…</p>;
  if (!session) return <Navigate to="/login" replace state={{ from: location }} />;
  return <>{children}</>;
}
