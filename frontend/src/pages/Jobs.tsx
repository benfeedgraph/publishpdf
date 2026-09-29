import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, type Job } from "../api";
import JobTable from "../components/JobTable";

export default function Jobs() {
  const { tenantId } = useParams();
  const q = useQuery({
    queryKey: ["jobs", tenantId],
    queryFn: () => api<{ jobs: Job[] }>(`/api/tenants/${tenantId}/jobs`),
    refetchInterval: 5000,
  });
  return (
    <>
      <h1>Processing</h1>
      <p className="muted">Background processing steps for this workspace. Updates every few seconds.</p>
      {q.isPending && <p className="muted">Loading…</p>}
      {q.isError && <p className="error">{q.error.message}</p>}
      {q.data && <JobTable jobs={q.data.jobs} />}
    </>
  );
}
