# Repo understanding and quality context tools plan

## Scope

Planning-only document for issue `pi-code-index-3jo.5.1`. No product-code changes are included in this change.

Target outcome for the implementation chain:

- Add `repo_map`, `find_tests`, `find_similar_code`, and `review_context` capabilities for Pi.
- Let Pi ask for compact architecture maps, likely tests for a file/symbol/change, duplicate or drift-prone similar code, and review context for changed files/symbols.
- Reduce exploratory `find`/`grep`/`read` calls while preserving the existing CLI/Pi UX.
- Keep all existing tools and commands stable. New Pi tools, CLI subcommands, daemon request types, backend functions, status fields, and payload fields must be additive.
- Use CocoIndex V1 concepts only: `coco.App`, `coco.AppConfig`, `@coco.fn`, `@coco.fn(memo=True)`, `@coco.lifespan`, `coco.ContextKey`, `localfs.walk_dir`, `coco.map`, `coco.mount_each`, `postgres.mount_table_target`, `postgres.TableSchema.from_class`, `TableTarget.declare_row`, `TableTarget.declare_vector_index`, and explicit `asyncpg` DDL/query code where CocoIndex table targets are insufficient.

## Current state inspected

- `index.ts`
  - Registers `code_search`, `symbol_search`, `symbol_definition`, `symbol_context`, `find_callers`, `find_callees`, and `impact_analysis` Pi tools.
  - Tools shell out through `uv run --project <extension> pi-code-index ... --json` in Pi's active working directory.
  - Formatters produce compact bounded text and preserve raw JSON under `details.cli_json`.
  - Prompt guidance mentions symbol and graph tools, but no repo-quality context tools exist.
- `src/pi_code_index/cli.py`
  - Public commands are `init`, `search`, `refresh`, `status`, `stop`, `live start|stop|status`, `symbols search|definition|context`, and `graph callers|callees|impact`.
  - Auto-starts the daemon unless `--no-daemon` is used.
  - No repo map, test discovery, similar-code, or review-context commands exist.
- `src/pi_code_index/daemon.py`
  - Owns Unix socket lifecycle, version/config handshake, warm CocoIndex/Postgres resources, polling live indexing, and request dispatch.
  - `BackendResourceCache._key()` already includes schema/table/pipeline, chunking, symbol/reference/test feature flags, and AST config fields.
  - No request types exist for `repo_map`, `find_tests`, `find_similar_code`, or `review_context`.
- `src/pi_code_index/backend.py`
  - Routes `auto`, `lexical`, and `cocoindex` backends for search, status, refresh, symbols, graph, and impact analysis.
  - Lexical fallback returns safe empty payloads for tools that require canonical CocoIndex/Postgres data.
- `src/pi_code_index/coco_backend.py`
  - Defines canonical dataclasses and DDL for repos, branches, files, chunks, symbols, symbol embeddings, references, call edges, repo hierarchy, test links, freshness, and schema migrations.
  - Search can read canonical chunks or legacy embeddings and return additive metadata.
  - Symbol APIs and graph APIs already use canonical tables when enabled.
  - `repo_hierarchy` and `test_links` tables exist, but quality-context extraction/population and public queries are not exposed as first-class capabilities.
- `src/pi_code_index/config.py`
  - Defines `enable_symbols`, `enable_references`, `enable_test_links`, `chunk_strategy`, language lists, graph limits, and table/schema config.
  - `enable_test_links` defaults to `false`; repo map and similar-code specific gates do not exist.
- `src/pi_code_index/indexer.py`
  - Lexical backend supports file chunk indexing and token scoring only. It has no durable repo hierarchy, test link, or duplicate index.
- Docs inspected
  - `docs/architecture/final-integration-plan.md` defines canonical architecture, tables, compatibility, rollout, and validation.
  - `docs/architecture/ast-aware-semantic-search-plan.md` defines chunk/freshness/result metadata contracts.
  - `docs/architecture/symbol-intelligence-plan.md` defines symbol extraction and Pi/CLI contracts.
  - `docs/architecture/call-graph-impact-plan.md` defines reference/call graph/impact contracts and fallback rules.

## Non-goals

- Do not change or remove existing `code_search`, symbol, graph, live, status, refresh, or stop behavior.
- Do not require CocoIndex/Postgres for existing lexical search users.
- Do not introduce Docker commands; local container development examples must use Podman.
- Do not claim exact architectural ownership, test coverage, or duplicate detection when only heuristics are available.
- Do not add language-server, static type-checker, or external SaaS dependencies in the first implementation.
- Do not use CocoIndex APIs outside V1 concepts.

## Proposed user-facing capabilities

### `repo_map`

Returns a compact architecture map for the current repo or a subtree.

Typical uses:

- "Show me the architecture around `src/pi_code_index`."
- "What are the main modules and where should I edit config behavior?"
- "Before coding, map the extension/CLI/daemon/backend boundaries."

### `find_tests`

Returns likely tests for one or more files/symbols/change targets.

Typical uses:

- "What tests should I run after editing `src/pi_code_index/config.py`?"
- "Find likely tests for symbol `load_project_config`."
- "Suggest regression tests for this changed file list."

### `find_similar_code`

Returns similar code chunks/symbols/files to catch duplicates, parallel implementations, and drift risk.

Typical uses:

- "Find similar command handlers before adding a new subcommand."
- "Does another module already implement this fallback pattern?"
- "Show duplicate or near-duplicate parsing logic."

### `review_context`

Returns review-oriented context for changed files/symbols: ownership/module role, affected symbols, callers/callees, likely tests, similar code, freshness, and risks.

Typical uses:

- "Give review context for my changed files."
- "Before opening a PR, what should I inspect and test?"
- "What drift/duplicate risks does this change introduce?"

## Affected modules

### `index.ts`

Additive Pi tools:

```ts
repo_map({
  target?: string,          // repo-relative path, symbol target, or omitted for repo root
  depth?: number,           // default 2, max 5
  include_symbols?: boolean,// default true
  include_tests?: boolean,  // default false
  refresh?: boolean
})

find_tests({
  target: string | string[],// repo-relative file, symbol_id/name, file:line, or changed-file list
  top_k?: number,           // default 20, max 100
  include_indirect?: boolean,
  refresh?: boolean
})

find_similar_code({
  target?: string,          // symbol target, file path, file:line, or raw query/code snippet
  query?: string,           // optional natural-language intent or code text
  top_k?: number,           // default 12, max 100
  mode?: "semantic" | "lexical" | "hybrid",
  scope?: "chunks" | "symbols" | "files",
  exclude_self?: boolean,   // default true
  refresh?: boolean
})

review_context({
  targets: string[],        // changed files/symbols or file:line targets
  top_k?: number,           // default 30, max 200
  include_map?: boolean,
  include_tests?: boolean,
  include_similar?: boolean,
  include_impact?: boolean,
  refresh?: boolean
})
```

Formatting should mirror current symbol/graph formatters: bounded text sections, full JSON in `details.cli_json`, and unknown metadata ignored. Compact text should show paths, symbol names, confidence, and next-step guidance, not dense raw metadata.

Prompt guidance should add: use `repo_map` for architecture orientation; use `find_tests` before running or editing tests; use `find_similar_code` before adding new patterns; use `review_context` before final review or PR handoff.

### `src/pi_code_index/cli.py`

Additive command group, preserving existing commands:

```bash
pi-code-index context repo-map [--json] [--target TARGET] [--depth N] [--include-symbols/--no-include-symbols] [--include-tests/--no-include-tests] [--refresh] [--repo PATH]
pi-code-index context tests [--json] [--top-k N] [--include-indirect] [--refresh] [--repo PATH] TARGET [TARGET ...]
pi-code-index context similar [--json] [--top-k N] [--mode semantic|lexical|hybrid] [--scope chunks|symbols|files] [--exclude-self/--no-exclude-self] [--query QUERY] [--refresh] [--repo PATH] [TARGET]
pi-code-index context review [--json] [--top-k N] [--include-map/--no-include-map] [--include-tests/--no-include-tests] [--include-similar/--no-include-similar] [--include-impact/--no-include-impact] [--refresh] [--repo PATH] TARGET [TARGET ...]
```

`TARGET` should reuse existing target parsing where possible: `symbol_id`, qualified name/name, repo-relative path, `repo-relative-file:line[:column]`, or a JSON target for daemon-internal callers.

### `src/pi_code_index/daemon.py`

Add request types:

- `repo_map`
- `find_tests`
- `find_similar_code`
- `review_context`

Responses should include the same identity and compatibility metadata as existing symbol/graph payloads:

```json
{
  "ok": true,
  "backend": "cocoindex",
  "operation": "repo_map",
  "repo": "/path/to/repo",
  "repo_id": "...",
  "branch": "main",
  "branch_id": "...",
  "schema_version": 1,
  "pipeline_version": "canonical-v1-ast-v1",
  "capabilities": {
    "repo_hierarchy": true,
    "symbols": true,
    "references": true,
    "test_links": true,
    "similar_code": true,
    "review_context": true
  },
  "warning": null
}
```

Status should add quality-context readiness when CocoIndex is active:

```json
{
  "counts": {
    "repo_hierarchy_nodes": 42,
    "test_links": 18,
    "test_files": 12,
    "similarity_candidates": 900,
    "freshness_current": 120,
    "freshness_stale": 0
  },
  "capabilities": {
    "repo_map": true,
    "find_tests": true,
    "find_similar_code": true,
    "review_context": true
  }
}
```

### `src/pi_code_index/backend.py`

Add routing functions:

```python
repo_map(repo, target=None, depth=2, include_symbols=True, include_tests=False, refresh_first=False, coco_resources=None)
find_tests(repo, targets, top_k=20, include_indirect=False, refresh_first=False, coco_resources=None)
find_similar_code(repo, target=None, query=None, top_k=12, mode="hybrid", scope="chunks", exclude_self=True, refresh_first=False, coco_resources=None)
review_context(repo, targets, top_k=30, include_map=True, include_tests=True, include_similar=True, include_impact=True, refresh_first=False, coco_resources=None)
```

Fallback rules:

- `cocoindex`: return backend errors for real backend failures, consistent with existing operations.
- `auto`: if CocoIndex/Postgres is unavailable, return lexical best-effort only when honest and cheap; otherwise return a successful empty payload with `backend: lexical`, `backend_fallback: true`, and an explanatory warning.
- `lexical`: never pretend to have graph/test/architecture facts. It may provide file-path based test hints and lexical similar chunks with `confidence: low` and `fallback_reason: lexical_heuristic`.

### `src/pi_code_index/coco_backend.py`

Primary implementation point.

Planned additions:

1. Populate `repo_hierarchy` from files during canonical refresh.
2. Populate `test_links` when `enable_test_links: true`, initially from deterministic path/name/import heuristics over canonical files, chunks, symbols, and optional references.
3. Add query helpers for map construction, test discovery, similarity search, and review context composition.
4. Reuse existing `chunks` embeddings for chunk similarity and existing `symbol_embeddings` for symbol similarity. Do not introduce a second embedding model unless a later measured need exists.
5. Reuse existing graph/impact helpers when `review_context.include_impact` is true and references are enabled.
6. Add confidence and evidence metadata to every heuristic result.
7. Keep search/symbol/graph APIs independent; missing hierarchy/test/similar rows should produce warnings, not break existing tools.

Suggested internal dataclasses if product code chooses typed rows instead of plain dicts:

```python
@dataclass
class RepoMapNode: ...

@dataclass
class TestCandidate: ...

@dataclass
class SimilarCodeCandidate: ...

@dataclass
class ReviewContextSection: ...
```

### `src/pi_code_index/config.py`

Use existing config first:

- `enable_symbols` gates symbol-aware map/test/review enrichment.
- `enable_references` gates graph-backed review context and indirect test discovery.
- `enable_test_links` gates durable `test_links` population.
- `chunk_strategy`, `ast_languages`, `symbol_languages`, `max_graph_depth`, and `max_graph_edges` remain relevant.

Optional additive config only if implementation needs it:

```python
enable_repo_hierarchy: bool = True
similarity_candidate_limit: int = 5000
repo_map_max_nodes: int = 200
review_context_max_sections: int = 20
test_file_patterns: list[str] | None = None
source_test_path_patterns: list[dict[str, str]] | None = None
min_similar_code_score: float = 0.65
```

Defaults must preserve existing config loading. Add new fields to daemon resource cache keys only when they affect cached CocoIndex/Postgres behavior.

### `src/pi_code_index/indexer.py`

Keep lexical fallback simple and clearly labeled:

- `repo_map`: derive a shallow file tree from indexed file paths with no symbol/ownership claims.
- `find_tests`: match common path/name patterns such as `tests/test_<module>.py`, `<module>.test.ts`, `<module>.spec.ts`, and directories named `test`/`tests`.
- `find_similar_code`: reuse loaded lexical chunks and token scoring; return `fallback_reason: lexical_chunk_similarity`.
- `review_context`: compose lexical `repo_map`, `find_tests`, and similar chunks only; omit graph/symbol impact with warnings.

## Data/API contracts

### Shared envelope

Every new operation should use a shared additive envelope:

```json
{
  "ok": true,
  "backend": "cocoindex",
  "operation": "find_tests",
  "repo": "/path/to/repo",
  "repo_id": "...",
  "branch": "main",
  "branch_id": "...",
  "schema_version": 1,
  "pipeline_version": "canonical-v1-ast-v1",
  "target": "src/pi_code_index/config.py",
  "targets": ["src/pi_code_index/config.py"],
  "capabilities": {},
  "warning": null,
  "truncated": false,
  "truncation": {}
}
```

### `repo_map` payload

```json
{
  "operation": "repo_map",
  "target": "src/pi_code_index",
  "depth": 2,
  "nodes": [
    {
      "node_id": "...",
      "path": "src/pi_code_index",
      "name": "pi_code_index",
      "node_kind": "package",
      "parent_id": null,
      "summary": "CLI/daemon/backend package",
      "languages": ["python"],
      "file_count": 7,
      "symbol_count": 54,
      "test_count": 8,
      "key_symbols": [
        {"symbol_id": "...", "qualified_name": "pi_code_index.cli.main", "kind": "function", "filename": "src/pi_code_index/cli.py", "start_line": 120, "end_line": 260}
      ],
      "metadata": {"confidence": 0.8, "evidence": ["path", "symbols", "imports"]}
    }
  ],
  "edges": [
    {"from_node_id": "...", "to_node_id": "...", "edge_kind": "contains", "confidence": 1.0}
  ]
}
```

### `find_tests` payload

```json
{
  "operation": "find_tests",
  "targets": ["src/pi_code_index/config.py"],
  "results": [
    {
      "test_file": "tests/test_config.py",
      "test_symbol": "test_load_project_config_validates_chunk_strategy",
      "target_file": "src/pi_code_index/config.py",
      "target_symbol_id": null,
      "score": 0.91,
      "confidence": 0.88,
      "evidence": ["path_pattern", "name_overlap", "imports_target_module"],
      "recommended_command": "uv run pytest tests/test_config.py",
      "metadata": {"source": "heuristic-v1"}
    }
  ]
}
```

### `find_similar_code` payload

```json
{
  "operation": "find_similar_code",
  "target": "src/pi_code_index/cli.py:130",
  "query": null,
  "mode": "hybrid",
  "scope": "chunks",
  "results": [
    {
      "score": 0.86,
      "similarity": {"semantic": 0.78, "lexical": 0.64, "structure": 0.55},
      "filename": "src/pi_code_index/cli.py",
      "start_line": 180,
      "end_line": 220,
      "code": "...",
      "symbol": "main",
      "chunk_id": "...",
      "risk": "parallel_command_handler",
      "metadata": {"excluded_self": true, "ranking_profile": "similar-code-v1"}
    }
  ]
}
```

### `review_context` payload

```json
{
  "operation": "review_context",
  "targets": ["src/pi_code_index/cli.py", "index.ts"],
  "summary": {
    "changed_files": 2,
    "affected_symbols": 8,
    "likely_tests": 5,
    "similar_code_hits": 4,
    "risk_level": "medium"
  },
  "sections": [
    {
      "section": "architecture",
      "items": [{"path": "src/pi_code_index/cli.py", "role": "public CLI boundary", "evidence": ["command parser", "daemon request construction"]}]
    },
    {
      "section": "tests",
      "items": []
    },
    {
      "section": "similar_code",
      "items": []
    },
    {
      "section": "impact",
      "items": []
    },
    {
      "section": "risks",
      "items": [{"risk": "CLI/Pi contract drift", "mitigation": "run TypeScript formatter tests and CLI JSON tests"}]
    }
  ],
  "recommended_commands": ["npm run typecheck", "uv run pytest"]
}
```

## Indexing and query strategy

### Repo map

Indexing:

1. During canonical refresh, derive hierarchy nodes from every indexed file path.
2. Create stable node IDs from `repo_id`, `branch_id`, `node_kind`, and normalized path.
3. Classify nodes as `root`, `directory`, `package`, `module`, `test_directory`, or `test_file` using path and language metadata.
4. Attach summaries as deterministic metadata only: language counts, file counts, symbol counts, test counts, top symbol kinds, and feature flags. Do not require LLM summarization.

Query:

1. Resolve `target` to path or symbol/file and choose an anchor node.
2. Traverse parent/child nodes up to bounded `depth`.
3. Join `files`, `symbols`, and `test_links` for counts and key symbols.
4. Rank nodes by proximity to anchor, file/symbol density, public symbols, and direct test links.
5. Truncate by node budget and include `truncation.omitted_nodes`.

### Test discovery

Indexing:

1. Identify test files by path patterns and framework naming conventions.
2. Extract test symbols when symbols are enabled: Python functions/classes starting with `test_`, TypeScript/Jest-style names later only when parser support exists.
3. Populate `test_links` when `enable_test_links` is true using deterministic evidence.

Heuristics:

- Path proximity: `src/foo/bar.py` -> `tests/test_bar.py`, `tests/foo/test_bar.py`, `src/foo/test_bar.py`.
- Naming: `foo.py` -> `test_foo.py`, `foo_test.py`, `foo.test.ts`, `foo.spec.ts`.
- Import/reference evidence: test chunk imports module path or calls a target symbol.
- Symbol overlap: target symbol name appears in test symbol name or test body.
- Graph evidence: tests call affected symbols through call edges when references are enabled.
- Directory ownership: nearest test directory under same package/subtree.

Scoring:

```text
score = 0.35 path_proximity
      + 0.25 import_or_reference_evidence
      + 0.20 name_overlap
      + 0.10 graph_reachability
      + 0.10 freshness
```

Every result should include evidence labels and confidence. Low-confidence hints remain useful but must be labeled.

### Similar code

Indexing:

1. Reuse existing chunk embeddings from `chunks.embedding`.
2. Reuse `symbol_embeddings` for symbol-level similarity when `enable_symbols` is true.
3. Add lightweight structural metadata to chunk/symbol metadata where already available: language, symbol kind, chunk kind, decorators, parser/source version.
4. Avoid creating a new duplicate table until query performance requires it.

Query:

1. Resolve `target` to symbol/chunk/file or build an embedding from `query` text/code.
2. Fetch semantic candidates with pgvector from `chunks` or `symbol_embeddings`.
3. Fetch lexical candidates by token overlap/name/path patterns for hybrid mode.
4. Remove exact self matches when `exclude_self` is true.
5. Blend semantic, lexical, and structure similarity.
6. Label likely duplicate/drift risks, for example `parallel_command_handler`, `similar_config_validation`, `duplicate_fallback_payload`, or `shared_test_pattern`.

Fallback:

- Lexical backend can run token similarity over JSON chunks, labeled `backend: lexical` and `confidence: low`.
- If a raw code/query target is provided and embeddings are unavailable, use lexical scoring only.

### Review context

Composition strategy:

1. Normalize targets to files and symbols.
2. Build a small repo map around changed files.
3. Add direct symbols and chunks for each target.
4. Add call graph/impact section if `enable_references` is true; otherwise include a warning.
5. Add likely tests from `test_links` and path/name heuristics.
6. Add similar-code hits filtered to risk-prone matches.
7. Add freshness and capability status.
8. Return recommended validation commands based on changed file types and matched tests.

Review context should be an orchestrator over already-indexed data, not a new index. It should call internal helpers for map, tests, similar code, and graph impact.

## Fallback behavior

- Existing `code_search` remains the recommended fallback for broad semantic questions.
- `repo_map` in lexical mode returns a path-only map with `capabilities.repo_hierarchy: false` and `warning` explaining that canonical hierarchy requires CocoIndex/Postgres.
- `find_tests` in lexical mode returns path/name matches only, with `confidence <= 0.5` unless explicit same-path evidence exists.
- `find_similar_code` in lexical mode returns token-similar chunks only and sets `fallback_reason: lexical_chunk_similarity`.
- `review_context` in lexical mode composes available lexical hints and omits symbol/graph-backed sections with warnings.
- In `auto`, CocoIndex failure should follow existing patterns: fallback to lexical if possible and set `backend_fallback: true`.

## Risks and mitigations

- **CLI/Pi UX drift**: add only new tools/commands; keep existing command names, flags, result fields, and compact formatting stable.
- **Overconfident heuristics**: include `confidence`, `score`, and `evidence` on each result; use warnings for missing parser/graph/test capabilities.
- **Duplicate detection false positives**: expose components of similarity score and rank same-language/same-kind matches higher, but keep top-k bounded.
- **Performance on large repos**: bound node, edge, candidate, and result budgets; use repo/branch/path indexes; use pgvector for semantic candidate retrieval.
- **Stale review context**: include freshness status and support `refresh`; status should report stale/error counts.
- **Cross-language gaps**: start with language-agnostic path/chunk metadata plus Python symbol/test enrichment already supported; add parser-specific enrichments later.
- **Config/cache mismatch**: add new behavior-affecting config keys to `BackendResourceCache._key()` before product rollout.
- **Schema migration complexity**: prefer using existing `repo_hierarchy` and `test_links` tables before adding new durable tables.
- **Review context overload**: enforce section budgets and keep full details in structured JSON.

## Rollout plan

1. **Contract lock**
   - Add tests/specs for the four JSON payload contracts and compact formatter behavior.
   - Document fallback warnings and required capabilities.

2. **Repo map foundation**
   - Populate/query `repo_hierarchy` from canonical files.
   - Add `repo_map` backend/CLI/daemon/Pi tool with path-only and symbol-enriched modes.

3. **Test discovery**
   - Implement test-file detection and path/name heuristics.
   - Populate `test_links` when `enable_test_links: true`.
   - Add `find_tests` tool/command and status counts.

4. **Similar code**
   - Implement chunk/symbol similarity queries using existing embeddings plus lexical blending.
   - Add lexical fallback over JSON chunks.
   - Add risk labels and truncation metadata.

5. **Review context composition**
   - Compose repo map, tests, similar code, symbols, freshness, and optional graph/impact into one bounded payload.
   - Add recommended validation commands.

6. **Docs and examples**
   - Update README tool list and examples.
   - Update example project settings only for additive config.
   - Keep Podman-based CocoIndex/Postgres validation snippets.

## Validation commands

Planning-only validation for this issue:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
git diff -- docs/architecture/repo-quality-context-plan.md
```

Future product-code validation should include:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
scripts/setup.sh
npm run typecheck
npm run test:ts
uv run python -m compileall src tests
uv run pytest
uv run pi-code-index --help
uv run pi-code-index --no-daemon search --json --refresh "where is config loaded"
uv run pi-code-index --no-daemon context repo-map --json --target src/pi_code_index --depth 2
uv run pi-code-index --no-daemon context tests --json src/pi_code_index/config.py
uv run pi-code-index --no-daemon context similar --json --query "CLI subcommand dispatch" --top-k 5
uv run pi-code-index --no-daemon context review --json src/pi_code_index/cli.py index.ts
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
uv run pi-code-index context repo-map --json --target src/pi_code_index --depth 2
uv run pi-code-index context tests --json src/pi_code_index/config.py
uv run pi-code-index context similar --json --target src/pi_code_index/cli.py:1 --top-k 8
uv run pi-code-index context review --json src/pi_code_index/cli.py index.ts
podman exec pi-code-index-postgres psql -U cocoindex -d cocoindex \
  -c "SELECT count(*) AS hierarchy_nodes FROM pi_code_index_repo_hierarchy; SELECT count(*) AS test_links FROM pi_code_index_test_links;"
```

## Open decisions before product-code changes

- Whether `repo_map` should be a standalone CLI group (`repo map`) or live under a shared `context` group.
- Whether `enable_repo_hierarchy` should exist or hierarchy should always populate with canonical files.
- How much lexical fallback should be shown in compact Pi text versus only in structured details.
- Whether similar-code risk labels should be rule-based only or configurable by repository conventions.
- Whether review context should support automatic changed-file discovery from Git, or require explicit targets for the first rollout.
