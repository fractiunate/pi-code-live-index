#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "examples/podman-pgvector.sh is deprecated; use runtime/postgres/podman-pgvector.sh" >&2
exec "$REPO_ROOT/runtime/postgres/podman-pgvector.sh" "$@"
