import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, post, ROLE_LABEL, type Role } from "../api";
import { enterApp } from "../enter";
import { Button, Loading } from "../components/Spinner";

export default function InviteAccept() {
  const [params] = useSearchParams();
  const token = params.get("token") ?? "";
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const preview = useQuery({
    queryKey: ["invite", token],
    queryFn: () =>
      api<{ tenant_name: string; email: string; role: Role }>(
        `/api/auth/invites/preview?token=${encodeURIComponent(token)}`,
      ),
    enabled: !!token,
  });

  async function accept() {
    setBusy(true);
    setError(null);
    try {
      await post("/api/auth/invites/accept", { token });
      await enterApp(qc, navigate);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="auth-card">
      <h1>Join workspace</h1>
      {preview.isPending && token && <Loading label="Checking the invitation">Checking your invitation</Loading>}
      {(preview.isError || !token) && (
        <>
          <p className="error">{preview.error?.message ?? "This invitation link is incomplete."}</p>
          <Link to="/login">Go to sign in</Link>
        </>
      )}
      {preview.data && (
        <>
          <p>
            <strong>{preview.data.email}</strong> has been invited to <strong>{preview.data.tenant_name}</strong> as{" "}
            {ROLE_LABEL[preview.data.role]}.
          </p>
          {error && <p className="error">{error}</p>}
          <Button className="primary" busy={busy} busyLabel="Joining the workspace" onClick={accept}>Accept invitation</Button>
        </>
      )}
    </div>
  );
}
