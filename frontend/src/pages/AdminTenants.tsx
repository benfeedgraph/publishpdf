import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, formatDateTime, post } from "../api";

interface TenantRow {
  id: string;
  slug: string;
  name: string;
  status: "active" | "suspended";
  created_at: string;
  member_count: number;
}

const slugify = (s: string) =>
  s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 50);

export default function AdminTenants() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["admin-tenants"], queryFn: () => api<{ tenants: TenantRow[] }>("/api/admin/tenants") });
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [adminEmail, setAdminEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["admin-tenants"] });

  const create = useMutation({
    mutationFn: () =>
      post("/api/admin/tenants", { name, slug, first_admin_email: adminEmail || undefined }),
    onSuccess: () => {
      setName("");
      setSlug("");
      setSlugTouched(false);
      setAdminEmail("");
      setError(null);
      refresh();
    },
    onError: (e: Error) => setError(e.message),
  });
  const setStatus = useMutation({
    mutationFn: (v: { id: string; status: string }) =>
      api(`/api/admin/tenants/${v.id}`, { method: "PATCH", body: JSON.stringify({ status: v.status }) }),
    onSuccess: refresh,
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    create.mutate();
  }

  return (
    <>
      <h1>Tenants</h1>
      <form className="card" onSubmit={submit}>
        <h2>New tenant</h2>
        <div className="grid3">
          <label className="field">
            <span className="label">Company name</span>
            <input
              required
              value={name}
              onChange={(e) => {
                setName(e.target.value);
                if (!slugTouched) setSlug(slugify(e.target.value));
              }}
            />
          </label>
          <label className="field">
            <span className="label">Slug</span>
            <input
              required
              pattern="[a-z0-9](?:[a-z0-9\-]{0,48}[a-z0-9])?"
              value={slug}
              onChange={(e) => {
                setSlugTouched(true);
                setSlug(e.target.value);
              }}
            />
          </label>
          <label className="field">
            <span className="label">First admin email (optional)</span>
            <input type="email" value={adminEmail} onChange={(e) => setAdminEmail(e.target.value)} />
          </label>
        </div>
        {error && <p className="error">{error}</p>}
        <button className="primary" disabled={create.isPending}>Create tenant</button>
      </form>

      {q.isPending && <p className="muted">Loading…</p>}
      {q.data && (
        <table>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Slug</th>
              <th scope="col">Members</th>
              <th scope="col">Status</th>
              <th scope="col">Created</th>
              <th scope="col"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {q.data.tenants.map((t) => (
              <tr key={t.id}>
                <td><Link to={`/admin/tenants/${t.id}`}>{t.name}</Link></td>
                <td className="mono">{t.slug}</td>
                <td>{t.member_count}</td>
                <td><span className={`status status-${t.status}`}>{t.status}</span></td>
                <td className="nowrap">{formatDateTime(t.created_at)}</td>
                <td className="right">
                  <button
                    className={`link ${t.status === "active" ? "danger" : ""}`}
                    onClick={() => {
                      const next = t.status === "active" ? "suspended" : "active";
                      if (next === "active" || confirm(`Suspend ${t.name}? Its users lose access immediately.`))
                        setStatus.mutate({ id: t.id, status: next });
                    }}
                  >
                    {t.status === "active" ? "Suspend" : "Reactivate"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
