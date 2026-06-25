# PSQL-first runtime layout plan

## Decision

`pi-code-index` should treat local PostgreSQL/pgvector as first-class managed runtime infrastructure, not as an example. The lexical JSON backend remains a supported degraded mode, but the intended full-capability path is:

```text
Pi tools -> index.ts -> pi-code-index CLI -> daemon -> backend router -> CocoIndex -> Postgres/pgvector
```

Keeping compose/start assets under `examples/` is insufficient because semantic search, symbol navigation, call graph, impact analysis, repo maps, likely tests, similar-code ranking, and review context all depend on CocoIndex/Postgres for their high-precision behavior. `examples/` should only contain sample config snippets and compatibility wrappers, not the canonical runtime entrypoint.

## Target directory layout

Implement in the smallest useful move:

```text
runtime/
  postgres/
    compose.pgvector.yml        # canonical Podman Compose service
    podman-pgvector.sh          # canonical start/check helper
    init/01-vector.sql          # pgvector bootstrap SQL

docs/
  postgres-runtime.md           # user-facing setup/lifecycle reference
  architecture/psql-first-runtime-layout-plan.md

examples/
  global-config.yml             # sample config only
  project-settings.yml          # sample project settings only
  compose.pgvector.yml          # temporary shim or pointer to runtime/postgres
  podman-pgvector.sh            # temporary shim or pointer to runtime/postgres
  postgres-init/                # temporary shim or removed after migration window

src/pi_code_index/
  config.py                     # config hierarchy and defaults
  backend.py                    # backend choice and degraded fallback semantics
  setup_checks.py               # doctor/status checks and suggested commands
  cli.py                        # lifecycle command surface
  daemon.py                     # daemon/resource lifecycle
  coco_backend.py               # Postgres/pgvector schema and query implementation
```

Do not add a new top-level service framework. The runtime assets are repo-owned development/runtime infrastructure, and Python modules keep their current responsibilities.

## Ownership boundaries

- `runtime/postgres/*`: owns the canonical Podman Compose file, container name, volume name, init SQL, and helper script for starting and validating pgvector.
- `scripts/setup.sh`: validates dependencies and can point to or invoke the canonical runtime path for `--postgres-check`; it should not own database lifecycle.
- `src/pi_code_index/config.py`: owns config precedence, defaults, and env names. It should expose Postgres config consistently but not shell out to Podman.
- `src/pi_code_index/backend.py`: owns `auto`/`lexical`/`cocoindex` selection and JSON-safe degraded fallback behavior.
- `src/pi_code_index/setup_checks.py`: owns lightweight doctor checks and next-step guidance; deeper DB checks may call through existing backend/status helpers.
- `src/pi_code_index/cli.py`: owns user lifecycle commands. It may add thin `postgres` or `runtime` subcommands later, but should delegate to runtime assets rather than duplicate compose logic.
- `src/pi_code_index/daemon.py`: owns Unix socket lifecycle and warm CocoIndex/Postgres resources after a backend is selected; it does not start containers.
- `src/pi_code_index/coco_backend.py`: owns pgvector extension use, schemas/tables, indexes, and queries.
- `README.md` and `docs/postgres-runtime.md`: own user-facing setup, lifecycle, and fallback explanation.
- `examples/`: owns sample config and migration shims only.

## Config hierarchy

Keep existing precedence:

```text
environment variables -> project .pi-code-index/settings.yml -> ~/.pi-code-index/config.yml -> defaults
```

PSQL-first clarification:

- `PI_CODE_INDEX_BACKEND=cocoindex` means Postgres is required; failures are errors with direct setup guidance.
- `PI_CODE_INDEX_BACKEND=auto` means use CocoIndex/Postgres when `PI_CODE_INDEX_POSTGRES_URL` or `POSTGRES_URL` is configured; otherwise use lexical degraded mode.
- `PI_CODE_INDEX_POSTGRES_URL` is the pi-code-index-specific override and should be documented before generic `POSTGRES_URL`.
- `COCOINDEX_DATABASE_URL` remains an internal compatibility/export detail for CocoIndex when needed, not the primary user-facing setting.
- Container knobs stay environment-based for runtime assets: `PI_CODE_INDEX_POSTGRES_USER`, `PI_CODE_INDEX_POSTGRES_PASSWORD`, `PI_CODE_INDEX_POSTGRES_DB`, and `PI_CODE_INDEX_POSTGRES_PORT`.

## Lifecycle command strategy

Step 2 should move the current helpers without expanding scope:

```bash
runtime/postgres/podman-pgvector.sh
podman compose -f runtime/postgres/compose.pgvector.yml up -d
scripts/setup.sh --with-cocoindex --postgres-check
pi-code-index doctor --json
pi-code-index status --json
```

Possible later CLI additions should be thin aliases only, for example `pi-code-index postgres start|status|stop|env`, and should call/reuse the canonical runtime assets. Avoid implementing a second compose engine in Python unless tests prove a user-facing CLI wrapper is needed.

## Fallback semantics

- Lexical fallback is deliberate degraded mode, not the primary architecture.
- `backend: auto` without a configured Postgres URL should report `backend=lexical`, `requested_backend=auto`, and capabilities that clearly mark missing symbols/graph/high-precision context.
- `backend: auto` with a configured Postgres URL may fall back to lexical only when CocoIndex/Postgres is unavailable, and must include warning fields.
- `backend: cocoindex` must fail loudly when Postgres/CocoIndex is unavailable; it should not silently degrade.
- Symbol, graph, and impact tools may return empty lexical-compatible payloads, but warnings must say the feature requires CocoIndex/Postgres.

## Migration path from `examples/`

1. Create `runtime/postgres/` with the current compose file, helper script, and init SQL.
2. Update internal references in `README.md` and `scripts/setup.sh` from `examples/...` to `runtime/postgres/...`.
3. Leave `examples/podman-pgvector.sh` as a short compatibility shim that execs `../runtime/postgres/podman-pgvector.sh` for one migration window.
4. Replace or remove `examples/compose.pgvector.yml` with a pointer once no tests/docs require it directly.
5. Keep `examples/global-config.yml` and `examples/project-settings.yml` as sample configs; remove database lifecycle language from examples.
6. Add tests or validation commands proving old entrypoints still guide users to the canonical path.

## Risks

- Broken compose-relative init path after moving files. Mitigation: keep `init/` beside the compose file and validate with Podman.
- User docs still imply Postgres is optional demo infra. Mitigation: update README/setup docs in the same implementation step.
- Duplicate lifecycle logic between shell scripts and Python CLI. Mitigation: one canonical script; future CLI aliases delegate.
- Silent capability loss in `auto` mode. Mitigation: status/doctor warnings and capability fields must make lexical degraded mode explicit.
- Docker drift. Mitigation: all runtime commands use Podman/Podman Compose only.
- Port/container conflicts with existing local Postgres. Mitigation: keep `PI_CODE_INDEX_POSTGRES_PORT` and stable container name documented.

## Validation commands

Run after implementation moves assets:

```bash
cd ~/.pi/agent/extensions/pi-code-index
uv run python -m compileall src tests
uv run --extra dev pytest
npm run typecheck
npm run test:ts
bash -n runtime/postgres/podman-pgvector.sh
podman compose -f runtime/postgres/compose.pgvector.yml config
runtime/postgres/podman-pgvector.sh
scripts/setup.sh --with-cocoindex --postgres-check --skip-tests
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex uv run --extra cocoindex pi-code-index doctor --json
```

If Podman is unavailable in CI, keep the shell/compose config checks and run the live Podman checks manually before closing implementation.

## Acceptance criteria for spec/implementation

- Architecture/spec documents identify `runtime/postgres/` as the canonical Postgres/pgvector runtime location.
- `examples/` no longer appears to own database lifecycle; any remaining files are samples or compatibility shims.
- `README.md`, setup output, doctor/status guidance, and validation snippets reference the canonical runtime path.
- Config docs state the env/project/global precedence and clearly separate `auto`, `lexical`, and `cocoindex` behavior.
- Fallback behavior is explicit: lexical is degraded, CocoIndex/Postgres is required for semantic/symbol/graph/impact/high-precision context.
- Podman compose startup, pgvector extension creation, setup check, `doctor`, `status`, `refresh`, and representative search/context commands have documented validation coverage.
- No Docker commands are introduced.
