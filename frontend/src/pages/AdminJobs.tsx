import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, post, type Job } from "../api";
import JobTable from "../components/JobTable";
import { SkeletonRows } from "../components/Spinner";

export default function AdminJobs() {
  const [status, setStatus] = useState("");
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["admin-jobs", status],
    queryFn: () => api<{ jobs: Job[] }>(`/api/admin/jobs${status ? `?status=${status}` : ""}`),
    refetchInterval: 5000,
  });
  const rerun = useMutation({
    mutationFn: (id: string) => post(`/api/admin/jobs/${id}/rerun`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-jobs"] }),
  });
  return (
    <>
      <h1>Job queue</h1>
      <label className="field inline">
        <span className="label">Status</span>
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All</option>
          <option value="failed">Failed</option>
          <option value="queued">Queued</option>
          <option value="running">Running</option>
          <option value="succeeded">Succeeded</option>
        </select>
      </label>
      {rerun.isError && <p className="error">{rerun.error.message}</p>}
      {q.isPending && <SkeletonRows label="Loading jobs" />}
      {q.isError && <p className="error">{q.error.message}</p>}
      {q.data && <JobTable jobs={q.data.jobs} admin onRerun={(id) => rerun.mutate(id)} rerunning={rerun.isPending ? rerun.variables : null} />}
    </>
  );
}
