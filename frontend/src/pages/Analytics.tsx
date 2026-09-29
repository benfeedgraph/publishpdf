import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { api, put, useTenantRole } from "../api";
import { useMe } from "../App";

interface Stats {
  days: number;
  reports: { report_id: string; label: string; views: number; crawlers: Record<string, number> }[];
  daily: { day: string; views: number; crawler_visits: number }[];
}

export default function Analytics() {
  const { tenantId } = useParams();
  const me = useMe().data;
  const { isAdmin } = useTenantRole(me, tenantId);
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings", tenantId], queryFn: () => api<{ ga4_measurement_id: string | null; consent_banner_enabled: boolean }>(`/api/tenants/${tenantId}/settings`) });
  const [days, setDays] = useState(30);
  const stats = useQuery({ queryKey: ["stats", tenantId, days], queryFn: () => api<Stats>(`/api/tenants/${tenantId}/analytics/stats?days=${days}`) });
  const [gid, setGid] = useState("");
  const [banner, setBanner] = useState(true);
  const [ack, setAck] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => { if (settings.data) { setGid(settings.data.ga4_measurement_id ?? ""); setBanner(settings.data.consent_banner_enabled); } }, [settings.data]);
  const save = useMutation({
    mutationFn: () => put(`/api/tenants/${tenantId}/analytics`, { ga4_measurement_id: gid || null, consent_banner_enabled: banner, consent_banner_off_ack: banner ? null : ack }),
    onSuccess: () => { setMsg("Saved. Your published pages use this from the next visit."); qc.invalidateQueries({ queryKey: ["settings", tenantId] }); },
    onError: (e: Error) => setMsg(e.message),
  });
  function submit(e: FormEvent) { e.preventDefault(); save.mutate(); }

  const maxDay = Math.max(1, ...(stats.data?.daily.map((d) => d.views + d.crawler_visits) ?? [1]));
  const crawlers = new Map<string, number>();
  stats.data?.reports.forEach((r) => Object.entries(r.crawlers).forEach(([k, n]) => crawlers.set(k, (crawlers.get(k) ?? 0) + n)));

  return (
    <>
      <h1>Analytics</h1>
      <form className="card stack" onSubmit={submit}>
        <h2>Google Analytics 4</h2>
        <p className="muted small">Added only to your published report pages — never to this dashboard or anyone else's site.</p>
        <label className="field"><span className="label">Measurement ID</span><input value={gid} disabled={!isAdmin} onChange={(e) => setGid(e.target.value.toUpperCase())} placeholder="G-XXXXXXXXXX" pattern="G-[A-Z0-9]{4,12}" /></label>
        <label className="check"><input type="checkbox" checked={banner} disabled={!isAdmin} onChange={(e) => setBanner(e.target.checked)} /> Show a cookie consent banner (analytics load only after a visitor accepts)</label>
        {!banner && (
          <label className="field"><span className="label">Why no banner? (recorded in the audit log)</span>
            <input required value={ack} onChange={(e) => setAck(e.target.value)} placeholder="e.g. Legal confirmed another lawful basis for our markets" /></label>
        )}
        {isAdmin && <div><button className="primary" disabled={save.isPending}>Save</button></div>}
        {msg && <p className="small" role="status">{msg}</p>}
      </form>

      <div className="card">
        <div className="toolbar">
          <h2>Visits (from our servers, no cookies)</h2>
          <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Period"><option value={7}>Last 7 days</option><option value={30}>Last 30 days</option><option value={90}>Last 90 days</option></select>
        </div>
        {stats.data && stats.data.daily.length === 0 && <p className="muted">No visits yet.</p>}
        {stats.data && stats.data.daily.length > 0 && (
          <>
            <div className="bars" role="img" aria-label="Daily visits: people and crawlers">
              {stats.data.daily.map((d) => (
                <div key={d.day} className="bar" title={`${d.day}: ${d.views} people, ${d.crawler_visits} crawler visits`}>
                  <span className="b-bot" style={{ height: `${(d.crawler_visits / maxDay) * 100}%` }} />
                  <span className="b-human" style={{ height: `${(d.views / maxDay) * 100}%` }} />
                </div>
              ))}
            </div>
            <p className="legend small"><i className="b-human" /> people <i className="b-bot" /> AI and search crawlers</p>
            <h3>By report</h3>
            <table>
              <thead><tr><th scope="col">Report</th><th scope="col">Page views</th><th scope="col">Crawler visits</th></tr></thead>
              <tbody>
                {stats.data.reports.map((r) => (
                  <tr key={r.report_id}><td>{r.label}</td><td>{r.views}</td>
                    <td className="small">{Object.entries(r.crawlers).sort((a, b) => b[1] - a[1]).map(([k, n]) => `${k} ${n}`).join(" · ") || "—"}</td></tr>
                ))}
              </tbody>
            </table>
            <h3>By crawler</h3>
            <table>
              <thead><tr><th scope="col">Crawler</th><th scope="col">Visits</th></tr></thead>
              <tbody>{[...crawlers.entries()].sort((a, b) => b[1] - a[1]).map(([k, n]) => <tr key={k}><td>{k}</td><td>{n}</td></tr>)}</tbody>
            </table>
          </>
        )}
      </div>
    </>
  );
}
