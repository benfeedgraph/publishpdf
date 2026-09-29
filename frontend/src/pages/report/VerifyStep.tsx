import { useMutation, useQueryClient } from "@tanstack/react-query";
import type React from "react";
import { post, useTenantRole, type Report, type VersionDetail } from "../../api";
import { useMe } from "../../App";
import ValidationTab from "./ValidationTab";

interface Agent { key: string; name: string; description: string; ran: boolean; confirmed: number; blocking: number; warnings: number }

const STAGE_ORDER = ["queued", "extract", "render", "validate", "done"];

function stageProgress(stage: string | null, status: string): { pct: number; label: string; exact: boolean } {
  if (status !== "processing") return { pct: 100, label: "Done", exact: true };
  const s = stage ?? "queued";
  const m = s.match(/reading page (\d+) of (\d+)/);
  if (m) return { pct: 5 + (Number(m[1]) / Number(m[2])) * 55, label: `Reading page ${m[1]} of ${m[2]}`, exact: true };
  const v = s.match(/validate: (.+) \((\d+) of (\d+)\)/);
  if (v) return { pct: 70 + (Number(v[2]) / Number(v[3])) * 28, label: `Checking figures — ${v[1]}`, exact: true };
  const key = s.split(":")[0];
  const i = Math.max(0, STAGE_ORDER.indexOf(key));
  const label = { queued: "Waiting to start", extract: "Reading the PDF", render: "Building the web page", validate: "Checking every figure" }[key] ?? "Working";
  return { pct: [3, 30, 65, 80][i] ?? 50, label, exact: false };
}

export default function VerifyStep({ tenantId, report, version, onContinue }: { tenantId: string; report: Report; version: VersionDetail; onContinue: () => void }) {
  const me = useMe().data;
  const { isAdmin } = useTenantRole(me, tenantId);
  const qc = useQueryClient();
  const rerun = useMutation({
    mutationFn: () => post(`/api/tenants/${tenantId}/reports/${report.id}/versions/${version.id}/rerun`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["version", version.id] }),
  });
  const s = version.validation as (VersionDetail["validation"] & { agents?: Agent[]; consensus?: Record<string, number> }) | null;
  const processing = version.status === "processing";
  const p = stageProgress(version.stage, version.status);
  const cons = s?.consensus;
  const consTotal = cons ? Object.values(cons).reduce((a, b) => a + b, 0) : 0;

  if (version.status === "failed") {
    return (
      <div className="card">
        <div className="callout bad" role="alert"><strong>We couldn't process this PDF.</strong> {version.error ?? "Something went wrong."}</div>
        {isAdmin && <button className="primary" onClick={() => rerun.mutate()} disabled={rerun.isPending}>Try again</button>}
      </div>
    );
  }

  return (
    <div className="stack">
      {processing && (
        <div className="card">
          <div className="toolbar" style={{ justifyContent: "space-between", marginBottom: 10 }}>
            <h2 style={{ margin: 0 }}>{p.label}…</h2>
            <span className="muted small">{version.pages.length ? `${version.pages.length} pages` : version.page_count ? `${version.page_count} pages` : ""}</span>
          </div>
          <div className={`progress ${p.exact ? "" : "indeterminate"}`}><span style={{ width: `${p.pct}%` }} /></div>
          <p className="muted small" style={{ marginTop: 10 }}>Large reports take a few minutes. You can leave this page — processing continues in the background.</p>
        </div>
      )}

      {s && (
        <>
          <div className="stat-row">
            <div className="stat"><span className="stat-n">{s.figures_checked.toLocaleString()}</span><span className="stat-l">figures extracted</span></div>
            <div className="stat"><span className="stat-n ok-text">{s.passed.toLocaleString()}</span><span className="stat-l">passed every check</span></div>
            <div className="stat"><span className={`stat-n ${s.blocking ? "error" : ""}`}>{s.blocking}</span><span className="stat-l">need your attention</span></div>
            <div className="stat"><span className="stat-n muted">{s.warnings}</span><span className="stat-l">warnings</span></div>
          </div>

          {cons && consTotal > 0 && (
            <div className="card">
              <h2>How many independent agents confirmed each number</h2>
              <div className="consensus" role="img" aria-label="Share of numbers confirmed by three or more, two, one, or no agents">
                {(["3+", "2", "1", "0"] as const).map((k) => cons[k] ? <span key={k} className={`c${k[0]}`} style={{ width: `${(cons[k] / consTotal) * 100}%` }} title={`${cons[k]} numbers`} /> : null)}
              </div>
              <div className="legend">
                <span><i className="c3" />3 or more agents: {cons["3+"].toLocaleString()}</span>
                <span><i className="c2" />2 agents: {cons["2"].toLocaleString()}</span>
                <span><i className="c1" />1 agent: {cons["1"].toLocaleString()}</span>
                <span><i className="c0" />not confirmed: {cons["0"].toLocaleString()}</span>
              </div>
            </div>
          )}

          {s.agents && (
            <div className="card">
              <h2>Validation agents</h2>
              <p className="muted small">Each agent checks the extracted data a different way. A number is published only when nothing blocks it.</p>
              <div className="agents">
                {s.agents.filter((a) => a.key !== "schema").map((a) => {
                  const state = !a.ran ? "idle" : a.blocking ? "bad" : a.warnings ? "warn" : "ok";
                  return (
                    <div key={a.key} className={`agent ${state}`}>
                      <span className="agent-dot" aria-hidden>{state === "ok" ? "✓" : state === "bad" ? "!" : state === "warn" ? "•" : "–"}</span>
                      <div>
                        <div className="agent-name">{a.name}</div>
                        <div className="agent-desc">{a.description}</div>
                        <div className="agent-res">
                          {!a.ran ? <span className="muted">{a.key === "second_parser" ? "Not applicable (scanned pages have no text layer)" : "Not run"}</span> : (
                            [a.confirmed > 0 && <span key="c" className="ok-text">{a.confirmed.toLocaleString()} confirmed</span>,
                             a.blocking > 0 && <span key="b" className="error">{a.blocking} blocking</span>,
                             a.warnings > 0 && <span key="w" className="muted">{a.warnings} warning{a.warnings > 1 ? "s" : ""}</span>]
                              .filter(Boolean).reduce<React.ReactNode[]>((acc, x, i) => (i ? [...acc, " · ", x] : [x]), [])
                              .concat(!a.confirmed && !a.blocking && !a.warnings ? [<span key="n" className="ok-text">No problems</span>] : [])
                          )}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {!processing && s.blocking === 0 && (
            <div className="callout ok row" style={{ justifyContent: "space-between" }}>
              <span><strong>All checks passed.</strong> Every figure matches the PDF. Next, choose how the page should look.</span>
              <button className="primary" onClick={onContinue}>Continue to design →</button>
            </div>
          )}
          {!processing && s.blocking > 0 && (
            <div className="callout bad"><strong>{s.blocking} item(s) need a decision before this can be published.</strong> Each one shows the exact spot in the PDF next to what we read — confirm it, correct it, or mark it as not a figure.</div>
          )}
        </>
      )}

      {s && (s.blocking > 0 || s.warnings > 0) && <ValidationTab tenantId={tenantId} report={report} version={version} embedded />}
      {isAdmin && !processing && version.published_at === null && (
        <p className="muted small">
          Checks improved since this ran?{" "}
          <button className="link" disabled={rerun.isPending} onClick={() => rerun.mutate()}>Re-read the PDF and re-run all checks</button>
        </p>
      )}
    </div>
  );
}
