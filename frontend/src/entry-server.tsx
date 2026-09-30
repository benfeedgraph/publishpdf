/* Build-time render of the public landing page to static HTML (see scripts/prerender.mjs).
 * The browser then hydrates the same markup, so the page is complete before any
 * JavaScript runs — instant for visitors, fully readable for crawlers. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import Landing from "./pages/Landing";

export function render(url: string): string {
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false } } });
  return renderToString(
    <QueryClientProvider client={client}>
      <StaticRouter location={url}>
        <Landing />
      </StaticRouter>
    </QueryClientProvider>,
  );
}
