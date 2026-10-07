#!/usr/bin/env bash
# Start the whole platform locally: Postgres, API, worker, public sites, dashboard.
#   scripts/dev.sh            Ctrl+C stops everything
# Dashboard  http://localhost:5173     (sign-in link is shown on the page in development)
# Sites      http://<tenant-slug>.preview.localhost:8080
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
"$ROOT/scripts/dev-db.sh" init >/dev/null
(cd "$ROOT/backend" && uv sync -q)
# Pointed at the live database (a remote DATABASE_*_URL in .env), this laptop neither runs
# migrations there nor processes live jobs: the deployed worker does both.
if (cd "$ROOT/backend" && uv run python -c "import sys; from app.cli import laptop_on_live_database as f; sys.exit(0 if f() else 1)"); then
  LIVE_DB=1
  echo "Using the LIVE database from .env: no migrations and no local worker (production's worker processes jobs)."
else
  LIVE_DB=0
  (cd "$ROOT/backend" && uv run python -m app.cli migrate)
fi
(cd "$ROOT/frontend" && [ -d node_modules ] || npm install --silent)

pids=()
run() { local name=$1; shift; ( cd "$ROOT/$1" && shift && exec "$@" ) 2>&1 | sed -u "s/^/[$name] /" & pids+=($!); }
cleanup() { kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

run api      backend  uv run uvicorn app.main:app --reload --port 8000
# The worker restarts itself whenever backend code changes (the API does the same via --reload).
# If the worker ever exits (a crash, a refused start), it comes back after 2 s instead of
# staying down until the next file change.
# A refused start (exit 2: live database) is said once, not retried every 2 s.
if [ "$LIVE_DB" = 0 ]; then
  run worker backend uv run watchfiles --filter python "sh -c 'while true; do python -m app.cli worker; [ \$? -eq 2 ] && exec sleep 2147483647; echo worker exited, restarting; sleep 2; done'" app
fi
run sites    backend  uv run uvicorn app.public:app --reload --port 8080
run web      frontend npm run dev -- --strictPort
echo "PublishPDF is starting: dashboard http://localhost:5173 · sites http://<slug>.preview.localhost:8080"
wait
