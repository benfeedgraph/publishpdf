import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ClipboardEvent, type FormEvent, type KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import { post } from "../api";

type Step = "email" | "code" | "profile";

export default function Login() {
  const [step, setStep] = useState<Step>("email");
  const [email, setEmail] = useState("");
  const [digits, setDigits] = useState<string[]>(Array(6).fill(""));
  const [devCode, setDevCode] = useState<string | null>(null);
  const [signupToken, setSignupToken] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [company, setCompany] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [resendIn, setResendIn] = useState(0);
  const boxes = useRef<(HTMLInputElement | null)[]>([]);
  const qc = useQueryClient();
  const navigate = useNavigate();

  useEffect(() => {
    if (resendIn <= 0) return;
    const t = setTimeout(() => setResendIn((n) => n - 1), 1000);
    return () => clearTimeout(t);
  }, [resendIn]);

  async function sendCode(e?: FormEvent) {
    e?.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await post<{ dev_code?: string }>("/api/auth/code", { email });
      setDevCode(r.dev_code ?? null);
      setDigits(Array(6).fill(""));
      setStep("code");
      setResendIn(30);
      setTimeout(() => boxes.current[0]?.focus(), 50);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function verify(code: string) {
    setBusy(true);
    setError(null);
    try {
      const r = await post<{ status: string; signup_token?: string }>("/api/auth/code/verify", { email, code });
      if (r.status === "needs_profile") {
        setSignupToken(r.signup_token ?? null);
        setStep("profile");
      } else {
        await done();
      }
    } catch (err) {
      setError((err as Error).message);
      setDigits(Array(6).fill(""));
      boxes.current[0]?.focus();
    } finally {
      setBusy(false);
    }
  }

  async function signup(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await post("/api/auth/signup", { signup_token: signupToken, name, company });
      await done();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function done() {
    await qc.invalidateQueries({ queryKey: ["me"] });
    navigate("/", { replace: true });
  }

  function setDigit(i: number, v: string) {
    const d = v.replace(/\D/g, "");
    const next = [...digits];
    next[i] = d.slice(-1);
    setDigits(next);
    if (d && i < 5) boxes.current[i + 1]?.focus();
    if (next.every(Boolean)) verify(next.join(""));
  }
  function onKey(i: number, e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Backspace" && !digits[i] && i > 0) boxes.current[i - 1]?.focus();
  }
  function onPaste(e: ClipboardEvent<HTMLInputElement>) {
    const d = e.clipboardData.getData("text").replace(/\D/g, "").slice(0, 6);
    if (d.length === 6) {
      e.preventDefault();
      setDigits(d.split(""));
      verify(d);
    }
  }

  return (
    <div className="auth">
      <aside className="auth-art">
        <div className="brand"><img src="/brand/publishpdf-logo-full-on-dark.svg" alt="PublishPDF" width="157" height="34" /></div>
        <div>
          <h1>Turn dense report PDFs into web pages people actually read.</h1>
          <p>Upload a results PDF, annual report or investor deck. We extract every figure, check each one against the PDF with independent validation agents, and build a clear, on-brand page for you to approve.</p>
          <ul>
            <li><span className="tick">✓</span><span><b>Zero-tolerance figures.</b> Every number traced to its place in the PDF and re-checked independently.</span></li>
            <li><span className="tick">✓</span><span><b>Your brand.</b> Match your website, a palette, or a brand PDF.</span></li>
            <li><span className="tick">✓</span><span><b>Readable by people and AI.</b> Clean pages, tables, Markdown and data downloads.</span></li>
          </ul>
        </div>
        <p className="small foot">The PDF stays the official document; your page links back to it.</p>
      </aside>
      <main className="auth-main">
        <div className="auth-card">
          {step === "email" && (
            <form onSubmit={sendCode}>
              <h1>Sign in or create an account</h1>
              <p className="muted">Enter your work email and we'll send you a 6-digit code. No password needed.</p>
              <label className="field">
                <span className="label">Work email</span>
                <input type="email" required autoFocus autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" />
              </label>
              {error && <p className="error" role="alert">{error}</p>}
              <button className="primary lg" disabled={busy}>{busy ? "Sending…" : "Continue"}</button>
            </form>
          )}

          {step === "code" && (
            <form onSubmit={(e) => { e.preventDefault(); verify(digits.join("")); }}>
              <h1>Check your email</h1>
              <p className="muted">We sent a 6-digit code to <strong style={{ color: "var(--text)" }}>{email}</strong>. It expires in 10 minutes.</p>
              <div className="code-inputs" role="group" aria-label="6-digit code">
                {digits.map((d, i) => (
                  <input key={i} ref={(el) => (boxes.current[i] = el)} inputMode="numeric" autoComplete={i === 0 ? "one-time-code" : "off"}
                    maxLength={1} value={d} aria-label={`Digit ${i + 1}`} onChange={(e) => setDigit(i, e.target.value)}
                    onKeyDown={(e) => onKey(i, e)} onPaste={onPaste} disabled={busy} />
                ))}
              </div>
              {devCode && <p className="callout info small">Development mode (no email is sent). Your code is <strong className="mono">{devCode}</strong>{" "}
                <button type="button" className="link" onClick={() => { setDigits(devCode.split("")); verify(devCode); }}>Use it</button></p>}
              {error && <p className="error" role="alert">{error}</p>}
              <button className="primary lg" disabled={busy || digits.some((x) => !x)}>{busy ? "Checking…" : "Verify"}</button>
              <div className="row" style={{ marginTop: 14, justifyContent: "space-between" }}>
                <button type="button" className="link" onClick={() => { setStep("email"); setError(null); }}>Use a different email</button>
                <button type="button" className="link" disabled={resendIn > 0 || busy} onClick={() => sendCode()}>
                  {resendIn > 0 ? `Resend in ${resendIn}s` : "Resend code"}
                </button>
              </div>
            </form>
          )}

          {step === "profile" && (
            <form onSubmit={signup}>
              <h1>Create your workspace</h1>
              <p className="muted">You're new here. Tell us who you are, and you'll be the admin of your company's workspace.</p>
              <label className="field">
                <span className="label">Your name</span>
                <input required autoFocus value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
              </label>
              <label className="field">
                <span className="label">Company name</span>
                <input required value={company} onChange={(e) => setCompany(e.target.value)} autoComplete="organization" placeholder="e.g. Acme Industries Limited" />
              </label>
              {error && <p className="error" role="alert">{error}</p>}
              <button className="primary lg" disabled={busy}>{busy ? "Creating…" : "Create workspace"}</button>
            </form>
          )}
        </div>
      </main>
    </div>
  );
}
