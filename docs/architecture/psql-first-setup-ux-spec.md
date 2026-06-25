# PSQL-first configuration, status, and setup UX spec

## Scope

This spec is the implementation contract for making `auto`/`cocoindex` setup clearly Postgres-first while keeping lexical indexing as a supported degraded fallback. It follows the step 3 plan and the step 1/2 runtime layout specs. This issue is spec-only; do not implement these changes here.

Canonical full-capability path:

```bash
runtime/postgres/podman-pgvector.sh
export PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex
export PI_CODE_INDEX_BACKEND=auto
pi-code-index doctor --json
pi-code-index status --json
pi-code-index refresh --json
```

Canonical runtime assets remain under `runtime/postgres/`:

```text
runtime/postgres/compose.pgvector.yml
runtime/postgres/podman-pgvector.sh
runtime/postgres/init/01-vector.sql
```

## Files to update

- `src/pi_code_index/config.py`
- `src/pi_code_index/backend.py`
- `src/pi_code_index/setup_checks.py`
- `src/pi_code_index/cli.py`
- `src/pi_code_index/daemon.py`
- `README.md`
- `docs/postgres-runtime.md`
- `examples/global-config.yml`
- `scripts/setup.sh`
- targeted tests under `tests/`

Do not move runtime files in this step; step 1/2 already made `runtime/postgres/` canonical.

## User-facing configuration contract

### Environment and config precedence

Preserve existing precedence exactly:

```text
environment variables -> project .pi-code-index/settings.yml -> ~/.pi-code-index/config.yml -> defaults
```

Preferred and compatibility variables:

| Variable | Contract |
| --- | --- |
| `PI_CODE_INDEX_BACKEND` | User-facing backend selector. Valid values: `auto`, `lexical`, `cocoindex`. |
| `PI_CODE_INDEX_POSTGRES_URL` | Preferred user-facing Postgres URL. Document and emit before generic `POSTGRES_URL`. |
| `POSTGRES_URL` | Compatibility fallback only when `PI_CODE_INDEX_POSTGRES_URL` is absent. |
| `COCOINDEX_DATABASE_URL` | Internal CocoIndex export detail. Do not present as the primary user setting. |
| `PI_CODE_INDEX_POSTGRES_USER` | Runtime container user; default `cocoindex`. Env-only runtime knob. |
| `PI_CODE_INDEX_POSTGRES_PASSWORD` | Runtime container password; default `cocoindex`. Env-only runtime knob. |
| `PI_CODE_INDEX_POSTGRES_DB` | Runtime container database; default `cocoindex`. Env-only runtime knob. |
| `PI_CODE_INDEX_POSTGRES_PORT` | Runtime container host port; default `5432`. Env-only runtime knob. |

`src/pi_code_index/config.py` currently defaults `GlobalConfig.postgres_url` to `postgres://cocoindex:cocoindex@localhost/cocoindex`, which can make generated config look Postgres-ready even when `auto` will not choose Postgres. Implementation must choose one of these compatible approaches:

1. Preferred: make `postgres_url` optional/empty in generated config and comments, while preserving loading of existing config files that set `postgres_url`.
2. Acceptable: keep the dataclass default for compatibility but make generated config docs/comments say that `auto` selects Postgres only from explicit URL sources.

In either approach, `auto` backend selection must be based on configured URL sources, not a misleading implicit default.

### Backend selector values

Use these exact behavior descriptions in docs/help comments where feasible:

- `auto`: `Use CocoIndex/Postgres when PI_CODE_INDEX_POSTGRES_URL or POSTGRES_URL is configured; otherwise use lexical degraded mode.`
- `cocoindex`: `Require CocoIndex/Postgres. Fail with setup guidance instead of falling back to lexical.`
- `lexical`: `Force local JSON lexical mode even if Postgres is configured.`

## Backend selection contract

`src/pi_code_index/backend.py` owns this contract.

### URL source detection

Add a single helper or equivalent shared logic to identify the URL source:

```text
postgres.configured_url_source = "pi_code_index" when PI_CODE_INDEX_POSTGRES_URL is set
postgres.configured_url_source = "postgres_url" when POSTGRES_URL is set and PI_CODE_INDEX_POSTGRES_URL is absent
postgres.configured_url_source = "config" when an explicit config-file postgres_url is used and env vars are absent
postgres.configured_url_source = "none" when no explicit URL source is configured
```

For the top-level `status --json` contract below, use only the documented values `pi_code_index`, `postgres_url`, `config`, and `none`. If implementation cannot reliably distinguish dataclass defaults from file config without more plumbing, prefer `none` for the implicit default rather than reporting configured Postgres.

### Required outcomes

| Scenario | Effective behavior | Required fields |
| --- | --- | --- |
| `PI_CODE_INDEX_BACKEND=auto`, no explicit URL | Use lexical degraded mode. | `backend="lexical"`, `requested_backend="auto"`, `backend_fallback=false`. |
| `PI_CODE_INDEX_BACKEND=auto`, explicit URL, CocoIndex success | Use CocoIndex/Postgres. | `backend="cocoindex"`, `requested_backend="auto"`, `backend_fallback=false`. |
| `PI_CODE_INDEX_BACKEND=auto`, explicit URL, CocoIndex failure | Fall back only where current operation already supports fallback. | `backend="lexical"`, `requested_backend="auto"`, `backend_fallback=true`, `warnings[]` includes failure and setup command. |
| `PI_CODE_INDEX_BACKEND=cocoindex`, missing deps/url/reachability/failure | Fail loudly. | `ok=false`, `backend="cocoindex"`, no lexical fallback. Error includes setup guidance. |
| `PI_CODE_INDEX_BACKEND=lexical` | Force lexical. | `backend="lexical"`, `requested_backend="lexical"`, `backend_fallback=false`; no Postgres required for success. |

### Fallback error wording

For required `cocoindex` failures, include this wording or a clear superset:

```text
CocoIndex/Postgres backend is required but unavailable: <error>. Configure PI_CODE_INDEX_POSTGRES_URL, start Postgres with runtime/postgres/podman-pgvector.sh, then validate with scripts/setup.sh --with-cocoindex --postgres-check.
```

For `auto` fallback after a configured URL fails, include this wording or a clear superset in both `warning` and `warnings[]`:

```text
CocoIndex/Postgres <operation> failed; falling back to lexical degraded mode: <error>. Start Postgres with runtime/postgres/podman-pgvector.sh and validate with scripts/setup.sh --with-cocoindex --postgres-check.
```

For `auto` without URL and forced `lexical`, include this warning string in status/fallback-capability surfaces:

```text
Lexical degraded mode: semantic pgvector ranking, symbol indexing, references, call graph, and impact analysis require CocoIndex/Postgres. Set PI_CODE_INDEX_POSTGRES_URL and run runtime/postgres/podman-pgvector.sh to enable full capabilities.
```

## JSON payload contract

Add fields; do not remove or rename existing fields.

### Common backend metadata

Every backend operation that returns JSON should include these fields when feasible:

```json
{
  "backend": "lexical",
  "requested_backend": "auto",
  "backend_fallback": false,
  "capabilities": {
    "semantic_search": false,
    "lexical_search": true,
    "symbols": false,
    "references": false,
    "call_graph": false,
    "impact_analysis": false,
    "repo_map": "path_only",
    "find_tests": "path_heuristic",
    "find_similar_code": "lexical_only",
    "review_context": "lexical_composition"
  },
  "warnings": [
    "Lexical degraded mode: semantic pgvector ranking, symbol indexing, references, call graph, and impact analysis require CocoIndex/Postgres. Set PI_CODE_INDEX_POSTGRES_URL and run runtime/postgres/podman-pgvector.sh to enable full capabilities."
  ]
}
```

Existing capability keys such as `search`, `graph`, `quality_context`, and `live` may remain. Add the explicit keys above rather than breaking current consumers.

### `pi-code-index status --json`

Top-level payload must keep existing fields and make these fields easy to find:

```json
{
  "ok": true,
  "repo": "/path/to/repo",
  "backend": {
    "backend": "lexical",
    "requested_backend": "auto",
    "backend_fallback": false,
    "capabilities": {},
    "warnings": []
  },
  "postgres": {
    "configured": false,
    "configured_url_source": "none",
    "url": null,
    "credentials_redacted": true,
    "lifecycle_command": "runtime/postgres/podman-pgvector.sh",
    "compose_command": "podman compose -f runtime/postgres/compose.pgvector.yml up -d",
    "validation_command": "scripts/setup.sh --with-cocoindex --postgres-check"
  },
  "setup": {
    "checks": [
      {
        "id": "postgres.url",
        "ok": false,
        "severity": "warning",
        "message": "Postgres URL is not configured; using lexical degraded mode for backend=auto.",
        "details": {
          "configured_url_source": "none",
          "preferred_env": "PI_CODE_INDEX_POSTGRES_URL",
          "compat_env": "POSTGRES_URL"
        },
        "suggested_command": "export PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex"
      }
    ],
    "summary": {"errors": 0, "warnings": 1}
  },
  "warnings": []
}
```

For configured URLs, redact credentials in any displayed URL. Either set `url` to a redacted URL such as `postgres://cocoindex:***@localhost:5432/cocoindex`, or keep `url: null` and expose parsed identity fields:

```json
{
  "host": "localhost",
  "port": 5432,
  "database": "cocoindex",
  "user": "cocoindex",
  "credentials_redacted": true
}
```

### `pi-code-index doctor --json`

Top-level payload must include `backend` and `postgres` summaries in addition to existing `setup`:

```json
{
  "ok": true,
  "repo": "/path/to/repo",
  "backend": {
    "requested_backend": "auto",
    "effective_backend": "lexical",
    "backend_fallback": false,
    "mode": "lexical_degraded"
  },
  "postgres": {
    "configured": false,
    "configured_url_source": "none",
    "reachable_checked": false,
    "lifecycle_command": "runtime/postgres/podman-pgvector.sh",
    "validation_command": "scripts/setup.sh --with-cocoindex --postgres-check"
  },
  "setup": {"checks": [], "summary": {"errors": 0, "warnings": 0}},
  "runtime_cleanup": {}
}
```

Doctor must distinguish these states:

- `backend=lexical`: missing Postgres URL is `info` or `warning`, never `error`.
- `backend=auto` with no URL: missing Postgres URL is `warning`; command exits successfully unless unrelated error checks fail.
- `backend=auto` with URL: optional dependency and Postgres checks are `warning` unless the check actually proves the current environment cannot use the configured backend.
- `backend=cocoindex`: missing optional deps, missing URL, and skipped/unreachable Postgres checks are `error`.
- Lightweight doctor does not perform a live DB connection. It must say reachability/pgvector/permissions were not checked unless `scripts/setup.sh --postgres-check` or a future explicit live check is run.

## Setup checks contract

`src/pi_code_index/setup_checks.py` must keep the current check IDs and enrich messages/details. Required check IDs remain:

```text
cocoindex.optional_deps
postgres.url
postgres.reachable
postgres.pgvector
postgres.permissions
postgres.canonical_tables
cocoindex.version
```

Add `details` fields where useful:

```json
{
  "configured_url_source": "pi_code_index|postgres_url|config|none",
  "preferred_env": "PI_CODE_INDEX_POSTGRES_URL",
  "compat_env": "POSTGRES_URL",
  "lifecycle_command": "runtime/postgres/podman-pgvector.sh",
  "validation_command": "scripts/setup.sh --with-cocoindex --postgres-check",
  "live_check_performed": false
}
```

Recommended messages and commands:

| Check | Scenario | Severity | Message | Suggested command |
| --- | --- | --- | --- | --- |
| `postgres.url` | `auto` no URL | `warning` | `Postgres URL is not configured; backend=auto is using lexical degraded mode.` | `export PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex` |
| `postgres.url` | `lexical` no URL | `info` or `warning` | `Postgres URL is not configured; backend=lexical does not require Postgres.` | `runtime/postgres/podman-pgvector.sh` |
| `postgres.url` | `cocoindex` no URL | `error` | `Postgres URL is required for backend=cocoindex.` | `export PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex` |
| `postgres.reachable` | live check skipped, not required | `warning` | `Postgres reachability was not checked by lightweight doctor/status.` | `scripts/setup.sh --with-cocoindex --postgres-check` |
| `postgres.reachable` | `cocoindex` required and not live-checked | `error` | `Postgres reachability must be validated for backend=cocoindex.` | `scripts/setup.sh --with-cocoindex --postgres-check` |
| `postgres.pgvector` | live check skipped | `warning`/`error` by requiredness | `pgvector was not checked by lightweight doctor/status.` | `scripts/setup.sh --with-cocoindex --postgres-check` |
| `postgres.permissions` | live check skipped | `warning`/`error` by requiredness | `Postgres permissions were not checked by lightweight doctor/status.` | `scripts/setup.sh --with-cocoindex --postgres-check` |
| `postgres.canonical_tables` | before refresh | `warning`/`error` by requiredness | `CocoIndex canonical table presence is checked after refresh.` | `pi-code-index refresh --json` |

## Non-JSON status and doctor UX

`src/pi_code_index/cli.py` currently falls through to raw JSON for status/doctor. Add readable summaries before any detailed dump for non-JSON output.

For `status` in lexical degraded mode, print exactly this shape:

```text
Backend: lexical (requested: auto, degraded: yes)
Postgres: not configured
Full semantic/symbol/graph features: unavailable until Postgres is configured
Start Postgres: runtime/postgres/podman-pgvector.sh
Validate: scripts/setup.sh --with-cocoindex --postgres-check
```

For `status` with CocoIndex active:

```text
Backend: cocoindex (requested: auto, degraded: no)
Postgres: configured via PI_CODE_INDEX_POSTGRES_URL
Full semantic/symbol/graph features: available when index feature gates are enabled
Validate: scripts/setup.sh --with-cocoindex --postgres-check
```

For `doctor` with required CocoIndex missing URL:

```text
Backend: cocoindex (requested: cocoindex, degraded: no)
Postgres: not configured (required)
Error: Postgres URL is required for backend=cocoindex
Set URL: export PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex
Start Postgres: runtime/postgres/podman-pgvector.sh
Validate: scripts/setup.sh --with-cocoindex --postgres-check
```

After the summary, it is acceptable to print concise failed checks or the existing JSON-like dump. `--json` must remain machine-only JSON with no human preamble.

## Degraded lexical mode contract by operation

Normalize lexical/degraded metadata with one small helper in `backend.py` and reuse it across refresh, search, status, symbol, graph, and context operations.

Required capability and warning meanings:

- Search: lexical keyword search works; semantic pgvector ranking is unavailable.
- Symbol search/definition/context: unavailable unless CocoIndex/Postgres symbol indexing is enabled.
- Caller/callee/impact: unavailable unless CocoIndex/Postgres reference indexing is enabled.
- Repo map: path-only or heuristic without symbol hierarchy.
- Find tests: path/name heuristic only without test-link indexing.
- Similar code: lexical-only unless CocoIndex semantic vectors are active.
- Review context: composed from lexical/heuristic pieces only.

Exact operation warnings:

```text
semantic pgvector ranking is unavailable in lexical degraded mode
symbol_search requires CocoIndex/Postgres symbol indexing; lexical backend cannot prove symbol absence
symbol_definition requires CocoIndex/Postgres symbol indexing; lexical backend cannot prove definition absence
symbol_context requires CocoIndex/Postgres symbol indexing; lexical backend cannot build symbol relationships
call graph tools require CocoIndex/Postgres reference indexing; lexical backend cannot prove caller/callee absence
impact_analysis requires CocoIndex/Postgres reference indexing; lexical backend cannot compute blast radius
repo_map is path-only in lexical degraded mode; symbol hierarchy requires CocoIndex/Postgres
find_tests is path-heuristic only in lexical degraded mode; indexed test links require CocoIndex/Postgres
find_similar_code is lexical-only in degraded mode; semantic similarity requires CocoIndex/Postgres
review_context uses lexical/heuristic evidence only in degraded mode
```

Graph and impact lexical responses may stay `ok=true` for compatibility, but empty results must not read as proof that no callers/callees/impact exist.

## Daemon contract

`src/pi_code_index/daemon.py` owns daemon/socket/resource lifecycle only. It must not start, stop, or validate the Postgres container beyond reporting configuration/resource state.

Required behavior:

- Keep Postgres lifecycle guidance as commands, not daemon actions.
- Daemon status must expose CocoIndex resource cache state without implying that the daemon started Postgres.
- Redact credentials for any Postgres URL in `daemon_resource_cache` and status payloads.
- If users change backend/Postgres environment, docs and warnings must remind them to restart the daemon:

```text
After changing PI_CODE_INDEX_BACKEND or PI_CODE_INDEX_POSTGRES_URL, run pi-code-index stop --json so the daemon inherits the new environment on the next request.
```

## `scripts/setup.sh` contract

`scripts/setup.sh` remains install/validation only. It must not start containers automatically.

When `--postgres-check` is omitted, output should include:

```text
Skipping optional Postgres check
Start Postgres with: runtime/postgres/podman-pgvector.sh
Validate later with: scripts/setup.sh --with-cocoindex --postgres-check
```

When Podman is installed but the container does not exist:

```text
Podman is installed, but container 'pi-code-index-postgres' does not exist.
Start it with: runtime/postgres/podman-pgvector.sh
Compose file: runtime/postgres/compose.pgvector.yml
```

When the container exists but is stopped:

```text
Container 'pi-code-index-postgres' exists but is not running.
Start it with: podman start pi-code-index-postgres or runtime/postgres/podman-pgvector.sh
```

`--postgres-check` must validate at least:

- `podman` is installed.
- container `pi-code-index-postgres` exists.
- container is running.
- `CREATE EXTENSION IF NOT EXISTS vector;` succeeds.
- `\dx vector` succeeds.

## README and docs contract

Update `README.md` and `docs/postgres-runtime.md` to use this copy-paste setup block:

```bash
cd ~/.pi/agent/extensions/pi-code-index
runtime/postgres/podman-pgvector.sh
export PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex
export PI_CODE_INDEX_BACKEND=auto
scripts/setup.sh --with-cocoindex --postgres-check
pi-code-index doctor --json
pi-code-index status --json
```

Document backend modes with these exact statements:

```text
backend=auto uses CocoIndex/Postgres only when PI_CODE_INDEX_POSTGRES_URL or POSTGRES_URL is configured. Without a URL it uses lexical degraded mode.
backend=cocoindex requires CocoIndex/Postgres and fails with setup guidance instead of falling back to lexical.
backend=lexical forces the local JSON lexical backend even if Postgres is configured.
```

Document compatibility:

```text
PI_CODE_INDEX_POSTGRES_URL is preferred. POSTGRES_URL remains a compatibility fallback. COCOINDEX_DATABASE_URL is an internal CocoIndex export detail and is not the primary user setting.
```

Document daemon env changes:

```text
After changing PI_CODE_INDEX_BACKEND or PI_CODE_INDEX_POSTGRES_URL, run pi-code-index stop --json so the daemon inherits the new environment on the next request.
```

No Docker or Docker Compose commands may be introduced. Use only Podman wording.

## Tests to add or update

Prefer extending existing tests over broad new fixtures.

### `tests/test_indexer.py` or new backend-focused tests

Add coverage for:

1. `auto` without `PI_CODE_INDEX_POSTGRES_URL`/`POSTGRES_URL`:
   - `choose_backend(repo).name == "lexical"`
   - payload includes `requested_backend == "auto"`
   - payload includes `backend_fallback is False`
   - warnings mention `Lexical degraded mode` and `PI_CODE_INDEX_POSTGRES_URL`.
2. `lexical` with Postgres env present:
   - `choose_backend(repo).name == "lexical"`
   - payload says forced lexical and no fallback.
3. `auto` with `PI_CODE_INDEX_POSTGRES_URL` and mocked CocoIndex failure:
   - fallback payload `backend == "lexical"`
   - `backend_fallback is True`
   - `warnings[]` includes `runtime/postgres/podman-pgvector.sh` and `scripts/setup.sh --with-cocoindex --postgres-check`.
4. `cocoindex` with mocked CocoIndex failure:
   - no lexical fallback
   - `ok is False`
   - error includes `PI_CODE_INDEX_POSTGRES_URL`, `runtime/postgres/podman-pgvector.sh`, and setup validation command.

### `tests/test_setup_checks.py`

Add coverage for:

1. Required check IDs still exist.
2. URL source detection values: `pi_code_index`, `postgres_url`, `none`; `config` if explicit config-source support is implemented.
3. `backend=auto` no URL marks `postgres.url` as warning/info, not error.
4. `backend=cocoindex` no URL marks `postgres.url` and skipped live checks as error.
5. `postgres.reachable`, `postgres.pgvector`, and `postgres.permissions` details include `live_check_performed: false` and validation command.

### `tests/test_daemon_lifecycle.py`

Add coverage for:

1. `daemon_resource_cache` redacts credentials when URL includes password.
2. Daemon status does not expose lifecycle actions as performed actions.
3. Status payload includes restart reminder when backend/Postgres env guidance is emitted.

### CLI tests

Add or extend CLI tests for:

1. `doctor --json` includes top-level `backend`, `postgres`, and `setup`.
2. `status --json --no-daemon` includes top-level `postgres.lifecycle_command` and `postgres.validation_command`.
3. non-JSON `status` prints the five-line degraded summary instead of raw JSON only.
4. non-JSON `doctor` for `PI_CODE_INDEX_BACKEND=cocoindex` without URL prints required-Postgres setup guidance.

### Docs/setup tests

Add lightweight assertions where existing test patterns allow:

- `scripts/setup.sh` contains `runtime/postgres/podman-pgvector.sh`.
- no new docs/scripts references to `docker` commands for this runtime flow.
- `examples/global-config.yml` mentions `PI_CODE_INDEX_POSTGRES_URL` before `POSTGRES_URL`.

## Validation commands

Static/local validation from repo root:

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

If Podman is unavailable, run syntax/unit/type checks and record live Podman validation as manual follow-up evidence.

## Rollout notes

- Keep current JSON fields. Add new fields rather than renaming/removing.
- Make warnings visible enough that lexical empty symbol/graph results are not mistaken for proof of absence.
- Keep `examples/` compatibility shims for one migration window.
- Keep `scripts/setup.sh` validation-only.
- Restart daemon after config/env changes so it observes new backend/Postgres settings.

## Non-goals

- Do not implement this spec in the spec-generation issue.
- Do not add a Python Postgres service manager.
- Do not add `pi-code-index postgres start|stop|status` commands in this step.
- Do not introduce Docker commands or Docker Compose references.
- Do not remove lexical backend support.
- Do not delete examples compatibility files.
- Do not redesign CocoIndex schemas, ranking, daemon protocol, or Pi tool names.
- Do not run live Postgres checks inside lightweight `doctor` unless a future explicit flag is added.
- Do not make the daemon own database lifecycle.
