import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type React from "react";
import { api, post, useTenantRole, type Report, type VersionDetail } from "../../api";
import { useMe } from "../../App";
import { IconAlert, IconCheck, IconSparkle } from "../../components/Icons";
import { Button, Spinner } from "../../components/Spinner";
import ValidationTab from "./ValidationTab";
import WizardFooter from "./WizardFooter";

interface Agent { key: string; name: string; description: string; ran: boolean; confirmed: number; blocking: number; warnings: number }

const STAGE_ORDER = ["queued", "extract", "render", "validate", "done"];

function stageProgress(stage: string | null, status: string): { pct: number; label: string; exact: boolean } {
  if (status !== "processing") return { pct: 100, label: "Done", exact: true };
  const s = stage ?? "queued";
  const m = s.match(/reading page (\d+) of (\d+)/);
  if (m) return { pct: 5 + (Number(m[1]) / Number(m[2])) * 55, label: `Reading page ${m[1]} of ${m[2]}`, exact: true };
  const r = s.match(/building page (\d+) of (\d+)/);
  if (r) return { pct: 62 + (Number(r[1]) / Number(r[2])) * 6, label: `Building the web page — page ${r[1]} of ${r[2]}`, exact: true };
  const u = s.match(/saving file (\d+) of (\d+)/);
  if (u) return { pct: 68 + (Number(u[1]) / Math.max(1, Number(u[2]))) * 2, label: `Saving the web page — file ${u[1]} of ${u[2]}`, exact: true };
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
  const queue = processing ? version.queue : null;
  // A queued step hasn't started, whatever stage name the version carries.
  const p = queue?.waiting ? { pct: 3, label: "Waiting to start", exact: false } : stageProgress(version.stage, version.status);
  const noWorker = !!queue?.waiting && (queue.stalled || (queue.workers_alive === 0 && queue.waiting_seconds > 30));
  const cons = s?.consensus;
  const consTotal = cons ? Object.values(cons).reduce((a, b) => a + b, 0) : 0;

  // Same rule as the stepper: the step is done once the version is out of processing,
  // failed and validation_issues.
  const open = version.open_blocking;
  const next = {
    label: "Next: Design →", onClick: onContinue,
    disabled: processing || version.status === "failed" || version.status === "validation_issues", waiting: processing,
    reason: processing ? `${p.label}… the next step unlocks automatically when the checks finish.`
      : version.status === "failed" ? (isAdmin ? "Processing failed — use Try again above." : "Processing failed — ask a workspace admin to try again.")
      : open > 0 ? `Resolve ${open} blocking figure${open === 1 ? "" : "s"} first.` : "Resolve the blocking items first.",
  };

  if (version.status === "failed") {
    return (
      <>
        <div className="card">
          <div className="callout bad" role="alert"><strong>We couldn't process this PDF.</strong> {version.error ?? "Something went wrong."}</div>
          {isAdmin && <Button className="primary" onClick={() => rerun.mutate()} busy={rerun.isPending} busyLabel="Restarting">Try again</Button>}
        </div>
        <WizardFooter next={next} />
      </>
    );
  }

  return (
    <div className="stack">
      {processing && (
        <div className="card">
          <div className="toolbar" style={{ justifyContent: "space-between", marginBottom: 10 }}>
            <h2 style={{ margin: 0, display: "flex", alignItems: "center", gap: 10 }}><Spinner label="Processing" /> {p.label}…</h2>
            <span className="muted small">{version.pages.length ? `${version.pages.length} pages` : version.page_count ? `${version.page_count} pages` : ""}</span>
          </div>
          <div className={`progress ${p.exact ? "" : "indeterminate"}`}><span style={{ width: `${p.pct}%` }} /></div>
          {noWorker ? (
            <div className="callout warn" role="status" style={{ marginTop: 12 }}>
              <strong>Processing hasn't started yet.</strong> Our processing service isn't running right now, so this report is waiting in line.
              It will start on its own as soon as the service is back. You don't need to upload it again.
            </div>
          ) : (
            <p className="muted small" style={{ marginTop: 10 }}>Large reports take a few minutes. You can leave this page — processing continues in the background.</p>
          )}
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
                      <span className="agent-dot" aria-hidden>{state === "ok" ? <IconCheck size={14} /> : state === "bad" ? <IconAlert size={16} /> : state === "warn" ? "•" : "–"}</span>
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
            <div className="callout ok">
              <strong>All checks passed.</strong> Every figure matches the PDF. Next, choose how the page should look — use <strong>Next: Design</strong> below.
            </div>
          )}
          {!processing && s.blocking > 0 && (
            <div className="callout bad"><strong>{s.blocking} item(s) need a decision before this can be published.</strong> Each one shows the exact spot in the PDF next to what we read — confirm it, correct it, or mark it as not a figure.</div>
          )}
        </>
      )}

      {s && !processing && isAdmin && version.published_at === null && <AiCheckCard tenantId={tenantId} reportId={report.id} versionId={version.id} />}
      {s && (s.blocking > 0 || s.warnings > 0) && <ValidationTab tenantId={tenantId} report={report} version={version} embedded />}
      {isAdmin && !processing && version.published_at === null && (
        <p className="muted small">
          Checks improved since this ran?{" "}
          <Button className="link" busy={rerun.isPending} busyLabel="Restarting the checks" onClick={() => rerun.mutate()}>Re-read the PDF and re-run all checks</Button>
        </p>
      )}
      <WizardFooter next={next} hint="Every figure is checked. Next, choose how the page should look." />
    </div>
  );
}

interface AiEstimate { items: number; requests: number; input_tokens: number; output_tokens: number; usd: number; credits: number; model: string; credit_usd: number }
interface AiAllowance { limit: number | null; used: number; remaining: number | null }
interface AiState { available: boolean; auto_cap_credits?: number; estimate: AiEstimate; allowance?: AiAllowance; last: { status: string; result: (AiEstimate & { confirmed: number; disagreed: number }) | null; error: string | null; at: string | null } | null }

/** Opt-in AI double-check of the flagged figures, with the cost shown before it runs. */
function AiCheckCard({ tenantId, reportId, versionId }: { tenantId: string; reportId: string; versionId: string }) {
  const qc = useQueryClient();
  const url = `/api/tenants/${tenantId}/reports/${reportId}/versions/${versionId}/ai-check`;
  const q = useQuery({
    queryKey: ["ai-check", versionId],
    queryFn: () => api<AiState>(url),
    refetchInterval: (d) => (d.state.data?.last && ["queued", "running"].includes(d.state.data.last.status) ? 2500 : false),
  });
  const [err, setErr] = useState<string | null>(null);
  const start = useMutation({
    mutationFn: (credits: number) => post(url, { credits_shown: credits }),
    onSuccess: () => { setErr(null); qc.invalidateQueries({ queryKey: ["ai-check", versionId] }); },
    onError: (e: Error) => { setErr(e.message); qc.invalidateQueries({ queryKey: ["ai-check", versionId] }); },
  });
  if (!q.data) return null;
  const { available, estimate: e, last, allowance: a } = q.data;
  const running = last && ["queued", "running"].includes(last.status);
  const overLimit = !!a && a.remaining !== null && e.credits > a.remaining;
  if (!e.items && !last) return null;
  const done = last?.status === "succeeded" && last.result;
  return (
    <div className="card">
      <h2 style={{ marginTop: 0, display: "flex", alignItems: "center", gap: 8 }}><span className="icon-chip"><IconSparkle /></span>AI double-check</h2>
      <p className="muted small">The AI reads a small crop of the PDF for each flagged figure. It can only confirm the value we extracted — it never types a number. A match turns the item into a warning; a mismatch stays for you to decide.</p>
      {e.items > 0 && (
        <div className="ai-estimate">
          <div className="stat"><span className="stat-n">{e.items}</span><span className="stat-l">flagged figures to check</span></div>
          <div className="stat"><span className="stat-n">{e.credits}</span><span className="stat-l">credits, estimated</span></div>
          <div className="stat"><span className="stat-n muted">${e.usd.toFixed(4)}</span><span className="stat-l">{(e.input_tokens + e.output_tokens).toLocaleString()} tokens · {e.model}</span></div>
        </div>
      )}
      {!available && <p className="callout info small">AI double-check isn't set up yet: add a Gemini key to the platform settings.</p>}
      {available && !!q.data.auto_cap_credits && (
        <p className="muted small">Runs automatically on figures read from images, up to {q.data.auto_cap_credits} credits per report. Anything beyond that is your call, with the estimate above.</p>
      )}
      {a && a.limit !== null && (
        <p className="muted small">This month: {a.used} of {a.limit} AI credits used · {a.remaining} left.</p>
      )}
      {available && e.items > 0 && !running && overLimit && (
        <p className="callout warn small">This check needs {e.credits} credits, but your workspace has {a?.remaining} of its monthly AI credits left. Ask your PublishPDF contact to raise the limit.</p>
      )}
      {available && e.items > 0 && !running && !overLimit && (
        <Button className="primary" busy={start.isPending} busyLabel="Starting the AI double-check"
          onClick={() => { if (window.confirm(`Run the AI double-check on ${e.items} figures for about ${e.credits} credits ($${e.usd.toFixed(4)})?`)) start.mutate(e.credits); }}>
          Run AI double-check · ≈ {e.credits} credits
        </Button>
      )}
      {running && <p className="callout info small row" style={{ gap: 8 }}><Spinner label="AI double-check running" /> AI double-check running — this updates automatically.</p>}
      {err && <p className="error" role="alert">{err}</p>}
      {done && last?.result && (
        <p className="small" style={{ marginBottom: 0 }}>Last run: <strong>{last.result.confirmed}</strong> confirmed · <strong>{last.result.disagreed}</strong> need you · used <strong>{last.result.credits}</strong> credits (${last.result.usd.toFixed(4)}, {(last.result.input_tokens + last.result.output_tokens).toLocaleString()} tokens).</p>
      )}
      {last?.status === "failed" && <p className="error small">The last AI run didn't finish: {last.error ?? "unknown error"}. Nothing in the report was changed; any requests that did run are counted under Settings → AI usage.</p>}
    </div>
  );
}
