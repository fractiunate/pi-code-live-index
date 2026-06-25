# PSQL runtime assets move plan

## Goal

Promote the existing Podman/Postgres runtime assets from `examples/` into first-class project-owned runtime infrastructure without changing backend behavior yet. This is an implementation plan only; the moves happen in the next issue.

## Exact file moves and delegates

| Current path | New canonical path | Step-2 action |
| --- | --- | --- |
| `examples/compose.pgvector.yml` | `runtime/postgres/compose.pgvector.yml` | Move/copy canonical compose content. Change the init mount from `./postgres-init:/docker-entrypoint-initdb.d:ro,Z` to `./init:/docker-entrypoint-initdb.d:ro,Z` because compose resolves relative to `runtime/postgres/`. |
| `examples/postgres-init/01-vector.sql` | `runtime/postgres/init/01-vector.sql` | Move/copy canonical `CREATE EXTENSION IF NOT EXISTS vector;` bootstrap SQL. |
| `examples/podman-pgvector.sh` | `runtime/postgres/podman-pgvector.sh` | Move canonical helper. Update `ROOT_DIR` resolution for the new script location and set `COMPOSE_FILE="$ROOT_DIR/runtime/postgres/compose.pgvector.yml"`. Keep all current args/env behavior. |

Canonical ownership after the move:

- `runtime/postgres/compose.pgvector.yml`: Podman Compose service, image, container name, volume, env defaults, healthcheck.
- `runtime/postgres/init/01-vector.sql`: pgvector bootstrap SQL.
- `runtime/postgres/podman-pgvector.sh`: start/check helper and exported URL hint.
- `scripts/setup.sh`: validation and guidance only; no database lifecycle logic.
- README and `docs/postgres-runtime.md`: user-facing runtime setup, migration notes, and fallback explanation.

## Compatibility shims

Keep compatibility for one migration window:

- Replace `examples/podman-pgvector.sh` with an executable shim:
  - resolve the repo root from the script path, not the caller cwd;
  - print `examples/podman-pgvector.sh is deprecated; use runtime/postgres/podman-pgvector.sh` to stderr;
  - `exec "$repo_root/runtime/postgres/podman-pgvector.sh" "$@"`.
- Keep `examples/compose.pgvector.yml` as either:
  - a compatibility copy using the old `./postgres-init` mount, if users may still run `podman compose -f examples/compose.pgvector.yml up -d`; or
  - a short pointer/comment-only file if compose compatibility is intentionally dropped later. For this step, prefer a compatibility copy to avoid breaking old documented commands immediately.
- Keep `examples/postgres-init/01-vector.sql` while `examples/compose.pgvector.yml` remains runnable.
- Do not add new lifecycle docs or scripts under `examples/`; examples own samples and shims only.

## Setup script updates

Update `scripts/setup.sh` only where it reports Postgres guidance:

- Missing container message should say:
  - `Start it with: runtime/postgres/podman-pgvector.sh`
  - `Compose file: runtime/postgres/compose.pgvector.yml`
- Existing-but-stopped message should say:
  - `Start it with: podman start pi-code-index-postgres or runtime/postgres/podman-pgvector.sh`
- Leave `--postgres-check` as validation-only. It should continue to check Podman, container existence/running state, and `CREATE EXTENSION IF NOT EXISTS vector;`.

No Python CLI service manager is required. Existing `setup_checks.py` suggested command `scripts/setup.sh --with-cocoindex --postgres-check` can remain; if direct start guidance is added there, use `runtime/postgres/podman-pgvector.sh`.

## README and docs changes

Update first-class user docs in the same implementation step:

- `README.md`
  - Change the optional CocoIndex + pgvector section from examples-owned setup to canonical runtime setup.
  - Show `runtime/postgres/podman-pgvector.sh` as the primary start command.
  - Show `podman compose -f runtime/postgres/compose.pgvector.yml up -d` as the equivalent compose command.
  - Prefer `PI_CODE_INDEX_POSTGRES_URL` in examples; keep `POSTGRES_URL` documented as compatibility fallback.
  - Keep the degraded-mode explanation: lexical works without Postgres, but semantic/symbol/graph/impact/review context are full-capability only with CocoIndex/Postgres.
- `docs/postgres-runtime.md`
  - Replace “target layout until migration” wording with “canonical layout”.
  - Add lifecycle commands: start, compose up, setup validation, doctor/status checks.
  - Add migration notes for old `examples/` entrypoints.
- `examples/global-config.yml`
  - Keep as sample config only.
  - Ensure the comment says `auto` uses CocoIndex when `PI_CODE_INDEX_POSTGRES_URL` or `POSTGRES_URL` is set, with pi-specific env preferred.
- `examples/project-settings.yml`
  - No lifecycle wording expected; leave sample settings-only.

Historical architecture docs can remain as history unless they present current commands in active setup instructions. New active docs should point to `runtime/postgres/`.

## Validation commands

Run after the implementation move from repo root:

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

If live Podman is unavailable in CI, still run shell syntax checks, compose config validation if Podman Compose is installed, Python/TS tests, and document live Podman validation as manual follow-up evidence.

## Risks and mitigations

- Compose init path breaks after move: keep `init/` beside the compose file and validate `podman compose -f runtime/postgres/compose.pgvector.yml config` plus a live start.
- Old users run examples scripts: keep an executable shim and a runnable compatibility compose copy for one migration window.
- Docs keep implying Postgres is demo-only: update README and `docs/postgres-runtime.md` with canonical runtime wording in the same change.
- Duplicate lifecycle logic appears in Python: do not add new CLI subcommands in this step; future commands must delegate to `runtime/postgres/podman-pgvector.sh`.
- Docker drift: use only `podman`, `podman compose`, or `podman-compose` in docs/scripts.
- Port/container conflicts: preserve existing env knobs and names: `PI_CODE_INDEX_POSTGRES_USER`, `PI_CODE_INDEX_POSTGRES_PASSWORD`, `PI_CODE_INDEX_POSTGRES_DB`, `PI_CODE_INDEX_POSTGRES_PORT`, and `pi-code-index-postgres`.

## Rollback and migration notes

- Rollback is file-level: restore `examples/compose.pgvector.yml`, `examples/postgres-init/01-vector.sql`, and `examples/podman-pgvector.sh` canonical content from git, then revert README/setup/doc path references.
- Existing containers and the `pi-code-index-postgres-data` volume do not need migration; the move changes repository paths, not container names or volume names.
- If a user started Postgres from the old compose file, the new helper should still detect and start/check the same `pi-code-index-postgres` container.
- Keep compatibility files until a later cleanup issue removes them after docs and users have migrated.

## Acceptance criteria for implementation issue

- `runtime/postgres/` contains the canonical compose file, helper script, and init SQL.
- `examples/podman-pgvector.sh` delegates to the canonical helper and preserves args.
- README, `docs/postgres-runtime.md`, and `scripts/setup.sh` reference `runtime/postgres/` as canonical.
- Compatibility entrypoints either continue to work or explicitly point users to the canonical path.
- Validation commands above pass or have documented Podman-unavailable exceptions.
