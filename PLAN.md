# PDF-to-Web Quarterly Report Platform — Build Plan (for approval)

Status: **Approved 2026-09-24; all six phases implemented** (see README.md for what's real vs placeholder). Decisions below were accepted as recommended; the platform domain is assumed to be publishpdf.ai.

---

## 1. Architecture overview

```
                    ┌──────────────────────────── Control plane ────────────────────────────┐
 Browser (dashboard)│  React SPA  ──►  API (FastAPI)  ──►  Postgres (RLS by tenant_id)      │
                    │                     │   ▲                                               │
                    │                     ▼   │                                               │
                    │               Job queue (Postgres-backed)                               │
                    │                     │                                                   │
                    │   Workers: extract │ ocr │ build-schema │ validate │ render │ publish   │
                    │                     │                                                   │
                    │           Object storage (S3-compatible, private, tenant-prefixed)     │
                    └─────────────────────────────────────┬──────────────────────────────────┘
                                                          │ publish = write immutable static bundle
                    ┌──────────────────────────── Serving plane ────────────────────────────┐
 Public visitor ──► │ Edge (TLS on-demand) ──► host→tenant lookup ──► static bundle for live  │
 / AI crawler       │                        │                        version (+ GA4 per tenant)
                    │                        └──► access log → crawler/visit stats             │
                    └───────────────────────────────────────────────────────────────────────┘
```

Key properties:

- **Control plane and serving plane are separate.** Published sites are plain static files (HTML/MD/JSON/CSV/sitemap/robots/llms.txt), so pages work with JavaScript off and the dashboard can go down without taking client sites down.
- **Every pipeline stage is a pure function of content-addressed inputs.** Idempotency key = `hash(input artifact hashes + stage code version + stage config)`. Re-running a stage with the same inputs is a no-op; changed inputs make a new artifact. Nothing is overwritten.
- **Figures are never retyped.** Each figure gets an ID at extraction time. Later stages (schema, LLM mapping, page rendering) pass figure **IDs** around. The value comes from the extraction record, or from a logged human edit, and from nowhere else.
- **Numbers are never floats.** Normalised values are stored as decimal strings / `NUMERIC`, so `1,23,456.78` can't turn into `123456.77999`.

## 2. Proposed stack (recommendation — see Decision 1)

| Layer | Choice | Why |
| --- | --- | --- |
| Backend API + workers | **Python 3.12, FastAPI, SQLAlchemy 2, Pydantic v2** | Python has the best PDF, OCR and table tools. One language for the API and the extraction code means extraction types don't get copied across a service boundary. |
| Job queue | **Postgres-backed queue (procrastinate)** | No Redis to run. Jobs can be enqueued in the same transaction as the record they belong to, so no lost or phantom jobs. |
| Database | **PostgreSQL 16 with Row-Level Security** | Tenant isolation is enforced in the database as well as in the code. |
| File storage | **S3-compatible** (MinIO locally, cloud bucket in prod) | Private bucket, keys prefixed `tenants/{tenant_id}/…`, signed URLs. |
| PDF text layer | **PyMuPDF** (chars/words with bboxes, fonts) + **pdfplumber** (table cell geometry) | Exact characters from the PDF, no recognition step, so the best digit accuracy. |
| OCR | **Tesseract 5** (primary) with a pluggable adapter for PaddleOCR / cloud OCR | Per-token confidence, runs on our own servers (no data leaves). |
| Page generator | **Jinja2 templates → static HTML**, CSS from theme tokens | Server-rendered, no JS needed, one HTML structure for every theme. |
| Dashboard | **React + TypeScript (Vite), TanStack Query, PDF.js** for the PDF viewer and bbox highlighting | PDF.js renders the source page and draws the issue's bbox. |
| Edge / TLS | **Caddy with on-demand TLS** (see Decision 5) | Automatic ACME certs for verified hostnames only (an `ask` endpoint gates issuance). |
| Tests | pytest (+ hypothesis for number parsing), Playwright for dashboard + JS-off page checks | |
| Local dev | docker-compose: postgres, minio, api, worker, caddy, pebble (test ACME CA) | The full CNAME→HTTPS flow can be tested locally against Pebble. |

## 3. Data model (core tables — all carry `tenant_id` except platform-global ones)

| Table | Purpose / key columns |
| --- | --- |
| `tenants` | id, slug, name, status (active/suspended), created_at |
| `users` | id, email, name, platform_role (null / platform_admin) |
| `memberships` | tenant_id, user_id, role (client_admin / client_reviewer) |
| `invites` | tenant_id, email, role, token_hash, expires_at |
| `source_files` | tenant_id, storage_key, sha256, size, page_count, uploaded_by |
| `reports` | tenant_id, company_name, report_type, fiscal_year, quarter, currency, reporting_unit, slug |
| `report_versions` | tenant_id, report_id, version_no, source_file_id (hash), state (draft / in_review / approved / published / superseded), schema_artifact_id, page_bundle_id, theme_snapshot, created_from_version_id. **Immutable once published** (DB trigger). |
| `pipeline_runs` / `jobs` | tenant_id, version_id, stage, status, idempotency_key, input_artifact_ids, output_artifact_id, error_code, error_message_plain, attempts, timings |
| `artifacts` | tenant_id, kind (extraction / schema / validation / page_bundle), sha256, storage_key, stage_version |
| `extracted_pages` | per page: has_text_layer, method, rotation, header/footer bands, numeric_token_count |
| `figures` | tenant_id, version_id, figure_id (stable), kind (number / date / percent / nil), raw_string, normalized (NUMERIC or ISO date), unit, currency, period, row_label, col_label, table_id, page, bbox, method (text_layer / ocr), confidence, status (extracted / confirmed / edited / not_a_figure) |
| `tables` / `table_cells` | row/col index, spans, header flags, figure_id link |
| `validation_runs` | version_id, summary counts, blocking_open_count |
| `validation_issues` | run_id, check (traceability / re_extraction / arithmetic / period_unit / rendered_page / completeness / low_confidence), severity (blocking / warning), figure_ids, page, section, expected, actual, status (open / resolved) |
| `figure_edits` | figure_id, action (confirm / edit / not_a_figure), old_value, new_value, user_id, at, reason — feeds audit and triggers re-validation |
| `comments` | version_id, section_id, user_id, body, resolved |
| `themes` | tenant_id, report_id (nullable = tenant default), mode, tokens JSON, contrast_report |
| `domains` | tenant_id, hostname, verify_token, status (pending / verified / ssl_issuing / live / failed), failure_reason, last_checked_at, cert_expires_at |
| `analytics_settings` | tenant_id, ga4_measurement_id, consent_banner_enabled |
| `live_pointers` | tenant_id, report_id → published version_id (a republish or rollback just moves this pointer and rebuilds the site index) |
| `audit_log` | tenant_id, actor, action, target, before, after, at, ip. **Append-only** (INSERT-only grant plus a trigger that rejects UPDATE/DELETE). |
| `edge_hits` | tenant_id, host, path, user_agent_class (crawler name / human), at — daily rollups for stats |

**Tenant isolation**: (1) a request/worker context sets `app.tenant_id` per transaction, and RLS policies on every tenant table check it; (2) the repository layer takes a `TenantContext` and never takes raw ids alone; (3) storage access goes only through a helper that builds `tenants/{ctx.tenant_id}/…` keys. The test suite tries cross-tenant reads/writes at the API, repository, SQL (RLS) and storage layers.

## 4. Pipeline design (per stage)

1. **Upload** — PDF magic-byte and structure check, size limit, sha256, store untouched. Metadata form per §4.1.
2. **Extract** — for each page: text-layer check (char count + whether the glyphs map to real Unicode; broken `ToUnicode` maps are common in Indian filings and look fine but extract as garbage, so those pages are sent to OCR). Words and chars get bboxes. Repeated header/footer bands are found across pages and excluded. Multi-column reading order comes from block clustering. Tables come from ruling lines + text alignment into cells with row/col spans and header rows. Charts: vector charts with text labels get their labels extracted; everything else is marked `image_only_not_extracted`.
3. **Number/date parser** — one strict parser, used everywhere: Indian grouping `1,23,456.78`, international `123,456.78`, brackets `(1,234)` → negative, leading `-`/`−` (Unicode minus), `%`, `-`/`–`/`—`/`nil`/`Nil` → nil, `bps`, and currency symbols. Anything ambiguous is flagged as ambiguous, not guessed. Property-based tests.
4. **Build schema** — sections typed by rules (heading dictionaries), optionally helped by an LLM (see Decision 4). Tables are mapped to the schema by cell IDs. Period/unit come from table headers and captions ("₹ in crore", "Q2 FY26", "Quarter ended 30.09.2025"). The output is checked against the published JSON Schema (`schema/report.schema.json`, draft 2020-12).
5. **Validate** — the 7 checks in §4.4. Re-extraction = a second, independent method (Decision 3). Arithmetic checks are only run where totals are detected; failures show both values. Completeness = numeric-token count per page vs figures captured. Low-confidence OCR → mandatory human check.
6. **Render** — Jinja2 → static bundle. Every figure is emitted as `<data value="{normalized}" data-fig="{figure_id}">{raw_string}</data>`, **showing the raw PDF string exactly as it appears** (see Decision 16).
7. **Rendered-page check** — parse the generated HTML, collect every number/date-like token in the text, and confirm each one is either a `data-fig` element whose text equals the schema raw string exactly, or a whitelisted template string that comes from schema metadata. Anything else is a blocking issue.
8. **Publish** — needs a client_admin, zero open blocking issues, and the "I reviewed the figures" checkbox (stored in the audit log). The bundle is frozen, the live pointer moves, and sitemap/llms.txt/latest/archive are rebuilt.

**LLM hard rule enforcement (in code + tests):**
- LLM output is parsed with a strict Pydantic model whose fields are only enums (section types) and existing IDs (block/table/cell/figure). Free text is not accepted.
- A guard rejects any LLM response containing a digit sequence or date pattern outside ID fields.
- The schema builder copies figure values only from `figures` rows. It has no code path that reads a value from LLM output.
- An invariant test: the multiset of `(figure_id, raw_string, normalized)` in the final schema == extraction + logged human edits, no matter what the LLM returned (tested with an adversarial fake LLM that tries to inject and alter numbers).

## 5. Open decisions — options and recommendations

### D1. Tech stack
| Option | Trade-offs |
| --- | --- |
| **A. Python (FastAPI) API + workers, React dashboard** ✅ | Best PDF/OCR tooling, one language on the backend. Two languages across the whole repo. |
| B. TypeScript everywhere (Node API) + Python extraction microservice | Matches FeedGraph's stack, but the figure types sit on both sides of a service boundary where they can drift, and there are two backends to run. |
| C. Python full-stack with server-rendered dashboard (HTMX) | Simplest, but the PDF viewer, side-by-side scroll sync and theme live preview are painful without a real SPA. |

**Recommend A.** Queue: Postgres-backed (procrastinate) over Celery+Redis, to run less infrastructure and enqueue in the same transaction.

### D2. Extraction approach
| Option | Digit accuracy | Cost | Data residency |
| --- | --- | --- | --- |
| A. Open-source only (PyMuPDF, pdfplumber, Tesseract/PaddleOCR) | Text-layer pages: exact (no recognition step). Scanned: good on clean scans, weaker on complex scanned tables | Compute only | Stays on our servers |
| B. Paid document-AI (Azure Document Intelligence, AWS Textract, Google Document AI) | Strong tables + OCR on scans | Per page, billed by the vendor (prices to confirm with vendor, not estimated here) | Vendor region, and the vendor sees **unpublished results** |
| **C. Hybrid** ✅ | Open-source by default (most listed-company PDFs have a real text layer); paid adapter switched on per tenant only for scanned pages/tables | Paid cost only on scanned pages | Paid path is opt-in per tenant |

**Recommend C**, built open-source-first with the paid provider behind an adapter interface. **Important:** results uploaded before public release are likely price-sensitive/UPSI (SEBI PIT Regulations). Every third party that sees the PDF is a disclosure concern, which argues for defaulting to our own processing. Please confirm with legal.

### D3. Second (independent) extraction method
| Option | Independence | Notes |
| --- | --- | --- |
| **A. Render page at 300 DPI → OCR each figure's bbox crop (digit-whitelisted) and compare to the text-layer value** ✅ | High: catches wrong/broken text layers, hidden text, overlapping glyphs | Mismatches from OCR noise are possible. A mismatch is blocking, and the reviewer resolves it with **Confirm correct** (logged). Per-crop OCR with a digit whitelist keeps noise low. |
| B. Second text parser (pdfminer vs PyMuPDF) | Low: both read the same text layer | Catches parser bugs, not bad PDFs. Cheap. |
| C. Vision LLM reads the crop | High | Sends UPSI to a third party. An LLM producing numbers (even only to compare) conflicts with the spirit of the hard rule. |

**Recommend A**, plus B as a cheap extra check. For OCR-origin pages, the second method = a second OCR engine (Tesseract ↔ PaddleOCR), or the cloud OCR if D2 enables it.

### D4. LLM use and provider
| Option | Trade-offs |
| --- | --- |
| A. No LLM (rules only) | Fully deterministic, no data leaves. Weaker section typing on unusual layouts. |
| **B. Optional LLM for section classification + table→schema mapping only, IDs/enums out, behind an interface; off by default per tenant** ✅ | Better structure on messy decks. Enforcement is described in §4. Rules-only fallback always works. |
| C. LLM-first mapping | Most flexible, most risk, most data exposure. |

**Recommend B.** Provider: Claude (Sonnet 5) via API as default, adapter for others. Given UPSI, only send the heading/label text and structure, never the full document, and only for tenants that opt in. You need to decide whether any LLM may see pre-publication content.

### D5. Hosting, CDN and SSL
| Option | Effort | Cost | Lock-in |
| --- | --- | --- | --- |
| **A. Self-managed edge: Caddy on-demand TLS (Let's Encrypt/ZeroSSL), `ask` endpoint only allows verified hostnames, CDN optional in front of origin** ✅ for MVP | Medium: we run the edge, cert storage (shared), HA | Infra only | Low |
| B. Cloudflare for SaaS (Custom Hostnames) | Low: managed certs, renewal, global edge | Per-hostname fees above a free tier (check current pricing) | Medium–high |
| C. AWS CloudFront + ACM | High: cert/distribution quotas per custom hostname, slow provisioning | Per-request + distribution | High (AWS) |

**Recommend A** behind a `CertificateProvider`/`EdgeRouter` interface so B can be swapped in later. A can be tested end to end locally with Pebble (a test ACME CA), which is what makes the Phase 5 "done when" provable.

### D6. Platform domain, `PLATFORM_TARGET_HOSTNAME`, preview URL pattern
I will not invent these. Placeholders in `.env.example`:
- `PLATFORM_DOMAIN` — **you supply**
- `PLATFORM_TARGET_HOSTNAME` — suggested shape `edge.${PLATFORM_DOMAIN}` (CNAME target)
- `PREVIEW_URL_PATTERN` — suggested `{tenant_slug}.preview.${PLATFORM_DOMAIN}` (wildcard cert, `noindex`, `X-Robots-Tag: noindex`, disallow-all robots.txt)

### D7. TXT verification record name
| Option | Notes |
| --- | --- |
| **A. `_${VERIFY_PREFIX}.investors.client.com` TXT `${VERIFY_PREFIX}-verify=<token>`** ✅ | Scoped to the exact subdomain. Required anyway: a CNAME can't coexist with a TXT at the *same* name. |
| B. `_${VERIFY_PREFIX}.client.com` (on the parent) | One record could verify many subdomains, which is too broad. |
| C. Token in the CNAME target (`<token>.edge…`) | One record instead of two, but it puts the token in the routing path and conflicts with the brief's "two records". |

`VERIFY_PREFIX` is a placeholder until you give me a brand/platform name.

### D8. Published URL pattern
The brief's `/{fy}/{quarter}/{section}` **collides** when the same quarter has both results and an investor presentation, and annual reports have no quarter.
| Option | Example |
| --- | --- |
| A. `/{fy}/{quarter}/{section}`, one report per quarter | `/fy2026/q2/profit-and-loss` — collides |
| **B. `/{fy}/{period}/{report-type}/` + `#section` anchors, with per-section pages at `/{fy}/{period}/{report-type}/{section}`** ✅ | `/fy2026/q2/results/profit-and-loss`, `/fy2026/q2/investor-presentation/`, `/fy2026/annual-report/segment-results` |
| C. Opaque slugs `/reports/{slug}` | Stable but not human-readable |

Plus the fixed routes: `/latest` (302 to the newest; or `/latest/` serving it with a canonical to the real URL, which I recommend), `/archive/`, `…/report.md`, `…/figures.json`, `…/figures.csv`, `/sitemap.xml`, `/robots.txt`, `/llms.txt`.
**Question:** should sections be separate pages *and* anchors on the landing page (I recommend both, with the canonical on the section page), or anchors only?

### D9. Default robots.txt for AI crawlers
| Option | Notes |
| --- | --- |
| **A. Allow all (search + AI retrieval + AI training) by default; client can switch to B** ✅ | Matches the product's purpose (being read correctly by AI). |
| B. Allow search + AI retrieval/answer bots (e.g. OAI-SearchBot, ChatGPT-User, PerplexityBot, Claude-User), block training crawlers (GPTBot, CCBot, Google-Extended, ClaudeBot) | Some IR/legal teams will prefer this. |
| C. Block all AI | Defeats the product. Allowed only as an explicit client choice. |

**Recommend A default + per-tenant preset switch (A/B/C)**, no free-form robots editing (so nobody can accidentally `Disallow: /`). Preview hosts are always disallow-all.

### D10. Cookie consent for GA4 (DPDP Act 2023 / DPDP Rules; GDPR if EU visitors)
| Option | Notes |
| --- | --- |
| A. No banner, GA4 loads immediately | Simplest; weakest compliance position |
| **B. Banner per tenant, GA4 not loaded until the visitor accepts (or Consent Mode v2 default = denied)** ✅ | Visitors who decline lose no content; banner is a tiny progressive JS on top of a JS-free page |
| C. Platform-level CMP vendor | More features, another third party and cost |

**Recommend B, banner ON by default** for every tenant that sets a GA4 ID. The client can only turn it off by confirming a statement (logged). Not legal advice; your legal team should confirm.

### D11. Disclaimer wording
Placeholder `DISCLAIMER_DEFAULT_TEXT` plus a per-tenant override (plain text / limited Markdown, sanitised). **Recommend: publishing is blocked until a disclaimer is set** (platform default or tenant). You supply the text after legal review.

### D12. Limits and retention (all config; values proposed for you to confirm, not decided)
| Setting | Proposal |
| --- | --- |
| `MAX_UPLOAD_MB` | 100 (annual reports with images can run large; confirm) |
| `MAX_PAGES` | 600 |
| `OCR_CONFIDENCE_THRESHOLD` | 0.95 per token for digits (financial content warrants a high bar; tune on your corpus) |
| `RETENTION_UNPUBLISHED_DAYS` | You decide. Proposal: delete source PDFs for never-published reports after N days |
| Published PDFs | Kept while any published version references them (they're the official source link) |

### D13. Data residency
| Option | Notes |
| --- | --- |
| **A. All storage + processing in an India region; serving edge can be global (published content is public)** ✅ | Safest for Indian listed companies + UPSI |
| B. Any region | Simpler, cheaper choices |
| C. Per-tenant region | Most flexible, heaviest ops |

Need your answer, and whether any clients are outside India.

### D14. Authentication
| Option | Notes |
| --- | --- |
| A. Email + password (+ TOTP MFA) | Familiar; we own password storage and reset |
| **B. Email magic link / OTP + mandatory TOTP MFA for client_admin and platform_admin** ✅ | No passwords to leak. MFA on the role that publishes financial content. |
| C. Enterprise SSO (SAML/OIDC) | Large clients will ask. **Recommend as a later phase**, with the auth layer built so it plugs in. |

### D15. AI-written summaries / Q&A
**Recommend: out of scope** (as the brief says). If ever added: AI text goes through the same rendered-page check (it may only cite figures by `data-fig` ID), is visibly labelled, and needs its own separate approval checkbox. Needs a yes/no from you before I design anything for it.

### D16. (Raised by me) Display format of figures on the page
| Option | Notes |
| --- | --- |
| **A. Show the raw PDF string exactly as it appears (`(1,234.5)`, `1,23,456`)** ✅ | Zero transformation, and the rendered-page check is a plain string-equality test |
| B. Reformat (e.g. `−1,234.5`, add unit inline) | Arguably more readable, but adds a transformation layer that can introduce errors |

Machine-readable values go in `<data value>` / JSON / CSV either way.

## 6. Phase breakdown

| Phase | Scope | Done when |
| --- | --- | --- |
| **0. Foundations** (added) | Repo, docker-compose (pg, minio, caddy, pebble), migrations, auth (per D14), tenants/memberships, **RLS + TenantContext from day one**, audit log, job runner, env template, CI | App boots locally; login works; isolation test harness exists |
| **1. Extraction + schema** | Upload + metadata, text-layer/OCR per page, tables, chart handling, number/date parser, schema builder, JSON Schema, schema view (tree + raw + download), processing-status screen, corpus runner | Sample PDFs → schema docs with every figure traced to page+bbox; per-sample accuracy report (needs ground-truth for your PDFs) |
| **2. Validation** | All 7 checks, validation report UI with PDF bbox side-by-side, confirm/edit/not-a-figure, audit trail, re-validation, publish gate | Seeded-error corpus: 100% of seeded errors caught; publish API refuses with open blocking issues |
| **3. Page generation + review** | Static generator, rendered-page check, discoverability (schema.org, JSON/CSV, MD, llms.txt, sitemap, robots, canonical, latest/archive), preview desktop/mobile, side-by-side review, comments, approve & publish, versions + rollback, preview host | Rendered-page check passes on all samples; Playwright confirms pages are readable with JS disabled |
| **4. Theming** | Token model, editor with live preview, "match my website" (fetch + propose), reference-based (URLs/images → palette), contrast enforcement + fixes, per-report override | Same report in all 3 modes → identical HTML structure (DOM-structure hash equal, only CSS differs); contrast AA enforced |
| **5. Multi-tenancy + domains** | Roles UI, team invites, platform admin screens (tenants, jobs, re-run, cross-tenant issue overview), full CNAME flow, DNS checker state machine, provider instructions (GoDaddy, Cloudflare "DNS only", Route 53, Squarespace/Google), IT email, on-demand TLS, 301 preview→custom, monitoring + alerts | Test tenant: subdomain entry → verified → live HTTPS (Pebble locally, real ACME on staging); cross-tenant tests pass |
| **6. Analytics** | GA4 ID (format `G-XXXXXXXXXX`), injection only into that tenant's published pages, consent banner toggle, auto-republish on change, edge-log crawler/visit stats | Tests prove the GA4 tag only exists in that tenant's published bundle; stats show by crawler |

Every phase ends with: tests run, a demo of what works, a list of what's stubbed, and waiting for approval.

## 7. What I need from you

1. Approval of this plan (or changes).
2. Answers to D1–D16.
3. **Sample PDFs for the test corpus** (text-layer results, scanned, multi-column deck, complex tables, an annual report). Ideally with a hand-checked list of key figures per PDF, so accuracy numbers mean something.
4. Platform/brand name + domain (D6, D7).
5. Whether this should stay fully standalone (my assumption) or share anything with FeedGraph (auth, infra).
