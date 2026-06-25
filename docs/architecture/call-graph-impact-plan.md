# Call graph and impact analysis plan

## Scope

Planning-only document for issue `pi-code-index-3jo.4.1`. No product-code changes are included in this change.

Target outcome for the implementation chain:

- Index reference and call edges in the existing CocoIndex/Postgres canonical model.
- Add `find_callers`, `find_callees`, and `impact_analysis` Pi tools and additive CLI/daemon/backend commands.
- Let Pi answer caller/callee/blast-radius questions with ranked direct and indirect relationships, affected files, and likely affected tests.
- Preserve existing `code_search`, symbol tools, CLI commands, daemon protocol behavior, and lexical fallback UX. New tool/command surfaces are additive only.
- Use CocoIndex V1 concepts only: `coco.App`, `coco.AppConfig`, `@coco.fn`, `@coco.fn(memo=True)`, `@coco.lifespan`, `coco.ContextKey`, `localfs.walk_dir`, `coco.map`, `coco.mount_each`, `postgres.mount_table_target`, `postgres.TableSchema.from_class`, `TableTarget.declare_row`, `TableTarget.declare_vector_index`, and idempotent `asyncpg` DDL/query code where CocoIndex table targets are insufficient.

## Current state inspected

- `index.ts`
  - Registers `code_search`, `symbol_search`, `symbol_definition`, and `symbol_context` tools.
  - Tools shell out to `uv run --project <extension> pi-code-index ... --json` in Pi's active cwd.
  - Compact formatters display only bounded text while raw JSON is preserved in `details.cli_json`.
  - Prompt guidance currently mentions symbol tools and `code_search`; no call graph tools exist.
- `src/pi_code_index/cli.py`
  - Public commands are `init`, `search`, `refresh`, `status`, `stop`, `live start|stop|status`, and `symbols search|definition|context`.
  - Auto-starts the daemon unless `--no-daemon` is used.
  - No references/calls/impact command group exists.
- `src/pi_code_index/daemon.py`
  - Handles `search`, `refresh`, symbol request types, live request types, status, and stop.
  - `BackendResourceCache` key already includes `enable_references`, `enable_test_links`, symbol/AST options, schema/table prefix, branch mode, and pipeline version.
  - No caller/callee/impact request types exist.
- `src/pi_code_index/backend.py`
  - Routes `auto`, `lexical`, and `cocoindex` backends for search/status/refresh and symbol operations.
  - Lexical fallback for symbols returns safe empty payloads with warnings; no call graph fallback exists.
- `src/pi_code_index/coco_backend.py`
  - Defines canonical dataclasses and DDL for `ReferenceRow`, `CallEdgeRow`, `TestLinkRow`, plus `symbols`, `chunks`, `files`, `freshness`, and `symbol_embeddings`.
  - DDL already creates `{prefix}_references` and `{prefix}_call_edges` with indexes on reference name, reference symbol, caller, and callee.
  - `refresh()` result/status counters already include `references`, `call_edges`, and `test_links`, but counts remain zero because extraction/population is not implemented.
  - `_symbol_base_payload()` currently reports `capabilities.references: false`.
  - Python AST symbol extraction exists; reference/call extraction and public graph queries do not.
- `src/pi_code_index/config.py`
  - `ProjectConfig` already has `enable_references` and `enable_test_links`, both defaulting to `false`.
  - Symbol and AST config supports Python-first extraction with bounded payload fields.
- `src/pi_code_index/indexer.py`
  - Lexical JSON backend supports chunk search only.
  - It has no symbol table, reference index, or call graph.
- Docs inspected
  - `docs/architecture/final-integration-plan.md` / spec define canonical schema and compatibility rules.
  - `docs/architecture/ast-aware-semantic-search-plan.md` / spec define AST chunk metadata and freshness conventions.
  - `docs/architecture/symbol-intelligence-plan.md` / spec define symbol identity, symbol tool contracts, and Python-first parser boundaries.

## Non-goals

- Do not remove or change existing Pi tools, CLI flags, daemon request types, JSON fields, or compact text output.
- Do not require CocoIndex/Postgres for users who rely on `backend: auto` lexical search.
- Do not add a full static type checker, language server dependency, or cross-repository graph in the first implementation.
- Do not claim high-confidence edges for dynamic calls that cannot be resolved statically.
- Do not add parser dependencies for TypeScript/JavaScript, Go, Rust, or Java without a separate dependency decision issue.
- Do not use non-V1 CocoIndex APIs.

## Affected modules

### `index.ts`

Additive Pi tools:

1. `find_callers`
2. `find_callees`
3. `impact_analysis`

Recommended parameter shapes:

```ts
find_callers({
  target: string,          // symbol_id, qualified name, name, or file:line[:column]
  depth?: number,         // default 1, max 5
  top_k?: number,         // default 20, max 100
  include_indirect?: boolean,
  refresh?: boolean
})

find_callees({
  target: string,
  depth?: number,
  top_k?: number,
  include_indirect?: boolean,
  refresh?: boolean
})

impact_analysis({
  target: string,          // symbol target or repo-relative file path
  depth?: number,         // default 2, max 5
  top_k?: number,         // default 50, max 200
  include_tests?: boolean,
  include_files?: boolean,
  refresh?: boolean
})
```

Formatting should mirror current symbol tools: compact ranked lines in `content`, full raw JSON in `details.cli_json`, and bounded display counts in `details.display`. Unknown future fields must be ignored.

Prompt guidance should add: use `find_callers`, `find_callees`, or `impact_analysis` for caller/callee/blast-radius questions; use `symbol_definition` or `read` to inspect exact source before editing.

### `src/pi_code_index/cli.py`

Additive command group, keeping existing commands unchanged:

```bash
pi-code-index graph callers [--json] [--top-k N] [--depth N] [--include-indirect] [--refresh] [--repo PATH] TARGET
pi-code-index graph callees [--json] [--top-k N] [--depth N] [--include-indirect] [--refresh] [--repo PATH] TARGET
pi-code-index graph impact [--json] [--top-k N] [--depth N] [--include-tests/--no-include-tests] [--include-files/--no-include-files] [--refresh] [--repo PATH] TARGET
```

`TARGET` should reuse the symbol target parser contract from `symbols definition`: stable `symbol_id`, qualified name/name, `repo-relative-file:line[:column]`, and optionally a structured JSON target for daemon-internal callers.

### `src/pi_code_index/daemon.py`

Add request types:

- `find_callers`
- `find_callees`
- `impact_analysis`

Each request carries `repo`, `target`, `depth`, `top_k`, flags, filters if added later, and `refresh`. Responses should include the same identity metadata as symbol payloads: `schema_version`, `pipeline_version`, `repo_id`, `branch`, `branch_id`, `capabilities`, and `warning`.

Status should report graph readiness:

```json
{
  "counts": {
    "references": 123,
    "call_edges": 45,
    "unresolved_references": 12,
    "low_confidence_call_edges": 8,
    "test_links": 10
  },
  "capabilities": {
    "references": true,
    "call_graph": true,
    "impact_analysis": true
  }
}
```

### `src/pi_code_index/backend.py`

Add routing functions:

```python
find_callers(repo, target, depth=1, top_k=20, include_indirect=False, refresh_first=False, coco_resources=None)
find_callees(repo, target, depth=1, top_k=20, include_indirect=False, refresh_first=False, coco_resources=None)
impact_analysis(repo, target, depth=2, top_k=50, include_tests=True, include_files=True, refresh_first=False, coco_resources=None)
```

Fallback rules:

- `cocoindex`: return errors for real backend failures as existing operations do.
- `auto`: if CocoIndex is unavailable, return a successful empty payload with `backend: lexical`, `backend_fallback: true`, and a warning that call graph tools require CocoIndex/Postgres reference indexing.
- `lexical`: return empty graph payloads with the same warning; do not scan text and pretend to have a graph in the initial rollout.

### `src/pi_code_index/coco_backend.py`

Primary implementation point.

Planned additions:

1. Extract references and call expressions from parsed AST files when `enable_references: true`.
2. Resolve references to indexed symbols within the current repo/branch.
3. Populate existing `{prefix}_references` and `{prefix}_call_edges` tables idempotently during canonical AST population.
4. Query direct and bounded transitive call graph relationships with recursive SQL or iterative breadth-first traversal in Python.
5. Rank graph edges and paths with a deterministic confidence model.
6. Join graph results to `symbols`, `files`, `chunks`, `freshness`, and optional `test_links` for blast-radius payloads.
7. Keep existing semantic `search` and symbol APIs independent from graph tables; missing graph rows should produce warnings, not break search.

Suggested internal helpers:

```python
@dataclass
class ExtractedReference: ...
@dataclass
class ExtractedCall: ...
@dataclass
class GraphPath: ...

def reference_id_for(repo_id, branch_id, file_id, name, kind, line, column): ...
def call_edge_id_for(repo_id, branch_id, caller_symbol_id, callee_symbol_id, source_span): ...
```

### `src/pi_code_index/config.py`

Use existing gates first:

- `enable_references: bool = False` gates reference/call extraction and graph tool readiness.
- `enable_test_links: bool = False` gates test link enrichment when implemented.
- `enable_symbols: true` and `chunk_strategy in {"ast", "hybrid"}` are prerequisites for meaningful call graph extraction.

Optional additive config only if implementation needs it:

```python
max_graph_depth: int = 5
max_graph_edges: int = 5000
reference_languages: list[str] | None = None
min_call_edge_confidence: float = 0.35
```

Defaults must preserve existing config loading. If added, validate with the same positive-int/list patterns already used.

### `src/pi_code_index/indexer.py`

No persistent lexical graph is recommended for the first implementation. Safe fallback payloads are preferred because regex caller/callee extraction would be misleading. Later, a separate issue may add best-effort lexical `grep` hints clearly labeled `fallback_reason: lexical_text_scan`.

## Extraction strategy

### Phase 1 language: Python

Use the existing Python `ast` parse in `extract_ast_chunks()`/canonical population as the source of truth. Only produce graph rows when symbols are enabled and the file parsed successfully.

For each supported Python file:

1. Build a local symbol scope map from extracted module/class/function/method symbols.
2. Walk every function/method/class/module body with parent symbol context.
3. Emit `ReferenceRow` candidates for:
   - `ast.Name` loads: `name`, kind `name`.
   - `ast.Attribute`: `attr` and best-effort dotted text, kind `attribute`.
   - import aliases: kind `import` / `import_from` where cheap.
   - call sites: kind `call` with source span metadata.
4. Emit `CallEdgeRow` only when a call site resolves to an indexed symbol with acceptable confidence.
5. Store unresolved call/reference rows with `symbol_id = null` in `references`; do not create call edges for unresolved calls.

### Python call resolution tiers

Rank resolution by confidence:

| Pattern | Example | Resolution | Base confidence |
| --- | --- | --- | --- |
| Same-scope direct function | `helper()` | nearest symbol in lexical/module scope | `0.95` |
| Method on `self` / `cls` | `self.save()` | method on containing class/MRO not modeled yet | `0.90` if same class, `0.70` if class uncertain |
| Qualified module function | `config.load_project_config()` | import alias/module map plus qualified name | `0.85` |
| Class constructor | `Widget(...)` | class symbol in scope | `0.80` |
| Imported direct name | `load_project_config()` after `from x import load_project_config` | import map to qualified symbol | `0.80` |
| Attribute on unknown object | `client.send()` | unresolved reference only | no edge |
| Dynamic call | `getattr(x, name)()` / callable variable | unresolved reference only | no edge |

Confidence modifiers:

- `+0.03` exact qualified-name match.
- `+0.02` same file.
- `-0.10` ambiguous multiple symbols with same name.
- `-0.15` stale/error freshness on caller or callee.
- Clamp to `[0.0, 1.0]`.

### Multi-language rollout

Phase 1: Python only. Later phases can add parser adapters after dependency decisions:

- TypeScript/JavaScript: likely tree-sitter or TypeScript compiler API via a separate dependency decision; dynamic dispatch remains low confidence.
- Go: parser can resolve package-level functions reasonably, method receivers require type context.
- Rust: function/method paths are parseable, trait dispatch is often unresolved without compiler analysis.
- Java: method calls require type/classpath context; start with same-class/static calls only.

Unsupported languages must keep chunk/search indexing current and set graph capability warnings rather than producing misleading edges.

## Data/API contracts

### Existing tables reused

Use the canonical tables already created by `ensure_canonical_schema()`:

- `{prefix}_references`
- `{prefix}_call_edges`
- `{prefix}_symbols`
- `{prefix}_files`
- `{prefix}_chunks`
- `{prefix}_freshness`
- `{prefix}_test_links` when test enrichment is enabled

### `{prefix}_references` metadata

Columns already exist:

```text
reference_id text primary key
repo_id text
branch_id text
file_id text
symbol_id text null
name text
kind text
line integer
column_number integer
metadata jsonb
```

Recommended metadata:

```json
{
  "language": "python",
  "parser": "python_ast",
  "extractor_version": "reference-extractor-v1",
  "source_hash": "...",
  "caller_symbol_id": "...",
  "target_qualified_name": "pi_code_index.config.load_project_config",
  "dotted_name": "config.load_project_config",
  "span": {"start_line": 10, "end_line": 10, "start_col": 4, "end_col": 29},
  "resolution": {
    "status": "resolved|unresolved|ambiguous",
    "strategy": "same_scope|self_method|import_alias|qualified_name|unknown_attribute",
    "candidate_symbol_ids": [],
    "confidence": 0.85
  },
  "freshness_status": "current"
}
```

### `{prefix}_call_edges` metadata

Columns already exist:

```text
edge_id text primary key
repo_id text
branch_id text
caller_symbol_id text
callee_symbol_id text
confidence real
source text
metadata jsonb
```

Recommended metadata:

```json
{
  "language": "python",
  "reference_id": "...",
  "callsite": {"file_id": "...", "path": "src/x.py", "line": 42, "column": 8},
  "resolution_strategy": "import_alias",
  "edge_kind": "call",
  "direct": true,
  "freshness_status": "current",
  "confidence_factors": {
    "base": 0.85,
    "same_file_bonus": 0.0,
    "ambiguity_penalty": 0.0,
    "freshness_penalty": 0.0
  }
}
```

### Tool response contract: common graph fields

All graph payloads should include:

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
  "target_symbol": {"symbol_id": "...", "qualified_name": "..."},
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

### `find_callers` / `find_callees` result item

```json
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
    "freshness_penalty": 0.0,
    "test_boost": 0.0,
    "final_score": 0.91
  }
}
```

### `impact_analysis` response additions

```json
{
  "affected_symbols": [],
  "affected_files": [
    {
      "filename": "src/pkg/caller.py",
      "score": 0.87,
      "relationship_counts": {"direct_callers": 2, "indirect_callers": 4, "callees": 1},
      "highest_confidence_path": 0.93,
      "freshness_status": "current"
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
    "affected_files": 3,
    "affected_tests": 1,
    "truncated": false
  }
}
```

## Ranking and confidence model

### Caller/callee ranking

Score each related symbol/path with:

```text
final_score =
  0.45 * path_confidence
+ 0.25 * directness_score
+ 0.15 * symbol_relevance
+ 0.10 * freshness_score
+ 0.05 * test_or_entrypoint_boost
```

Where:

- `path_confidence` is the product of edge confidences along the shortest/highest-confidence path.
- `directness_score = 1 / distance`.
- `symbol_relevance` boosts exact target-adjacent relationships, same module/file, exported/public symbols, and symbols with matching names.
- `freshness_score = 1.0` for current, `0.7` for stale, `0.5` for parser error-adjacent, `0.0` for deleted.
- `test_or_entrypoint_boost` helps impact analysis surface tests and command/API entrypoints, but should not dominate confidence.

Return ranking breakdowns in metadata for auditability.

### Traversal limits

- Default direct queries use `depth=1`; indirect enabled queries default to `depth=2`.
- Hard cap `depth <= 5` and `top_k <= 200`.
- Stop expanding when cumulative path confidence falls below `min_call_edge_confidence` or when edge budget is exhausted.
- Deduplicate by symbol and keep the highest-scoring path plus a bounded list of alternate paths.

## Fallback behavior

- If `enable_references` is false: return `ok: true`, empty results, and warning `reference indexing is disabled; set enable_references: true with enable_symbols: true and chunk_strategy: hybrid|ast`.
- If symbol tables are missing: return warning to run `pi-code-index refresh --json` after enabling symbols.
- If call edge tables exist but are empty: return warning that no call edges were indexed for the current repo/branch.
- If target is ambiguous: return `matches` using the same symbol item contract as `symbol_definition`; ask caller to retry with `symbol_id`.
- If target cannot be resolved but is a file path, `impact_analysis` may fall back to all symbols in that file and report `target_kind: file`.
- In lexical backend or `auto` fallback: return empty graph payloads with a warning, not product errors.
- Parser errors should not break the whole graph. They should appear in status counts and warnings.

## Risks and language limits

- Python dynamic dispatch, monkey-patching, decorators, dependency injection, and calls through variables cannot be resolved reliably with `ast` only.
- Same-name symbols can create false positives if import alias and scope resolution are incomplete.
- Recursive SQL over dense graphs can be expensive; enforce depth and edge caps.
- Stable IDs currently include start line, so moving code can churn graph edges. This is consistent with the current symbol contract but should be visible in rollout notes.
- `enable_references` may increase refresh time and database size substantially on large repos.
- Existing canonical DDL has no unique constraint on call edge endpoints, so idempotent inserts must use stable `edge_id` and explicit delete/repopulate per file or upsert by primary key.
- Cross-language and generated-code repos will have incomplete graphs until language adapters exist.
- Test impact requires `test_links`; until that feature is implemented, `affected_tests` should use only conservative path/import/call hints and label confidence accordingly.

## Tests and validation

### Unit tests

- Python AST reference extraction:
  - same-scope calls
  - class methods and `self.method()`
  - direct imports and import aliases
  - unresolved dynamic calls
  - ambiguous same-name symbols
  - parser errors/fallback behavior
- Stable IDs for references and call edges.
- Confidence scoring and ranking breakdowns.
- Target resolution reuse from symbol tools.
- Bounded traversal and deduplication.
- Lexical fallback payload shape and warnings.

### Integration tests

- CocoIndex/Postgres refresh populates `references` and `call_edges` when `enable_references=true`.
- `find_callers` returns direct and optional indirect callers with paths/callsite metadata.
- `find_callees` returns direct and optional indirect callees.
- `impact_analysis` returns affected symbols/files and conservative affected tests.
- Daemon request types reuse warm resources and return schema/pipeline metadata.
- `status --json` reports reference/call edge counts.
- Existing `code_search` and symbol tests remain unchanged.

### TypeScript/Pi tests

- Parameter schema tests for all three new tools.
- Formatter tests for empty, warning, direct, indirect, ambiguous, and truncated payloads.
- Verify raw CLI JSON remains in `details.cli_json`.
- Verify current `code_search` and symbol formatter snapshots remain unchanged.

### Manual validation commands

From `~/.pi/agent/extensions/pi-code-index`:

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
uv run pi-code-index --no-daemon graph callers --json --depth 2 "pkg.module.target" --repo /path/to/repo
uv run pi-code-index --no-daemon graph callees --json --depth 2 "pkg.module.target" --repo /path/to/repo
uv run pi-code-index --no-daemon graph impact --json --depth 2 "pkg.module.target" --repo /path/to/repo
```

Daemon/Pi path validation:

```bash
uv run pi-code-index stop --json || true
uv run pi-code-index graph callers --json --depth 1 "pkg.module.target" --repo /path/to/repo
uv run pi-code-index status --json --repo /path/to/repo
```

## Rollout plan

1. **Spec and tests first**
   - Convert this plan into a buildable spec.
   - Add tests for payload contracts, extractor cases, fallback warnings, and CLI/daemon routing.
2. **Python extraction behind gates**
   - Populate references and call edges only when `enable_symbols=true`, `enable_references=true`, and `chunk_strategy` is `ast` or `hybrid`.
   - Keep defaults off.
3. **Backend/CLI/daemon APIs**
   - Add graph routing and safe fallbacks.
   - Add status counts and warnings.
4. **Pi tools**
   - Register additive tools and compact formatters.
   - Update prompt guidance without changing current `code_search` guidance.
5. **Impact enrichment**
   - Start with affected symbols/files from caller graph.
   - Add tests only from existing `test_links` when available; otherwise conservative path/import/call hints with low confidence.
6. **Performance and correctness pass**
   - Validate on this extension repo and at least one medium Python repo.
   - Tune caps and confidence thresholds.
7. **Future language adapters**
   - File separate dependency/design issues before adding non-Python parsers.

## Open questions for implementation issue

- Should `graph` be the CLI group name, or should commands be top-level (`callers`, `callees`, `impact`) for shorter human use?
- Should `impact_analysis` expand both callers and callees by default, or prioritize callers as the primary blast-radius direction?
- Should references for imports be included in caller/callee traversal or exposed later as a separate import graph?
- What threshold should hide low-confidence edges from compact Pi text while keeping them in `details.cli_json`?
