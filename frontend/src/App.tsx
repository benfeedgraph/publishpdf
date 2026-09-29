import { useQuery } from "@tanstack/react-query";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, ApiError, type Me } from "./api";
import Shell from "./components/Shell";
import AdminIssues from "./pages/AdminIssues";
import AdminJobs from "./pages/AdminJobs";
import AdminTenantDetail from "./pages/AdminTenantDetail";
import Analytics from "./pages/Analytics";
import Domain from "./pages/Domain";
import ReportPage from "./pages/report/ReportPage";
import Settings from "./pages/Settings";
import Theme from "./pages/Theme";
import Upload from "./pages/Upload";
import AdminTenants from "./pages/AdminTenants";
import AuditLog from "./pages/AuditLog";
import Home from "./pages/Home";
import InviteAccept from "./pages/InviteAccept";
import Jobs from "./pages/Jobs";
import Login from "./pages/Login";
import Mfa from "./pages/Mfa";
import Team from "./pages/Team";
import Verify from "./pages/Verify";

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return await api<Me>("/api/auth/me");
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return null;
        throw e;
      }
    },
  });
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/auth/verify" element={<Verify />} />
      <Route path="/invite" element={<InviteAccept />} />
      <Route path="/mfa" element={<Mfa />} />
      <Route path="/*" element={<Authenticated />} />
    </Routes>
  );
}

function Authenticated() {
  const me = useMe();
  const location = useLocation();
  if (me.isPending) return <p className="center muted">Loading…</p>;
  if (me.isError) return <p className="center error">Couldn't reach the server. Refresh to try again.</p>;
  if (!me.data) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  if (!me.data.fully_authenticated) return <Navigate to="/mfa" replace />;

  const data = me.data;
  const first = data.tenants[0];
  return (
    <Shell me={data}>
      <Routes>
        <Route
          index
          element={
            first ? (
              <Navigate to={`/t/${first.id}`} replace />
            ) : data.user.is_platform_admin ? (
              <Navigate to="/admin/tenants" replace />
            ) : (
              <p className="muted">You aren't a member of any workspace yet. Ask your admin for an invitation.</p>
            )
          }
        />
        <Route path="t/:tenantId" element={<Home />} />
        <Route path="t/:tenantId/upload" element={<Upload />} />
        <Route path="t/:tenantId/reports/:reportId" element={<ReportPage />} />
        <Route path="t/:tenantId/theme" element={<Theme />} />
        <Route path="t/:tenantId/domain" element={<Domain />} />
        <Route path="t/:tenantId/analytics" element={<Analytics />} />
        <Route path="t/:tenantId/settings" element={<Settings />} />
        <Route path="t/:tenantId/team" element={<Team />} />
        <Route path="t/:tenantId/audit" element={<AuditLog />} />
        <Route path="t/:tenantId/jobs" element={<Jobs />} />
        {data.user.is_platform_admin && (
          <>
            <Route path="admin/tenants" element={<AdminTenants />} />
            <Route path="admin/tenants/:id" element={<AdminTenantDetail />} />
            <Route path="admin/jobs" element={<AdminJobs />} />
            <Route path="admin/issues" element={<AdminIssues />} />
          </>
        )}
        <Route path="*" element={<p className="muted">Page not found.</p>} />
      </Routes>
    </Shell>
  );
}
