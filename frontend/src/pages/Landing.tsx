import { Link, Navigate } from "react-router-dom";
import { useMe } from "../App";
import { workspacePath } from "../enter";

const POINTS = [
  {
    title: "Zero-tolerance figures",
    body: "Every number is traced to its place in the PDF and re-checked by independent validation agents.",
  },
  {
    title: "Your brand",
    body: "Match your website, a palette, or a brand PDF, then fine-tune before anything goes live.",
  },
  {
    title: "Readable by people and AI",
    body: "Clean pages, tables, Markdown and data downloads. The PDF stays the official document.",
  },
];

export default function Landing() {
  const me = useMe();
  if (me.isPending) return <p className="center muted">Loading…</p>;
  if (me.data?.fully_authenticated) {
    const path = workspacePath(me.data);
    if (path !== "/") return <Navigate to={path} replace />;
  }

  return (
    <div className="landing">
      <header className="landing-bar">
        <img src="/brand/publishpdf-logo-full-color.svg" alt="PublishPDF" width="148" height="32" />
        <nav className="landing-nav">
          <Link className="button primary" to="/login">Sign in</Link>
        </nav>
      </header>
      <main className="landing-main">
        <p className="eyebrow">PublishPDF</p>
        <h1>Turn dense report PDFs into web pages people actually read.</h1>
        <p className="landing-lead">
          Upload a results PDF, annual report or investor deck. We extract every figure, check each one against the PDF, and build a clear, on-brand page for you to approve.
        </p>
        <div className="landing-actions">
          <Link className="button primary lg" to="/login">Sign in</Link>
          <Link className="button secondary lg" to="/login">Use the demo account</Link>
        </div>
        <ul className="landing-points">
          {POINTS.map((point) => (
            <li key={point.title}>
              <h2>{point.title}</h2>
              <p>{point.body}</p>
            </li>
          ))}
        </ul>
      </main>
      <footer className="landing-foot">The PDF stays the official document. Your page links back to it.</footer>
    </div>
  );
}
