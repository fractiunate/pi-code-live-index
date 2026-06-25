# PSQL-first runtime layout spec

## Decision

PostgreSQL/pgvector is first-class local runtime infrastructure for `pi-code-index`. The canonical runtime assets move out of `examples/` into `runtime/postgres/`. Lexical JSON indexing remains supported only as explicit degraded mode for no-Postgres or fallback scenarios.

Intended full-capability path:

```text
Pi tools -> index.ts -> pi-code-index CLI -> daemon -> backend router -> CocoIndex -> Postgres/pgvector
```

## Target paths

Create these canonical runtime paths:

```text
runtime/postgres/compose.pgvector.yml
runtime/postgres/podman-pgvector.sh
runtime/postgres/init/01-vector.sql
docs/postgres-runtime.md
docs/architecture/psql-first-runtime-layout-spec.md
```

Keep these sample/config paths:

```text
examples/global-config.yml
examples/project-settings.yml
```

Keep these compatibility paths for one migration window:

```text
examples/podman-pgvector.sh
examples/compose.pgvector.yml
examples/postgres-init/01-vector.sql
```

## File moves and delegation

| Current path | Target behavior |
| --- | --- |
| `examples/compose.pgvector.yml` | Move canonical content to `runtime/postgres/compose.pgvector.yml`. The init volume must become `./init:/docker-entrypoint-initdb.d:ro,Z` because compose resolves relative to `runtime/postgres/`. |
| `examples/postgres-init/01-vector.sql` | Move canonical SQL to `runtime/postgres/init/01-vector.sql`. Keep a compatibility copy or documented pointer under `examples/postgres-init/01-vector.sql` only if old compose still needs it. |
| `examples/podman-pgvector.sh` | Replace with a short shim that prints a deprecation notice to stderr and `exec`s `../runtime/postgres/podman-pgvector.sh "$@"`. |
| `scripts/setup.sh` | Update `--postgres-check` messages to reference `runtime/postgres/podman-pgvector.sh` and `runtime/postgres/compose.pgvector.yml`. It may validate the container; it must not own lifecycle/start logic. |
| `README.md` | Replace examples-owned Postgres setup with canonical runtime setup and link to `docs/postgres-runtime.md`. |
| `examples/global-config.yml` | Remains sample config only. Update comments to prefer `PI_CODE_INDEX_POSTGRES_URL` over generic `POSTGRES_URL`. |
| `examples/project-settings.yml` | Remains project sample only. No database lifecycle language. |

Compatibility shim contract:

- `examples/podman-pgvector.sh` must be executable and preserve all args.
- It must work when invoked from any cwd.
- It must not duplicate Podman logic.
- It should say: `examples/podman-pgvector.sh is deprecated; use runtime/postgres/podman-pgvector.sh`.

`examples/compose.pgvector.yml` can remain as a compatibility copy or pointer for this step, but docs and setup output must stop presenting it as canonical.

## Ownership boundaries

- `runtime/postgres/*`: owns Podman Compose service, image, container name, volume name, init SQL mount, start/check helper, and exported URL hint.
- `scripts/setup.sh`: owns dependency validation and optional container health validation only.
- `src/pi_code_index/config.py`: owns config/env precedence, defaults, validation, and Postgres URL resolution. It must not shell out to Podman.
- `src/pi_code_index/backend.py`: owns `auto`/`lexical`/`cocoindex` routing and JSON-safe degraded fallback.
- `src/pi_code_index/setup_checks.py`: owns doctor/status checks and suggested commands.
- `src/pi_code_index/cli.py`: owns user command surface and status/doctor payload shape. Any future runtime command must delegate to `runtime/postgres/podman-pgvector.sh`.
- `src/pi_code_index/daemon.py`: owns daemon/socket/resource lifecycle after backend selection. It must not start Postgres containers.
- `src/pi_code_index/coco_backend.py`: owns pgvector extension use, schema/tables/indexes, refresh/search/symbol/graph/query behavior.
- `README.md` and `docs/postgres-runtime.md`: own user-facing setup, lifecycle, fallback explanation, and migration notes.
- `examples/`: owns samples and temporary compatibility shims only.

## Config and environment contract

Precedence stays unchanged:

```text
environment variables -> project .pi-code-index/settings.yml -> ~/.pi-code-index/config.yml -> defaults
```

User-facing variables:

| Variable | Contract |
| --- | --- |
| `PI_CODE_INDEX_BACKEND` | `auto`, `lexical`, or `cocoindex`. |
| `PI_CODE_INDEX_POSTGRES_URL` | Preferred pi-code-index Postgres URL override. Document before `POSTGRES_URL`. |
| `POSTGRES_URL` | Generic compatibility URL used when the pi-specific variable is absent. |
| `COCOINDEX_DATABASE_URL` | Internal compatibility/export detail for CocoIndex; not the primary user setting. |
| `PI_CODE_INDEX_POSTGRES_USER` | Runtime container user; default `cocoindex`. |
| `PI_CODE_INDEX_POSTGRES_PASSWORD` | Runtime container password; default `cocoindex`. |
| `PI_CODE_INDEX_POSTGRES_DB` | Runtime container database; default `cocoindex`. |
| `PI_CODE_INDEX_POSTGRES_PORT` | Host port; default `5432`. |

Backend contract:

- `PI_CODE_INDEX_BACKEND=cocoindex`: Postgres/CocoIndex is required. Do not silently degrade.
- `PI_CODE_INDEX_BACKEND=auto` with `PI_CODE_INDEX_POSTGRES_URL` or `POSTGRES_URL`: attempt CocoIndex/Postgres first; lexical fallback is allowed only with warning fields.
- `PI_CODE_INDEX_BACKEND=auto` without a configured Postgres URL: use lexical degraded mode and report it clearly.
- `PI_CODE_INDEX_BACKEND=lexical`: use local JSON lexical backend regardless of Postgres URL.

## CLI, status, and doctor contract

Canonical lifecycle commands for this implementation:

```bash
runtime/postgres/podman-pgvector.sh
podman compose -f runtime/postgres/compose.pgvector.yml up -d
scripts/setup.sh --with-cocoindex --postgres-check
pi-code-index doctor --json
pi-code-index status --json
```

Required user-visible references:

- README and docs must show `runtime/postgres/podman-pgvector.sh`, not `examples/podman-pgvector.sh`.
- `scripts/setup.sh --postgres-check` failure messages must suggest `runtime/postgres/podman-pgvector.sh`.
- `setup_checks.py` suggested commands for Postgres checks must reference `scripts/setup.sh --with-cocoindex --postgres-check`; if a direct start command is included, it must be `runtime/postgres/podman-pgvector.sh`.
- `doctor --json` and `status --json` should keep current shapes, but Postgres-related checks/messages must not imply examples-owned infrastructure.
- No new CLI subcommands are required for this spec. Future `pi-code-index postgres start|status|stop|env` commands are allowed only as thin delegates to runtime assets.

Status/fallback fields to preserve or add where missing:

```text
backend
requested_backend
backend_fallback
warnings[] or warning
capabilities
```

Lexical payload capabilities must continue to mark missing symbols/references/graph/high-precision semantic behavior.

## Fallback semantics

- Lexical is intentional degraded mode, not the primary architecture.
- `auto` without Postgres URL reports lexical, `requested_backend=auto`, `backend_fallback=false`, and degraded capabilities.
- `auto` with Postgres URL may fall back to lexical on CocoIndex/Postgres failure, with `backend_fallback=true` and warnings describing the failure.
- `cocoindex` failure returns `ok=false` with setup guidance; no silent lexical fallback.
- Symbol, graph, and impact tools may return empty lexical-compatible payloads, but warnings must say the feature requires CocoIndex/Postgres.

## Migration notes

1. Add `runtime/postgres/` with moved compose, helper, and init SQL.
2. Adjust compose init mount to `./init`.
3. Update README, `docs/postgres-runtime.md`, `scripts/setup.sh`, and setup/doctor text to use canonical paths.
4. Replace `examples/podman-pgvector.sh` with the delegating shim.
5. Keep config examples in `examples/`; remove lifecycle ownership wording from examples.
6. Validate old script path still delegates successfully.
7. Do not delete compatibility files until a later issue explicitly removes them.

## Validation commands

After implementation, run from repo root:

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

If Podman is unavailable in CI, run the shell syntax, compose config, unit/type tests, and document live Podman validation as manual evidence before closing implementation.

## Non-goals

- Do not implement file moves in this spec-only issue.
- Do not add a Python service manager for Postgres.
- Do not add Docker commands or Docker Compose references.
- Do not remove lexical backend support.
- Do not remove examples compatibility paths in the first migration window.
- Do not redesign CocoIndex schemas, query ranking, daemon protocol, or Pi tool names.
- Do not introduce new top-level runtime framework beyond `runtime/postgres/`.
