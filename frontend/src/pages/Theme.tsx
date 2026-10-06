import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { api, del, post, put, upload, useTenantRole } from "../api";
import { useMe } from "../App";
import DesignPicker, { type ThemeT } from "../components/DesignPicker";
import { Button, Spinner } from "../components/Spinner";

export default function Theme() {
  const { tenantId } = useParams();
  const me = useMe().data;
  const { isAdmin } = useTenantRole(me, tenantId);
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings", tenantId], queryFn: () => api<{ theme: ThemeT }>(`/api/tenants/${tenantId}/settings`) });
  const [msg, setMsg] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: ({ theme, mode }: { theme: ThemeT; mode: string }) => put(`/api/tenants/${tenantId}/theme`, { mode, theme }),
    onSuccess: () => { setMsg("Saved. New reports use this design. Existing published pages keep theirs until you apply it."); qc.invalidateQueries({ queryKey: ["settings", tenantId] }); },
    onError: (e: Error) => setMsg(e.message),
  });
  const applyLive = useMutation({
    mutationFn: () => post<{ drafts_created: string[] }>(`/api/tenants/${tenantId}/theme/apply-to-live`),
    onError: (e: Error) => setMsg(e.message),
    onSuccess: (r) => setMsg(r.drafts_created.length ? `Created ${r.drafts_created.length} new draft(s) in this design. Review and publish them from Reports.` : "No published reports yet."),
  });
  if (settings.isError) return <p className="error">{settings.error.message}</p>;
  if (!settings.data) return <><h1>Brand & design</h1><div className="sk sk-line" /><div className="sk sk-block" /></>;

  return (
    <div className="stack">
      <div className="page-head" style={{ marginBottom: 0 }}>
        <div>
          <h1>Brand & design</h1>
          <p className="muted">The default look for your report pages. Each report can also have its own design, chosen during its Design step.</p>
        </div>
        {isAdmin && <Button className="secondary" busy={applyLive.isPending} busyLabel="Creating drafts in this design" onClick={() => applyLive.mutate()}>Apply to published reports…</Button>}
      </div>
      {msg && <p className="callout info" role="status">{msg}</p>}
      <Logo tenantId={tenantId!} has={!!settings.data.theme.logo} disabled={!isAdmin} onChange={() => qc.invalidateQueries({ queryKey: ["settings", tenantId] })} />
      {isAdmin ? (
        <DesignPicker tenantId={tenantId!} current={settings.data.theme} busy={save.isPending} applyLabel="Save as workspace design"
          onApply={(theme, { mode }) => save.mutate({ theme, mode })} />
      ) : <p className="muted">Only admins can change the design.</p>}
    </div>
  );
}

function Logo({ tenantId, has, onChange, disabled }: { tenantId: string; has: boolean; onChange: () => void; disabled: boolean }) {
  const [err, setErr] = useState<string | null>(null);
  const [v, setV] = useState(0);
  const [busy, setBusy] = useState<null | "upload" | "remove">(null);
  return (
    <div className="card row" style={{ justifyContent: "space-between" }}>
      <div className="row">
        {has ? <img className="logo-thumb" src={`/api/tenants/${tenantId}/theme/logo?v=${v}`} alt="Current logo" /> : <span className="muted small">No logo yet</span>}
        <div><strong>Logo</strong><div className="muted small">Shown in the header of every page. PNG, JPEG or WebP.</div></div>
      </div>
      {!disabled && (
        <div className="btn-row">
          <label className={`button secondary ${busy === "upload" ? "is-busy" : ""}`} aria-busy={busy === "upload" || undefined}>
            <span className="btn-label">{has ? "Replace logo" : "Upload logo"}</span>
            {busy === "upload" && <Spinner label="Uploading the logo" />}
            <input type="file" hidden disabled={busy !== null} accept="image/png,image/jpeg,image/webp" onChange={async (e) => {
              const f = e.target.files?.[0]; if (!f) return;
              const form = new FormData(); form.append("file", f);
              setBusy("upload");
              try { await upload(`/api/tenants/${tenantId}/theme/logo`, form); setErr(null); setV((x) => x + 1); onChange(); } catch (x) { setErr((x as Error).message); }
              finally { setBusy(null); e.target.value = ""; }
            }} />
          </label>
          {has && <Button className="link danger" busy={busy === "remove"} disabled={busy !== null} busyLabel="Removing the logo" onClick={async () => {
            setBusy("remove");
            try { await del(`/api/tenants/${tenantId}/theme/logo`); setErr(null); onChange(); } catch (x) { setErr((x as Error).message); }
            finally { setBusy(null); }
          }}>Remove</Button>}
        </div>
      )}
      {err && <p className="error">{err}</p>}
    </div>
  );
}
