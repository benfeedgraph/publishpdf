import { useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { NavLink, useMatch, useNavigate } from "react-router-dom";
import { post, ROLE_LABEL, type Me } from "../api";
import { IconBuilding, IconChart, IconDesign, IconGear, IconGlobe, IconList, IconLogout, IconReports, IconShield, IconUsers } from "./Icons";

export default function Shell({ me, children }: { me: Me; children: ReactNode }) {
  const match = useMatch("/t/:tenantId/*");
  const tenantId = match?.params.tenantId ?? me.tenants[0]?.id;
  const tenant = me.tenants.find((t) => t.id === tenantId);
  const isAdmin = me.user.is_platform_admin || tenant?.role === "client_admin";
  const navigate = useNavigate();
  const qc = useQueryClient();

  async function signOut() {
    await post("/api/auth/logout");
    qc.clear();
    navigate("/login");
  }
  const initial = (me.user.name || me.user.email).slice(0, 1).toUpperCase();

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand"><img src="/brand/publishpdf-logo-full-color.svg" alt="PublishPDF" width="148" height="32" /></div>
        {me.tenants.length > 1 && (
          <label className="ws-switch">
            <span className="sr-only">Workspace</span>
            <select value={tenantId ?? ""} onChange={(e) => navigate(`/t/${e.target.value}`)}>
              {me.tenants.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          </label>
        )}
        {me.tenants.length === 1 && tenant && <div className="ws-name" title={tenant.name}>{tenant.name}</div>}
        {tenantId && (
          <nav className="nav" aria-label="Workspace">
            <NavLink end to={`/t/${tenantId}`}><IconReports /> Reports</NavLink>
            <NavLink to={`/t/${tenantId}/theme`}><IconDesign /> Brand & design</NavLink>
            <NavLink to={`/t/${tenantId}/domain`}><IconGlobe /> Domain</NavLink>
            <NavLink to={`/t/${tenantId}/analytics`}><IconChart /> Analytics</NavLink>
            <NavLink to={`/t/${tenantId}/team`}><IconUsers /> Team</NavLink>
            <NavLink to={`/t/${tenantId}/settings`}><IconGear /> Settings</NavLink>
            {isAdmin && <NavLink to={`/t/${tenantId}/audit`}><IconShield /> Audit log</NavLink>}
          </nav>
        )}
        {me.user.is_platform_admin && (
          <nav className="nav" aria-label="Platform admin">
            <div className="nav-label">Platform admin</div>
            <NavLink to="/admin/tenants"><IconBuilding /> Tenants</NavLink>
            <NavLink to="/admin/jobs"><IconList /> Job queue</NavLink>
            <NavLink to="/admin/issues"><IconShield /> Validation quality</NavLink>
          </nav>
        )}
        <div className="sidebar-foot">
          <span className="avatar" aria-hidden>{initial}</span>
          <div className="who">
            <div className="email" title={me.user.email}>{me.user.name || me.user.email}</div>
            {tenant && <div className="muted small">{ROLE_LABEL[tenant.role]}</div>}
          </div>
          <button className="link" onClick={signOut} title="Sign out" aria-label="Sign out"><IconLogout /></button>
        </div>
      </aside>
      <main className="content">{children}</main>
    </div>
  );
}
