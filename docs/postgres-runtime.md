# Postgres runtime

PostgreSQL/pgvector is first-class runtime infrastructure for the full `pi-code-index` backend.

Canonical layout:

```text
runtime/postgres/compose.pgvector.yml
runtime/postgres/podman-pgvector.sh
runtime/postgres/init/01-vector.sql
```

Copy-paste setup:

```bash
cd ~/.pi/agent/extensions/pi-code-index
runtime/postgres/podman-pgvector.sh
export PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex
export PI_CODE_INDEX_BACKEND=auto
scripts/setup.sh --with-cocoindex --postgres-check
pi-code-index doctor --json
pi-code-index status --json
```

Equivalent compose command:

```bash
podman compose -f runtime/postgres/compose.pgvector.yml up -d
```

Validate setup and inspect runtime state:

```bash
scripts/setup.sh --with-cocoindex --postgres-check
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex uv run --extra cocoindex pi-code-index doctor --json
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex uv run --extra cocoindex pi-code-index status --json
```

`PI_CODE_INDEX_POSTGRES_URL` is preferred. `POSTGRES_URL` remains a compatibility fallback. `COCOINDEX_DATABASE_URL` is an internal CocoIndex export detail and is not the primary user setting. Config precedence remains environment variables, then project `.pi-code-index/settings.yml`, then `~/.pi-code-index/config.yml`, then defaults.

Old entrypoints remain for one migration window: `examples/podman-pgvector.sh` delegates to the canonical helper, and `examples/compose.pgvector.yml` remains runnable with its old `./postgres-init` mount.

Backend behavior:

- `backend=auto` uses CocoIndex/Postgres only when `PI_CODE_INDEX_POSTGRES_URL` or `POSTGRES_URL` is configured. Without a URL it uses lexical degraded mode.
- `backend=cocoindex` requires CocoIndex/Postgres and fails with setup guidance instead of falling back to lexical.
- `backend=lexical` forces the local JSON lexical backend even if Postgres is configured.

After changing `PI_CODE_INDEX_BACKEND` or `PI_CODE_INDEX_POSTGRES_URL`, run `pi-code-index stop --json` so the daemon inherits the new environment on the next request.

Lexical works without Postgres, but semantic, symbol, graph, impact, and review-context behavior is full-capability only with CocoIndex/Postgres.
