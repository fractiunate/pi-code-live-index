# Symbol intelligence plan

## Scope

Planning-only document for issue `pi-code-index-3jo.3.1`. No product-code changes are required for this plan.

Target outcome for the implementation chain:

- Add symbol extraction/indexing for supported languages.
- Add Pi tools for `symbol_search`, definition lookup, and symbol-aware navigation.
- Allow Pi to find functions, classes, methods, modules, and packages by name or intent.
- Return precise repo-relative file, line range, symbol kind, language, and confidence/metadata.
- Preserve the existing `code_search` Pi UX and current CLI behavior. New tools and CLI subcommands are additive only.
- Use CocoIndex V1 concepts only: `coco.App`, `@coco.fn`, `@coco.fn(memo=True)`, `@coco.lifespan`, `coco.ContextKey`, `localfs.walk_dir`, `coco.map`, `coco.mount_each`, `postgres.mount_table_target`, `postgres.TableSchema.from_class`, `TableTarget.declare_row`, `TableTarget.declare_vector_index`, and explicit asyncpg DDL where table targets are insufficient.

## Current-state summary

Inspected modules and docs:

- `index.ts`
  - Registers only the `code_search` tool and `/code-index-status`, `/code-index-refresh`, `/code-index-stop` commands.
  - Shells out to `uv run --project <extension> pi-code-index search --json ...` in the active Pi cwd.
  - Formats compact search results from `score`, `filename`, `start_line`, `end_line`, and `code`; structured metadata remains in `details.cli_json`.
- `src/pi_code_index/cli.py`
  - Public commands are `init`, `search`, `refresh`, `status`, `stop`, `live start|stop|status`, plus hidden `daemon`.
  - No symbol-specific commands exist yet.
  - Auto-starts the daemon unless `--no-daemon` is used.
- `src/pi_code_index/daemon.py`
  - Owns Unix socket protocol, resource cache, live polling, and backend request dispatch.
  - Resource cache key already includes symbol/reference/test feature flags, AST language/chunking knobs, schema/table prefix, branch mode, and pipeline version.
  - Current request types do not include symbol lookup/navigation.
- `src/pi_code_index/backend.py`
  - Routes `lexical`, `cocoindex`, and `auto` with CocoIndex-to-lexical fallback in `auto` mode.
  - Exposes only `refresh`, `search`, and `status` operations.
- `src/pi_code_index/coco_backend.py`
  - Primary existing implementation point for CocoIndex/Postgres.
  - Already defines canonical dataclasses/tables for `symbols`, `references`, `call_edges`, `repo_hierarchy`, `test_links`, and `freshness`.
  - Current AST extraction uses Python `ast` only, derives `ExtractedSymbol` rows for classes/functions/methods, and stores symbol-aware chunk metadata when `chunk_strategy` is `ast` or `hybrid`.
  - Search ranking already uses symbol metadata as a boost for chunk search, but there is no public symbol query API.
- `src/pi_code_index/indexer.py`
  - Lexical JSON fallback chunks files and scores token cosine.
  - Has no symbol extraction or symbol index.
- `src/pi_code_index/config.py`
  - Defines `enable_symbols`, `enable_references`, `enable_test_links`, `chunk_strategy`, `ast_languages`, `max_ast_chunk_bytes`, `max_result_code_bytes`, and `ast_context_lines`.
  - Symbols are disabled by default via config and not exposed as a tool yet.
- Docs
  - `docs/architecture/final-integration-plan.md` defines canonical tables and compatibility rules.
  - `docs/architecture/ast-aware-semantic-search-plan.md` and spec define AST chunks and structured metadata.
  - This plan should build on those contracts rather than replacing them.

## Affected modules

### `index.ts`

Additive Pi surface:

1. Register `symbol_search`.
2. Register `definition_lookup` or `symbol_definition`.
3. Register a symbol-aware navigation tool, proposed as `symbol_context`.
4. Keep `code_search` unchanged and continue placing raw CLI JSON in `details.cli_json`.
5. Add formatters for symbol results that show compact lines such as:
   - `src/file.py:12-40 function load_config language=python score=0.912`
   - plus signature/docstring snippets when useful.
6. Add optional commands only if they improve manual UX:
   - `/code-index-symbol-search <query>`
   - `/code-index-definition <symbol-or-file:line>`

No existing command/tool names should be removed or repurposed.

### `src/pi_code_index/cli.py`

Additive command surface:

- `pi-code-index symbols search [--json] [--top-k N] [--kind KIND] [--language LANG] [--refresh] [--repo PATH] QUERY`
- `pi-code-index symbols definition [--json] [--repo PATH] TARGET`
- `pi-code-index symbols context [--json] [--repo PATH] [--depth N] TARGET`

`TARGET` should accept, in priority order:

1. Stable `symbol_id`.
2. `qualified_name` or `name`.
3. `repo-relative-file:line[:column]`.
4. A structured JSON string only for daemon-internal callers if needed.

`search`, `refresh`, `status`, and live commands remain backward-compatible.

### `src/pi_code_index/daemon.py`

Additive protocol request types:

- `symbol_search`
- `symbol_definition`
- `symbol_context`

Each request includes `repo`, query/target, `top_k` where relevant, filters, and `refresh`. Responses should include daemon metadata already used for search: `schema_version`, `pipeline_version`, `repo_id`, `branch`, `branch_id`, and warnings.

Status should add symbol-specific counts/errors when CocoIndex is active:

- `counts.symbols`
- `counts.symbols_by_language`
- `counts.symbols_by_kind`
- `counts.symbol_parser_errors`
- `counts.symbols_stale`

### `src/pi_code_index/backend.py`

Add routing functions without changing existing signatures:

- `symbol_search(repo, query, top_k=8, filters=None, refresh_first=False, coco_resources=None)`
- `symbol_definition(repo, target, filters=None, refresh_first=False, coco_resources=None)`
- `symbol_context(repo, target, depth=1, filters=None, refresh_first=False, coco_resources=None)`

`auto` behavior mirrors current search:

- If CocoIndex/Postgres is available, use canonical symbol tables.
- If CocoIndex fails in `auto`, return lexical fallback where possible, with `backend_fallback: true` and a warning.
- If a requested operation cannot be meaningfully supported by lexical fallback, return an empty successful payload with a warning rather than crashing.

### `src/pi_code_index/coco_backend.py`

Primary implementation point.

Planned work:

1. Stabilize symbol extraction as a memoized CocoIndex V1 function where inputs are file path, content/source hash, language, parser/extractor version, and relevant config.
2. Keep current Python AST extractor as the first supported language path.
3. Add parser adapters behind a small internal interface for TypeScript/JavaScript, Go, Rust, and Java only after dependency decisions are explicit.
4. Store extracted symbols in the canonical `symbols` table and connect `chunks.symbol_id` to the owning definition span.
5. Add symbol embeddings to support intent search. Either:
   - add `embedding vector` to the canonical `symbols` table, or
   - add a separate `symbol_embeddings` table keyed by `symbol_id`.
6. Rank symbol candidates with a weighted blend of exact name, qualified-name, fuzzy/token score, semantic intent score, kind/language filters, docstring/signature hits, and freshness penalty.
7. Implement point lookup by `symbol_id`, `qualified_name`, or file/line containment.
8. Implement context/navigation by returning owner symbol, parents, children, sibling symbols in the same file/module, and linked chunk IDs. References/callers remain future work unless `enable_references` is explicitly enabled.
9. Preserve existing `code_search` result mapping and metadata.

### `src/pi_code_index/config.py`

Additive config candidates:

- `enable_symbols: bool` should gate symbol extraction and symbol tool readiness. Rollout can default to `false`, then switch to `true` once validated.
- `symbol_languages: list[str] | null` may be added if symbol language support needs to differ from `ast_languages`.
- `symbol_kinds: list[str] | null` may constrain indexed symbol kinds for large repositories.
- `symbol_embedding_model: str | null` should default to the existing `embedding_model` unless there is a measured reason to split.
- `max_symbol_docstring_bytes` and `max_symbol_signature_bytes` can bound payload size.
- `symbol_parser_versions` can remain internal metadata unless users need overrides.

Existing config defaults must continue to load.

### `src/pi_code_index/indexer.py`

Lexical fallback options:

- Minimal fallback: no persistent symbol index; `symbol_search` scans loaded lexical chunks with language-aware regex patterns for supported simple definitions and returns best-effort rows with `backend: lexical`, `confidence: low`, and `fallback_reason: lexical_symbol_scan`.
- Safer initial fallback: return no results with warning `symbol intelligence requires CocoIndex/Postgres; use code_search fallback instead`.

Recommended rollout: start with safe empty fallback for definition/context, and best-effort lexical scan only for `symbol_search` after tests define its limitations.

### Tests

Add tests without weakening existing ones:

- TypeScript formatter tests for all new Pi tools, including unknown metadata tolerance and compact output limits.
- CLI parser tests for `symbols search`, `symbols definition`, and `symbols context` JSON payloads.
- Python unit tests for symbol ID stability, qualified-name construction, parent/child relationships, docstring/signature extraction, file/line containment lookup, and fallback warnings.
- CocoIndex/Postgres integration tests for symbol table population and symbol semantic search.
- Daemon tests for new request types, resource cache reuse, status counts, and stale daemon behavior.
- Regression tests proving `code_search` compact output and CLI flags remain unchanged.

### Docs/examples

Update after implementation:

- `README.md`: symbol tools, setup, backend requirements, examples.
- `examples/project-settings.yml`: any additive symbol config with defaults.
- Architecture specs: symbol contract and rollout status.

## Data model

Use the existing canonical table prefix/schemas.

### `symbols` table

Current fields are enough for definition lookup:

```text
symbol_id text primary key
file_id text
repo_id text
branch_id text
name text
qualified_name text
kind text
start_line integer
end_line integer
signature text null
docstring text null
metadata jsonb
```

Recommended metadata fields:

```json
{
  "language": "python",
  "parser": "python_ast",
  "parser_version": "py-ast-v1",
  "extractor_version": "symbol-extractor-v1",
  "parent_symbol_id": "...",
  "module": "pi_code_index.config",
  "visibility": "public|private|unknown",
  "decorators": [],
  "is_async": false,
  "source_hash": "sha256:...",
  "freshness_status": "current",
  "confidence": 1.0
}
```

### Symbol embeddings

Preferred contract for implementation:

```text
symbol_embedding_id text primary key
symbol_id text references symbols(symbol_id)
repo_id text
branch_id text
embedding vector
embedding_text text
metadata jsonb
```

`embedding_text` should be deterministic and bounded, for example:

```text
<kind> <qualified_name>
<signature>
<docstring first N bytes>
<owning module/path>
```

If implementation instead adds `embedding vector` directly to `symbols`, document the migration and keep lookup queries independent from embedding availability.

### Relationships for navigation

Initial navigation can use `metadata.parent_symbol_id` and file-level containment. A future normalized table can be added if JSON metadata becomes too slow:

```text
symbol_relations(relation_id, repo_id, branch_id, source_symbol_id, target_symbol_id, relation_kind, confidence, metadata)
```

Initial relation kinds:

- `parent`
- `child`
- `sibling`
- `module_member`
- later: `references`, `calls`, `imports`, `implements`, `overrides`

## API contracts

### `symbol_search` Pi/CLI payload

Request parameters:

```json
{
  "query": "config loader",
  "top_k": 8,
  "kind": "function|class|method|module|null",
  "language": "python|null",
  "refresh": false
}
```

Response:

```json
{
  "ok": true,
  "backend": "cocoindex",
  "operation": "symbol_search",
  "query": "config loader",
  "top_k": 8,
  "repo": "/absolute/repo",
  "repo_id": "...",
  "branch": "main",
  "branch_id": "...",
  "schema_version": 1,
  "pipeline_version": "canonical-v1-ast-v1",
  "results": [
    {
      "score": 0.94,
      "symbol_id": "...",
      "name": "load_project_config",
      "qualified_name": "pi_code_index.config.load_project_config",
      "kind": "function",
      "language": "python",
      "filename": "src/pi_code_index/config.py",
      "start_line": 132,
      "end_line": 158,
      "signature": "def load_project_config(repo: Path) -> ProjectConfig:",
      "docstring": null,
      "metadata": {
        "backend": "cocoindex",
        "file_id": "...",
        "chunk_id": "...",
        "parent_symbol_id": null,
        "freshness_status": "current",
        "ranking": {
          "exact_name_score": 0.0,
          "token_score": 0.38,
          "semantic_score": 0.72,
          "path_score": 0.12,
          "freshness_penalty": 0.0,
          "final_score": 0.94
        }
      }
    }
  ],
  "warning": null
}
```

### Definition lookup payload

Request target examples:

- `{ "symbol_id": "..." }`
- `{ "qualified_name": "pi_code_index.config.load_project_config" }`
- `{ "filename": "src/pi_code_index/config.py", "line": 132 }`

Response:

```json
{
  "ok": true,
  "backend": "cocoindex",
  "operation": "symbol_definition",
  "target": "load_project_config",
  "definition": {
    "symbol_id": "...",
    "name": "load_project_config",
    "qualified_name": "pi_code_index.config.load_project_config",
    "kind": "function",
    "language": "python",
    "filename": "src/pi_code_index/config.py",
    "start_line": 132,
    "end_line": 158,
    "start_byte": 4210,
    "end_byte": 4980,
    "signature": "def load_project_config(repo: Path) -> ProjectConfig:",
    "code": "...optional bounded snippet...",
    "metadata": {}
  },
  "matches": [],
  "warning": null
}
```

If a name is ambiguous, `definition` may be null and `matches` should contain ranked candidates with a warning asking the caller to choose a `symbol_id` or qualified name.

### Symbol context/navigation payload

Response shape:

```json
{
  "ok": true,
  "backend": "cocoindex",
  "operation": "symbol_context",
  "target_symbol_id": "...",
  "symbol": {},
  "parents": [],
  "children": [],
  "siblings": [],
  "module_symbols": [],
  "chunks": [],
  "references_available": false,
  "warning": null
}
```

All symbol list entries should reuse the `symbol_search` result item contract.

## Extraction/indexing strategy

### Phase 1: Python symbol baseline

- Use the current Python `ast` extractor as the baseline.
- Extract module, class, function, async function, and method symbols.
- Generate stable IDs from `repo_id`, `branch_id`, `file_id`, `qualified_name`, `kind`, and start line.
- Store parent symbol ID, docstring, signature, decorators, async/class metadata, and lineage.
- Connect AST chunks to owning `symbol_id`.
- Support lookup by exact `symbol_id`, exact/ILIKE `qualified_name`, exact name, and file/line containment.

### Phase 2: Intent search

- Build symbol embedding text from kind/name/signature/docstring/path.
- Use the same `SentenceTransformerEmbedder` and Postgres vector support as chunk search.
- Add lexical exact/prefix/fuzzy/token scoring on `name` and `qualified_name`.
- Use SQL candidate retrieval plus Python reranking, mirroring `_rank_search_rows`.

### Phase 3: Multi-language support

Supported language order should follow current include defaults and parser practicality:

1. Python: built-in `ast`, no extra dependency.
2. TypeScript/JavaScript: tree-sitter or language-specific parser, gated by dependency decision.
3. Go/Rust/Java: parser adapters only after tests and dependency footprint are accepted.

For unsupported languages, keep file/chunk search working and mark symbol capability as unavailable for that file.

### Phase 4: Navigation enrichment

- Parent/child/sibling/module navigation from symbol rows and metadata.
- Add references/imports only behind `enable_references` and preferably in the call-graph implementation chain.
- Do not block definition lookup on reference extraction.

## Fallback behavior

- `backend: lexical`:
  - `symbol_search`: either safe empty result with warning, or best-effort regex scan after dedicated tests.
  - `definition_lookup` and `symbol_context`: empty/ambiguous responses with warning recommending `code_search`.
- `backend: auto` and CocoIndex unavailable:
  - Use the same warning pattern as current `backend.py` fallback.
  - Preserve `ok: true` for safe empty fallback unless the request itself is invalid.
- Parser errors:
  - Record parser error/fallback in freshness or metadata.
  - Continue indexing recursive chunks.
  - Symbol tools exclude errored files unless best-effort fallback is explicitly implemented.
- Stale index:
  - Results include `freshness_status`.
  - `refresh: true` should refresh before symbol operations, matching current search behavior.

## Dependencies

Current dependencies remain:

- Required: `pyyaml`.
- Optional CocoIndex backend: `cocoindex>=1.0.0`, `asyncpg>=0.29.0`, `sentence-transformers>=3.0.0`.
- Postgres with pgvector for semantic symbol search.
- Node/npm and `typebox` for Pi tool schemas.
- `uv` for Python execution.

Potential future parser dependencies must be justified in the implementation spec before product-code changes. Local container validation must use Podman, not Docker.

## Risks and mitigations

- **Public UX breakage**: keep `code_search` unchanged; add new tools/commands only.
- **CocoIndex API drift**: use V1 concepts already present in this project and keep raw parsing in memoized Python functions.
- **Parser dependency bloat**: start with Python `ast`; require an explicit dependency decision for tree-sitter or language-specific parsers.
- **Ambiguous names**: return ranked candidates and require `symbol_id`/qualified name for exact definition when ambiguous.
- **Symbol ID churn**: document ID inputs and test stability. Accept start-line changes as a new definition identity unless a later migration adds content-near matching.
- **Index freshness**: include pipeline/parser versions in metadata and daemon cache keys; expose freshness warnings.
- **Large repos/performance**: add indexes on `(repo_id, name)`, `(repo_id, qualified_name)`, `(repo_id, branch_id, kind)`, and vector indexes if symbol embeddings are stored separately.
- **Partial language support**: expose capability/status counts so agents know when symbols are incomplete.
- **Overconfident fallback**: lexical fallback must mark `confidence: low` or return safe empty results.

## Rollout

1. Lock contracts in tests and docs.
2. Add backend symbol query functions over current Python symbol rows.
3. Add CLI `symbols` subcommands and daemon request types.
4. Add Pi tools and compact result formatting.
5. Add symbol embeddings and intent ranking.
6. Add status/capability reporting.
7. Evaluate parser dependencies for additional languages.
8. Consider enabling symbols by default only after performance and UX are validated.

## Validation commands

Planning-only validation for this issue:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
git diff -- docs/architecture/symbol-intelligence-plan.md
bd show pi-code-index-3jo.3.1 --json
```

Future implementation validation:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
scripts/setup.sh
npm run typecheck
npm run test:ts
uv run python -m compileall src tests
uv run pytest
uv run pi-code-index --help
uv run pi-code-index --no-daemon search --json --refresh "where is config loaded"
uv run pi-code-index --no-daemon symbols search --json --refresh "config loader"
uv run pi-code-index --no-daemon symbols definition --json "src/pi_code_index/config.py:132"
uv run pi-code-index --no-daemon symbols context --json "pi_code_index.config.load_project_config"
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
export PI_CODE_INDEX_CHUNK_STRATEGY=hybrid
export COCOINDEX_DB=.pi-code-index/cocoindex.db
scripts/setup.sh --with-cocoindex --postgres-check
uv run pi-code-index stop --json
uv run pi-code-index refresh --json
uv run pi-code-index status --json
uv run pi-code-index symbols search --json --top-k 8 "configuration loading"

podman exec pi-code-index-postgres psql -U cocoindex -d cocoindex \
  -c "SELECT kind, count(*) FROM pi_code_index_symbols GROUP BY kind ORDER BY kind;"
```

## Open decisions before product-code changes

- Should symbol embeddings live on `symbols` or in a separate `symbol_embeddings` table?
- Should `enable_symbols` default to true for Python-only extraction once the tool exists?
- Which parser dependency is acceptable for TypeScript/JavaScript support?
- Should the public Pi tool be named `definition_lookup`, `symbol_definition`, or both with one alias?
- How much source code should `definition_lookup` return by default versus only metadata and line range?
- Should lexical fallback attempt regex definition extraction, or always steer to `code_search`?
