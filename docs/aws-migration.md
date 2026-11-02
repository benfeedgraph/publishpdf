# Migration runbook: Railway → AWS (EC2 + RDS)

Moves the **backend API + worker** to a single EC2 box and **Postgres** to RDS.
Everything else stays external and unchanged:

| Piece | Stays on | Action needed |
|---|---|---|
| Frontend | Vercel | repoint its `/api` + `/sites` proxy to the new backend URL |
| Object storage | Cloudflare R2 | none (same `S3_*` env, R2 endpoint) |
| Email | Resend | none (same SMTP/API env) |
| LLM | Anthropic / Gemini | none |

Target footprint: 1× EC2 `t4g.small` (API + worker) + RDS `db.t4g.micro`. ≈ $32/mo.

---

## Phase 0 — Prerequisites

- An AWS account + a region (examples use `us-east-1`; pick one near your users).
- Your **Railway Postgres connection string** (Railway dashboard → Postgres → Connect).
- Your **current Railway env vars** for the backend (copy them; you'll adapt a few).
- Locally: `psql` + `pg_dump`/`pg_restore` (Postgres 16 client), and an SSH key pair.
- Decide two new strong passwords for the DB roles `publishpdf_owner` and `publishpdf_app`.

> Do the whole migration against a **test workspace first** if you can, and schedule a short
> maintenance window for the real cutover (uploads in flight during the switch can be lost).

---

## Phase 1 — Provision RDS PostgreSQL

1. RDS → Create database → **Standard create** → PostgreSQL (version **16**, match or exceed Railway's).
2. Templates: **Free tier** if offered, else **Dev/Test**.
3. Instance: **db.t4g.micro**. Storage: 20 GB gp3. Disable storage autoscaling for now (optional).
4. Credentials: set a **master username** (e.g. `pgadmin`) + master password. Store them in a password manager.
5. Connectivity:
   - **Public access: No** (the EC2 box will reach it privately). Set to **Yes** only if you must run `pg_restore` from your laptop — if so, turn it back off after.
   - Create/choose a **security group** for RDS; you'll open port 5432 to the EC2 box in Phase 4.
6. Create. Note the **endpoint** hostname once available (e.g. `publishpdf.xxxx.us-east-1.rds.amazonaws.com`).

---

## Phase 2 — Create the two roles + database on RDS

RDS has no `docker-entrypoint-initdb`, so run your `infra/postgres/init.sql` equivalent by hand.
Connect as the **master user** to the default `postgres` database:

```bash
psql "postgresql://pgadmin:MASTER_PW@RDS_ENDPOINT:5432/postgres?sslmode=require"
```

Then (production version of `infra/postgres/init.sql` — note: **no** `publishpdf_test` DB):

```sql
CREATE ROLE publishpdf_owner LOGIN PASSWORD 'OWNER_PW' CREATEDB;
CREATE ROLE publishpdf_app   LOGIN PASSWORD 'APP_PW'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE DATABASE publishpdf OWNER publishpdf_owner;
```

---

## Phase 3 — Move the data

Dump from Railway, restore into RDS. Role names are identical on both sides, so ownership
and grants carry over cleanly.

```bash
# 1. Dump Railway (custom format)
pg_dump "RAILWAY_DATABASE_URL" -Fc -f railway.dump

# 2. Restore into RDS, into the publishpdf database owned by publishpdf_owner
pg_restore -d "postgresql://publishpdf_owner:OWNER_PW@RDS_ENDPOINT:5432/publishpdf?sslmode=require" \
  --no-privileges=false --exit-on-error railway.dump
```

Then sanity-check:

```bash
psql "postgresql://publishpdf_app:APP_PW@RDS_ENDPOINT:5432/publishpdf?sslmode=require" \
  -c "\dt" -c "select count(*) from users;"   # adjust table name
```

> If `pg_restore` complains about missing roles or grants, confirm Phase 2 ran on the same
> RDS instance. If you'd rather start from a clean schema, you can instead create the schema
> with `cli migrate` (Phase 6) and restore **data only** with `pg_dump --data-only`.

---

## Phase 4 — Provision the EC2 box

1. EC2 → Launch instance.
   - AMI: **Ubuntu 24.04** (or Amazon Linux 2023), **Arm64**.
   - Type: **t4g.small** (2 vCPU / 2 GB). Bump to `t4g.medium` if OCR jobs run tight.
   - Storage: 30 GB gp3.
   - Key pair: your SSH key.
2. **Security group** (inbound):
   - `22/tcp` from **your IP only**.
   - `80/tcp` and `443/tcp` from `0.0.0.0/0` (so Vercel can reach the API and ACME can issue certs).
3. **Elastic IP**: allocate one and associate it with the instance (stable public address).
4. Open RDS to the box: edit the **RDS security group** → add inbound `5432/tcp` from the
   **EC2 instance's security group** (not a raw IP).

---

## Phase 5 — Install Docker + the app on the box

SSH in (`ssh ubuntu@ELASTIC_IP`), then:

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker ubuntu && newgrp docker
git clone YOUR_REPO_URL app && cd app/publishPdf   # or copy ./backend up
```

Create the production env file `backend/.env.prod` from your Railway vars, changing only:

```ini
APP_ENV=production                 # worker refuses to run if this is 'development'
COOKIE_SECURE=true
PUBLIC_SCHEME=https

# --- Database → RDS (two roles) ---
DATABASE_OWNER_URL=postgresql+psycopg://publishpdf_owner:OWNER_PW@RDS_ENDPOINT:5432/publishpdf?sslmode=require
DATABASE_APP_URL=postgresql+psycopg://publishpdf_app:APP_PW@RDS_ENDPOINT:5432/publishpdf?sslmode=require

# --- Storage → Cloudflare R2 (unchanged keys, R2 endpoint) ---
STORAGE_BACKEND=s3                  # worker refuses local storage with a remote DB
S3_BUCKET=your-r2-bucket
S3_REGION=auto
S3_ENDPOINT_URL=https://<accountid>.r2.cloudflarestorage.com
AWS_ACCESS_KEY_ID=your-r2-access-key-id        # R2 credentials for boto3
AWS_SECRET_ACCESS_KEY=your-r2-secret

# --- Email (Resend), LLM keys, secrets: copy as-is from Railway ---
# EMAIL_BACKEND / SMTP_* (Resend), ANTHROPIC_API_KEY, GEMINI_API_KEY,
# APP_SECRET_KEY, INTERNAL_API_TOKEN, DASHBOARD_BASE_URL, PLATFORM_DOMAIN, ...
```

> Keep secrets out of git. For now a `600`-permission `.env.prod` on the box is fine;
> graduate to **SSM Parameter Store** later (pull at boot with `aws ssm get-parameters`).

---

## Phase 6 — Run migrations, then start both services

Create `docker-compose.prod.yml` at the repo root (API + worker only — no local Postgres/MinIO):

```yaml
services:
  api:
    build: ./backend
    env_file: backend/.env.prod
    command: >
      uvicorn app.main:app --host 0.0.0.0 --port 8000
      --proxy-headers --forwarded-allow-ips=*
    restart: on-failure
    ports: ["127.0.0.1:8000:8000"]   # fronted by Caddy/TLS below

  worker:
    build: ./backend
    env_file: backend/.env.prod
    command: python -m app.cli worker
    restart: always
```

Build, migrate (as the owner role — this is your Railway `preDeployCommand`), then start:

```bash
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml run --rm api python -m app.cli migrate
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f --tail=50
```

Confirm health: `curl -s localhost:8000/healthz` should return OK, and the worker log should
show it polling (not a refusal message).

---

## Phase 7 — TLS + wire up the front door

Vercel proxies `/api/*` and `/sites/*` to your backend over **HTTPS**, so the box needs a cert.

**Decision — how TLS is terminated.** Your app has built-in ACME/TLS settings (`TLS_MODE`,
`ACME_CA_DOMAINS`, `EDGE_PROBE_ADDRESS`). Check your current Railway `TLS_MODE`:

- If the app terminates its own TLS on 443, run the API container on `443:443` directly and
  point `ACME_CA_DOMAINS` at your backend hostname.
- Otherwise (simplest, recommended), put **Caddy** in front for automatic HTTPS:

```yaml
  caddy:
    image: caddy:2
    restart: always
    ports: ["80:80", "443:443"]
    volumes: [caddy_data:/data, ./Caddyfile:/etc/caddy/Caddyfile:ro]
volumes: { caddy_data: {} }
```

`Caddyfile`:

```
api.yourdomain.com {
    reverse_proxy api:8000
}
```

Then:
1. Create a DNS `A` record `api.yourdomain.com → ELASTIC_IP`.
2. Update **Vercel** `vercel.json` rewrites: change the `/api/*` and `/sites/*` destinations
   from the old Railway/Vercel backend URL to `https://api.yourdomain.com/...`. Redeploy Vercel.
3. Update `DASHBOARD_BASE_URL` / `PLATFORM_DOMAIN` env if they referenced the old host.

---

## Phase 8 — Verify, cut over, and keep a rollback

**Verify** on the new stack:
- `curl https://api.yourdomain.com/healthz`
- Log in through the Vercel frontend; confirm it reaches the API.
- Upload a test PDF → confirm the **worker** processes it (watch logs) and the result lands in R2.
- Confirm an email (magic link / invite) sends via Resend.

**Cut over:** do Phase 3 (data dump/restore) during a short window so no rows are missed, then
flip Vercel's rewrites to the new backend.

**Rollback:** keep the Railway services **up but idle** for a few days. If something breaks,
revert the Vercel `vercel.json` to the Railway backend URL and redeploy — instant fallback.
Decommission Railway only after the AWS stack has run clean for a few days.

---

## After it's stable — the scale ladder (no rewrite)

1. **Vertical:** resize EC2 `t4g.small → medium → large`; RDS `micro → small`.
2. **Split the worker** onto its own EC2, or move both services to **ECS Fargate** (same image).
3. **Front door:** add **ALB + CloudFront + ACM** when you want autoscaling + CDN.
4. **Horizontal:** Fargate autoscaling on job-queue depth; RDS read replica / Aurora.

Your Postgres-polling job queue carries through all of it unchanged.
