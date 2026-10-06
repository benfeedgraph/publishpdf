import { useQueryClient } from "@tanstack/react-query";
import { useState, type DragEvent, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { PERIOD_OPTIONS, post, REPORT_TYPE_LABEL, upload, type ReportType } from "../api";
import { IconCheck, IconClose, IconUpload } from "./Icons";
import { Button, Spinner } from "./Spinner";
import { AiCostNote } from "./AiCostNote";

interface Inspected {
  source_file_id: string;
  filename: string;
  page_count: number;
  size_bytes: number;
  detected: Partial<{ company_name: string; report_type: ReportType; fiscal_year: number; period: string; currency: string; reporting_unit: string; confidence: Record<string, string> }>;
}

export function Dropzone({ tenantId, compact = false }: { tenantId: string; compact?: boolean }) {
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [inspected, setInspected] = useState<Inspected | null>(null);
  const [progress, setProgress] = useState<number | null>(null);

  async function handle(file: File | undefined) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf") && file.type !== "application/pdf") {
      setError("That isn't a PDF. Upload the report as a PDF file.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const base = `/api/tenants/${tenantId}/reports`;
      const form = new FormData();
      // The PDF goes from the browser straight to storage (Vercel Blob, or a presigned URL
      // for S3 / Cloudflare R2), so an annual report isn't stopped by the ~4.5 MB limit on
      // requests to the API.
      const dest = await post<{ mode: "direct" | "presigned" | "form"; key?: string; pathname?: string; token?: string;
        url?: string; headers?: Record<string, string> }>(`${base}/upload-url`, { filename: file.name, size_bytes: file.size });
      if (dest.mode === "presigned" && dest.url && dest.key) {
        await putWithProgress(dest.url, file, dest.headers ?? {}, setProgress);
        form.append("incoming", dest.key);
        form.append("filename", file.name);
      } else if (dest.mode === "direct" && dest.pathname && dest.token && dest.key) {
        const { put } = await import("@vercel/blob/client");
        await put(dest.pathname, file, {
          access: "private", token: dest.token, contentType: "application/pdf", multipart: file.size > 8 * 1024 * 1024,
          onUploadProgress: ({ percentage }) => setProgress(Math.round(percentage)),
        });
        form.append("incoming", dest.key);
        form.append("filename", file.name);
      } else {
        form.append("file", file);
      }
      setProgress(null);
      setInspected(await upload<Inspected>(`${base}/inspect`, form));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      setProgress(null);
    }
  }
  function onDrop(e: DragEvent) {
    e.preventDefault();
    setOver(false);
    handle(e.dataTransfer.files?.[0]);
  }

  return (
    <>
      <label className={`dropzone ${over ? "over" : ""} ${busy ? "busy" : ""}`} style={compact ? { padding: "22px 18px" } : undefined} aria-busy={busy || undefined}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)} onDrop={onDrop}>
        <input type="file" accept="application/pdf,.pdf" disabled={busy} onChange={(e) => handle(e.target.files?.[0])} aria-label="Upload a report PDF" />
        <div className="dz-icon">{busy ? <Spinner size={22} label={progress !== null ? "Uploading" : "Reading your PDF"} /> : <IconUpload />}</div>
        <p className="dz-title">{busy ? (progress !== null ? `Uploading your PDF — ${progress}%` : "Reading your PDF") : "Drop a report PDF here, or click to choose"}</p>
        {busy ? (
          <div className={`progress dz-progress ${progress === null ? "indeterminate" : ""}`} role="progressbar" aria-label="Upload progress"
            aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress ?? undefined}><span style={{ width: `${progress ?? 35}%` }} /></div>
        ) : (
          <p className="muted small" style={{ margin: 0 }}>Quarterly results, investor presentations, annual reports — 100+ pages is fine.</p>
        )}
        {error && <p className="error" style={{ marginTop: 10 }}>{error}</p>}
      </label>
      {inspected && <ConfirmModal tenantId={tenantId} data={inspected} onClose={() => setInspected(null)} />}
    </>
  );
}

function ConfirmModal({ tenantId, data, onClose }: { tenantId: string; data: Inspected; onClose: () => void }) {
  const d = data.detected;
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [company, setCompany] = useState(d.company_name ?? "");
  const [type, setType] = useState<ReportType>(d.report_type ?? "quarterly_results");
  const [fy, setFy] = useState(String(d.fiscal_year ?? new Date().getFullYear()));
  const [period, setPeriod] = useState(d.period ?? "q1");
  const [currency, setCurrency] = useState(d.currency ?? "INR");
  const [unit, setUnit] = useState(d.reporting_unit ?? "₹ crore");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const found = (k: string) => (d as Record<string, unknown>)[k] !== undefined;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await post<{ report_id: string; version: { id: string } }>(`/api/tenants/${tenantId}/reports/create`, {
        source_file_id: data.source_file_id, company_name: company, report_type: type, fiscal_year: Number(fy),
        period: type === "annual_report" ? "fy" : period, currency, reporting_unit: unit,
      });
      await qc.invalidateQueries({ queryKey: ["reports", tenantId] });
      navigate(`/t/${tenantId}/reports/${r.report_id}?v=${r.version.id}`);
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }
  const Tag = ({ k }: { k: string }) => (found(k) ? <span className="detected"><IconCheck size={12} /> detected</span> : null);

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="confirm-h" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <form className="modal" onSubmit={submit}>
        <div className="modal-head">
          <div>
            <h2 id="confirm-h">Confirm report details</h2>
            <p className="muted small" style={{ margin: 0 }}>{data.filename} · {data.page_count} pages · {(data.size_bytes / 1024 / 1024).toFixed(1)} MB. We filled these in from the PDF — check them before we start.</p>
          </div>
          <button type="button" className="link" onClick={onClose} aria-label="Close"><IconClose /></button>
        </div>
        <div className="form-grid">
          <label className="field span2"><span className="label">Company <Tag k="company_name" /></span><input required value={company} onChange={(e) => setCompany(e.target.value)} /></label>
          <label className="field"><span className="label">Report type <Tag k="report_type" /></span>
            <select value={type} onChange={(e) => setType(e.target.value as ReportType)}>{Object.entries(REPORT_TYPE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
          <label className="field"><span className="label">Fiscal year (year it ends) <Tag k="fiscal_year" /></span><input required inputMode="numeric" pattern="[0-9]{4}" value={fy} onChange={(e) => setFy(e.target.value)} /></label>
          <label className="field"><span className="label">Period <Tag k="period" /></span>
            <select value={type === "annual_report" ? "fy" : period} disabled={type === "annual_report"} onChange={(e) => setPeriod(e.target.value)}>{PERIOD_OPTIONS.map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
          <label className="field"><span className="label">Currency <Tag k="currency" /></span><input required maxLength={3} value={currency} onChange={(e) => setCurrency(e.target.value.toUpperCase())} /></label>
          <label className="field span2"><span className="label">Reporting unit <Tag k="reporting_unit" /></span><input required value={unit} onChange={(e) => setUnit(e.target.value)} />
            <span className="hint">Used only where a table doesn't state its unit — those tables are flagged for you to confirm.</span></label>
        </div>
        <AiCostNote tenantId={tenantId} compact />
        {error && <p className="error" role="alert">{error}</p>}
        <div className="btn-row" style={{ justifyContent: "flex-end" }}>
          <button type="button" className="secondary" onClick={onClose}>Cancel</button>
          <Button className="primary" busy={busy} busyLabel="Starting processing">Start processing</Button>
        </div>
      </form>
    </div>
  );
}


/** PUT a file to a presigned storage URL, reporting progress (fetch can't report upload progress). */
function putWithProgress(url: string, file: File, headers: Record<string, string>, onProgress: (p: number) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    for (const [k, v] of Object.entries(headers)) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100)); };
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve()
      : reject(new Error(`The upload to storage failed (${xhr.status}). Please try again.`)));
    // A CORS rule missing on the bucket shows up as a network error with no status.
    xhr.onerror = () => reject(new Error("The upload couldn't reach storage. If this keeps happening, the storage bucket's CORS settings need this site's address."));
    xhr.send(file);
  });
}
