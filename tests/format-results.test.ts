import assert from "node:assert/strict";
import test from "node:test";

import { formatDoctorCommand, formatFindTestsResults, formatGraphResult, formatImpactResult, formatRepoMapResults, formatResults, formatReviewContextResults, formatSimilarCodeResults, formatStatusCommand, formatSymbolContextResult, formatSymbolDefinitionResult, formatSymbolSearchResults } from "../index.ts";

function result(index: number, code = "const value = 1;") {
  return {
    score: 0.9 - index / 100,
    filename: `src/file${index}.ts`,
    start_line: index + 1,
    end_line: index + 2,
    code,
  };
}

test("formatResults keeps code_search_local output compact and action-oriented", () => {
  const formatted = formatResults({
    query: "where is search formatted",
    top_k: 2,
    refresh: false,
    repo: "/repo",
    results: [result(0), result(1)],
  });

  assert.match(formatted.text, /code_search_local: where is search formatted/);
  assert.match(formatted.text, /src\/file0.ts:1-2 score=0\.900/);
  assert.match(formatted.text, /Next: use `read` or open the listed file ranges/);
  assert.equal(formatted.summary.displayedResults, 2);
  assert.equal(formatted.summary.omittedResults, 0);
});

test("formatResults truncates large result sets without losing summary counts", () => {
  const formatted = formatResults({
    query: "many matches",
    top_k: 20,
    refresh: false,
    repo: "/repo",
    results: Array.from({ length: 12 }, (_, index) => result(index)),
  });

  assert.equal(formatted.summary.totalResults, 12);
  assert.equal(formatted.summary.displayedResults, 8);
  assert.equal(formatted.summary.omittedResults, 4);
  assert.match(formatted.text, /4 more results omitted from display/);
  assert.doesNotMatch(formatted.text, /src\/file11.ts/);
});

test("formatResults truncates long snippets", () => {
  const formatted = formatResults({
    query: "long snippet",
    top_k: 1,
    refresh: false,
    repo: "/repo",
    results: [result(0, "x".repeat(2_000))],
  });

  assert.equal(formatted.summary.truncatedSnippets, 1);
  assert.match(formatted.text, /snippet truncated; open\/read file for more/);
});

test("formatResults ignores optional canonical metadata in compact output", () => {
  const withMetadata = {
    ...result(0),
    result_id: "chunk",
    metadata: {
      backend: "cocoindex",
      repo_id: "repo",
      file_id: "file",
      chunk_id: "chunk",
      freshness_status: "current",
      ranking: { final_score: 0.9 },
      lineage: { parser: "python_ast" },
      truncation: { code_truncated: false },
    },
  };

  const formatted = formatResults({
    query: "metadata",
    top_k: 1,
    refresh: false,
    repo: "/repo",
    ranking_profile: "semantic_ast_v1",
    truncation: { candidate_limit: 160, result_code_bytes_limit: 12000, omitted_candidates: 0 },
    results: [withMetadata],
  });

  assert.match(formatted.text, /src\/file0.ts:1-2 score=0\.900/);
  assert.doesNotMatch(formatted.text, /chunk_id|repo_id|freshness_status|semantic_ast_v1|result_id|python_ast/);
});

test("formatSymbolSearchResults shows compact symbol lines and signatures", () => {
  const formatted = formatSymbolSearchResults({
    ok: true,
    query: "config loader",
    results: [{ score: 0.94, filename: "src/config.py", start_line: 10, end_line: 20, kind: "function", qualified_name: "config.load", language: "python", signature: "def load():" }],
  });

  assert.match(formatted.text, /symbol_search: config loader/);
  assert.match(formatted.text, /src\/config.py:10-20 function config.load language=python score=0\.940/);
  assert.match(formatted.text, /def load\(\):/);
  assert.equal(formatted.summary.totalResults, 1);
});

test("formatSymbolDefinitionResult handles resolved, ambiguous, and not found", () => {
  const resolved = formatSymbolDefinitionResult({ ok: true, target: "config.load", definition: { filename: "src/config.py", start_line: 10, end_line: 20, kind: "function", qualified_name: "config.load", language: "python", signature: "def load():" } });
  assert.match(resolved.text, /symbol_definition: config.load -> src\/config.py:10-20/);

  const ambiguous = formatSymbolDefinitionResult({ ok: true, target: "load", definition: null, warning: "ambiguous target", matches: [{ score: 1, filename: "a.py", start_line: 1, end_line: 2, kind: "function", qualified_name: "a.load", language: "python" }] });
  assert.match(ambiguous.text, /Warning: ambiguous target/);
  assert.match(ambiguous.text, /a.py:1-2/);

  const missing = formatSymbolDefinitionResult({ ok: true, target: "missing", definition: null, matches: [], warning: "symbol target not found" });
  assert.match(missing.text, /Try `code_search_local`/);
});

test("formatSymbolDefinitionResult reports omitted ambiguous matches", () => {
  const many = Array.from({ length: 12 }, (_, i) => ({ score: 0.5, filename: `f${i}.py`, start_line: 1, end_line: 2, kind: "function", qualified_name: `f${i}.load`, language: "python" }));
  const formatted = formatSymbolDefinitionResult({ ok: true, target: "load", definition: null, warning: "ambiguous target", matches: many });
  assert.match(formatted.text, /ambiguous target/);
  assert.equal(formatted.summary.omittedResults, 4);
  assert.match(formatted.text, /4 more candidates omitted/);
});

test("formatGraphResult shows compact caller lines and callsite", () => {
  const formatted = formatGraphResult({
    ok: true,
    operation: "find_callers",
    target: "pkg.target",
    results: [{ relationship: "caller", distance: 1, score: 0.91, path_confidence: 0.93, symbol: { filename: "src/caller.py", start_line: 5, end_line: 8, kind: "function", qualified_name: "pkg.caller", language: "python" }, paths: [{ callsite: { filename: "src/caller.py", line: 7, column: 4 } }] }],
  });

  assert.match(formatted.text, /find_callers: pkg.target/);
  assert.match(formatted.text, /src\/caller.py:5-8 function pkg.caller language=python distance=1 score=0\.910 confidence=0\.930/);
  assert.match(formatted.text, /callsite: src\/caller.py:7:4/);
});

test("formatGraphResult does not duplicate message as warning", () => {
  const res = formatGraphResult({ ok: true, operation: "find_callers", target: "pkg.target", available: false, unsupported: true, status: "unsupported", warning: "Unsupported without CocoIndex/Postgres", message: "Unsupported without CocoIndex/Postgres: call graph tools require reference indexing", results: [] });
  const occurrences = res.text.split("Unsupported without CocoIndex/Postgres").length - 1;
  assert.equal(occurrences, 1);
});

test("formatGraphResult labels unsupported graph payloads instead of no results", () => {
  const formatted = formatGraphResult({
    ok: true,
    operation: "find_callers",
    target: "pkg.target",
    available: false,
    unsupported: true,
    status: "unsupported",
    message: "Unsupported without CocoIndex/Postgres: call graph tools require reference indexing",
    results: [],
  });

  assert.match(formatted.text, /Unsupported without CocoIndex\/Postgres/);
  assert.doesNotMatch(formatted.text, /No call graph results found/);
});

test("formatImpactResult labels unsupported impact payloads", () => {
  const formatted = formatImpactResult({
    ok: true,
    operation: "impact_analysis",
    target: "pkg.target",
    available: false,
    unsupported: true,
    status: "unsupported",
    message: "Unsupported without CocoIndex/Postgres: impact_analysis requires reference indexing",
    summary: { affected_files: 0 },
  });

  assert.match(formatted.text, /Unsupported without CocoIndex\/Postgres/);
});

test("formatImpactResult summarizes affected files and tests", () => {
  const formatted = formatImpactResult({
    ok: true,
    operation: "impact_analysis",
    target: "pkg.target",
    summary: { direct_callers: 1, affected_files: 1 },
    affected_symbols: [{ relationship: "caller", distance: 1, score: 0.8, symbol: { filename: "src/caller.py", start_line: 5, end_line: 8, kind: "function", qualified_name: "pkg.caller", language: "python" } }],
    affected_files: [{ filename: "src/caller.py", score: 0.8, reasons: ["direct_caller"] }],
    affected_tests: [{ filename: "tests/test_caller.py", score: 0.48, reason: "path_convention" }],
  });

  assert.match(formatted.text, /Summary:/);
  assert.match(formatted.text, /src\/caller.py score=0\.800 reasons=\["direct_caller"\]/);
  assert.match(formatted.text, /tests\/test_caller.py score=0\.480 reason=path_convention/);
});

test("formatSymbolContextResult sections tolerate empty metadata", () => {
  const formatted = formatSymbolContextResult({ ok: true, target: "config.load", symbol: { filename: "src/config.py", start_line: 10, end_line: 20, kind: "function", qualified_name: "config.load", language: "python" }, parents: [], children: [], siblings: [], module_symbols: [], chunks: [] });

  assert.match(formatted.text, /Target: src\/config.py:10-20 function config.load/);
  assert.match(formatted.text, /Parents:\n  \(none\)/);
  assert.match(formatted.text, /Chunks:\n  \(none\)/);
});

test("formatRepoMapResults bounds nodes and includes next step", () => {
  const formatted = formatRepoMapResults({
    ok: true,
    operation: "repo_map",
    target: "src",
    nodes: Array.from({ length: 10 }, (_, index) => ({ path: `src/mod${index}.py`, node_kind: "module", file_count: 1, symbol_count: index, test_count: 0, key_symbols: [], metadata: { confidence: 0.5 } })),
  });
  assert.equal(formatted.summary.displayedResults, 8);
  assert.equal(formatted.summary.omittedResults, 2);
  assert.match(formatted.text, /repo_map: src/);
  assert.match(formatted.text, /Next: use read\/symbol tools/);
});

test("formatFindTestsResults renders command and failure payload", () => {
  const formatted = formatFindTestsResults({ ok: true, operation: "find_tests", targets: ["src/config.py"], results: [{ test_file: "tests/test_config.py", target_file: "src/config.py", score: 0.8, confidence: 0.7, evidence: ["name_overlap"], recommended_command: "uv run pytest tests/test_config.py" }] });
  assert.match(formatted.text, /uv run pytest tests\/test_config.py/);
  assert.match(formatFindTestsResults({ ok: false, error: "bad" }).text, /find_tests failed: bad/);
});

test("formatSimilarCodeResults clips snippets and preserves summary counts", () => {
  const formatted = formatSimilarCodeResults({ ok: true, operation: "find_similar_code", query: "dispatch", results: [{ filename: "src/cli.py", start_line: 1, end_line: 20, score: 0.9, similarity: { lexical: 0.8 }, risk: "parallel_command_handler", code: "x".repeat(2_000) }] });
  assert.equal(formatted.summary.truncatedSnippets, 1);
  assert.match(formatted.text, /Found 1 similar-code hit; showing 1\./);
  assert.match(formatted.text, /parallel_command_handler/);
  assert.match(formatted.text, /Next: inspect similar ranges/);
});

test("formatStatusCommand summarizes status without raw JSON dump", () => {
  const text = formatStatusCommand({ ok: true, repo: "/repo", effective_backend: "cocoindex", requested_backend: "auto", counts: { files: 2, chunks: 3, symbols: 0, call_edges: 0 }, live: { running: false }, setup: { summary: { errors: 0, warnings: 1 } }, performance: { durations_ms: { last: 12, average: 9, max: 20 } } });
  assert.match(text, /pi-code-index status: ok backend=cocoindex requested=auto/);
  assert.match(text, /indexed: files=2 chunks=3 symbols=0 graph_edges=0/);
  assert.match(text, /setup: errors=0 warnings=1/);
  assert.doesNotMatch(text, /\{\n|"counts"/);
});

test("formatDoctorCommand summarizes failing checks", () => {
  const text = formatDoctorCommand({ ok: false, repo: "/repo", effective_backend: "cocoindex", setup: { summary: { errors: 1, warnings: 0, ok: 3 }, checks: [{ id: "postgres.url", ok: false, message: "invalid URL" }] } });
  assert.match(text, /doctor: needs attention/);
  assert.match(text, /postgres\.url: invalid URL/);
  assert.doesNotMatch(text, /\{\n|"checks"/);
});

test("formatReviewContextResults shows ordered sections and commands", () => {
  const formatted = formatReviewContextResults({ ok: true, operation: "review_context", targets: ["index.ts"], summary: { risk_level: "medium" }, sections: [{ section: "architecture", items: [{ path: "index.ts" }] }, { section: "commands", items: [{ command: "npm run typecheck" }] }], recommended_commands: ["npm run typecheck"] });
  assert.match(formatted.text, /review_context: 1 target/);
  assert.match(formatted.text, /architecture:/);
  assert.match(formatted.text, /npm run typecheck/);
});
