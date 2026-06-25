# Call graph and impact analysis tools spec

## Scope

This specification converts `docs/architecture/call-graph-impact-plan.md` into buildable requirements for issue `pi-code-index-3jo.4.2` and later implementation issues.

The implementation must add call graph extraction and graph navigation for CocoIndex-backed repositories while preserving existing CLI, daemon, and Pi UX. Product changes are additive only: existing `code_search`, `symbol_search`, `symbol_definition`, `symbol_context`, `search`, `refresh`, `status`, `stop`, `live`, daemon handshake behavior, and lexical fallback behavior remain backward-compatible.

This document is specs/docs only. It does not implement product code.

## Architecture references

Implementers must keep these documents consistent:

- `docs/architecture/final-integration-spec.md`: canonical schema, identity, daemon compatibility, status conventions, and stable `code_search` behavior.
- `docs/architecture/ast-aware-semantic-search-spec.md`: AST chunk metadata, freshness conventions, parser fallback behavior, and bounded payload expectations.
- `docs/architecture/symbol-intelligence-spec.md`: symbol identity, target resolution, symbol item contracts, and Python AST parser boundaries.
- `docs/architecture/call-graph-impact-plan.md`: roadmap and rationale for this graph feature.

## Current code inspected

The spec is based on the current repository state of:

- `index.ts`
  - Registers `code_search`, `symbol_search`, `symbol_definition`, and `symbol_context` tools.
  - Tools shell out to `uv run --project <extension> pi-code-index ... --json` in Pi's active cwd.
  - Tool responses put compact text in `content` and full raw JSON in `details.cli_json`.
  - Prompt guidance mentions symbol tools and `code_search`; no graph tools exist.
- `src/pi_code_index/cli.py`
  - Exposes `init`, `search`, `refresh`, `status`, `stop`, `live start|stop|status`, `symbols search|definition|context`, and hidden `daemon`.
  - Auto-starts the daemon unless `--no-daemon` is passed.
  - No `graph` command group exists.
- `src/pi_code_index/daemon.py`
  - Handles `handshake`, `search`, `refresh`, symbol request types, live request types, `status`, and `stop`.
  - `BackendResourceCache` key already includes `enable_references`, `enable_test_links`, symbol/AST config, schema/table prefix, branch mode, and pipeline version.
  - No caller/callee/impact request types exist.
- `src/pi_code_index/backend.py`
  - Routes `auto`, `lexical`, and `cocoindex` for refresh/search/status and symbol operations.
  - Symbol lexical fallback returns safe empty payloads with warnings.
  - No graph routing or graph fallback helpers exist.
- `src/pi_code_index/coco_backend.py`
  - Defines `ReferenceRow`, `CallEdgeRow`, and `TestLinkRow` dataclasses.
  - Creates `{prefix}_references`, `{prefix}_call_edges`, and `{prefix}_test_links` with indexes.
  - `refresh()` and status counters already mention references/call edges/test links, but extraction/population is not implemented.
  - `_symbol_base_payload()` currently reports `capabilities.references: false`.
  - Python AST symbol extraction exists; reference/call extraction and graph queries do not.
- `src/pi_code_index/config.py`
  - `ProjectConfig.enable_references` and `ProjectConfig.enable_test_links` already exist and default to `false`.
  - `enable_symbols`, AST language options, symbol language options, and bounded symbol payload config already exist.
- `src/pi_code_index/indexer.py`
  - Lexical JSON backend supports chunk search only.
  - It has no symbol, reference, call graph, or impact index.

## Non-goals

- Do not remove, rename, or repurpose existing tools, CLI commands, CLI flags, daemon request types, JSON fields, compact format text, or status fields.
- Do not require CocoIndex/Postgres for users who rely on `backend: auto` lexical search.
- Do not add a persistent lexical call graph in the first implementation.
- Do not claim high-confidence edges for dynamic calls that cannot be resolved statically.
- Do not add parser dependencies for TypeScript/JavaScript, Go, Rust, Java, or any non-Python language without a separate dependency decision issue.
- Do not build a full type checker, language server integration, cross-repository graph, cross-branch graph, import graph UI, inheritance graph, or override analysis in this release.
- Do not use unreleased CocoIndex APIs or non-V1 concepts.

## CocoIndex V1 boundary

Use only these CocoIndex V1 concepts already accepted by the project:

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
- idempotent `asyncpg` DDL/query code where CocoIndex V1 table targets are insufficient

Raw Python parsing, graph resolution, and graph querying may happen in memoized Python functions and/or idempotent `asyncpg` code. Do not add a custom DSL or depend on CocoIndex APIs not listed above.

## Feature gates and rollout

### Required gates

- `ProjectConfig.enable_symbols` gates symbol table population and is a prerequisite for graph extraction.
- `ProjectConfig.enable_references` gates reference extraction, call edge extraction, graph table population, and graph tool readiness.
- `ProjectConfig.chunk_strategy in {"ast", "hybrid"}` is required for Python AST graph extraction.
- `ProjectConfig.ast_languages` or `ProjectConfig.symbol_languages` controls parser language allow-list. The first implementation supports Python only.
- `ProjectConfig.enable_test_links` gates durable test-link enrichment. If false, `impact_analysis` may still return conservative test hints, but they must be labeled as heuristic and low confidence.

### Optional additive config

Use the existing gates first. The implementation may add these fields only if needed:

```python
max_graph_depth: int = 5
max_graph_edges: int = 5000
reference_languages: list[str] | None = None
min_call_edge_confidence: float = 0.35
```

Rules:

- Defaults must preserve existing config loading.
- `reference_languages` defaults to `symbol_languages`, then `ast_languages`, then `['python']` for the first release.
- `max_graph_depth` must be positive and no greater than 5 after CLI/daemon clamping.
- `max_graph_edges` must be positive.
- `min_call_edge_confidence` must be in `[0.0, 1.0]`.
- Environment overrides, if added, must follow the existing `PI_CODE_INDEX_*` validation pattern.

### Default behavior

Keep `enable_references: false` and `enable_test_links: false` by default. Users opt in with project settings like:

```yaml
enable_symbols: true
enable_references: true
chunk_strategy: hybrid
ast_languages: [python]
```

## Extraction behavior

### Supported language baseline

Phase 1 supports Python via the built-in `ast` module only. Unsupported languages must keep normal chunk/search indexing working and must not create graph rows. Status and graph payloads should warn when graph extraction is disabled because no supported language was indexed.

### When to extract

During canonical population for a file, extract references and call edges only when all are true:

- backend is CocoIndex/Postgres;
- `enable_symbols` is true;
- `enable_references` is true;
- `chunk_strategy` is `ast` or `hybrid`;
- language is Python and the file parsed successfully;
- symbol rows for the file are available in memory or queryable from the canonical symbol table.

Parser errors must not fail the whole refresh. They must be recorded in freshness/status metadata and cause graph rows for that file to be absent or stale.

### Python reference extraction

For each parsed Python file:

1. Build a local symbol map from extracted module/class/function/method symbols. Include parent relationships from symbol metadata.
2. Build import maps:
   - `import pkg.mod` maps `pkg` and/or `pkg.mod` to module candidates when indexed.
   - `import pkg.mod as alias` maps `alias` to `pkg.mod`.
   - `from pkg.mod import name` maps `name` to `pkg.mod.name`.
   - `from pkg.mod import name as alias` maps `alias` to `pkg.mod.name`.
   - Star imports produce unresolved references with `resolution.strategy = 'star_import'`; they do not create call edges.
3. Walk module, class, function, and method bodies with the current caller symbol context.
4. Emit `ReferenceRow` candidates for:
   - `ast.Name` in load context: `kind = 'name'`.
   - `ast.Attribute`: `kind = 'attribute'`; store both `attr` and best-effort dotted text.
   - `ast.Import` / `ast.ImportFrom`: `kind = 'import'` or `kind = 'import_from'`.
   - `ast.Call`: emit or update a reference with `kind = 'call'` and source span metadata.
5. Store unresolved references with `symbol_id = null` and detailed resolution metadata.
6. Create `CallEdgeRow` only for call references that resolve to an indexed symbol with confidence at or above `min_call_edge_confidence` (default `0.35`).

### Python caller symbol context

- A call inside a function or method has `caller_symbol_id` equal to that function/method symbol.
- A call at class body scope has `caller_symbol_id` equal to the class symbol.
- A call at module top level has `caller_symbol_id` equal to the module symbol.
- If no caller symbol can be determined, emit an unresolved reference and do not create a call edge.
- Do not create self-edges for a recursive call unless the call resolves to the same symbol. Recursive self-edges are allowed and must be marked `metadata.recursive = true`.

### Python call resolution tiers

Resolution must be deterministic and auditable. Use these base confidences:

| Pattern | Example | Resolution | Base confidence |
| --- | --- | --- | --- |
| Same-scope direct function | `helper()` | nearest symbol in lexical or module scope | `0.95` |
| Method on `self` | `self.save()` | method on containing class | `0.90` |
| Method on `cls` | `cls.make()` | classmethod/staticmethod or method on containing class | `0.85` |
| Qualified module function | `config.load_project_config()` | import alias/module map plus qualified name | `0.85` |
| Class constructor | `Widget(...)` | class symbol in scope | `0.80` |
| Imported direct name | `load_project_config()` after `from x import load_project_config` | import map to qualified symbol | `0.80` |
| Same-file bare name fallback | `helper()` with one same-file symbol named `helper` | unique same-file symbol | `0.70` |
| Attribute on unknown object | `client.send()` | unresolved reference only | no edge |
| Dynamic call | `getattr(x, name)()` / callable variable | unresolved reference only | no edge |

Confidence modifiers:

- `+0.03` for exact qualified-name match.
- `+0.02` for same file.
- `-0.10` for ambiguous multiple symbols with the same name when a tie-breaker selects one.
- `-0.15` for stale/error freshness on caller or callee.
- Clamp final edge confidence to `[0.0, 1.0]`.

If ambiguity cannot be resolved deterministically above the threshold, do not create a call edge. Store the candidate symbol IDs in reference metadata.

### Stable IDs

Add helpers in `coco_backend.py` or equivalent:

```python
def reference_id_for(repo_id: str, branch_id: str, file_id: str, name: str, kind: str, line: int, column: int) -> str:
    return _stable_id('reference', repo_id, branch_id, file_id, name, kind, line, column)

def call_edge_id_for(repo_id: str, branch_id: str, caller_symbol_id: str, callee_symbol_id: str, source_span: dict[str, int]) -> str:
    return _stable_id('call_edge', repo_id, branch_id, caller_symbol_id, callee_symbol_id, source_span.get('line'), source_span.get('column'))
```

Tests must lock these identity inputs. Moving a call site may change the edge ID; this is acceptable for the first release.

### Idempotent population

Refresh must not duplicate graph rows.

- Before repopulating graph rows for a file, delete old rows for that `repo_id`, `branch_id`, and `file_id` from `{prefix}_references`.
- Delete old call edges whose metadata callsite `file_id` equals the file being repopulated, or upsert by stable `edge_id` and remove stale edges no longer emitted.
- Insert/update references and call edges with primary-key upserts.
- If a file is deleted or no longer included, its graph rows must be removed or marked stale consistently with existing freshness behavior.

## Table/data contracts

### Existing canonical tables reused

Use the canonical tables already created by `ensure_canonical_schema()`:

- `{prefix}_references`
- `{prefix}_call_edges`
- `{prefix}_symbols`
- `{prefix}_files`
- `{prefix}_chunks`
- `{prefix}_freshness`
- `{prefix}_test_links` when test enrichment is enabled

No table rename is allowed.

### `{prefix}_references` columns

Existing columns are the contract:

```text
reference_id text primary key
repo_id text not null
branch_id text not null
file_id text not null
symbol_id text null
name text not null
kind text not null
line integer not null
column_number integer not null default 0
metadata jsonb not null default '{}'
```

Required metadata for Python references:

```json
{
  "language": "python",
  "parser": "python_ast",
  "parser_version": "py-ast-v1",
  "extractor_version": "reference-extractor-v1",
  "source_hash": "<file sha256>",
  "caller_symbol_id": "<symbol id or null>",
  "target_qualified_name": "pi_code_index.config.load_project_config",
  "dotted_name": "config.load_project_config",
  "span": {"start_line": 10, "end_line": 10, "start_col": 4, "end_col": 29},
  "resolution": {
    "status": "resolved|unresolved|ambiguous",
    "strategy": "same_scope|self_method|cls_method|import_alias|qualified_name|same_file_bare_name|unknown_attribute|dynamic_call|star_import",
    "candidate_symbol_ids": [],
    "confidence": 0.85
  },
  "freshness_status": "current"
}
```

Additional metadata is allowed. All consumers must ignore unknown metadata fields.

### `{prefix}_call_edges` columns

Existing columns are the contract:

```text
edge_id text primary key
repo_id text not null
branch_id text not null
caller_symbol_id text not null
callee_symbol_id text not null
confidence real not null
source text not null
metadata jsonb not null default '{}'
```

Required metadata:

```json
{
  "language": "python",
  "reference_id": "<reference id>",
  "callsite": {"file_id": "<file id>", "path": "src/x.py", "line": 42, "column": 8},
  "resolution_strategy": "import_alias",
  "edge_kind": "call",
  "direct": true,
  "recursive": false,
  "freshness_status": "current",
  "confidence_factors": {
    "base": 0.85,
    "qualified_name_bonus": 0.03,
    "same_file_bonus": 0.0,
    "ambiguity_penalty": 0.0,
    "freshness_penalty": 0.0
  }
}
```

`source` must be the extractor source string `python_ast` for phase 1.

### `{prefix}_test_links` use

When `enable_test_links` is true and rows exist, `impact_analysis` may join `{prefix}_test_links` to enrich `affected_tests`. Until durable test-link extraction is implemented, graph implementation must not invent high-confidence test links. Path/import/call hints are allowed only as response-level heuristic items with `reason` and `confidence <= 0.60`.

## Backend API contract

Add these functions to `src/pi_code_index/backend.py`:

```python
def find_callers(repo: Path, target: object, depth: int = 1, top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]: ...

def find_callees(repo: Path, target: object, depth: int = 1, top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]: ...

def impact_analysis(repo: Path, target: object, depth: int = 2, top_k: int = 50, include_tests: bool = True, include_files: bool = True, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]: ...
```

Clamp arguments at the backend boundary:

- callers/callees: `depth` min `1`, max `5`; if `include_indirect` is false, effective traversal depth is `1`; `top_k` min `1`, max `100`.
- impact: `depth` min `1`, max `5`; `top_k` min `1`, max `200`.

Fallback rules:

- `cocoindex` requested: real backend failures return `ok: false`, `backend: 'cocoindex'`, `operation`, `repo`, and `error`, matching existing backend error style.
- `auto` with CocoIndex unavailable: return `ok: true`, `backend: 'lexical'`, `backend_fallback: true`, empty graph results, and a warning that call graph tools require CocoIndex/Postgres reference indexing.
- `lexical` requested: return `ok: true`, `backend: 'lexical'`, empty graph results, and the same warning. Do not scan text and present it as a graph in the first rollout.

Lexical fallback payloads must include the same operation-specific top-level shape as CocoIndex payloads, with empty arrays and `capabilities.call_graph = false`.

## Coco backend graph query contract

Add matching public functions to `src/pi_code_index/coco_backend.py`:

```python
def find_callers(repo: Path, target: object, depth: int = 1, top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]: ...

def find_callees(repo: Path, target: object, depth: int = 1, top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]: ...

def impact_analysis(repo: Path, target: object, depth: int = 2, top_k: int = 50, include_tests: bool = True, include_files: bool = True, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]: ...
```

Required internal behavior:

- Reuse symbol target resolution from `symbol_definition` so accepted targets are identical.
- If `refresh_first` is true, call `refresh(repo)` before querying.
- Use the daemon-provided `CocoBackendResources` pool/embedder when present.
- Query only the current `repo_id` and `branch_id`.
- Use bounded iterative breadth-first traversal or recursive SQL. Either is acceptable if depth/edge caps are enforced and tests lock behavior.
- Deduplicate by related symbol. Keep the highest-scoring path and at most three alternate paths per symbol in payloads.
- Join symbols to files to return `filename`, `start_line`, `end_line`, `signature`, `kind`, `name`, and `qualified_name`.
- Join freshness where cheap. Missing freshness should be reported as `freshness_status: 'unknown'` rather than failing the query.

## CLI contract

Add a `graph` command group to `src/pi_code_index/cli.py`. Existing commands and flags must remain unchanged.

```bash
pi-code-index graph callers [--json] [--top-k N] [--depth N] [--include-indirect] [--refresh] [--repo PATH] TARGET
pi-code-index graph callees [--json] [--top-k N] [--depth N] [--include-indirect] [--refresh] [--repo PATH] TARGET
pi-code-index graph impact [--json] [--top-k N] [--depth N] [--include-tests | --no-include-tests] [--include-files | --no-include-files] [--refresh] [--repo PATH] TARGET
```

Argument behavior:

- `TARGET` accepts the same forms as `symbols definition`: `symbol_id`, qualified name, bare name, and `repo-relative-file:line[:column]`.
- `--json` prints the raw JSON payload with `json.dumps(..., ensure_ascii=False)`.
- Non-JSON callers/callees output must be compact human lines: `filename:start-end kind qualified_name distance=N score=0.000 confidence=0.000`, followed by callsite/path detail when available.
- Non-JSON impact output must show summary, affected files, affected symbols, and affected tests in bounded text. It may print JSON for complex nested data if kept consistent with current symbol context style.
- `--no-daemon` must call backend functions directly.
- Daemon mode must send the request types specified below.
- Return code is `0` when `payload.get('ok', True)` is true; otherwise `1`.

CLI clamping:

- callers/callees: clamp `top_k` to `1..100`; clamp `depth` to `1..5`.
- impact: clamp `top_k` to `1..200`; clamp `depth` to `1..5`.
- `--include-indirect` false means only distance `1` results even if `--depth` is greater than `1`.

## Daemon protocol contract

Add request types to `src/pi_code_index/daemon.py`:

### `find_callers`

Request:

```json
{
  "type": "find_callers",
  "repo": "/repo",
  "target": "pkg.module.func",
  "depth": 1,
  "top_k": 20,
  "include_indirect": false,
  "refresh": false
}
```

Response: common graph payload with `operation: 'find_callers'` and `results` array.

### `find_callees`

Request:

```json
{
  "type": "find_callees",
  "repo": "/repo",
  "target": "pkg.module.func",
  "depth": 1,
  "top_k": 20,
  "include_indirect": false,
  "refresh": false
}
```

Response: common graph payload with `operation: 'find_callees'` and `results` array.

### `impact_analysis`

Request:

```json
{
  "type": "impact_analysis",
  "repo": "/repo",
  "target": "pkg.module.func",
  "depth": 2,
  "top_k": 50,
  "include_tests": true,
  "include_files": true,
  "refresh": false
}
```

Response: common graph payload with `operation: 'impact_analysis'`, `affected_symbols`, `affected_files`, `affected_tests`, and `summary`.

Unknown future request fields must be ignored. Existing request types must be unchanged.

## Common graph response contract

All graph payloads must include:

```json
{
  "ok": true,
  "backend": "cocoindex",
  "operation": "find_callers|find_callees|impact_analysis",
  "repo": "/repo",
  "repo_id": "...",
  "branch": "main",
  "branch_id": "...",
  "schema_version": 1,
  "pipeline_version": "canonical-v1-ast-v1",
  "target": "...",
  "target_kind": "symbol|file|ambiguous|unresolved",
  "target_symbol": {"symbol_id": "...", "qualified_name": "..."},
  "matches": [],
  "depth": 2,
  "top_k": 20,
  "capabilities": {
    "symbols": true,
    "references": true,
    "call_graph": true,
    "impact_analysis": true,
    "test_links": false,
    "languages": ["python"]
  },
  "warning": null
}
```

Rules:

- `target_symbol` is `null` for unresolved targets and for file impact targets.
- `matches` uses the same symbol item contract as `symbol_definition` when a target is ambiguous.
- If `target_kind = 'ambiguous'`, return empty graph arrays and warning instructing the caller to retry with `symbol_id`.
- Payloads may include `truncated` and `truncation` fields when caps are hit.
- Unknown future fields must be tolerated by CLI and Pi formatters.

## `find_callers` / `find_callees` result contract

Response addition:

```json
{
  "results": [
    {
      "relationship": "caller|callee",
      "distance": 1,
      "score": 0.91,
      "path_confidence": 0.91,
      "edge_count": 1,
      "symbol": {
        "symbol_id": "...",
        "qualified_name": "pkg.module.func",
        "name": "func",
        "kind": "function",
        "language": "python",
        "filename": "src/pkg/module.py",
        "start_line": 12,
        "end_line": 34,
        "signature": "def func(...):"
      },
      "paths": [
        {
          "symbols": ["caller_symbol_id", "target_symbol_id"],
          "edges": ["edge_id"],
          "callsite": {"filename": "src/pkg/caller.py", "line": 88, "column": 10},
          "confidence": 0.91
        }
      ],
      "ranking": {
        "directness_score": 1.0,
        "confidence_score": 0.91,
        "symbol_relevance": 0.8,
        "freshness_score": 1.0,
        "test_or_entrypoint_boost": 0.0,
        "final_score": 0.91
      }
    }
  ],
  "truncated": false,
  "truncation": {"edge_budget": 5000, "omitted_paths": 0, "omitted_results": 0}
}
```

Ordering:

1. Descending `score`.
2. Ascending `distance`.
3. Descending `path_confidence`.
4. Lexicographic `symbol.qualified_name` for deterministic ties.

## `impact_analysis` contract

`impact_analysis` answers blast-radius questions for a symbol or file. It prioritizes callers because callers are most likely to be affected by a changed callee. It may include callees for context but must label counts separately.

Response additions:

```json
{
  "affected_symbols": [],
  "affected_files": [
    {
      "filename": "src/pkg/caller.py",
      "score": 0.87,
      "relationship_counts": {"direct_callers": 2, "indirect_callers": 4, "direct_callees": 1, "indirect_callees": 0},
      "highest_confidence_path": 0.93,
      "freshness_status": "current",
      "reasons": ["direct_caller", "same_file_callee"]
    }
  ],
  "affected_tests": [
    {
      "filename": "tests/test_caller.py",
      "score": 0.78,
      "confidence": 0.75,
      "reason": "test_link|path_convention|calls_target|imports_target",
      "test_symbols": []
    }
  ],
  "summary": {
    "direct_callers": 2,
    "indirect_callers": 4,
    "direct_callees": 1,
    "indirect_callees": 0,
    "affected_symbols": 6,
    "affected_files": 3,
    "affected_tests": 1,
    "truncated": false
  }
}
```

File targets:

- If `TARGET` resolves to a repo-relative file path, set `target_kind = 'file'`.
- Seed traversal from all symbols in that file.
- Include the target file in `affected_files` with reason `target_file` and score `1.0`.
- If the file has no symbols, return file-level heuristics only and warn that no symbols were indexed for the file.

Test hints:

- Durable `{prefix}_test_links` rows can produce confidence above `0.60` according to their stored confidence.
- Heuristic path conventions, imports, or calls must use `confidence <= 0.60` and `reason` that identifies the heuristic.
- If `include_tests` is false, `affected_tests` must be `[]` and summary `affected_tests` must be `0`.
- If `include_files` is false, `affected_files` may be `[]`, but `affected_symbols` and summary counts must still be returned.

## Ranking and confidence model

### Caller/callee final score

Score each related symbol/path with:

```text
final_score =
  0.45 * path_confidence
+ 0.25 * directness_score
+ 0.15 * symbol_relevance
+ 0.10 * freshness_score
+ 0.05 * test_or_entrypoint_boost
```

Definitions:

- `path_confidence` is the product of edge confidences along the selected path.
- `directness_score = 1 / distance`.
- `symbol_relevance` is deterministic: start at `0.5`, add `0.2` for same file, add `0.1` for same module, add `0.1` for public symbol, add `0.1` for exact target-adjacent name hints, clamp to `1.0`.
- `freshness_score = 1.0` for current, `0.7` for stale, `0.5` for parser error-adjacent, `0.0` for deleted, `0.5` for unknown.
- `test_or_entrypoint_boost = 1.0` for tests or likely entrypoints, else `0.0`; it is weighted low and must not dominate confidence.

Return the ranking breakdown in each result.

### Impact scoring

- `affected_symbols` use the same caller/callee score.
- `affected_files` score is the max related symbol score in the file plus small capped boosts for multiple direct callers/tests, clamped to `1.0`.
- `affected_tests` score is stored test-link confidence when available; otherwise the heuristic confidence multiplied by the best related file/symbol score.

### Traversal limits

- Default callers/callees depth is `1`.
- If `include_indirect` is true and no explicit depth is passed, CLI/Pi should pass `depth = 2`.
- Default impact depth is `2`.
- Hard cap `depth <= 5` and `top_k <= 200`.
- Stop expanding when cumulative path confidence falls below `min_call_edge_confidence` or when `max_graph_edges` is reached.
- Deduplicate by symbol and keep the highest-scoring path plus at most three alternate paths.

## Status contract

`status --json` and daemon status must add graph readiness without removing current fields. Under the backend payload include counts and capabilities like:

```json
{
  "counts": {
    "references": 123,
    "resolved_references": 111,
    "unresolved_references": 12,
    "call_edges": 45,
    "low_confidence_call_edges": 8,
    "test_links": 10
  },
  "capabilities": {
    "symbols": true,
    "references": true,
    "call_graph": true,
    "impact_analysis": true,
    "test_links": false,
    "languages": ["python"]
  }
}
```

If references are disabled, counts may be zero but `capabilities.references`, `call_graph`, and `impact_analysis` must be false with a warning.

## Fallback and warning behavior

Use these exact warning substrings so tests can assert them:

- References disabled: `reference indexing is disabled; set enable_references: true with enable_symbols: true and chunk_strategy: hybrid|ast`.
- Symbols disabled: `call graph requires enable_symbols: true`.
- Unsupported backend: `call graph tools require CocoIndex/Postgres reference indexing`.
- Missing symbol rows: `no indexed symbols found for graph target; run pi-code-index refresh after enabling symbols`.
- Empty call graph: `no call edges were indexed for the current repo/branch`.
- Ambiguous target: `target is ambiguous; retry with symbol_id`.
- Unresolved target: `target could not be resolved to an indexed symbol`.

Behavior by case:

- `enable_references` false: return `ok: true`, empty results, warning with references-disabled substring.
- Symbol tables missing or empty: return `ok: true`, empty results, warning with missing-symbol substring.
- Call edge tables exist but are empty: return `ok: true`, empty results, warning with empty-graph substring.
- Ambiguous target: return `ok: true`, `target_kind: 'ambiguous'`, `matches`, empty graph arrays, warning with ambiguous-target substring.
- Unresolved symbol target: return `ok: true`, `target_kind: 'unresolved'`, empty graph arrays, warning with unresolved-target substring.
- File target for impact: use file-seeded behavior described above.
- Lexical backend or auto fallback: return successful empty graph payloads with unsupported-backend warning, not product errors.
- Parser errors: record in status/freshness and warnings; do not fail unrelated files.

## Pi tool contract

Add three tools to `index.ts`.

### `find_callers`

Parameters:

```ts
const FindCallersParams = Type.Object({
  target: Type.String({ description: 'symbol_id, qualified name, name, or file:line[:column].' }),
  depth: Type.Optional(Type.Number({ description: 'Traversal depth. Default: 1; max: 5.', minimum: 1, maximum: 5 })),
  top_k: Type.Optional(Type.Number({ description: 'Maximum caller symbols. Default: 20; max: 100.', minimum: 1, maximum: 100 })),
  include_indirect: Type.Optional(Type.Boolean({ description: 'Include transitive callers. Default: false.' })),
  refresh: Type.Optional(Type.Boolean({ description: 'Refresh before lookup. Default: false.' })),
});
```

Execution:

- Clamp `top_k` to `1..100`.
- Clamp `depth` to `1..5`.
- If `include_indirect` is false, pass depth `1` regardless of user depth.
- Run CLI args: `['graph', 'callers', '--json', '--top-k', String(topK), '--depth', String(depth), ...flags, target]`.

### `find_callees`

Same as `find_callers`, but CLI args use `['graph', 'callees', ...]`, relationship label is callee, and description says it finds symbols called by the target.

### `impact_analysis`

Parameters:

```ts
const ImpactAnalysisParams = Type.Object({
  target: Type.String({ description: 'symbol_id, qualified name, name, file:line[:column], or repo-relative file path.' }),
  depth: Type.Optional(Type.Number({ description: 'Traversal depth. Default: 2; max: 5.', minimum: 1, maximum: 5 })),
  top_k: Type.Optional(Type.Number({ description: 'Maximum affected items. Default: 50; max: 200.', minimum: 1, maximum: 200 })),
  include_tests: Type.Optional(Type.Boolean({ description: 'Include likely affected tests. Default: true.' })),
  include_files: Type.Optional(Type.Boolean({ description: 'Include affected file rollup. Default: true.' })),
  refresh: Type.Optional(Type.Boolean({ description: 'Refresh before lookup. Default: false.' })),
});
```

Execution:

- Clamp `top_k` to `1..200`.
- Clamp `depth` to `1..5`.
- Run CLI args: `['graph', 'impact', '--json', '--top-k', String(topK), '--depth', String(depth), ...include flags, target]`.
- Use explicit `--include-tests`/`--no-include-tests` and `--include-files`/`--no-include-files` only if the CLI implements both flags; otherwise omit defaults and pass negative flags when false.

### Pi formatting

Formatters must mirror current symbol tools:

- Compact text in `content[0].text`.
- Full raw JSON preserved in `details.cli_json`.
- Spread raw payload into `details` for easy inspection.
- `details.display` contains counts: `totalResults`, `displayedResults`, `omittedResults`, `truncatedSnippets`, `truncatedText`.
- Unknown fields are ignored.

Caller/callee compact text:

- Header: `find_callers: <target>` or `find_callees: <target>`.
- Include warning line when `payload.warning` exists.
- Empty state: `No indexed call graph matches found. Try symbol_definition, code_search, or refresh with enable_references=true.`
- Result line: `<rank>. <filename>:<start>-<end> <kind> <qualified_name> distance=<N> score=<0.000> confidence=<0.000>`.
- Include up to `MAX_DISPLAY_RESULTS` results and mention omitted results.
- End with: `Next: use symbol_definition or read the listed file ranges before editing.`

Impact compact text:

- Header: `impact_analysis: <target>`.
- Summary line with direct/indirect callers, affected files, and tests.
- Sections: `Affected symbols`, `Affected files`, `Affected tests`.
- Bound each section to the existing display limit or a small constant.
- End with: `Next: inspect high-confidence callers/tests before editing.`

Error text must be `find_callers failed: <message>`, `find_callees failed: <message>`, or `impact_analysis failed: <message>`.

### Pi prompt guidance

Update `before_agent_start` guidance additively:

```text
Use find_callers, find_callees, or impact_analysis for caller/callee/blast-radius questions. Use symbol_definition or read to inspect exact source before editing.
```

Do not remove existing `code_search` or symbol guidance.

## Tests

### Python unit tests

Add tests for:

- Python AST reference extraction for same-scope calls.
- `self.method()` and `cls.method()` resolution.
- direct imports, import aliases, and `from x import y` calls.
- class constructor calls.
- unresolved unknown-object attributes.
- dynamic calls (`getattr`, callable variable) becoming unresolved references only.
- ambiguous same-name symbols storing candidates without high-confidence edge.
- recursive self-call edge with `metadata.recursive = true`.
- parser errors not failing refresh.
- stable `reference_id_for` and `call_edge_id_for` inputs.
- confidence scoring modifiers and clamping.
- traversal depth caps, edge budget caps, deduplication, and deterministic ordering.
- target resolution reuse from symbol tools.
- lexical fallback payload shapes and exact warning substrings.

### Python integration tests

Add tests for:

- CocoIndex/Postgres refresh populates `references` and `call_edges` when `enable_symbols=true`, `enable_references=true`, and `chunk_strategy=hybrid|ast`.
- Refresh does not duplicate references/call edges on repeated runs.
- Disabled references leave graph tables empty and graph queries return disabled warnings.
- `find_callers` returns direct callers with callsite metadata.
- `find_callers --include-indirect --depth 2` returns indirect callers with path metadata.
- `find_callees` returns direct and indirect callees.
- `impact_analysis` returns affected symbols/files and conservative affected tests.
- Daemon request types use warm resources and return schema/pipeline/repo/branch metadata.
- `status --json` reports graph counts and capabilities.
- Existing `code_search` and symbol tests continue passing unchanged.

### CLI tests

Add tests analogous to `tests/test_cli_symbols.py`:

- `--no-daemon graph callers --json` calls `backend.find_callers` with clamped args.
- `graph callers --json` sends daemon request type `find_callers`.
- `graph callees --json` sends daemon request type `find_callees`.
- `graph impact --json --no-include-tests --no-include-files` sends request type `impact_analysis` with false flags.
- Non-JSON print paths handle empty payloads, warnings, and populated payloads.
- Return code is `1` for `ok: false` graph payloads.

### Daemon tests

Add tests for `handle()`:

- request type `find_callers` calls backend `find_callers` with `resource_cache.get(repo)`.
- request type `find_callees` calls backend `find_callees`.
- request type `impact_analysis` calls backend `impact_analysis`.
- depth/top_k are clamped before backend call or backend clamps deterministically.
- unknown existing request behavior remains unchanged.

### TypeScript/Pi tests

Add tests for:

- Parameter schema exports/registration for `find_callers`, `find_callees`, and `impact_analysis`.
- Formatter empty, warning, direct, indirect, ambiguous, and truncated payloads.
- Raw CLI JSON remains in `details.cli_json`.
- Current `code_search` and symbol formatter snapshots remain unchanged.
- CLI args produced by tool execution match the command contract.

## Manual validation commands

From `/home/fractiunate/.pi/agent/extensions/pi-code-index`:

```bash
npm run typecheck
npm test
uv run pytest
uv run python -m compileall src tests
```

With Postgres/pgvector via Podman and optional CocoIndex deps:

```bash
scripts/setup.sh --with-cocoindex --postgres-check
export POSTGRES_URL=postgres://cocoindex:cocoindex@localhost/cocoindex
export PI_CODE_INDEX_BACKEND=cocoindex
export PI_CODE_INDEX_CHUNK_STRATEGY=hybrid
export PI_CODE_INDEX_AST_LANGUAGES=python
# project settings must set enable_symbols: true and enable_references: true
uv run pi-code-index --no-daemon refresh --json --repo /path/to/repo
uv run pi-code-index --no-daemon status --json --repo /path/to/repo
uv run pi-code-index --no-daemon graph callers --json --depth 2 --include-indirect --repo /path/to/repo "pkg.module.target"
uv run pi-code-index --no-daemon graph callees --json --depth 2 --include-indirect --repo /path/to/repo "pkg.module.target"
uv run pi-code-index --no-daemon graph impact --json --depth 2 --repo /path/to/repo "pkg.module.target"
```

Daemon/Pi path validation:

```bash
uv run pi-code-index stop --json || true
uv run pi-code-index graph callers --json --depth 1 --repo /path/to/repo "pkg.module.target"
uv run pi-code-index graph impact --json --depth 2 --repo /path/to/repo "src/pkg/module.py"
uv run pi-code-index status --json --repo /path/to/repo
```

## Implementation acceptance criteria

A future implementation issue is complete only when all are true:

- Graph extraction is gated by `enable_symbols`, `enable_references`, AST/hybrid chunking, and Python language support.
- `{prefix}_references` and `{prefix}_call_edges` are populated idempotently for Python files.
- `find_callers`, `find_callees`, and `impact_analysis` exist in backend, Coco backend, daemon, CLI, and Pi tool surfaces.
- Lexical and auto fallback payloads are successful empty graph payloads with explicit warnings.
- Response payloads follow this spec's common graph, caller/callee, and impact contracts.
- Ranking/confidence fields are deterministic and exposed for auditability.
- Status reports graph counts and capabilities.
- Existing search/symbol CLI, daemon, and Pi behavior remains unchanged.
- Unit, integration, CLI, daemon, and TypeScript/Pi tests cover the cases listed above.
- Manual validation commands pass in normal and CocoIndex/Postgres modes.
