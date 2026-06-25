#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_COINDEX=0
POSTGRES_CHECK=0
SKIP_TESTS=0

usage() {
  cat <<'USAGE'
Usage: scripts/setup.sh [--with-cocoindex] [--postgres-check] [--skip-tests]

Idempotently prepares and validates pi-code-index for local Pi use.
It does not require root and never uses Docker. Optional Postgres checks use Podman.

Options:
  --with-cocoindex   Install/validate optional CocoIndex + semantic dependencies.
  --postgres-check   Validate Podman and a running pi-code-index-postgres container.
  --skip-tests       Install dependencies and run import/type checks, but skip pytest/TS tests.
USAGE
}

for arg in "$@"; do
  case "$arg" in
    --with-cocoindex) WITH_COINDEX=1 ;;
    --postgres-check) POSTGRES_CHECK=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    echo "Install $1 and re-run this script. No root install is attempted here." >&2
    exit 1
  fi
}

cd "$ROOT_DIR"

step "Checking required tools"
need_cmd node
need_cmd npm
need_cmd uv
node --version
npm --version
uv --version

step "Syncing Python dependencies with uv"
UV_ARGS=(sync --inexact --extra dev)
if [[ "$WITH_COINDEX" -eq 1 ]]; then
  UV_ARGS+=(--extra cocoindex)
fi
uv "${UV_ARGS[@]}"

step "Installing Node dependencies"
npm install

step "Validating Python CLI import"
uv run python -c 'import pi_code_index.cli; print("pi_code_index.cli import ok")'
uv run pi-code-index --help >/dev/null
printf 'pi-code-index CLI ok\n'

step "Type-checking Pi extension"
npm run typecheck

if [[ "$SKIP_TESTS" -eq 0 ]]; then
  step "Compiling Python sources"
  uv run python -m compileall src tests

  step "Running Python tests"
  uv run --extra dev pytest

  step "Running TypeScript tests"
  npm run test:ts
else
  step "Skipping test suites (--skip-tests)"
fi

if [[ "$POSTGRES_CHECK" -eq 1 ]]; then
  step "Checking optional Podman/Postgres/pgvector backend"
  need_cmd podman
  if ! podman container exists pi-code-index-postgres; then
    echo "Podman is installed, but container 'pi-code-index-postgres' does not exist." >&2
    echo "Start it with: runtime/postgres/podman-pgvector.sh" >&2
    echo "Compose file: runtime/postgres/compose.pgvector.yml" >&2
    exit 1
  fi
  if [[ "$(podman inspect -f '{{.State.Running}}' pi-code-index-postgres)" != "true" ]]; then
    echo "Container 'pi-code-index-postgres' exists but is not running." >&2
    echo "Start it with: podman start pi-code-index-postgres or runtime/postgres/podman-pgvector.sh" >&2
    exit 1
  fi
  podman exec pi-code-index-postgres psql -U cocoindex -d cocoindex -c 'CREATE EXTENSION IF NOT EXISTS vector;' -c '\dx vector'
else
  step "Skipping optional Postgres check"
  echo "Skipping optional Postgres check"
  echo "Start Postgres with: runtime/postgres/podman-pgvector.sh"
  echo "Validate later with: scripts/setup.sh --with-cocoindex --postgres-check"
  if command -v podman >/dev/null 2>&1; then
    echo "Podman available. Run '$0 --postgres-check' to validate pgvector."
  else
    echo "Podman not found; only needed for optional CocoIndex/Postgres backend."
  fi
fi

cat <<'DONE'

Setup validation complete.
Next for Pi usage:
  uv tool install -e .
  pi-code-index init --repo /path/to/repo
  pi-code-index --no-daemon search --json --refresh --repo /path/to/repo "where is config loaded"
  # then run /reload in Pi
DONE
