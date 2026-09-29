#!/usr/bin/env bash
# Project-local PostgreSQL cluster for development and tests.
#   scripts/dev-db.sh init    create cluster, roles, dev + test databases
#   scripts/dev-db.sh start | stop | status | psql
# Data lives in .data/pg (gitignored). Listens on localhost:${PGPORT:-54329} only.
set -euo pipefail
export LC_ALL="${LC_ALL:-en_US.UTF-8}"  # macOS: postmaster aborts ("became multithreaded") without it
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PGDATA="$ROOT/.data/pg"
PORT="${PGPORT:-54329}"
PGBIN="${PGBIN:-$(brew --prefix postgresql@16 2>/dev/null)/bin}"
OWNER_PW="${DEV_DB_OWNER_PASSWORD:-owner_dev_pw}"
APP_PW="${DEV_DB_APP_PASSWORD:-app_dev_pw}"

pgctl() { "$PGBIN/pg_ctl" -D "$PGDATA" -l "$PGDATA/server.log" -o "-p $PORT -k $PGDATA -c listen_addresses=localhost" "$@"; }
# Superuser only over the Unix socket inside .data/pg; TCP requires passwords (scram).
sql() { "$PGBIN/psql" -h "$PGDATA" -p "$PORT" -U postgres -v ON_ERROR_STOP=1 -qAt "$@"; }

case "${1:-}" in
  init)
    if [ ! -f "$PGDATA/PG_VERSION" ]; then
      mkdir -p "$PGDATA"
      "$PGBIN/initdb" -D "$PGDATA" -U postgres --auth-local=trust --auth-host=scram-sha-256 --encoding=UTF8 --locale=C >/dev/null
    fi
    pgctl status >/dev/null 2>&1 || pgctl start -w >/dev/null
    sql -d postgres <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'publishpdf_owner') THEN
    CREATE ROLE publishpdf_owner LOGIN PASSWORD '$OWNER_PW' CREATEDB;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'publishpdf_app') THEN
    CREATE ROLE publishpdf_app LOGIN PASSWORD '$APP_PW' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END \$\$;
SQL
    for db in publishpdf publishpdf_test; do
      [ "$(sql -d postgres -c "SELECT 1 FROM pg_database WHERE datname='$db'")" = "1" ] || \
        sql -d postgres -c "CREATE DATABASE $db OWNER publishpdf_owner"
    done
    echo "Postgres ready on localhost:$PORT (databases: publishpdf, publishpdf_test)"
    ;;
  start) if pgctl status >/dev/null 2>&1; then echo "Postgres is already running on localhost:$PORT"; else pgctl start -w; fi ;;
  stop) pgctl stop ;;
  status) pgctl status ;;
  psql) shift; "$PGBIN/psql" -h localhost -p "$PORT" -U publishpdf_owner publishpdf "$@" ;;
  *) echo "usage: $0 init|start|stop|status|psql"; exit 1 ;;
esac
