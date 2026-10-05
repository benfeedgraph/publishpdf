import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { api, post, put, useTenantRole } from "../api";
import { useMe } from "../App";
import AiUsagePanel, { type AiUsageSummary } from "../components/AiUsage";

interface S { disclaimer: string | null; effective_disclaimer: string | null; robots_policy: string; llm_assist_enabled: boolean }

const ROBOTS: [string, string, string][] = [
  ["allow_all", "Allow all crawlers (recommended)", "Search engines and AI assistants can read and learn from your reports."],
  ["search_and_answer", "Search and AI answers only", "Blocks AI training crawlers (GPTBot, ClaudeBot, CCBot, Google-Extended…) but allows search and assistants answering questions."],
  ["block_ai", "Block AI crawlers", "Only regular search engines. AI assistants won't read your pages directly."],
];

export default function Settings() {
  const { tenantId } = useParams();
  const me = useMe().data;
  const { isAdmin } = useTenantRole(me, tenantId);
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["settings", tenantId], queryFn: () => api<S>(`/api/tenants/${tenantId}/settings`) });
  const [disc, setDisc] = useState("");
  const [robots, setRobots] = useState("allow_all");
  const [llm, setLlm] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => { if (q.data) { setDisc(q.data.disclaimer ?? ""); setRobots(q.data.robots_policy); setLlm(q.data.llm_assist_enabled); } }, [q.data]);
  const save = useMutation({
    mutationFn: () => put(`/api/tenants/${tenantId}/settings`, { disclaimer: disc, robots_policy: robots, llm_assist_enabled: llm }),
    onSuccess: () => { setMsg("Saved."); qc.invalidateQueries({ queryKey: ["settings", tenantId] }); },
    onError: (e: Error) => setMsg(e.message),
  });
  if (!q.data) return <p className="muted">Loading…</p>;
  function submit(e: FormEvent) { e.preventDefault(); save.mutate(); }
  return (
    <>
      <h1>Settings</h1>
      <form className="stack" onSubmit={submit}>
        <div className="card">
          <h2>Disclaimer</h2>
          <p className="muted small">Shown on every published page, stating that the PDF filing is the official document. Plain text; blank lines make paragraphs, **bold** and [links](https://…) are allowed. Publishing is blocked until a disclaimer exists.</p>
          <textarea rows={5} value={disc} disabled={!isAdmin} onChange={(e) => setDisc(e.target.value)} aria-label="Disclaimer" />
          {!disc && q.data.effective_disclaimer && <p className="small muted">Currently using the platform default: “{q.data.effective_disclaimer}”</p>}
          {!disc && !q.data.effective_disclaimer && <p className="small error">No disclaimer set — you can't publish until you add one.</p>}
        </div>
        <div className="card">
          <h2>AI and search crawlers</h2>
          {ROBOTS.map(([k, l, d]) => (
            <label key={k} className="radio"><input type="radio" name="robots" checked={robots === k} disabled={!isAdmin} onChange={() => setRobots(k)} /> <span><strong>{l}</strong><br /><span className="muted small">{d}</span></span></label>
          ))}
          <p className="muted small">Preview addresses are always blocked from indexing, whatever you choose here.</p>
        </div>
        <div className="card">
          <h2>AI-assisted section labels</h2>
          <label className="check"><input type="checkbox" checked={llm} disabled={!isAdmin} onChange={(e) => setLlm(e.target.checked)} /> Let an AI model help label sections that our rules can't classify</label>
          <p className="muted small">Off by default. Only section headings with every digit removed are sent; the model can only pick a section type. It never sees, produces or changes a figure. Requires the platform operator to have configured a provider.</p>
          <p className="muted small">When on, it runs automatically on each upload and re-run where some headings are left unlabelled, and uses AI credits. Every call is recorded below under AI usage with its real cost, and it stops running once your workspace reaches its monthly AI credit limit.</p>
        </div>
        {isAdmin && <div><button className="primary" disabled={save.isPending}>Save settings</button> {msg && <span className="small" role="status">{msg}</span>}</div>}
      </form>
      <AiUsageCard tenantId={tenantId!} />
      <TwoFactor />
    </>
  );
}

function AiUsageCard({ tenantId }: { tenantId: string }) {
  const q = useQuery({ queryKey: ["ai-usage", tenantId], queryFn: () => api<AiUsageSummary>(`/api/tenants/${tenantId}/ai-usage`) });
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2>AI usage</h2>
      {q.isError ? <p className="error small">{q.error.message}</p> : q.data ? <AiUsagePanel data={q.data} tenantId={tenantId} /> : <p className="muted small">Loading…</p>}
    </div>
  );
}

function TwoFactor() {
  const me = useMe();
  const qc = useQueryClient();
  const [enrol, setEnrol] = useState<{ otpauth_uri: string; qr_svg: string } | null>(null);
  const [code, setCode] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  if (!me.data) return null;
  const on = me.data.mfa.enrolled;
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2>Two-factor sign-in (optional)</h2>
      {on ? <p className="ok-text">On. You'll be asked for a code from your authenticator app each time you sign in.</p> : (
        <>
          <p className="muted small">Add a second step with an authenticator app (Google Authenticator, 1Password…) on top of the email code.</p>
          {!enrol && <button className="secondary" onClick={async () => { try { setEnrol(await post("/api/auth/mfa/enroll")); } catch (e) { setMsg((e as Error).message); } }}>Set up two-factor</button>}
          {enrol && (
            <form className="stack" onSubmit={async (e) => {
              e.preventDefault();
              try { await post("/api/auth/mfa/confirm", { code }); await qc.invalidateQueries({ queryKey: ["me"] }); setEnrol(null); setMsg("Two-factor is on."); }
              catch (err) { setMsg((err as Error).message); }
            }}>
              <div className="qr" dangerouslySetInnerHTML={{ __html: enrol.qr_svg }} />
              <label className="field" style={{ maxWidth: 220 }}><span className="label">6-digit code from the app</span><input inputMode="numeric" required value={code} onChange={(e) => setCode(e.target.value)} /></label>
              <div><button className="primary">Turn on</button></div>
            </form>
          )}
        </>
      )}
      {msg && <p className="small" role="status">{msg}</p>}
    </div>
  );
}
