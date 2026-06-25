# AST-aware semantic search foundation plan

## Scope

This is a planning-only document for upgrading `code_search` from recursive text chunks plus optional pgvector search into an AST-aware semantic search foundation. No product-code changes are required for this plan.

Target outcome:

- Preserve the existing CLI and Pi UX: `code_search` still returns compact text with `score`, `filename`, `start_line`, `end_line`, and `code` in each result.
- Add full structured details behind the compact Pi output: stable result IDs, AST-aware chunk metadata, lineage, freshness, ranking explanation, and truncation metadata.
- Use CocoIndex V1 concepts only: `coco.App`, `@coco.fn`, `@coco.fn(memo=True)`, `@coco.lifespan`, `localfs.walk_dir`, `coco.mount_each`/`coco.map`, `postgres.mount_table_target`, Postgres targets, and memoized Python functions.

## Current-state summary

Inspected modules:

- `index.ts`
  - Registers the Pi `code_search` tool and commands `/code-index-status`, `/code-index-refresh`, `/code-index-stop`.
  - Calls `uv run --project <extension> pi-code-index search --json --top-k <n> [--refresh] <query>`.
  - Formats compact text, caps displayed results at 8, clips snippets, and places the raw payload in `details.cli_json`.
  - Current formatter intentionally ignores optional `metadata` in compact output.
- `src/pi_code_index/cli.py`
  - Public command surface is `init`, `search`, `refresh`, `status`, `stop`, `live start|stop|status`, plus hidden `daemon`.
  - `search --json` prints backend payload JSON unchanged; non-JSON mode prints file ranges and code.
  - Auto-starts the daemon unless `--no-daemon` is used.
- `src/pi_code_index/daemon.py`
  - Owns Unix socket lifecycle, version/config handshake, live polling, and warm CocoIndex resources.
  - Resource cache keys already include schema/table/pipeline and chunking config.
- `src/pi_code_index/backend.py`
  - Routes `lexical`, `cocoindex`, and `auto`.
  - `auto` falls back to lexical if CocoIndex/Postgres is unavailable.
- `src/pi_code_index/indexer.py`
  - Lexical fallback chunks by line/character budget and scores with token cosine.
  - Result shape matches the public contract but has no semantic metadata.
- `src/pi_code_index/coco_backend.py`
  - Uses CocoIndex V1 app plumbing, `RecursiveSplitter`, `detect_code_language`, `SentenceTransformerEmbedder`, Postgres/pgvector, and canonical table helpers.
  - Already defines canonical rows for repos, branches, files, chunks, symbols, references, call edges, hierarchy, test links, and freshness.
  - Current indexing path writes a legacy `CodeEmbedding` table, then populates canonical tables from legacy rows.
  - Current search reads canonical chunks when present, otherwise legacy rows, ranks with semantic score plus lexical/token boosts, and returns optional `metadata`.
- `src/pi_code_index/config.py`
  - Defines backend, Postgres URL, embedding model, schema/table prefix, pipeline version, include/exclude globs, and chunk size knobs.
  - Has feature flags for symbols, references, and test links, currently disabled by default.
- Tests and docs
  - `tests/format-results.test.ts` asserts compact Pi output and metadata tolerance.
  - `tests/test_canonical_foundation.py` covers stable IDs, config, canonical metadata preservation, and daemon cache keys.
  - `tests/test_cocoindex_postgres_integration.py` validates refresh/search against Postgres when enabled.
  - `docs/architecture/final-integration-plan.md` defines the broader canonical architecture.

## Affected modules

### `src/pi_code_index/coco_backend.py`

Primary implementation point.

Planned changes:

1. Replace the legacy-first indexing flow with canonical AST-aware chunk rows as the source of truth.
2. Add parser/extractor functions as CocoIndex V1 Python functions, preferably memoized where file content and parser version determine output.
3. Populate `chunks` directly with AST-aware spans and richer metadata.
4. Populate `symbols`, `references`, `repo_hierarchy`, and later `call_edges`/`test_links` behind feature flags.
5. Keep legacy compatibility reads until migration is complete.
6. Extend search rows with stable result IDs, lineage/freshness fields, ranking components, and truncation metadata.

### `src/pi_code_index/config.py`

Additive config only, with current defaults valid.

Candidate fields:

- `chunk_strategy: "recursive" | "ast" | "hybrid"` defaulting initially to `"hybrid"` only after implementation is stable; during rollout keep existing behavior unless explicitly enabled.
- `ast_languages: list[str] | null` to scope AST extraction by language.
- `max_chunk_bytes`, `max_result_code_bytes`, or `max_ast_context_bytes` if runtime limits need to be configurable.
- Parser feature flags should remain opt-in at first: `enable_symbols`, `enable_references`, `enable_test_links` already exist.

### `src/pi_code_index/backend.py`

Keep as compatibility boundary.

Planned changes:

- Preserve fallback behavior for `auto`.
- Normalize optional metadata if CocoIndex and lexical payloads diverge.
- Do not change public `search(repo, query, top_k, refresh_first, coco_resources)` signature unless an additive internal adapter is needed.

### `src/pi_code_index/indexer.py`

Lexical fallback remains product-compatible and should not require AST parsing.

Planned changes:

- Optionally add metadata fields later only if useful for parity, such as `metadata.backend = "lexical"` and `metadata.freshness_status = "unknown"`.
- Do not block AST search on lexical parity.

### `src/pi_code_index/cli.py`

CLI surface should stay stable.

Planned changes:

- Keep `search --json --top-k --refresh <query>` unchanged.
- Keep non-JSON output simple and backward-compatible.
- Add diagnostic subcommands only if needed, for example `inspect-result <result_id>` or `status --json` fields, not required for the foundation.

### `src/pi_code_index/daemon.py`

Planned changes:

- Include any new AST/parser/pipeline config in `BackendResourceCache._key()` so stale daemons do not reuse incompatible resources.
- Extend status payloads with counts for AST chunks, parser errors, stale rows, and pipeline version.
- Keep daemon request types unchanged.

### `index.ts`

Preserve Pi UX.

Planned changes:

- Keep compact text format and truncation behavior.
- Extend TypeScript result types additively to accept `id`, `result_id`, `metadata`, `ranking`, `freshness`, `lineage`, and `truncation`.
- Keep full structured payload in `details` and continue exposing `details.cli_json`.
- Do not show dense AST metadata in default text unless explicitly designed later.

### Tests

Planned test additions:

- Python unit tests for stable AST chunk/result IDs and metadata contracts.
- Python unit tests for AST fallback to recursive splitting on unsupported languages or parser errors.
- CocoIndex/Postgres integration tests for canonical chunks, freshness, and search metadata.
- Daemon tests for resource cache key additions and status fields.
- TypeScript tests proving compact output is unchanged and structured details tolerate/add new fields.

### Docs/examples

Planned changes after implementation:

- Update README backend section with AST-aware behavior and metadata.
- Update `examples/project-settings.yml` only for additive opt-in settings.
- Keep Podman examples for Postgres/pgvector.

## Data and API contracts

### Public compact search result contract

These fields remain mandatory for every `code_search` result:

```json
{
  "score": 0.93,
  "filename": "src/pi_code_index/config.py",
  "start_line": 1,
  "end_line": 30,
  "code": "..."
}
```

### Additive structured result contract

CocoIndex-backed results should add optional fields without breaking current callers:

```json
{
  "id": "result:<stable-id>",
  "result_id": "result:<stable-id>",
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
    "chunk_kind": "function",
    "symbol_id": "...",
    "symbol": "load_global_config",
    "qualified_symbol": "pi_code_index.config.load_global_config",
    "freshness_status": "current"
  },
  "lineage": {
    "source": "localfs.walk_dir",
    "parser": "tree-sitter-python",
    "parser_version": "...",
    "chunker": "ast-aware-v1",
    "source_hash": "sha256:...",
    "content_hash": "sha256:...",
    "indexed_at": "2026-06-22T00:00:00Z"
  },
  "freshness": {
    "status": "current",
    "pipeline_version": "canonical-v1",
    "last_seen_at": "2026-06-22T00:00:00Z",
    "last_indexed_at": "2026-06-22T00:00:00Z",
    "error": null
  },
  "ranking": {
    "semantic_score": 0.71,
    "lexical_score": 0.12,
    "symbol_score": 0.10,
    "freshness_penalty": 0.0,
    "final_score": 0.93,
    "matched_tokens": ["config", "load"]
  },
  "truncation": {
    "code_truncated": false,
    "original_code_bytes": 840,
    "returned_code_bytes": 840
  }
}
```

`index.ts` should continue to display only compact information while preserving all structured fields in `details`.

### Top-level search payload contract

Keep current fields:

```json
{
  "ok": true,
  "backend": "cocoindex",
  "query": "where is config loaded",
  "top_k": 8,
  "refresh": false,
  "repo": "/repo",
  "results": []
}
```

Add optional fields:

```json
{
  "schema_version": 1,
  "pipeline_version": "canonical-v1",
  "repo_id": "...",
  "branch": "main",
  "branch_id": "...",
  "freshness_summary": {
    "current": 120,
    "stale": 0,
    "deleted": 0,
    "error": 0
  },
  "result_contract_version": 2,
  "details_truncated": false
}
```

## AST chunking strategy

### Goals

- Return chunks that align with code structure: functions, classes, methods, interfaces, types, modules, and meaningful markdown/config sections.
- Keep chunks stable across unrelated edits so result IDs and embeddings do not churn unnecessarily.
- Preserve enough context for Pi to know what to read next without flooding the compact output.
- Degrade gracefully to the current `RecursiveSplitter` behavior.

### Language detection

Use existing `detect_code_language(filename=...)` first. Then map detected language/file extension to a parser adapter. Unsupported languages should use recursive splitting and set:

```json
{"chunk_kind": "text", "metadata": {"ast_available": false, "fallback_reason": "unsupported_language"}}
```

### Parser adapter boundary

Introduce a small internal adapter layer in `coco_backend.py` or a future `ast_chunks.py` module. The adapter should accept `(path, text, language, parser_version)` and return plain Python dataclasses/dicts so CocoIndex V1 can memoize the function.

Candidate internal shape:

```python
@dataclass
class AstChunk:
    stable_path: str
    language: str | None
    chunk_kind: str
    symbol_name: str | None
    qualified_name: str | None
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    text: str
    context_before: str | None
    context_after: str | None
    metadata: dict[str, object]
```

### Chunk formation rules

1. Prefer one searchable chunk per function/method/class/interface/type when the AST node is within size bounds.
2. Include leading signature/decorator/comment/docstring context in the chunk when available.
3. If a symbol exceeds `chunk_size`, split within the symbol using `RecursiveSplitter`, but preserve `symbol_id`, `qualified_name`, and `parent_chunk_id` metadata.
4. If tiny sibling nodes are below `min_chunk_size`, group them by parent module/class to avoid low-value embeddings.
5. For markdown/config files, keep section/key-path chunks rather than forcing code AST metadata.
6. Always preserve byte and line ranges from source text.

### Stable IDs

- `repo_id`: existing `repo_id_for(repo)`.
- `branch_id`: existing `branch_id_for(repo_id, branch, head_sha)` for current branch mode.
- `file_id`: existing `file_id_for(repo_id, branch_id, path)`.
- `symbol_id`: stable hash of `file_id`, `qualified_name`, `kind`, and definition start byte. If a symbol moves, this may churn; this is acceptable for foundation, but the lineage should include `qualified_name` for cross-version matching.
- `chunk_id`: stable hash of `file_id`, AST node identity, byte range, chunk kind, and content hash. For fallback chunks, continue using byte range plus content hash.
- `result_id`: stable hash of `chunk_id`, query-normalized ranking version, and result contract version. If query-specific IDs are not needed, `result_id` can equal `chunk_id` in the first implementation.

## CocoIndex V1 pipeline plan

Use CocoIndex V1 primitives already present in the codebase.

1. Keep `build_app()` returning a `coco.App` with repository parameters.
2. Keep `localfs.walk_dir(..., live=True)` as source enumeration.
3. Replace or augment `process_file()`:
   - Read text once.
   - Compute file hash and metadata.
   - Detect language.
   - Call `extract_ast_chunks()` as `@coco.fn(memo=True, version=<parser-version>)`.
   - Fall back to `RecursiveSplitter` when extraction fails or is unsupported.
4. Map chunks with `coco.map()` or `coco.mount_each()` to declare rows into Postgres targets.
5. Mount canonical targets directly:
   - `repos`
   - `branches`
   - `files`
   - `chunks`
   - optionally `symbols`, `references`, `repo_hierarchy`, `freshness`
6. Keep embedding generation inside the chunk processing function with the existing `SentenceTransformerEmbedder` context.
7. Keep parser failures as data, not hard failures, where possible: declare freshness status `error` and fallback text chunks for the file.

Avoid introducing non-V1 concepts or an external pipeline DSL.

## Ranking plan

Current ranking is:

```text
semantic_score + 0.50 * lexical_score + 0.20 * token_matches
```

Planned ranking should remain deterministic and explainable:

1. Candidate retrieval
   - Retrieve vector candidates from `chunks.embedding` with pgvector.
   - Retrieve lexical candidates using path/code/symbol fields with bounded `ILIKE` or full-text search if later introduced.
   - Optionally boost exact symbol/name matches by querying `symbols` and joining to `chunks`.
2. Deduplication
   - Deduplicate by `chunk_id` first, then by `(filename, start_line, end_line, code)` for compatibility rows.
3. Score components
   - `semantic_score`: cosine similarity from pgvector.
   - `lexical_score`: existing token cosine over `path`, `qualified_name`, and `code`.
   - `symbol_score`: exact or fuzzy match against symbol names and qualified names.
   - `path_score`: query tokens matching path segments.
   - `freshness_penalty`: small penalty for stale rows; exclude deleted rows by default.
4. Output
   - Keep `score` as final score.
   - Add `ranking` object with components in structured results.

Initial weights should be conservative and covered by tests:

```text
final = semantic_score + 0.50 * lexical_score + 0.25 * symbol_score + 0.10 * path_score - freshness_penalty
```

## Freshness and lineage metadata

### Freshness

Use the existing `freshness` table as the source of truth:

- `source_hash`: file content hash.
- `pipeline_version`: global pipeline version.
- `last_seen_at`: latest source observation.
- `last_indexed_at`: latest successful indexing.
- `status`: `current`, `stale`, `deleted`, `error`, or `pending`.
- `error`: parser/indexing failure detail, bounded in length.

Search should:

- Include `freshness_status` in each result metadata.
- Exclude `deleted` rows by default.
- Warn when many results are stale or when the query uses fallback legacy mode.
- Include top-level `freshness_summary` when using canonical tables.

### Lineage

Each canonical chunk should carry enough lineage to explain where it came from:

- repository, branch, head SHA, file ID, chunk ID;
- source path, byte range, line range;
- parser/chunker name and version;
- AST node type and qualified symbol where applicable;
- source hash and content hash;
- fallback reason if AST extraction was not used.

Lineage belongs in structured details and row metadata, not compact Pi text.

## Truncation strategy

There are two truncation layers:

1. Backend/result-level truncation
   - Store full chunk text in Postgres where reasonable.
   - Return bounded `code` snippets if chunks exceed output limits.
   - Include `truncation.original_code_bytes`, `returned_code_bytes`, and `code_truncated`.
2. Pi display truncation in `index.ts`
   - Keep current compact display caps: max displayed results, max snippet chars, max text bytes.
   - Continue telling the model to use `read` for listed ranges.
   - Preserve full structured result data in `details` when the CLI returned it.

For oversized AST nodes, prefer splitting at indexing time over returning huge chunks.

## Fallback behavior

Required fallback paths:

- `backend: auto` with no Postgres URL: lexical JSON backend, unchanged.
- CocoIndex unavailable in `auto`: lexical fallback with warning, unchanged.
- AST parser unavailable: recursive chunking with `ast_available: false` metadata.
- AST parser error for one file: mark file freshness `error`, store parser error metadata, and fall back to recursive text chunks for that file when safe.
- Canonical table missing/empty: existing legacy compatibility search path remains until migration is complete.
- Daemon stale config: include new AST settings in resource key; handshake/config mtime restart remains unchanged.

## Dependencies

Current dependencies:

- Required Python: `pyyaml`.
- Optional CocoIndex: `cocoindex>=1.0.0`, `asyncpg>=0.29.0`, `sentence-transformers>=3.0.0`.
- Node: `typebox`; dev TypeScript tooling.
- Postgres with pgvector, started with Podman for local development.

Potential AST dependencies must be selected before implementation. Options:

- `tree-sitter` plus language packages for target languages.
- Language-specific stdlib parsers where available, e.g. Python `ast`, with tree-sitter for other languages.

Dependency constraints:

- Keep AST parser dependencies optional or feature-gated until the default install path is validated.
- Do not add cloud APIs.
- Do not use Docker commands in docs; use Podman.
- Do not introduce non-CocoIndex pipeline frameworks.

## Tests and validation plan

### Planning-only validation for this issue

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
git diff -- docs/architecture/ast-aware-semantic-search-plan.md
bd show pi-code-index-3jo.2.1 --json
```

### Future unit tests

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
uv run pytest tests/test_canonical_foundation.py
node --experimental-strip-types --test tests/format-results.test.ts
npm run typecheck
```

Add tests for:

- AST chunk formation for functions/classes/tiny nodes/oversized nodes.
- Stable `chunk_id` and `result_id` generation.
- Parser failure fallback metadata.
- Ranking component calculation and deterministic ordering.
- Compact TypeScript formatting ignores dense metadata.
- Structured details preserve full result fields.

### Future integration tests

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
uv run pi-code-index --no-daemon refresh --json
uv run pi-code-index --no-daemon search --json --top-k 8 "where is config loaded"
uv run pytest -m integration tests/test_cocoindex_postgres_integration.py
```

Suggested database checks:

```bash
podman exec pi-code-index-postgres psql -U cocoindex -d cocoindex \
  -c "SELECT count(*) AS chunks FROM pi_code_index_chunks;" \
  -c "SELECT chunk_kind, count(*) FROM pi_code_index_chunks GROUP BY chunk_kind;" \
  -c "SELECT status, count(*) FROM pi_code_index_freshness GROUP BY status;"
```

### Full future regression suite

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
scripts/setup.sh
uv run python -m compileall src tests
uv run pytest
npm run typecheck
npm run test:ts
uv run pi-code-index --help
uv run pi-code-index --no-daemon search --json --refresh "where is config loaded"
```

## Risks and mitigations

- CLI/Pi contract breakage
  - Mitigation: keep mandatory result fields unchanged; add fields only under optional objects; keep TypeScript formatter compact.
- Parser dependency weight or install failures
  - Mitigation: optional parser extras, feature flags, and recursive splitter fallback.
- AST chunk ID churn on code movement
  - Mitigation: include qualified symbol lineage and content hash; accept initial movement churn while preserving stable IDs for unchanged spans.
- Incorrect AST ranges
  - Mitigation: unit tests per language and fallback to recursive chunks on invalid ranges.
- Embedding quality regression from too-small chunks
  - Mitigation: group tiny sibling nodes and include signature/docstring/context.
- Ranking opacity
  - Mitigation: expose ranking components in structured details and test deterministic ordering.
- Stale or deleted rows appearing in search
  - Mitigation: join freshness in search SQL, exclude deleted rows, and report freshness summaries.
- Daemon serving incompatible resources after config changes
  - Mitigation: add AST/parser settings and pipeline version to cache key and status.
- Schema migration complexity
  - Mitigation: version schema and keep legacy compatibility mode until canonical direct-write indexing is proven.
- Performance on large repos
  - Mitigation: bounded candidate retrieval, indexes on repo/path/symbol/freshness, pgvector index, and chunk-size limits.

## Sequencing

1. Contract tests
   - Lock compact result shape and additive structured fields.
   - Add stable ID and ranking metadata tests.
2. Parser adapter prototype behind feature flag
   - Implement AST extraction for one language first, likely Python.
   - Recursive fallback for all other languages.
3. Canonical direct-write pipeline
   - Mount canonical Postgres targets and declare `files`, `chunks`, and `freshness` directly from CocoIndex V1 flow.
   - Keep legacy compatibility path.
4. Search contract expansion
   - Add `id/result_id`, `lineage`, `freshness`, `ranking`, and `truncation` fields.
   - Keep Pi compact output unchanged.
5. Status and daemon updates
   - Add AST/freshness counts and cache key fields.
6. Broaden language support and enrichment
   - Add `symbols`, then `references`, then call edges/test links behind existing feature flags.

## Open decisions before implementation

- Which AST parser dependency and language set to support first.
- Whether `result_id` should be query-specific or equal to `chunk_id` initially.
- Whether canonical direct-write should replace the legacy `CodeEmbedding` table immediately or continue dual-writing for one release.
- How much source context around AST nodes should be embedded versus stored only as metadata.
- Whether symbol/reference enrichment should be enabled by default once AST chunking is stable.
