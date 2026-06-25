# Symbol intelligence tools spec

## Scope

This specification converts `docs/architecture/symbol-intelligence-plan.md` into buildable requirements for issue `pi-code-index-3jo.3.2` and the following implementation issue(s).

The implementation must add symbol intelligence for CocoIndex-backed repositories while preserving the current CLI and Pi UX. Existing `code_search`, `search`, `refresh`, `status`, `stop`, `live`, daemon handshake behavior, and lexical fallback behavior remain backward-compatible. Product changes are additive only.

This document is a spec only. It does not implement product code.

## Architecture references

Implementers must keep these documents consistent:

- `docs/architecture/final-integration-spec.md`: canonical schema, identity, daemon compatibility, and stable `code_search` behavior.
- `docs/architecture/ast-aware-semantic-search-spec.md`: AST chunk metadata, ranking metadata, freshness, and compact formatter compatibility.
- `docs/architecture/symbol-intelligence-plan.md`: roadmap and rollout context.

## Current code inspected

The spec is based on the current repository state of:

- `index.ts`
  - Registers only `code_search` and `/code-index-status`, `/code-index-refresh`, `/code-index-stop`.
  - Uses `uv run --project <extension> pi-code-index ...` and stores raw CLI JSON in `details.cli_json`.
  - Has compact result formatting that ignores unknown fields.
- `src/pi_code_index/cli.py`
  - Exposes `init`, `search`, `refresh`, `status`, `stop`, `live start|stop|status`, and hidden `daemon`.
  - Has no `symbols` command group.
- `src/pi_code_index/daemon.py`
  - Handles request types `handshake`, `search`, `refresh`, `live_start`, `live_stop`, `live_status`, `status`, and `stop`.
  - Its CocoIndex resource cache key already includes symbol-related config flags.
- `src/pi_code_index/backend.py`
  - Routes `auto`, `lexical`, and `cocoindex` for `refresh`, `search`, and `status`.
  - Implements auto fallback from CocoIndex to lexical JSON.
- `src/pi_code_index/coco_backend.py`
  - Defines canonical dataclasses including `SymbolRow` and `ExtractedSymbol`.
  - Creates canonical tables including `{prefix}_symbols`, `{prefix}_references`, `{prefix}_call_edges`, `{prefix}_repo_hierarchy`, `{prefix}_test_links`, and `{prefix}_freshness`.
  - Implements Python AST extraction for classes/functions/methods and inserts symbols during AST population.
  - Does not expose public symbol search/definition/context operations.
- `src/pi_code_index/config.py`
  - Defines `enable_symbols`, `enable_references`, `enable_test_links`, `chunk_strategy`, `ast_languages`, `max_ast_chunk_bytes`, `max_result_code_bytes`, and `ast_context_lines`.
  - Symbols are disabled by default.
- `src/pi_code_index/indexer.py`
  - Provides lexical JSON fallback for chunk search only.
- `tests/`
  - Contains TypeScript formatter tests plus Python canonical foundation, CocoIndex/Postgres integration, daemon lifecycle, and lexical indexer tests.

## Non-goals

- Do not remove, rename, or repurpose existing Pi tools, commands, CLI flags, payload fields, or daemon request types.
- Do not change the default compact text produced for `code_search`.
- Do not require CocoIndex/Postgres for `backend: auto` users who currently rely on lexical JSON.
- Do not add multi-repository, cross-branch, reference resolution, call graph, inheritance, override, or import graph behavior unless it is explicitly behind `enable_references` and separately tested.
- Do not introduce parser dependencies for TypeScript/JavaScript, Go, Rust, or Java in the first implementation unless a separate dependency decision issue approves them.
- Do not use unreleased CocoIndex APIs or non-V1 concepts.

## CocoIndex V1 boundary

Use only CocoIndex V1 concepts already accepted by the project:

- `coco.App` and `coco.AppConfig`
- `@coco.fn`
- `@coco.fn(memo=True)`
- `@coco.lifespan`
- `coco.ContextKey`
- `localfs.walk_dir`
- `coco.map`
- `coco.mount_each`
- `postgres.mount_table_target`
- `postgres.TableSchema.from_class`
- `TableTarget.declare_row`
- `TableTarget.declare_vector_index`
- idempotent `asyncpg` DDL where CocoIndex V1 table targets are insufficient

Raw parsing may happen in memoized Python functions. Do not add a custom DSL or depend on CocoIndex APIs not listed above.

## Feature gates and rollout

### Required gates

- `ProjectConfig.enable_symbols` gates symbol extraction, symbol table population beyond already-required canonical DDL, and public tool readiness.
- `ProjectConfig.chunk_strategy in {"ast", "hybrid"}` controls AST chunking. Symbol operations must report a warning if symbols are enabled but no symbol rows exist because recursive chunking is active.
- `ProjectConfig.ast_languages` controls parser language allow-list. The first implementation supports Python only.

### Optional additive config

The implementation may add these fields only if needed by the implementation issue:

```python
symbol_languages: list[str] | None = None
symbol_kinds: list[str] | None = None
symbol_embedding_model: str | None = None
max_symbol_docstring_bytes: int = 4000
max_symbol_signature_bytes: int = 1000
```

Rules:

- `symbol_languages` defaults to `ast_languages`; when both are `None`, Python is allowed for the first release.
- `symbol_embedding_model` defaults to `GlobalConfig.embedding_model`.
- New integer limits must be positive and environment overrides, if added, must use the existing config validation pattern.
- Existing config files must continue loading unchanged.

### Default behavior

For the initial implementation, keep `enable_symbols: false` by default. Users can opt in with:

```yaml
enable_symbols: true
chunk_strategy: hybrid
ast_languages: [python]
```

The project may switch defaults only after a separate validation issue proves performance and UX are acceptable.

## Symbol extraction behavior

### Supported language baseline

Phase 1 supports Python via built-in `ast`. Unsupported languages must keep normal chunk indexing working and must not create misleading symbol rows.

### Extracted symbol kinds

For Python files, extract these rows:

| AST node | Symbol kind | Notes |
| --- | --- | --- |
| module file | `module` | Required as a lightweight row for navigation; may span line 1 through EOF or the module docstring span. |
| `ast.ClassDef` | `class` | Include decorators, bases if cheap, signature, docstring. |
| `ast.FunctionDef` at module scope | `function` | Include decorators, signature, docstring, async flag false. |
| `ast.AsyncFunctionDef` at module scope | `function` | Include decorators, signature, docstring, async flag true. |
| `ast.FunctionDef` or `ast.AsyncFunctionDef` nested directly/indirectly under a class | `method` | Include class parent lineage. |
| nested function not under class | `function` | Include parent function as parent symbol. |

Module rows are required even though the current extractor only emits module docstring chunks. They allow `symbol_context` to return module members consistently.

### Qualified names

Use deterministic qualified names:

- Python module name is the repo-relative path without suffix, with path separators replaced by `.`, and with leading `src.` removed only if the package root is detected by existing repo conventions. If package-root detection is not implemented, use the full path-derived module name and document that in metadata.
- Top-level class/function: `<module>.<name>`.
- Class method: `<module>.<ClassName>.<method_name>`.
- Nested function: `<module>.<outer>.<inner>`.

Do not use bare names as `qualified_name` except when module derivation fails; if that happens set `metadata.qualified_name_fallback = true`.

### Stable IDs

Use the canonical identity rules from `final-integration-spec.md`:

```text
symbol_id = sha256("symbol\0" + file_id + "\0" + qualified_name + "\0" + kind + "\0" + start_line)[:32]
```

A line move changes identity in the first release. Tests must lock this behavior.

### Required symbol metadata

Each `symbols.metadata` object must include:

```json
{
  "language": "python",
  "parser": "python_ast",
  "parser_version": "py-ast-v1",
  "extractor_version": "symbol-extractor-v1",
  "source_hash": "<sha256>",
  "module": "pi_code_index.config",
  "parent_symbol_id": null,
  "visibility": "public|private|unknown",
  "decorators": [],
  "is_async": false,
  "lineage": {
    "source": "ast_parser",
    "generated_at": "<ISO-8601 or null>"
  },
  "freshness_status": "current",
  "confidence": 1.0
}
```

Additional metadata may include `bases`, `returns`, `parameters`, `qualified_name_fallback`, and parser warnings. Unknown metadata fields must be tolerated by all callers.

### Signature and docstring extraction

- `signature` is the source text from `def`, `async def`, or `class` through the trailing `:` with decorators excluded.
- Decorators are stored separately in metadata and do not appear in `signature`.
- `docstring` uses `ast.get_docstring(node)` and is bounded by `max_symbol_docstring_bytes` if that config exists; otherwise it must be bounded to at most 4000 UTF-8 bytes.
- `signature` must be bounded to at most 1000 UTF-8 bytes.
- Bounds must not split invalid UTF-8.

### Parent/child relationships

- Every non-module symbol has `metadata.parent_symbol_id` when a containing class/function exists; top-level symbols use the module symbol ID as parent once module rows exist.
- Module symbols have `parent_symbol_id = null`.
- `symbol_context` computes children by querying rows whose `metadata.parent_symbol_id` equals the target symbol ID.
- Siblings are rows with the same parent symbol ID excluding the target.

### Parser errors and unsupported files

- A Python parse error must create/update a freshness row with `status = 'error'`, `error` set to a bounded parser message, and `metadata.parser = 'python_ast'`.
- Unsupported languages must not be freshness errors. They may have recursive chunks and freshness `current` with `metadata.symbols_supported = false`.
- Symbol tools exclude files with parser errors unless lexical fallback is explicitly used; payload warnings must report parser error counts when relevant.

## Data and schema requirements

### Existing canonical tables

Use the existing canonical tables from `final-integration-spec.md`, especially:

- `{prefix}_symbols`
- `{prefix}_chunks.symbol_id`
- `{prefix}_freshness`
- optional future `{prefix}_references` and `{prefix}_call_edges`

The implementation must not replace these with parallel incompatible tables.

### `{prefix}_symbols` required columns

The existing table is sufficient for definition lookup and navigation:

```sql
symbol_id text PRIMARY KEY,
file_id text NOT NULL,
repo_id text NOT NULL,
branch_id text NOT NULL,
name text NOT NULL,
qualified_name text NOT NULL,
kind text NOT NULL,
start_line integer NOT NULL,
end_line integer NOT NULL,
signature text,
docstring text,
metadata jsonb NOT NULL DEFAULT '{}'::jsonb
```

Add or verify indexes:

```sql
CREATE INDEX IF NOT EXISTS {prefix}_symbols_repo_name_idx ON {schema}.{prefix}_symbols(repo_id, name);
CREATE INDEX IF NOT EXISTS {prefix}_symbols_qualified_idx ON {schema}.{prefix}_symbols(repo_id, qualified_name);
CREATE INDEX IF NOT EXISTS {prefix}_symbols_repo_branch_kind_idx ON {schema}.{prefix}_symbols(repo_id, branch_id, kind);
CREATE INDEX IF NOT EXISTS {prefix}_symbols_file_range_idx ON {schema}.{prefix}_symbols(file_id, start_line, end_line);
```

### Symbol embeddings

Intent search requires symbol embeddings. Use a separate table to avoid changing the canonical `symbols` row shape and to allow operation when embeddings are missing:

```sql
CREATE TABLE IF NOT EXISTS {schema}.{prefix}_symbol_embeddings (
  symbol_embedding_id text PRIMARY KEY,
  symbol_id text NOT NULL REFERENCES {schema}.{prefix}_symbols(symbol_id) ON DELETE CASCADE,
  repo_id text NOT NULL REFERENCES {schema}.{prefix}_repos(repo_id) ON DELETE CASCADE,
  branch_id text NOT NULL REFERENCES {schema}.{prefix}_branches(branch_id) ON DELETE CASCADE,
  embedding vector NOT NULL,
  embedding_text text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(symbol_id)
);
CREATE INDEX IF NOT EXISTS {prefix}_symbol_embeddings_symbol_idx ON {schema}.{prefix}_symbol_embeddings(symbol_id);
CREATE INDEX IF NOT EXISTS {prefix}_symbol_embeddings_repo_branch_idx ON {schema}.{prefix}_symbol_embeddings(repo_id, branch_id);
```

Declare a pgvector index for `embedding` with `TableTarget.declare_vector_index` if implemented as a CocoIndex table target; otherwise create it with idempotent asyncpg DDL and ignore dimensionless-vector index errors the same way chunks currently do.

`symbol_embedding_id` input material:

```text
sha256("symbol_embedding\0" + symbol_id + "\0" + embedding_model + "\0" + sha256(embedding_text))[:32]
```

### Deterministic embedding text

Build bounded `embedding_text` exactly as:

```text
<kind> <qualified_name>
<signature or empty>
<docstring first max_symbol_docstring_bytes or empty>
<repo-relative filename>
```

Normalize line endings to `\n`, strip trailing whitespace on each line, and cap the final text to 8000 UTF-8 bytes.

### Chunk linkage

AST chunks that represent a symbol definition must set:

- `chunks.symbol_id = symbols.symbol_id`
- `chunks.metadata.symbol_id`
- `chunks.metadata.qualified_name`
- `chunks.metadata.symbol_kind`
- `chunks.metadata.definition_start_line`
- `chunks.metadata.definition_end_line`

Recursive fallback chunks use `symbol_id = null`.

## Backend API requirements

Add functions to `src/pi_code_index/backend.py` without changing existing function signatures:

```python
def symbol_search(repo: Path, query: str, top_k: int = 8, filters: dict[str, object] | None = None, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]: ...
def symbol_definition(repo: Path, target: object, filters: dict[str, object] | None = None, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]: ...
def symbol_context(repo: Path, target: object, depth: int = 1, filters: dict[str, object] | None = None, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]: ...
```

Routing rules:

- `backend: cocoindex`: call matching `coco_backend` function and return errors as JSON-safe `{ok:false}` payloads through the existing backend boundary pattern.
- `backend: auto`: if CocoIndex is available, use it; if it raises, use lexical fallback payloads with `backend = "lexical"`, `backend_fallback = true`, and a human-readable warning.
- `backend: lexical`: return safe fallback payloads. `symbol_search` may add best-effort regex only after tests define limitations; first implementation should return empty results with a warning.

Lexical fallback payloads must use `ok: true` for unsupported-but-safe operations and include warnings recommending `code_search`. Invalid requests still return `ok: false`.

## CocoIndex backend operations

Add functions to `src/pi_code_index/coco_backend.py`:

```python
def symbol_search(repo: Path, query: str, top_k: int = 8, filters: dict[str, object] | None = None, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]: ...
def symbol_definition(repo: Path, target: object, filters: dict[str, object] | None = None, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]: ...
def symbol_context(repo: Path, target: object, depth: int = 1, filters: dict[str, object] | None = None, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]: ...
```

Rules:

- `refresh_first` must call existing CocoIndex refresh before querying, matching `search` behavior.
- All queries are scoped by `repo_id` and `branch_id` from `repo_identity(repo)`.
- Results use repo-relative POSIX filenames from `{prefix}_files.path`.
- Payloads include `schema_version`, `pipeline_version`, `repo_id`, `branch`, `branch_id`, `backend`, `operation`, `warning`, and `capabilities`.
- If symbol tables do not exist, return `ok: true`, empty results, and warning `symbol tables are not available; run pi-code-index refresh with enable_symbols=true`.
- If symbols are disabled in project config, return `ok: true`, empty results, and warning `symbol intelligence is disabled; set enable_symbols: true`.

## Ranking behavior for `symbol_search`

### Candidate retrieval

Retrieve candidates from `{prefix}_symbols` joined to files and optionally symbol embeddings. Candidate pool should be at least `max(top_k * 20, 100)` and bounded at 500 by default.

Apply filters before ranking:

- `kind`: exact match. Accept `function`, `class`, `method`, `module`, or a list of these.
- `language`: exact match against `symbols.metadata->>'language'`.
- `filename`: optional repo-relative glob or exact path if provided by future callers.

### Scoring components

Return results sorted by descending `score`. Score must be deterministic and must include these normalized components in `metadata.ranking`:

```json
{
  "exact_name_score": 0.0,
  "qualified_name_score": 0.0,
  "token_score": 0.0,
  "semantic_score": 0.0,
  "signature_score": 0.0,
  "docstring_score": 0.0,
  "path_score": 0.0,
  "freshness_penalty": 0.0,
  "final_score": 0.0,
  "matched_tokens": []
}
```

Required scoring semantics:

- `exact_name_score = 1.0` if lowercased query equals lowercased `name`; `0.8` if query equals the final segment of `qualified_name`; otherwise `0`.
- `qualified_name_score = 1.0` for exact qualified-name match; `0.7` for prefix/substring match; otherwise `0`.
- `token_score` uses existing tokenization style over `name`, `qualified_name`, `signature`, and filename.
- `semantic_score` uses vector similarity when a symbol embedding exists; otherwise `0` and `metadata.ranking.semantic_unavailable = true`.
- `signature_score`, `docstring_score`, and `path_score` are lightweight token overlap boosts.
- `freshness_penalty` is `0` for current rows and positive for stale/error metadata.

Initial weights:

```text
final_score =
  2.0 * exact_name_score +
  1.5 * qualified_name_score +
  1.0 * token_score +
  1.2 * semantic_score +
  0.4 * signature_score +
  0.3 * docstring_score +
  0.2 * path_score -
  freshness_penalty
```

Clamp final scores to `>= 0`. Tests must lock ordering, not exact floating point internals beyond reasonable tolerances.

## Target parsing for definition/context

`TARGET` is accepted in priority order:

1. Structured object with `symbol_id`.
2. Structured object with `qualified_name`.
3. Structured object with `name`.
4. Structured object with `filename`, `line`, and optional `column`.
5. String that is an exact `symbol_id`.
6. String matching `repo-relative-file:line[:column]`.
7. String treated as `qualified_name` first, then as bare `name`.
8. JSON string with one of the structured object shapes above, for daemon-internal or advanced CLI callers.

Ambiguity rules:

- Exact `symbol_id` returns at most one definition.
- Exact `qualified_name` returns one definition if unique; if multiple rows match across generated/duplicate files, return `definition: null`, ranked `matches`, and warning.
- Bare `name` may be ambiguous; return `definition: null`, ranked `matches`, and warning unless exactly one current-row match exists.
- File/line lookup returns the smallest containing symbol span. If no symbol contains the line, return the module symbol if available; otherwise return null with warning.

## CLI contract

Add an additive `symbols` command group:

```text
pi-code-index symbols search [--json] [--top-k N] [--kind KIND] [--language LANG] [--refresh] [--repo PATH] QUERY
pi-code-index symbols definition [--json] [--refresh] [--repo PATH] TARGET
pi-code-index symbols context [--json] [--refresh] [--repo PATH] [--depth N] TARGET
```

Rules:

- Global `--no-daemon` remains supported exactly as for `search`.
- `--top-k` minimum is 1 and effective maximum is 50.
- `--kind` and `--language` may be repeated or comma-separated only if implemented and tested; otherwise accept one value.
- Non-JSON output is human-readable and compact:
  - search: `filename:start-end kind qualified_name language=<lang> score=<score>` plus optional signature line.
  - definition: one definition location plus signature and bounded snippet if present.
  - context: target plus parents/children/siblings/module symbols sections.
- JSON output prints the backend payload unchanged.
- Existing commands and flags must still appear and behave as before.

CLI direct mode examples:

```bash
uv run pi-code-index --no-daemon symbols search --json --top-k 8 --kind function "config loader"
uv run pi-code-index --no-daemon symbols definition --json "src/pi_code_index/config.py:132"
uv run pi-code-index --no-daemon symbols context --json --depth 2 "pi_code_index.config.load_project_config"
```

## Daemon protocol

Add request types:

### `symbol_search`

```json
{
  "type": "symbol_search",
  "repo": "/absolute/repo",
  "query": "config loader",
  "top_k": 8,
  "filters": {"kind": "function", "language": "python"},
  "refresh": false
}
```

### `symbol_definition`

```json
{
  "type": "symbol_definition",
  "repo": "/absolute/repo",
  "target": "src/pi_code_index/config.py:132",
  "filters": {"language": "python"},
  "refresh": false
}
```

### `symbol_context`

```json
{
  "type": "symbol_context",
  "repo": "/absolute/repo",
  "target": {"symbol_id": "<id>"},
  "depth": 1,
  "filters": {},
  "refresh": false
}
```

Rules:

- Handshake response shape is unchanged except additive metadata is allowed.
- Resource cache reuse must pass `resource_cache.get(repo)` to backend symbol operations.
- Unknown request types still return `{ok:false, error:"unknown request type: ..."}`.
- Request-specific failures return JSON-safe payloads and must not crash the daemon.
- `depth` is clamped to `0..5`.

### Status additions

CocoIndex status payloads should add:

```json
{
  "counts": {
    "symbols": 123,
    "symbols_by_language": {"python": 123},
    "symbols_by_kind": {"module": 10, "class": 20, "function": 70, "method": 23},
    "symbol_parser_errors": 1,
    "symbols_stale": 0
  },
  "capabilities": {
    "symbols": true,
    "symbol_search": true,
    "symbol_definition": true,
    "symbol_context": true,
    "symbol_embeddings": true,
    "references": false,
    "languages": ["python"]
  }
}
```

For lexical backend, `capabilities.symbols` is false and counts are zero or omitted.

## Pi tool contract

Add tools in `index.ts`. Keep `code_search` unchanged.

### `symbol_search`

Purpose: Find functions, classes, methods, and modules by name or intent.

Parameters:

```ts
{
  query: string;
  top_k?: number;      // default 8, clamp 1..50
  kind?: "function" | "class" | "method" | "module";
  language?: string;   // optional, first release supports "python"
  refresh?: boolean;   // default false
}
```

Execution:

```text
uv run --project <extension> pi-code-index symbols search --json --top-k <N> [--kind K] [--language L] [--refresh] <query>
```

Return content text format:

```text
symbol_search: config loader
Found 2 symbols; showing 2.

1. src/pi_code_index/config.py:132-158 function pi_code_index.config.load_project_config language=python score=0.940
   def load_project_config(repo: Path) -> ProjectConfig:

2. ...
Next: use `read` or definition lookup before editing or answering in detail.
```

Details:

```ts
{
  ...payload,
  display: { totalResults, displayedResults, omittedResults, truncatedText },
  cli_json: payload
}
```

### `symbol_definition`

Purpose: Resolve a symbol target to its defining location.

Parameters:

```ts
{
  target: string;      // symbol_id, qualified name, name, or file:line[:column]
  refresh?: boolean;
}
```

Execution:

```text
uv run --project <extension> pi-code-index symbols definition --json [--refresh] <target>
```

Return content text:

- If exactly resolved: `symbol_definition: <qualified_name> -> filename:start-end kind=<kind> language=<language>` plus signature and bounded snippet when provided.
- If ambiguous: list matches using the symbol-search item format and instruct caller to retry with `symbol_id` or qualified name.
- If not found: show warning and suggest `code_search`.

### `symbol_context`

Purpose: Navigate around a symbol by returning parents, children, siblings, module members, and linked chunks.

Parameters:

```ts
{
  target: string;
  depth?: number;      // default 1, clamp 0..5
  refresh?: boolean;
}
```

Execution:

```text
uv run --project <extension> pi-code-index symbols context --json --depth <D> [--refresh] <target>
```

Return content text must be compact and sectioned:

```text
symbol_context: pi_code_index.config.load_project_config
Target: src/pi_code_index/config.py:132-158 function ...
Parents: ...
Children: ...
Siblings: ...
Module symbols: ...
Chunks: ...
```

All Pi tool formatters must tolerate unknown fields and missing optional metadata.

## Result payload contracts

### Common payload fields

Every symbol operation returns:

```json
{
  "ok": true,
  "backend": "cocoindex|lexical",
  "operation": "symbol_search|symbol_definition|symbol_context",
  "repo": "/absolute/repo",
  "repo_id": "<id or null>",
  "branch": "main or null",
  "branch_id": "<id or null>",
  "schema_version": 1,
  "pipeline_version": "canonical-v1-ast-v1",
  "capabilities": {},
  "warning": null
}
```

### Symbol item shape

All symbol lists reuse this shape:

```json
{
  "score": 0.94,
  "symbol_id": "<32 hex>",
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
  "docstring": null,
  "metadata": {
    "backend": "cocoindex",
    "file_id": "<id>",
    "chunk_id": "<id or null>",
    "parent_symbol_id": "<id or null>",
    "freshness_status": "current",
    "ranking": {}
  }
}
```

`score` is optional only in context lists where ranking is not meaningful; if absent, formatters display no score.

### `symbol_search` payload

```json
{
  "ok": true,
  "backend": "cocoindex",
  "operation": "symbol_search",
  "query": "config loader",
  "top_k": 8,
  "filters": {"kind": "function", "language": "python"},
  "repo": "/absolute/repo",
  "repo_id": "<id>",
  "branch": "main",
  "branch_id": "<id>",
  "schema_version": 1,
  "pipeline_version": "canonical-v1-ast-v1",
  "results": [],
  "truncated": false,
  "truncation": {
    "candidate_limit": 160,
    "omitted_candidates": 0
  },
  "warning": null
}
```

### `symbol_definition` payload

```json
{
  "ok": true,
  "backend": "cocoindex",
  "operation": "symbol_definition",
  "target": "load_project_config",
  "definition": {
    "symbol_id": "<id>",
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
    "docstring": null,
    "code": "<bounded source snippet or null>",
    "metadata": {}
  },
  "matches": [],
  "warning": null
}
```

If ambiguous:

```json
{
  "ok": true,
  "definition": null,
  "matches": ["<symbol item objects>"],
  "warning": "ambiguous target; retry with symbol_id or qualified_name"
}
```

### `symbol_context` payload

```json
{
  "ok": true,
  "backend": "cocoindex",
  "operation": "symbol_context",
  "target": "pi_code_index.config.load_project_config",
  "target_symbol_id": "<id>",
  "symbol": {},
  "parents": [],
  "children": [],
  "siblings": [],
  "module_symbols": [],
  "chunks": [
    {
      "chunk_id": "<id>",
      "filename": "src/pi_code_index/config.py",
      "start_line": 132,
      "end_line": 158,
      "chunk_kind": "function",
      "chunk_role": "primary",
      "metadata": {}
    }
  ],
  "references_available": false,
  "warning": null
}
```

`depth = 0` returns only `symbol` and directly linked chunks. `depth > 1` may recursively include descendant children but must bound total returned symbols to 200 by default and report truncation.

## Fallback behavior

### Lexical backend

Initial safe fallback:

- `symbol_search`: `ok: true`, `results: []`, `backend: "lexical"`, warning `symbol_search requires CocoIndex/Postgres symbol indexing; use code_search for lexical fallback`.
- `symbol_definition`: `ok: true`, `definition: null`, `matches: []`, warning `symbol_definition requires CocoIndex/Postgres symbol indexing; use code_search or read file:line directly`.
- `symbol_context`: `ok: true`, empty lists, warning `symbol_context requires CocoIndex/Postgres symbol indexing`.

Do not implement regex fallback unless tests lock language limitations and mark every result with `confidence: low` and `fallback_reason: lexical_symbol_scan`.

### CocoIndex unavailable in auto mode

Use the existing `_with_auto_fallback` pattern. Payloads must include:

```json
{
  "backend": "lexical",
  "backend_fallback": true,
  "warning": "CocoIndex symbol_search unavailable; fell back to lexical JSON backend: <error>; symbol_search requires CocoIndex/Postgres symbol indexing"
}
```

### Invalid requests

Invalid `top_k`, `depth`, malformed JSON targets, unsupported kind filters, or path traversal in filenames return `ok: false` with `error` and do not trigger fallback.

## Tests required for implementation

### TypeScript tests

Add/update tests under `tests/` for `index.ts` formatters and tool wiring:

- `formatSymbolSearchResults` shows compact symbol lines, signature snippets, warning text, omitted counts, and details summary.
- `formatSymbolDefinitionResult` handles resolved, ambiguous, not found, and error payloads.
- `formatSymbolContextResult` handles empty sections, truncation, and unknown metadata.
- Existing `formatResults` tests for `code_search` continue passing unchanged.
- Tool registration tests, if present or added, assert `code_search` still exists and new tools are additive.

### CLI parser tests

Add Python tests for:

- `pi-code-index symbols search --json --top-k 3 --kind function --language python QUERY` sends/uses operation `symbol_search`.
- `pi-code-index symbols definition --json TARGET` sends/uses `symbol_definition`.
- `pi-code-index symbols context --json --depth 2 TARGET` sends/uses `symbol_context`.
- `--no-daemon` calls backend functions directly.
- Existing `search`, `refresh`, `status`, `stop`, and `live` parser behavior is unchanged.

### Symbol extraction unit tests

Add tests for Python extraction:

- Module, class, function, async function, method, and nested function rows.
- Qualified-name construction includes module name.
- Stable `symbol_id` input material.
- Parent/child relationships via `metadata.parent_symbol_id`.
- Signature excludes decorators and docstring extraction is bounded.
- Private names set visibility `private`; normal names set `public` or `unknown` per implementation.
- Parse errors create parser-error freshness metadata and do not crash chunk indexing.
- Unsupported language creates no symbol rows and no parser error.

### Backend query tests

Add unit tests with mocked rows or lightweight DB fixtures for:

- Exact name and qualified-name ranking.
- Semantic-unavailable ranking when no symbol embeddings exist.
- Kind/language filters.
- File/line containment chooses the smallest containing symbol.
- Ambiguous bare-name lookup returns matches and warning.
- Context returns parents, children, siblings, module symbols, and linked chunks.
- Lexical fallback payloads are safe empty payloads with warnings.

### CocoIndex/Postgres integration tests

Using the existing Postgres test setup, add tests for:

- `refresh` with `enable_symbols=true` and `chunk_strategy=hybrid` populates `{prefix}_symbols` and `{prefix}_symbol_embeddings` for Python files.
- `symbols search --json "config loader"` returns symbol rows with repo-relative filename, kind, language, score, and ranking metadata.
- `symbols definition --json file.py:line` resolves a function or method.
- `symbols context --json qualified.name` returns parent/child/sibling/module lists.
- Status includes symbol counts and capabilities.

Local container validation must use Podman, not Docker.

### Daemon tests

Add tests for:

- New request types route to backend functions.
- Resource cache is reused for symbol operations.
- Bad request type behavior remains unchanged.
- Status includes symbol counts/capabilities when available.
- Daemon returns JSON-safe errors for symbol exceptions.

### Regression tests

- Existing lexical `code_search` tests pass.
- Existing CocoIndex chunk search tests pass.
- Existing TypeScript compact `code_search` output tests pass.
- Existing CLI commands remain listed in `--help` and still accept previous flags.

## Validation commands

Docs/spec validation for this issue:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
git diff -- docs/architecture/symbol-intelligence-spec.md
grep -n "symbol_search\|symbol_definition\|symbol_context\|CocoIndex V1" docs/architecture/symbol-intelligence-spec.md
bd show pi-code-index-3jo.3.2 --json
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
uv run pi-code-index symbols --help
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
uv run pi-code-index symbols definition --json "src/pi_code_index/config.py:132"
uv run pi-code-index symbols context --json "pi_code_index.config.load_project_config"

podman exec pi-code-index-postgres psql -U cocoindex -d cocoindex \
  -c "SELECT kind, count(*) FROM pi_code_index_symbols GROUP BY kind ORDER BY kind;"
```

Cleanup:

```bash
podman rm -f pi-code-index-postgres
```

## Implementation acceptance criteria

An implementation subagent is done only when all of these are true:

1. `code_search` Pi tool behavior and existing CLI behavior are unchanged except for additive metadata.
2. `symbol_search`, `symbol_definition`, and `symbol_context` Pi tools are registered and return compact text plus full `details.cli_json`.
3. CLI `symbols search`, `symbols definition`, and `symbols context` work with and without daemon.
4. Daemon protocol supports `symbol_search`, `symbol_definition`, and `symbol_context` and reuses CocoIndex resources.
5. Python symbol extraction creates deterministic module/class/function/method/nested-function rows with required metadata.
6. `{prefix}_symbols`, `{prefix}_symbol_embeddings`, chunks, and freshness rows are populated consistently for enabled Python repositories.
7. Symbol search ranking includes exact, qualified-name, token, semantic, signature, docstring, path, and freshness components in metadata.
8. Definition lookup resolves by `symbol_id`, qualified name, bare name, and file/line containment with explicit ambiguity behavior.
9. Context lookup returns target, parents, children, siblings, module symbols, and linked chunks with bounded/truncated payloads.
10. Lexical and auto fallback paths return safe payloads with clear warnings and do not crash.
11. Status reports symbol counts and capabilities.
12. Tests listed above are added and pass.
13. Validation commands above pass in the appropriate local environment.

## Open follow-up decisions

These are explicitly out of scope for the first implementation unless separate issues approve them:

- Turn `enable_symbols` on by default.
- Add tree-sitter or language-specific parser dependencies for TypeScript/JavaScript, Go, Rust, or Java.
- Implement regex lexical symbol fallback.
- Normalize parent/child/sibling relationships into a separate `symbol_relations` table.
- Add references/callers/imports to `symbol_context` by default.
