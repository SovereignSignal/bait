#!/usr/bin/env bash
# Run the pipeline locally against the bundled hooklist fixture.
# Loads ./.env (if present; a variable you set on the command line wins over it, as with python-dotenv), then:
# python -m bait run --hooklist-file fixtures/hooklist.sample.json [extra args]
# With DATABASE_URL unset the pipeline uses sqlite:///./bait.sqlite inside pipeline/ and creates tables itself.
# Examples:
#   scripts/run-pipeline-local.sh                                   # all steps, today UTC
#   scripts/run-pipeline-local.sh --steps ingest,snapshot --day 2026-10-06
#   scripts/run-pipeline-local.sh --steps ingest --database-url sqlite:///./dev.sqlite
#   KEYLESS=0 scripts/run-pipeline-local.sh                         # no GeckoTerminal / public RPC calls
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  caller_env=$(export -p)
  set -a; . ./.env; set +a
  eval "$caller_env" || true   # put back what the caller exported: KEYLESS=0 here beats KEYLESS= in .env
fi

PY=pipeline/.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "no venv at pipeline/.venv. Create it:" >&2
  echo "  python3 -m venv pipeline/.venv && pipeline/.venv/bin/pip install -r pipeline/requirements.txt" >&2
  exit 1
fi

cd pipeline
exec "../$PY" -m bait run --hooklist-file fixtures/hooklist.sample.json "$@"
