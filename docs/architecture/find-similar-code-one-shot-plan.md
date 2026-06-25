# find_similar_code one-shot implementation plan

## Scope and constraints

This plan is for a single final implementation of improved `find_similar_code` relevance. It is not a phased rollout. The implementation must preserve the existing Pi tool, CLI, daemon, backend routing, and lexical fallback UX. Product changes should be additive: extra response fields, optional CLI/Pi options, and richer metadata are allowed, but existing parameters and required fields must remain valid.

Use only CocoIndex V1 concepts already used by this extension: `coco.App`, `coco.AppConfig`, `@coco.fn`, `@coco.fn(memo=True)`, `@coco.lifespan`, `coco.ContextKey`, `localfs.walk_dir`, `coco.map`, `coco.mount_each`, `postgres.mount_table_target`, `postgres.TableSchema.from_class`, `TableTarget.declare_row`, `TableTarget.declare_vector_index`, and idempotent `asyncpg` DDL where V1 table targets are insufficient.

## Current state

Inspected files:

- `index.ts`
- `src/pi_code_index/backend.py`
- `src/pi_code_index/cli.py`
- `src/pi_code_index/context_tools.py`
- `src/pi_code_index/coco_backend.py`
- `src/pi_code_index/daemon.py`
- `src/pi_code_index/indexer.py`
- `tests/test_context_tools.py`
- `tests/format-results.test.ts`
- `docs/architecture/repo-quality-context-spec.md`
- `docs/architecture/ast-aware-semantic-search-spec.md`
- `docs/architecture/final-integration-spec.md`

Current request path:

```text
Pi tool find_similar_code in index.ts
  -> uv run --project <extension> pi-code-index context similar --json ...
  -> cli.py context similar
  -> optional daemon request type find_similar_code
  -> backend.py find_similar_code router
  -> context_tools.find_similar_code for lexical fallback
  -> coco_backend.find_similar_code currently wraps the same context_tools implementation
```

Current behavior:

- `index.ts` registers `find_similar_code` with `target`, `query`, `top_k`, `mode`, `scope`, `exclude_self`, and `refresh`.
- `cli.py` exposes `pi-code-index context similar --json --top-k --mode semantic|lexical|hybrid --scope chunks|symbols|files --exclude-self|--no-exclude-self --query [TARGET]`.
- `daemon.py` forwards the same fields to `backend.find_similar_code()`.
- `backend.py` validates missing target/query, `mode`, and `scope`, clamps `top_k`, and routes to lexical or CocoIndex.
- `coco_backend.find_similar_code()` only enriches identity/capabilities and delegates to the lexical implementation, so the CocoIndex path does not use embeddings, AST chunks, symbols, or Postgres ranking yet.
- `context_tools.find_similar_code()` builds a query text from `query` plus up to 4000 bytes of target-file chunks, tokenizes it, scores every lexical chunk, adds a weak path-role structure score for hybrid mode, and returns `similarity.lexical` plus `similarity.structure`.
- `scope="symbols"` and `scope="files"` are accepted but not meaningfully implemented beyond chunk results.
- Docs/tests assert lexical fallback metadata such as `metadata.fallback_reason == "lexical_chunk_similarity"`.

Problem from recent benchmarks: lexical mode is fast but noisy. It can find real retry/helper matches, but docs/README or weakly related files can rank above or near source code because token overlap is the dominant signal and there are no default code/test/docs priors.

## Target behavior

### User-visible behavior

Keep existing calls valid:

```ts
find_similar_code({
  target?: string,
  query?: string,
  top_k?: number,
  mode?: "semantic" | "lexical" | "hybrid",
  scope?: "chunks" | "symbols" | "files",
  exclude_self?: boolean,
  refresh?: boolean
})
```

Default behavior remains `mode="hybrid"`, `scope="chunks"`, `exclude_self=true`, `top_k=12`.

Expected relevance rules:

1. Source/function chunks rank above docs/tests/config by default when the query/target is source code.
2. AST/function chunks rank above broad recursive text chunks when both are otherwise similar.
3. Exact or near-exact code/token matches still surface even without embeddings.
4. Symbol name/kind/path-role matches boost candidates only when they agree with lexical or vector evidence.
5. Docs and tests are not hidden; they are penalized by default and can still rank when the target/query is docs/tests or the only strong evidence is in docs/tests.
6. `mode="lexical"` preserves no-Postgres fallback semantics but gains deterministic code-first penalties/boosts from local path/chunk metadata.
7. `mode="semantic"` uses vector similarity as the primary signal when CocoIndex/Postgres is available; if unavailable, return lexical fallback with `backend_fallback`/warning using existing router behavior.
8. `mode="hybrid"` uses lexical, vector, AST, symbol, and path-role signals when available, with honest component values and evidence labels.
9. `scope="chunks"` returns chunk ranges; `scope="symbols"` returns symbol-centered candidates where available; `scope="files"` aggregates top chunk/symbol evidence per file.
10. `exclude_self=true` excludes the target file/chunk/symbol itself but does not exclude sibling chunks from the same file when `query` is provided, because snippet queries may intentionally search within a file.

### Exact ranking outcomes to validate

On dummy benchmark repositories:

- A retry helper query should rank source helpers and functions above `README.md`, `docs/*`, and unrelated tests.
- A target source function should rank similar source functions before broad module-level chunks.
- A docs query or markdown target should not be over-penalized; docs remain eligible when the query clearly targets documentation.
- With CocoIndex disabled or Postgres unavailable, results still return quickly with lexical fallback and `metadata.fallback_reason="lexical_chunk_similarity"`.

## Affected modules

### `src/pi_code_index/context_tools.py`

Lexical fallback owner. Add reusable helpers shared by fallback and CocoIndex result normalization:

- `content_role_for(path) -> "source" | "test" | "docs" | "config" | "generated" | "unknown"`.
- `chunk_kind_weight(chunk_kind, metadata) -> float`.
- `role_prior(query_role, target_role, candidate_role) -> float`.
- `scope` handling for file aggregation in lexical mode.
- Expanded `similarity` and `score_components` fields while preserving existing `similarity.lexical`, `similarity.structure`, required top-level fields, and `metadata.fallback_reason`.

### `src/pi_code_index/coco_backend.py`

Primary implementation owner for hybrid search. Add async Postgres-backed similarity functions that query canonical tables:

- chunks: `{prefix}_chunks` joined to `{prefix}_files` and optionally `{prefix}_symbols`.
- symbols: `{prefix}_symbols` joined to `{prefix}_symbol_embeddings` and files.
- freshness: exclude stale/error rows by default only when a current equivalent exists; otherwise include with penalty and evidence.

No new pipeline concept is required. Reuse existing stored `chunks.embedding`, `chunks.chunk_kind`, `chunks.symbol_id`, `chunks.metadata`, `symbols.kind`, `symbols.qualified_name`, and `symbol_embeddings.embedding` generated by the existing CocoIndex V1 app.

Add vector indexes via existing table/vector-index patterns where missing. If idempotent `asyncpg` DDL is needed for pgvector indexes, keep it within the existing schema setup path.

### `src/pi_code_index/backend.py`

Keep validation and routing as the compatibility boundary. Add only additive enum validation if new optional values are introduced. Ensure explicit `PI_CODE_INDEX_BACKEND=cocoindex` surfaces real CocoIndex errors, while `auto` preserves `_with_auto_fallback()`.

### `src/pi_code_index/cli.py`

Preserve current command and flags. Optional additive flags may be added, for example:

- `--include-docs` / `--no-include-docs` only if needed later; default remains docs penalized, not excluded.
- `--ranking-profile similar-code-v2` only if multiple scoring profiles become necessary.

The one-shot implementation should avoid new flags unless required for tests. Existing `--mode` and `--scope` are enough.

### `src/pi_code_index/daemon.py`

Preserve `find_similar_code` request shape. Forward any additive optional fields only after Python-side validation and bounded defaults. Reuse cached `CocoBackendResources` for embedding/vector query work.

### `index.ts`

Preserve Pi tool schema and compact formatter. Additive rendering is allowed:

- Keep displaying `score`, `sim=<json>`, `risk`, file range, and clipped code.
- Unknown fields remain in `details.cli_json`.
- If adding display text, keep it compact and bounded; do not require formatter changes for the feature to work.

### Tests/docs

Update or add tests under:

- `tests/test_context_tools.py`
- `tests/test_cocoindex_postgres_integration.py`
- `tests/test_daemon_lifecycle.py`
- `tests/format-results.test.ts` only if compact text changes
- Benchmark fixture tests under existing pytest style if a fixture exists, otherwise add a small temporary repo in pytest

## Data and API contracts

### Request contract

Existing request fields remain unchanged. Validation remains:

- `target` or `query` is required.
- `top_k`: clamp to `1..100`.
- `mode`: `semantic | lexical | hybrid`.
- `scope`: `chunks | symbols | files`.
- `exclude_self`: default `true`.

### Response envelope

Keep the shared context envelope:

```json
{
  "ok": true,
  "backend": "cocoindex",
  "operation": "find_similar_code",
  "repo": "/repo",
  "repo_id": "...",
  "branch": "main",
  "branch_id": "...",
  "schema_version": 1,
  "pipeline_version": "...",
  "capabilities": { "similar_code": true },
  "target": "src/pkg/retry.py",
  "query": null,
  "mode": "hybrid",
  "scope": "chunks",
  "exclude_self": true,
  "top_k": 12,
  "results": [],
  "truncated": false,
  "truncation": {}
}
```

### Result contract

Every result keeps required fields:

```json
{
  "score": 0.873421,
  "confidence": 0.78,
  "filename": "src/pkg/retry.py",
  "start_line": 10,
  "end_line": 42,
  "code": "...",
  "symbol": "retry_with_backoff",
  "symbol_id": "...",
  "chunk_id": "...",
  "risk": "near_duplicate_chunk",
  "evidence": ["vector_similarity", "shared_tokens", "same_symbol_kind", "source_role"],
  "similarity": {
    "semantic": 0.82,
    "lexical": 0.71,
    "structure": 0.65,
    "symbol": 0.60,
    "ast": 0.75,
    "role_prior": 1.00,
    "freshness": 1.00
  },
  "score_components": {
    "semantic": 0.328,
    "lexical": 0.213,
    "symbol": 0.090,
    "ast": 0.075,
    "structure": 0.065,
    "role_prior": 0.050,
    "freshness": 0.050,
    "penalty": -0.000
  },
  "metadata": {
    "ranking_profile": "similar-code-v2",
    "candidate_kind": "chunk",
    "content_role": "source",
    "chunk_kind": "function",
    "source": "cocoindex-hybrid-similar-code-v2"
  }
}
```

Additive fields are optional. Existing clients may ignore them. `similarity.lexical` and `similarity.structure` remain for compatibility. Lexical fallback must keep:

```json
"metadata": {
  "ranking_profile": "similar-code-v2-lexical-fallback",
  "source": "similar-code-v2-lexical-fallback",
  "fallback_reason": "lexical_chunk_similarity"
}
```

### Scope contracts

`scope="chunks"`:

- Return chunk candidates with `chunk_id` and optional `symbol_id`.
- Prefer AST/function chunks over recursive text chunks using `ast` and `chunk_kind` signals.

`scope="symbols"`:

- Return one result per symbol candidate.
- `filename`, line range, `code`, `symbol`, and `symbol_id` are required when available.
- Score can use `symbol_embeddings.embedding`, symbol qualified-name token overlap, kind match, and related chunk evidence.
- If symbols are disabled, return chunk fallback with a warning, not an empty success unless no candidates exist.

`scope="files"`:

- Aggregate by file using max chunk/symbol score plus secondary evidence count.
- Return a representative best range and `metadata.aggregated_candidates`.
- Penalize docs/tests by default unless target/query role is docs/tests.

## Ranking approach

### Candidate retrieval

Use a bounded two-stage approach to control latency:

1. Resolve target/query context.
   - For `target` file/line/symbol, resolve target file path and line range. If a symbol covers the target line, use symbol text plus surrounding AST chunk as query text.
   - For `query`, embed/query tokenize the raw query.
2. Build candidate pool.
   - Lexical candidates: top `max(200, top_k * 20)` by token score from the lexical index or SQL-side token material if available.
   - Vector chunk candidates: top `max(100, top_k * 10)` by `chunks.embedding <=> query_embedding`.
   - Vector symbol candidates: top `max(100, top_k * 10)` by `symbol_embeddings.embedding <=> query_embedding` when symbols are enabled.
   - AST/symbol neighbors: chunks with same `symbol.kind`, same basename/stem tokens, same `chunk_kind`, or same parent directory role.
3. Merge candidates by stable key (`chunk_id`, `symbol_id`, or `filename`) and score once.

### Component normalization

All component scores are normalized to `0..1` before weighting:

- `semantic`: `1 - cosine_distance`, clamped to `0..1`.
- `lexical`: existing `score_tokens()` output or SQL-equivalent token overlap.
- `symbol`: max of qualified-name token overlap, same symbol kind, same receiver/class/module stem.
- `ast`: function/class/method chunks get higher score than broad recursive text for code queries; fallback recursive chunks remain eligible.
- `structure`: role/path/module proximity from existing `role_for()` plus package/path stem overlap.
- `role_prior`: source/docs/tests/config prior based on query/target role.
- `freshness`: 1.0 current, 0.75 stale, 0.50 parser-error fallback, 0.0 deleted/unavailable.
- `penalty`: generated/vendor/docs/tests penalties where applicable.

### Final formula

For `mode="hybrid"`:

```text
base =
  0.40 * semantic_available_or_0 +
  0.30 * lexical +
  0.10 * symbol +
  0.08 * ast +
  0.07 * structure +
  0.05 * freshness

score = clamp(base * role_prior - penalty, 0, 1)
```

If semantic is unavailable, redistribute its 0.40 weight proportionally to lexical, symbol, AST, and structure:

```text
lexical 0.50, symbol 0.17, ast 0.13, structure 0.12, freshness 0.08
```

For `mode="semantic"`:

```text
score = clamp((0.70 * semantic + 0.10 * lexical + 0.10 * symbol + 0.05 * ast + 0.05 * freshness) * role_prior - penalty, 0, 1)
```

If semantic is unavailable in `auto`, use lexical fallback with warning. If `backend=cocoindex` was explicitly requested and embeddings/tables are missing, return an actionable error.

For `mode="lexical"`:

```text
score = clamp((0.70 * lexical + 0.10 * symbol + 0.08 * ast + 0.07 * structure + 0.05 * freshness) * role_prior - penalty, 0, 1)
```

### Role priors and penalties

Default content roles:

- `source`: `.py`, `.ts`, `.tsx`, `.js`, `.jsx` outside test/docs directories.
- `test`: existing `is_test_path()` rules.
- `docs`: `docs/**`, `README*`, `*.md`, `*.rst`.
- `config`: `.toml`, `.yaml`, `.yml`, `.json`, lockfiles.
- `generated`: minified files, lockfiles, large generated directories, vendored paths.

Default priors for source query/target:

| candidate role | multiplier/penalty |
| --- | --- |
| source | `role_prior=1.00` |
| config | `role_prior=0.85` |
| test | `role_prior=0.70` |
| docs | `role_prior=0.55` |
| generated/vendor | `role_prior=0.30`, plus `penalty=0.10` |

For docs target/query, docs prior becomes `1.00` and source becomes `0.85`. For test target/query, tests become `1.00` and source remains `0.90`.

### Tie-breaking

Sort by:

1. Final `score` descending.
2. Candidate kind preference: function/method/class AST chunk, symbol, source chunk, config chunk, test chunk, docs chunk.
3. Higher `lexical` for exact token overlap.
4. Shorter chunk length when scores differ by less than `0.01`.
5. Stable `filename`, `start_line`, `chunk_id`.

### Risk labels

Set `risk` from evidence:

- `near_duplicate_chunk`: lexical >= 0.75 or semantic >= 0.88 with source/function chunk.
- `parallel_helper`: same symbol kind/name stem or helper tokens with semantic/lexical agreement.
- `parallel_command_handler`: CLI/command path role and shared command-handler tokens.
- `test_drift`: test candidate mirrors source target.
- `documentation_overlap`: docs candidate matches source behavior but is penalized.
- `semantic_overlap`: default.

## Fallback behavior

Lexical fallback remains first-class:

- It must not require Postgres, pgvector, or CocoIndex.
- It must continue using `.pi-code-index/index.json` and `indexer.py` chunking.
- It must return the same envelope, required result fields, `similarity.lexical`, `similarity.structure`, and `metadata.fallback_reason`.
- It should adopt source/docs/tests role priors and `scope="files"` aggregation to reduce noise even without embeddings.
- `scope="symbols"` in lexical fallback returns chunk results with warning `symbols unavailable in lexical fallback; returned chunks`.

`backend=auto`:

- On CocoIndex query failure, preserve `_with_auto_fallback()` and include warning with original error.

`backend=cocoindex`:

- Do not silently degrade semantic/hybrid data-access failures to lexical. Return `ok:false` or raise through existing router behavior with clear setup guidance.

## Tests and validation

### Unit tests

Add or update pytest coverage:

- `tests/test_context_tools.py`
  - lexical source query ranks source code over README/docs when token overlap is comparable.
  - docs query/target can rank docs without permanent exclusion.
  - `scope="files"` aggregates multiple chunks into one file result.
  - `scope="symbols"` in lexical fallback warns and returns chunks.
  - missing target/query and enum validation still pass.

- `tests/test_cocoindex_postgres_integration.py`
  - hybrid query uses chunk embeddings and exposes `similarity.semantic`, `score_components`, and evidence.
  - AST/function chunks outrank README/docs for a retry-helper fixture.
  - `scope="symbols"` returns symbol-centered candidates when symbols are enabled.
  - `scope="files"` aggregates best evidence per file.
  - explicit CocoIndex mode errors clearly when canonical tables/embeddings are missing.

- `tests/test_daemon_lifecycle.py`
  - daemon forwards `mode`, `scope`, `exclude_self`, and any additive fields.
  - response remains JSON-compatible through socket path.

- `tests/format-results.test.ts`
  - Only update if formatter text changes. Existing test that verifies clipping and `risk` should continue passing with additive fields.

### Benchmark fixture tests

Create a small temporary repository in pytest with:

```text
src/retry.py              # real retry helper
src/http_retry.py         # similar source helper
src/unrelated.py          # unrelated source
README.md                 # mentions retry/backoff text heavily
docs/retry.md             # related docs
tests/test_retry.py       # related test
```

Assertions:

- `find_similar_code(query="retry backoff helper", mode="hybrid")` ranks `src/http_retry.py` before `README.md` and `docs/retry.md`.
- `mode="lexical"` also keeps source before docs because of role priors.
- `target="README.md"` allows docs to rank strongly.
- Result details include evidence explaining each boost/penalty.

### Validation commands

Run local checks:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
uv run pytest tests/test_context_tools.py tests/test_daemon_lifecycle.py
uv run pytest tests/test_cocoindex_postgres_integration.py
npm run typecheck
npm run test:ts
```

Run full suite when implementation is complete:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
uv run pytest
npm run typecheck
npm run test:ts
```

For Postgres/pgvector integration, use Podman, not Docker:

```bash
podman run --rm --name pi-code-index-pgvector \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_DB=pi_code_index \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

Then run, in another shell:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
PI_CODE_INDEX_BACKEND=cocoindex \
PI_CODE_INDEX_POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/pi_code_index \
uv run pytest tests/test_cocoindex_postgres_integration.py
```

Manual CLI smoke tests:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
uv run pi-code-index context similar --json --repo . --query "retry backoff helper" --mode lexical --scope chunks --top-k 8
uv run pi-code-index context similar --json --repo . --query "retry backoff helper" --mode hybrid --scope files --top-k 8
uv run pi-code-index context similar --json --repo . --target src/pi_code_index/context_tools.py --mode lexical --scope chunks --top-k 8
```

## Overhead targets

Latency targets on a warmed index:

- Lexical fallback, `top_k=12`: p95 under 250 ms on medium repos already supported by the extension.
- CocoIndex hybrid, `top_k=12`: p95 under 750 ms after daemon/resource warmup.
- CocoIndex semantic-only, `top_k=12`: p95 under 600 ms after query embedding is computed/cached.
- Daemon socket overhead: no more than 50 ms beyond backend query time.

Candidate limits:

- Default candidate pool bounded to at most 500 merged candidates per request.
- SQL vector candidate limit at most `max(100, top_k * 10)` per vector source.
- Lexical candidate limit at most `max(200, top_k * 20)`.
- Returned code snippets keep existing formatter clipping; backend snippets should remain bounded to current `12000`-character pattern unless changed by a separate UX issue.

Storage/index overhead:

- No new large durable table is required for the one-shot design.
- Reuse existing chunk embeddings and symbol embeddings.
- Optional pgvector indexes should be idempotent and tied to existing canonical tables.
- No new file watching behavior; refresh/live behavior remains as currently implemented.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Docs/tests become invisible | Use priors, not filters; invert priors for docs/test target/query roles. |
| Hybrid scoring is hard to debug | Always expose `similarity`, `score_components`, `evidence`, and `metadata.ranking_profile`. |
| Embedding/vector setup varies by user | Preserve lexical fallback and explicit setup errors for `backend=cocoindex`. |
| AST/symbol data may be disabled or parse-error fallback | Treat missing components as unavailable, redistribute weights, include freshness/parser evidence. |
| Scope semantics could break clients | Preserve existing fields and only add scope-specific metadata; keep chunks as fallback for unavailable symbols. |
| Performance regresses on large repos | Use two-stage bounded retrieval, daemon resource reuse, query embedding memoization where available, and SQL limits. |
| Ranking constants drift without tests | Lock benchmark fixture expectations for source-over-docs and evidence fields. |
| Generated/vendor files pollute candidates | Add role detection and penalties; keep candidates eligible only with strong evidence. |

## Definition of done for implementation issue

- Existing Pi/CLI/daemon contracts still pass current tests.
- Hybrid CocoIndex results use vector, lexical, AST, symbol, structure, role, and freshness signals when available.
- Lexical fallback is preserved and less noisy through source/docs/tests priors.
- `scope="chunks"`, `scope="symbols"`, and `scope="files"` have documented and tested behavior.
- Results expose component scores and evidence in full JSON details.
- Benchmark fixture proves retry/source helpers rank above README/docs by default.
- Validation commands above pass with Podman-backed Postgres for integration tests.
