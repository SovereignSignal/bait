#!/usr/bin/env bash
# Run the pipeline locally against the bundled hooklist fixture.
# Loads ./.env (if present), then: python -m hookfeed run --hooklist-file fixtures/hooklist.sample.json [extra args]
# With DATABASE_URL unset the pipeline uses sqlite:///./hookfeed.sqlite inside pipeline/ and creates tables itself.
# Examples:
#   scripts/run-pipeline-local.sh                                   # all steps, today UTC
#   scripts/run-pipeline-local.sh --steps ingest,snapshot --day 2026-10-06
#   scripts/run-pipeline-local.sh --steps ingest --database-url sqlite:///./dev.sqlite
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

PY=pipeline/.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "no venv at pipeline/.venv. Create it:" >&2
  echo "  python3 -m venv pipeline/.venv && pipeline/.venv/bin/pip install -r pipeline/requirements.txt" >&2
  exit 1
fi

cd pipeline
exec "../$PY" -m hookfeed run --hooklist-file fixtures/hooklist.sample.json "$@"
