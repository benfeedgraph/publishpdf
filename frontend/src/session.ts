import type { Me } from "./api";

// The signed-in user is remembered on this device so the app opens instantly on the next
// visit; the server re-checks it in the background, and the copy is cleared on sign-out or
// when the session has ended. It holds only what the header already shows.
const ME_KEY = "ppdf.me";

export function cachedMe(): Me | undefined {
  try {
    const raw = typeof window !== "undefined" ? window.localStorage.getItem(ME_KEY) : null;
    return raw ? (JSON.parse(raw) as Me) : undefined;
  } catch {
    return undefined;
  }
}

export function rememberMe(me: Me): void {
  try { window.localStorage.setItem(ME_KEY, JSON.stringify(me)); } catch { /* storage unavailable */ }
}

export function forgetMe(): void {
  try {
    window.localStorage.removeItem(ME_KEY);
    // the workspace report lists remembered on this device go with the session
    for (let i = window.localStorage.length - 1; i >= 0; i--) {
      const k = window.localStorage.key(i);
      if (k && k.startsWith(REPORTS_KEY)) window.localStorage.removeItem(k);
    }
  } catch { /* storage unavailable */ }
}

// The last report list seen for a workspace, shown instantly on the next visit while the
// server is asked again in the background (same privacy as the header: titles and statuses).
const REPORTS_KEY = "ppdf.reports.";

export function cachedReports<T>(tenantId: string | undefined): T | undefined {
  if (!tenantId) return undefined;
  try {
    const raw = window.localStorage.getItem(REPORTS_KEY + tenantId);
    return raw ? (JSON.parse(raw) as T) : undefined;
  } catch {
    return undefined;
  }
}

export function rememberReports(tenantId: string | undefined, data: unknown): void {
  if (!tenantId) return;
  try { window.localStorage.setItem(REPORTS_KEY + tenantId, JSON.stringify(data)); } catch { /* storage unavailable */ }
}
