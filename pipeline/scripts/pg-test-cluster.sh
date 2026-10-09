#!/usr/bin/env bash
# Throwaway Postgres for the integration tests. Usage: scripts/pg-test-cluster.sh start|stop
# Prints the TEST_DATABASE_URL to export. Needs pg_ctl/initdb on PATH (Postgres 15+).
set -euo pipefail
TMP="${TMPDIR:-/tmp}"
DIR="${PG_TEST_DIR:-${TMP%/}/bait-pg-test}"
PORT="${PG_TEST_PORT:-5499}"
case "${1:-}" in
  start)
    if [ ! -d "$DIR" ]; then initdb -D "$DIR" -A trust -U postgres >/dev/null; fi
    pg_ctl -D "$DIR" -o "-p $PORT -k $DIR -c listen_addresses=" -l "$DIR/log" start >/dev/null
    # single-quoted so eval does not treat the & as a background operator
    echo "export TEST_DATABASE_URL='postgresql+psycopg://postgres@/postgres?host=$DIR&port=$PORT'"
    ;;
  stop)
    pg_ctl -D "$DIR" stop >/dev/null && rm -rf "$DIR"
    ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac
