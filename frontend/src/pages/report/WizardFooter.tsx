import { useId, type ReactNode } from "react";
import { Button, Spinner } from "../../components/Spinner";

export interface NextAction {
  label: string;
  onClick?: () => void;
  /** Opens a link instead (e.g. the live page). */
  href?: string;
  disabled?: boolean;
  /** Why it's disabled, in plain words: shown beside the button. */
  reason?: string;
  /** Disabled only because work is still running: the reason gets a spinner. */
  waiting?: boolean;
  busy?: boolean;
  busyLabel?: string;
}

/** The bar under every wizard step: back, what's needed, and the one next action. It is
 * sticky, so the next action is on screen without scrolling and sits at the end of the
 * step. The next button is the step's primary only once it can be used — until then the
 * step's own primary (fix, apply, publish) is the thing to click. */
export default function WizardFooter({ back, next, hint, extra }: {
  back?: { label: string; onClick: () => void };
  next?: NextAction | null;
  hint?: ReactNode;
  extra?: ReactNode;
}) {
  const id = useId();
  const blocked = !!next?.disabled && !next.busy;
  const message = blocked && next?.reason ? next.reason : hint;
  return (
    <div className="wizard-footer" role="region" aria-label="Next step">
      {back && <button type="button" className="link" onClick={back.onClick} aria-label={`Back to ${back.label}`} title={`Back to ${back.label}`}>← Back</button>}
      <div id={id} className={`wf-hint ${blocked && !next?.waiting ? "blocked" : ""}`} aria-live="polite">
        {blocked && next?.waiting && <Spinner decorative />}
        {message && <span>{message}</span>}
      </div>
      <div className="wf-actions">
        {extra}
        {next && (next.href && !next.disabled ? (
          <a className="button primary" href={next.href} target="_blank" rel="noreferrer">{next.label}</a>
        ) : (
          <Button type="button" className={blocked ? "secondary" : "primary"} disabled={next.disabled} busy={next.busy}
            busyLabel={next.busyLabel} aria-describedby={blocked ? id : undefined} onClick={next.onClick}>
            {next.label}
          </Button>
        ))}
      </div>
    </div>
  );
}
