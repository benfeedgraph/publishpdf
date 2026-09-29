import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { post } from "../api";
import { useMe } from "../App";

export default function Mfa() {
  const me = useMe();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [enrol, setEnrol] = useState<{ otpauth_uri: string; qr_svg: string } | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (me.isPending) return <p className="center muted">Loading…</p>;
  if (!me.data) return <Navigate to="/login" replace />;
  if (me.data.fully_authenticated) return <Navigate to="/" replace />;
  const enrolled = me.data.mfa.enrolled;

  async function start() {
    setError(null);
    try {
      setEnrol(await post("/api/auth/mfa/enroll"));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post(enrolled ? "/api/auth/mfa/verify" : "/api/auth/mfa/confirm", { code });
      await qc.invalidateQueries({ queryKey: ["me"] });
      navigate("/", { replace: true });
    } catch (err) {
      const msg = (err as Error).message;
      setError(msg);
      setCode("");
      if (msg.startsWith("Too many")) {
        qc.clear();
        navigate("/login", { replace: true });
      }
    } finally {
      setBusy(false);
    }
  }

  const secret = enrol ? new URL(enrol.otpauth_uri).searchParams.get("secret") : null;

  return (
    <div className="auth-card">
      <h1>Two-factor authentication</h1>
      {!enrolled && !enrol && (
        <>
          <p>
            Your role can approve and publish financial reports, so your account needs a second sign-in step. You'll need
            an authenticator app (Google Authenticator, Microsoft Authenticator, 1Password, etc.).
          </p>
          <button className="primary" onClick={start}>Set up two-factor</button>
        </>
      )}
      {!enrolled && enrol && (
        <>
          <p>Scan this code with your authenticator app, then enter the 6-digit code it shows.</p>
          <div className="qr" dangerouslySetInnerHTML={{ __html: enrol.qr_svg }} />
          <p className="muted small">
            Can't scan? Enter this key manually: <code>{secret}</code>
          </p>
        </>
      )}
      {(enrolled || enrol) && (
        <form onSubmit={submit}>
          <label className="field">
            <span className="label">6-digit code</span>
            <input
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="[0-9 ]{6,7}"
              required
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoFocus
            />
          </label>
          {error && <p className="error">{error}</p>}
          <button className="primary" disabled={busy}>{busy ? "Checking…" : "Continue"}</button>
        </form>
      )}
      {error && !enrolled && !enrol && <p className="error">{error}</p>}
    </div>
  );
}
