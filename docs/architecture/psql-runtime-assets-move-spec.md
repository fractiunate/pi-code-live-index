# PSQL runtime assets move spec

## Scope

This spec formalizes the step-2 implementation that promotes the Podman/Postgres runtime assets from `examples/` into canonical project runtime infrastructure under `runtime/postgres/`.

This is a spec-only document. Do not perform the moves in this issue.

## Current files inspected

- `examples/compose.pgvector.yml`
- `examples/postgres-init/01-vector.sql`
- `examples/podman-pgvector.sh`
- `scripts/setup.sh`
- `README.md`
- `docs/postgres-runtime.md`
- `docs/architecture/psql-first-runtime-layout-spec.md`
- `docs/architecture/psql-runtime-assets-move-plan.md`
- `examples/global-config.yml`

## Target ownership

| Path | Ownership after implementation |
| --- | --- |
| `runtime/postgres/compose.pgvector.yml` | Canonical Podman Compose service definition for local Postgres/pgvector. |
| `runtime/postgres/init/01-vector.sql` | Canonical bootstrap SQL for pgvector extension creation. |
| `runtime/postgres/podman-pgvector.sh` | Canonical start/check helper and URL guidance. |
| `examples/podman-pgvector.sh` | Temporary executable compatibility shim only. |
| `examples/compose.pgvector.yml` | Temporary compatibility compose file only. |
| `examples/postgres-init/01-vector.sql` | Temporary compatibility init SQL used by the examples compose file. |
| `scripts/setup.sh` | Validation and guidance only; no database lifecycle/start logic. |
| `README.md` | Primary user-facing setup and runtime path documentation. |
| `docs/postgres-runtime.md` | Focused lifecycle, migration, and fallback reference. |
| `examples/global-config.yml` | Sample config only; no runtime lifecycle ownership. |

## Exact path mapping

| Current path | New canonical path | Implementation requirement |
| --- | --- | --- |
| `examples/compose.pgvector.yml` | `runtime/postgres/compose.pgvector.yml` | Copy/move current compose content and change only path-sensitive mount(s) needed by the new location. |
| `examples/postgres-init/01-vector.sql` | `runtime/postgres/init/01-vector.sql` | Copy/move canonical SQL content exactly: `CREATE EXTENSION IF NOT EXISTS vector;`. |
| `examples/podman-pgvector.sh` | `runtime/postgres/podman-pgvector.sh` | Copy/move helper and update repo-root and compose-file resolution for the new script path. |

Compatibility files to keep for one migration window:

| Compatibility path | Required behavior |
| --- | --- |
| `examples/podman-pgvector.sh` | Executable shim that prints a deprecation notice to stderr and delegates all args to `runtime/postgres/podman-pgvector.sh`. |
| `examples/compose.pgvector.yml` | Remains runnable by old documented command `podman compose -f examples/compose.pgvector.yml up -d`. Keep old `./postgres-init` mount. |
| `examples/postgres-init/01-vector.sql` | Keep existing SQL while `examples/compose.pgvector.yml` remains runnable. |

Do not add new lifecycle scripts or docs under `examples/`.

## Canonical compose file requirements

Target file: `runtime/postgres/compose.pgvector.yml`

Required content semantics:

- Preserve service name `postgres`.
- Preserve image `pgvector/pgvector:pg17`.
- Preserve container name `pi-code-index-postgres`.
- Preserve env defaults:
  - `PI_CODE_INDEX_POSTGRES_USER` default `cocoindex`
  - `PI_CODE_INDEX_POSTGRES_PASSWORD` default `cocoindex`
  - `PI_CODE_INDEX_POSTGRES_DB` default `cocoindex`
  - `PI_CODE_INDEX_POSTGRES_PORT` default `5432`
- Preserve named data volume `pi-code-index-postgres-data`.
- Preserve healthcheck using `pg_isready`.
- Preserve `restart: unless-stopped`.
- Change init SQL mount from the examples-relative path to the runtime-relative path:

```yaml
volumes:
  - pi-code-index-postgres-data:/var/lib/postgresql/data
  - ./init:/docker-entrypoint-initdb.d:ro,Z
```

Compose relative-path rule:

- `podman compose -f runtime/postgres/compose.pgvector.yml up -d` resolves `./init` relative to `runtime/postgres/`, so `runtime/postgres/init/01-vector.sql` must exist before this command is documented as valid.
- Keep `examples/compose.pgvector.yml` using `./postgres-init:/docker-entrypoint-initdb.d:ro,Z` because that compatibility file is still located in `examples/`.

## Canonical helper script requirements

Target file: `runtime/postgres/podman-pgvector.sh`

Start from the current `examples/podman-pgvector.sh` behavior and preserve:

- `set -euo pipefail`
- Podman requirement check.
- `podman compose` first, `podman-compose` second.
- Existing-container start path using `podman start pi-code-index-postgres`.
- Fallback `podman run` path when compose is unavailable.
- Readiness loop using `pg_isready`.
- Extension validation using `CREATE EXTENSION IF NOT EXISTS vector;` and `\dx vector`.
- All existing env knobs:
  - `PI_CODE_INDEX_POSTGRES_USER`
  - `PI_CODE_INDEX_POSTGRES_PASSWORD`
  - `PI_CODE_INDEX_POSTGRES_DB`
  - `PI_CODE_INDEX_POSTGRES_PORT`
- Container name `pi-code-index-postgres`.
- Exit non-zero on timeout and print container logs when possible.

Required path changes:

```bash
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/runtime/postgres/compose.pgvector.yml"
```

Rationale: from `runtime/postgres/podman-pgvector.sh`, `../..` is the repository root.

Required URL guidance change:

- Prefer the pi-specific URL in output.
- Keep generic `POSTGRES_URL` as compatibility fallback.

The ready message should include at least:

```text
Postgres + pgvector is ready.
Export this for CocoIndex/pi-code-index:
  export PI_CODE_INDEX_POSTGRES_URL=postgres://<user>:<password>@localhost:<port>/<db>
  export PI_CODE_INDEX_BACKEND=cocoindex
Compatibility fallback:
  export POSTGRES_URL=postgres://<user>:<password>@localhost:<port>/<db>
```

Do not add Docker or Docker Compose commands.

## Compatibility shim requirements

Target file: `examples/podman-pgvector.sh`

Replace the old implementation with this exact behavior:

- executable shell script;
- resolves the repository root from the script path, not caller cwd;
- prints a deprecation notice to stderr;
- `exec`s the canonical helper;
- preserves all arguments and exit code;
- contains no duplicate Podman lifecycle logic.

Required shim content:

```bash
#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "examples/podman-pgvector.sh is deprecated; use runtime/postgres/podman-pgvector.sh" >&2
exec "$REPO_ROOT/runtime/postgres/podman-pgvector.sh" "$@"
```

Permissions:

```bash
chmod +x runtime/postgres/podman-pgvector.sh examples/podman-pgvector.sh
```

## `scripts/setup.sh` updates

`--postgres-check` remains validation-only. It must not start, stop, create, or migrate the Postgres container.

Required message changes:

When the container does not exist, replace the current examples guidance with:

```text
Podman is installed, but container 'pi-code-index-postgres' does not exist.
Start it with: runtime/postgres/podman-pgvector.sh
Compose file: runtime/postgres/compose.pgvector.yml
```

When the container exists but is stopped, replace the current examples guidance with:

```text
Container 'pi-code-index-postgres' exists but is not running.
Start it with: podman start pi-code-index-postgres or runtime/postgres/podman-pgvector.sh
```

Keep existing validation behavior:

- check required base commands;
- optionally sync CocoIndex extras;
- run tests unless `--skip-tests` is supplied;
- require `podman` only when `--postgres-check` is supplied;
- check container existence;
- check running state;
- run `CREATE EXTENSION IF NOT EXISTS vector;` and `\dx vector` in the container.

## Documentation updates required in the implementation issue

### `README.md`

Update the active optional CocoIndex/Postgres section:

- Present `runtime/postgres/` as the canonical runtime layout, not a future target.
- Primary start command:

```bash
runtime/postgres/podman-pgvector.sh
```

- Equivalent compose command:

```bash
podman compose -f runtime/postgres/compose.pgvector.yml up -d
# or: podman-compose -f runtime/postgres/compose.pgvector.yml up -d
```

- Say the helper uses `runtime/postgres/compose.pgvector.yml` when compose is available.
- Prefer:

```bash
export PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex
```

- Keep `POSTGRES_URL` documented as a compatibility fallback.
- Keep the degraded-mode explanation: lexical works without Postgres, but semantic/symbol/graph/impact/review-context behavior is full-capability only with CocoIndex/Postgres.
- Update troubleshooting text that currently says to start with `examples/podman-pgvector.sh` or `examples/compose.pgvector.yml`.

### `docs/postgres-runtime.md`

Replace “target layout until migration” wording with canonical wording.

Required sections:

- canonical layout;
- start helper command;
- equivalent compose command;
- setup validation command;
- doctor/status commands;
- env var precedence and preferred URL variable;
- migration notes for old `examples/` entrypoints;
- fallback behavior for `auto`, `cocoindex`, and `lexical`.

Required lifecycle commands to include:

```bash
runtime/postgres/podman-pgvector.sh
podman compose -f runtime/postgres/compose.pgvector.yml up -d
scripts/setup.sh --with-cocoindex --postgres-check
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex uv run --extra cocoindex pi-code-index doctor --json
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex uv run --extra cocoindex pi-code-index status --json
```

### `examples/global-config.yml`

Keep this file as sample config only. Ensure the backend comment says:

```yaml
backend: "auto" # auto|lexical|cocoindex; auto uses cocoindex when PI_CODE_INDEX_POSTGRES_URL or POSTGRES_URL is set, preferring PI_CODE_INDEX_POSTGRES_URL
```

### Historical architecture docs

Do not bulk-edit old architecture plans/specs that are clearly historical. Only update historical docs if they are linked as active setup instructions or would confuse users into using `examples/` as the current canonical runtime.

## Config and runtime behavior contracts

Do not change config precedence:

```text
environment variables -> project .pi-code-index/settings.yml -> ~/.pi-code-index/config.yml -> defaults
```

Do not change backend semantics:

- `PI_CODE_INDEX_BACKEND=cocoindex`: require CocoIndex/Postgres; do not silently degrade.
- `PI_CODE_INDEX_BACKEND=auto` with `PI_CODE_INDEX_POSTGRES_URL` or `POSTGRES_URL`: attempt CocoIndex/Postgres first; lexical fallback may occur only with warnings/fallback fields.
- `PI_CODE_INDEX_BACKEND=auto` without a Postgres URL: use lexical degraded mode and report reduced capabilities.
- `PI_CODE_INDEX_BACKEND=lexical`: use lexical JSON backend even when a Postgres URL is configured.

Do not add a Python service manager or new CLI lifecycle command in this implementation. If future CLI commands are added, they must delegate to `runtime/postgres/podman-pgvector.sh` instead of duplicating Podman logic.

## Validation commands for the implementation issue

Run from repository root after implementing the moves:

```bash
uv run python -m compileall src tests
uv run --extra dev pytest
npm run typecheck
npm run test:ts
bash -n runtime/postgres/podman-pgvector.sh
bash -n examples/podman-pgvector.sh
podman compose -f runtime/postgres/compose.pgvector.yml config
runtime/postgres/podman-pgvector.sh
examples/podman-pgvector.sh
scripts/setup.sh --with-cocoindex --postgres-check --skip-tests
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex uv run --extra cocoindex pi-code-index doctor --json
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex uv run --extra cocoindex pi-code-index status --json
```

If live Podman is unavailable, still run and record:

```bash
uv run python -m compileall src tests
uv run --extra dev pytest
npm run typecheck
npm run test:ts
bash -n runtime/postgres/podman-pgvector.sh
bash -n examples/podman-pgvector.sh
podman compose -f runtime/postgres/compose.pgvector.yml config
```

If `podman compose` is unavailable but `podman` exists, record that compose config validation was skipped due to missing compose provider and run shell syntax/tests. Live start via `runtime/postgres/podman-pgvector.sh` remains manual follow-up evidence.

## Rollout and rollback

Rollout:

1. Add `runtime/postgres/` canonical files.
2. Update compose init mount to `./init` only in the canonical compose file.
3. Replace `examples/podman-pgvector.sh` with the shim.
4. Keep examples compose/init compatibility files runnable.
5. Update setup guidance and docs.
6. Run validation commands and record any Podman-unavailable exceptions.

Rollback:

- Restore canonical content under `examples/` from git if needed.
- Revert docs/setup references to examples paths.
- Existing container `pi-code-index-postgres` and named volume `pi-code-index-postgres-data` need no data migration because names do not change.

## Non-goals

- Do not implement the moves in this spec-only issue.
- Do not remove compatibility files under `examples/` in the first migration window.
- Do not change backend selection or fallback semantics.
- Do not change CocoIndex schema, table names, query ranking, symbol extraction, graph extraction, daemon protocol, Pi tool names, or TypeScript tool schemas.
- Do not add a Python/Postgres service manager.
- Do not add Docker commands or Docker Compose references.
- Do not remove lexical fallback support.
- Do not introduce a generic top-level runtime framework beyond `runtime/postgres/`.
