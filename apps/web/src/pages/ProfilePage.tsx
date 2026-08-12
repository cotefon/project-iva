import { useEffect, useState } from "react";
import { ApiError, getProfile, updateProfile } from "../lib/api";
import { supabase } from "../lib/supabase";

export function ProfilePage() {
  const [nombre, setNombre] = useState("");
  const [email, setEmail] = useState("");
  /** The values last saved, so we only send fields that actually changed. */
  const [saved, setSaved] = useState({ nombre: "", email: "" });

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");

  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    getProfile()
      .then((p) => {
        setNombre(p.nombre ?? "");
        setEmail(p.email ?? "");
        setSaved({ nombre: p.nombre ?? "", email: p.email ?? "" });
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setNotice(null);

    if (password && password !== confirm) {
      setError("Las contraseñas no coinciden.");
      return;
    }

    const changes: { nombre?: string; email?: string; password?: string } = {};
    if (nombre !== saved.nombre) changes.nombre = nombre;
    if (email !== saved.email) changes.email = email;
    if (password) changes.password = password;

    if (Object.keys(changes).length === 0) {
      setNotice("No hay cambios que guardar.");
      return;
    }

    setBusy(true);
    try {
      const updated = await updateProfile(changes);
      setNombre(updated.nombre ?? "");
      setSaved({ nombre: updated.nombre ?? "", email: saved.email });
      setPassword("");
      setConfirm("");

      if (updated.email_confirmation_pending) {
        // The address in auth.users has not changed yet, so keep showing the old
        // one as saved until the user confirms.
        setNotice(
          "Guardado. Revisa tu correo nuevo para confirmar el cambio de dirección.",
        );
      } else {
        setNotice("Perfil actualizado.");
      }

      // Pull the refreshed user into the session so the top bar and the next
      // request use the current values.
      await supabase.auth.refreshSession();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (loading) return <p className="muted">Cargando…</p>;

  return (
    <form className="card" onSubmit={submit}>
      <h1>Mi perfil</h1>

      <label className="field">
        <span>Nombre</span>
        <input
          value={nombre}
          onChange={(e) => setNombre(e.target.value)}
          autoComplete="name"
        />
      </label>

      <label className="field">
        <span>Correo</span>
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="email"
        />
      </label>

      <label className="field">
        <span>Nueva contraseña (deja en blanco para no cambiarla)</span>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="new-password"
          minLength={6}
        />
      </label>

      <label className="field">
        <span>Repetir nueva contraseña</span>
        <input
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          autoComplete="new-password"
        />
      </label>

      {error && <p className="error">{error}</p>}
      {notice && <p className="ok">{notice}</p>}

      <button className="primary" type="submit" disabled={busy}>
        {busy ? "Guardando…" : "Guardar cambios"}
      </button>
    </form>
  );
}
