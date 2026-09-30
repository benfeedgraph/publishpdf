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
  try { window.localStorage.removeItem(ME_KEY); } catch { /* storage unavailable */ }
}
