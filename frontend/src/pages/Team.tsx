import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useParams } from "react-router-dom";
import { api, formatDateTime, post, ROLE_LABEL, type Role } from "../api";
import { useMe } from "../App";

interface Members {
  members: { user_id: string; email: string; name: string | null; role: Role; mfa_enrolled: boolean }[];
  pending_invites: { id: string; email: string; role: Role; expires_at: string }[];
}

export default function Team() {
  const { tenantId } = useParams();
  const me = useMe().data!;
  const canManage =
    me.user.is_platform_admin || me.tenants.find((t) => t.id === tenantId)?.role === "client_admin";
  const qc = useQueryClient();
  const key = ["members", tenantId];
  const members = useQuery({ queryKey: key, queryFn: () => api<Members>(`/api/tenants/${tenantId}/members`) });
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("client_reviewer");
  const [error, setError] = useState<string | null>(null);

  const refresh = () => qc.invalidateQueries({ queryKey: key });
  const onError = (e: Error) => setError(e.message);
  const invite = useMutation({
    mutationFn: () => post(`/api/tenants/${tenantId}/invites`, { email, role }),
    onSuccess: () => {
      setEmail("");
      setError(null);
      refresh();
    },
    onError,
  });
  const changeRole = useMutation({
    mutationFn: (v: { userId: string; role: Role }) =>
      api(`/api/tenants/${tenantId}/members/${v.userId}`, { method: "PATCH", body: JSON.stringify({ role: v.role }) }),
    onSuccess: refresh,
    onError,
  });
  const remove = useMutation({
    mutationFn: (userId: string) => api(`/api/tenants/${tenantId}/members/${userId}`, { method: "DELETE" }),
    onSuccess: refresh,
    onError,
  });
  const revoke = useMutation({
    mutationFn: (id: string) => api(`/api/tenants/${tenantId}/invites/${id}`, { method: "DELETE" }),
    onSuccess: refresh,
    onError,
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    invite.mutate();
  }

  return (
    <>
      <h1>Team</h1>
      <p className="muted">
        Admins can upload, change settings, and approve and publish. Reviewers can view reports, leave comments and
        flag issues, but can't publish.
      </p>
      {error && <p className="error" role="alert">{error}</p>}
      {canManage && (
        <form className="inline-form" onSubmit={submit}>
          <input type="email" required placeholder="colleague@company.com" value={email} onChange={(e) => setEmail(e.target.value)} aria-label="Email to invite" />
          <select value={role} onChange={(e) => setRole(e.target.value as Role)} aria-label="Role">
            <option value="client_reviewer">Reviewer</option>
            <option value="client_admin">Admin</option>
          </select>
          <button className="primary" disabled={invite.isPending}>Send invitation</button>
        </form>
      )}
      {members.isPending && <p className="muted">Loading…</p>}
      {members.data && (
        <table>
          <thead>
            <tr>
              <th scope="col">Email</th>
              <th scope="col">Role</th>
              <th scope="col">Two-factor</th>
              {canManage && <th scope="col"><span className="sr-only">Actions</span></th>}
            </tr>
          </thead>
          <tbody>
            {members.data.members.map((m) => (
              <tr key={m.user_id}>
                <td>{m.email}</td>
                <td>
                  {canManage ? (
                    <select
                      value={m.role}
                      aria-label={`Role for ${m.email}`}
                      onChange={(e) => changeRole.mutate({ userId: m.user_id, role: e.target.value as Role })}
                    >
                      <option value="client_reviewer">Reviewer</option>
                      <option value="client_admin">Admin</option>
                    </select>
                  ) : (
                    ROLE_LABEL[m.role]
                  )}
                </td>
                <td>{m.mfa_enrolled ? "On" : m.role === "client_admin" ? "Pending setup" : "Not required"}</td>
                {canManage && (
                  <td className="right">
                    <button
                      className="link danger"
                      onClick={() => confirm(`Remove ${m.email} from this workspace?`) && remove.mutate(m.user_id)}
                    >
                      Remove
                    </button>
                  </td>
                )}
              </tr>
            ))}
            {members.data.pending_invites.map((i) => (
              <tr key={i.id} className="muted">
                <td>{i.email}</td>
                <td>{ROLE_LABEL[i.role]}</td>
                <td>Invited · expires {formatDateTime(i.expires_at)}</td>
                {canManage && (
                  <td className="right">
                    <button className="link danger" onClick={() => revoke.mutate(i.id)}>Revoke</button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
