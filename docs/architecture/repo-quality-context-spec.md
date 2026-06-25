# Repo understanding and quality context tools spec

## Scope

This specification converts `docs/architecture/repo-quality-context-plan.md` into buildable requirements for issue `pi-code-index-3jo.5.2` and the following implementation issue(s).

The implementation must add repository-understanding and quality-context capabilities for CocoIndex-backed repositories while preserving the existing CLI, daemon, and Pi UX. Product changes are additive only: existing `code_search`, symbol tools, graph tools, `search`, `refresh`, `status`, `stop`, `live`, daemon handshake behavior, and lexical fallback behavior remain backward-compatible.

This document is specs/docs only. It does not implement product code.

## Architecture references

Implementers must keep these documents consistent:

- `docs/architecture/final-integration-spec.md`: canonical schema, repo/branch/file identity, daemon compatibility, status conventions, and stable `code_search` behavior.
- `docs/architecture/ast-aware-semantic-search-spec.md`: chunk metadata, freshness, ranking metadata, bounded result payloads, and parser fallback behavior.
- `docs/architecture/symbol-intelligence-spec.md`: symbol identity, target resolution, symbol item contracts, and Python AST parser boundaries.
- `docs/architecture/call-graph-impact-spec.md`: reference/call graph/test-link schema, graph fallback rules, and impact-analysis contracts.
- `docs/architecture/repo-quality-context-plan.md`: roadmap, rationale, and rollout context.

## Current code inspected

The spec is based on the current repository state of:

- `index.ts`
  - Registers Pi tools `code_search`, `symbol_search`, `symbol_definition`, `symbol_context`, `find_callers`, `find_callees`, and `impact_analysis`.
  - Each tool shells out with `uv run --project <extension> pi-code-index ... --json` in Pi's active working directory.
  - Compact formatter output is bounded and raw CLI JSON is preserved under `details.cli_json`.
  - Prompt guidance mentions symbol and graph tools, but no repo-quality context tools exist.
- `src/pi_code_index/cli.py`
  - Public commands are `init`, `search`, `refresh`, `status`, `stop`, `live start|stop|status`, `symbols search|definition|context`, and `graph callers|callees|impact`.
  - The CLI auto-starts the daemon unless `--no-daemon` is passed.
  - No `context` command group exists.
- `src/pi_code_index/daemon.py`
  - Handles request types `handshake`, `search`, `refresh`, symbol requests, graph requests, live requests, `status`, and `stop`.
  - `BackendResourceCache._key()` includes repo/backend identity, Postgres/embedding config, schema/table/pipeline identity, branch mode, symbol/reference/test-link gates, AST/chunking config, and include/exclude patterns.
  - No request types exist for `repo_map`, `find_tests`, `find_similar_code`, or `review_context`.
- `src/pi_code_index/backend.py`
  - Routes `auto`, `lexical`, and `cocoindex` for refresh/search/status, symbol operations, graph operations, and impact analysis.
  - Lexical fallback for symbol/graph operations returns honest empty payloads and warnings instead of pretending canonical data exists.
  - No routing functions exist for repo-quality context operations.
- `src/pi_code_index/coco_backend.py`
  - Defines canonical rows for repos, branches, files, chunks, symbols, references, call edges, repo hierarchy, test links, and freshness.
  - Creates canonical tables and indexes for `repo_hierarchy` and `test_links`, but does not populate them as first-class quality-context data or expose public queries for these tools.
  - Search, symbol, graph, and impact APIs already use canonical tables when enabled.
- `src/pi_code_index/config.py`
  - Defines `enable_symbols`, `enable_references`, `enable_test_links`, `chunk_strategy`, AST/symbol/reference language options, graph limits, and table/schema settings.
  - `enable_test_links` defaults to `false`; no repo-map or similar-code-specific config exists.
- `src/pi_code_index/indexer.py`
  - The lexical backend stores file chunks and token-search metadata only. It has no durable hierarchy, symbol graph, test-link, or duplicate index.
- `tests/`
  - Contains TypeScript formatter tests plus Python tests for canonical foundation, CocoIndex/Postgres integration, daemon lifecycle, lexical indexing, symbols, graph, and impact.

## Non-goals

- Do not remove, rename, or repurpose existing tools, CLI commands, CLI flags, daemon request types, JSON fields, compact formatter text, or status fields.
- Do not require CocoIndex/Postgres for users who rely on `backend: auto` lexical search.
- Do not add language-server, static type-checker, external SaaS, or LLM summarization dependencies.
- Do not claim exact ownership, test coverage, duplicate detection, or impact when only heuristics are available.
- Do not add parser dependencies for TypeScript/JavaScript, Go, Rust, Java, or other non-Python languages in this feature unless a separate dependency decision issue approves them.
- Do not create a second embedding model or duplicate table for similar-code search unless a later measured performance issue justifies it.
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

Raw path classification, Python parsing, test-link heuristics, ranking, and SQL queries may happen in memoized Python functions and/or explicit `asyncpg` code. Do not add a custom DSL or depend on any CocoIndex API not listed above.

## Feature gates and rollout

### Required gates

- `ProjectConfig.enable_symbols` gates symbol-aware map enrichment, symbol-level test links, symbol-level similar-code search, and symbol sections in review context.
- `ProjectConfig.enable_references` gates graph-backed review context, indirect test discovery through call edges, and graph evidence in test ranking.
- `ProjectConfig.enable_test_links` gates durable population of `{prefix}_test_links`. If false, `find_tests` may still return path/name/import heuristics, but each such result must be marked `metadata.source = "heuristic-v1"` and include low/medium confidence evidence.
- `ProjectConfig.chunk_strategy in {"ast", "hybrid"}` is required for Python AST symbol/test enrichment. Recursive chunking still supports path-only repo maps and chunk-level similarity.
- `ProjectConfig.ast_languages`, `symbol_languages`, and `reference_languages` continue to control parser language allow-lists. First implementation supports Python-specific enrichment only.

### Optional additive config

Prefer existing config first. Add these fields only if the implementation needs them:

```python
enable_repo_hierarchy: bool = True
similarity_candidate_limit: int = 5000
repo_map_max_nodes: int = 200
review_context_max_sections: int = 20
test_file_patterns: list[str] | None = None
source_test_path_patterns: list[dict[str, str]] | None = None
min_similar_code_score: float = 0.65
```

Rules:

- Defaults must preserve loading of existing global and project config files.
- Boolean environment overrides, if added, must follow existing `PI_CODE_INDEX_*` naming and validation patterns.
- Integer limits must be positive; `repo_map_max_nodes` and `review_context_max_sections` must be bounded in CLI/daemon handlers before backend calls.
- `min_similar_code_score` must be in `[0.0, 1.0]`.
- Add any behavior-affecting config fields to `BackendResourceCache._key()` before enabling daemon reuse of cached CocoIndex/Postgres resources.
- If `enable_repo_hierarchy` is added and false, `repo_map` must still return a lexical/path-only map with an explicit warning.

### Default behavior

Keep the current defaults: `enable_symbols: false`, `enable_references: false`, `enable_test_links: false`, and `chunk_strategy: recursive`. Users opt into richer context with project settings like:

```yaml
backend: cocoindex
enable_symbols: true
enable_references: true
enable_test_links: true
chunk_strategy: hybrid
ast_languages: [python]
```

## Public capabilities

### `repo_map`

Returns a compact architecture map for the whole repository, a subtree, or the area around a resolved symbol/file target.

Use when Pi needs architecture orientation before coding or reviewing. The tool must return path/module/package boundaries, key symbols when available, test counts/links when requested, and confidence/evidence metadata for any inferred role/summary.

### `find_tests`

Returns likely tests for one or more files, symbols, file-line targets, or changed-file targets.

Use before editing tests, running validation, or handing off a change. Results must include a recommended command and evidence labels explaining why each test was selected.

### `find_similar_code`

Returns similar chunks, symbols, or files to detect duplicate implementations, drift-prone parallel code, reusable patterns, and risky copy/paste.

Use before adding a new pattern or when reviewing changes. Results must expose semantic, lexical, and structural score components when available.

### `review_context`

Returns a bounded review packet for changed files/symbols: repo map, affected symbols/chunks, callers/callees or impact when available, likely tests, similar code, freshness/capability status, risks, and recommended validation commands.

Use before final review, PR handoff, or issue closure. It is a composition layer over existing data and the three new helper capabilities, not a separate durable index.

## Pi tool contract (`index.ts`)

Add these tools without changing existing registrations, names, or formatter behavior.

### Tool schemas

```ts
repo_map({
  target?: string,           // repo-relative path, symbol target, file:line[:column], or omitted for repo root
  depth?: number,            // default 2, min 0, max 5
  include_symbols?: boolean, // default true
  include_tests?: boolean,   // default false
  refresh?: boolean          // default false
})

find_tests({
  target: string | string[], // repo-relative file, symbol_id/name, file:line[:column], or changed-file list
  top_k?: number,            // default 20, min 1, max 100
  include_indirect?: boolean,// default false; graph-backed only when references are enabled
  refresh?: boolean          // default false
})

find_similar_code({
  target?: string,           // symbol target, repo-relative file, file:line[:column], or raw snippet identifier
  query?: string,            // natural-language intent or code text; required when target omitted
  top_k?: number,            // default 12, min 1, max 100
  mode?: "semantic" | "lexical" | "hybrid", // default "hybrid"
  scope?: "chunks" | "symbols" | "files",  // default "chunks"
  exclude_self?: boolean,    // default true
  refresh?: boolean          // default false
})

review_context({
  targets: string[],         // changed files, symbol targets, or file:line[:column] targets; required non-empty
  top_k?: number,            // default 30, min 1, max 200; shared budget for subqueries
  include_map?: boolean,     // default true
  include_tests?: boolean,   // default true
  include_similar?: boolean, // default true
  include_impact?: boolean,  // default true
  refresh?: boolean          // default false
})
```

Validation:

- Clamp numeric limits in TypeBox schemas and again in Python CLI/daemon code.
- `find_similar_code` must reject calls where both `target` and `query` are absent with `ok: false` and an actionable error.
- `review_context.targets` must be non-empty; empty arrays return `ok: false`.
- Unknown additive metadata from the CLI must be ignored by formatters.

### Shell commands issued by Pi tools

Each Pi tool must mirror existing tools by running `uv run --project <extension> pi-code-index ... --json` in Pi's active cwd:

```bash
uv run --project <extension> pi-code-index context repo-map --json [--target TARGET] [--depth N] [--include-symbols|--no-include-symbols] [--include-tests|--no-include-tests] [--refresh]
uv run --project <extension> pi-code-index context tests --json [--top-k N] [--include-indirect] [--refresh] TARGET [TARGET ...]
uv run --project <extension> pi-code-index context similar --json [--top-k N] [--mode semantic|lexical|hybrid] [--scope chunks|symbols|files] [--exclude-self|--no-exclude-self] [--query QUERY] [--refresh] [TARGET]
uv run --project <extension> pi-code-index context review --json [--top-k N] [--include-map|--no-include-map] [--include-tests|--no-include-tests] [--include-similar|--no-include-similar] [--include-impact|--no-include-impact] [--refresh] TARGET [TARGET ...]
```

### Compact formatter requirements

Add bounded formatters matching current style:

- `formatRepoMapResults(payload)`
  - Header: `repo_map: <target-or-repo-root>`.
  - Show warning if present.
  - Show total nodes and displayed nodes.
  - Display at most `MAX_DISPLAY_RESULTS` nodes with `path`, `node_kind`, `file_count`, `symbol_count`, `test_count`, confidence, and up to 3 key symbols.
  - End with: `Next: use read/symbol tools for listed files or repo_map with a narrower target before editing.`
- `formatFindTestsResults(payload)`
  - Header: `find_tests: <targets>`.
  - Display at most `MAX_DISPLAY_RESULTS` test candidates with `test_file`, optional `test_symbol`, target, score/confidence, evidence, and recommended command.
  - End with concrete next step: run listed tests or broaden target.
- `formatSimilarCodeResults(payload)`
  - Header: `find_similar_code: <target-or-query>`.
  - Display at most `MAX_DISPLAY_RESULTS` candidates with file range, score, semantic/lexical/structure components when present, risk label, and clipped code snippet.
  - End with: `Next: inspect similar ranges before adding or changing duplicate logic.`
- `formatReviewContextResults(payload)`
  - Header: `review_context: <N> target(s)`.
  - Show summary counts and risk level.
  - Display bounded sections in order: architecture, impact, tests, similar_code, freshness, risks, commands.
  - Include recommended commands as shell-ready strings.

All four formatters must:

- Use `MAX_TEXT_BYTES`, `MAX_DISPLAY_RESULTS`, and `MAX_CODE_CHARS` consistently with existing formatters.
- Return `FormatSummary` with displayed/omitted/truncation counts.
- Preserve full structured JSON under `details.cli_json` and formatter summary under `details.summary`.
- Return failure text `<tool> failed: <error>` when `payload.error` exists or `payload.ok === false`.

### Prompt guidance

Extend Pi prompt guidance additively:

- Use `repo_map` for architecture orientation before broad edits.
- Use `find_tests` before selecting validation commands for a file, symbol, or change.
- Use `find_similar_code` before adding a new command handler, config parser, fallback payload, or other repeated pattern.
- Use `review_context` before final review, PR handoff, or closing implementation issues.
- Continue to use `symbol_definition`, `symbol_context`, and `read` to inspect exact source before editing.

## CLI contract (`src/pi_code_index/cli.py`)

Add an additive `context` command group. Existing commands and exit behavior must not change.

```bash
pi-code-index context repo-map [--json] [--target TARGET] [--depth N] [--include-symbols|--no-include-symbols] [--include-tests|--no-include-tests] [--refresh] [--repo PATH]
pi-code-index context tests [--json] [--top-k N] [--include-indirect] [--refresh] [--repo PATH] TARGET [TARGET ...]
pi-code-index context similar [--json] [--top-k N] [--mode semantic|lexical|hybrid] [--scope chunks|symbols|files] [--exclude-self|--no-exclude-self] [--query QUERY] [--refresh] [--repo PATH] [TARGET]
pi-code-index context review [--json] [--top-k N] [--include-map|--no-include-map] [--include-tests|--no-include-tests] [--include-similar|--no-include-similar] [--include-impact|--no-include-impact] [--refresh] [--repo PATH] TARGET [TARGET ...]
```

Argument behavior:

- `--json` prints raw JSON with `ensure_ascii=False`, consistent with `print_result`.
- `--repo` uses existing `repo_root` resolution.
- `--no-daemon` calls backend routing functions directly.
- Without `--no-daemon`, requests use `request_or_start()` with the request types in the daemon section.
- `depth` clamps to `0..5` for `repo-map`.
- `top_k` clamps to `1..100` for `tests` and `similar`; `1..200` for `review`.
- `mode` accepts only `semantic`, `lexical`, or `hybrid`; default `hybrid`.
- `scope` accepts only `chunks`, `symbols`, or `files`; default `chunks`.
- `--include-symbols`, `--include-tests`, `--exclude-self`, and review include flags use `argparse.BooleanOptionalAction`.
- `context similar` must require either positional `TARGET` or `--query`; it may accept both and treat the query as additional intent text.

Human-readable output:

- Add `print_result` branches for the four new operations.
- Human output may be JSON-like for `review_context`, but `repo_map`, `find_tests`, and `find_similar_code` should be readable line-oriented summaries.
- CLI returns exit code `0` when `payload.ok` is absent or true; `1` when `payload.ok === false`; `2` for argparse errors.

## Daemon protocol (`src/pi_code_index/daemon.py`)

Add request types:

- `repo_map`
- `find_tests`
- `find_similar_code`
- `review_context`

### Requests

```json
{"type":"repo_map","repo":"/repo","target":"src/pi_code_index","depth":2,"include_symbols":true,"include_tests":false,"refresh":false}
{"type":"find_tests","repo":"/repo","targets":["src/pi_code_index/config.py"],"top_k":20,"include_indirect":false,"refresh":false}
{"type":"find_similar_code","repo":"/repo","target":"src/pi_code_index/cli.py:130","query":null,"top_k":12,"mode":"hybrid","scope":"chunks","exclude_self":true,"refresh":false}
{"type":"review_context","repo":"/repo","targets":["src/pi_code_index/cli.py","index.ts"],"top_k":30,"include_map":true,"include_tests":true,"include_similar":true,"include_impact":true,"refresh":false}
```

### Handler behavior

- Resolve `repo` through existing `repo_root(Path(...))`.
- Sanitize/clamp all numeric and enum inputs before backend calls.
- Pass `resource_cache.get(repo)` to CocoIndex-backed routing functions.
- Preserve daemon handshake, restart, status, live, and stop behavior.
- Unknown request types continue to return `{"ok": false, "error": "unknown request type: ..."}`.

### Shared response envelope

Every successful new operation must include:

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
  "capabilities": {
    "repo_hierarchy": true,
    "repo_map": true,
    "symbols": true,
    "references": true,
    "call_graph": true,
    "test_links": true,
    "find_tests": true,
    "similar_code": true,
    "review_context": true,
    "languages": ["python"]
  },
  "warning": null,
  "truncated": false,
  "truncation": {}
}
```

Fields may be null when unavailable, but keys should be present for compatibility. Existing clients must tolerate unknown additive fields.

### Status additions

When CocoIndex is active, `status` must add quality-context counts and capabilities without removing existing fields:

```json
{
  "counts": {
    "repo_hierarchy_nodes": 42,
    "test_links": 18,
    "test_files": 12,
    "similarity_candidates": 900,
    "freshness_current": 120,
    "freshness_stale": 0,
    "freshness_error": 0
  },
  "capabilities": {
    "repo_map": true,
    "find_tests": true,
    "find_similar_code": true,
    "review_context": true
  },
  "quality_context": {
    "ready": true,
    "warnings": []
  }
}
```

Lexical status may include `capabilities.repo_map = "path_only"`, `capabilities.find_tests = "path_heuristic"`, and `capabilities.find_similar_code = "lexical_only"`, but must not claim symbol/test-link/graph readiness.

## Backend routing contract (`src/pi_code_index/backend.py`)

Add routing functions:

```python
def repo_map(repo: Path, target: object | None = None, depth: int = 2, include_symbols: bool = True, include_tests: bool = False, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]: ...

def find_tests(repo: Path, targets: list[object], top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]: ...

def find_similar_code(repo: Path, target: object | None = None, query: str | None = None, top_k: int = 12, mode: str = "hybrid", scope: str = "chunks", exclude_self: bool = True, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]: ...

def review_context(repo: Path, targets: list[object], top_k: int = 30, include_map: bool = True, include_tests: bool = True, include_similar: bool = True, include_impact: bool = True, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]: ...
```

Routing rules:

- Resolve `repo` and use `choose_backend(repo)` exactly like existing operations.
- For requested `cocoindex`, return backend errors for real CocoIndex failures; do not silently downgrade.
- For `auto`, use existing `_with_auto_fallback()` conventions: fallback to lexical where possible, set `backend: "lexical"`, `backend_fallback: true`, and include the CocoIndex error in a warning.
- For `lexical`, return honest best-effort payloads only:
  - `repo_map`: path-only tree derived from indexed files or current file scan.
  - `find_tests`: path/name test hints with low/medium confidence.
  - `find_similar_code`: token-overlap chunk similarity with `fallback_reason: "lexical_chunk_similarity"`.
  - `review_context`: composition of lexical map/tests/similar with graph/symbol sections omitted and warnings present.
- Validate `find_similar_code` target/query and `review_context` target list before backend selection where possible.

## CocoIndex backend contract (`src/pi_code_index/coco_backend.py`)

### Schema reuse

Use existing canonical tables before adding new ones:

- `{prefix}_repos`
- `{prefix}_branches`
- `{prefix}_files`
- `{prefix}_chunks`
- `{prefix}_symbols`
- `{prefix}_symbol_embeddings`
- `{prefix}_references`
- `{prefix}_call_edges`
- `{prefix}_repo_hierarchy`
- `{prefix}_test_links`
- `{prefix}_freshness`

Do not create incompatible parallel tables for repo maps, test links, similar code, or review context. If new indexes are needed, create them idempotently with explicit `asyncpg` DDL.

### Required row semantics

#### `RepoHierarchyRow`

Existing dataclass fields are sufficient:

```python
RepoHierarchyRow(
  node_id: str,
  repo_id: str,
  branch_id: str,
  parent_id: str | None,
  path: str,
  node_kind: str,
  name: str,
  metadata: dict[str, object] | None,
)
```

Populate during canonical refresh when CocoIndex is active. Stable ID:

```text
node_id = sha256("repo_hierarchy\0" + repo_id + "\0" + branch_id + "\0" + node_kind + "\0" + normalized_path)[:32]
```

`path` is repo-relative with `/` separators. Root path is `""`. `parent_id` must reference another hierarchy node or null for root.

Allowed `node_kind` values:

- `root`
- `directory`
- `package` (directory containing `__init__.py`, `package.json`, `pyproject.toml`, or other recognized package marker)
- `module` (non-test source file)
- `test_directory`
- `test_file`
- `docs_directory`
- `config_file`

Metadata must be deterministic and bounded:

```json
{
  "language_counts": {"python": 7, "typescript": 1},
  "file_count": 7,
  "symbol_count": 54,
  "test_count": 8,
  "public_symbol_count": 20,
  "top_symbol_kinds": {"function": 30, "class": 5},
  "role": "cli|daemon|backend|tests|docs|config|package|unknown",
  "summary": "deterministic path/symbol summary, no LLM",
  "confidence": 0.0,
  "evidence": ["path", "package_marker", "symbols"]
}
```

#### `TestLinkRow`

Existing dataclass fields are sufficient:

```python
TestLinkRow(
  test_link_id: str,
  repo_id: str,
  branch_id: str,
  test_file_id: str,
  source_file_id: str,
  test_symbol_id: str | None = None,
  source_symbol_id: str | None = None,
  confidence: float = 0.5,
  metadata: dict[str, object] | None = None,
)
```

Populate only when `enable_test_links` is true. `test_file_id` must reference a file classified as a test file. `source_file_id` must reference a non-test source file when known. Stable ID:

```text
test_link_id = sha256("test_link\0" + repo_id + "\0" + branch_id + "\0" + test_file_id + "\0" + source_file_id + "\0" + (test_symbol_id or "") + "\0" + (source_symbol_id or ""))[:32]
```

Metadata must include:

```json
{
  "source": "test-link-heuristic-v1",
  "evidence": ["path_pattern", "name_overlap", "imports_target_module"],
  "score_components": {
    "path_proximity": 0.8,
    "import_or_reference_evidence": 1.0,
    "name_overlap": 0.6,
    "graph_reachability": 0.0,
    "freshness": 1.0
  },
  "recommended_command": "uv run pytest tests/test_config.py",
  "framework": "pytest|node_test|jest|unknown",
  "confidence_label": "low|medium|high"
}
```

### Indexing behavior

#### Repo hierarchy population

During canonical refresh:

1. Derive one root node for the repo/branch.
2. For every indexed file, derive directory ancestors and file node.
3. Classify directories and files using path, language, package markers, and test patterns.
4. Aggregate file counts, language counts, symbol counts, and test counts bottom-up.
5. Attach key symbol summaries only when `enable_symbols` is true and symbols exist.
6. Upsert rows idempotently; remove stale hierarchy nodes for files no longer present in the branch.

Classification rules:

- Test directories match any path component in `{test, tests, spec, specs, __tests__}`.
- Python test files match `test_*.py` or `*_test.py`.
- TypeScript/JavaScript test files match `*.test.ts`, `*.spec.ts`, `*.test.tsx`, `*.spec.tsx`, `*.test.js`, `*.spec.js`, `*.test.jsx`, or `*.spec.jsx`.
- Config files include `pyproject.toml`, `package.json`, `tsconfig.json`, `.yml/.yaml` under `.github/workflows`, and `.pi-code-index/settings.yml` if indexed.
- Docs directories include `docs`, `doc`, `documentation`, and Markdown-heavy directories.

#### Test-link population

When `enable_test_links` is true:

1. Identify test files using the rules above.
2. Identify source files as indexed non-test files.
3. Generate candidate links from path proximity, name overlap, imports/references, symbol overlap, and optional graph reachability.
4. Compute score and confidence using the ranking formula below.
5. Upsert candidates with score `>= 0.25`; omit weaker candidates but include aggregate omitted counts in status/debug metadata when cheap.
6. Bound per-source and per-test candidates to avoid large fan-out; default max 20 candidates per target source file before final `top_k` truncation.

Python-specific enrichment:

- When symbols are enabled, test symbols are Python functions/methods/classes whose names start with `test_` or classes whose names start/end with `Test`.
- Use symbol body chunks or references to detect target symbol names in test code.
- When references are enabled, graph reachability can raise confidence but must not create high-confidence results by itself if static resolution is weak.

#### Similar-code data

Do not add a new table for the initial implementation. Use:

- `{prefix}_chunks.embedding` for chunk similarity.
- `{prefix}_symbol_embeddings.embedding` for symbol similarity when enabled.
- Existing chunk/symbol metadata for structure similarity: language, `chunk_kind`, `symbol_kind`, decorators, function/class names, path role, and parser version.

If chunk embeddings are absent or stale, return lexical-only similarity with warning. If `scope = "symbols"` and symbols are disabled or no symbol embeddings exist, return an empty successful payload with warning unless lexical fallback can honestly map to chunks.

### Target resolution

All four operations must reuse or share existing symbol target resolution behavior where possible.

Accepted target forms:

- repo-relative path: `src/pi_code_index/config.py`
- directory/subtree path: `src/pi_code_index`
- file location: `src/pi_code_index/config.py:42` or `src/pi_code_index/config.py:42:5`
- `symbol_id`
- qualified name or bare name, resolved with the same ambiguity rules as `symbol_definition`
- JSON object for daemon-internal callers, with fields like `{ "path": "...", "line": 42, "symbol_id": "..." }`

Resolution output should be a bounded internal object:

```json
{
  "target": "src/pi_code_index/config.py:42",
  "target_kind": "file|directory|symbol|location|query|unresolved",
  "file_id": "...",
  "symbol_id": "...",
  "path": "src/pi_code_index/config.py",
  "line": 42,
  "matches": [],
  "warning": null
}
```

Ambiguous targets must return `matches` and a warning; operations may proceed with the top-ranked match only when that matches existing symbol-definition behavior.

## Payload contracts

### Shared candidate fields

Every heuristic candidate must include:

- `score`: numeric rank score in `[0.0, 1.0]` unless using raw similarity, then still normalized before output.
- `confidence`: numeric confidence in `[0.0, 1.0]` when applicable.
- `evidence`: array of short stable strings.
- `metadata.source`: stable algorithm identifier such as `repo-map-v1`, `test-link-heuristic-v1`, `similar-code-v1`, or `review-context-v1`.

Do not expose unbounded code, docstrings, or metadata. Use existing max result code byte limits.

### `repo_map` payload

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
  "target": "src/pi_code_index",
  "target_kind": "directory",
  "depth": 2,
  "include_symbols": true,
  "include_tests": false,
  "capabilities": {},
  "nodes": [
    {
      "node_id": "...",
      "path": "src/pi_code_index",
      "name": "pi_code_index",
      "node_kind": "package",
      "parent_id": "...",
      "summary": "Python package containing CLI, daemon, backend routing, and index storage modules",
      "role": "backend",
      "languages": ["python"],
      "file_count": 7,
      "symbol_count": 54,
      "test_count": 8,
      "key_symbols": [
        {"symbol_id":"...","qualified_name":"pi_code_index.cli.main","kind":"function","filename":"src/pi_code_index/cli.py","start_line":120,"end_line":260}
      ],
      "metadata": {"confidence":0.8,"evidence":["path","symbols","package_marker"],"source":"repo-map-v1"}
    }
  ],
  "edges": [
    {"from_node_id":"...","to_node_id":"...","edge_kind":"contains","confidence":1.0}
  ],
  "truncated": false,
  "truncation": {"node_budget": 200, "omitted_nodes": 0},
  "warning": null
}
```

Required edge kinds: `contains`; optional future edge kinds may include `imports`, `tests`, or `references`, but initial implementation must not claim those unless backed by indexed evidence.

### `find_tests` payload

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
  "targets": ["src/pi_code_index/config.py"],
  "top_k": 20,
  "include_indirect": false,
  "results": [
    {
      "test_file": "tests/test_config.py",
      "test_symbol": "test_load_project_config_validates_chunk_strategy",
      "test_symbol_id": "...",
      "target_file": "src/pi_code_index/config.py",
      "target_symbol_id": null,
      "score": 0.91,
      "confidence": 0.88,
      "evidence": ["path_pattern", "name_overlap", "imports_target_module"],
      "recommended_command": "uv run pytest tests/test_config.py",
      "metadata": {"source":"test-link-heuristic-v1","framework":"pytest","confidence_label":"high"}
    }
  ],
  "truncated": false,
  "truncation": {"candidate_budget": 1000, "omitted_candidates": 0, "omitted_results": 0},
  "warning": null
}
```

Recommended command rules:

- Python test file: `uv run pytest <test_file>`.
- Python test symbol/function: `uv run pytest <test_file>::<test_symbol>` when safe.
- TypeScript extension tests: `npm run test:ts -- <file>` only if the target is in `tests/*.test.ts` or package scripts support it; otherwise use `npm run test:ts`.
- TypeScript type-only validation target: include `npm run typecheck` as review-context command, not as a per-test candidate unless there is a test file.

### `find_similar_code` payload

```json
{
  "ok": true,
  "backend": "cocoindex",
  "operation": "find_similar_code",
  "repo": "/path/to/repo",
  "repo_id": "...",
  "branch": "main",
  "branch_id": "...",
  "schema_version": 1,
  "pipeline_version": "canonical-v1-ast-v1",
  "target": "src/pi_code_index/cli.py:130",
  "query": null,
  "mode": "hybrid",
  "scope": "chunks",
  "exclude_self": true,
  "top_k": 12,
  "results": [
    {
      "score": 0.86,
      "confidence": 0.78,
      "similarity": {"semantic":0.78,"lexical":0.64,"structure":0.55},
      "filename": "src/pi_code_index/cli.py",
      "start_line": 180,
      "end_line": 220,
      "code": "...",
      "symbol": "main",
      "symbol_id": "...",
      "chunk_id": "...",
      "risk": "parallel_command_handler",
      "evidence": ["same_file_role", "shared_tokens", "same_symbol_kind"],
      "metadata": {"excluded_self":true,"ranking_profile":"similar-code-v1","source":"similar-code-v1"}
    }
  ],
  "truncated": false,
  "truncation": {"candidate_limit": 5000, "omitted_candidates": 0, "omitted_results": 0},
  "warning": null
}
```

`code` must be bounded by existing result-code limits. If the candidate is a symbol and code is omitted for size, include `metadata.code_omitted_reason`.

### `review_context` payload

```json
{
  "ok": true,
  "backend": "cocoindex",
  "operation": "review_context",
  "repo": "/path/to/repo",
  "repo_id": "...",
  "branch": "main",
  "branch_id": "...",
  "schema_version": 1,
  "pipeline_version": "canonical-v1-ast-v1",
  "targets": ["src/pi_code_index/cli.py", "index.ts"],
  "top_k": 30,
  "summary": {
    "changed_files": 2,
    "resolved_targets": 2,
    "affected_symbols": 8,
    "likely_tests": 5,
    "similar_code_hits": 4,
    "freshness_current": 2,
    "freshness_stale": 0,
    "risk_level": "medium"
  },
  "sections": [
    {"section":"architecture","items":[{"path":"src/pi_code_index/cli.py","role":"public CLI boundary","evidence":["command parser","daemon request construction"]}]},
    {"section":"impact","items":[]},
    {"section":"tests","items":[]},
    {"section":"similar_code","items":[]},
    {"section":"freshness","items":[]},
    {"section":"risks","items":[{"risk":"CLI/Pi contract drift","severity":"medium","mitigation":"run TypeScript formatter tests and CLI JSON tests"}]},
    {"section":"commands","items":[{"command":"npm run typecheck","reason":"index.ts changed"}]}
  ],
  "recommended_commands": ["npm run typecheck", "npm run test:ts", "uv run pytest tests/test_cli.py"],
  "truncated": false,
  "truncation": {"section_budget":20,"item_budget":30,"omitted_sections":0,"omitted_items":0},
  "warning": null
}
```

Allowed section names for initial implementation: `architecture`, `symbols`, `impact`, `tests`, `similar_code`, `freshness`, `risks`, `commands`, `warnings`. Future sections are additive.

## Ranking and heuristics

### Repo-map ranking

When returning nodes, rank by:

```text
score = 0.40 proximity_to_anchor
      + 0.20 symbol_density
      + 0.15 public_boundary_score
      + 0.15 test_link_relevance
      + 0.10 freshness
```

Definitions:

- `proximity_to_anchor`: 1.0 for target node, decays by tree distance.
- `symbol_density`: normalized count of public symbols and key symbol kinds.
- `public_boundary_score`: high for CLI, daemon, extension entrypoint, public config/API modules, package roots.
- `test_link_relevance`: direct test links or co-located tests.
- `freshness`: 1.0 when all files under node are current, 0.5 when some stale, 0.0 when parse/index errors dominate.

### Test discovery ranking

Use this normalized formula:

```text
score = 0.35 path_proximity
      + 0.25 import_or_reference_evidence
      + 0.20 name_overlap
      + 0.10 graph_reachability
      + 0.10 freshness
```

Evidence labels:

- `path_pattern`
- `same_directory`
- `nearest_test_directory`
- `name_overlap`
- `imports_target_module`
- `references_target_symbol`
- `calls_target_symbol`
- `graph_reachability`
- `symbol_name_overlap`
- `framework_pattern`
- `freshness_current`

Confidence mapping:

- `high`: score `>= 0.75` and at least two distinct evidence families.
- `medium`: score `>= 0.45` or one strong evidence family.
- `low`: score `< 0.45`; still useful, but warnings/metadata must state heuristic uncertainty.

### Similar-code ranking

For `mode = "semantic"`:

```text
score = semantic_similarity
```

For `mode = "lexical"`:

```text
score = 0.80 token_overlap + 0.20 path_or_name_similarity
```

For `mode = "hybrid"`:

```text
score = 0.55 semantic_similarity
      + 0.30 lexical_similarity
      + 0.15 structure_similarity
```

Structure similarity inputs:

- same language
- same chunk kind or symbol kind
- same path role (`cli`, `daemon`, `backend`, `config`, `test`, etc.)
- same decorator names or function/class naming pattern
- both test or both source

Risk labels are rule-based and optional. Initial labels:

- `parallel_command_handler`: CLI/daemon command/request dispatch shapes match.
- `duplicate_fallback_payload`: fallback payload/warning construction shape matches.
- `similar_config_validation`: validation or environment override logic matches.
- `shared_test_pattern`: test helper/assertion patterns match.
- `near_duplicate_chunk`: high lexical similarity with same language/kind.
- `semantic_overlap`: high semantic similarity without stronger rule label.

Self-exclusion:

- If `exclude_self` is true, remove exact same `chunk_id`, `symbol_id`, or same file span.
- For file targets, do not remove all same-file results; only remove the exact target span when known.

### Review risk scoring

`review_context.summary.risk_level` must be one of `low`, `medium`, `high`.

Start at `low`; raise to `medium` when any is true:

- changed target includes CLI, daemon, Pi tool registration, backend routing, config validation, schema/migration, or parser logic;
- similar-code hits include medium/high risk labels;
- likely tests exist but no high-confidence direct test is found;
- freshness has stale/error rows.

Raise to `high` when any is true:

- schema/identity/daemon protocol changes without corresponding tests;
- graph/reference/test-link extraction changes with stale/error freshness;
- public Pi/CLI contract changes with no formatter/CLI JSON tests;
- high-confidence duplicate risk across different modules.

Every risk item must include `risk`, `severity`, `evidence`, and `mitigation`.

## Fallback behavior

### Lexical backend

Lexical payloads must set `backend: "lexical"` and capabilities honestly:

```json
{
  "capabilities": {
    "repo_hierarchy": false,
    "repo_map": "path_only",
    "symbols": false,
    "references": false,
    "call_graph": false,
    "test_links": false,
    "find_tests": "path_heuristic",
    "similar_code": "lexical_only",
    "review_context": "lexical_composition"
  },
  "warning": "CocoIndex/Postgres is required for symbol, graph, durable test-link, and semantic similar-code context; returned lexical heuristics only"
}
```

Specific behavior:

- `repo_map`: derive a shallow tree from indexed file paths. `metadata.confidence <= 0.5` and evidence limited to `path`/`file_scan`.
- `find_tests`: use path/name conventions only. `confidence <= 0.5` unless exact same-directory test path exists.
- `find_similar_code`: use token overlap over lexical chunks. Set `fallback_reason: "lexical_chunk_similarity"`; `similarity.semantic` must be null or omitted.
- `review_context`: compose lexical map/tests/similar and include warnings for omitted symbol/graph/semantic sections.

### Auto backend

When `backend: auto` chooses CocoIndex but CocoIndex/Postgres fails, follow existing `_with_auto_fallback()` behavior:

- return a successful lexical payload when lexical fallback can answer honestly;
- set `backend_fallback: true`;
- include the original CocoIndex failure in `warning`;
- never return fabricated symbol/graph/test-link facts.

### Empty and degraded states

- If repo hierarchy has no rows, `repo_map` should return path-only map from files when possible and warn.
- If `enable_test_links` is false, `find_tests` should still return heuristics with `capabilities.test_links: false` and warning.
- If symbol embeddings are missing for symbol scope, `find_similar_code` should fall back to chunk scope only if this is clear in warning and metadata; otherwise return empty results.
- If targets cannot be resolved, return `ok: true` with empty results and `target_kind: "unresolved"` only for exploratory operations (`repo_map`, `find_tests`, `find_similar_code` with query). For `review_context`, unresolved targets should appear in a `warnings` section and affect risk.

## Tests required for implementation

### TypeScript tests (`tests/*.test.ts`)

Add formatter and tool-command tests that assert:

- `repo_map`, `find_tests`, `find_similar_code`, and `review_context` schemas accept defaults and reject invalid enum/numeric values.
- Tool invocations build the expected `pi-code-index context ... --json` commands.
- Formatters bound display output, count omitted results, clip snippets, and preserve full raw JSON in details.
- Failure payloads render `<tool> failed: <error>`.
- Unknown additive metadata does not break formatting.

### CLI unit tests

Add Python tests for `cli.main()`/argparse behavior:

- `pi-code-index context repo-map --json --target src --depth 9` clamps depth to 5 in daemon request/direct call.
- `context tests --top-k 999 target.py` clamps to 100.
- `context similar` without target or query exits with argparse error or returns `ok: false` according to final implementation choice; the behavior must be documented and tested.
- `context review` with no targets errors.
- `--no-daemon` calls backend routing functions; daemon mode sends request types and payload fields specified above.

### Daemon tests

Add tests around `daemon.handle()`:

- New request types dispatch to backend functions with sanitized inputs.
- Unknown request behavior remains unchanged.
- Status includes additive quality-context counts/capabilities when backend status returns them.
- Resource cache key includes any newly added behavior-affecting config fields.

### Backend routing tests

Add tests for `backend.py`:

- Lexical backend returns honest fallback capabilities and warnings for all four operations.
- Auto fallback sets `backend_fallback: true` and includes CocoIndex error when CocoIndex raises.
- Requested `cocoindex` returns backend errors rather than silently falling back.
- Invalid inputs return `ok: false` with actionable errors.

### CocoIndex/Postgres integration tests

With Postgres/pgvector available, add tests that:

- Refresh populates `repo_hierarchy` rows for root, directories, packages, modules, test directories, and test files.
- `repo_map` returns expected nodes, contains edges, counts, key symbols, and truncation metadata.
- With `enable_test_links: true`, refresh populates deterministic `test_links`; `find_tests` ranks direct tests above weak path-only candidates.
- With `enable_test_links: false`, `find_tests` returns heuristic candidates and warning without writing durable links.
- `find_similar_code` returns chunk candidates from existing embeddings, excludes exact self matches, supports `semantic`, `lexical`, and `hybrid`, and degrades when embeddings are unavailable.
- `review_context` composes map/tests/similar/impact sections according to include flags and recommends validation commands.
- Freshness stale/error rows are reflected in warnings/risk.

### Lexical tests

Add or extend lexical indexer tests:

- Path-only repo map can be derived from lexical indexed files.
- Test path heuristics find common Python and TypeScript test names.
- Token-similar chunks are returned with low confidence and `fallback_reason`.
- Lexical review context omits graph/symbol facts and includes warnings.

## Validation commands

Specs-only validation for this issue:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
git diff -- docs/architecture/repo-quality-context-spec.md
```

Implementation validation must include:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
scripts/setup.sh
npm run typecheck
npm run test:ts
uv run python -m compileall src tests
uv run pytest
uv run pi-code-index --help
uv run pi-code-index context --help
uv run pi-code-index --no-daemon search --json --refresh "where is config loaded"
uv run pi-code-index --no-daemon context repo-map --json --target src/pi_code_index --depth 2
uv run pi-code-index --no-daemon context tests --json src/pi_code_index/config.py
uv run pi-code-index --no-daemon context similar --json --query "CLI subcommand dispatch" --top-k 5
uv run pi-code-index --no-daemon context review --json src/pi_code_index/cli.py index.ts
```

CocoIndex/Postgres validation must use Podman for local container development:

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

Cleanup after local validation:

```bash
podman rm -f pi-code-index-postgres
```

## Implementation acceptance criteria

A future implementation subagent may close its product-code issue only when all are true:

1. The four Pi tools exist with additive schemas, compact bounded formatters, and raw CLI JSON details.
2. The `pi-code-index context` CLI group exists with the commands, flags, validation, daemon/direct behavior, and exit codes specified here.
3. The daemon handles the four new request types and status adds quality-context readiness without changing existing request behavior.
4. Backend routing supports CocoIndex, auto fallback, and lexical fallback honestly.
5. CocoIndex refresh populates/query-uses repo hierarchy and, when enabled, deterministic test links using only CocoIndex V1 concepts and explicit `asyncpg` where needed.
6. Similar-code search reuses existing chunk/symbol embeddings and lexical scoring; no new embedding model or duplicate table is required for initial release.
7. Review context composes map/tests/similar/impact/freshness/risk/commands sections with include flags and bounded budgets.
8. Every heuristic result includes score, confidence, evidence, and source metadata.
9. Existing code search, symbol, graph, live, status, refresh, stop, and daemon handshake tests continue to pass unchanged.
10. The validation commands above pass in lexical mode and CocoIndex/Postgres mode where optional dependencies are available.

## Specs acceptance criteria for issue `pi-code-index-3jo.5.2`

This specs/docs issue is complete when:

- `docs/architecture/repo-quality-context-spec.md` exists.
- The spec covers exact behavior, data/query contracts, CLI commands, daemon protocol, Pi tools, ranking/heuristics, fallback behavior, tests, validation commands, and implementation acceptance criteria.
- The spec is detailed enough that an implementation subagent can execute without guessing.
- No product implementation files are modified for this issue.
