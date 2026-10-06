import type { QueryClient } from "@tanstack/react-query";
import type { NavigateFunction } from "react-router-dom";
import { api, type Me } from "./api";

/** Where a signed-in person should land. The public homepage stays at `/`. */
export function workspacePath(me: Me): string {
  if (!me.fully_authenticated) return "/mfa";
  const first = me.tenants[0];
  if (first) return `/t/${first.id}`;
  if (me.user.is_platform_admin) return "/admin/tenants";
  return "/";
}

/** `signedIn` is the sign-in response: it already says who is signed in, so no second call. */
export async function enterApp(qc: QueryClient, navigate: NavigateFunction, signedIn?: { me?: Me | null }) {
  const me = signedIn?.me ?? await api<Me>("/api/auth/me");
  qc.setQueryData(["me"], me);
  navigate(workspacePath(me), { replace: true });
}
