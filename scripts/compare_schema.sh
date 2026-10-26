#!/bin/bash
# Compare the schemas of two PostgreSQL databases.
#
# Usage: scripts/compare_schema.sh URL_A URL_B
#
# URLs are libpq connection URIs (postgresql://user[:password]@host:port/dbname). Leave the
# password out to use PGPASSWORD or ~/.pgpass instead. Both are dumped schema-only with the
# PostgreSQL 17 client from the postgres:17 image, over the host network, in read-only sessions.
# Prints a unified diff and exits 1 when the schemas differ, 0 when they match.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 URL_A URL_B" >&2
  exit 2
fi

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

dump() {
  docker run --rm --network host \
    -e PGPASSWORD -e PGOPTIONS="-c default_transaction_read_only=on" \
    postgres:17 pg_dump --schema-only --no-owner --no-privileges "$1" \
    | grep -v -E '^(--|SET |SELECT pg_catalog\.set_config\(.search_path.|\\restrict |\\unrestrict )' \
    | grep -v -E '^[[:space:]]*$'
}

dump "$1" > "$workdir/a.sql"
dump "$2" > "$workdir/b.sql"
diff -u "$workdir/a.sql" "$workdir/b.sql"
