# PSQL-first setup UX plan

## Goal

Make `auto`/`cocoindex` setup unmistakably Postgres-first while keeping lexical indexing as an explicit degraded fallback. This step should improve user-facing contracts and messages only; it should not add a Python service manager or change the canonical runtime asset ownership established under `runtime/postgres/`.

Primary user path:

```text
runtime/postgres/podman-pgvector.sh
export PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex
export PI_CODE_INDEX_BACKEND=auto   # or cocoindex to require Postgres
pi-code-index doctor --json
pi-code-index status --json
pi-code-index refresh --json
```

## Files to update in implementation

- `src/pi_code_index/config.py`
  - Generate default config that documents Postgres-first intent without making `auto` silently look fully configured.
  - Preserve precedence: environment variables -> project settings -> global config -> defaults.
  - Prefer `PI_CODE_INDEX_POSTGRES_URL` over `POSTGRES_URL` in docs/comments and generated guidance.
- `src/pi_code_index/backend.py`
  - Keep `auto`, `lexical`, and `cocoindex` semantics, but make lexical responses self-describing as degraded mode.
  - Normalize fallback fields across refresh/search/status/symbol/graph/context operations.
- `src/pi_code_index/setup_checks.py`
  - Make doctor checks explain whether Postgres is configured, required, reachable-check-skipped, or lexical-degraded.
  - Point setup hints at `runtime/postgres/podman-pgvector.sh` and `scripts/setup.sh --with-cocoindex --postgres-check`.
- `src/pi_code_index/cli.py`
  - Improve non-JSON status/doctor readability where payloads are currently dumped as raw JSON.
  - Ensure JSON status surfaces requested backend, effective backend, fallback state, capabilities, warnings, and setup hints at top-level or predictable nested fields.
- `src/pi_code_index/daemon.py`
  - Keep daemon lifecycle separate from database lifecycle.
  - Ensure daemon status/resource cache redacts credentials and reports CocoIndex resource state without implying it starts Postgres.
- `README.md`, `docs/postgres-runtime.md`, and examples comments
  - Present `runtime/postgres/` as canonical.
  - Show env setup, backend modes, lifecycle commands, and degraded lexical behavior consistently.
- `scripts/setup.sh`
  - Keep validation-only behavior.
  - Make skipped Postgres check output say how to start and validate the canonical runtime.

## User-facing contracts

### Env/config contract

- `PI_CODE_INDEX_POSTGRES_URL` is the preferred user-facing database URL.
- `POSTGRES_URL` remains compatibility fallback when the pi-specific URL is absent.
- `COCOINDEX_DATABASE_URL` is an internal compatibility/export detail only.
- `PI_CODE_INDEX_BACKEND` values:
  - `auto`: use CocoIndex/Postgres only when a Postgres URL is configured; otherwise lexical degraded mode.
  - `cocoindex`: require CocoIndex/Postgres and fail loudly with setup guidance.
  - `lexical`: force local JSON lexical mode even if Postgres env exists.
- Runtime container knobs remain env-only: `PI_CODE_INDEX_POSTGRES_USER`, `PI_CODE_INDEX_POSTGRES_PASSWORD`, `PI_CODE_INDEX_POSTGRES_DB`, `PI_CODE_INDEX_POSTGRES_PORT`.
- Generated `~/.pi-code-index/config.yml` should not mislead users into thinking the default Postgres URL is active unless a URL is configured by env/config. If retaining `postgres_url` default for compatibility, generated comments/docs must say `auto` only chooses Postgres from configured URL sources.

### Backend selection contract

- `auto` without URL:
  - `backend=lexical`
  - `requested_backend=auto`
  - `backend_fallback=false`
  - warning/capability text says lexical is degraded and how to enable Postgres.
- `auto` with URL and CocoIndex/Postgres success:
  - `backend=cocoindex`
  - `requested_backend=auto`
  - `backend_fallback=false`
- `auto` with URL and CocoIndex/Postgres failure:
  - operation may return lexical payload only where fallback is already supported.
  - `backend=lexical`, `requested_backend=auto`, `backend_fallback=true`, `warnings[]` includes failure and setup command.
- `cocoindex` failure:
  - `ok=false`; no lexical fallback.
  - error includes `PI_CODE_INDEX_POSTGRES_URL`, `runtime/postgres/podman-pgvector.sh`, and `scripts/setup.sh --with-cocoindex --postgres-check`.
- `lexical`:
  - no Postgres checks are required for success, but status should identify reduced capabilities.

### Status/doctor output contract

`pi-code-index status --json` should make these fields easy for Pi and humans to find:

```text
backend.backend
backend.requested_backend
backend.backend_fallback
backend.capabilities
backend.warnings[]
setup.checks[].id/severity/message/suggested_command
postgres.configured_url_source: pi_code_index|postgres_url|none
postgres.lifecycle_command: runtime/postgres/podman-pgvector.sh
postgres.validation_command: scripts/setup.sh --with-cocoindex --postgres-check
```

`pi-code-index doctor --json` should distinguish:

- optional lexical mode: Postgres URL absent is warning/info, not error.
- required `cocoindex`: missing deps/url/reachability checks are errors.
- `auto` with URL: Postgres checks are warnings unless proven required by the requested operation.
- lightweight doctor did not perform a live DB connection unless `scripts/setup.sh --postgres-check` or future live checks are run.

Non-JSON `status` and `doctor` should show a short summary before any detailed dump:

```text
Backend: lexical (requested: auto, degraded: yes)
Postgres: not configured
Full semantic/symbol/graph features: unavailable until Postgres is configured
Start Postgres: runtime/postgres/podman-pgvector.sh
Validate: scripts/setup.sh --with-cocoindex --postgres-check
```

## Setup guidance contract

- `scripts/setup.sh` remains install/validation only; it must not start containers automatically.
- `scripts/setup.sh --with-cocoindex --postgres-check` validates Podman, container running state, pgvector extension, and permissions.
- When Postgres is skipped, missing, or stopped, guidance uses only Podman commands and canonical paths:
  - `runtime/postgres/podman-pgvector.sh`
  - `podman compose -f runtime/postgres/compose.pgvector.yml up -d`
  - `podman start pi-code-index-postgres`
- README and `docs/postgres-runtime.md` should include a copy-paste env block after successful startup:

```bash
export PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex
export PI_CODE_INDEX_BACKEND=auto
```

## Degraded lexical mode contract

Lexical mode is supported, but every status/fallback payload should say what is missing:

- semantic pgvector ranking is unavailable.
- symbol search/definition/context require CocoIndex/Postgres symbol indexing.
- caller/callee/impact require CocoIndex/Postgres reference indexing.
- repo-map/find-tests/similar/review context are heuristic/lexical only.

Graph and impact lexical responses may stay `ok=true` for compatibility, but warnings must be strong enough that an empty result cannot be confused with “no callers/callees exist”.

## Implementation slices

1. **Normalize backend metadata**
   - Add a small helper in `backend.py` for lexical/degraded warnings and capability text.
   - Apply it to `refresh`, `search`, `status`, symbol, graph, and context fallbacks without changing result shapes more than necessary.

2. **Clarify setup checks**
   - Add URL-source detection in `setup_checks.py`.
   - Update Postgres check messages and suggested commands.
   - Keep severity rules tied to requested backend.

3. **Improve CLI summaries**
   - Add concise non-JSON summaries for `status` and `doctor`.
   - Preserve `--json` output compatibility.

4. **Refresh docs and generated config comments**
   - Update README/docs/examples to use the same env block and lifecycle commands.
   - If comments cannot be preserved in generated YAML, keep generated values simple and put the contract in docs.

5. **Targeted tests**
   - Extend existing setup/backend/daemon tests instead of adding broad fixtures.

## Validation commands

Static/local validation:

```bash
uv run python -m compileall src tests
uv run --extra dev pytest tests/test_setup_checks.py tests/test_daemon_lifecycle.py tests/test_indexer.py
uv run --extra dev pytest
npm run typecheck
npm run test:ts
bash -n scripts/setup.sh
bash -n runtime/postgres/podman-pgvector.sh
bash -n examples/podman-pgvector.sh
podman compose -f runtime/postgres/compose.pgvector.yml config
```

Behavior checks without live Postgres:

```bash
PI_CODE_INDEX_BACKEND=auto env -u PI_CODE_INDEX_POSTGRES_URL -u POSTGRES_URL uv run pi-code-index --no-daemon status --json --repo .
PI_CODE_INDEX_BACKEND=lexical uv run pi-code-index doctor --json --repo .
PI_CODE_INDEX_BACKEND=cocoindex env -u PI_CODE_INDEX_POSTGRES_URL -u POSTGRES_URL uv run pi-code-index doctor --json --repo .
```

Live Postgres validation when Podman is available:

```bash
runtime/postgres/podman-pgvector.sh
scripts/setup.sh --with-cocoindex --postgres-check --skip-tests
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex uv run --extra cocoindex pi-code-index doctor --json --repo .
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex uv run --extra cocoindex pi-code-index status --json --repo .
PI_CODE_INDEX_BACKEND=cocoindex PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex uv run --extra cocoindex pytest tests/test_cocoindex_postgres_integration.py
```

## Risks and mitigations

- **Breaking JSON consumers**: keep existing fields; add fields rather than renaming/removing.
- **Making `auto` too noisy**: use warning/info severity for no-URL lexical mode, but still mark capabilities as degraded.
- **Silent fallback remains ambiguous**: require `backend_fallback` and `warnings[]` on every CocoIndex-to-lexical fallback path.
- **Docs drift from scripts**: use the same canonical commands in README, docs, setup checks, and setup script output.
- **Daemon env confusion**: docs and error messages must remind users to `pi-code-index stop --json` after changing backend/Postgres env so the daemon inherits it.
- **Scope creep into service management**: no new Python lifecycle manager in this step; future CLI aliases must delegate to `runtime/postgres/podman-pgvector.sh`.

## Acceptance criteria

- Generated/config/docs clearly prefer `PI_CODE_INDEX_POSTGRES_URL` and explain `POSTGRES_URL` compatibility.
- `auto`, `lexical`, and `cocoindex` behavior is documented and visible in status/doctor payloads.
- `status --json` and `doctor --json` expose requested/effective backend, fallback state, capabilities, warnings, and canonical setup commands.
- Non-JSON status/doctor output is readable enough to diagnose “why am I in lexical mode?” without parsing raw JSON.
- `scripts/setup.sh` guidance remains validation-only and references `runtime/postgres/` canonical assets.
- Lexical mode warnings explicitly say semantic/symbol/graph/impact behavior is degraded or unavailable.
- `cocoindex` failures do not silently degrade to lexical.
- Tests cover no-URL `auto`, forced `lexical`, required `cocoindex`, and auto-with-URL fallback messaging.
- No Docker commands are introduced.
