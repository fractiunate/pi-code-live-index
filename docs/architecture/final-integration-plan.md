# pi-code-index final integration plan

## Goal

Define the canonical architecture and data model for moving `pi-code-index` from the current lexical/CocoIndex prototype toward a structured repository intelligence index while preserving the existing Pi tool and CLI UX.

Current inspected state:

- `index.ts` registers the Pi `code_search` tool plus `/code-index-status`, `/code-index-refresh`, and `/code-index-stop`; it shells out through `uv run --project <extension> pi-code-index ...` and expects JSON.
- `src/pi_code_index/cli.py` owns the public CLI commands: `init`, `search`, `refresh`, `status`, `stop`, `live start|stop|status`, and hidden `daemon`.
- `src/pi_code_index/daemon.py` owns Unix-socket lifecycle, daemon resource caching, and polling live indexing.
- `src/pi_code_index/backend.py` routes between `lexical` and `cocoindex`, with `auto` choosing CocoIndex only when Postgres config is present.
- `src/pi_code_index/coco_backend.py` already uses CocoIndex V1 concepts: `coco.App`, `@coco.fn`, `@coco.lifespan`, `localfs.walk_dir`, `postgres.TableTarget`, and Postgres/pgvector.
- `src/pi_code_index/config.py` defines global/project config, backend selection inputs, table name, include/exclude globs, and chunking knobs.
- `tests/` currently covers lexical indexing, daemon lifecycle, CocoIndex/Postgres integration, and TypeScript result formatting.

## Target architecture

```text
Pi extension (`index.ts`)
  -> Python CLI (`src/pi_code_index/cli.py`)
  -> Unix socket daemon (`src/pi_code_index/daemon.py`)
  -> backend router (`src/pi_code_index/backend.py`)
  -> CocoIndex V1 app (`src/pi_code_index/coco_backend.py`)
  -> Postgres + pgvector
```

The daemon remains optional from the user's perspective. Existing command behavior stays intact:

- `code_search` still calls `pi-code-index search --json --top-k <n> [--refresh] <query>`.
- `/code-index-status`, `/code-index-refresh`, and `/code-index-stop` remain unchanged.
- CLI commands and flags remain backward-compatible.
- `backend: auto` continues to fall back to the lexical JSON backend when Postgres/CocoIndex is unavailable.
- The current result fields remain stable: `score`, `filename`, `start_line`, `end_line`, and `code`.

New information should be added through optional metadata fields only, so existing callers and tests continue to work.

## Canonical data model

Use Postgres as the durable query surface, with pgvector for embeddings and CocoIndex V1 for incremental source processing. Keep repository identity explicit on every table so multiple repositories can share one database.

### Tables

| Table | Purpose | Key fields |
| --- | --- | --- |
| `repos` | One row per indexed repository. | `repo_id`, `root_path`, `worktree_id`, `created_at`, `updated_at` |
| `branches` | Branch/head metadata for freshness and cross-branch analysis. | `branch_id`, `repo_id`, `name`, `head_sha`, `is_default`, `updated_at` |
| `files` | File-level metadata and content identity. | `file_id`, `repo_id`, `branch_id`, `path`, `language`, `sha256`, `mtime_ns`, `size_bytes`, `indexed_at` |
| `chunks` | Searchable text spans. This supersedes but remains compatible with current `code_embeddings`. | `chunk_id`, `file_id`, `repo_id`, `path`, `start_line`, `end_line`, `start_byte`, `end_byte`, `code`, `embedding vector`, `chunk_kind`, `symbol_id`, `metadata jsonb` |
| `symbols` | Definitions extracted from code. | `symbol_id`, `file_id`, `repo_id`, `name`, `qualified_name`, `kind`, `start_line`, `end_line`, `signature`, `metadata jsonb` |
| `references` | Symbol references/call sites/imports. | `reference_id`, `repo_id`, `file_id`, `symbol_id`, `name`, `kind`, `line`, `column`, `metadata jsonb` |
| `call_edges` | Caller/callee graph edges. | `edge_id`, `repo_id`, `caller_symbol_id`, `callee_symbol_id`, `confidence`, `source`, `metadata jsonb` |
| `repo_hierarchy` | Directory/package/module hierarchy. | `node_id`, `repo_id`, `parent_id`, `path`, `node_kind`, `name`, `metadata jsonb` |
| `test_links` | Test-to-source relationships. | `test_link_id`, `repo_id`, `test_file_id`, `source_file_id`, `test_symbol_id`, `source_symbol_id`, `confidence`, `metadata jsonb` |
| `freshness` | Incremental state and invalidation checks. | `freshness_id`, `repo_id`, `branch_id`, `file_id`, `source_hash`, `pipeline_version`, `last_seen_at`, `last_indexed_at`, `status`, `error` |

### Search API contract

Preserve the current `code_search` result contract:

```json
{
  "score": 0.93,
  "filename": "src/pi_code_index/config.py",
  "start_line": 1,
  "end_line": 30,
  "code": "..."
}
```

Add only optional fields under `metadata` or as nullable additions:

```json
{
  "score": 0.93,
  "filename": "src/pi_code_index/config.py",
  "start_line": 1,
  "end_line": 30,
  "code": "...",
  "metadata": {
    "backend": "cocoindex",
    "repo_id": "...",
    "branch": "main",
    "file_id": "...",
    "chunk_id": "...",
    "language": "python",
    "symbol": "load_global_config",
    "chunk_kind": "function",
    "freshness_status": "current"
  }
}
```

Existing formatter behavior in `index.ts` should continue to ignore unknown fields while exposing the full structured payload in details.

## Affected modules

- `index.ts`: preserve `code_search` parameters and formatted output. Later changes may surface optional metadata only in `details`, not in the default text format unless explicitly designed.
- `src/pi_code_index/cli.py`: keep command names/flags stable. Add future subcommands only if needed for diagnostics or migrations.
- `src/pi_code_index/daemon.py`: continue owning socket lifecycle, warm CocoIndex/Postgres resources, and live polling. Extend daemon status to include new table/freshness counts when available.
- `src/pi_code_index/backend.py`: remain the routing boundary for `auto`, `lexical`, and `cocoindex`. Add compatibility adapters here if the CocoIndex table layout changes.
- `src/pi_code_index/coco_backend.py`: primary implementation point for CocoIndex V1 flows, Postgres targets, pgvector indexes, table schemas, and search SQL.
- `src/pi_code_index/config.py`: add opt-in config keys for schema/table naming, branch handling, pipeline versioning, or feature flags; keep current defaults valid.
- `tests/`: update/add tests for compatibility, metadata, CocoIndex/Postgres schema creation, daemon status, fallback behavior, and TypeScript formatting.
- `examples/`: keep Podman-based Postgres commands and config examples aligned with any schema/config additions.

## Dependencies

Runtime/development dependencies remain based on the current `pyproject.toml`:

- Required: `pyyaml`.
- Optional CocoIndex backend: `cocoindex>=1.0.0`, `asyncpg>=0.29.0`, `sentence-transformers>=3.0.0`.
- Postgres with pgvector, launched with Podman for local development.
- Node/npm dependencies for the Pi extension and TypeScript tests.
- `uv` for Python environment and CLI execution.

Do not introduce Docker-based commands. Local database examples should use Podman, for example:

```bash
podman run -d \
  --name pi-code-index-postgres \
  -e POSTGRES_USER=cocoindex \
  -e POSTGRES_PASSWORD=cocoindex \
  -e POSTGRES_DB=cocoindex \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

## Rollout phases

1. **Contract lock**
   - Record the stable search payload fields and CLI commands.
   - Add compatibility tests proving unknown metadata does not break `index.ts` formatting.

2. **Schema foundation**
   - Introduce versioned Postgres table schemas behind the CocoIndex backend.
   - Keep reads compatible with the current `code_embeddings` shape or provide a compatibility view/query adapter.
   - Add `freshness` and `files` first, because they support safe incremental behavior.

3. **Chunk migration**
   - Move from the single embedding row model to canonical `files` + `chunks` rows.
   - Preserve `search()` output by mapping canonical chunk rows back to current result fields.

4. **Code intelligence layers**
   - Add `symbols`, `references`, `call_edges`, `repo_hierarchy`, and `test_links` as optional enrichment.
   - Store parser confidence/source in `metadata` so partial extraction does not block search.

5. **Daemon/status integration**
   - Extend daemon resource status and `status --json` with table counts, freshness status, and pipeline version.
   - Keep lexical fallback status unchanged.

6. **Documentation and examples**
   - Update README, `examples/global-config.yml`, `examples/project-settings.yml`, and Podman validation snippets.

## Risks and mitigations

- **CLI/tool contract breakage**: keep `score`, `filename`, `start_line`, `end_line`, and `code` mandatory; add metadata as optional only.
- **CocoIndex V1 API drift**: use only the V1 concepts already present in `coco_backend.py`; pin/validate `cocoindex>=1.0.0` behavior in integration tests.
- **Schema migration complexity**: version schemas and add compatibility views/adapters before switching search SQL.
- **Multi-repo data collision**: include `repo_id`/`repo` on all queryable tables and validate table/schema config names.
- **Branch/worktree ambiguity**: model branches explicitly and default to current repository root behavior until branch detection is implemented.
- **Daemon stale resources**: include schema/config/pipeline version in daemon resource cache keys before changing persistent layouts.
- **Index freshness errors**: centralize freshness status and last error in the `freshness` table and status payload.
- **Performance degradation**: add indexes on repo/path IDs, vector indexes on chunk embeddings, and bounded top-k candidate retrieval.
- **Local environment variance**: keep `backend: auto` lexical fallback and document Podman-only database commands.

## Validation commands

Planning-only validation for this issue:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
git diff -- docs/architecture/final-integration-plan.md
```

Future implementation validation should include:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
scripts/setup.sh
npm run typecheck
uv run pytest
uv run python -m compileall src tests
uv run pi-code-index --help
uv run pi-code-index --no-daemon search --json --refresh "where is config loaded"
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
uv run pi-code-index search --json --top-k 8 "where is config loaded"
podman exec pi-code-index-postgres psql -U cocoindex -d cocoindex \
  -c "SELECT count(*) AS chunks, count(DISTINCT filename) AS files FROM code_embeddings;"
```

## Open decisions before product-code changes

- Whether canonical tables live in one configurable schema or keep per-project table names.
- Whether to retain `code_embeddings` as a compatibility table/view or migrate directly to `chunks`.
- Which parser/extractor should populate `symbols`, `references`, and `call_edges` first.
- How branch identity should be derived in detached HEAD and worktree scenarios.
- How much optional metadata should be shown in the Pi text output versus only in structured details.
