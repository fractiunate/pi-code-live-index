# Daemon live indexing operations spec

## Scope

This document is the implementation contract for hardening daemon lifecycle, live indexing, resource reuse, status/freshness reporting, setup checks, and operational UX. It refines `docs/architecture/daemon-live-ops-plan.md` into concrete requirements for an implementation subagent.

This is a specs/docs-only artifact. The implementation issue must preserve the existing CLI and Pi UX and may only add fields, flags, commands, or Pi slash commands where this spec explicitly marks them additive.

## Current baseline to preserve

The current request path is:

```text
Pi extension index.ts
  -> uv run --project <extension> pi-code-index ... --json
  -> src/pi_code_index/cli.py
  -> optional Unix socket daemon in src/pi_code_index/daemon.py
  -> src/pi_code_index/backend.py
  -> lexical JSON index or src/pi_code_index/coco_backend.py
```

The implementation must not remove or rename:

- Pi tools: `code_search`, `symbol_search`, `symbol_definition`, `symbol_context`, `find_callers`, `find_callees`, `impact_analysis`, `repo_map`, `find_tests`, `find_similar_code`, `review_context`.
- Pi commands: `/code-index-status`, `/code-index-refresh`, `/code-index-stop`.
- CLI commands: `init`, `search`, `refresh`, `status`, `stop`, `live start`, `live stop`, `live status`, `symbols search|definition|context`, `graph callers|callees|impact`, `context repo-map|tests|similar|review`, and hidden `daemon`.
- CLI flags used today: `--json`, `--repo`, `--no-daemon`, `--top-k`, `--refresh`, live `--poll-interval`, symbol filters, graph/context flags.
- Search result required fields: `score`, `filename`, `start_line`, `end_line`, `code`.
- `backend: auto` fallback behavior: CocoIndex/Postgres failures must fall back to lexical where current code already does so, with a warning.

Default human output must remain compact. Rich diagnostics belong in JSON payloads and Pi details.

## CocoIndex constraints

Use CocoIndex V1 concepts only. The current allowed concept set is:

- `coco.App`
- `@coco.fn`
- `@coco.lifespan`
- `coco.ContextKey`
- `coco.runtime`
- `localfs.walk_dir(..., live=True)`
- `postgres.TableTarget`
- existing Postgres/pgvector runtime access through `asyncpg`

Do not design or implement against non-V1 or speculative CocoIndex APIs. Daemon-supervised polling remains the operational live indexing mechanism for this work. The CocoIndex app may continue to declare live file walking internally, but user-visible freshness is driven by daemon refresh/catch-up runs.

## Module responsibilities

| Module | Required changes in implementation issue |
| --- | --- |
| `index.ts` | Preserve current tools and commands. Optionally add slash commands only after CLI support exists: `/code-index-live-status`, `/code-index-live-start`, `/code-index-live-stop`, `/code-index-doctor`. Continue returning full JSON in details and compact text in user-visible output. |
| `src/pi_code_index/cli.py` | Keep command names stable. Enrich `status --json` and `live status --json`. Add optional `doctor --json` only if implemented as an additive command. Maintain auto-start and stale runtime cleanup before daemon requests. |
| `src/pi_code_index/daemon.py` | Own lifecycle state, handshake decisions, resource cache status, request metrics, live watcher supervision, stale watcher recovery, per-repo status aggregation, and graceful shutdown. |
| `src/pi_code_index/backend.py` | Preserve backend routing and lexical fallback. Normalize `backend`, `requested_backend`, `backend_fallback`, `freshness`, and `warnings` fields across operations. |
| `src/pi_code_index/coco_backend.py` | Continue using CocoIndex V1 app/update flow. Provide Postgres status helpers for canonical tables, pgvector, schema/pipeline versions, freshness counts, and resource warm/cold state. |
| `src/pi_code_index/config.py` | Add only backward-compatible config keys. Existing global/project config files must remain valid. |
| `src/pi_code_index/indexer.py` | Remain lexical file iteration/indexing source. Live scan optimization must preserve include/exclude semantics exactly. |
| `tests/` | Add or update tests listed in this spec. |
| `README.md`, `examples/` | Document operational commands, status fields, troubleshooting, Podman Postgres checks, and validation flows. |

## Daemon lifecycle contract

### Runtime files

The daemon uses configured paths from global config:

- `socket_path`
- `pid_path`
- `log_path`

Before attempting a daemon request, CLI code must run stale runtime cleanup equivalent to today's `cleanup_stale_runtime_files()`:

1. If `socket_path` exists but a Unix socket connection fails within a short timeout, unlink it.
2. If `pid_path` exists and the PID is absent, unlink it.
3. Do not unlink a live socket or PID owned by a running process.
4. Record cleanup facts in status/doctor payloads when available:
   - `socket_removed: true|false`
   - `pid_removed: true|false`
   - `reason: "connect_failed"|"pid_not_running"|...`

### Handshake and restart

`request_or_start()` must retain today's behavior:

1. Send handshake containing client version, protocol version, and global config mtime.
2. If the daemon responds with mismatched server version, protocol version, or global config mtime, stop it and start a replacement.
3. If no daemon responds, clean stale files and start one.
4. Wait for a matching handshake using a bounded retry window.
5. On failure, raise/report an error containing the configured daemon log path.

The handshake response must keep current fields and may add lifecycle fields:

```json
{
  "ok": true,
  "restart_required": false,
  "server_version": "0.1.0",
  "protocol_version": 1,
  "global_config_mtime": 12345,
  "schema_version": 1,
  "pipeline_version": "canonical-v1-ast-v1",
  "ranking_profile": "semantic_ast_v1",
  "lifecycle_state": "running"
}
```

### Lifecycle states

Report daemon lifecycle as an additive string field `lifecycle_state` under `daemon` status. Allowed values:

| State | Meaning | Required recovery/reporting |
| --- | --- | --- |
| `not_running` | No live socket/pid, or `--no-daemon` path. | Status still succeeds via direct backend status. Include socket/pid paths. |
| `starting` | CLI spawned daemon and is waiting for matching handshake. | Retry with bounded timeout. On failure include log path. |
| `running` | Handshake matches client version, protocol version, and config mtime. | Normal serving. |
| `restart_required` | Handshake mismatch was detected. | `request_or_start()` stops/restarts. Status may report last restart reason. |
| `degraded` | Daemon responds but one or more repos/backends/watchers/setup checks have errors. | Status identifies component, repo, error, and next command. |
| `stopping` | Stop request accepted or cleanup is running. | Stop live watchers, close resources, unlink socket/pid. |

### Stop behavior

`pi-code-index stop --json` must remain successful when no daemon is running. When a daemon is running:

1. Accept stop request and return `ok: true`, `stopping: true`.
2. Stop all live watchers before closing backend resources.
3. Close all cached CocoIndex resources.
4. Unlink socket and pid paths.
5. Do not delete logs.

## Request handling and metrics

The daemon is currently a single-process Unix socket server. Implementation may keep the current sequential request loop, but status must expose bounded metrics so operational bottlenecks are visible.

Required additive metrics under `daemon.performance`:

```json
{
  "uptime_seconds": 42.3,
  "requests": {
    "total": 17,
    "by_type": {"search": 10, "status": 4, "refresh": 1, "live_status": 2},
    "errors": 1
  },
  "durations_ms": {
    "last": 12.4,
    "average": 18.9,
    "max": 120.1
  }
}
```

Requirements:

- Track request counts by type.
- Track last/average/max durations with bounded memory. Running aggregates are sufficient.
- Track last error by component without storing unbounded logs.
- A slow refresh for one repo must not corrupt metrics for other request types.
- If concurrency is added later, refresh serialization must be per repo, not global.

## Resource cache contract

`BackendResourceCache` remains daemon-owned and keyed by repository plus backend configuration. Lexical backend does not need cached CocoIndex resources.

### Cache key ingredients

The cache key must include all fields that can change query/index semantics:

- resolved repo root
- selected backend name
- requested backend name
- redacted Postgres URL identity, with credentials excluded from human-readable status
- embedding model
- schema name
- table prefix
- compatibility table name
- global pipeline version
- branch mode
- current branch identity when branch-aware indexing is enabled
- feature gates: `enable_symbols`, `enable_references`, `enable_test_links`
- chunking: `chunk_strategy`, `ast_languages`, `chunk_size`, `min_chunk_size`, `chunk_overlap`, `max_ast_chunk_bytes`, `max_result_code_bytes`, `ast_context_lines`
- symbol/reference config: `symbol_languages`, `symbol_kinds`, `symbol_embedding_model`, graph limits, reference languages, minimum call edge confidence
- include/exclude globs

If a key ingredient changes, the next request must create/use a distinct resource entry. Old entries may remain until shutdown unless optional eviction is implemented.

### Status payload

Expose cache status under `daemon_resource_cache` with secrets redacted:

```json
{
  "daemon_resource_cache": {
    "entries": 2,
    "resources": [
      {
        "repo": "/repo/a",
        "backend": "cocoindex",
        "requested_backend": "auto",
        "postgres": {
          "host": "localhost",
          "port": 5432,
          "database": "cocoindex",
          "user": "cocoindex",
          "credentials_redacted": true
        },
        "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
        "schema_name": "public",
        "table_prefix": "pi_code_index",
        "table_name": "code_embeddings",
        "pipeline_version": "canonical-v1-ast-v1",
        "branch_mode": "current",
        "chunk_strategy": "hybrid",
        "features": {
          "symbols": true,
          "references": true,
          "test_links": false
        },
        "resources": {
          "postgres_pool": "warm",
          "embedder": "warm",
          "pool_creations": 1,
          "embedder_creations": 1,
          "closed": false
        },
        "last_used_at": "2026-06-22T00:00:00Z"
      }
    ]
  }
}
```

Do not include raw `POSTGRES_URL` with password in human output or JSON status. It is acceptable to include a stable redacted identity/fingerprint if needed for debugging.

### Optional eviction

Eviction is optional and additive. If implemented, add global config keys:

```yaml
daemon_max_cached_repos: null      # null means current no-eviction behavior
daemon_resource_idle_ttl_seconds: null
```

Defaults must preserve today's no-eviction behavior for small local use.

## Live watcher contract

Live mode remains daemon-supervised polling. It must work for lexical and CocoIndex backends through the existing backend router.

### Registry identity

`LiveWatcherRegistry` must keep one watcher per resolved repo root. Multi-repo status must not mix repo state.

### Start semantics

`pi-code-index live start --json`:

1. Requires daemon; `--no-daemon live ...` continues returning an error.
2. Resolves repo root using existing rules.
3. Creates or reuses a watcher for that repo.
4. Takes a baseline snapshot of files matching project include/exclude globs.
5. Does not force an immediate refresh by default.
6. Starts a daemon thread/task polling at the requested interval.
7. Returns live status including `running: true`, `watched_files`, and timestamps.

Additive future flag allowed: `--refresh-initial`. If implemented, it explicitly performs a refresh after baseline and reports `initial_refresh: true`.

### Polling and change detection

Watcher scan semantics:

- Use the same project config and `iter_files()` include/exclude behavior as lexical indexing.
- Snapshot each watched file by resolved path, mtime ns, and size.
- Treat created, modified, deleted, newly included, and newly excluded files as changes.
- Update `last_scan_started_at` before each scan and `last_scan_finished_at` after each scan.
- Update `watched_files` after successful scans.
- Do not crash the watcher on scan or refresh failures.

### Debounce and serialization

Implement bounded refresh coalescing per watcher:

- Add project/global config defaults or internal constants:
  - `live_refresh_debounce_seconds`: default `0.25`
  - `live_max_consecutive_errors_before_stale`: default `3`
  - `live_stale_after_seconds`: default `300`
- When rapid changes are detected, wait for the debounce window before refreshing.
- If more changes arrive during debounce or while refresh is running, perform at most one follow-up refresh.
- Never run overlapping refreshes for the same repo.
- Refresh serialization is per repo; one repo's refresh must not mark another repo stale.

### Refresh path

When a watcher refreshes:

- Call the existing backend router (`refresh(repo)` or equivalent) so lexical and CocoIndex behavior remain aligned.
- For CocoIndex, use the existing V1 catch-up/update path (`app.update_blocking()` through current backend code). Do not introduce non-V1 live runtime APIs.
- Record refresh result, duration, warning, and error.
- If `backend: auto` falls back to lexical, preserve warning and increment fallback counters.

### Error and stale semantics

A watcher failure must update status, not terminate the process.

Definitions:

- `consecutive_errors`: number of consecutive scan/refresh failures since last success.
- `stale: true` when any of these are true:
  - `consecutive_errors >= live_max_consecutive_errors_before_stale`
  - pending changes could not be refreshed within `live_stale_after_seconds`
  - last refresh failed and no later successful refresh happened
  - watcher thread/task is expected to run but is dead
- `stale_reason`: concise machine-readable reason, such as `refresh_failed`, `scan_failed`, `pending_too_long`, `watcher_dead`.

### Stop semantics

`pi-code-index live stop --json`:

- Stops and removes the watcher for the current repo if it exists.
- Returns the last known state plus `stopped: true` and `watcher_found: true` when a watcher existed and stopped cleanly.
- If no watcher existed, stop is idempotent and returns `running: false`, `stopped: true`, `watcher_found: false`, and the resolved repo path.
- Stop must join the watcher thread/task with a bounded timeout and report `stop_timeout: true` plus `stopped: false` if it cannot join cleanly.

### Live status payload

`pi-code-index live status --json` for current repo must return:

```json
{
  "ok": true,
  "live": {
    "repo": "/repo/a",
    "running": true,
    "poll_interval": 1.0,
    "watched_files": 312,
    "last_scan_started_at": "2026-06-22T00:00:00Z",
    "last_scan_finished_at": "2026-06-22T00:00:00Z",
    "last_update": "2026-06-22T00:00:01Z",
    "last_refresh": "2026-06-22T00:00:01Z",
    "last_refresh_duration_ms": 88.4,
    "refresh_count": 7,
    "debounced_refresh_count": 3,
    "pending_changes": false,
    "last_error": null,
    "consecutive_errors": 0,
    "stale": false,
    "stale_reason": null
  }
}
```

Additive aggregate command/flag allowed after current-repo status is stable:

```bash
pi-code-index live status --json --all
```

Aggregate shape:

```json
{"ok": true, "watchers": [{"repo": "/repo/a", "running": true}, {"repo": "/repo/b", "running": false}]}
```

## Freshness contract

### Status values

Use these freshness statuses everywhere:

| Status | Meaning |
| --- | --- |
| `current` | File was seen and indexed with current source hash and current pipeline version. |
| `pending` | Change detected but refresh has not completed. |
| `stale` | Indexed data exists but source hash, mtime/size, branch, or pipeline version no longer matches. |
| `deleted` | Previously indexed file no longer exists or no longer matches include/exclude globs. |
| `error` | Last indexing attempt failed for this file. Include a concise cause in metadata. |

### CocoIndex/Postgres source of truth

For CocoIndex mode, freshness comes from the canonical freshness table already defined by the final integration architecture. Status helpers in `coco_backend.py` must query by current `repo_id` and `branch_id` and return:

```json
{
  "freshness": {
    "source": "postgres",
    "repo_id": "...",
    "branch_id": "...",
    "pipeline_version": "canonical-v1-ast-v1",
    "counts": {
      "current": 100,
      "pending": 1,
      "stale": 2,
      "deleted": 0,
      "error": 1
    },
    "last_indexed_at": "2026-06-22T00:00:00Z",
    "oldest_pending_at": "2026-06-22T00:00:10Z",
    "latest_errors": [
      {"path": "src/example.py", "error": "parser failed", "updated_at": "2026-06-22T00:00:11Z"}
    ]
  }
}
```

`latest_errors` must be bounded, default maximum 5.

### Lexical synthesized freshness

For lexical mode, synthesize freshness from the local JSON index and current file scan until a richer lexical freshness table exists:

- `current`: indexed files still present with same mtime/size where metadata exists.
- `stale`: indexed files with changed mtime/size or missing metadata.
- `deleted`: indexed files no longer present or excluded.
- `pending`: live watcher has pending changes not yet refreshed.
- `error`: last lexical refresh error if known.

Mark synthesized payloads with `source: "lexical_index"`.

### Search metadata

Search results must keep required fields and may add metadata:

```json
{
  "score": 0.91,
  "filename": "src/pi_code_index/daemon.py",
  "start_line": 135,
  "end_line": 214,
  "code": "...",
  "metadata": {
    "backend": "cocoindex",
    "repo_id": "...",
    "branch_id": "...",
    "chunk_id": "...",
    "freshness_status": "current",
    "indexed_at": "2026-06-22T00:00:00Z",
    "pipeline_version": "canonical-v1-ast-v1"
  }
}
```

If metadata is absent, TypeScript formatting must still work.

## Status payload contract

`pi-code-index status --json` must succeed even when the daemon is not running or CocoIndex/Postgres is unavailable.

Top-level current-repo payload should include existing fields and additive structured fields:

```json
{
  "ok": true,
  "repo": "/repo/a",
  "index_path": "~/.pi-code-index/indexes/...json",
  "index_exists": true,
  "backend": {
    "ok": true,
    "backend": "cocoindex",
    "requested_backend": "auto",
    "backend_fallback": false
  },
  "socket_path": "~/.pi-code-index/daemon.sock",
  "socket_exists": true,
  "pid_path": "~/.pi-code-index/daemon.pid",
  "pid_exists": true,
  "client_version": "0.1.0",
  "protocol_version": 1,
  "global_config_mtime": 12345,
  "daemon": {
    "lifecycle_state": "running",
    "pid": 1234,
    "server_version": "0.1.0",
    "protocol_version": 1,
    "global_config_mtime": 12345,
    "socket_path": "~/.pi-code-index/daemon.sock",
    "pid_path": "~/.pi-code-index/daemon.pid",
    "log_path": "~/.pi-code-index/daemon.log",
    "daemon_resource_cache": {"entries": 1, "resources": []},
    "performance": {"uptime_seconds": 42.3}
  },
  "live": {"repo": "/repo/a", "running": false},
  "freshness": {"source": "postgres", "counts": {"current": 0, "pending": 0, "stale": 0, "deleted": 0, "error": 0}},
  "counts": {
    "files": 0,
    "chunks": 0,
    "symbols": 0,
    "references": 0,
    "call_edges": 0,
    "test_links": 0,
    "repo_hierarchy_nodes": 0
  },
  "capabilities": {
    "search": true,
    "symbols": false,
    "references": false,
    "graph": false,
    "quality_context": true,
    "live": true
  },
  "setup": {"checks": [], "summary": {"errors": 0, "warnings": 0}},
  "warnings": []
}
```

Compatibility rule: existing fields with different current shapes may remain during migration, but implementation tests must lock the new additive fields without breaking current consumers. If a field name currently contains a string (for example daemon status), place structured lifecycle data under a new compatible subkey rather than breaking callers, or update all in-repo callers/tests in the same implementation issue.

## Backend status and fallback contract

All backend operations should include, where practical:

- `backend`: actual backend used.
- `requested_backend`: requested backend before fallback.
- `backend_fallback`: boolean.
- `warning` or `warnings`: non-fatal fallback/setup warnings.
- `freshness_status` or `freshness` for status/search payloads.

When `backend: auto` falls back from CocoIndex to lexical:

```json
{
  "ok": true,
  "backend": "lexical",
  "requested_backend": "auto",
  "backend_fallback": true,
  "warning": "CocoIndex search unavailable; fell back to lexical: ..."
}
```

Status must not hide CocoIndex/Postgres failure behind lexical fallback. Include setup warning/check details so users can fix the richer backend.

## Setup checks and doctor contract

Implementation must expose setup/troubleshooting checks through either:

1. additive `setup` field in `status --json`, or
2. additive `pi-code-index doctor --json` command plus a summarized `setup` field in status.

If `doctor` is added, it must be additive and must not be required for normal search.

### Required checks

Each check result shape:

```json
{
  "id": "postgres.reachable",
  "ok": true,
  "severity": "error",
  "message": "Postgres is reachable",
  "details": {},
  "suggested_command": null
}
```

Allowed severities: `info`, `warning`, `error`.

Required check IDs:

| Check ID | Requirement | Severity when failing |
| --- | --- | --- |
| `tool.uv` | `uv` is installed and CLI can run through project environment. | error |
| `tool.node_npm` | Node/npm dependencies are available enough for extension execution. | warning |
| `python.import` | `import pi_code_index` succeeds. | error |
| `cli.help` | `pi-code-index --help` or `uv run pi-code-index --help` succeeds. | error |
| `config.global` | Global config path readable; parent writable. | error |
| `config.project` | Project settings path readable or creatable. | warning |
| `runtime.paths` | socket/pid/log parent directories writable. | error |
| `repo.root` | Current cwd resolves to git repo or usable directory root. | error |
| `globs.non_empty` | Include/exclude globs match at least one file. | warning |
| `backend.valid` | Backend is `auto`, `lexical`, or `cocoindex`. | error |
| `features.consistent` | Graph/test features are consistent with symbols/references/test-link gates. | warning |
| `daemon.runtime_stale` | Stale socket/pid files absent or cleaned. | warning |
| `cocoindex.optional_deps` | CocoIndex deps import when backend requires them. | error for `cocoindex`, warning for `auto` |
| `postgres.url` | Postgres URL is configured when CocoIndex is requested. | error for `cocoindex`, warning for `auto` |
| `postgres.reachable` | Postgres connection succeeds. | error for `cocoindex`, warning for `auto` |
| `postgres.pgvector` | `vector` extension exists or can be created. | error for `cocoindex`, warning for `auto` |
| `postgres.permissions` | Schema/table create/select/insert/update permissions are sufficient. | error for `cocoindex`, warning for `auto` |
| `postgres.canonical_tables` | Canonical tables/views exist after refresh or migration. | warning before first refresh, error after failed refresh |
| `cocoindex.version` | Installed CocoIndex is `>=1.0.0`. | error for `cocoindex`, warning for `auto` |

Do not run expensive refreshes from status/doctor unless an explicit future `--fix` or `--refresh` flag is introduced.

### Troubleshooting mapping

README and/or doctor output must map these symptoms to commands:

| Symptom | Suggested command |
| --- | --- |
| No results after edits | `pi-code-index live status --json` then `pi-code-index refresh --json` |
| Daemon will not start | `pi-code-index status --json` and inspect `~/.pi-code-index/daemon.log` |
| CocoIndex unavailable | `scripts/setup.sh --with-cocoindex --postgres-check` |
| Postgres not reachable | `podman ps` and `podman exec pi-code-index-postgres psql -U cocoindex -d cocoindex -c "SELECT 1"` |
| Graph tools empty | Enable symbols/references, refresh, then `pi-code-index symbols search --json "target"` |
| Status reports stale pipeline | `pi-code-index stop --json` then `pi-code-index refresh --json` |
| Stale socket/pid reported | `pi-code-index stop --json` then rerun original command |

## Configuration contract

Existing configs must remain valid. New keys are optional and must have safe defaults.

### Global config additions

Add only if used by implementation:

```yaml
# Daemon operations
daemon_request_timeout_seconds: 120.0
daemon_start_timeout_seconds: 4.0
daemon_handshake_retry_interval_seconds: 0.1
daemon_max_cached_repos: null
daemon_resource_idle_ttl_seconds: null

# Live indexing defaults
live_poll_interval_seconds: 1.0
live_refresh_debounce_seconds: 0.25
live_stale_after_seconds: 300
live_max_consecutive_errors_before_stale: 3

# Status/doctor output
setup_error_on_empty_globs: false
status_latest_errors_limit: 5
```

### Environment variables

Optional environment overrides may be added using the existing naming style:

- `PI_CODE_INDEX_DAEMON_REQUEST_TIMEOUT_SECONDS`
- `PI_CODE_INDEX_LIVE_POLL_INTERVAL_SECONDS`
- `PI_CODE_INDEX_LIVE_REFRESH_DEBOUNCE_SECONDS`
- `PI_CODE_INDEX_LIVE_STALE_AFTER_SECONDS`
- `PI_CODE_INDEX_LIVE_MAX_CONSECUTIVE_ERRORS_BEFORE_STALE`
- `PI_CODE_INDEX_STATUS_LATEST_ERRORS_LIMIT`

All numeric values must be validated with clear errors. Defaults must preserve existing behavior where possible.

## Operational UX

Preserve current commands:

```bash
pi-code-index status --json
pi-code-index refresh --json
pi-code-index live start --json
pi-code-index live status --json
pi-code-index live stop --json
pi-code-index stop --json
```

Allowed additive CLI commands/flags after core status is stable:

```bash
pi-code-index doctor --json
pi-code-index live status --json --all
pi-code-index status --json --all-repos
pi-code-index daemon resources --json
```

Allowed additive Pi commands after CLI support exists:

- `/code-index-live-status`
- `/code-index-live-start`
- `/code-index-live-stop`
- `/code-index-doctor`

Human text output should summarize only high-signal fields. JSON/details are authoritative for operations.

## Documentation requirements

Update docs/README/examples in the implementation issue to include:

- lifecycle states and what users should do for each state
- live mode semantics: baseline snapshot, polling, debounce, no initial refresh by default
- status/freshness field examples
- resource cache redaction behavior
- stale socket/pid recovery
- setup/doctor checks
- Podman Postgres startup and validation commands
- troubleshooting table from this spec
- no Docker commands

## Test requirements

Implementation must add/update tests before closing the implementation issue. Use unit tests for daemon/live behavior and optional integration tests for Postgres/CocoIndex.

### Daemon lifecycle tests

- Stale socket and dead PID are removed; live socket/PID are not removed.
- Handshake mismatch on version, protocol, or global config mtime triggers stop/start.
- Failed daemon start error includes log path.
- Stop request stops all watchers, closes resources, and removes runtime files.
- `status --json` succeeds when daemon is not running.
- `status --json` includes lifecycle state and runtime paths.
- Request metrics count successes/errors and durations by type.

Suggested file: `tests/test_daemon_lifecycle.py`.

### Resource cache tests

- Repeated CocoIndex requests for the same repo/config reuse one resource.
- Different repo roots produce different resource entries.
- Different schema/table/pipeline/feature/chunk/glob settings produce different keys.
- Status redacts Postgres credentials.
- Cache close calls resource close hooks and clears entries.
- Optional eviction preserves no-eviction default when config is unset.

Suggested file: `tests/test_daemon_lifecycle.py` or new `tests/test_resource_cache.py`.

### Live watcher tests

- `live start` takes baseline without immediate refresh by default.
- File create/modify/delete triggers exactly one refresh after debounce.
- Rapid edits are coalesced and increment debounce/coalescing counters.
- Refreshes for the same repo never overlap.
- Refresh failure increments `consecutive_errors`, records `last_error`, and eventually marks stale.
- A later successful refresh clears error/stale state.
- `live stop` returns last state with `stopped: true`.
- `live status --all` returns separate repo entries if implemented.

Suggested file: `tests/test_daemon_lifecycle.py` or new `tests/test_live_watcher.py`.

### Status/freshness/setup tests

- Lexical status includes synthesized freshness counts.
- CocoIndex status helper returns counts for files/chunks/symbols/references/call edges/test links/hierarchy nodes.
- Freshness latest errors are bounded.
- `backend: auto` fallback includes warning and does not hide setup failures in status.
- Setup checks return required IDs with correct severities for lexical, auto, and cocoindex modes.
- Empty glob match is warning by default.

Suggested files: `tests/test_cocoindex_postgres_integration.py`, `tests/test_indexer.py`, and a new `tests/test_setup_checks.py`.

### CLI/Pi compatibility tests

- Existing CLI commands and flags still parse.
- Existing TypeScript formatting tolerates optional metadata/status fields.
- Pi slash commands still shell out through `uv run --project <extension> pi-code-index ... --json`.
- No existing required JSON field is removed.

Suggested files: `tests/format-results.test.ts` and Python CLI tests.

## Validation commands

### Spec-only validation for this issue

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
git diff -- docs/architecture/daemon-live-ops-spec.md
git diff --name-only
```

### Future implementation validation

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

### CocoIndex/Postgres validation with Podman

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

Cleanup:

```bash
podman rm -f pi-code-index-postgres
```

## Implementation acceptance criteria

The later implementation issue is complete when:

- Existing CLI and Pi commands still work with the same required fields.
- Daemon lifecycle status reports allowed states and restart reasons.
- Stale socket/pid cleanup is observable and safe.
- CocoIndex resources are reused per repo/config and status redacts credentials.
- Live watcher status includes scan timings, debounce/coalescing counters, errors, and stale state.
- Live refreshes are serialized per repo and failures do not kill watchers.
- `status --json` succeeds without daemon and with unavailable CocoIndex/Postgres.
- Freshness counts are reported for lexical and CocoIndex modes.
- Setup checks or `doctor --json` cover all required check IDs.
- README/examples document Podman-based operations and troubleshooting.
- Tests listed above pass, including optional Postgres integration when the Podman container is running.

## Spec acceptance criteria

This spec is complete when it gives an implementation subagent exact guidance for:

- lifecycle behavior
- live watcher semantics
- resource cache behavior
- status/freshness payloads
- stale recovery
- setup/troubleshooting checks
- config additions
- tests
- validation commands
- acceptance criteria
