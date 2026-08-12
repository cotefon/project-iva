import { useState } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { supabase } from "../lib/supabase";
import { useAuth } from "../auth/AuthProvider";

type Mode = "signin" | "signup";

export function LoginPage() {
  const { session, loading } = useAuth();
  const location = useLocation() as { state?: { from?: { pathname: string } } };

  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  if (loading) return <p className="muted centered">Cargando…</p>;
  // Already signed in: go where the guard was sending them, or home.
  if (session) {
    return <Navigate to={location.state?.from?.pathname ?? "/"} replace />;
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);

    const { data, error } =
      mode === "signin"
        ? await supabase.auth.signInWithPassword({ email, password })
        : await supabase.auth.signUp({ email, password });

    setBusy(false);
    if (error) {
      setError(error.message);
      return;
    }
    // With email confirmation switched on, signUp returns a user but no
    // session — say so instead of leaving the screen looking stuck.
    if (mode === "signup" && !data.session) {
      setNotice("Cuenta creada. Revisa tu correo para confirmarla antes de entrar.");
      setMode("signin");
    }
    // On success the AuthProvider's onAuthStateChange fires and the redirect
    // above takes over.
  }

  return (
    <form className="card" onSubmit={submit}>
      <h1>{mode === "signin" ? "Iniciar sesión" : "Crear cuenta"}</h1>

      <label className="field">
        <span>Correo</span>
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="email"
          required
        />
      </label>

      <label className="field">
        <span>Contraseña</span>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete={mode === "signin" ? "current-password" : "new-password"}
          minLength={6}
          required
        />
      </label>

      {error && <p className="error">{error}</p>}
      {notice && <p className="ok">{notice}</p>}

      <button className="primary" type="submit" disabled={busy}>
        {busy ? "Un momento…" : mode === "signin" ? "Entrar" : "Registrarme"}
      </button>

      <p className="muted" style={{ marginTop: "1rem" }}>
        {mode === "signin" ? "¿No tienes cuenta? " : "¿Ya tienes cuenta? "}
        <button
          type="button"
          className="link"
          onClick={() => {
            setMode(mode === "signin" ? "signup" : "signin");
            setError(null);
            setNotice(null);
          }}
        >
          {mode === "signin" ? "Crear una" : "Iniciar sesión"}
        </button>
      </p>
    </form>
  );
}
