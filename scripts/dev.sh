#!/usr/bin/env bash
# Start the whole platform locally: Postgres, API, worker, public sites, dashboard.
#   scripts/dev.sh            Ctrl+C stops everything
# Dashboard  http://localhost:5173     (sign-in link is shown on the page in development)
# Sites      http://<tenant-slug>.preview.localhost:8080
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
"$ROOT/scripts/dev-db.sh" init >/dev/null
(cd "$ROOT/backend" && uv sync -q && uv run python -m app.cli migrate)
(cd "$ROOT/frontend" && [ -d node_modules ] || npm install --silent)

pids=()
run() { local name=$1; shift; ( cd "$ROOT/$1" && shift && exec "$@" ) 2>&1 | sed -u "s/^/[$name] /" & pids+=($!); }
cleanup() { kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

run api      backend  uv run uvicorn app.main:app --reload --port 8000
run worker   backend  uv run python -m app.cli worker
run sites    backend  uv run uvicorn app.public:app --reload --port 8080
run web      frontend npm run dev -- --strictPort
echo "PublishPDF is starting: dashboard http://localhost:5173 · sites http://<slug>.preview.localhost:8080"
wait
