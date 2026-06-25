#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/runtime/postgres/compose.pgvector.yml"
CONTAINER_NAME="pi-code-index-postgres"
POSTGRES_USER="${PI_CODE_INDEX_POSTGRES_USER:-cocoindex}"
POSTGRES_DB="${PI_CODE_INDEX_POSTGRES_DB:-cocoindex}"

if ! command -v podman >/dev/null 2>&1; then
  echo "Missing required command: podman" >&2
  exit 1
fi

compose_up() {
  if podman compose version >/dev/null 2>&1; then
    podman compose -f "$COMPOSE_FILE" up -d
    return 0
  fi
  if command -v podman-compose >/dev/null 2>&1; then
    podman-compose -f "$COMPOSE_FILE" up -d
    return 0
  fi
  return 1
}

if podman container exists "$CONTAINER_NAME"; then
  podman start "$CONTAINER_NAME" >/dev/null
else
  if ! compose_up; then
    podman run -d \
      --name "$CONTAINER_NAME" \
      -e POSTGRES_USER="$POSTGRES_USER" \
      -e POSTGRES_PASSWORD="${PI_CODE_INDEX_POSTGRES_PASSWORD:-cocoindex}" \
      -e POSTGRES_DB="$POSTGRES_DB" \
      -p "${PI_CODE_INDEX_POSTGRES_PORT:-5432}:5432" \
      pgvector/pgvector:pg17 >/dev/null
  fi
fi

printf 'Waiting for %s to accept connections' "$CONTAINER_NAME"
for _ in $(seq 1 60); do
  if podman exec "$CONTAINER_NAME" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
    printf '\n'
    podman exec "$CONTAINER_NAME" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c 'CREATE EXTENSION IF NOT EXISTS vector;' -c '\dx vector'
    cat <<EOF

Postgres + pgvector is ready.
Export this for CocoIndex/pi-code-index:
  export PI_CODE_INDEX_POSTGRES_URL=postgres://$POSTGRES_USER:${PI_CODE_INDEX_POSTGRES_PASSWORD:-cocoindex}@localhost:${PI_CODE_INDEX_POSTGRES_PORT:-5432}/$POSTGRES_DB
  export PI_CODE_INDEX_BACKEND=cocoindex
Compatibility fallback:
  export POSTGRES_URL=postgres://$POSTGRES_USER:${PI_CODE_INDEX_POSTGRES_PASSWORD:-cocoindex}@localhost:${PI_CODE_INDEX_POSTGRES_PORT:-5432}/$POSTGRES_DB
EOF
    exit 0
  fi
  printf '.'
  sleep 1
done
printf '\nTimed out waiting for %s\n' "$CONTAINER_NAME" >&2
podman logs "$CONTAINER_NAME" >&2 || true
exit 1
