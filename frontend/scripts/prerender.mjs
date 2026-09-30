// After `vite build`: write the landing page as prerendered HTML (dist/index.html, served for
// "/") and keep the plain app shell for every other route (dist/app.html, noindex).
import { readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const dist = join(root, "dist");
const { render } = await import(join(root, "dist-ssr", "entry-server.js"));

const shell = readFileSync(join(dist, "index.html"), "utf8");
writeFileSync(join(dist, "app.html"), shell);

const description = "PublishPDF turns report PDFs into fast, on-brand web pages: every figure checked against the PDF, readable on any screen, found by search and AI.";
const landing = shell
  .replace('<div id="root"></div>', `<div id="root">${render("/")}</div>`)
  .replace('<meta name="robots" content="noindex, nofollow" />', '<meta name="robots" content="index, follow" />')
  .replace("<title>PublishPDF</title>",
    `<title>PublishPDF — report PDFs, rebuilt for the web</title>\n    <meta name="description" content="${description}" />` +
    `\n    <meta property="og:title" content="PublishPDF — report PDFs, rebuilt for the web" />\n    <meta property="og:description" content="${description}" />`);
if (!landing.includes('class="lp"')) throw new Error("prerender produced no landing markup");
writeFileSync(join(dist, "index.html"), landing);
rmSync(join(root, "dist-ssr"), { recursive: true, force: true });
console.log(`prerendered landing page: ${(landing.length / 1024).toFixed(1)} KB`);
