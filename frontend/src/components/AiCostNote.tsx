import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { IconSparkle } from "./Icons";

interface AiCosts { ai_available: boolean; auto_check_cap_credits: number; layout_cap_credits: number; credit_usd: number }

/** What a report can spend on AI, said before anything runs: the web page and all checks
 *  are free; only the automatic AI double-check of figures read from images uses credits,
 *  and never more than the per-report cap. */
export function AiCostNote({ tenantId, compact = false }: { tenantId: string; compact?: boolean }) {
  const q = useQuery({
    queryKey: ["settings", tenantId],
    queryFn: () => api<{ ai_costs?: AiCosts }>(`/api/tenants/${tenantId}/settings`),
    staleTime: 60_000,
  });
  const c = q.data?.ai_costs;
  if (!c) return null;
  const cap = c.auto_check_cap_credits + c.layout_cap_credits;
  const usd = (cap * c.credit_usd).toFixed(2);
  return (
    <div className={`ai-cost-note${compact ? " compact" : ""}`}>
      <span className="icon-chip"><IconSparkle /></span>
      <div>
        <strong>AI credits for this report: {cap > 0 ? `at most ${cap}` : "none"}</strong>
        <p className="muted small" style={{ margin: "2px 0 0" }}>
          Building the web page, its charts and all nine checks use no AI credits.
          {c.auto_check_cap_credits > 0
            ? ` If numbers read from images or photos can't be confirmed, the AI double-checks them automatically — up to ${c.auto_check_cap_credits} credits ($${usd}); anything beyond that waits for your OK with its own estimate.`
            : c.ai_available ? " The AI double-check only runs when you start it, after seeing its estimate." : ""}
          {c.layout_cap_credits > 0 ? ` AI page layout: up to ${c.layout_cap_credits} credits.` : ""}
        </p>
      </div>
    </div>
  );
}
