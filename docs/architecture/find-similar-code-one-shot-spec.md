# find_similar_code one-shot integration spec

## Scope

This document is the implementation contract for issue `pi-code-index-199.2`. It converts `docs/architecture/find-similar-code-one-shot-plan.md` into one final, buildable specification for improving `find_similar_code` ranking precision.

This is a specs/docs-only change. It does not implement product code. The later implementation must be one-shot, not a phased rollout: all contracts, ranking behavior, fallback behavior, tests, fixtures, and validation thresholds below must land together.

## Current code inspected

The spec is based on the current repository state of:

- `index.ts`
  - Registers Pi tool `find_similar_code` with `target`, `query`, `top_k`, `mode`, `scope`, `exclude_self`, and `refresh`.
  - Calls `uv run --project <extension> pi-code-index context similar --json ...`.
  - Compact formatter displays `filename:start-end`, `score`, `sim=<json>`, `risk`, and clipped code, while preserving full CLI JSON in `details.cli_json`.
- `src/pi_code_index/cli.py`
  - Exposes `pi-code-index context similar [TARGET] --json --top-k --mode semantic|lexical|hybrid --scope chunks|symbols|files --exclude-self|--no-exclude-self --query --refresh --repo`.
  - Non-JSON output prints compact `filename:start-end score risk` lines.
- `src/pi_code_index/daemon.py`
  - Handles request type `find_similar_code` and forwards `repo`, `target`, `query`, bounded `top_k`, `mode`, `scope`, `exclude_self`, `refresh`, and cached CocoIndex resources.
- `src/pi_code_index/backend.py`
  - Validates missing `target`/`query`, `mode`, and `scope`.
  - Clamps `top_k` to `1..100`.
  - Routes lexical requests to `context_tools.find_similar_code` and CocoIndex requests to `coco_backend.find_similar_code`, with `_with_auto_fallback()` for `backend=auto`.
- `src/pi_code_index/context_tools.py`
  - Owns lexical fallback.
  - Current fallback builds query text from `query` plus up to 4000 bytes of target-file chunks, tokenizes it, scores each indexed chunk, adds a weak path-role structure score in hybrid mode, and returns `similarity.lexical`, `similarity.structure`, and `metadata.fallback_reason = "lexical_chunk_similarity"`.
  - Current `scope="symbols"` and `scope="files"` are accepted but still return chunk-like results.
- `src/pi_code_index/coco_backend.py`
  - Defines canonical repo, branch, file, chunk, symbol, symbol embedding, reference, call edge, hierarchy, test-link, and freshness rows.
  - Creates canonical tables including `{prefix}_chunks`, `{prefix}_symbols`, and `{prefix}_symbol_embeddings`, plus a pgvector index on chunk embeddings.
  - Current `find_similar_code()` wraps `context_tools.find_similar_code()` and therefore does not yet use embeddings, AST chunk metadata, symbols, Postgres ranking, or canonical freshness.
- `tests/test_context_tools.py`
  - Covers lexical context payloads and asserts `metadata.fallback_reason == "lexical_chunk_similarity"`.
- `tests/test_cocoindex_postgres_integration.py`
  - Covers CocoIndex refresh/search/symbol/graph integration, but not improved `find_similar_code` ranking yet.
- `tests/test_daemon_lifecycle.py`
  - Covers daemon resource reuse and request routing for other context tools; `find_similar_code` forwarding coverage must be added.
- `tests/format-results.test.ts`
  - Covers compact TypeScript formatter output for `find_similar_code` and must continue passing unless additive compact text is intentionally added.

## Non-goals

- Do not remove, rename, or repurpose existing Pi tool parameters, CLI commands, CLI flags, daemon request fields, or required JSON result fields.
- Do not require CocoIndex/Postgres for `backend=auto` users without Postgres configuration.
- Do not hide docs/tests/config files by default. The implementation uses priors and penalties, not hard filters.
- Do not introduce a phased rollout flag or multiple partially enabled ranking profiles.
- Do not add a second durable similar-code table or a new embedding model unless the existing canonical chunk/symbol embeddings are unavailable.
- Do not change the `code_search` behavior.
- Do not use Docker in documentation or validation commands; use Podman.
- Do not use CocoIndex APIs or concepts outside the V1 boundary listed below.

## CocoIndex V1 boundary

Use only CocoIndex V1 concepts already used or accepted by this extension:

- `coco.App`
- `coco.AppConfig`
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

The ranking query itself may be explicit `asyncpg` SQL. Query embedding may use the daemon-cached `SentenceTransformerEmbedder` resource already exposed by `CocoBackendResources`. Do not add unreleased CocoIndex APIs, a custom DSL, or non-V1 lifecycle concepts.

## Stable public behavior

### Pi tool contract

`index.ts` must keep registering `find_similar_code` with the same existing parameters:

```ts
{
  target?: string;
  query?: string;
  top_k?: number;              // default 12, clamp 1..100
  mode?: "semantic" | "lexical" | "hybrid"; // default "hybrid"
  scope?: "chunks" | "symbols" | "files";  // default "chunks"
  exclude_self?: boolean;      // default true
  refresh?: boolean;           // default false
}
```

Pi behavior must remain:

- If both `target` and `query` are absent, return an error payload from Python; the Pi formatter renders `find_similar_code failed: ...`.
- Execute the same CLI path: `uv run --project <extension> pi-code-index context similar --json ...`.
- Preserve `details.cli_json` as the unmodified backend JSON.
- Preserve compact display shape: header, optional warning, up to 8 results, `score`, `sim=<json>`, `risk`, clipped code, omitted count, and next-step guidance.
- Additive display text is allowed only if bounded and covered by `tests/format-results.test.ts`. The implementation does not need formatter changes because new score components and evidence are already visible in `sim=<json>` and full details.

### CLI contract

The existing command remains the only public CLI entry point for this feature:

```bash
pi-code-index context similar [TARGET] \
  --json \
  --top-k 12 \
  --mode hybrid \
  --scope chunks \
  --exclude-self \
  --query "..." \
  --refresh \
  --repo /path/to/repo
```

Rules:

- Keep `TARGET` optional when `--query` is present.
- Keep `--mode` choices exactly `semantic`, `lexical`, `hybrid`.
- Keep `--scope` choices exactly `chunks`, `symbols`, `files`.
- Keep `--exclude-self`/`--no-exclude-self` with default `true`.
- Do not add required flags.
- Avoid new optional flags for the one-shot implementation unless a test proves they are necessary. Existing `mode` and `scope` are sufficient.
- Non-JSON output may remain as current compact lines. It does not need to expose all components.

### Daemon contract

Request type `find_similar_code` remains:

```json
{
  "type": "find_similar_code",
  "repo": "/absolute/or/relative/repo",
  "target": "src/pkg/retry.py:10",
  "query": "retry backoff helper",
  "top_k": 12,
  "mode": "hybrid",
  "scope": "chunks",
  "exclude_self": true,
  "refresh": false
}
```

Daemon behavior:

- Resolve `repo` through `repo_root()` as today.
- Clamp `top_k` to `1..100` before calling backend.
- Default `mode="hybrid"`, `scope="chunks"`, `exclude_self=true`, `refresh=false`.
- Forward cached `CocoBackendResources` for CocoIndex requests.
- If additive request fields are later added, validate and bound them in Python before forwarding, and include them in `BackendResourceCache._key()` only when they affect cached resources.

### Backend routing contract

`backend.find_similar_code()` remains the compatibility boundary:

- Validate `target` or `query` is present.
- Validate `mode in {semantic, lexical, hybrid}`.
- Validate `scope in {chunks, symbols, files}`.
- Clamp `top_k` to `1..100`.
- `backend=lexical` always uses lexical fallback.
- `backend=auto` uses CocoIndex when configured and falls back through `_with_auto_fallback()` on CocoIndex query/setup failures. The fallback payload must include `backend_fallback=true`, `requested_backend="auto"`, and a warning preserving the original CocoIndex error.
- `backend=cocoindex` must not silently degrade semantic/hybrid data-access failures to lexical. Return the existing JSON-safe `ok:false` error behavior with actionable setup guidance.

## Response envelope contract

All modes and backends return the shared context envelope. Existing fields stay valid; new fields are additive.

```json
{
  "ok": true,
  "backend": "cocoindex",
  "requested_backend": "auto",
  "backend_fallback": false,
  "operation": "find_similar_code",
  "repo": "/repo",
  "repo_id": "<32 hex or null>",
  "branch": "main",
  "branch_id": "<32 hex or null>",
  "schema_version": 1,
  "pipeline_version": "canonical-v1-ast-v1",
  "capabilities": { "similar_code": true },
  "target": "src/pkg/retry.py:10",
  "query": "retry backoff helper",
  "mode": "hybrid",
  "scope": "chunks",
  "exclude_self": true,
  "top_k": 12,
  "ranking_profile": "similar-code-v2",
  "results": [],
  "warning": null,
  "warnings": [],
  "truncated": false,
  "truncation": {
    "candidate_limit": 500,
    "lexical_candidate_limit": 240,
    "vector_chunk_candidate_limit": 120,
    "vector_symbol_candidate_limit": 120,
    "omitted_candidates": 0,
    "omitted_results": 0
  }
}
```

Lexical fallback envelope differences:

- `backend` is `lexical` unless auto fallback overwrites it as currently implemented.
- `repo_id`, `branch`, and `branch_id` may be `null`.
- `pipeline_version` may be `null`.
- `capabilities.similar_code` should remain honest, for example `"lexical_only"`.
- `ranking_profile` should be `similar-code-v2-lexical-fallback`.

## Result contract

Every result must keep the required top-level fields used by existing callers:

```json
{
  "score": 0.873421,
  "confidence": 0.78,
  "filename": "src/pkg/retry.py",
  "start_line": 10,
  "end_line": 42,
  "code": "...",
  "symbol": "retry_with_backoff",
  "symbol_id": "<id or null>",
  "chunk_id": "<id or null>",
  "risk": "near_duplicate_chunk",
  "evidence": [
    "semantic:chunk_vector>=0.82",
    "lexical:shared_tokens=retry,backoff",
    "ast:function_chunk",
    "symbol:same_kind=function",
    "role:source_query_source_candidate"
  ],
  "similarity": {
    "semantic": 0.82,
    "lexical": 0.71,
    "structure": 0.65,
    "symbol": 0.60,
    "ast": 0.75,
    "role_prior": 1.00,
    "freshness": 1.00,
    "penalty": 0.00
  },
  "score_components": {
    "semantic": 0.328,
    "lexical": 0.213,
    "symbol": 0.090,
    "ast": 0.060,
    "structure": 0.046,
    "freshness": 0.050,
    "role_prior_multiplier": 1.000,
    "penalty": -0.000,
    "final": 0.787
  },
  "metadata": {
    "ranking_profile": "similar-code-v2",
    "candidate_kind": "chunk",
    "content_role": "source",
    "chunk_kind": "function",
    "source": "cocoindex-hybrid-similar-code-v2",
    "candidate_key": "chunk:<chunk_id>",
    "freshness_status": "current",
    "semantic_available": true,
    "lexical_available": true,
    "symbol_available": true,
    "ast_available": true
  }
}
```

Required result-field rules:

- `score`: final score in `[0, 1]`, rounded to at most 6 decimals, sorted descending.
- `confidence`: in `[0, 1]`; may be lower than `score` when ranking evidence is weak or fallback-only.
- `filename`: repo-relative POSIX path.
- `start_line`/`end_line`: 1-based inclusive representative range.
- `code`: bounded to the current backend maximum result code bytes pattern, currently 12000 characters/bytes unless an existing config provides a lower bound.
- `symbol`: symbol name or qualified name when available; otherwise `null`.
- `symbol_id`: required for symbol candidates when available; otherwise `null`.
- `chunk_id`: required for chunk candidates when available; otherwise `null`.
- `risk`: one of the labels defined in this spec.
- `evidence`: non-empty for every non-zero score result.
- `similarity.lexical` and `similarity.structure`: always present for compatibility, even if `0.0`.
- `score_components`: present for every result from both CocoIndex and lexical fallback.
- `metadata.ranking_profile`: present for every result.

Lexical fallback result metadata must keep compatibility:

```json
{
  "metadata": {
    "ranking_profile": "similar-code-v2-lexical-fallback",
    "source": "similar-code-v2-lexical-fallback",
    "fallback_reason": "lexical_chunk_similarity"
  }
}
```

## Scope semantics

### `scope="chunks"`

Return chunk candidates.

- `chunk_id` must be set for CocoIndex results and existing lexical chunk IDs.
- `symbol_id` is set when the chunk is associated with a symbol.
- `metadata.candidate_kind = "chunk"`.
- `metadata.chunk_kind` must reflect canonical `chunks.chunk_kind` when available; lexical fallback may use `"text"`, `"file_chunk"`, or inferred `"function"` only when deterministic from local metadata.
- Prefer AST/function/method/class chunks over broad recursive text chunks when scores are otherwise close.

### `scope="symbols"`

Return one result per symbol candidate when symbol data is available.

Required behavior:

- `metadata.candidate_kind = "symbol"`.
- `symbol_id`, `symbol`, `filename`, `start_line`, `end_line`, and `code` must be populated when canonical symbol and file rows exist.
- Score uses symbol embedding similarity, qualified-name token overlap, symbol kind agreement, and best related chunk evidence.
- Deduplicate by `symbol_id`.
- Related chunk evidence may be included in `metadata.best_chunk_id`, `metadata.best_chunk_score`, and `metadata.related_chunks` capped to 3.

Fallback behavior:

- If CocoIndex is active but symbols are disabled or tables are missing in `backend=auto`, return chunk fallback with warning: `symbols unavailable; returned chunk candidates`.
- If `backend=cocoindex` is explicit and required symbol tables are missing, return `ok:false` with setup guidance unless the implementation can prove symbols are disabled by project config; when disabled by config, return chunk fallback plus warning.
- Lexical fallback returns chunk results with warning: `symbols unavailable in lexical fallback; returned chunks`.

### `scope="files"`

Return one result per file.

Aggregation contract:

- Group merged chunk/symbol candidates by `filename`.
- Representative range is the highest-scoring candidate's range.
- `score` is:

```text
file_score = clamp(
  max(candidate.score) + min(0.08, 0.02 * (supporting_evidence_count - 1)),
  0,
  1
)
```

- `confidence` is the max candidate confidence capped by freshness and role confidence.
- `metadata.candidate_kind = "file"`.
- `metadata.aggregated_candidates` includes counts:

```json
{
  "chunks": 3,
  "symbols": 1,
  "supporting_evidence_count": 4,
  "best_candidate_key": "chunk:<id>",
  "top_candidate_keys": ["chunk:<id>", "symbol:<id>"]
}
```

- `evidence` must include `file_aggregate:max_candidate` and at least one candidate evidence string.
- Role priors apply at file level using the file path role.

## Content roles

Add a reusable classifier in `context_tools.py` so lexical and CocoIndex result normalization use the same role labels.

### Function contract

```python
def content_role_for(path: str) -> Literal["source", "test", "docs", "config", "generated", "unknown"]:
    ...
```

### Classification rules

Evaluate in this order:

1. `generated`
   - Path contains `node_modules/`, `vendor/`, `.venv/`, `dist/`, `build/`, `.next/`, `coverage/`, `.pytest_cache/`, `__pycache__/`.
   - File is a lockfile: `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `uv.lock`, `poetry.lock`, `Pipfile.lock`, `Cargo.lock`, `go.sum`.
   - File ends with `.min.js`, `.min.css`, `.map`, generated protobuf artifacts, or has generated markers in metadata.
2. `test`
   - Existing `is_test_path()` rules.
   - Directories `test`, `tests`, `spec`, `specs`, `__tests__`.
   - Names matching `test_*`, `*_test.py`, `*.test.ts`, `*.spec.ts`, `*.test.tsx`, `*.spec.tsx`, `*.test.js`, `*.spec.js`, `*.test.jsx`, `*.spec.jsx`.
3. `docs`
   - `docs/**`, `doc/**`, `documentation/**`.
   - `README*`, `CHANGELOG*`, `CONTRIBUTING*`, `*.md`, `*.rst`, `*.adoc`.
4. `config`
   - `*.toml`, `*.yaml`, `*.yml`, `*.json`, `*.ini`, `*.cfg`, `*.conf`.
   - Dotfiles and CI files when not source scripts.
5. `source`
   - `.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.go`, `.rs`, `.java`, `.kt`, `.rb`, `.php`, `.c`, `.cc`, `.cpp`, `.h`, `.hpp`, `.cs`, `.swift`, `.scala`, `.sh` outside docs/tests/generated paths.
6. `unknown`
   - Everything else.

Keep existing `role_for()` for path-role strings such as `cli`, `daemon`, and `backend`, but use `content_role_for()` for source/docs/tests/config/generated ranking priors.

## Ranking inputs

### Query context resolution

For every request, build a normalized `QueryContext` with:

```json
{
  "query_text": "...",
  "target_file": "src/pkg/retry.py",
  "target_line": 10,
  "target_symbol_id": "...",
  "target_symbol_kind": "function",
  "target_symbol_name": "retry_with_backoff",
  "target_chunk_id": "...",
  "target_chunk_kind": "function",
  "target_content_role": "source",
  "query_content_role": "source",
  "query_tokens": ["retry", "backoff", "helper"]
}
```

Rules:

- If `query` is present, `query_text` starts with raw `query`.
- If `target` resolves to a file/chunk/symbol, append target code context capped to 4000 bytes for lexical fallback and use symbol/chunk text for embedding in CocoIndex.
- If both `query` and `target` are present, do not exclude sibling chunks from the target file; snippet queries may intentionally search within the same file.
- If only `target` is present and `exclude_self=true`, exclude the exact target candidate:
  - target symbol: exclude same `symbol_id` and same exact covering chunk when known.
  - target chunk: exclude same `chunk_id`.
  - target file: exclude chunks/symbols from same file.
- Infer `query_content_role` from target role when target exists; otherwise from query tokens:
  - docs-like tokens such as `README`, `documentation`, `docs`, `guide`, `tutorial` bias to `docs` only when clear.
  - test-like tokens such as `pytest`, `test`, `assert`, `spec` bias to `test` only when clear.
  - default query role is `source`.

### Candidate retrieval

Use bounded two-stage retrieval. Candidate pools must be merged and scored once.

Default limits:

```text
merged_candidate_limit = 500
lexical_candidate_limit = max(200, top_k * 20)
vector_chunk_candidate_limit = max(100, top_k * 10)
vector_symbol_candidate_limit = max(100, top_k * 10)
ast_neighbor_limit = max(50, top_k * 5)
```

Cap merged candidates at 500 before final scoring. Include all limits in `truncation`.

Candidate sources:

1. Lexical candidates
   - Lexical fallback: use `.pi-code-index/index.json`, `indexer.py` chunking, `tokenize()`, and `score_tokens()`.
   - CocoIndex: may reuse local lexical index or compute SQL/Python token overlap from canonical chunk code. The resulting normalized component must match `score_tokens()` semantics closely enough for tests.
2. Vector chunk candidates
   - Query `{prefix}_chunks` with `embedding <=> query_embedding`.
   - Join `{prefix}_files` for path/language/metadata and optionally `{prefix}_symbols` for symbol metadata.
   - Limit to current repo/branch.
3. Vector symbol candidates
   - Query `{prefix}_symbol_embeddings` joined to `{prefix}_symbols` and `{prefix}_files` with `embedding <=> query_embedding`.
   - Limit to current repo/branch and enabled symbols.
4. AST/symbol neighbors
   - Add chunks with same `chunk_kind`, same `symbol.kind`, matching symbol/name stems, matching class/module stems, or same path-role family.
   - These candidates must still require lexical or vector evidence above zero before receiving a large symbol/path boost.

Stable candidate keys:

- Chunk: `chunk:<chunk_id>`.
- Symbol: `symbol:<symbol_id>`.
- File aggregate: `file:<filename>`.
- Lexical-only chunk: `chunk:<indexer_chunk_id>`.

## Component normalization

All component values must be normalized to `[0, 1]` before weighting.

### `semantic`

- CocoIndex vector similarity: `semantic = clamp(1 - cosine_distance, 0, 1)`.
- If pgvector returns inner-product/cosine distance in a different scale, normalize to the same meaning before scoring.
- Lexical fallback or missing embeddings: component is unavailable, represented as `0.0` in `similarity.semantic` and `metadata.semantic_available=false`.

### `lexical`

- Use existing `score_tokens(query_tokens, candidate_tokens)` or SQL/Python equivalent.
- Must reward exact token overlap, helper/function names, and rare identifiers.
- Must be deterministic and independent of Postgres availability.

### `symbol`

Use the max of:

- qualified-name token overlap,
- exact symbol name/stem overlap,
- same symbol kind (`function`, `method`, `class`, `module`),
- same receiver/class/module stem.

Symbol boost may not exceed `0.35` when both lexical and semantic are zero. This prevents path/name coincidences from ranking unrelated candidates.

### `ast`

Score by chunk kind and query role:

| candidate chunk kind | source query AST score |
| --- | ---: |
| `function`, `method` | 1.00 |
| `class` | 0.90 |
| `module` with symbol coverage | 0.65 |
| recursive/text chunk containing one complete symbol | 0.55 |
| broad recursive/text chunk | 0.30 |
| docs/config/test prose chunk | 0.20 |

For docs/test query roles, docs/test prose chunks may receive up to `0.65` so documentation targets are not over-penalized.

### `structure`

Reuse and refine path-role proximity:

- same package/module directory: `0.80..1.00`
- same current `role_for()` path role (`cli`, `daemon`, `backend`, `config`, `docs`): `0.65`
- basename/stem overlap: `0.20..0.50`
- unrelated paths: `0.10..0.30`

### `role_prior`

`role_prior` is a multiplier, not an additive score.

For source query/target:

| candidate role | multiplier |
| --- | ---: |
| source | 1.00 |
| config | 0.85 |
| test | 0.70 |
| docs | 0.55 |
| generated | 0.30 |
| unknown | 0.75 |

For docs query/target:

| candidate role | multiplier |
| --- | ---: |
| docs | 1.00 |
| source | 0.85 |
| config | 0.75 |
| test | 0.65 |
| generated | 0.30 |
| unknown | 0.70 |

For test query/target:

| candidate role | multiplier |
| --- | ---: |
| test | 1.00 |
| source | 0.90 |
| config | 0.75 |
| docs | 0.60 |
| generated | 0.30 |
| unknown | 0.70 |

### `freshness`

Use canonical freshness when available:

| status | score | evidence |
| --- | ---: | --- |
| `current` | 1.00 | `freshness:current` |
| `stale` | 0.75 | `freshness:stale` |
| `error` with parser fallback chunks | 0.50 | `freshness:parser_error_fallback` |
| `pending` | 0.50 | `freshness:pending` |
| `deleted` or unavailable content | 0.00 | exclude unless no current equivalent exists |

Lexical fallback sets `freshness=1.0` for indexed chunks and evidence `freshness:lexical_index`.

### `penalty`

Penalties subtract after role-prior multiplication:

| condition | penalty |
| --- | ---: |
| generated/vendor candidate | 0.10 |
| docs candidate for source query with weak lexical `<0.30` and semantic `<0.60` | 0.05 |
| test candidate for source query with weak lexical `<0.30` and semantic `<0.60` | 0.03 |
| broad recursive chunk when a function/class chunk from same file/range is also in pool | 0.04 |
| stale/error freshness | already handled by freshness; no extra penalty unless generated |

## Final ranking formula

### Hybrid mode with semantic available

```text
base =
  0.40 * semantic +
  0.30 * lexical +
  0.10 * symbol +
  0.08 * ast +
  0.07 * structure +
  0.05 * freshness

score = clamp(base * role_prior - penalty, 0, 1)
```

### Hybrid mode without semantic

Redistribute semantic weight proportionally:

```text
base =
  0.50 * lexical +
  0.17 * symbol +
  0.13 * ast +
  0.12 * structure +
  0.08 * freshness

score = clamp(base * role_prior - penalty, 0, 1)
```

### Semantic mode

When semantic is available:

```text
base =
  0.70 * semantic +
  0.10 * lexical +
  0.10 * symbol +
  0.05 * ast +
  0.05 * freshness

score = clamp(base * role_prior - penalty, 0, 1)
```

When semantic is unavailable:

- `backend=auto`: lexical fallback with warning and `backend_fallback=true` if the failure occurred in CocoIndex routing.
- `backend=lexical`: lexical fallback with `metadata.fallback_reason="lexical_chunk_similarity"`.
- `backend=cocoindex`: return `ok:false` with setup guidance for missing tables/embeddings/pgvector unless the request was explicitly impossible because no indexed candidates exist.

### Lexical mode

```text
base =
  0.70 * lexical +
  0.10 * symbol +
  0.08 * ast +
  0.07 * structure +
  0.05 * freshness

score = clamp(base * role_prior - penalty, 0, 1)
```

Lexical mode must not require Postgres, CocoIndex, pgvector, or sentence-transformers.

### Tie-breaking

Sort final candidates by:

1. Final `score` descending.
2. Candidate kind preference:
   - function/method AST chunk,
   - class AST chunk,
   - symbol candidate,
   - source chunk,
   - config chunk,
   - test chunk,
   - docs chunk,
   - generated/unknown chunk.
3. Higher `lexical` score.
4. Shorter chunk length when scores differ by less than `0.01`.
5. Stable `filename`, `start_line`, `end_line`, `candidate_key`.

## Evidence strings

Evidence strings must be deterministic, compact, and user-readable. They are API data, not prose paragraphs.

Use these prefixes:

- `semantic:*`
- `lexical:*`
- `symbol:*`
- `ast:*`
- `structure:*`
- `role:*`
- `freshness:*`
- `penalty:*`
- `fallback:*`
- `file_aggregate:*`

Required evidence examples:

| Condition | Evidence string |
| --- | --- |
| chunk vector similarity used | `semantic:chunk_vector=<rounded>` |
| symbol vector similarity used | `semantic:symbol_vector=<rounded>` |
| shared tokens | `lexical:shared_tokens=retry,backoff` capped to 5 tokens |
| exact symbol stem match | `symbol:name_stem_match=retry` |
| same symbol kind | `symbol:same_kind=function` |
| function/method chunk | `ast:function_chunk` or `ast:method_chunk` |
| broad recursive chunk penalty | `penalty:broad_recursive_chunk` |
| source query/source candidate | `role:source_query_source_candidate` |
| docs query/docs candidate | `role:docs_query_docs_candidate` |
| docs penalty | `penalty:docs_for_source_query` |
| test penalty | `penalty:test_for_source_query` |
| generated penalty | `penalty:generated_or_vendor` |
| lexical fallback | `fallback:lexical_chunk_similarity` |
| symbols unavailable | `fallback:symbols_unavailable_returned_chunks` |
| file aggregation | `file_aggregate:max_candidate` |

Every result with `score > 0` must have at least one lexical or semantic evidence string. Symbol/path/AST-only evidence is not sufficient to rank an otherwise unrelated candidate above zero.

## Risk labels

Set `risk` from strongest evidence:

| Risk | Rule |
| --- | --- |
| `near_duplicate_chunk` | `lexical >= 0.75` or `semantic >= 0.88`, candidate role source, and chunk kind function/method/class/source chunk. |
| `parallel_helper` | Same helper/name stem or symbol kind plus `lexical >= 0.35` or `semantic >= 0.70`. |
| `parallel_command_handler` | Candidate path role is CLI/command handler and shared command/handler tokens exist. |
| `test_drift` | Candidate role test and target/query role source/test with shared implementation tokens. |
| `documentation_overlap` | Candidate role docs and source query/target with meaningful lexical/semantic evidence. |
| `semantic_overlap` | Default when semantic is dominant but no more specific label applies. |

Lexical fallback may still use `semantic_overlap` as the generic default, but should use the more specific labels when deterministic.

## CocoIndex implementation requirements

### Tables and metadata to use

The implementation must use existing canonical data:

- `{prefix}_chunks`
  - `chunk_id`, `file_id`, `repo_id`, `branch_id`, `path`, `start_line`, `end_line`, `code`, `embedding`, `chunk_kind`, `symbol_id`, `token_count`, `metadata`.
- `{prefix}_files`
  - `file_id`, `repo_id`, `branch_id`, `path`, `language`, `sha256`, `metadata`.
- `{prefix}_symbols`
  - `symbol_id`, `file_id`, `repo_id`, `branch_id`, `name`, `qualified_name`, `kind`, `start_line`, `end_line`, `signature`, `docstring`, `metadata`.
- `{prefix}_symbol_embeddings`
  - `symbol_embedding_id`, `symbol_id`, `repo_id`, `branch_id`, `embedding`, `embedding_text`, `metadata`.
- `{prefix}_freshness`
  - freshness status and parser errors when available.

No new durable table is required for one-shot similar-code ranking.

### Pgvector indexes

Ensure vector indexes are idempotent:

- Existing chunk vector index may remain: `{prefix}_chunks_embedding_idx ON {prefix}_chunks USING ivfflat (embedding vector_cosine_ops)`.
- Add or ensure an idempotent symbol embedding vector index:

```sql
CREATE INDEX IF NOT EXISTS {prefix}_symbol_embeddings_embedding_idx
ON {schema}.{prefix}_symbol_embeddings
USING ivfflat (embedding vector_cosine_ops);
```

If pgvector cannot create an index before vector dimensions are known, catch the error like current chunk index setup and continue. Query correctness must not depend on the index existing.

### Query execution

Implement CocoIndex search in `coco_backend.find_similar_code()` or helpers it calls:

1. Load `ProjectConfig` and `GlobalConfig`.
2. If `refresh_first`, run existing refresh.
3. Require CocoIndex dependencies for semantic/hybrid data access.
4. Resolve repo/branch identity using existing helpers.
5. Ensure canonical schema exists.
6. Resolve query context.
7. Compute query embedding when `mode in {semantic, hybrid}` and embeddings are available.
8. Retrieve bounded lexical/vector/symbol/AST candidate pools.
9. Merge by stable candidate key.
10. Score with the formulas in this spec.
11. Apply scope-specific shaping.
12. Return `_coco_context_payload(...)`-compatible envelope with full score components.

### Explicit CocoIndex failures

For `PI_CODE_INDEX_BACKEND=cocoindex` or project/global backend `cocoindex`, these conditions must produce an actionable error instead of silent lexical fallback:

- Postgres URL missing or invalid.
- `pgvector` extension unavailable for semantic/hybrid vector query.
- Canonical chunk table missing after schema setup.
- Embedding column/table missing for `mode="semantic"`.
- Symbol table/embedding missing for `scope="symbols"` when symbols are enabled.

Error text should include the failed requirement and a next action, for example: `run pi-code-index refresh`, `enable_symbols: true`, or `configure PI_CODE_INDEX_POSTGRES_URL`.

## Lexical fallback requirements

Lexical fallback remains first-class and fast.

- Must not import or require CocoIndex, asyncpg, pgvector, Postgres, or sentence-transformers.
- Must continue using `.pi-code-index/index.json` and `indexer.py` chunking.
- Must preserve `metadata.fallback_reason="lexical_chunk_similarity"`.
- Must expose `similarity.lexical`, `similarity.structure`, `similarity.semantic=0.0`, `similarity.symbol`, `similarity.ast`, `similarity.role_prior`, `similarity.freshness`, and `similarity.penalty`.
- Must expose `score_components` with the lexical formula.
- Must adopt `content_role_for()`, role priors, generated/docs/tests penalties, and deterministic tie-breaking.
- Must implement `scope="files"` aggregation.
- Must return chunk results plus warning for `scope="symbols"`.
- Must keep p95 latency under 250 ms for `top_k=12` on medium repos already handled by the extension.

## Acceptance thresholds

The implementation is acceptable only if all thresholds below pass.

### Ranking precision thresholds

Using the benchmark fixture described below:

1. Source-over-docs, hybrid:
   - `find_similar_code(query="retry backoff helper", mode="hybrid", scope="chunks", top_k=8)` ranks `src/http_retry.py` above `README.md` and `docs/retry.md`.
   - At least one source function/method chunk appears in the top 2.
2. Source-over-docs, lexical:
   - Same query with `mode="lexical"` ranks `src/http_retry.py` above `README.md` and `docs/retry.md`.
3. Function-over-broad-chunk:
   - A target source function ranks a similar source function chunk above a broad module-level or recursive chunk from the same or another file when both contain overlapping tokens.
4. Docs target inversion:
   - `target="README.md"` or query clearly about docs allows `README.md`/`docs/retry.md` to rank above unrelated source and does not apply the source-query docs penalty.
5. Symbol scope:
   - With symbols enabled, `scope="symbols"` returns at least one result with non-null `symbol_id`, `symbol`, and `metadata.candidate_kind="symbol"`.
6. File scope:
   - `scope="files"` returns one result per filename with `metadata.aggregated_candidates` and no duplicate filenames.
7. Evidence:
   - Every returned non-zero result includes non-empty `evidence`, `similarity`, `score_components`, and `metadata.ranking_profile`.
8. Fallback:
   - With CocoIndex disabled or Postgres unavailable under `backend=auto`, results return lexical fallback with `metadata.fallback_reason="lexical_chunk_similarity"` and warning/backend fallback fields where appropriate.

### Performance thresholds

On a warmed index:

- Lexical fallback, `top_k=12`: p95 under 250 ms.
- CocoIndex hybrid, `top_k=12`: p95 under 750 ms after daemon/resource warmup.
- CocoIndex semantic-only, `top_k=12`: p95 under 600 ms after query embedding is computed/cached.
- Daemon socket overhead: no more than 50 ms beyond backend query time.
- Merged candidate scoring: default cap 500 candidates.

### Contract thresholds

- Existing Pi tool schema remains compatible.
- Existing CLI command and flags remain compatible.
- Existing TypeScript formatter tests pass without requiring clients to understand new fields.
- Existing lexical test asserting `metadata.fallback_reason == "lexical_chunk_similarity"` still passes.
- `top_k` clamps to `1..100` through CLI/daemon/backend.
- Missing target/query and invalid enum errors remain JSON-safe.

## Test requirements

### `tests/test_context_tools.py`

Add or update tests for lexical fallback:

- `test_find_similar_code_lexical_source_role_prior_ranks_source_over_docs`
  - Build a temp repo with source retry helper, docs/README mentioning retry/backoff heavily, and tests.
  - `backend.find_similar_code(..., query="retry backoff helper", mode="lexical")` returns source before docs.
- `test_find_similar_code_lexical_docs_target_inverts_prior`
  - `target="README.md"` or docs-like query returns docs with high role prior and no docs-for-source penalty.
- `test_find_similar_code_lexical_files_scope_aggregates_by_file`
  - Assert unique filenames, `metadata.candidate_kind="file"`, and `metadata.aggregated_candidates`.
- `test_find_similar_code_lexical_symbols_scope_warns_and_returns_chunks`
  - Assert warning contains `symbols unavailable in lexical fallback; returned chunks` and results are chunk-like.
- `test_find_similar_code_components_and_evidence_present`
  - Assert `similarity.lexical`, `similarity.structure`, `similarity.role_prior`, `score_components.final`, `evidence`, and fallback metadata.
- Keep existing validation tests for missing target/query and enum validation.

### `tests/test_cocoindex_postgres_integration.py`

Add integration tests gated by the existing Postgres skip helper:

- `test_find_similar_code_hybrid_uses_vectors_and_components`
  - Refresh fixture with `backend: cocoindex`, `chunk_strategy: hybrid`, `enable_symbols: true`.
  - Query retry/backoff fixture.
  - Assert `backend="cocoindex"`, `ranking_profile="similar-code-v2"`, `similarity.semantic` present, `score_components.semantic` present, and evidence contains `semantic:*`.
- `test_find_similar_code_hybrid_ranks_source_functions_over_docs`
  - Assert source retry helper/function chunk ranks above `README.md` and `docs/retry.md`.
- `test_find_similar_code_symbols_scope_returns_symbol_candidates`
  - Assert non-null `symbol_id`, `symbol`, `metadata.candidate_kind="symbol"`, and relevant symbol evidence.
- `test_find_similar_code_files_scope_aggregates_candidates`
  - Assert no duplicate filenames and aggregate metadata.
- `test_find_similar_code_explicit_cocoindex_errors_when_embeddings_missing`
  - Drop or hide required embedding table/column in an isolated schema or configure missing tables.
  - With explicit backend `cocoindex`, assert `ok is False` or JSON-safe error with setup guidance, not lexical fallback.

### `tests/test_daemon_lifecycle.py`

Add daemon forwarding coverage:

- `test_daemon_routes_find_similar_code_with_cached_resources_and_options`
  - Monkeypatch `pi_code_index.daemon.find_similar_code`.
  - Send request with `mode`, `scope`, `exclude_self=false`, `top_k=999`, `query`, and `target`.
  - Assert `top_k == 100`, options are forwarded unchanged, and `coco_resources` is reused when CocoIndex backend is configured.

### `tests/format-results.test.ts`

No formatter change is required. Existing test must continue passing. Add a test only if compact output is intentionally extended, and then assert output remains bounded and does not dump full `score_components`.

## Benchmark fixture

Use a small temporary repository in pytest, not a checked-in large fixture. Create at least:

```text
src/retry.py
src/http_retry.py
src/unrelated.py
README.md
docs/retry.md
tests/test_retry.py
```

Suggested contents:

- `src/retry.py`
  - Defines `retry_with_backoff(fn, attempts=3, base_delay=0.1)` with loop, exception handling, exponential backoff, and jitter tokens.
- `src/http_retry.py`
  - Defines `retry_http_request(client, request, max_attempts=3)` or similar with overlapping retry/backoff behavior but different names.
- `src/unrelated.py`
  - Defines unrelated parsing/math/string helper with no retry/backoff tokens.
- `README.md`
  - Mentions retry/backoff many times in prose, enough to be noisy under old lexical ranking.
- `docs/retry.md`
  - Explains retry/backoff behavior in prose.
- `tests/test_retry.py`
  - Tests retry helper with `pytest`, `assert`, and mocks.

Fixture assertions must compare relative ordering, not exact floating scores, except where score range/component presence is part of the contract.

## Validation commands

Local fast checks:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
uv run pytest tests/test_context_tools.py tests/test_daemon_lifecycle.py
npm run typecheck
npm run test:ts
```

Full non-Postgres suite:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
uv run pytest
npm run typecheck
npm run test:ts
```

Postgres/pgvector integration uses Podman:

```bash
podman run --rm --name pi-code-index-pgvector \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_DB=pi_code_index \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

In another shell:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
PI_CODE_INDEX_BACKEND=cocoindex \
PI_CODE_INDEX_POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/pi_code_index \
uv run pytest tests/test_cocoindex_postgres_integration.py
```

Manual CLI smoke tests after implementation:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
uv run pi-code-index context similar --json --repo . --query "retry backoff helper" --mode lexical --scope chunks --top-k 8
uv run pi-code-index context similar --json --repo . --query "retry backoff helper" --mode hybrid --scope files --top-k 8
uv run pi-code-index context similar --json --repo . --target src/pi_code_index/context_tools.py --mode lexical --scope chunks --top-k 8
```

## Implementation checklist for the next agent

The implementation issue is done only when:

- `content_role_for()`, role priors, penalties, component scoring, and deterministic tie-breaking exist in shared helpers.
- Lexical fallback uses `similar-code-v2-lexical-fallback`, keeps `fallback_reason`, implements file aggregation, and warns for symbol scope.
- CocoIndex path uses canonical chunks, files, symbols, symbol embeddings, freshness, vector search, lexical evidence, AST metadata, and bounded candidate merging.
- Results expose `similarity`, `score_components`, `evidence`, `risk`, and scope metadata.
- `scope="chunks"`, `scope="symbols"`, and `scope="files"` behave as documented.
- `backend=auto` preserves fast lexical fallback; explicit `backend=cocoindex` surfaces setup/data errors.
- Benchmark fixture proves source/function results outrank docs/tests by default while docs/test targets invert priors appropriately.
- Validation commands above pass, with Podman-backed Postgres for integration tests.
