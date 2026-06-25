# Daemon live indexing operations plan

## Goal

Harden the current daemon and live-indexing path for multi-repository use without changing the existing CLI or Pi tool contract. The daemon should reliably serve all query tools, keep CocoIndex/Postgres resources warm per repository/configuration, report freshness and operational state, recover from stale runtime state, and give users clear setup, troubleshooting, and validation paths.

This is a planning document only. It does not change product code.

## Current inspected state

- `index.ts` registers Pi tools for `code_search`, symbol tools, graph tools, and repo-quality context tools. It also exposes `/code-index-status`, `/code-index-refresh`, and `/code-index-stop`. The extension shells out through `uv run --project <extension> pi-code-index ... --json` and keeps formatted text compact while returning full JSON details.
- `src/pi_code_index/cli.py` owns public commands: `init`, `search`, `refresh`, `status`, `stop`, `live start|stop|status`, `symbols`, `graph`, `context`, and hidden `daemon`. Auto-start and stale socket/pid cleanup already live here.
- `src/pi_code_index/daemon.py` owns the Unix socket server, handshake/version checks, `BackendResourceCache`, `LiveWatcher`, `LiveWatcherRegistry`, per-request routing, daemon status payloads, and shutdown cleanup.
- `src/pi_code_index/backend.py` routes `auto`, `lexical`, and `cocoindex`; lexical remains the no-Postgres fallback and CocoIndex failures in `auto` fall back to lexical payloads with warnings.
- `src/pi_code_index/coco_backend.py` uses CocoIndex V1 concepts only: `coco.App`, `@coco.fn`, `@coco.lifespan`, `coco.ContextKey`, `coco.runtime`, `localfs.walk_dir(..., live=True)`, `postgres.TableTarget`, and Postgres/pgvector. Runtime search/status paths use `asyncpg`, canonical tables, and optional daemon-provided `CocoBackendResources`.
- `src/pi_code_index/config.py` defines global and project config, including socket/pid/log paths, schema/table prefix, pipeline version, include/exclude globs, backend selection, symbol/reference/test-link feature gates, and AST chunk settings.
- `tests/test_daemon_lifecycle.py` covers stale runtime cleanup, handshake restarts, daemon status, cached CocoIndex resources, symbol/graph routing through cached resources, live start/status/stop, and lexical live refresh behavior.
- `README.md`, `examples/global-config.yml`, `examples/project-settings.yml`, and `examples/podman-pgvector.sh` document setup, Podman Postgres, status, refresh, live mode, and CocoIndex validation.

## Non-goals and constraints

- Do not remove or rename existing CLI commands, Pi tools, flags, or required JSON fields.
- Additive CLI commands/status fields are acceptable when they preserve current behavior.
- Keep `backend: auto` lexical fallback behavior.
- Use Podman, not Docker, in examples and validation.
- Use CocoIndex V1 concepts only; do not plan around non-V1 APIs.
- Product-code changes should happen in later implementation issues, not in this planning issue.

## Affected modules

| Module | Planned responsibility |
| --- | --- |
| `index.ts` | Preserve existing tools and commands. Optionally add additive commands such as `/code-index-live-status` or `/code-index-doctor` only after CLI support exists. Keep structured details as the place for verbose status/observability fields. |
| `src/pi_code_index/cli.py` | Keep public command names stable. Extend `status --json`, `live status --json`, and future additive `doctor`/`setup-check` commands with daemon, setup, freshness, and troubleshooting data. Keep auto-start and stale runtime cleanup before daemon requests. |
| `src/pi_code_index/daemon.py` | Main implementation point for lifecycle hardening, resource reuse policy, live watcher supervision, per-repo status aggregation, freshness summaries, stale watcher recovery, request timing, and graceful shutdown. |
| `src/pi_code_index/backend.py` | Keep routing and fallback boundary. Normalize status/error payloads so all tools get consistent backend, fallback, freshness, and warning fields. |
| `src/pi_code_index/coco_backend.py` | Continue using CocoIndex V1 app/update flows plus canonical Postgres tables. Provide status queries for repo/branch/table counts, freshness counts, schema/pipeline versions, pgvector availability, and resource warm/cold state. |
| `src/pi_code_index/config.py` | Add only backward-compatible config keys for daemon/live tuning if needed: poll defaults, max watched repos, refresh debounce, stale thresholds, request timeout, and setup-check severity. Existing configs remain valid. |
| `src/pi_code_index/indexer.py` | Remains lexical fallback and file iteration source for live polling. Any live scan optimization must preserve include/exclude semantics. |
| `tests/` | Add daemon/live/status/doctor tests, no-daemon fallback tests, multi-repo resource tests, and CocoIndex/Postgres integration status tests. |
| `README.md` and `examples/` | Document setup checks, live-mode operation, status fields, troubleshooting flows, Podman Postgres commands, and performance-observability validation. |

## Lifecycle and resource model

### Daemon lifecycle

The daemon remains optional and auto-started by CLI commands unless `--no-daemon` is supplied.

Lifecycle states should be reported as additive JSON fields:

| State | Meaning | Recovery path |
| --- | --- | --- |
| `not_running` | No live socket/pid, or user used `--no-daemon`. | `pi-code-index status --json` can show direct backend status; any daemon-backed command can auto-start. |
| `starting` | CLI spawned daemon and is waiting for handshake. | Retry handshake with bounded timeout; point to log path on failure. |
| `running` | Handshake matches client version, protocol version, and global config mtime. | Normal operation. |
| `restart_required` | Version/protocol/global config mtime mismatch. | Existing `request_or_start()` stops and restarts daemon. Preserve this path. |
| `degraded` | Daemon responds but one or more repos/backends/watchers report errors. | Status explains repo/backend/error and next command. |
| `stopping` | Stop request accepted or runtime files are being removed. | Stop all live watchers, close resources, unlink socket/pid. |

### Resource cache

`BackendResourceCache` should remain keyed by repo plus backend configuration. Preserve existing key ingredients and include any new operationally relevant fields:

- resolved repo root
- selected backend and requested backend
- Postgres URL identity, without exposing credentials in human-readable output
- embedding model
- schema name, table prefix, table name
- pipeline version and schema version
- branch mode and current branch identity when branch-aware indexing is enabled
- feature gates: symbols, references, test links
- chunking strategy, AST languages, chunk sizes, result-code limits
- include/exclude globs

Resource payloads should expose warm/cold counters but not secrets:

```json
{
  "daemon_resource_cache": {
    "entries": 2,
    "resources": [
      {
        "repo": "/repo/a",
        "backend": "cocoindex",
        "schema_name": "public",
        "table_prefix": "pi_code_index",
        "table_name": "code_embeddings",
        "pipeline_version": "canonical-v1-ast-v1",
        "resources": {
          "postgres_pool": "warm",
          "embedder": "warm",
          "pool_creations": 1,
          "embedder_creations": 1,
          "closed": false
        }
      }
    ]
  }
}
```

Later implementation should add resource eviction only as an additive policy, for example `max_cached_repos` and `idle_ttl_seconds`, defaulting to current no-eviction behavior for small local use.

## Multi-repo and live indexing strategy

### Repository identity

Use the existing canonical identity fields in `coco_backend.py` as the durable multi-repo contract:

- `repo_id` from resolved repo root
- `worktree_id` from git common dir/root identity
- `branch` and `branch_id` from current branch/head
- `root_path` as human-readable status data

All CocoIndex/Postgres status and query payloads should remain scoped by `repo_id` and `branch_id`; lexical status remains scoped by repo-local index path.

### Live watcher registry

`LiveWatcherRegistry` already keeps one watcher per resolved repo. Harden it with additive status and recovery fields:

```json
{
  "live": {
    "repo": "/repo/a",
    "running": true,
    "poll_interval": 1.0,
    "watched_files": 312,
    "last_scan_started_at": "...",
    "last_scan_finished_at": "...",
    "last_update": "...",
    "last_refresh": "...",
    "refresh_count": 7,
    "last_error": null,
    "consecutive_errors": 0,
    "stale": false,
    "stale_reason": null
  }
}
```

Planned behavior:

- One watcher per resolved repo, independent of other repos.
- Initial `live start` takes a baseline snapshot and reports `watched_files` without forcing an immediate refresh unless requested later by an additive flag.
- File changes trigger backend refresh through the existing router so lexical and CocoIndex paths stay aligned.
- Refresh calls are serialized per watcher to avoid overlapping CocoIndex updates for the same repo.
- A bounded debounce window should coalesce rapid edits before refresh.
- Failures do not kill the watcher; they update `last_error`, increment `consecutive_errors`, and mark status `degraded` or `stale`.
- `live stop` should always return the last known state and `stopped: true` when a watcher existed.
- Daemon shutdown must stop all watchers before closing resource pools.

### CocoIndex live relationship

Keep CLI live mode as daemon-supervised polling for now. It works consistently for both lexical and CocoIndex backends and preserves current UX. The CocoIndex V1 app may continue to declare `localfs.walk_dir(..., live=True)` internally, but operational live behavior should be driven by daemon refresh triggers and `app.update_blocking()` catch-up runs until a future implementation issue proves a direct CocoIndex live runtime path is more reliable.

## Data and API contracts

### Stable search result contract

All Pi tools and CLI JSON payloads must preserve current required fields. For `code_search` results:

```json
{
  "score": 0.91,
  "filename": "src/pi_code_index/daemon.py",
  "start_line": 135,
  "end_line": 214,
  "code": "..."
}
```

Add operational metadata only as optional fields:

```json
{
  "metadata": {
    "backend": "cocoindex",
    "repo_id": "...",
    "branch_id": "...",
    "chunk_id": "...",
    "freshness_status": "current",
    "indexed_at": "...",
    "pipeline_version": "canonical-v1-ast-v1"
  }
}
```

### Status payload contract

`status --json` should remain successful even when the daemon is not running or CocoIndex is unavailable. Additive fields should include:

- `ok`, `repo`, `backend`, `requested_backend`
- `daemon`: lifecycle state, pid, server/client versions, protocol version, config mtime, socket/pid/log paths
- `live`: watcher status for the current repo
- `all_live` or `watchers`: optional aggregate for multi-repo status
- `daemon_resource_cache`: warm resources and non-secret config keys
- `freshness`: counts by `current`, `pending`, `stale`, `deleted`, `error`, plus newest `last_indexed_at`
- `counts`: files, chunks, symbols, references, call edges, test links, repo hierarchy nodes
- `capabilities`: search, symbols, references, graph, quality context, live mode
- `setup`: prerequisite checks and warnings
- `performance`: last refresh/search timings when available
- `warnings`: non-fatal fallback/degraded setup messages

### Freshness contract

Use the existing canonical `freshness` table as the source of truth for CocoIndex/Postgres freshness. Lexical status can synthesize freshness from index metadata until a richer lexical freshness table exists.

Freshness statuses:

| Status | Meaning |
| --- | --- |
| `current` | File was seen and indexed with current source hash and pipeline version. |
| `pending` | Change detected but refresh not complete. |
| `stale` | Indexed row exists but source hash, mtime/size, branch, or pipeline version no longer matches. |
| `deleted` | Previously indexed file no longer exists or no longer matches include/exclude globs. |
| `error` | Last indexing attempt failed for this file; `error` and metadata contain cause. |

Status should include both per-repo counts and enough detail for troubleshooting the latest errors without dumping large payloads.

## Operational UX

Preserve the current UX:

```bash
pi-code-index status --json
pi-code-index refresh --json
pi-code-index live start --json
pi-code-index live status --json
pi-code-index live stop --json
pi-code-index stop --json
```

Additive commands can be introduced in a future implementation issue:

```bash
pi-code-index doctor --json
pi-code-index live status --json --all
pi-code-index status --json --all-repos
pi-code-index daemon resources --json
```

Potential additive Pi commands after CLI support:

- `/code-index-live-status` for current repo watcher state
- `/code-index-live-start` and `/code-index-live-stop` if interactive daemon live control is useful
- `/code-index-doctor` for setup and troubleshooting checks

Default human text should remain compact. Verbose diagnostics should live in JSON/details so existing Pi output is not made noisy.

## Setup checks and troubleshooting

A future `doctor` or extended `status --json` path should check:

- `uv` is installed and can run `pi-code-index --help`.
- CLI package import succeeds from the extension project.
- Node/npm dependencies are installed enough for Pi extension execution.
- Global config path, project config path, socket path, pid path, and log path are readable/writable as needed.
- Current cwd resolves to a git repo or a usable directory root.
- Include/exclude globs match at least one file, with warnings for overly broad or empty matches.
- `backend` is one of `auto`, `lexical`, or `cocoindex`.
- For CocoIndex/Postgres: `POSTGRES_URL` or `PI_CODE_INDEX_POSTGRES_URL`, network reachability, pgvector extension, schema/table permissions, schema migration row, and canonical table availability.
- CocoIndex optional dependencies are installed only when CocoIndex is requested or detected.
- Feature gates are consistent: graph tools require `enable_symbols=true` and `enable_references=true`; test links require `enable_test_links=true`.
- Runtime files are not stale; if stale, report that cleanup was performed or suggest `pi-code-index stop --json`.
- Daemon log tail location is surfaced, not embedded wholesale.

Troubleshooting guidance should map common symptoms to commands:

| Symptom | Suggested command |
| --- | --- |
| No results after edits | `pi-code-index live status --json` then `pi-code-index refresh --json` |
| Daemon will not start | `pi-code-index status --json` and inspect `~/.pi-code-index/daemon.log` |
| CocoIndex unavailable | `scripts/setup.sh --with-cocoindex --postgres-check` |
| Postgres not reachable | `podman ps` and `podman exec pi-code-index-postgres psql -U cocoindex -d cocoindex -c "SELECT 1"` |
| Graph tools empty | Enable symbols/references, refresh, then `pi-code-index symbols search --json "target"` |
| Status reports stale pipeline | `pi-code-index stop --json` then `pi-code-index refresh --json` |

## Observability and performance model

Add low-overhead in-process metrics. Do not introduce external telemetry.

Recommended metrics in status/details:

- daemon uptime seconds
- request counts by type and repo
- request durations: last, rolling average, max
- refresh durations and last refresh result
- live scan durations and watched file counts
- refresh coalescing/debounce counts
- resource cache entries, warm/cold state, creation counts
- CocoIndex/Postgres table counts and freshness counts
- fallback counts from CocoIndex to lexical under `backend: auto`
- last error by component: daemon, backend, live watcher, setup, Postgres

Keep metric history bounded in memory. Expose summaries, not unbounded event logs.

## Dependencies

Current dependency plan remains valid:

- Required Python: `pyyaml` and standard library Unix socket/threading/subprocess support.
- Optional CocoIndex backend: `cocoindex>=1.0.0`, `asyncpg>=0.29.0`, `sentence-transformers>=3.0.0`.
- CocoIndex V1 concepts only: `coco.App`, `@coco.fn`, `@coco.lifespan`, `coco.ContextKey`, `coco.runtime`, `localfs.walk_dir`, `postgres.TableTarget`.
- Postgres with pgvector for CocoIndex mode.
- Node/npm for the Pi extension TypeScript code and tests.
- `uv` for Python environment management and CLI execution.
- Podman for local Postgres/pgvector development and validation.

Podman example:

```bash
podman run -d \
  --name pi-code-index-postgres \
  -e POSTGRES_USER=cocoindex \
  -e POSTGRES_PASSWORD=cocoindex \
  -e POSTGRES_DB=cocoindex \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

## Implementation sequencing for later issues

1. Lock status contracts in tests with current payloads and additive fields.
2. Add daemon lifecycle state, uptime, request timing, and bounded metrics.
3. Extend live watcher state with scan timings, consecutive errors, stale flags, and refresh serialization/debounce.
4. Add CocoIndex/Postgres status query helpers for schema, table counts, freshness counts, and pgvector availability.
5. Add setup checks under `status --json` or a new `doctor --json` command.
6. Add multi-repo aggregate status (`--all` or daemon resources command) without changing current repo defaults.
7. Update README and examples with operational flows and troubleshooting commands.
8. Add Pi commands only after CLI JSON contracts are stable.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Existing Pi/CLI UX breaks | Preserve command names, flags, and required result fields; add fields only. |
| Status becomes too noisy | Keep text compact; place verbose data in JSON/details. |
| Long refresh blocks all daemon requests | Serialize per repo, not globally; add request timing and consider later worker queue if needed. |
| Rapid edits cause repeated expensive refreshes | Add debounce/coalescing with status counters. |
| Multi-repo resource growth | Add optional max cached repos/idle TTL with default behavior preserving current small-use assumptions. |
| Postgres credentials leak in status | Redact URLs or report only host/db/user fingerprints. |
| Stale runtime files prevent startup | Preserve and extend stale socket/pid cleanup; report cleanup in status/doctor. |
| CocoIndex/Postgres failure hides behind lexical fallback | Keep fallback warning and add fallback counters/setup warnings. |
| Freshness incorrectly marked current after parser fallback | Distinguish `current` searchability from parser `error`; include parser errors in freshness metadata. |
| Branch/worktree ambiguity | Continue current branch mode by default; report branch/head/worktree IDs in status. |
| CocoIndex API drift | Use only V1 concepts already present and validate with optional integration tests. |

## Validation commands

Planning-only validation for this issue:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
git diff -- docs/architecture/daemon-live-ops-plan.md
git diff --name-only
```

Future implementation validation should include:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
scripts/setup.sh
npm run typecheck
uv run python -m compileall src tests
uv run pytest tests/test_daemon_lifecycle.py
uv run pytest
uv run pi-code-index --help
uv run pi-code-index status --json
uv run pi-code-index --no-daemon status --json
uv run pi-code-index --no-daemon search --json --refresh "where is daemon lifecycle handled"
uv run pi-code-index live start --json --poll-interval 0.2
uv run pi-code-index live status --json
uv run pi-code-index live stop --json
uv run pi-code-index stop --json
```

CocoIndex/Postgres validation with Podman:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
podman run -d \
  --name pi-code-index-postgres \
  -e POSTGRES_USER=cocoindex \
  -e POSTGRES_PASSWORD=cocoindex \
  -e POSTGRES_DB=cocoindex \
  -p 5432:5432 \
  pgvector/pgvector:pg16

export POSTGRES_URL=postgres://cocoindex:cocoindex@localhost/cocoindex
export PI_CODE_INDEX_BACKEND=cocoindex
export COCOINDEX_DB=.pi-code-index/cocoindex.db
scripts/setup.sh --with-cocoindex --postgres-check
uv run pi-code-index stop --json
uv run pi-code-index refresh --json
uv run pi-code-index status --json
uv run pi-code-index search --json --top-k 8 "where is live watcher status"
uv run pi-code-index symbols search --json --top-k 5 "daemon resource cache"
uv run pi-code-index graph callers --json --top-k 5 "pi_code_index.daemon.handle"
podman exec pi-code-index-postgres psql -U cocoindex -d cocoindex \
  -c "SELECT count(*) AS chunks FROM pi_code_index_chunks" \
  -c "SELECT status, count(*) FROM pi_code_index_freshness GROUP BY status ORDER BY status"
```

Cleanup after local validation:

```bash
podman rm -f pi-code-index-postgres
```
