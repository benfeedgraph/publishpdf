import { useQueryClient } from "@tanstack/react-query";
import { useState, type DragEvent, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { PERIOD_OPTIONS, post, REPORT_TYPE_LABEL, upload, type ReportType } from "../api";
import { IconCheck, IconClose, IconUpload } from "./Icons";

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

  async function handle(file: File | undefined) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf") && file.type !== "application/pdf") {
      setError("That isn't a PDF. Upload the report as a PDF file.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      setInspected(await upload<Inspected>(`/api/tenants/${tenantId}/reports/inspect`, form));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function onDrop(e: DragEvent) {
    e.preventDefault();
    setOver(false);
    handle(e.dataTransfer.files?.[0]);
  }

  return (
    <>
      <label className={`dropzone ${over ? "over" : ""}`} style={compact ? { padding: "22px 18px" } : undefined}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)} onDrop={onDrop}>
        <input type="file" accept="application/pdf,.pdf" disabled={busy} onChange={(e) => handle(e.target.files?.[0])} aria-label="Upload a report PDF" />
        <div className="dz-icon"><IconUpload /></div>
        <p className="dz-title">{busy ? "Reading your PDF…" : "Drop a report PDF here, or click to choose"}</p>
        <p className="muted small" style={{ margin: 0 }}>Quarterly results, investor presentations, annual reports — 100+ pages is fine.</p>
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
        {error && <p className="error" role="alert">{error}</p>}
        <div className="btn-row" style={{ justifyContent: "flex-end" }}>
          <button type="button" className="secondary" onClick={onClose}>Cancel</button>
          <button className="primary" disabled={busy}>{busy ? "Starting…" : "Start processing"}</button>
        </div>
      </form>
    </div>
  );
}
