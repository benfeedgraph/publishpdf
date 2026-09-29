import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { api, del, formatDateTime, post, useTenantRole } from "../api";
import { useMe } from "../App";

interface DomainT {
  id: string; hostname: string; status: string; failure_code: string | null; failure_reason: string | null;
  last_checked_at: string | null; verified_at: string | null; live_at: string | null; cert_expires_at: string | null;
  records: { type: string; name: string; host_label: string; value: string; ttl: number }[];
  providers: Record<string, string[]>; it_email: string;
}
interface Resp { domain: DomainT | null; preview_origin: string; site_origin: string; custom_domain_live: boolean }

const STEPS = ["pending", "verified", "ssl_issuing", "live"] as const;
const STEP_LABEL: Record<string, string> = { pending: "Pending DNS", verified: "Verified", ssl_issuing: "Issuing SSL", live: "Live", failed: "Needs attention" };
const PROVIDERS: [string, string][] = [["cloudflare", "Cloudflare"], ["godaddy", "GoDaddy"], ["route53", "AWS Route 53"], ["squarespace", "Squarespace / Google Domains"], ["other", "Other"]];

function Copy({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return <button type="button" className="link small" onClick={() => navigator.clipboard.writeText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); })}>{done ? "Copied" : "Copy"}</button>;
}

export default function Domain() {
  const { tenantId } = useParams();
  const me = useMe().data;
  const { isAdmin } = useTenantRole(me, tenantId);
  const qc = useQueryClient();
  const key = ["domain", tenantId];
  const q = useQuery({ queryKey: key, queryFn: () => api<Resp>(`/api/tenants/${tenantId}/domain`),
                       refetchInterval: (s) => (s.state.data?.domain && s.state.data.domain.status !== "live" ? 15000 : false) });
  const [host, setHost] = useState("");
  const [provider, setProvider] = useState("cloudflare");
  const [err, setErr] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: key });
  const add = useMutation({ mutationFn: () => post(`/api/tenants/${tenantId}/domain`, { hostname: host }), onSuccess: () => { setErr(null); refresh(); }, onError: (e: Error) => setErr(e.message) });
  const check = useMutation({ mutationFn: () => post(`/api/tenants/${tenantId}/domain/check`), onSuccess: refresh, onError: (e: Error) => setErr(e.message) });
  const remove = useMutation({ mutationFn: () => del(`/api/tenants/${tenantId}/domain`), onSuccess: refresh });

  if (q.isPending) return <p className="muted">Loading…</p>;
  if (q.isError) return <p className="error">{q.error.message}</p>;
  const { domain: d, preview_origin } = q.data;
  function submit(e: FormEvent) { e.preventDefault(); add.mutate(); }

  return (
    <>
      <h1>Custom domain</h1>
      <p className="muted">Serve your reports on your own subdomain, for example <code>investors.yourcompany.com</code>. Until it's live, your site is at <a href={preview_origin} target="_blank" rel="noreferrer">{preview_origin.replace(/^https?:\/\//, "")}</a> — a private preview address that search engines are told not to index.</p>
      {!d && isAdmin && (
        <form className="card inline-form" onSubmit={submit}>
          <input required value={host} onChange={(e) => setHost(e.target.value)} placeholder="investors.yourcompany.com" aria-label="Subdomain" />
          <button className="primary" disabled={add.isPending}>Connect subdomain</button>
          {err && <p className="error" role="alert">{err}</p>}
          <p className="muted small">Use a subdomain, not your main domain: a root domain can't point to us and would replace your website.</p>
        </form>
      )}
      {!d && !isAdmin && <p className="muted">No custom domain yet. An admin can connect one.</p>}
      {d && (
        <div className="stack">
          <div className="card">
            <div className="toolbar">
              <h2>{d.hostname}</h2>
              <span className={`badge ${d.status === "live" ? "ok" : d.status === "failed" ? "bad" : "warn"}`}>{STEP_LABEL[d.status] ?? d.status}</span>
            </div>
            <ol className="steps">
              {STEPS.map((s) => {
                const idx = STEPS.indexOf(d.status as (typeof STEPS)[number]);
                const done = d.status === "live" || (idx >= 0 && STEPS.indexOf(s) < idx) || (s === "verified" && !!d.verified_at);
                return <li key={s} className={d.status === s ? "current" : done ? "done" : ""}>{STEP_LABEL[s]}</li>;
              })}
            </ol>
            {d.failure_reason && <p className={`callout ${d.status === "failed" ? "bad" : "warn"}`} role="alert">{d.failure_reason}</p>}
            {d.status === "live" && <p className="callout ok">Live at <a href={`https://${d.hostname}`} target="_blank" rel="noreferrer">https://{d.hostname}</a>. Canonical links and the sitemap now use this address, and the preview address redirects here.{d.cert_expires_at && ` Certificate renews automatically (current one valid until ${formatDateTime(d.cert_expires_at)}).`}</p>}
            <p className="muted small">Last checked {formatDateTime(d.last_checked_at)} · we re-check automatically.</p>
            {isAdmin && (
              <div className="btn-row">
                <button className="primary" disabled={check.isPending} onClick={() => check.mutate()}>{check.isPending ? "Checking…" : "Check now"}</button>
                <button className="link danger" onClick={() => confirm(`Disconnect ${d.hostname}? The site will only be available at the preview address.`) && remove.mutate()}>Disconnect</button>
              </div>
            )}
            {err && <p className="error">{err}</p>}
          </div>
          {d.status !== "live" && (
            <div className="card">
              <h2>Add these two DNS records</h2>
              <table>
                <thead><tr><th scope="col">Type</th><th scope="col">Name</th><th scope="col">Value</th><th scope="col">TTL</th></tr></thead>
                <tbody>
                  {d.records.map((r) => (
                    <tr key={r.type}>
                      <td>{r.type}</td>
                      <td><code>{r.name}</code> <Copy text={r.name} /><div className="muted small">host part only: <code>{r.host_label}</code> <Copy text={r.host_label} /></div></td>
                      <td><code>{r.value}</code> <Copy text={r.value} /></td>
                      <td>{r.ttl}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <h3>Step by step</h3>
              <div className="seg" role="tablist" aria-label="DNS provider">
                {PROVIDERS.map(([k, l]) => <button key={k} role="tab" aria-selected={provider === k} className={provider === k ? "active" : ""} onClick={() => setProvider(k)}>{l}</button>)}
              </div>
              <ol className="provider-steps">{d.providers[provider]?.map((s) => <li key={s}>{s}</li>)}</ol>
              <h3>Send this to your IT team</h3>
              <pre className="email">{d.it_email}</pre>
              <Copy text={d.it_email} />
            </div>
          )}
        </div>
      )}
    </>
  );
}
