import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "typebox";

const MAX_TEXT_BYTES = 12_000;
const MAX_DISPLAY_RESULTS = 8;
const MAX_CODE_CHARS = 700;
const EXTENSION_DIR = dirname(fileURLToPath(import.meta.url));

const CodeSearchParams = Type.Object({
  query: Type.String({ description: "Natural-language or semantic query to search in the current repository." }),
  top_k: Type.Optional(Type.Number({ description: "Maximum number of results to return. Default: 8.", minimum: 1, maximum: 50 })),
  refresh: Type.Optional(Type.Boolean({ description: "Refresh the local index before searching. Default: false." })),
});

const SymbolSearchParams = Type.Object({
  query: Type.String({ description: "Symbol name or intent to search for." }),
  top_k: Type.Optional(Type.Number({ description: "Maximum number of symbols. Default: 8.", minimum: 1, maximum: 50 })),
  kind: Type.Optional(Type.Union([Type.Literal("function"), Type.Literal("class"), Type.Literal("method"), Type.Literal("module")])),
  language: Type.Optional(Type.String({ description: "Symbol language filter. First release supports python." })),
  refresh: Type.Optional(Type.Boolean({ description: "Refresh before searching. Default: false." })),
});

const SymbolDefinitionParams = Type.Object({
  target: Type.String({ description: "symbol_id, qualified name, name, or file:line[:column]." }),
  refresh: Type.Optional(Type.Boolean({ description: "Refresh before lookup. Default: false." })),
});

const SymbolContextParams = Type.Object({
  target: Type.String({ description: "symbol_id, qualified name, name, or file:line[:column]." }),
  depth: Type.Optional(Type.Number({ description: "Context depth. Default: 1.", minimum: 0, maximum: 5 })),
  refresh: Type.Optional(Type.Boolean({ description: "Refresh before lookup. Default: false." })),
});

const GraphNavParams = Type.Object({
  target: Type.String({ description: "symbol_id, qualified name, name, or file:line[:column]." }),
  depth: Type.Optional(Type.Number({ description: "Traversal depth. Default: 1, max: 5.", minimum: 1, maximum: 5 })),
  top_k: Type.Optional(Type.Number({ description: "Maximum graph results. Default: 20.", minimum: 1, maximum: 100 })),
  include_indirect: Type.Optional(Type.Boolean({ description: "Include indirect callers/callees. Default: false." })),
  refresh: Type.Optional(Type.Boolean({ description: "Refresh before lookup. Default: false." })),
});

const ImpactAnalysisParams = Type.Object({
  target: Type.String({ description: "symbol_id, qualified name, name, file:line[:column], or repo-relative file path." }),
  depth: Type.Optional(Type.Number({ description: "Traversal depth. Default: 2, max: 5.", minimum: 1, maximum: 5 })),
  top_k: Type.Optional(Type.Number({ description: "Maximum impact results. Default: 50.", minimum: 1, maximum: 200 })),
  include_tests: Type.Optional(Type.Boolean({ description: "Include affected test hints. Default: true." })),
  include_files: Type.Optional(Type.Boolean({ description: "Include affected files. Default: true." })),
  refresh: Type.Optional(Type.Boolean({ description: "Refresh before lookup. Default: false." })),
});

const RepoMapParams = Type.Object({
  target: Type.Optional(Type.String()),
  depth: Type.Optional(Type.Number({ minimum: 0, maximum: 5 })),
  include_symbols: Type.Optional(Type.Boolean()),
  include_tests: Type.Optional(Type.Boolean()),
  refresh: Type.Optional(Type.Boolean()),
});
const FindTestsParams = Type.Object({
  target: Type.Union([Type.String(), Type.Array(Type.String(), { minItems: 1 })]),
  top_k: Type.Optional(Type.Number({ minimum: 1, maximum: 100 })),
  include_indirect: Type.Optional(Type.Boolean()),
  refresh: Type.Optional(Type.Boolean()),
});
const SimilarCodeParams = Type.Object({
  target: Type.Optional(Type.String()),
  query: Type.Optional(Type.String()),
  top_k: Type.Optional(Type.Number({ minimum: 1, maximum: 100 })),
  mode: Type.Optional(Type.Union([Type.Literal("semantic"), Type.Literal("hybrid")])),
  scope: Type.Optional(Type.Union([Type.Literal("chunks"), Type.Literal("symbols"), Type.Literal("files")])),
  exclude_self: Type.Optional(Type.Boolean()),
  refresh: Type.Optional(Type.Boolean()),
});
const ReviewContextParams = Type.Object({
  targets: Type.Array(Type.String(), { minItems: 1 }),
  top_k: Type.Optional(Type.Number({ minimum: 1, maximum: 200 })),
  include_map: Type.Optional(Type.Boolean()),
  include_tests: Type.Optional(Type.Boolean()),
  include_similar: Type.Optional(Type.Boolean()),
  include_impact: Type.Optional(Type.Boolean()),
  refresh: Type.Optional(Type.Boolean()),
});

type CodeSearchResult = {
  score: number;
  filename: string;
  start_line: number;
  end_line: number;
  code: string;
  result_id?: string;
  metadata?: Record<string, unknown>;
};

type SearchPayload = {
  query: string;
  top_k: number;
  refresh: boolean;
  repo: string;
  results: CodeSearchResult[];
  warning?: string;
  error?: string;
  schema_version?: number;
  pipeline_version?: string;
  compatibility_mode?: string;
  ranking_profile?: string;
  truncated?: boolean;
  truncation?: Record<string, unknown>;
};

function clip(text: string, max = MAX_CODE_CHARS): { text: string; truncated: boolean } {
  if (text.length <= max) return { text, truncated: false };
  return { text: `${text.slice(0, max)}\n... [snippet truncated; open/read file for more]`, truncated: true };
}

export type FormatSummary = {
  totalResults: number;
  displayedResults: number;
  omittedResults: number;
  truncatedSnippets: number;
  truncatedText: boolean;
};

export function formatResults(payload: SearchPayload): { text: string; summary: FormatSummary } {
  const totalResults = payload.results?.length ?? 0;
  const summary: FormatSummary = {
    totalResults,
    displayedResults: 0,
    omittedResults: 0,
    truncatedSnippets: 0,
    truncatedText: false,
  };

  if (payload.error) return { text: `code_search failed: ${payload.error}`, summary };

  const lines = [`code_search: ${payload.query}`];
  if (payload.warning) lines.push(`Warning: ${payload.warning}`);
  if (!totalResults) {
    lines.push("No indexed matches found. Try `pi-code-index refresh` or broaden the query.");
    return { text: lines.join("\n"), summary };
  }

  const displayResults = payload.results.slice(0, MAX_DISPLAY_RESULTS);
  summary.displayedResults = displayResults.length;
  summary.omittedResults = Math.max(0, totalResults - displayResults.length);

  lines.push(`Found ${totalResults} match${totalResults === 1 ? "" : "es"}; showing ${displayResults.length}.`);

  for (const [idx, result] of displayResults.entries()) {
    const clipped = clip(result.code);
    if (clipped.truncated) summary.truncatedSnippets += 1;
    lines.push(
      `\n${idx + 1}. ${result.filename}:${result.start_line}-${result.end_line} score=${result.score.toFixed(3)}`,
      "```",
      clipped.text,
      "```",
    );
  }

  if (summary.omittedResults > 0) {
    lines.push(`\n... ${summary.omittedResults} more result${summary.omittedResults === 1 ? "" : "s"} omitted from display. Full structured results are in details.`);
  }
  lines.push("Next: use `read` or open the listed file ranges before editing or answering in detail.");

  let text = lines.join("\n");
  if (Buffer.byteLength(text, "utf8") > MAX_TEXT_BYTES) {
    text = `${text.slice(0, MAX_TEXT_BYTES)}\n... [code_search display truncated; full structured results are in details]`;
    summary.truncatedText = true;
  }
  return { text, summary };
}
type SymbolItem = {
  score?: number;
  symbol_id?: string;
  name?: string;
  qualified_name?: string;
  kind?: string;
  language?: string;
  filename?: string;
  start_line?: number;
  end_line?: number;
  signature?: string | null;
  docstring?: string | null;
  code?: string | null;
  metadata?: Record<string, unknown>;
};

type SymbolPayload = {
  ok?: boolean;
  error?: string;
  warning?: string | null;
  query?: string;
  target?: string;
  results?: SymbolItem[];
  definition?: SymbolItem | null;
  matches?: SymbolItem[];
  symbol?: SymbolItem | null;
  parents?: SymbolItem[];
  children?: SymbolItem[];
  siblings?: SymbolItem[];
  module_symbols?: SymbolItem[];
  chunks?: Array<Record<string, unknown>>;
};

type GraphResult = {
  relationship?: string;
  distance?: number;
  score?: number;
  path_confidence?: number;
  symbol?: SymbolItem;
  paths?: Array<{ callsite?: Record<string, unknown>; confidence?: number }>;
};

type GraphPayload = Omit<SymbolPayload, "results"> & {
  operation?: "find_callers" | "find_callees" | "impact_analysis";
  available?: boolean;
  unsupported?: boolean;
  status?: string;
  message?: string;
  results?: GraphResult[];
  affected_symbols?: GraphResult[];
  affected_files?: Array<Record<string, unknown>>;
  affected_tests?: Array<Record<string, unknown>>;
  summary?: Record<string, unknown>;
};

type ContextPayload = {
  ok?: boolean;
  error?: string;
  warning?: string | null;
  operation?: "repo_map" | "find_tests" | "find_similar_code" | "review_context";
  target?: unknown;
  targets?: string[];
  query?: string | null;
  nodes?: Array<Record<string, any>>;
  results?: Array<Record<string, any>>;
  sections?: Array<{ section?: string; items?: Array<Record<string, any>> }>;
  summary?: Record<string, unknown>;
  recommended_commands?: string[];
};

function symbolLine(item: SymbolItem, includeScore = true): string {
  const score = includeScore && typeof item.score === "number" ? ` score=${item.score.toFixed(3)}` : "";
  const lang = item.language ?? (item.metadata?.language as string | undefined) ?? "unknown";
  return `${item.filename ?? "<unknown>"}:${item.start_line ?? "?"}-${item.end_line ?? "?"} ${item.kind ?? "symbol"} ${item.qualified_name ?? item.name ?? "<unknown>"} language=${lang}${score}`;
}

export function formatSymbolSearchResults(payload: SymbolPayload): { text: string; summary: FormatSummary } {
  const totalResults = payload.results?.length ?? 0;
  const summary: FormatSummary = { totalResults, displayedResults: 0, omittedResults: 0, truncatedSnippets: 0, truncatedText: false };
  if (payload.error || payload.ok === false) return { text: `symbol_search failed: ${payload.error ?? "unknown error"}`, summary };
  const lines = [`symbol_search: ${payload.query ?? ""}`];
  if (payload.warning) lines.push(`Warning: ${payload.warning}`);
  if (!totalResults) {
    lines.push("No indexed symbols found. Try `code_search` or refresh with enable_symbols=true.");
    return { text: lines.join("\n"), summary };
  }
  const displayResults = payload.results!.slice(0, MAX_DISPLAY_RESULTS);
  summary.displayedResults = displayResults.length;
  summary.omittedResults = Math.max(0, totalResults - displayResults.length);
  lines.push(`Found ${totalResults} symbol${totalResults === 1 ? "" : "s"}; showing ${displayResults.length}.`);
  for (const [idx, result] of displayResults.entries()) {
    lines.push(`\n${idx + 1}. ${symbolLine(result)}`);
    if (result.signature) lines.push(`   ${result.signature}`);
  }
  if (summary.omittedResults) lines.push(`\n... ${summary.omittedResults} more symbol${summary.omittedResults === 1 ? "" : "s"} omitted from display. Full structured results are in details.`);
  lines.push("Next: use `read` or definition lookup before editing or answering in detail.");
  let text = lines.join("\n");
  if (Buffer.byteLength(text, "utf8") > MAX_TEXT_BYTES) {
    text = `${text.slice(0, MAX_TEXT_BYTES)}\n... [symbol_search display truncated; full structured results are in details]`;
    summary.truncatedText = true;
  }
  return { text, summary };
}

export function formatSymbolDefinitionResult(payload: SymbolPayload): { text: string; summary: FormatSummary } {
  const matches = payload.matches ?? [];
  const summary: FormatSummary = { totalResults: payload.definition ? 1 : matches.length, displayedResults: payload.definition ? 1 : Math.min(matches.length, MAX_DISPLAY_RESULTS), omittedResults: Math.max(0, matches.length - MAX_DISPLAY_RESULTS), truncatedSnippets: 0, truncatedText: false };
  if (payload.error || payload.ok === false) return { text: `symbol_definition failed: ${payload.error ?? "unknown error"}`, summary };
  const lines: string[] = [];
  if (payload.definition) {
    const def = payload.definition;
    lines.push(`symbol_definition: ${def.qualified_name ?? def.name ?? payload.target} -> ${symbolLine(def, false)}`);
    if (def.signature) lines.push(def.signature);
    if (def.code) lines.push("```", clip(def.code).text, "```");
  } else {
    lines.push(`symbol_definition: ${payload.target ?? ""}`);
    if (payload.warning) lines.push(`Warning: ${payload.warning}`);
    const shown = matches.slice(0, MAX_DISPLAY_RESULTS);
    for (const [idx, match] of shown.entries()) lines.push(`${idx + 1}. ${symbolLine(match)}`);
    if (!matches.length) lines.push("No definition found. Try `code_search`.");
    else if (summary.omittedResults > 0) lines.push(`\n... ${summary.omittedResults} more candidate${summary.omittedResults === 1 ? "" : "s"} omitted from display. Full matches are in details.`);
  }
  return { text: lines.join("\n"), summary };
}

export function formatSymbolContextResult(payload: SymbolPayload): { text: string; summary: FormatSummary } {
  const total = (payload.children?.length ?? 0) + (payload.siblings?.length ?? 0) + (payload.parents?.length ?? 0) + (payload.module_symbols?.length ?? 0);
  const summary: FormatSummary = { totalResults: total, displayedResults: total, omittedResults: 0, truncatedSnippets: 0, truncatedText: false };
  if (payload.error || payload.ok === false) return { text: `symbol_context failed: ${payload.error ?? "unknown error"}`, summary };
  const lines = [`symbol_context: ${payload.target ?? payload.symbol?.qualified_name ?? ""}`];
  if (payload.warning) lines.push(`Warning: ${payload.warning}`);
  if (payload.symbol) lines.push(`Target: ${symbolLine(payload.symbol, false)}`);
  const section = (name: string, items?: SymbolItem[]) => {
    lines.push(`${name}:`);
    if (!items?.length) lines.push("  (none)");
    else for (const item of items.slice(0, 20)) lines.push(`  - ${symbolLine(item, false)}`);
  };
  section("Parents", payload.parents);
  section("Children", payload.children);
  section("Siblings", payload.siblings);
  section("Module symbols", payload.module_symbols);
  lines.push("Chunks:");
  if (!payload.chunks?.length) lines.push("  (none)");
  else for (const chunk of payload.chunks.slice(0, 20)) lines.push(`  - ${chunk.filename}:${chunk.start_line}-${chunk.end_line} ${chunk.chunk_kind ?? "chunk"}`);
  return { text: lines.join("\n"), summary };
}

export function formatGraphResult(payload: GraphPayload): { text: string; summary: FormatSummary } {
  const results = payload.results ?? [];
  const summary: FormatSummary = { totalResults: results.length, displayedResults: Math.min(results.length, MAX_DISPLAY_RESULTS), omittedResults: Math.max(0, results.length - MAX_DISPLAY_RESULTS), truncatedSnippets: 0, truncatedText: false };
  if (payload.error || payload.ok === false) return { text: `${payload.operation ?? "graph"} failed: ${payload.error ?? "unknown error"}`, summary };
  const lines = [`${payload.operation ?? "graph"}: ${payload.target ?? ""}`];
  if (payload.unsupported || payload.available === false || payload.status === "unsupported") lines.push(payload.message ?? "Call graph unsupported on this backend. Enable CocoIndex/Postgres with references.");
  else if (payload.warning) lines.push(`Warning: ${payload.warning}`);
  else if (!results.length) lines.push("No call graph results found. Ensure CocoIndex/Postgres is enabled with enable_symbols and enable_references.");
  for (const [idx, result] of results.slice(0, MAX_DISPLAY_RESULTS).entries()) {
    const sym = result.symbol ?? {};
    lines.push(`\n${idx + 1}. ${symbolLine(sym, false)} distance=${result.distance ?? "?"} score=${(result.score ?? 0).toFixed(3)} confidence=${(result.path_confidence ?? 0).toFixed(3)}`);
    const callsite = result.paths?.[0]?.callsite;
    if (callsite) lines.push(`   callsite: ${String(callsite.filename ?? callsite.path ?? "?")}:${String(callsite.line ?? "?")}:${String(callsite.column ?? "?")}`);
  }
  if (summary.omittedResults) lines.push(`\n... ${summary.omittedResults} more graph result${summary.omittedResults === 1 ? "" : "s"} omitted. Full structured results are in details.`);
  lines.push("Next: use `symbol_definition` or `read` to inspect exact source before editing.");
  return { text: lines.join("\n"), summary };
}

export function formatImpactResult(payload: GraphPayload): { text: string; summary: FormatSummary } {
  const affectedSymbols = payload.affected_symbols ?? [];
  const files = payload.affected_files ?? [];
  const tests = payload.affected_tests ?? [];
  const total = affectedSymbols.length + files.length + tests.length;
  const summary: FormatSummary = { totalResults: total, displayedResults: Math.min(total, MAX_DISPLAY_RESULTS * 3), omittedResults: 0, truncatedSnippets: 0, truncatedText: false };
  if (payload.error || payload.ok === false) return { text: `impact_analysis failed: ${payload.error ?? "unknown error"}`, summary };
  const lines = [`impact_analysis: ${payload.target ?? ""}`];
  if (payload.warning) lines.push(`Warning: ${payload.warning}`);
  if (payload.unsupported || payload.available === false || payload.status === "unsupported") lines.push(payload.message ?? "Impact analysis unsupported on this backend. Enable CocoIndex/Postgres with references.");
  if (payload.summary) lines.push(`Summary: ${JSON.stringify(payload.summary)}`);
  lines.push("Affected symbols:");
  for (const item of affectedSymbols.slice(0, MAX_DISPLAY_RESULTS)) lines.push(`  - ${symbolLine(item.symbol ?? {}, false)} distance=${item.distance ?? "?"} score=${(item.score ?? 0).toFixed(3)}`);
  if (!affectedSymbols.length) lines.push("  (none)");
  lines.push("Affected files:");
  for (const file of files.slice(0, MAX_DISPLAY_RESULTS)) lines.push(`  - ${String(file.filename)} score=${Number(file.score ?? 0).toFixed(3)} reasons=${JSON.stringify(file.reasons ?? [])}`);
  if (!files.length) lines.push("  (none)");
  lines.push("Affected tests:");
  for (const test of tests.slice(0, MAX_DISPLAY_RESULTS)) lines.push(`  - ${String(test.filename)} score=${Number(test.score ?? 0).toFixed(3)} reason=${String(test.reason ?? "")}`);
  if (!tests.length) lines.push("  (none)");
  lines.push("Next: inspect listed files/tests before editing.");
  return { text: lines.join("\n"), summary };
}


function finishContext(lines: string[], summary: FormatSummary): { text: string; summary: FormatSummary } {
  let text = lines.join("\n");
  if (Buffer.byteLength(text, "utf8") > MAX_TEXT_BYTES) {
    text = `${text.slice(0, MAX_TEXT_BYTES)}\n... [context display truncated; full structured results are in details]`;
    summary.truncatedText = true;
  }
  return { text, summary };
}

export function formatRepoMapResults(payload: ContextPayload): { text: string; summary: FormatSummary } {
  const nodes = payload.nodes ?? [];
  const summary: FormatSummary = { totalResults: nodes.length, displayedResults: Math.min(nodes.length, MAX_DISPLAY_RESULTS), omittedResults: Math.max(0, nodes.length - MAX_DISPLAY_RESULTS), truncatedSnippets: 0, truncatedText: false };
  if (payload.error || payload.ok === false) return { text: `repo_map failed: ${payload.error ?? "unknown error"}`, summary };
  const lines = [`repo_map: ${String(payload.target ?? "repo-root")}`];
  if (payload.warning) lines.push(`Warning: ${payload.warning}`);
  lines.push(`Nodes: ${nodes.length}; showing ${summary.displayedResults}.`);
  for (const n of nodes.slice(0, MAX_DISPLAY_RESULTS)) lines.push(`- ${n.path || "."} ${n.node_kind} files=${n.file_count ?? 0} symbols=${n.symbol_count ?? 0} tests=${n.test_count ?? 0} confidence=${Number(n.metadata?.confidence ?? 0).toFixed(2)} symbols=${(n.key_symbols ?? []).slice(0,3).map((s: any) => s.qualified_name ?? s.name).join(",")}`);
  if (summary.omittedResults) lines.push(`... ${summary.omittedResults} more node(s) omitted.`);
  lines.push("Next: use read/symbol tools for listed files or repo_map with a narrower target before editing.");
  return finishContext(lines, summary);
}

export function formatFindTestsResults(payload: ContextPayload): { text: string; summary: FormatSummary } {
  const results = payload.results ?? [];
  const summary: FormatSummary = { totalResults: results.length, displayedResults: Math.min(results.length, MAX_DISPLAY_RESULTS), omittedResults: Math.max(0, results.length - MAX_DISPLAY_RESULTS), truncatedSnippets: 0, truncatedText: false };
  if (payload.error || payload.ok === false) return { text: `find_tests failed: ${payload.error ?? "unknown error"}`, summary };
  const lines = [`find_tests: ${(payload.targets ?? [String(payload.target ?? "")]).join(", ")}`];
  if (payload.warning) lines.push(`Warning: ${payload.warning}`);
  for (const r of results.slice(0, MAX_DISPLAY_RESULTS)) lines.push(`- ${r.test_file}${r.test_symbol ? `::${r.test_symbol}` : ""} target=${r.target_file ?? ""} score=${Number(r.score ?? 0).toFixed(3)} confidence=${Number(r.confidence ?? 0).toFixed(3)} evidence=${JSON.stringify(r.evidence ?? [])} command=${r.recommended_command ?? ""}`);
  if (!results.length) lines.push("No likely tests found.");
  if (summary.omittedResults) lines.push(`... ${summary.omittedResults} more test candidate(s) omitted.`);
  lines.push("Next: run listed tests or broaden target.");
  return finishContext(lines, summary);
}

export function formatSimilarCodeResults(payload: ContextPayload): { text: string; summary: FormatSummary } {
  const results = payload.results ?? [];
  const summary: FormatSummary = { totalResults: results.length, displayedResults: Math.min(results.length, MAX_DISPLAY_RESULTS), omittedResults: Math.max(0, results.length - MAX_DISPLAY_RESULTS), truncatedSnippets: 0, truncatedText: false };
  if (payload.error || payload.ok === false) return { text: `find_similar_code failed: ${payload.error ?? "unknown error"}`, summary };
  const lines = [`find_similar_code: ${String(payload.target ?? payload.query ?? "")}`];
  if (payload.warning) lines.push(`Warning: ${payload.warning}`);
  if (results.length) lines.push(`Found ${results.length} similar-code hit${results.length === 1 ? "" : "s"}; showing ${summary.displayedResults}.`);
  for (const r of results.slice(0, MAX_DISPLAY_RESULTS)) { const c = clip(String(r.code ?? "")); if (c.truncated) summary.truncatedSnippets += 1; lines.push(`\n- ${r.filename}:${r.start_line}-${r.end_line} score=${Number(r.score ?? 0).toFixed(3)} sim=${JSON.stringify(r.similarity ?? {})} risk=${r.risk ?? ""}`, "```", c.text, "```"); }
  if (!results.length) lines.push("No similar code found.");
  if (summary.omittedResults) lines.push(`... ${summary.omittedResults} more similar candidate(s) omitted.`);
  lines.push("Next: inspect similar ranges before adding or changing duplicate logic.");
  return finishContext(lines, summary);
}

export function formatReviewContextResults(payload: ContextPayload): { text: string; summary: FormatSummary } {
  const sections = payload.sections ?? [];
  const total = sections.reduce((n, s) => n + (s.items?.length ?? 0), 0);
  const summary: FormatSummary = { totalResults: total, displayedResults: Math.min(total, MAX_DISPLAY_RESULTS * 2), omittedResults: 0, truncatedSnippets: 0, truncatedText: false };
  if (payload.error || payload.ok === false) return { text: `review_context failed: ${payload.error ?? "unknown error"}`, summary };
  const lines = [`review_context: ${(payload.targets?.length ?? 0)} target(s)`];
  if (payload.warning) lines.push(`Warning: ${payload.warning}`);
  lines.push(`Summary: ${JSON.stringify(payload.summary ?? {})}`);
  for (const section of sections) { lines.push(`${section.section}:`); for (const item of (section.items ?? []).slice(0, MAX_DISPLAY_RESULTS)) lines.push(`  - ${JSON.stringify(item).slice(0, 500)}`); if (!(section.items ?? []).length) lines.push("  (none)"); }
  if (payload.recommended_commands?.length) lines.push("Commands:", ...payload.recommended_commands.map(c => `  ${c}`));
  return finishContext(lines, summary);
}

export function formatStatusCommand(payload: Record<string, any>): string {
  const counts = payload.counts ?? {};
  const setup = payload.setup?.summary ?? {};
  const live = payload.live ?? {};
  const perf = payload.performance?.durations_ms ?? payload.daemon?.performance?.durations_ms ?? {};
  const backend = payload.effective_backend ?? payload.backend ?? "unknown";
  const requested = payload.requested_backend && payload.requested_backend !== backend ? ` requested=${payload.requested_backend}` : "";
  return [
    `pi-code-index status: ${payload.ok === false ? "error" : "ok"} backend=${backend}${requested}`,
    `repo: ${payload.repo ?? "<unknown>"}`,
    `indexed: files=${counts.files ?? payload.files ?? 0} chunks=${counts.chunks ?? payload.chunks ?? 0} symbols=${counts.symbols ?? 0} graph_edges=${counts.call_edges ?? 0}`,
    `live: ${live.running ? "running" : "stopped"}${live.stale ? ` stale=${live.stale_reason ?? true}` : ""}`,
    `setup: errors=${setup.errors ?? 0} warnings=${setup.warnings ?? 0}`,
    `latency_ms: last=${perf.last ?? "?"} avg=${perf.average ?? "?"} max=${perf.max ?? "?"}`,
    "Next: use `pi-code-index status --json` for full details or `/code-index-doctor` for setup checks.",
  ].join("\n");
}

export function formatDoctorCommand(payload: Record<string, any>): string {
  const setup = payload.setup ?? {};
  const summary = setup.summary ?? {};
  const checks = Array.isArray(setup.checks) ? setup.checks : [];
  const failing = checks.filter((check: any) => check?.ok === false).slice(0, 5);
  const lines = [
    `pi-code-index doctor: ${payload.ok === false || (summary.errors ?? 0) > 0 ? "needs attention" : "ok"}`,
    `repo: ${payload.repo ?? "<unknown>"}`,
    `backend: ${payload.effective_backend ?? payload.backend?.effective_backend ?? payload.backend?.backend ?? "unknown"}`,
    `checks: errors=${summary.errors ?? 0} warnings=${summary.warnings ?? 0} ok=${summary.ok ?? 0}`,
  ];
  for (const check of failing) lines.push(`- ${check.id ?? check.name ?? "check"}: ${check.message ?? check.error ?? "failed"}`);
  if (failing.length < checks.filter((check: any) => check?.ok === false).length) lines.push("- additional failures omitted; run `pi-code-index doctor --json`.");
  lines.push("Next: fix listed errors or run `runtime/postgres/podman-pgvector.sh` for CocoIndex/Postgres setup.");
  return lines.join("\n");
}

async function runCli(pi: ExtensionAPI, args: string[], cwd: string): Promise<SearchPayload | Record<string, unknown>> {
  const result = await pi.exec("uv", ["run", "--project", EXTENSION_DIR, "pi-code-index", ...args], { cwd, timeout: 120_000 });
  const raw = (result.stdout || result.stderr || "").trim();
  try {
    return JSON.parse(raw);
  } catch (error) {
    if (result.code !== 0) throw new Error(raw || `uv run pi-code-index exited with code ${result.code}`);
    throw new Error(`pi-code-index returned non-JSON output: ${raw.slice(0, 500)}`);
  }
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "code_search",
    label: "Code Search",
    description:
      "Semantic search over the current repository. Use for conceptual questions like where behavior is implemented, then use read to inspect matched files.",
    parameters: CodeSearchParams,
    async execute(_toolCallId: string, params: { query: string; top_k?: number; refresh?: boolean }, _signal: AbortSignal, _onUpdate: unknown, ctx: { cwd: string }) {
      const topK = Math.max(1, Math.min(Number(params.top_k ?? 8), 50));
      const refresh = Boolean(params.refresh ?? false);
      const args = ["search", "--json", "--top-k", String(topK)];
      if (refresh) args.push("--refresh");
      args.push(params.query);

      try {
        const payload = (await runCli(pi, args, ctx.cwd)) as SearchPayload;
        const formatted = formatResults(payload);
        return {
          content: [{ type: "text" as const, text: formatted.text }],
          details: {
            ...payload,
            display: formatted.summary,
            cli_json: payload,
          },
        };
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        const hint = message.includes("ENOENT")
          ? "pi-code-index executable was not found. Install with `uv tool install -e /home/fractiunate/.pi/agent/extensions/pi-code-index` or run via `uv run pi-code-index`."
          : message;
        return {
          content: [{ type: "text" as const, text: `code_search failed: ${hint}` }],
          details: { query: params.query, top_k: topK, refresh, error: hint },
        };
      }
    },
  });

  pi.registerTool({
    name: "symbol_search",
    label: "Symbol Search",
    description: "Find functions, classes, methods, and modules by name or intent.",
    parameters: SymbolSearchParams,
    async execute(_toolCallId: string, params: { query: string; top_k?: number; kind?: string; language?: string; refresh?: boolean }, _signal: AbortSignal, _onUpdate: unknown, ctx: { cwd: string }) {
      const topK = Math.max(1, Math.min(Number(params.top_k ?? 8), 50));
      const args = ["symbols", "search", "--json", "--top-k", String(topK)];
      if (params.kind) args.push("--kind", params.kind);
      if (params.language) args.push("--language", params.language);
      if (params.refresh) args.push("--refresh");
      args.push(params.query);
      try {
        const payload = (await runCli(pi, args, ctx.cwd)) as SymbolPayload;
        const formatted = formatSymbolSearchResults(payload);
        return { content: [{ type: "text" as const, text: formatted.text }], details: { ...payload, display: formatted.summary, cli_json: payload } };
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return { content: [{ type: "text" as const, text: `symbol_search failed: ${message}` }], details: { query: params.query, top_k: topK, error: message } };
      }
    },
  });

  pi.registerTool({
    name: "symbol_definition",
    label: "Symbol Definition",
    description: "Resolve a symbol_id, qualified name, name, or file:line[:column] to its definition.",
    parameters: SymbolDefinitionParams,
    async execute(_toolCallId: string, params: { target: string; refresh?: boolean }, _signal: AbortSignal, _onUpdate: unknown, ctx: { cwd: string }) {
      const args = ["symbols", "definition", "--json"];
      if (params.refresh) args.push("--refresh");
      args.push(params.target);
      try {
        const payload = (await runCli(pi, args, ctx.cwd)) as SymbolPayload;
        const formatted = formatSymbolDefinitionResult(payload);
        return { content: [{ type: "text" as const, text: formatted.text }], details: { ...payload, display: formatted.summary, cli_json: payload } };
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return { content: [{ type: "text" as const, text: `symbol_definition failed: ${message}` }], details: { target: params.target, error: message } };
      }
    },
  });

  pi.registerTool({
    name: "symbol_context",
    label: "Symbol Context",
    description: "Navigate around a symbol: parents, children, siblings, module symbols, and linked chunks.",
    parameters: SymbolContextParams,
    async execute(_toolCallId: string, params: { target: string; depth?: number; refresh?: boolean }, _signal: AbortSignal, _onUpdate: unknown, ctx: { cwd: string }) {
      const depth = Math.max(0, Math.min(Number(params.depth ?? 1), 5));
      const args = ["symbols", "context", "--json", "--depth", String(depth)];
      if (params.refresh) args.push("--refresh");
      args.push(params.target);
      try {
        const payload = (await runCli(pi, args, ctx.cwd)) as SymbolPayload;
        const formatted = formatSymbolContextResult(payload);
        return { content: [{ type: "text" as const, text: formatted.text }], details: { ...payload, display: formatted.summary, cli_json: payload } };
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return { content: [{ type: "text" as const, text: `symbol_context failed: ${message}` }], details: { target: params.target, depth, error: message } };
      }
    },
  });

  pi.registerTool({
    name: "find_callers",
    label: "Find Callers",
    description: "Find direct or indirect callers of a symbol using the indexed call graph.",
    parameters: GraphNavParams,
    async execute(_toolCallId: string, params: { target: string; depth?: number; top_k?: number; include_indirect?: boolean; refresh?: boolean }, _signal: AbortSignal, _onUpdate: unknown, ctx: { cwd: string }) {
      const topK = Math.max(1, Math.min(Number(params.top_k ?? 20), 100));
      const depth = Math.max(1, Math.min(Number(params.depth ?? 1), 5));
      const args = ["graph", "callers", "--json", "--top-k", String(topK), "--depth", String(depth)];
      if (params.include_indirect) args.push("--include-indirect");
      if (params.refresh) args.push("--refresh");
      args.push(params.target);
      try {
        const payload = (await runCli(pi, args, ctx.cwd)) as GraphPayload;
        const formatted = formatGraphResult(payload);
        return { content: [{ type: "text" as const, text: formatted.text }], details: { ...payload, display: formatted.summary, cli_json: payload } };
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return { content: [{ type: "text" as const, text: `find_callers failed: ${message}` }], details: { target: params.target, error: message } };
      }
    },
  });

  pi.registerTool({
    name: "find_callees",
    label: "Find Callees",
    description: "Find direct or indirect callees from a symbol using the indexed call graph.",
    parameters: GraphNavParams,
    async execute(_toolCallId: string, params: { target: string; depth?: number; top_k?: number; include_indirect?: boolean; refresh?: boolean }, _signal: AbortSignal, _onUpdate: unknown, ctx: { cwd: string }) {
      const topK = Math.max(1, Math.min(Number(params.top_k ?? 20), 100));
      const depth = Math.max(1, Math.min(Number(params.depth ?? 1), 5));
      const args = ["graph", "callees", "--json", "--top-k", String(topK), "--depth", String(depth)];
      if (params.include_indirect) args.push("--include-indirect");
      if (params.refresh) args.push("--refresh");
      args.push(params.target);
      try {
        const payload = (await runCli(pi, args, ctx.cwd)) as GraphPayload;
        const formatted = formatGraphResult(payload);
        return { content: [{ type: "text" as const, text: formatted.text }], details: { ...payload, display: formatted.summary, cli_json: payload } };
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return { content: [{ type: "text" as const, text: `find_callees failed: ${message}` }], details: { target: params.target, error: message } };
      }
    },
  });

  pi.registerTool({
    name: "impact_analysis",
    label: "Impact Analysis",
    description: "Estimate blast radius for a symbol using callers, callees, affected files, and test hints.",
    parameters: ImpactAnalysisParams,
    async execute(_toolCallId: string, params: { target: string; depth?: number; top_k?: number; include_tests?: boolean; include_files?: boolean; refresh?: boolean }, _signal: AbortSignal, _onUpdate: unknown, ctx: { cwd: string }) {
      const topK = Math.max(1, Math.min(Number(params.top_k ?? 50), 200));
      const depth = Math.max(1, Math.min(Number(params.depth ?? 2), 5));
      const args = ["graph", "impact", "--json", "--top-k", String(topK), "--depth", String(depth), params.include_tests === false ? "--no-include-tests" : "--include-tests", params.include_files === false ? "--no-include-files" : "--include-files"];
      if (params.refresh) args.push("--refresh");
      args.push(params.target);
      try {
        const payload = (await runCli(pi, args, ctx.cwd)) as GraphPayload;
        const formatted = formatImpactResult(payload);
        return { content: [{ type: "text" as const, text: formatted.text }], details: { ...payload, display: formatted.summary, cli_json: payload } };
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return { content: [{ type: "text" as const, text: `impact_analysis failed: ${message}` }], details: { target: params.target, error: message } };
      }
    },
  });

  pi.registerTool({
    name: "repo_map",
    label: "Repo Map",
    description: "Return a compact architecture map for the current repository or target subtree.",
    parameters: RepoMapParams,
    async execute(_id: string, params: { target?: string; depth?: number; include_symbols?: boolean; include_tests?: boolean; refresh?: boolean }, _signal: AbortSignal, _onUpdate: unknown, ctx: { cwd: string }) {
      const depth = Math.max(0, Math.min(Number(params.depth ?? 2), 5));
      const args = ["context", "repo-map", "--json", "--depth", String(depth), params.include_symbols === false ? "--no-include-symbols" : "--include-symbols", params.include_tests ? "--include-tests" : "--no-include-tests"];
      if (params.refresh) args.push("--refresh");
      if (params.target) args.push("--target", params.target);
      try { const payload = (await runCli(pi, args, ctx.cwd)) as ContextPayload; const formatted = formatRepoMapResults(payload); return { content: [{ type: "text" as const, text: formatted.text }], details: { ...payload, display: formatted.summary, summary: formatted.summary, cli_json: payload } }; }
      catch (error) { const message = error instanceof Error ? error.message : String(error); return { content: [{ type: "text" as const, text: `repo_map failed: ${message}` }], details: { error: message } }; }
    },
  });

  pi.registerTool({
    name: "find_tests",
    label: "Find Tests",
    description: "Find likely tests for files, symbols, or changed targets.",
    parameters: FindTestsParams,
    async execute(_id: string, params: { target: string | string[]; top_k?: number; include_indirect?: boolean; refresh?: boolean }, _signal: AbortSignal, _onUpdate: unknown, ctx: { cwd: string }) {
      const topK = Math.max(1, Math.min(Number(params.top_k ?? 20), 100));
      const targets = Array.isArray(params.target) ? params.target : [params.target];
      const args = ["context", "tests", "--json", "--top-k", String(topK)];
      if (params.include_indirect) args.push("--include-indirect");
      if (params.refresh) args.push("--refresh");
      args.push(...targets);
      try { const payload = (await runCli(pi, args, ctx.cwd)) as ContextPayload; const formatted = formatFindTestsResults(payload); return { content: [{ type: "text" as const, text: formatted.text }], details: { ...payload, display: formatted.summary, summary: formatted.summary, cli_json: payload } }; }
      catch (error) { const message = error instanceof Error ? error.message : String(error); return { content: [{ type: "text" as const, text: `find_tests failed: ${message}` }], details: { error: message } }; }
    },
  });

  pi.registerTool({
    name: "find_similar_code",
    label: "Find Similar Code",
    description: "Find similar chunks, symbols, or files to detect duplicates and drift risk.",
    parameters: SimilarCodeParams,
    async execute(_id: string, params: { target?: string; query?: string; top_k?: number; mode?: "semantic" | "hybrid"; scope?: "chunks" | "symbols" | "files"; exclude_self?: boolean; refresh?: boolean }, _signal: AbortSignal, _onUpdate: unknown, ctx: { cwd: string }) {
      const topK = Math.max(1, Math.min(Number(params.top_k ?? 12), 100));
      const args = ["context", "similar", "--json", "--top-k", String(topK), "--mode", params.mode ?? "hybrid", "--scope", params.scope ?? "chunks", params.exclude_self === false ? "--no-exclude-self" : "--exclude-self"];
      if (params.query) args.push("--query", params.query);
      if (params.refresh) args.push("--refresh");
      if (params.target) args.push(params.target);
      try { const payload = (await runCli(pi, args, ctx.cwd)) as ContextPayload; const formatted = formatSimilarCodeResults(payload); return { content: [{ type: "text" as const, text: formatted.text }], details: { ...payload, display: formatted.summary, summary: formatted.summary, cli_json: payload } }; }
      catch (error) { const message = error instanceof Error ? error.message : String(error); return { content: [{ type: "text" as const, text: `find_similar_code failed: ${message}` }], details: { error: message } }; }
    },
  });

  pi.registerTool({
    name: "review_context",
    label: "Review Context",
    description: "Compose review-oriented map, tests, similar-code, risks, and validation commands for changed targets.",
    parameters: ReviewContextParams,
    async execute(_id: string, params: { targets: string[]; top_k?: number; include_map?: boolean; include_tests?: boolean; include_similar?: boolean; include_impact?: boolean; refresh?: boolean }, _signal: AbortSignal, _onUpdate: unknown, ctx: { cwd: string }) {
      const topK = Math.max(1, Math.min(Number(params.top_k ?? 30), 200));
      const args = ["context", "review", "--json", "--top-k", String(topK), params.include_map === false ? "--no-include-map" : "--include-map", params.include_tests === false ? "--no-include-tests" : "--include-tests", params.include_similar === false ? "--no-include-similar" : "--include-similar", params.include_impact === false ? "--no-include-impact" : "--include-impact"];
      if (params.refresh) args.push("--refresh");
      args.push(...params.targets);
      try { const payload = (await runCli(pi, args, ctx.cwd)) as ContextPayload; const formatted = formatReviewContextResults(payload); return { content: [{ type: "text" as const, text: formatted.text }], details: { ...payload, display: formatted.summary, summary: formatted.summary, cli_json: payload } }; }
      catch (error) { const message = error instanceof Error ? error.message : String(error); return { content: [{ type: "text" as const, text: `review_context failed: ${message}` }], details: { error: message } }; }
    },
  });

  type CommandCtx = { cwd: string; ui: { notify: (message: string, level: "info" | "success" | "error") => void } };

  pi.registerCommand("code-index-status", {
    description: "Show pi-code-index status for the current repository.",
    handler: async (_args: string, ctx: CommandCtx) => {
      try {
        const payload = await runCli(pi, ["status", "--json"], ctx.cwd);
        ctx.ui.notify(formatStatusCommand(payload as Record<string, any>), "info");
      } catch (error) {
        ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
      }
    },
  });

  pi.registerCommand("code-index-refresh", {
    description: "Refresh the pi-code-index index for the current repository.",
    handler: async (_args: string, ctx: CommandCtx) => {
      try {
        const payload = await runCli(pi, ["refresh", "--json"], ctx.cwd);
        ctx.ui.notify(JSON.stringify(payload, null, 2), "success");
      } catch (error) {
        ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
      }
    },
  });

  pi.registerCommand("code-index-stop", {
    description: "Stop the pi-code-index daemon if it is running.",
    handler: async (_args: string, ctx: CommandCtx) => {
      try {
        const payload = await runCli(pi, ["stop", "--json"], ctx.cwd);
        ctx.ui.notify(JSON.stringify(payload, null, 2), "success");
      } catch (error) {
        ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
      }
    },
  });

  pi.registerCommand("code-index-live-status", {
    description: "Show pi-code-index live watcher status.",
    handler: async (_args: string, ctx: CommandCtx) => {
      try {
        const payload = await runCli(pi, ["live", "status", "--json"], ctx.cwd);
        ctx.ui.notify(JSON.stringify(payload, null, 2), "info");
      } catch (error) {
        ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
      }
    },
  });

  pi.registerCommand("code-index-live-start", {
    description: "Start pi-code-index live indexing for the current repository.",
    handler: async (_args: string, ctx: CommandCtx) => {
      try {
        const payload = await runCli(pi, ["live", "start", "--json"], ctx.cwd);
        ctx.ui.notify(JSON.stringify(payload, null, 2), "success");
      } catch (error) {
        ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
      }
    },
  });

  pi.registerCommand("code-index-live-stop", {
    description: "Stop pi-code-index live indexing for the current repository.",
    handler: async (_args: string, ctx: CommandCtx) => {
      try {
        const payload = await runCli(pi, ["live", "stop", "--json"], ctx.cwd);
        ctx.ui.notify(JSON.stringify(payload, null, 2), "success");
      } catch (error) {
        ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
      }
    },
  });

  pi.registerCommand("code-index-doctor", {
    description: "Run pi-code-index setup and troubleshooting checks.",
    handler: async (_args: string, ctx: CommandCtx) => {
      try {
        const payload = await runCli(pi, ["doctor", "--json"], ctx.cwd);
        ctx.ui.notify(formatDoctorCommand(payload as Record<string, any>), "info");
      } catch (error) {
        ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
      }
    },
  });

  pi.on("before_agent_start", async (event: { systemPrompt: string }) => ({
    systemPrompt:
      event.systemPrompt +
      "\n\nCode search guidance: use `repo_map` for architecture orientation before broad edits, `find_tests` before choosing validation for a file/symbol/change, `find_similar_code` before adding repeated command/config patterns, and `review_context` before final review or handoff. Use `symbol_search`, `symbol_definition`, or `symbol_context` when looking for functions/classes/methods/modules by name or intent. Use `find_callers`, `find_callees`, or `impact_analysis` for caller/callee/blast-radius questions, then use `symbol_definition` or `read` to inspect exact source before editing. Use the `code_search` tool for broader semantic or conceptual repository questions. Large result sets may be compacted in the message; full structured results remain available in tool details.",
  }));
}
