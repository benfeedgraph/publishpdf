# Architecture

Living document; each phase adds its section. Decisions referenced as D1–D16 are in `PLAN.md` §5.

## Components

| Component | Tech | Location |
| --- | --- | --- |
| API | FastAPI (Python 3.12) | `backend/app/main.py`, `backend/app/api/` |
| Worker | Same codebase, `python -m app.cli worker` | `backend/app/jobs.py`, `backend/app/pipeline.py` |
| Database | PostgreSQL 16 with Row-Level Security | `backend/migrations/` |
| File storage | Local FS (dev) / S3-compatible (prod) | `backend/app/storage.py` |
| Dashboard | React + TypeScript (Vite) | `frontend/` |
| Edge (Phase 5) | Caddy with on-demand TLS | `infra/caddy/` |

## Tenant isolation (Phase 0)

Three independent layers; a bug in one does not expose data.

1. **Database (RLS).** Every tenant-owned table has `FORCE ROW LEVEL SECURITY` and policies
   that compare `tenant_id` to the transaction-local setting `app.tenant_id`. The runtime role
   (`publishpdf_app`) is not a superuser, doesn't own the tables and lacks `BYPASSRLS`. An
   unset setting reads as NULL, which matches nothing, so a missing context fails closed.
2. **Session layer.** `app.db.session(ctx)` is the only way to get a DB session; it writes the
   `Context` into `app.tenant_id` / `app.user_id` / `app.platform_admin` / `app.system` at the
   start of *every* transaction with `set_config(..., true)` (transaction-local), so pooled
   connections can't carry one request's tenant into the next. Opening a session without a
   context raises.
3. **Storage.** `app.storage` takes a `Context` plus a relative key and always builds
   `tenants/{tenant_id}/{key}`; traversal and absolute keys are rejected. No API accepts a full key.

API: tenant-scoped routes are `/api/tenants/{tenant_id}/…`. `deps.tenant_ctx` resolves the
caller's membership; non-members get **404** (not 403), so tenant ids can't be probed.

`system_context()` bypasses RLS and is used only for pre-auth lookups (`security/auth.py`),
claiming jobs (`jobs.py`) and operator CLI commands.

## Roles

| Role | Scope | Where enforced |
| --- | --- | --- |
| Platform admin | all tenants | `users.is_platform_admin`; RLS `app.platform_admin`; `deps.platform_admin` |
| Client admin | one tenant | `memberships.role`; `tenancy.PERMISSIONS` via `deps.require(...)` |
| Client reviewer | one tenant | same |

A DB trigger (`users_guard_privileged`) stops users changing their own `is_platform_admin`,
`disabled_at` or `email`. A tenant must always keep at least one client admin.

## Authentication (D14)

- Passwordless magic links (15-minute, single-use, only SHA-256 hashes stored, no account
  enumeration, 5 links per 15 minutes per user).
- TOTP two-factor is **mandatory for platform admins and anyone who is a client admin in any
  tenant** (the roles that publish). This is checked on every request, so promoting someone takes
  effect immediately. TOTP secrets are Fernet-encrypted with `APP_SECRET_KEY`. 5 wrong codes
  revoke the session.
- A session that hasn't passed two-factor can still reach `/api/auth/me`, the two-factor
  endpoints and logout, and nothing else.
- Sessions: opaque 256-bit token in an HttpOnly, SameSite=Lax cookie; hash stored server-side.
- CSRF: every POST/PUT/PATCH/DELETE under `/api/` needs the header `X-PPDF-CSRF: 1`.
- Invites: 7-day single-use links; accepting one creates the account if it doesn't exist
  (the link proves the invitee owns the address).
- Enterprise SSO (SAML/OIDC) is a later phase.

## Audit log

`audit_log` is append-only: the runtime role has only SELECT and INSERT, and a trigger rejects
UPDATE, DELETE and TRUNCATE from anyone, the table owner included. Entries are written in the
same transaction as the change they describe.

## Job queue

The `jobs` table plus `FOR UPDATE SKIP LOCKED`. (The plan named the procrastinate library; a
~150-line in-house queue was chosen instead because it needed per-tenant idempotency keys and
RLS-scoped handler execution, both awkward to add on top.)

- Enqueue runs inside the caller's transaction.
- `UNIQUE (tenant_id, idempotency_key)` makes enqueueing idempotent.
- Handlers run under `worker_context(tenant_id)`, the same RLS as a tenant user.
- Retries with exponential backoff. Clients see only `error_plain`; the technical traceback
  (`last_error`) is visible only to platform admins.
- Jobs whose lock is older than 30 minutes are recovered. Platform admins can re-run jobs.

## Data model

| Table | Purpose |
| --- | --- |
| `tenants`, `users`, `memberships`, `invites`, `login_tokens`, `sessions` | Accounts and roles (0001) |
| `audit_log` | Append-only trail (0001) |
| `jobs` | Queue; `version_id` links pipeline jobs to a report version (0001/0002) |
| `source_files` | Original PDFs by SHA-256 (stored at `tenants/{t}/sources/{sha}.pdf`, private) |
| `reports` | One per (fiscal year, period, type); `live_version_id` = what's published; optional `theme_override` |
| `report_versions` | Schema JSON, bundle key + SHA-256, theme snapshot, validation summary, publish info. **Immutable once published** (trigger), undeletable once published |
| `validation_issues` | Per validation run; latest run is authoritative |
| `figure_reviews` | Confirm / edit / not-a-figure / flag, with old & new value — append-only for the app role |
| `comments` | Per section of a version |
| `tenant_settings` | Theme tokens, disclaimer, robots policy, GA4 + consent, LLM opt-in |
| `domains` | Custom subdomain state machine |
| `edge_hits` | Public-site requests classified by crawler, for the stats page |

All tables are RLS-scoped by tenant (migration 0002).

## Pipeline

```
upload ──► pipeline.extract ──► pipeline.render ──► pipeline.validate ──► needs_review | validation_issues
             (PDF → schema)      (schema → bundle)   (7 checks)                 │
                                                                                 ▼
            figure edit / confirm ──► render ──► validate          publish (admin, 0 blocking,
                                                                    checkbox, validated hashes match)
```

- Each stage's idempotency key is derived from its inputs (source SHA + pipeline version;
  schema SHA + theme; schema SHA + bundle SHA), so re-running with the same inputs is a no-op and
  any stage can be re-run.
- Extraction artifacts (every word with bbox and confidence) are stored per version; the schema is
  on the version row; the bundle under `tenants/{t}/versions/{v}/bundle/`.

### Extraction (`app/extraction/`)

| Module | Job |
| --- | --- |
| `pdf.py` | Per page: text layer from characters (exact bboxes) if usable, else OCR. A text layer with >2% unreadable glyphs (broken ToUnicode) is sent to OCR. Image regions and vector-chart regions (clustered filled shapes) are found here |
| `ocr.py` | Tesseract, local only. Scans are despeckled and deskewed; layout runs in deskewed space while each word keeps its original-page bbox for tracing |
| `layout.py` | Repeated header/footer removal (fuzzy across pages), visual lines, column segments, tables (numeric-column anchors, multi-row and merged headers, unit captions), headings, multi-column paragraphs, reading order |
| `numbers.py`, `dates.py` | The only parsers. Decimal values, never floats |
| `tokens.py` | Words → text runs + figure tokens. **Invariant: no digit survives in plain text** |
| `schema_builder.py` | Figure registry, sections, tables, charts, section classification, metadata |
| `classify.py` | Rule-based section types; optional LLM that only returns `{section id: type}`. Digits are masked before sending; any other output is discarded (tested with an adversarial model) |

### The figure invariant

A figure's characters exist once, in `schema.figures[fid].raw`, copied from the PDF (or entered by
a person, which is logged). Text runs are `{"t": ...}` (never containing a digit — enforced in code
and in the JSON Schema) or `{"f": fid}`. The renderer prints `figures[fid].raw` inside
`<data data-fig>` / `<time data-fig>`; it has no code path that formats or computes a number.

### Validation (`app/validation.py`)

| Check | Blocking when | Notes |
| --- | --- | --- |
| traceability | the raw string isn't at the recorded bbox in the PDF's text layer (or the OCR record) | re-reads the PDF, not our copy |
| re_extraction | a visual re-read of the bbox (rendered page → OCR crop, 3 resolutions) disagrees on digits, sign or kind | catches wrong/hidden text layers and OCR misreads |
| arithmetic | a total fails in one column while it adds up in others; balance sheet doesn't balance | stated % changes are warnings |
| period_unit | a figure's period ≠ its column; crore and lakh both used and a table has no unit label | missing unit/period labels are warnings |
| rendered_page | any digit on a generated page (text, title, attributes, JSON-LD, Markdown) isn't a schema figure/label with identical text | can't be waived by reviewers |
| completeness | — (warning) | numeric tokens per page vs captured; image-only charts |
| low_confidence | OCR confidence < `OCR_CONFIDENCE_THRESHOLD`, or a numeric-looking value that doesn't parse | human must confirm/edit |
| schema | document fails `backend/app/schema/report.schema.json` | |

A confirmation resolves a figure's issues only while its raw value equals the confirmed value.

## Rendering and serving

- `app/render/site.py` builds the bundle: `/{fy}/{period}/{type}/` landing page with all sections
  anchored, one page per section, `report.md`, `figures.json`, `figures.csv`. Tenant index:
  home, `/archive/`, `/latest/` (served), `sitemap.xml`, `robots.txt`, `llms.txt`.
- URLs in bundles use an origin placeholder, filled in at serve time with the tenant's canonical
  origin. When a custom domain goes live, canonicals and the sitemap switch immediately while the
  published (approved) bytes stay unchanged.
- `app/public.py` routes by Host: live custom domains, and preview hosts (`PREVIEW_URL_PATTERN`,
  always noindex, 301 to the custom domain once live). It injects the tenant's GA4 tag (with the
  consent banner unless the tenant opted out with a recorded reason) into HTML only, and logs
  each request with a crawler classification.
- Theme: tokens → CSS variables only (`app/render/theme.py`); WCAG contrast is enforced on
  render; a test asserts HTML (minus `<style>`) is byte-identical across themes.

## Custom domains and TLS

- `app/domains.py`: apex rejection (public-suffix aware, offline snapshot), records
  (CNAME → `PLATFORM_TARGET_HOSTNAME`, TXT `_${VERIFY_PREFIX}.{host}` = `${VERIFY_PREFIX}-verify={token}`),
  provider guides, IT email, and the pure state-transition function
  `pending → (verified) → ssl_issuing → live`, `failed` with a specific reason
  (CNAME missing / elsewhere / proxied, TXT missing / mismatch, CAA blocks, cert failed / expiring).
- `app/domain_jobs.py`: checks on demand ("Check now") and on a schedule (every minute for the first
  hour, then 10 min; live domains every 30 min). Live → failed alerts client admins and platform
  admins (at most every 6 h) and rebuilds the site.
- `infra/caddy/Caddyfile`: on-demand TLS; Caddy's `ask` hook (`/internal/tls-ask`) only approves
  hostnames the platform serves, so nobody can make us request certificates for arbitrary names.
  Preview hosts need a wildcard certificate (DNS-01) in production.
