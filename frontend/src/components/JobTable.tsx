import { formatDateTime, type Job } from "../api";
import { Button } from "./Spinner";

export default function JobTable({
  jobs,
  admin,
  onRerun,
  rerunning,
}: {
  jobs: Job[];
  admin?: boolean;
  onRerun?: (id: string) => void;
  /** The job whose re-run is being requested: its button shows busy. */
  rerunning?: string | null;
}) {
  if (jobs.length === 0) return <p className="muted">No jobs yet.</p>;
  return (
    <table>
      <thead>
        <tr>
          <th scope="col">Step</th>
          {admin && <th scope="col">Tenant</th>}
          <th scope="col">Status</th>
          <th scope="col">Attempts</th>
          <th scope="col">Started</th>
          <th scope="col">Finished</th>
          <th scope="col">Message</th>
          {admin && <th scope="col"><span className="sr-only">Actions</span></th>}
        </tr>
      </thead>
      <tbody>
        {jobs.map((j) => (
          <tr key={j.id}>
            <td><code>{j.kind}</code></td>
            {admin && <td className="mono small">{j.tenant_id?.slice(0, 8)}</td>}
            <td><span className={`status status-${j.status}`}>{j.status}</span></td>
            <td>{j.attempts}/{j.max_attempts}</td>
            <td className="nowrap">{formatDateTime(j.started_at)}</td>
            <td className="nowrap">{formatDateTime(j.finished_at)}</td>
            <td>
              {j.error}
              {admin && j.last_error && (
                <details>
                  <summary className="small">Technical detail</summary>
                  <pre className="small">{j.last_error}</pre>
                </details>
              )}
            </td>
            {admin && (
              <td className="right">
                {(j.status === "failed" || j.status === "cancelled" || j.status === "succeeded") && (
                  <Button className="link" busy={rerunning === j.id} disabled={!!rerunning} busyLabel="Re-running" onClick={() => onRerun?.(j.id)}>Re-run</Button>
                )}
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
