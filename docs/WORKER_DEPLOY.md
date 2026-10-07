# Deploying the processing worker

The dashboard and API run on Vercel. **Report processing does not**: reading the PDF,
checking every figure (including OCR with Tesseract) and building the web page run in the
*worker*, a long-running process that needs a container. Without a worker, uploads wait
in the queue and the dashboard says "Processing hasn't started yet".

The image is `backend/Dockerfile`. Its default command applies database migrations and
then starts the worker (`python -m app.cli worker`). It needs no open port and no public URL.

## Option A — Railway (config already in the repo)

1. railway.com → New Project → Deploy from GitHub repo → `benfeedgraph/publishpdf`.
2. In the new service → **Settings**:
   - Root Directory: `backend`
   - Config-as-code file path: `backend/railway.worker.json`
   - (No domain/port needed.)
3. **Variables**: add everything in the list below.
4. Deploy. The log should end with the worker starting its job slots.

## Option B — Render

1. render.com → New → **Background Worker** → connect `benfeedgraph/publishpdf`.
2. Runtime: Docker · Root Directory: `backend` · Dockerfile path: `./Dockerfile`.
   Leave the start command empty (the image's default runs migrations, then the worker).
3. Instance: Starter or larger (2 GB RAM recommended for 400-page reports).
4. **Environment**: add everything in the list below. Deploy.

## Variables (copy the values from the Vercel API project)

Required:

| Name | Value |
|---|---|
| `APP_ENV` | `production` |
| `DATABASE_OWNER_URL` | same as Vercel (runs migrations) |
| `DATABASE_APP_URL` | same as Vercel |
| `APP_SECRET_KEY` | same as Vercel — must match, or encrypted data can't be read |
| `PLATFORM_DOMAIN` | `publishpdf.ai` |
| `PLATFORM_TARGET_HOSTNAME` | same as Vercel |
| `PREVIEW_URL_PATTERN` | same as Vercel |
| `VERIFY_PREFIX` | same as Vercel |
| `DASHBOARD_BASE_URL` | `https://publishpdf.ai` |
| `STORAGE_BACKEND` | `s3` |
| `S3_BUCKET`, `S3_REGION`, `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | same as Vercel (Cloudflare R2) |

Recommended:

| Name | Value |
|---|---|
| `GEMINI_API_KEY` | same as Vercel (AI double-check; `AQ.` Vertex keys are detected) |
| `EMAIL_BACKEND`, `EMAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | same as Vercel (invite and notification emails sent by jobs) |
| `KEEP_WARM_URL` | `https://publishpdf.vercel.app/healthz` (keeps the API warm: faster sign-in) |
| `JOB_CONCURRENCY` | `3` (reports processed at once; lower to `2` on a 1 GB instance) |

## Check it's running

```bash
curl -s https://publishpdf.vercel.app/healthz
```

`"queue": {"workers_alive": 3, ...}` (one per job slot) means it's up. Anything waiting in
the queue starts within seconds — nobody needs to re-upload.

## Never

- Don't run the worker from a laptop against the live database. A development machine
  refuses to (`APP_ENV=development`); `ALLOW_REMOTE_WORKER=1` overrides that and is for
  emergencies only.
- Don't push code with a new migration before the worker deploys: the worker is the only
  thing that applies migrations.
