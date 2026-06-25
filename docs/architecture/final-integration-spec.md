# pi-code-index final integration spec

## Scope

This document is the implementation contract for the canonical architecture and data model foundation. It refines `docs/architecture/final-integration-plan.md` into concrete, buildable requirements for the next implementation issue.

Implementation must preserve the existing Pi tool and CLI UX. Product changes are limited to adding canonical CocoIndex/Postgres data structures, compatibility adapters, optional metadata, and status/protocol enrichments. Existing lexical behavior remains a supported fallback.

## Current baseline to preserve

The current request path is:

```text
Pi extension index.ts
  -> uv run --project <extension> pi-code-index ...
  -> src/pi_code_index/cli.py
  -> optional Unix socket daemon in src/pi_code_index/daemon.py
  -> src/pi_code_index/backend.py
  -> lexical JSON index or src/pi_code_index/coco_backend.py
```

The following public UX is stable and must not be removed or renamed:

- Pi tool: `code_search` with parameters `{ query: string, top_k?: number, refresh?: boolean }`.
- Pi commands: `/code-index-status`, `/code-index-refresh`, `/code-index-stop`.
- CLI commands: `init`, `search`, `refresh`, `status`, `stop`, `live start`, `live stop`, `live status`, and hidden `daemon`.
- CLI flags currently used by the extension, especially `--json`, `--top-k`, `--refresh`, `--repo`, `--no-daemon`.
- Search result required fields: `score`, `filename`, `start_line`, `end_line`, `code`.
- `backend: auto` behavior: choose CocoIndex only when Postgres configuration is present; otherwise use lexical JSON.

Unknown fields in JSON payloads must remain safe for existing TypeScript formatting. The default text output in `index.ts` must continue to display only the compact filename/range/score/snippet summary unless a later UX issue explicitly changes it.

## Target architecture

```text
Pi extension (`index.ts`)
  -> Python CLI (`src/pi_code_index/cli.py`)
  -> Unix socket daemon (`src/pi_code_index/daemon.py`)
  -> backend router (`src/pi_code_index/backend.py`)
  -> canonical CocoIndex V1 app (`src/pi_code_index/coco_backend.py`)
  -> Postgres schema + pgvector
```

Responsibilities:

- `index.ts`: keep the Pi tool contract stable; pass through full JSON in `details.cli_json`; tolerate optional metadata on results.
- `cli.py`: keep command names/flags stable; serialize protocol-compatible JSON; add no required arguments.
- `daemon.py`: keep daemon optional; cache CocoIndex resources by repository, backend config, schema version, and pipeline version; enrich status payloads without changing existing fields.
- `backend.py`: remain the compatibility boundary for `auto`, `lexical`, and `cocoindex`; normalize canonical CocoIndex rows back into current search payloads.
- `coco_backend.py`: own CocoIndex V1 flow definitions, dataclasses, Postgres table targets, vector indexes, migrations/compatibility views, search SQL, and canonical status counts.
- `config.py`: load new optional config keys while keeping existing defaults valid.

Use CocoIndex V1 concepts already present in the repo only: `coco.App`, `coco.AppConfig`, `@coco.fn`, `@coco.lifespan`, `coco.ContextKey`, `localfs.walk_dir`, `postgres.mount_table_target`, `postgres.TableSchema.from_class`, `TableTarget.declare_row`, `TableTarget.declare_vector_index`, `coco.map`, and `coco.mount_each`.

## Stable identity model

Canonical tables must be safe for multiple repositories, branches, and worktrees sharing one Postgres database.

All IDs are lowercase hex SHA-256 prefixes unless otherwise specified. Use a 32-character prefix for row IDs to match the current `_chunk_id` style and keep IDs readable. If collision handling is later needed, implementation may expand to full 64-character SHA-256 without changing input material.

### ID input material

| ID | Input material | Notes |
| --- | --- | --- |
| `repo_id` | `sha256("repo\0" + repo_root_realpath)` | `repo_root_realpath` is `Path(repo).resolve()` as a POSIX string. This preserves current repo scoping by absolute root path. |
| `worktree_id` | `sha256("worktree\0" + repo_root_realpath + "\0" + git_common_dir_realpath_or_empty)` | If Git metadata is unavailable, `git_common_dir_realpath_or_empty` is empty. |
| `branch_id` | `sha256("branch\0" + repo_id + "\0" + branch_name + "\0" + head_sha_or_empty)` | Detached HEAD uses branch name `HEAD`. |
| `file_id` | `sha256("file\0" + repo_id + "\0" + branch_id + "\0" + path)` | `path` is repo-relative POSIX path. |
| `chunk_id` | `sha256("chunk\0" + file_id + "\0" + start_byte + "\0" + end_byte + "\0" + sha256(code))` | Stable across unchanged content and chunk boundaries. |
| `symbol_id` | `sha256("symbol\0" + file_id + "\0" + qualified_name + "\0" + kind + "\0" + start_line)` | Parser-specific ambiguity lives in metadata. |
| `reference_id` | `sha256("reference\0" + file_id + "\0" + name + "\0" + line + "\0" + column + "\0" + kind)` | References may be unresolved; `symbol_id` is nullable. |
| `edge_id` | `sha256("edge\0" + repo_id + "\0" + caller_symbol_id + "\0" + callee_symbol_id + "\0" + source)` | For unresolved calls, do not create an edge. |
| `node_id` | `sha256("hierarchy\0" + repo_id + "\0" + branch_id + "\0" + path + "\0" + node_kind)` | Root path is empty string. |
| `test_link_id` | `sha256("test_link\0" + repo_id + "\0" + test_file_id + "\0" + source_file_id + "\0" + coalesce(test_symbol_id) + "\0" + coalesce(source_symbol_id))` | Confidence records heuristic strength. |
| `freshness_id` | `sha256("freshness\0" + repo_id + "\0" + branch_id + "\0" + file_id + "\0" + pipeline_version)` | One row per file per pipeline version. |

The implementation must place `repo_id` on every queryable table, even when the row also references another table that contains `repo_id`.

## Canonical schema

### Naming and versioning

Add a schema version constant in Python, for example:

```python
CANONICAL_SCHEMA_VERSION = 1
CANONICAL_PIPELINE_VERSION = "canonical-v1"
```

The Postgres schema name and table prefix are configurable. Defaults must support the current single-table setup without requiring users to edit config:

- `schema_name`: default `public`.
- `table_prefix`: default `pi_code_index`.
- `compat_table_name`: default to current `ProjectConfig.table_name` (`code_embeddings`) for backward compatibility.
- Canonical tables are named `{table_prefix}_repos`, `{table_prefix}_branches`, `{table_prefix}_files`, `{table_prefix}_chunks`, and so on when `schema_name = public`.
- Existing `table_name` remains accepted and must still control the compatibility table/view name.

Qualified identifiers must be validated as PostgreSQL identifiers. Do not accept arbitrary SQL fragments in config. `schema_name`, `table_prefix`, and `table_name` must match `^[A-Za-z_][A-Za-z0-9_]*$`.

### SQL tables

The implementation may create tables via CocoIndex `postgres.TableTarget` where practical and via `asyncpg` DDL for metadata/compatibility objects where CocoIndex V1 target support is insufficient. All DDL must be idempotent.

#### `{prefix}_repos`

```sql
CREATE TABLE IF NOT EXISTS {schema}.{prefix}_repos (
  repo_id text PRIMARY KEY,
  root_path text NOT NULL,
  worktree_id text NOT NULL,
  vcs_kind text NOT NULL DEFAULT 'git',
  default_branch text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX IF NOT EXISTS {prefix}_repos_root_path_idx ON {schema}.{prefix}_repos(root_path);
```

#### `{prefix}_branches`

```sql
CREATE TABLE IF NOT EXISTS {schema}.{prefix}_branches (
  branch_id text PRIMARY KEY,
  repo_id text NOT NULL REFERENCES {schema}.{prefix}_repos(repo_id) ON DELETE CASCADE,
  name text NOT NULL,
  head_sha text,
  is_default boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(repo_id, name, coalesce(head_sha, ''))
);
CREATE INDEX IF NOT EXISTS {prefix}_branches_repo_idx ON {schema}.{prefix}_branches(repo_id);
```

If the `UNIQUE` expression is awkward for the target Postgres version, use a unique expression index instead.

#### `{prefix}_files`

```sql
CREATE TABLE IF NOT EXISTS {schema}.{prefix}_files (
  file_id text PRIMARY KEY,
  repo_id text NOT NULL REFERENCES {schema}.{prefix}_repos(repo_id) ON DELETE CASCADE,
  branch_id text NOT NULL REFERENCES {schema}.{prefix}_branches(branch_id) ON DELETE CASCADE,
  path text NOT NULL,
  language text,
  sha256 text NOT NULL,
  mtime_ns bigint,
  size_bytes bigint NOT NULL DEFAULT 0,
  indexed_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(repo_id, branch_id, path)
);
CREATE INDEX IF NOT EXISTS {prefix}_files_repo_path_idx ON {schema}.{prefix}_files(repo_id, path);
CREATE INDEX IF NOT EXISTS {prefix}_files_branch_idx ON {schema}.{prefix}_files(branch_id);
```

#### `{prefix}_chunks`

```sql
CREATE TABLE IF NOT EXISTS {schema}.{prefix}_chunks (
  chunk_id text PRIMARY KEY,
  file_id text NOT NULL REFERENCES {schema}.{prefix}_files(file_id) ON DELETE CASCADE,
  repo_id text NOT NULL REFERENCES {schema}.{prefix}_repos(repo_id) ON DELETE CASCADE,
  branch_id text NOT NULL REFERENCES {schema}.{prefix}_branches(branch_id) ON DELETE CASCADE,
  path text NOT NULL,
  start_line integer NOT NULL,
  end_line integer NOT NULL,
  start_byte integer NOT NULL,
  end_byte integer NOT NULL,
  code text NOT NULL,
  embedding vector NOT NULL,
  chunk_kind text NOT NULL DEFAULT 'text',
  symbol_id text,
  token_count integer,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (start_line >= 1),
  CHECK (end_line >= start_line),
  CHECK (start_byte >= 0),
  CHECK (end_byte >= start_byte)
);
CREATE INDEX IF NOT EXISTS {prefix}_chunks_repo_path_idx ON {schema}.{prefix}_chunks(repo_id, path);
CREATE INDEX IF NOT EXISTS {prefix}_chunks_file_idx ON {schema}.{prefix}_chunks(file_id);
CREATE INDEX IF NOT EXISTS {prefix}_chunks_branch_idx ON {schema}.{prefix}_chunks(branch_id);
```

A pgvector index is required on `embedding`. The current implementation calls `table.declare_vector_index(column="embedding")`; canonical implementation must do the equivalent for `{prefix}_chunks.embedding`.

#### `{prefix}_symbols`

```sql
CREATE TABLE IF NOT EXISTS {schema}.{prefix}_symbols (
  symbol_id text PRIMARY KEY,
  file_id text NOT NULL REFERENCES {schema}.{prefix}_files(file_id) ON DELETE CASCADE,
  repo_id text NOT NULL REFERENCES {schema}.{prefix}_repos(repo_id) ON DELETE CASCADE,
  branch_id text NOT NULL REFERENCES {schema}.{prefix}_branches(branch_id) ON DELETE CASCADE,
  name text NOT NULL,
  qualified_name text NOT NULL,
  kind text NOT NULL,
  start_line integer NOT NULL,
  end_line integer NOT NULL,
  signature text,
  docstring text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(file_id, qualified_name, kind, start_line)
);
CREATE INDEX IF NOT EXISTS {prefix}_symbols_repo_name_idx ON {schema}.{prefix}_symbols(repo_id, name);
CREATE INDEX IF NOT EXISTS {prefix}_symbols_qualified_idx ON {schema}.{prefix}_symbols(repo_id, qualified_name);
```

#### `{prefix}_references`

```sql
CREATE TABLE IF NOT EXISTS {schema}.{prefix}_references (
  reference_id text PRIMARY KEY,
  repo_id text NOT NULL REFERENCES {schema}.{prefix}_repos(repo_id) ON DELETE CASCADE,
  branch_id text NOT NULL REFERENCES {schema}.{prefix}_branches(branch_id) ON DELETE CASCADE,
  file_id text NOT NULL REFERENCES {schema}.{prefix}_files(file_id) ON DELETE CASCADE,
  symbol_id text REFERENCES {schema}.{prefix}_symbols(symbol_id) ON DELETE SET NULL,
  name text NOT NULL,
  kind text NOT NULL,
  line integer NOT NULL,
  column_number integer NOT NULL DEFAULT 0,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS {prefix}_references_repo_name_idx ON {schema}.{prefix}_references(repo_id, name);
CREATE INDEX IF NOT EXISTS {prefix}_references_symbol_idx ON {schema}.{prefix}_references(symbol_id);
```

#### `{prefix}_call_edges`

```sql
CREATE TABLE IF NOT EXISTS {schema}.{prefix}_call_edges (
  edge_id text PRIMARY KEY,
  repo_id text NOT NULL REFERENCES {schema}.{prefix}_repos(repo_id) ON DELETE CASCADE,
  branch_id text NOT NULL REFERENCES {schema}.{prefix}_branches(branch_id) ON DELETE CASCADE,
  caller_symbol_id text NOT NULL REFERENCES {schema}.{prefix}_symbols(symbol_id) ON DELETE CASCADE,
  callee_symbol_id text NOT NULL REFERENCES {schema}.{prefix}_symbols(symbol_id) ON DELETE CASCADE,
  confidence real NOT NULL DEFAULT 1.0,
  source text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CHECK (confidence >= 0.0 AND confidence <= 1.0)
);
CREATE INDEX IF NOT EXISTS {prefix}_call_edges_repo_idx ON {schema}.{prefix}_call_edges(repo_id);
CREATE INDEX IF NOT EXISTS {prefix}_call_edges_caller_idx ON {schema}.{prefix}_call_edges(caller_symbol_id);
CREATE INDEX IF NOT EXISTS {prefix}_call_edges_callee_idx ON {schema}.{prefix}_call_edges(callee_symbol_id);
```

#### `{prefix}_repo_hierarchy`

```sql
CREATE TABLE IF NOT EXISTS {schema}.{prefix}_repo_hierarchy (
  node_id text PRIMARY KEY,
  repo_id text NOT NULL REFERENCES {schema}.{prefix}_repos(repo_id) ON DELETE CASCADE,
  branch_id text NOT NULL REFERENCES {schema}.{prefix}_branches(branch_id) ON DELETE CASCADE,
  parent_id text REFERENCES {schema}.{prefix}_repo_hierarchy(node_id) ON DELETE CASCADE,
  path text NOT NULL,
  node_kind text NOT NULL,
  name text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(repo_id, branch_id, path, node_kind)
);
CREATE INDEX IF NOT EXISTS {prefix}_repo_hierarchy_parent_idx ON {schema}.{prefix}_repo_hierarchy(parent_id);
```

#### `{prefix}_test_links`

```sql
CREATE TABLE IF NOT EXISTS {schema}.{prefix}_test_links (
  test_link_id text PRIMARY KEY,
  repo_id text NOT NULL REFERENCES {schema}.{prefix}_repos(repo_id) ON DELETE CASCADE,
  branch_id text NOT NULL REFERENCES {schema}.{prefix}_branches(branch_id) ON DELETE CASCADE,
  test_file_id text NOT NULL REFERENCES {schema}.{prefix}_files(file_id) ON DELETE CASCADE,
  source_file_id text NOT NULL REFERENCES {schema}.{prefix}_files(file_id) ON DELETE CASCADE,
  test_symbol_id text REFERENCES {schema}.{prefix}_symbols(symbol_id) ON DELETE SET NULL,
  source_symbol_id text REFERENCES {schema}.{prefix}_symbols(symbol_id) ON DELETE SET NULL,
  confidence real NOT NULL DEFAULT 0.5,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CHECK (confidence >= 0.0 AND confidence <= 1.0)
);
CREATE INDEX IF NOT EXISTS {prefix}_test_links_repo_idx ON {schema}.{prefix}_test_links(repo_id);
CREATE INDEX IF NOT EXISTS {prefix}_test_links_source_idx ON {schema}.{prefix}_test_links(source_file_id);
```

#### `{prefix}_freshness`

```sql
CREATE TABLE IF NOT EXISTS {schema}.{prefix}_freshness (
  freshness_id text PRIMARY KEY,
  repo_id text NOT NULL REFERENCES {schema}.{prefix}_repos(repo_id) ON DELETE CASCADE,
  branch_id text NOT NULL REFERENCES {schema}.{prefix}_branches(branch_id) ON DELETE CASCADE,
  file_id text REFERENCES {schema}.{prefix}_files(file_id) ON DELETE CASCADE,
  source_hash text NOT NULL,
  pipeline_version text NOT NULL,
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  last_indexed_at timestamptz,
  status text NOT NULL,
  error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CHECK (status IN ('current', 'stale', 'deleted', 'error', 'pending'))
);
CREATE INDEX IF NOT EXISTS {prefix}_freshness_repo_status_idx ON {schema}.{prefix}_freshness(repo_id, status);
CREATE INDEX IF NOT EXISTS {prefix}_freshness_file_idx ON {schema}.{prefix}_freshness(file_id);
```

#### `{prefix}_schema_migrations`

A tiny metadata table is allowed and recommended:

```sql
CREATE TABLE IF NOT EXISTS {schema}.{prefix}_schema_migrations (
  version integer PRIMARY KEY,
  pipeline_version text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
```

Record version `1` once canonical tables and compatibility objects are ready.

## Canonical Python dataclasses

Implementation should replace or supplement the current `CodeEmbedding` dataclass with typed dataclasses corresponding to the canonical tables. Names below are normative for implementation clarity, but exact module placement is left to the implementer.

```python
@dataclass
class RepoRow:
    repo_id: str
    root_path: str
    worktree_id: str
    vcs_kind: str
    default_branch: str | None
    metadata: dict[str, object]

@dataclass
class BranchRow:
    branch_id: str
    repo_id: str
    name: str
    head_sha: str | None
    is_default: bool
    metadata: dict[str, object]

@dataclass
class FileRow:
    file_id: str
    repo_id: str
    branch_id: str
    path: str
    language: str | None
    sha256: str
    mtime_ns: int | None
    size_bytes: int
    metadata: dict[str, object]

@dataclass
class ChunkRow:
    chunk_id: str
    file_id: str
    repo_id: str
    branch_id: str
    path: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    code: str
    embedding: Annotated[NDArray, EMBEDDER]
    chunk_kind: str
    symbol_id: str | None
    token_count: int | None
    metadata: dict[str, object]

@dataclass
class SymbolRow:
    symbol_id: str
    file_id: str
    repo_id: str
    branch_id: str
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    signature: str | None
    docstring: str | None
    metadata: dict[str, object]

@dataclass
class ReferenceRow:
    reference_id: str
    repo_id: str
    branch_id: str
    file_id: str
    symbol_id: str | None
    name: str
    kind: str
    line: int
    column: int
    metadata: dict[str, object]

@dataclass
class CallEdgeRow:
    edge_id: str
    repo_id: str
    branch_id: str
    caller_symbol_id: str
    callee_symbol_id: str
    confidence: float
    source: str
    metadata: dict[str, object]

@dataclass
class RepoHierarchyRow:
    node_id: str
    repo_id: str
    branch_id: str
    parent_id: str | None
    path: str
    node_kind: str
    name: str
    metadata: dict[str, object]

@dataclass
class TestLinkRow:
    test_link_id: str
    repo_id: str
    branch_id: str
    test_file_id: str
    source_file_id: str
    test_symbol_id: str | None
    source_symbol_id: str | None
    confidence: float
    metadata: dict[str, object]

@dataclass
class FreshnessRow:
    freshness_id: str
    repo_id: str
    branch_id: str
    file_id: str | None
    source_hash: str
    pipeline_version: str
    status: Literal['current', 'stale', 'deleted', 'error', 'pending']
    error: str | None
    metadata: dict[str, object]
```

If CocoIndex V1 `TableSchema.from_class` cannot infer `jsonb` or nullable fields for a given dataclass, the implementation must either adapt those fields to supported CocoIndex types or create the table via explicit SQL, while keeping the SQL contract above.

## Config additions

Add optional fields to `GlobalConfig` and `ProjectConfig`. Existing config files lacking these keys must load unchanged.

```python
@dataclass
class GlobalConfig:
    backend: str = "auto"
    postgres_url: str = "postgres://cocoindex:cocoindex@localhost/cocoindex"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    socket_path: str = "~/.pi-code-index/daemon.sock"
    pid_path: str = "~/.pi-code-index/daemon.pid"
    log_path: str = "~/.pi-code-index/daemon.log"
    schema_name: str = "public"
    table_prefix: str = "pi_code_index"
    pipeline_version: str = "canonical-v1"

@dataclass
class ProjectConfig:
    backend: str = "auto"
    table_name: str = "code_embeddings"
    chunk_size: int = 1000
    min_chunk_size: int = 120
    chunk_overlap: int = 120
    include: list[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE))
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    schema_name: str | None = None
    table_prefix: str | None = None
    branch_mode: str = "current"
    compatibility_view: bool = True
    enable_symbols: bool = False
    enable_references: bool = False
    enable_test_links: bool = False
```

Effective values:

- Project `schema_name`/`table_prefix` override global values only when not `None`.
- `branch_mode = "current"` indexes the current branch/head only. Future values may include `all` but are not in this foundation scope.
- `compatibility_view = True` means create/update a compatibility table or view named by `table_name`.
- Symbol/reference/test enrichment flags default off for the foundation; their empty tables are still created.

Environment variables:

- Preserve existing `PI_CODE_INDEX_BACKEND`, `PI_CODE_INDEX_POSTGRES_URL`, `POSTGRES_URL`, and `PI_CODE_INDEX_EMBEDDING_MODEL`.
- Add optional `PI_CODE_INDEX_SCHEMA_NAME`, `PI_CODE_INDEX_TABLE_PREFIX`, and `PI_CODE_INDEX_PIPELINE_VERSION` overrides.

Update examples with Podman-only database commands. Do not introduce Docker commands.

## CocoIndex V1 flow behavior

The canonical app must continue walking files with `localfs.walk_dir(..., live=True)` and the configured include/exclude patterns.

For each file:

1. Resolve repo-relative POSIX `path`.
2. Read text.
3. Compute `sha256` of the full text.
4. Detect language with `detect_code_language(filename=path)` when available.
5. Upsert or declare a `FileRow`.
6. Split text using `RecursiveSplitter` and existing chunk knobs.
7. For each chunk meeting `min_chunk_size`, declare a `ChunkRow` with byte offsets, line ranges, embedding, `chunk_kind = "text"` unless a parser later provides a better value, and metadata containing at least:

```json
{
  "schema_version": 1,
  "pipeline_version": "canonical-v1",
  "source": "recursive_splitter",
  "language": "python"
}
```

Before processing chunks for a repo, ensure `RepoRow` and current `BranchRow` exist. Git metadata discovery should be best effort and must not fail indexing when Git is unavailable:

- Branch name: `git rev-parse --abbrev-ref HEAD`, fallback `HEAD`.
- Head SHA: `git rev-parse HEAD`, fallback `null`.
- Default branch: `git symbolic-ref refs/remotes/origin/HEAD` parsed to branch name, fallback `null`.
- Git common dir: `git rev-parse --git-common-dir`, fallback empty.

Use Python subprocess with short timeouts and no interactive prompts if git commands are needed.

## Compatibility and migration strategy

### Existing `code_embeddings` table compatibility

Current integration tests and users may have a table like:

```text
id, repo, filename, start_line, end_line, code, embedding
```

Canonical implementation must support both states:

1. Fresh canonical install: create canonical tables plus a compatibility view/table named by `ProjectConfig.table_name` (`code_embeddings` by default) exposing current columns:

```sql
CREATE OR REPLACE VIEW {schema}.{compat_table_name} AS
SELECT
  chunk_id AS id,
  root_path AS repo,
  path AS filename,
  start_line,
  end_line,
  code,
  embedding
FROM {schema}.{prefix}_chunks c
JOIN {schema}.{prefix}_repos r USING (repo_id);
```

If Postgres or CocoIndex cannot use a view as a target for the legacy flow, the implementation may instead maintain an actual compatibility table. Search must prefer canonical tables when they exist.

2. Existing legacy table only: search/status must continue to work by querying the old `table_name` layout. Refresh may migrate by creating canonical tables and repopulating from source files; it does not need to transform old rows if re-indexing is available.

### Search query compatibility

`coco_backend.search()` must use this order:

1. If canonical chunks table exists and has rows for `repo_id`/current branch, query it.
2. Else if legacy `table_name` exists and has rows for `repo = str(repo)`, query it using current SQL shape.
3. Else return successful empty payload with warning telling the user to run `pi-code-index refresh --json`.

### Schema migration execution

- Run idempotent migration checks before refresh and before canonical status.
- Search may lazily check for table existence but must avoid destructive DDL on the hot path except `CREATE EXTENSION IF NOT EXISTS vector`.
- Record `{prefix}_schema_migrations.version = 1` only after all required foundation tables and compatibility objects have been created.
- Do not drop legacy `table_name` objects in the foundation issue.

## Search result payload contract

Required result fields remain unchanged:

```json
{
  "score": 0.93,
  "filename": "src/pi_code_index/config.py",
  "start_line": 1,
  "end_line": 30,
  "code": "..."
}
```

Canonical CocoIndex results must add optional metadata, not top-level breaking fields:

```json
{
  "score": 0.93,
  "filename": "src/pi_code_index/config.py",
  "start_line": 1,
  "end_line": 30,
  "code": "...",
  "metadata": {
    "backend": "cocoindex",
    "schema_version": 1,
    "pipeline_version": "canonical-v1",
    "repo_id": "...",
    "branch_id": "...",
    "branch": "main",
    "head_sha": "...",
    "file_id": "...",
    "chunk_id": "...",
    "language": "python",
    "chunk_kind": "text",
    "symbol_id": null,
    "symbol": null,
    "freshness_status": "current"
  }
}
```

Top-level search payload remains:

```json
{
  "ok": true,
  "backend": "cocoindex",
  "query": "where is config loaded",
  "top_k": 8,
  "refresh": false,
  "repo": "/abs/repo",
  "results": [],
  "warning": null
}
```

Optional payload additions are allowed:

```json
{
  "schema_version": 1,
  "pipeline_version": "canonical-v1",
  "repo_id": "...",
  "branch": "main",
  "branch_id": "...",
  "compatibility_mode": "canonical|legacy|fallback"
}
```

## CLI and daemon protocol

The daemon protocol remains newline-delimited JSON over the existing Unix socket. Existing request shapes stay valid.

### Handshake request

Current request remains:

```json
{
  "type": "handshake",
  "client_version": "...",
  "protocol_version": 1,
  "global_config_mtime": 123
}
```

Response must retain current fields and may add canonical fields:

```json
{
  "ok": true,
  "server_version": "...",
  "protocol_version": 1,
  "global_config_mtime": 123,
  "schema_version": 1,
  "pipeline_version": "canonical-v1"
}
```

Do not bump `PROTOCOL_VERSION` solely for additive fields. Bump only if required fields or semantics change.

### Search request

```json
{
  "type": "search",
  "repo": "/abs/repo",
  "query": "where is config loaded",
  "top_k": 8,
  "refresh": false
}
```

Response is the search payload above.

### Refresh request

```json
{
  "type": "refresh",
  "repo": "/abs/repo"
}
```

Response must retain current fields and may add counts:

```json
{
  "ok": true,
  "backend": "cocoindex",
  "repo": "/abs/repo",
  "table_name": "code_embeddings",
  "schema_name": "public",
  "table_prefix": "pi_code_index",
  "schema_version": 1,
  "pipeline_version": "canonical-v1",
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
  "live": false,
  "counts": {
    "files": 42,
    "chunks": 210,
    "symbols": 0,
    "references": 0,
    "call_edges": 0,
    "test_links": 0,
    "freshness_current": 42,
    "freshness_error": 0
  },
  "message": "CocoIndex catch-up refresh complete"
}
```

### Status request

```json
{
  "type": "status",
  "repo": "/abs/repo"
}
```

Response must retain current lexical and CocoIndex fields. Canonical CocoIndex status should include:

```json
{
  "ok": true,
  "backend": "cocoindex",
  "requested_backend": "cocoindex",
  "repo": "/abs/repo",
  "repo_id": "...",
  "branch": "main",
  "branch_id": "...",
  "table_name": "code_embeddings",
  "schema_name": "public",
  "table_prefix": "pi_code_index",
  "schema_version": 1,
  "pipeline_version": "canonical-v1",
  "table_exists": true,
  "canonical_tables_exist": true,
  "repo_chunks": 210,
  "repo_files": 42,
  "counts": {
    "files": 42,
    "chunks": 210,
    "symbols": 0,
    "references": 0,
    "call_edges": 0,
    "test_links": 0,
    "freshness_current": 42,
    "freshness_stale": 0,
    "freshness_error": 0
  },
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
  "live": false
}
```

### Stop and live requests

No shape changes required. Live status may include canonical table counts if already cheap to compute, but must not block on heavy database work.

### Resource cache key

`BackendResourceCache._key()` must include these new values when present:

- effective schema name,
- effective table prefix,
- compatibility table name,
- pipeline version,
- branch mode,
- enrichment flags that affect output rows.

This prevents stale daemon resources after config/schema changes.

## Testing plan

Implementation issue must add or update tests in these areas.

### Unit tests

1. Config loading:
   - Existing configs without new keys still load with defaults.
   - New global/project config keys load correctly.
   - Environment overrides for schema/table prefix/pipeline version work.
   - Invalid identifiers are rejected before SQL construction.

2. Stable IDs:
   - Same repo/path/content yields same IDs across calls.
   - Different repo roots produce different `repo_id` and downstream IDs.
   - Different chunk byte ranges produce different `chunk_id`.

3. Search normalization:
   - Canonical row maps to required result fields plus optional `metadata`.
   - Legacy row maps to required result fields and either no metadata or `compatibility_mode: legacy`.
   - Ranking still returns at most `top_k`.

4. TypeScript formatter:
   - Results containing `metadata` format exactly like current compact output.
   - Full metadata remains available in `details.cli_json` when returned by the tool execution path.

5. Daemon protocol helpers:
   - Handshake still matches with additive schema fields.
   - Resource cache key changes when schema/table prefix/pipeline version changes.

### Integration tests with Postgres/pgvector

Use Podman to run Postgres locally. Do not document or require Docker.

```bash
podman run -d \
  --name pi-code-index-postgres \
  -e POSTGRES_USER=cocoindex \
  -e POSTGRES_PASSWORD=cocoindex \
  -e POSTGRES_DB=cocoindex \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

Required integration assertions:

1. `backend_refresh(repo)` with `backend: cocoindex` creates canonical tables and records schema migration version `1`.
2. `pi_code_index_chunks` contains rows for indexed files and includes `repo_id`, `file_id`, `branch_id`, byte offsets, line ranges, code, and embeddings.
3. Compatibility view/table named by `table_name` exposes current columns and allows existing legacy search SQL shape.
4. `backend_search()` returns required fields and metadata for canonical results.
5. Legacy-only table search path still works when canonical tables are absent.
6. `backend_status()` returns existing fields plus canonical counts.
7. `backend: auto` still falls back to lexical when Postgres/CocoIndex is unavailable.
8. Daemon-backed search and status return the same core payloads as `--no-daemon`.

### CLI smoke tests

Run after implementation:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
scripts/setup.sh
npm run typecheck
uv run pytest
uv run python -m compileall src tests
uv run pi-code-index --help
uv run pi-code-index init --help
uv run pi-code-index search --json --top-k 3 "where is config loaded"
uv run pi-code-index --no-daemon search --json --refresh "where is config loaded"
uv run pi-code-index status --json
uv run pi-code-index stop --json
```

CocoIndex/Postgres smoke test:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
export POSTGRES_URL=postgres://cocoindex:cocoindex@localhost/cocoindex
export PI_CODE_INDEX_BACKEND=cocoindex
export COCOINDEX_DB=.pi-code-index/cocoindex.db
scripts/setup.sh --with-cocoindex --postgres-check
uv run pi-code-index stop --json
uv run pi-code-index refresh --json
uv run pi-code-index status --json
uv run pi-code-index search --json --top-k 8 "where is config loaded"
podman exec pi-code-index-postgres psql -U cocoindex -d cocoindex \
  -c "SELECT count(*) FROM pi_code_index_chunks;"
podman exec pi-code-index-postgres psql -U cocoindex -d cocoindex \
  -c "SELECT version, pipeline_version FROM pi_code_index_schema_migrations ORDER BY version;"
```

## Implementation acceptance criteria

The implementation subagent may close the implementation issue only when all of the following are true:

1. Existing CLI commands and Pi tool parameters are unchanged.
2. Existing search result required fields remain present for lexical, legacy CocoIndex, and canonical CocoIndex results.
3. Canonical schema version `1` is created idempotently in Postgres with pgvector enabled.
4. Canonical `repos`, `branches`, `files`, `chunks`, `symbols`, `references`, `call_edges`, `repo_hierarchy`, `test_links`, `freshness`, and `schema_migrations` tables exist using the fields in this spec or a documented compatible equivalent.
5. All canonical queryable tables contain `repo_id`.
6. Stable ID helpers are deterministic and covered by tests.
7. Refresh populates at least `repos`, `branches`, `files`, `chunks`, and `freshness` for the current repo; enrichment tables may be empty behind disabled flags.
8. Search prefers canonical chunks, falls back to legacy `table_name`, and then returns an empty successful payload with a refresh warning.
9. Result metadata includes schema/pipeline/repo/file/chunk identity for canonical rows.
10. Status includes canonical table existence, counts, schema version, and pipeline version while preserving existing fields.
11. Daemon resource cache invalidates on schema/table prefix/pipeline version changes.
12. `backend: auto` lexical fallback behavior still works without Postgres/CocoIndex.
13. TypeScript formatter tests prove optional metadata does not alter compact output.
14. Integration tests use Podman-documented Postgres/pgvector commands.
15. `npm run typecheck`, `uv run pytest`, and `uv run python -m compileall src tests` pass in the implementation environment, with CocoIndex/Postgres tests skipped unless `POSTGRES_URL` is set.

## Non-goals for the foundation issue

- No redesign of Pi command names, CLI flags, or default text formatting.
- No mandatory daemon usage.
- No removal of lexical backend.
- No destructive migration or dropping of existing `code_embeddings` tables.
- No requirement to fully populate symbols/references/call graph/test links beyond empty canonical tables and optional disabled flags.
- No Docker commands; use Podman in docs and examples.
