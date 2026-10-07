#!/usr/bin/env bash
# Apply db/migrations/*.sql in filename order against $DATABASE_URL with psql.
# Idempotent: a schema_migrations table records each applied file; re-runs skip them.
# Usage: DATABASE_URL=postgres://... scripts/apply-migrations.sh
#        scripts/apply-migrations.sh            (reads DATABASE_URL from ./.env if unset)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${DATABASE_URL:-}" ] && [ -f .env ]; then
  set -a; . ./.env; set +a
fi
: "${DATABASE_URL:?DATABASE_URL is not set (Railway: Postgres service > Variables > DATABASE_PUBLIC_URL for off-platform psql)}"
command -v psql >/dev/null || { echo "psql not found; install postgresql-client" >&2; exit 1; }

PSQL=(psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -q -X)

"${PSQL[@]}" -c "CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);"

applied=0; skipped=0
for f in $(LC_ALL=C ls db/migrations/*.sql | sort); do
  name=$(basename "$f")
  if [ "$("${PSQL[@]}" -tA -c "SELECT 1 FROM schema_migrations WHERE filename = '$name'")" = "1" ]; then
    echo "skip   $name"; skipped=$((skipped + 1)); continue
  fi
  echo "apply  $name"
  # The migration files are themselves IF NOT EXISTS, so a crash between these two
  # statements is harmless: the next run re-applies and then records it.
  "${PSQL[@]}" -f "$f"
  "${PSQL[@]}" -c "INSERT INTO schema_migrations (filename) VALUES ('$name')"
  applied=$((applied + 1))
done
echo "done: applied=$applied skipped=$skipped"
