import { useQuery } from "@tanstack/react-query";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, ApiError, type Me } from "./api";
import { cachedMe, forgetMe, rememberMe } from "./session";
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
import Landing from "./pages/Landing";
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
        const me = await api<Me>("/api/auth/me");
        rememberMe(me);
        return me;
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) { forgetMe(); return null; }
        throw e;
      }
    },
    initialData: cachedMe,
    initialDataUpdatedAt: 0,          // always re-check with the server straight away
    staleTime: 30_000,                // …but a sign-in that just fetched it needn't ask twice
  });
}

/** Shown only on a first visit, while the server confirms who you are. */
function ShellSkeleton() {
  return (
    <div className="shell skeleton-shell" aria-busy="true" aria-label="Opening your workspace">
      <aside className="sidebar"><div className="sk sk-logo" />{[0, 1, 2, 3, 4, 5].map((i) => <div key={i} className="sk sk-nav" />)}</aside>
      <main className="content"><div className="sk sk-title" /><div className="sk sk-line" /><div className="sk sk-card" /><div className="sk sk-card" /></main>
    </div>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
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
  if (me.isPending) return <ShellSkeleton />;
  if (me.isError && !me.data) return <p className="center error">Couldn't reach the server. Refresh to try again.</p>;
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
