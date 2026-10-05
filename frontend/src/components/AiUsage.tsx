import { Link } from "react-router-dom";

export interface AiUsageSummary {
  month_start: string;
  credit_usd: number;
  month: { credits: number; usd: number; requests: number; input_tokens: number; output_tokens: number };
  by_feature: { feature: string; label: string; requests: number; input_tokens: number; output_tokens: number; usd: number; credits: number }[];
  limit: number | null;
  remaining: number | null;
  recent: { at: string; feature: string; label: string; model: string; input_tokens: number; output_tokens: number; usd: number; credits: number; ok: boolean; report: string | null; report_id: string | null }[];
  available?: { ai_check: boolean; section_labels: boolean };
}

const fmtUsd = (n: number) => `$${n.toFixed(n < 1 ? 4 : 2)}`;

/** This month's AI spend for one workspace: totals against the limit, by feature, latest requests. */
export default function AiUsagePanel({ data, tenantId }: { data: AiUsageSummary; tenantId?: string }) {
  const month = new Date(data.month_start).toLocaleDateString(undefined, { month: "long", year: "numeric", timeZone: "UTC" });
  const pct = data.limit ? Math.min(100, (data.month.credits / data.limit) * 100) : 0;
  return (
    <div className="stack">
      <div className="stat-row">
        <div className="stat"><span className="stat-n">{data.month.credits.toLocaleString()}</span><span className="stat-l">credits used in {month}</span></div>
        <div className="stat"><span className="stat-n muted">{fmtUsd(data.month.usd)}</span><span className="stat-l">model cost at our rates</span></div>
        <div className="stat"><span className="stat-n muted">{(data.month.input_tokens + data.month.output_tokens).toLocaleString()}</span><span className="stat-l">tokens · {data.month.requests.toLocaleString()} requests</span></div>
        <div className="stat">
          <span className="stat-n">{data.limit === null ? "No limit" : data.remaining!.toLocaleString()}</span>
          <span className="stat-l">{data.limit === null ? "monthly credit limit" : `credits left of ${data.limit.toLocaleString()}`}</span>
        </div>
      </div>
      {data.limit !== null && (
        <div className="progress" role="img" aria-label={`${data.month.credits} of ${data.limit} credits used`}><span style={{ width: `${pct}%` }} /></div>
      )}
      <p className="muted small" style={{ margin: 0 }}>
        One credit = ${data.credit_usd.toFixed(2)}. Each request is recorded as it returns, with the provider's own token count, so a run that fails partway still shows what it used. Months run on UTC.
      </p>
      {data.by_feature.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Feature</th><th className="num">Requests</th><th className="num">Tokens</th><th className="num">Cost</th><th className="num">Credits</th></tr></thead>
            <tbody>
              {data.by_feature.map((f) => (
                <tr key={f.feature}><td>{f.label}</td><td className="num">{f.requests.toLocaleString()}</td>
                  <td className="num">{(f.input_tokens + f.output_tokens).toLocaleString()}</td><td className="num">{fmtUsd(f.usd)}</td><td className="num">{f.credits.toLocaleString()}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {data.recent.length > 0 ? (
        <details>
          <summary className="small">Latest {data.recent.length} requests</summary>
          <div className="table-wrap">
            <table>
              <thead><tr><th>When</th><th>Feature</th><th>Report</th><th>Model</th><th className="num">Tokens in / out</th><th className="num">Cost</th></tr></thead>
              <tbody>
                {data.recent.map((r, i) => (
                  <tr key={i}>
                    <td className="small">{new Date(r.at).toLocaleString()}</td>
                    <td>{r.label}{!r.ok && <span className="muted small"> (failed)</span>}</td>
                    <td className="small">{r.report && r.report_id && tenantId ? <Link to={`/t/${tenantId}/reports/${r.report_id}`}>{r.report}</Link> : r.report ?? "—"}</td>
                    <td className="small muted">{r.model}</td>
                    <td className="num small">{r.input_tokens.toLocaleString()} / {r.output_tokens.toLocaleString()}</td>
                    <td className="num small">{fmtUsd(r.usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ) : (
        <p className="muted small" style={{ margin: 0 }}>No AI requests yet.</p>
      )}
    </div>
  );
}
