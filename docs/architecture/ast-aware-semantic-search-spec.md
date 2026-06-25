# AST-aware semantic search foundation spec

## Scope

This specification converts `docs/architecture/ast-aware-semantic-search-plan.md` into buildable requirements for the implementation issue `pi-code-index-3jo.2.3`.

The implementation must add AST-aware indexing and ranking to the CocoIndex backend without changing the existing `code_search` CLI or Pi UX. This is a product-code implementation spec only; this document does not implement the feature.

Canonical architecture references:

- `docs/architecture/final-integration-plan.md`
- `docs/architecture/final-integration-spec.md`

Current code inspected while writing this spec:

- `index.ts`
- `src/pi_code_index/cli.py`
- `src/pi_code_index/backend.py`
- `src/pi_code_index/config.py`
- `src/pi_code_index/coco_backend.py`
- `src/pi_code_index/daemon.py`
- `src/pi_code_index/indexer.py`
- `tests/format-results.test.ts`
- `tests/test_canonical_foundation.py`
- `tests/test_cocoindex_postgres_integration.py`
- `tests/test_daemon_lifecycle.py`
- `tests/test_indexer.py`

## Non-goals

- Do not remove or rename existing Pi tool parameters, CLI commands, CLI flags, or required result fields.
- Do not change the default compact text shown by `index.ts` except for bug fixes required to keep existing behavior working.
- Do not require CocoIndex/Postgres for users on `backend: auto`; lexical fallback remains supported.
- Do not introduce non-CocoIndex-V1 pipeline concepts.
- Do not implement cross-repository or cross-branch search beyond the current repository/branch identity already modeled by the canonical schema.
- Do not require full symbol resolution before AST chunks can be searched.

## CocoIndex V1 usage boundary

Use only CocoIndex V1 concepts already accepted by the canonical architecture:

- `coco.App` and `coco.AppConfig`
- `@coco.fn` and `@coco.fn(memo=True)`
- `@coco.lifespan`
- `coco.ContextKey`
- `localfs.walk_dir`
- `coco.map`
- `coco.mount_each`
- `postgres.mount_table_target`
- `postgres.TableSchema.from_class`
- `TableTarget.declare_row`
- `TableTarget.declare_vector_index`
- Postgres targets plus idempotent `asyncpg` DDL where CocoIndex V1 table targets are insufficient

Do not use unreleased CocoIndex APIs, custom DSLs, or product concepts not present in CocoIndex V1.

## Stable public behavior

### Pi tool contract

`index.ts` must keep registering `code_search` with the same parameter contract:

```ts
{
  query: string;
  top_k?: number;
  refresh?: boolean;
}
```

The implementation may add optional fields to `details.cli_json`, but the compact text output must continue to show only:

- query header
- warning, when present
- total/displayed result count
- up to 8 displayed results
- each displayed result as `filename:start_line-end_line score=<number>` and a clipped code block
- final instruction to `read` or open returned ranges

Unknown result fields and `metadata` must remain ignored by the compact formatter. Existing TypeScript formatting tests must continue to pass.

### CLI contract

The following commands and flags must remain backward-compatible:

- `pi-code-index init`
- `pi-code-index search [--json] [--top-k N] [--refresh] [--repo PATH] [--no-daemon] QUERY`
- `pi-code-index refresh [--json] [--repo PATH] [--no-daemon]`
- `pi-code-index status [--json] [--repo PATH] [--no-daemon]`
- `pi-code-index stop [--json]`
- `pi-code-index live start|stop|status`
- hidden daemon command and protocol message types already used by `cli.py`/`daemon.py`

`search --json` must print the backend payload unchanged except for additive optional fields described below. Non-JSON search output must remain human-readable and must not require callers to understand AST metadata.

### Required search result fields

Every result, from every backend, must keep these required top-level fields:

```json
{
  "score": 0.0,
  "filename": "repo/relative/path.py",
  "start_line": 1,
  "end_line": 1,
  "code": "source snippet"
}
```

`score` must be numeric and sorted descending in the final `results` array. `filename` must be repo-relative POSIX style. Line numbers are 1-based and inclusive. `code` must match the returned line range as closely as the chunking strategy allows.

## Additive search payload contract

The CocoIndex backend must add structured details without breaking existing callers.

### Payload-level fields

CocoIndex `search --json` payloads must contain existing fields plus the following additive fields when available:

```json
{
  "ok": true,
  "backend": "cocoindex",
  "query": "where is config loaded",
  "top_k": 8,
  "refresh": false,
  "repo": "/absolute/repo/path",
  "results": [],
  "schema_version": 1,
  "pipeline_version": "canonical-v1-ast-v1",
  "repo_id": "<32 hex>",
  "branch": "main",
  "branch_id": "<32 hex>",
  "compatibility_mode": "ast|hybrid|recursive|legacy|fallback",
  "ranking_profile": "semantic_ast_v1",
  "truncated": false,
  "truncation": {
    "candidate_limit": 160,
    "result_code_bytes_limit": 12000,
    "omitted_candidates": 0
  },
  "warning": null
}
```

Rules:

- `compatibility_mode = "ast"` when all returned CocoIndex rows come from AST chunks.
- `compatibility_mode = "hybrid"` when returned rows may include AST chunks and recursive/text chunks.
- `compatibility_mode = "recursive"` when canonical chunks exist but AST extraction is disabled or unsupported and recursive chunks are the source of truth.
- `compatibility_mode = "legacy"` when reading the legacy `code_embeddings` table.
- `compatibility_mode = "fallback"` only for CocoIndex table-missing/error paths before backend-level lexical fallback occurs.
- `ranking_profile` must change when ranking formula semantics change.
- `truncated` is true when candidates or result code are clipped below what the backend could otherwise return.
- `warning` remains optional/null and must be human-readable.

### Result-level optional fields

Each CocoIndex result should include `metadata`. The top-level required fields remain authoritative for compatibility.

```json
{
  "score": 1.23456,
  "filename": "src/pi_code_index/config.py",
  "start_line": 25,
  "end_line": 75,
  "code": "...",
  "result_id": "<chunk_id or stable ranking result id>",
  "metadata": {
    "backend": "cocoindex",
    "schema_version": 1,
    "pipeline_version": "canonical-v1-ast-v1",
    "repo_id": "<32 hex>",
    "branch": "main",
    "branch_id": "<32 hex>",
    "head_sha": "<git sha or null>",
    "file_id": "<32 hex>",
    "chunk_id": "<32 hex>",
    "language": "python",
    "chunk_strategy": "ast",
    "chunk_kind": "function",
    "chunk_role": "primary",
    "symbol_id": "<32 hex or null>",
    "symbol": "load_global_config",
    "qualified_name": "pi_code_index.config.load_global_config",
    "symbol_kind": "function",
    "parent_symbol_id": "<32 hex or null>",
    "start_byte": 512,
    "end_byte": 2048,
    "definition_start_line": 25,
    "definition_end_line": 75,
    "context_start_line": 20,
    "context_end_line": 80,
    "freshness_status": "current",
    "lineage": {
      "source": "ast_parser",
      "parser": "python_ast",
      "parser_version": "py-ast-v1",
      "extractor_version": "ast-chunker-v1",
      "source_hash": "<sha256>",
      "generated_at": "<ISO-8601 or null>"
    },
    "ranking": {
      "semantic_score": 0.82,
      "lexical_score": 0.17,
      "symbol_score": 0.4,
      "path_score": 0.1,
      "freshness_penalty": 0.0,
      "final_score": 1.23456,
      "matched_tokens": ["config", "load"]
    },
    "truncation": {
      "code_truncated": false,
      "original_code_bytes": 1536,
      "returned_code_bytes": 1536,
      "max_result_code_bytes": 12000
    }
  }
}
```

Rules:

- `result_id` is optional but should equal `metadata.chunk_id` for chunk results.
- `metadata.backend` must be `cocoindex` for CocoIndex rows.
- `metadata.chunk_strategy` is one of `ast`, `recursive`, `hybrid`, or `legacy`.
- `metadata.chunk_kind` is one of the chunk kind values defined below.
- `metadata.freshness_status` is one of `current`, `stale`, `deleted`, `error`, `pending`, or `unknown`.
- `metadata.ranking.final_score` must equal top-level `score` before top-level rounding, or be rounded consistently with it.
- Metadata must be JSON-serializable and safe to pass through `index.ts` in `details.cli_json`.

## AST-aware chunking requirements

### Strategy values

Add a project config field:

```yaml
chunk_strategy: recursive | ast | hybrid
```

Default for the implementation issue: `recursive` unless the issue explicitly opts into a staged default change. This preserves existing behavior. Users can enable AST-aware behavior with `ast` or `hybrid`.

Semantics:

- `recursive`: keep current recursive splitter behavior and canonical metadata; no parser required.
- `ast`: use AST chunks for supported languages; for unsupported files, fall back per file to recursive chunks with metadata explaining the fallback.
- `hybrid`: prefer AST chunks for supported symbols and add recursive context chunks where AST chunks would miss meaningful file-level text.

Add a project/global override if needed by implementation:

```yaml
ast_languages: null        # null means all supported parser languages
max_ast_chunk_bytes: 12000
max_result_code_bytes: 12000
ast_context_lines: 3
```

Environment variables, if implemented, must be additive and named consistently:

- `PI_CODE_INDEX_CHUNK_STRATEGY`
- `PI_CODE_INDEX_AST_LANGUAGES` as comma-separated language names
- `PI_CODE_INDEX_MAX_AST_CHUNK_BYTES`
- `PI_CODE_INDEX_MAX_RESULT_CODE_BYTES`
- `PI_CODE_INDEX_AST_CONTEXT_LINES`

All numeric config values must be validated as positive integers. `chunk_strategy` must reject unknown values. `ast_languages` entries must be normalized lowercase and compared to detected language names.

### Supported languages for first implementation

Minimum first implementation:

- Python via the Python standard-library `ast` module.

Optional if low-risk:

- TypeScript/JavaScript through a parser dependency only if already present or explicitly added by the implementation issue.

For unsupported languages, unreadable files, parse errors, generated files, or oversized parser inputs, the indexer must keep indexing via recursive chunks and set metadata:

```json
{
  "chunk_strategy": "recursive",
  "ast_fallback_reason": "unsupported_language|parse_error|file_too_large|parser_unavailable|disabled"
}
```

### Chunk kind values

AST chunks must use these `chunk_kind` values where applicable:

- `module`: file-level module/documentation context
- `class`: class or equivalent type/container definition
- `function`: function, method, lambda body promoted to a named enclosing function when possible
- `method`: method when the parser can distinguish from free functions
- `decorator`: decorator block included only when useful as its own searchable context
- `import`: import block
- `docstring`: module/class/function docstring when stored as a separate context chunk
- `statement_block`: meaningful top-level statements not covered by a more specific symbol
- `text`: recursive fallback chunk

If a language cannot distinguish `function` and `method`, use `function` and set `metadata.symbol_kind` to the parser-specific kind if available.

### Chunk boundaries

AST chunk rows must satisfy:

- `start_line >= 1`
- `end_line >= start_line`
- `start_byte >= 0`
- `end_byte >= start_byte`
- `code` corresponds to `[start_byte:end_byte]` decoded from the original source text, adjusted only for valid UTF-8 replacement already used elsewhere in the project
- `start_line`/`end_line` correspond to the byte span after decoding
- decorators and signatures must be included in function/class chunks when the parser exposes their line spans
- leading comments immediately attached to a symbol may be included as context when this does not exceed `max_ast_chunk_bytes`
- nested functions/classes get their own chunks and may also appear inside the parent chunk unless doing so exceeds `max_ast_chunk_bytes`

Oversized AST nodes:

1. Prefer preserving the symbol signature/docstring as one AST chunk.
2. Split the body with the existing recursive splitter.
3. Mark body fragments with `chunk_strategy = "hybrid"`, `chunk_kind = "statement_block"`, `chunk_role = "body_fragment"`, and the parent `symbol_id`.

Small AST nodes:

- Nodes smaller than `min_chunk_size` may be merged with parent/module context in `hybrid` mode.
- In `ast` mode, small named symbols should still produce a chunk if they have a stable `symbol_id`, because symbol names are valuable for ranking.

### Symbol extraction

When `enable_symbols` is true or `chunk_strategy` is `ast`/`hybrid`, the implementation must populate the canonical `symbols` table for supported languages.

Python minimum fields:

- `symbol_id`
- `file_id`
- `repo_id`
- `branch_id`
- `name`
- `qualified_name`
- `kind`: `module`, `class`, `function`, or `method`
- `start_line`
- `end_line`
- `signature`, when available without executing code
- `docstring`, when available
- `metadata`, including parser/source details

`symbol_id` must follow the final integration spec identity rule:

```text
sha256("symbol\0" + file_id + "\0" + qualified_name + "\0" + kind + "\0" + start_line)[:32]
```

The chunk corresponding to a symbol must set `chunks.symbol_id` and duplicate useful human-readable fields in `chunks.metadata` for search payload construction.

### References, call edges, hierarchy, and test links

This foundation implementation may keep references, call edges, and test links behind existing feature flags:

- `enable_references`
- `enable_test_links`

Requirements:

- If a flag is false, status counts should report zero without error.
- If a flag is true but extraction is unsupported for a language, record no rows and include parser capability metadata or status diagnostics; do not fail refresh.
- `repo_hierarchy` should be safe to populate independently of AST parsing because directory/file hierarchy supports navigation and status.
- Unresolved references may be stored with `symbol_id = null`; unresolved call edges must not be created.

## Canonical storage requirements

The implementation must continue using the canonical table model from `final-integration-spec.md`:

- `repos`
- `branches`
- `files`
- `chunks`
- `symbols`
- `references`
- `call_edges`
- `repo_hierarchy`
- `test_links`
- `freshness`
- `schema_migrations`

### Pipeline/versioning

Add an AST pipeline version distinct from the existing canonical foundation version, for example:

```python
CANONICAL_PIPELINE_VERSION = "canonical-v1-ast-v1"
```

Alternatively, keep the constant name and change the default string only if migration compatibility is handled. The daemon resource cache key must include every config value that can change AST extraction or result scoring:

- `schema_name`
- `table_prefix`
- `table_name`
- `pipeline_version`
- `chunk_strategy`
- `ast_languages`
- `enable_symbols`
- `enable_references`
- `enable_test_links`
- `chunk_size`
- `min_chunk_size`
- `chunk_overlap`
- `max_ast_chunk_bytes`
- `max_result_code_bytes`
- `ast_context_lines`
- include/exclude globs
- embedding model
- Postgres URL

### Row lifecycle

A refresh must:

1. Upsert `repos` and `branches` for the current repo/branch.
2. Upsert a `files` row for each included file that can be read.
3. Delete or mark stale chunks/symbols/references for the same file and pipeline version that are no longer produced.
4. Insert/update AST or fallback chunks.
5. Insert/update symbols for supported files.
6. Insert/update `freshness` with `status = current` for successful files.
7. Insert/update `freshness` with `status = error` for files that fail parsing only when no fallback chunking can index them; otherwise use `current` plus fallback metadata.
8. Mark previously indexed files absent from the current walk as `deleted` or `stale` consistently with the existing freshness model.

Rows must remain scoped by `repo_id` and `branch_id`. Multi-repo data must not collide in shared Postgres.

### Compatibility table/view

Existing `ProjectConfig.table_name` (`code_embeddings` by default) must remain accepted. Implementation may:

- keep writing legacy rows temporarily,
- expose a compatibility view over canonical chunks, or
- keep legacy search read compatibility while canonical AST chunks are populated.

Regardless of internal choice, `backend: auto` and `search --json` behavior must not require users to migrate manually.

## Ranking requirements

The backend must combine semantic vector similarity with code-aware boosts. The implementation may tune weights, but it must expose components in `metadata.ranking` for CocoIndex results.

Minimum candidate sets:

1. Vector candidates from `chunks.embedding` ordered by cosine distance.
2. Lexical candidates from `path`, `code`, `symbol`, and `qualified_name` token matches.
3. Symbol candidates when AST metadata exists and the query matches symbol names or qualified names.

Final score requirements:

- Final result order must be descending by top-level `score`.
- Deduplicate by stable `chunk_id` first, then by `(filename, start_line, end_line, code)` for legacy/fallback rows.
- Prefer exact symbol/path token matches over semantically similar but unrelated body chunks when scores are otherwise close.
- Penalize `freshness_status` of `stale`, `deleted`, or `error` unless no current candidates exist.
- Do not return `deleted` chunks unless explicitly needed for diagnostics; normal search should exclude them.

Suggested formula for `semantic_ast_v1`:

```text
final_score = semantic_score
            + 0.50 * lexical_score
            + 0.35 * symbol_score
            + 0.15 * path_score
            + 0.10 * chunk_kind_boost
            - freshness_penalty
```

Suggested component ranges:

- `semantic_score`: cosine similarity transformed to higher-is-better, typically `1 - distance`
- `lexical_score`: 0.0 to 1.0 from existing token scoring
- `symbol_score`: 0.0 to 1.0; exact symbol/qualified-name matches should be highest
- `path_score`: 0.0 to 1.0
- `chunk_kind_boost`: 0.0 to 0.1, favoring `function`, `method`, `class`, then `module`, then fallback `text`
- `freshness_penalty`: 0.0 for current/unknown, larger for stale/error/deleted

The implementation must include tests that assert ranking components exist and that symbol-name queries can rank the owning function/class chunk above an unrelated text chunk with similar lexical content.

## Configuration additions

Add fields to `ProjectConfig` unless implementation proves a field must be global. Defaults must preserve current behavior.

```python
chunk_strategy: str = "recursive"
ast_languages: list[str] | None = None
max_ast_chunk_bytes: int = 12000
max_result_code_bytes: int = 12000
ast_context_lines: int = 3
```

Validation:

- `chunk_strategy` must be one of `recursive`, `ast`, `hybrid`.
- `ast_languages` must be null or a non-empty list of lowercase language names after normalization.
- byte/line limits must be positive integers.
- existing PostgreSQL identifier validation remains unchanged.
- `branch_mode` remains `current` for this foundation release.

Example project settings:

```yaml
backend: auto
table_name: code_embeddings
chunk_strategy: hybrid
ast_languages: [python]
max_ast_chunk_bytes: 12000
max_result_code_bytes: 12000
ast_context_lines: 3
enable_symbols: true
enable_references: false
enable_test_links: false
```

## Compatibility and fallback

### Backend selection

`src/pi_code_index/backend.py` behavior must remain:

- `backend: lexical` always uses lexical JSON.
- `backend: cocoindex` returns CocoIndex errors in JSON-safe form.
- `backend: auto` uses CocoIndex only when Postgres config is present and falls back to lexical on CocoIndex refresh/search/status failure.

### Per-file fallback

AST parsing failures must not fail the whole refresh when the file can still be chunked recursively.

Per-file fallback metadata must include:

```json
{
  "chunk_strategy": "recursive",
  "ast_fallback_reason": "parse_error",
  "parser_error": "short sanitized error message"
}
```

Parser error messages must be bounded and must not include huge source excerpts.

### Global fallback

If CocoIndex dependencies, Postgres, pgvector, or schema creation are unavailable:

- explicit `backend: cocoindex` returns a JSON-safe error, preserving current behavior;
- `backend: auto` falls back to lexical and includes `backend_fallback = true` plus a warning.

### Legacy compatibility

Search must continue to read legacy `code_embeddings` rows when canonical AST chunks are absent. Legacy rows should set metadata at least:

```json
{
  "backend": "cocoindex",
  "compatibility_mode": "legacy",
  "chunk_strategy": "legacy",
  "freshness_status": "unknown"
}
```

## Status and daemon requirements

### Status payload additions

`pi-code-index status --json` for CocoIndex should add or preserve:

```json
{
  "ok": true,
  "backend": "cocoindex",
  "repo": "/absolute/repo/path",
  "repo_id": "<32 hex>",
  "branch": "main",
  "branch_id": "<32 hex>",
  "table_name": "code_embeddings",
  "schema_name": "public",
  "table_prefix": "pi_code_index",
  "schema_version": 1,
  "pipeline_version": "canonical-v1-ast-v1",
  "chunk_strategy": "hybrid",
  "ast_languages": ["python"],
  "canonical_tables_exist": true,
  "repo_chunks": 123,
  "repo_files": 12,
  "counts": {
    "files": 12,
    "chunks": 123,
    "ast_chunks": 80,
    "recursive_chunks": 43,
    "symbols": 50,
    "references": 0,
    "call_edges": 0,
    "test_links": 0,
    "freshness_current": 12,
    "freshness_stale": 0,
    "freshness_error": 0,
    "parser_errors": 0
  },
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
  "live": false
}
```

Existing fields such as `repo_chunks`, `repo_files`, `table_exists`, and `counts` must remain safe for older callers.

### Daemon resource cache

`BackendResourceCache._key()` must include all new config that affects parsing, chunking, embedding, schema, or ranking. This avoids stale warm resources across config changes.

`daemon_metadata()` must continue returning protocol and version information. Additive metadata fields are allowed:

```json
{
  "schema_version": 1,
  "pipeline_version": "canonical-v1-ast-v1",
  "ranking_profile": "semantic_ast_v1"
}
```

Do not change daemon request type names or require a daemon restart for CLI compatibility beyond existing stale-config handling.

## Test requirements

### Unit tests

Add tests for config:

- defaults preserve current behavior: `chunk_strategy == "recursive"`, `ast_languages is None`, positive limits.
- invalid `chunk_strategy` is rejected.
- invalid numeric limits are rejected.
- `PI_CODE_INDEX_CHUNK_STRATEGY` and other environment overrides work if implemented.
- daemon resource cache keys differ when `chunk_strategy`, `ast_languages`, or AST limits change.

Add tests for AST extraction/chunking:

- Python function produces a `function` chunk with stable `chunk_id`, `symbol_id`, line/byte spans, and signature metadata.
- Python class with method produces class and method/function chunks with parent/qualified-name metadata.
- Decorated Python function includes decorator/signature lines.
- Nested function produces its own chunk and stable qualified name.
- Syntax error falls back to recursive chunks and records `ast_fallback_reason = "parse_error"`.
- Unsupported language falls back to recursive chunks and records `ast_fallback_reason = "unsupported_language"`.

Add tests for result contracts:

- `_rank_search_rows` preserves required fields and metadata.
- result metadata includes `ranking`, `lineage`, `truncation`, `chunk_strategy`, and freshness fields for canonical rows.
- `result_id` equals `chunk_id` for chunk results.
- result sorting is descending by final score.
- deduplication keeps one result per `chunk_id`.

Add TypeScript tests:

- `formatResults` ignores new `metadata`, `result_id`, payload-level `ranking_profile`, and `truncation` fields.
- compact text output remains unchanged for an equivalent payload.
- full structured payload remains available in `details.cli_json` through the existing tool path if test harness covers execution.

### Integration tests with Postgres/pgvector

Extend `tests/test_cocoindex_postgres_integration.py` or add a focused integration test gated the same way as existing CocoIndex/Postgres tests.

Minimum integration assertions:

1. Configure `chunk_strategy: hybrid` and `enable_symbols: true` for a temp repo with Python files.
2. Run `pi-code-index refresh --json` or direct backend refresh.
3. Assert canonical tables exist.
4. Assert `chunks` contains at least one AST chunk and, when fallback files exist, at least one recursive fallback chunk.
5. Assert `symbols` contains expected Python function/class rows.
6. Run `search --json --top-k 5 "function name or behavior query"`.
7. Assert required result fields exist.
8. Assert result metadata includes chunk, symbol, lineage, ranking, and freshness fields.
9. Assert `status --json` reports counts for `ast_chunks`, `recursive_chunks`, `symbols`, and parser errors.
10. Assert legacy compatibility mode still works when canonical AST chunks are absent or compatibility table is present from old refreshes.

### Fallback tests

- `backend: auto` with unavailable CocoIndex still falls back to lexical and sets `backend_fallback = true`.
- Explicit `backend: cocoindex` with unavailable Postgres returns JSON-safe error.
- Parse error in one file does not prevent other files from being indexed.
- Missing/deleted file updates freshness without crashing refresh.

## Validation commands

Run from `/home/fractiunate/.pi/agent/extensions/pi-code-index`.

Docs/spec validation for this issue:

```bash
git diff -- docs/architecture/ast-aware-semantic-search-spec.md
```

Baseline validation expected for the implementation issue:

```bash
scripts/setup.sh
npm run typecheck
npm test
uv run pytest
uv run python -m compileall src tests
uv run pi-code-index --help
uv run pi-code-index --no-daemon search --json --refresh "where is config loaded"
```

CocoIndex/Postgres validation must use Podman for local containers:

```bash
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
uv run pi-code-index search --json --top-k 8 "where is config loaded"
```

Suggested SQL checks after AST implementation:

```bash
podman exec pi-code-index-postgres psql -U cocoindex -d cocoindex -c \
  "SELECT chunk_kind, metadata->>'chunk_strategy' AS strategy, count(*) FROM pi_code_index_chunks GROUP BY 1, 2 ORDER BY 1, 2;"

podman exec pi-code-index-postgres psql -U cocoindex -d cocoindex -c \
  "SELECT kind, count(*) FROM pi_code_index_symbols GROUP BY kind ORDER BY kind;"

podman exec pi-code-index-postgres psql -U cocoindex -d cocoindex -c \
  "SELECT status, count(*) FROM pi_code_index_freshness GROUP BY status ORDER BY status;"
```

Cleanup:

```bash
podman rm -f pi-code-index-postgres
```

## Implementation acceptance criteria

The implementation issue is complete only when all of the following are true:

1. Existing CLI and Pi UX are preserved.
2. `backend: auto` lexical fallback still works without Postgres/CocoIndex.
3. New config fields exist, are validated, and default to preserving current behavior.
4. CocoIndex V1 app plumbing remains within the V1 usage boundary in this spec.
5. Python AST extraction creates stable symbols and AST chunks when enabled.
6. Unsupported languages and parse errors fall back per file to recursive chunks with bounded metadata.
7. Canonical `chunks` are searchable with metadata-rich results.
8. `symbols` rows are populated for supported languages when AST chunking or `enable_symbols` is active.
9. Ranking combines semantic, lexical, symbol/path, chunk-kind, and freshness components and exposes them in result metadata.
10. Search payloads keep required fields and add only optional fields.
11. `index.ts` compact formatting continues to ignore metadata while preserving full JSON in details.
12. Daemon cache keys include AST-affecting config values.
13. `status --json` includes AST chunk, recursive chunk, symbol, parser error, and freshness counts when available.
14. Unit tests cover config, parser/chunking, result contracts, ranking, daemon cache keys, and TypeScript formatting compatibility.
15. CocoIndex/Postgres integration tests cover refresh, status, search, canonical table rows, and fallback behavior.
16. Validation commands listed above pass in the appropriate environments.

## Implementation order recommendation

1. Add config fields and validation with tests.
2. Add pure Python AST extraction dataclasses/functions and unit tests, independent of CocoIndex.
3. Add AST chunk conversion to canonical row dataclasses and metadata contracts.
4. Wire AST extraction into the CocoIndex V1 file-processing path behind `chunk_strategy`.
5. Populate `symbols` and update canonical `chunks` directly, retaining legacy compatibility reads.
6. Extend search SQL and ranker to include AST metadata and ranking components.
7. Extend status/counts and daemon resource cache keys.
8. Add TypeScript compatibility tests.
9. Add Postgres integration tests and update examples if config examples change.
