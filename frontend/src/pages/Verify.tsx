import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { post } from "../api";

/** Magic-link landing. The token is exchanged by an explicit POST (not on GET by the
 * server), so email link scanners that prefetch the URL can't burn the token. */
export default function Verify() {
  const [params] = useSearchParams();
  const token = params.get("token");
  const [error, setError] = useState<string | null>(token ? null : "This link is missing its token.");
  const navigate = useNavigate();
  const qc = useQueryClient();
  const started = useRef(false);

  useEffect(() => {
    if (!token || started.current) return;
    started.current = true;
    post("/api/auth/verify", { token })
      .then(async () => {
        await qc.invalidateQueries({ queryKey: ["me"] });
        navigate("/", { replace: true });
      })
      .catch((e: Error) => setError(e.message));
  }, [token, navigate, qc]);

  return (
    <div className="auth-card">
      <h1>Signing you in…</h1>
      {error && (
        <>
          <p className="error">{error}</p>
          <Link to="/login">Request a new link</Link>
        </>
      )}
    </div>
  );
}
