import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { supabase } from "../lib/supabase";
import { useAuth } from "../auth/AuthProvider";

export function Layout() {
  const { session } = useAuth();
  const navigate = useNavigate();

  async function signOut() {
    await supabase.auth.signOut();
    navigate("/login", { replace: true });
  }

  const linkClass = ({ isActive }: { isActive: boolean }) =>
    isActive ? "active" : "";

  return (
    <>
      <nav className="nav">
        <span className="brand">Extractor IVA / F29</span>
        <NavLink to="/" className={linkClass} end>
          Extraer
        </NavLink>
        <NavLink to="/perfil" className={linkClass}>
          Mi perfil
        </NavLink>
        <span className="spacer" />
        <span className="muted">{session?.user.email}</span>
        <button onClick={signOut}>Cerrar sesión</button>
      </nav>
      <main className="page">
        <Outlet />
      </main>
    </>
  );
}
