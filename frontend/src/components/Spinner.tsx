/* Loading indicators: one spinner, a busy state for buttons, and skeletons for lists
 * and cards — so no action or page load is ever shown as plain "Loading…" text.
 * Pure CSS (see "loading indicators" in styles.css); safe to server-render. */
import type { ButtonHTMLAttributes, ReactNode } from "react";

/** A small ring that spins in the surrounding text colour. Pass `decorative` when the
 * thing it sits in already says what is happening (e.g. a busy button). */
export function Spinner({ size, label = "Loading", decorative = false, className = "" }: {
  size?: number; label?: string; decorative?: boolean; className?: string;
}) {
  const style = size ? { width: size, height: size } : undefined;
  return decorative
    ? <span className={`spinner ${className}`} style={style} aria-hidden />
    : <span className={`spinner ${className}`} style={style} role="status" aria-label={label} />;
}

/** A button that shows a spinner in place of its label while `busy`. The label stays in
 * the layout (hidden), so the button keeps its width, and the button is disabled. */
export function Button({ busy = false, busyLabel, className = "secondary", disabled, children, ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { busy?: boolean; busyLabel?: string }) {
  return (
    <button {...rest} className={`${className}${busy ? " is-busy" : ""}`} disabled={disabled || busy} aria-busy={busy || undefined}>
      <span className="btn-label">{children}</span>
      {busy && <><Spinner decorative /><span className="sr-only" role="status">{busyLabel ?? "Working"}</span></>}
    </button>
  );
}

/** A spinner with a short line saying what is loading, for a whole panel or page. */
export function Loading({ label = "Loading", children, className = "" }: { label?: string; children?: ReactNode; className?: string }) {
  return (
    <div className={`loading-line ${className}`}>
      <Spinner label={label} />
      <span>{children ?? label}</span>
    </div>
  );
}

/** Placeholder rows for a table or list that hasn't arrived yet. */
export function SkeletonRows({ rows = 5, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div className="sk-rows" aria-busy="true" role="status" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => <div key={i} className="sk sk-row" style={{ width: `${92 - (i % 3) * 9}%` }} />)}
    </div>
  );
}

/** Placeholder cards, laid out like the grid they stand in for. */
export function SkeletonCards({ count = 3, className = "report-grid", label = "Loading" }: { count?: number; className?: string; label?: string }) {
  return (
    <div className={className} aria-busy="true" role="status" aria-label={label}>
      {Array.from({ length: count }, (_, i) => <div key={i} className="sk sk-tile" />)}
    </div>
  );
}
