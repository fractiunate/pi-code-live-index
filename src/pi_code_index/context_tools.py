from __future__ import annotations

import ast
import fnmatch
import re
from pathlib import Path
from typing import Any, Literal

from . import indexer as lexical
from .config import load_project_config

TEST_DIRS = {"test", "tests", "spec", "specs", "__tests__"}


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def is_test_path(path: str) -> bool:
    p = Path(path)
    parts = {part.lower() for part in p.parts}
    name = p.name.lower()
    return bool(parts & TEST_DIRS) or name.startswith("test_") and name.endswith(".py") or name.endswith("_test.py") or any(name.endswith(s) for s in (".test.ts", ".spec.ts", ".test.tsx", ".spec.tsx", ".test.js", ".spec.js", ".test.jsx", ".spec.jsx"))


SOURCE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".swift", ".scala", ".sh"}
CONFIG_EXTS = {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".conf"}
GENERATED_DIRS = {"node_modules", "vendor", ".venv", "dist", "build", ".next", "coverage", ".pytest_cache", "__pycache__"}
LOCKFILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "uv.lock", "poetry.lock", "pipfile.lock", "cargo.lock", "go.sum"}
DOC_NAMES = ("readme", "changelog", "contributing")


def content_role_for(path: str) -> Literal["source", "test", "docs", "config", "generated", "unknown"]:
    p = Path(path)
    parts = {part.lower() for part in p.parts}
    name = p.name.lower()
    suffix = p.suffix.lower()
    low = path.lower()
    if parts & GENERATED_DIRS or name in LOCKFILES or low.endswith((".min.js", ".min.css", ".map", ".pb.go", "_pb2.py", ".pb.cc", ".pb.h")):
        return "generated"
    if is_test_path(path):
        return "test"
    if parts & {"docs", "doc", "documentation"} or any(name.startswith(prefix) for prefix in DOC_NAMES) or suffix in {".md", ".rst", ".adoc"}:
        return "docs"
    if suffix in CONFIG_EXTS or name.startswith(".") or "/.github/" in f"/{low}":
        return "config"
    if suffix in SOURCE_EXTS:
        return "source"
    return "unknown"


def role_for(path: str) -> str:
    low = path.lower()
    if is_test_path(path):
        return "tests"
    if "/cli" in low or low.endswith("cli.py") or low == "index.ts":
        return "cli"
    if "daemon" in low:
        return "daemon"
    if "backend" in low or "indexer" in low:
        return "backend"
    if "config" in low or low.endswith((".yml", ".yaml", ".toml", ".json")):
        return "config"
    if low.startswith("docs/") or low.endswith(".md"):
        return "docs"
    return "unknown"


def _load_or_refresh(repo: Path, refresh_first: bool = False) -> lexical.IndexData | None:
    if refresh_first or lexical.load_index(repo) is None:
        lexical.refresh(repo)
    return lexical.load_index(repo)


def _files_from_index(data: lexical.IndexData | None) -> list[str]:
    if not data:
        return []
    return sorted({chunk.filename for chunk in data.chunks})


def _base(repo: Path, operation: str, backend: str = "cocoindex") -> dict[str, Any]:
    return {
        "ok": True,
        "backend": backend,
        "operation": operation,
        "repo": str(repo.resolve()),
        "repo_id": None,
        "branch": None,
        "branch_id": None,
        "schema_version": 1,
        "pipeline_version": None,
        "capabilities": {
            "repo_hierarchy": False,
            "repo_map": True,
            "symbols": False,
            "references": False,
            "call_graph": False,
            "test_links": False,
            "find_tests": "heuristic",
            "similar_code": "hybrid",
            "review_context": "context_composition",
            "languages": ["python", "typescript", "javascript"],
        },
        "warning": None,
    }


def repo_map(repo: Path, target: object | None = None, depth: int = 2, include_symbols: bool = True, include_tests: bool = False, refresh_first: bool = False, backend: str = "cocoindex") -> dict[str, Any]:
    repo = repo.resolve(); depth = clamp(depth, 0, 5)
    data = _load_or_refresh(repo, refresh_first)
    files = _files_from_index(data)
    target_path = "" if target in (None, "") else str(target).split(":", 1)[0].strip("/")
    target_abs = repo / target_path if target_path else repo
    if target_path and not any(f == target_path or f.startswith(target_path.rstrip("/") + "/") for f in files) and not target_abs.exists():
        kind = "unresolved"
    else:
        kind = "directory" if (not target_path or target_abs.is_dir()) else "file"
    paths: set[str] = {""}
    for f in files:
        if target_path and not (f == target_path or f.startswith(target_path.rstrip("/") + "/")):
            continue
        parts = Path(f).parts
        for i in range(1, len(parts) + 1):
            if i - (len(Path(target_path).parts) if target_path else 0) <= depth + 1:
                paths.add("/".join(parts[:i]))
    symbols_by_file: dict[str, list[dict[str, object]]] = {}
    if include_symbols and data:
        by_file: dict[str, list[lexical.CodeChunk]] = {}
        for c in data.chunks:
            by_file.setdefault(c.filename, []).append(c)
        for fname, fchunks in by_file.items():
            if target_path and not (fname == target_path or fname.startswith(target_path.rstrip("/") + "/")):
                continue
            symbols_by_file[fname] = _symbol_records(fchunks)
    nodes = []
    for p in sorted(paths, key=lambda x: (x.count("/"), x)):
        if p and target_path and not (p == target_path or p.startswith(target_path.rstrip("/") + "/") or target_path.startswith(p + "/")):
            continue
        related = [f for f in files if not p or f == p or f.startswith(p.rstrip("/") + "/")]
        direct_file = p in files
        node_kind = "root" if not p else ("test_file" if direct_file and is_test_path(p) else "module" if direct_file else "test_directory" if any(part.lower() in TEST_DIRS for part in Path(p).parts) else "docs_directory" if Path(p).name.lower() in {"docs", "doc", "documentation"} else "directory")
        symbols = [r for f in related for r in symbols_by_file.get(f, [])] if include_symbols else []
        node = {
            "node_id": lexical.hashlib.sha256(f"repo_hierarchy\0{p}\0{node_kind}".encode()).hexdigest()[:32],
            "path": p,
            "name": Path(p).name if p else repo.name,
            "node_kind": node_kind,
            "parent_id": None,
            "summary": f"{role_for(p)} {node_kind} with {len(related)} indexed file(s)",
            "role": role_for(p),
            "languages": sorted({Path(f).suffix.lstrip('.') or 'text' for f in related})[:5],
            "file_count": len(related) if not direct_file else 1,
            "symbol_count": len(symbols),
            "test_count": sum(1 for f in related if is_test_path(f)),
            "key_symbols": symbols[:12] if include_symbols else [],
            "metadata": {"confidence": 0.45, "evidence": ["path", "file_scan"], "source": "repo-map-v1"},
        }
        nodes.append(node)
    by_path = {n["path"]: n["node_id"] for n in nodes}
    for n in nodes:
        p = str(n["path"])
        parent = "/".join(Path(p).parts[:-1]) if p else None
        n["parent_id"] = by_path.get(parent or "") if p else None
    edges = [{"from_node_id": n["parent_id"], "to_node_id": n["node_id"], "edge_kind": "contains", "confidence": 1.0} for n in nodes if n.get("parent_id")]
    payload = _base(repo, "repo_map", backend); payload.update({"target": target_path or None, "target_kind": kind, "depth": depth, "include_symbols": include_symbols, "include_tests": include_tests, "nodes": nodes[:200], "edges": edges, "truncated": len(nodes) > 200, "truncation": {"node_budget": 200, "omitted_nodes": max(0, len(nodes)-200)}})
    return payload


def _candidate_test_files(repo: Path, data: lexical.IndexData | None) -> list[str]:
    files = _files_from_index(data)
    if not files:
        cfg = load_project_config(repo)
        files = [lexical.rel_name(repo, p) for p in lexical.iter_files(repo, cfg)]
    return [f for f in files if is_test_path(f)]


def _target_file(target: object) -> str:
    text = str(target)
    if ":" in text and ("/" in text or "." in text.split(":", 1)[0]):
        return text.split(":", 1)[0]
    return text


def find_tests(repo: Path, targets: list[object], top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, backend: str = "cocoindex") -> dict[str, Any]:
    repo = repo.resolve(); top_k = clamp(top_k, 1, 100); data = _load_or_refresh(repo, refresh_first)
    tests = _candidate_test_files(repo, data)
    results: list[dict[str, Any]] = []
    for target in targets:
        tf = _target_file(target); stem = Path(tf).stem.replace("test_", "").replace("_test", "")
        stem_tokens = [tok for tok in stem.split("_") if len(tok) >= 3]
        for test in tests:
            evidence=[]; score=0.15
            if Path(test).name in {f"test_{Path(tf).name}", f"{Path(tf).stem}_test{Path(tf).suffix}"}: evidence.append("path_pattern"); score += .35
            if stem and stem in Path(test).stem: evidence.append("name_overlap"); score += .25
            elif stem_tokens and any(tok in Path(test).stem for tok in stem_tokens): evidence.append("name_overlap_token"); score += .20
            if Path(test).parent.name.lower() in TEST_DIRS: evidence.append("nearest_test_directory"); score += .1
            if Path(test).suffix == ".py": evidence.append("framework_pattern"); cmd = f"uv run pytest {test}"
            else: evidence.append("framework_pattern"); cmd = "npm run test:ts"
            conf = min(0.8, score)
            if evidence:
                label = "high" if conf >= .75 else "medium" if conf >= .45 else "low"
                results.append({"test_file": test, "test_symbol": None, "test_symbol_id": None, "target_file": tf, "target_symbol_id": None, "score": round(min(score,1.0),6), "confidence": round(conf,6), "evidence": evidence, "recommended_command": cmd, "metadata": {"source": "test-link-heuristic-v1", "framework": "pytest" if test.endswith('.py') else "unknown", "confidence_label": label}})
    results.sort(key=lambda r: (-float(r["score"]), str(r["test_file"])))
    payload = _base(repo, "find_tests", backend); payload.update({"targets": [str(t) for t in targets], "target": ", ".join(str(t) for t in targets), "top_k": top_k, "include_indirect": include_indirect, "results": results[:top_k], "truncated": len(results) > top_k, "truncation": {"candidate_budget": len(tests), "omitted_candidates": 0, "omitted_results": max(0, len(results)-top_k)}})
    if not results:
        payload["warning"] = (payload.get("warning") or "") + "; no likely tests found from path/name heuristics"
    return payload


def _query_content_role(target_file: str | None, query: str | None) -> str:
    if target_file:
        return content_role_for(target_file)
    tokens = set(lexical.tokenize(query or ""))
    if tokens & {"readme", "documentation", "docs", "guide", "tutorial"}:
        return "docs"
    if tokens & {"pytest", "test", "tests", "assert", "spec"}:
        return "test"
    return "source"


def _role_prior(query_role: str, candidate_role: str) -> float:
    priors = {
        "source": {"source": 1.00, "config": 0.85, "test": 0.70, "docs": 0.55, "generated": 0.30, "unknown": 0.75},
        "docs": {"docs": 1.00, "source": 0.85, "config": 0.75, "test": 0.65, "generated": 0.30, "unknown": 0.70},
        "test": {"test": 1.00, "source": 0.90, "config": 0.75, "docs": 0.60, "generated": 0.30, "unknown": 0.70},
    }
    return priors.get(query_role, priors["source"]).get(candidate_role, 0.70)


def _chunk_kind_for(chunk: lexical.CodeChunk) -> str:
    code = chunk.code.lstrip()
    if re.search(r"^(async\s+def|def)\s+", code, re.MULTILINE):
        return "function"
    if re.search(r"^class\s+", code, re.MULTILINE):
        return "class"
    if re.search(r"^(export\s+)?(async\s+)?function\s+|=>\s*[{(]", code, re.MULTILINE):
        return "function"
    return "text"


def _symbol_for(chunk: lexical.CodeChunk) -> str | None:
    match = re.search(r"^(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", chunk.code, re.MULTILINE)
    if match:
        return match.group(1)
    match = re.search(r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)", chunk.code, re.MULTILINE)
    return match.group(1) if match else None


def _symbol_records(chunks) -> list[dict[str, object]]:
    records = []
    seen: set[tuple[str, int, str]] = set()
    for chunk in chunks:
        parsed = None
        if chunk.filename.endswith(".py"):
            try:
                parsed = ast.parse(chunk.code)
            except SyntaxError:
                parsed = None
        if parsed is not None:
            nodes = [n for n in parsed.body if isinstance(n, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)]
            for node in nodes:
                start = chunk.start_line + int(node.lineno) - 1
                end = chunk.start_line + int(getattr(node, "end_lineno", node.lineno)) - 1
                key = (chunk.filename, start, node.name)
                if key in seen:
                    continue
                seen.add(key)
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                qualified = ".".join([*Path(chunk.filename).with_suffix("").parts, node.name])
                records.append({"symbol_id": lexical.hashlib.sha256(f"symbol\0{chunk.filename}\0{start}\0{node.name}".encode()).hexdigest()[:32], "qualified_name": qualified, "kind": kind, "filename": chunk.filename, "start_line": start, "end_line": end})
            continue
        symbol = _symbol_for(chunk)
        if not symbol:
            continue
        kind = "class" if re.search(r"^class\s+" + re.escape(symbol) + r"\b", chunk.code, re.MULTILINE) else "function"
        qualified = ".".join([*Path(chunk.filename).with_suffix("").parts, symbol])
        records.append({"symbol_id": lexical.hashlib.sha256(f"symbol\0{chunk.filename}\0{chunk.start_line}\0{symbol}".encode()).hexdigest()[:32], "qualified_name": qualified, "kind": kind, "filename": chunk.filename, "start_line": chunk.start_line, "end_line": chunk.end_line})
    records.sort(key=lambda r: (str(r["filename"]), int(r["start_line"]), str(r["qualified_name"])))
    return records


def _ast_score(chunk_kind: str, query_role: str, candidate_role: str) -> float:
    if candidate_role in {"docs", "test"} and query_role in {"docs", "test"}:
        return 0.65
    if chunk_kind == "function":
        return 1.0
    if chunk_kind == "class":
        return 0.90
    if candidate_role in {"docs", "config", "test"}:
        return 0.20
    return 0.30


def _structure_score(candidate: str, target_file: str | None, query_tokens: dict[str, float]) -> float:
    if target_file:
        cand_parent = Path(candidate).parent.as_posix()
        target_parent = Path(target_file).parent.as_posix()
        if cand_parent == target_parent:
            return 0.90
        if role_for(candidate) == role_for(target_file):
            return 0.65
    stem_tokens = set(lexical.tokenize(Path(candidate).stem))
    if stem_tokens & set(query_tokens):
        return 0.45
    return 0.20


def _shared_token_evidence(query_tokens: dict[str, float], chunk_tokens: dict[str, float]) -> str | None:
    shared = [tok for tok in query_tokens if tok in chunk_tokens]
    if not shared:
        return None
    shared.sort(key=lambda tok: (-query_tokens[tok] * chunk_tokens[tok], tok))
    return "lexical:shared_tokens=" + ",".join(shared[:5])


def _score_similar_candidate(*, mode: str, lexical_score: float, symbol_score: float, ast: float, structure: float, freshness: float, role_prior: float, penalty: float) -> tuple[float, dict[str, float]]:
    semantic = 0.0
    if mode == "semantic":
        weights = {"semantic": 0.0, "lexical": 0.70, "symbol": 0.10, "ast": 0.08, "structure": 0.07, "freshness": 0.05}
    else:
        weights = {"semantic": 0.0, "lexical": 0.50, "symbol": 0.17, "ast": 0.13, "structure": 0.12, "freshness": 0.08}
    base = weights["lexical"] * lexical_score + weights["symbol"] * symbol_score + weights["ast"] * ast + weights["structure"] * structure + weights["freshness"] * freshness
    final = max(0.0, min(1.0, base * role_prior - penalty))
    return final, {
        "semantic": round(weights["semantic"] * semantic, 6),
        "lexical": round(weights["lexical"] * lexical_score, 6),
        "symbol": round(weights["symbol"] * symbol_score, 6),
        "ast": round(weights["ast"] * ast, 6),
        "structure": round(weights["structure"] * structure, 6),
        "freshness": round(weights["freshness"] * freshness, 6),
        "role_prior_multiplier": round(role_prior, 6),
        "penalty": round(-penalty, 6),
        "final": round(final, 6),
    }


def _risk_label(role: str, chunk_kind: str, lex: float, symbol_score: float, path_role: str) -> str:
    if role == "test":
        return "test_drift"
    if role == "docs":
        return "documentation_overlap"
    if lex >= 0.75 and role == "source" and chunk_kind in {"function", "class"}:
        return "near_duplicate_chunk"
    if (symbol_score >= 0.35 or chunk_kind in {"function", "class"}) and lex >= 0.35:
        return "parallel_helper"
    if path_role == "cli" and lex > 0:
        return "parallel_command_handler"
    return "semantic_overlap"


def _aggregate_file_results(results: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(str(result["filename"]), []).append(result)
    files: list[dict[str, Any]] = []
    for filename, items in grouped.items():
        items.sort(key=lambda r: -float(r["score"]))
        best = dict(items[0])
        support = len(items)
        best["score"] = round(min(1.0, float(best["score"]) + min(0.08, 0.02 * (support - 1))), 6)
        best["chunk_id"] = None
        best["symbol_id"] = None
        best["evidence"] = ["file_aggregate:max_candidate", *list(best.get("evidence", []))[:5]]
        best["metadata"] = dict(best.get("metadata", {}))
        best["metadata"]["candidate_kind"] = "file"
        best["metadata"]["aggregated_candidates"] = {"chunks": support, "symbols": 0, "supporting_evidence_count": support, "best_candidate_key": items[0]["metadata"].get("candidate_key"), "top_candidate_keys": [item["metadata"].get("candidate_key") for item in items[:3]]}
        files.append(best)
    files.sort(key=lambda r: (-float(r["score"]), str(r["filename"])))
    return files[:top_k]


def find_similar_code(repo: Path, target: object | None = None, query: str | None = None, top_k: int = 12, mode: str = "hybrid", scope: str = "chunks", exclude_self: bool = True, refresh_first: bool = False, backend: str = "cocoindex") -> dict[str, Any]:
    if not target and not query:
        return {"ok": False, "backend": backend, "operation": "find_similar_code", "repo": str(repo.resolve()), "error": "find_similar_code requires target or query"}
    repo = repo.resolve(); top_k = clamp(top_k, 1, 100); data = _load_or_refresh(repo, refresh_first)
    ranking_profile = "similar-code-v2"
    if data is None:
        payload = _base(repo, "find_similar_code", backend); payload.update({"target": target, "query": query, "mode": mode, "scope": scope, "exclude_self": exclude_self, "top_k": top_k, "ranking_profile": ranking_profile, "results": [], "truncated": False, "truncation": {}}); return payload
    target_file = _target_file(target) if target else None
    query_role = _query_content_role(target_file, query)
    qtext = query or ""
    if target_file:
        qtext += "\n" + "\n".join(c.code for c in data.chunks if c.filename == target_file)[:4000]
    qtok = lexical.tokenize(qtext or str(target or ""))
    ranked: list[tuple[float, int, float, int, str, dict[str, Any]]] = []
    for c in data.chunks:
        if exclude_self and target_file and c.filename == target_file and not query:
            continue
        raw_lexical_score = lexical.score_tokens(qtok, c.tokens)
        if raw_lexical_score <= 0:
            continue
        candidate_role = content_role_for(c.filename)
        lexical_score = raw_lexical_score
        if query_role == "source" and candidate_role == "docs":
            lexical_score = min(lexical_score, 0.45)
        if query_role == "source" and candidate_role == "test":
            lexical_score = min(lexical_score, 0.25)
        path_role = role_for(c.filename)
        chunk_kind = _chunk_kind_for(c)
        symbol = _symbol_for(c)
        symbol_tokens = lexical.tokenize(symbol or Path(c.filename).stem)
        symbol_score = min(0.35 if lexical_score <= 0 else 1.0, lexical.score_tokens(qtok, symbol_tokens) + (0.20 if symbol and chunk_kind in {"function", "class"} else 0.0))
        ast = _ast_score(chunk_kind, query_role, candidate_role)
        structure = _structure_score(c.filename, target_file, qtok)
        freshness = 1.0
        role_prior = _role_prior(query_role, candidate_role)
        penalty = 0.0
        evidence: list[str] = []
        shared = _shared_token_evidence(qtok, c.tokens)
        if shared:
            evidence.append(shared)
        if symbol_score > 0.20 and symbol:
            evidence.append(f"symbol:name_stem_match={symbol.split('_')[0].lower()}")
        evidence.append(f"ast:{chunk_kind}_chunk" if chunk_kind != "text" else "ast:text_chunk")
        if structure >= 0.65:
            evidence.append("structure:same_path_role")
        evidence.append(f"role:{query_role}_query_{candidate_role}_candidate")
        evidence.append("freshness:lexical_index")
        if candidate_role == "generated":
            penalty += 0.10; evidence.append("penalty:generated_or_vendor")
        if query_role == "source" and candidate_role == "docs":
            penalty += 0.05; evidence.append("penalty:docs_for_source_query")
        if query_role == "source" and candidate_role == "test":
            penalty += 0.03; evidence.append("penalty:test_for_source_query")
        score, components = _score_similar_candidate(mode=mode, lexical_score=lexical_score, symbol_score=symbol_score, ast=ast, structure=structure, freshness=freshness, role_prior=role_prior, penalty=penalty)
        result = {"score": round(score, 6), "confidence": round(min(0.85, score + 0.10), 6), "similarity": {"semantic": 0.0, "lexical": round(lexical_score, 6), "structure": round(structure, 6), "symbol": round(symbol_score, 6), "ast": round(ast, 6), "role_prior": round(role_prior, 6), "freshness": round(freshness, 6), "penalty": round(penalty, 6)}, "score_components": components, "filename": c.filename, "start_line": c.start_line, "end_line": c.end_line, "code": c.code[:12000], "symbol": symbol, "symbol_id": None, "chunk_id": c.id, "risk": _risk_label(candidate_role, chunk_kind, lexical_score, symbol_score, path_role), "evidence": evidence, "metadata": {"excluded_self": exclude_self, "ranking_profile": ranking_profile, "candidate_kind": "chunk", "content_role": candidate_role, "chunk_kind": chunk_kind, "source": ranking_profile, "candidate_key": f"chunk:{c.id}", "freshness_status": "current", "semantic_available": False, "lexical_available": True, "symbol_available": symbol is not None, "ast_available": chunk_kind != "text"}}
        kind_rank = 0 if chunk_kind == "function" else 1 if chunk_kind == "class" else 3 if candidate_role == "source" else 4 if candidate_role == "config" else 5 if candidate_role == "test" else 6 if candidate_role == "docs" else 7
        ranked.append((score, -kind_rank, lexical_score, -len(c.code), f"{c.filename}:{c.start_line}:{c.id}", result))
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], -item[3], item[4]))
    min_score = 0.30 if scope == "symbols" else 0.15
    all_results = [item[-1] for item in ranked if item[0] >= min_score]
    warning = None
    if scope == "symbols":
        warning = "symbols unavailable; returned chunk candidates"
        for result in all_results:
            result["evidence"] = [*result["evidence"], "symbols_unavailable_returned_chunks"]
    results = _aggregate_file_results(all_results, top_k) if scope == "files" else all_results[:top_k]
    payload = _base(repo, "find_similar_code", backend)
    if warning:
        payload["warning"] = f"{payload.get('warning')}; {warning}" if payload.get("warning") else warning
    payload.update({"target": target, "query": query, "mode": mode, "scope": scope, "exclude_self": exclude_self, "top_k": top_k, "ranking_profile": ranking_profile, "results": results, "truncated": len(all_results) > top_k, "truncation": {"candidate_limit": min(500, len(data.chunks)), "lexical_candidate_limit": max(200, top_k * 20), "vector_chunk_candidate_limit": 0, "vector_symbol_candidate_limit": 0, "omitted_candidates": max(0, len(data.chunks) - 500), "omitted_results": max(0, len(all_results)-top_k)}})
    warnings = []
    if warning:
        warnings.append(warning)
    payload["warnings"] = warnings
    return payload


def review_context(repo: Path, targets: list[object], top_k: int = 30, include_map: bool = True, include_tests: bool = True, include_similar: bool = True, include_impact: bool = True, refresh_first: bool = False, backend: str = "cocoindex") -> dict[str, Any]:
    if not targets:
        return {"ok": False, "backend": backend, "operation": "review_context", "repo": str(repo.resolve()), "error": "review_context requires at least one target"}
    top_k = clamp(top_k, 1, 200)
    sections=[]; commands=[]; likely_tests=[]; similar=[]
    if include_map:
        m = repo_map(repo, targets[0], 2, True, include_tests, refresh_first, backend); sections.append({"section": "architecture", "items": m.get("nodes", [])[:5]})
    if include_impact:
        sections.append({"section": "impact", "items": [], "warning": None})
    if include_tests:
        t = find_tests(repo, targets, min(top_k, 100), False, refresh_first, backend); likely_tests = t.get("results", []); sections.append({"section": "tests", "items": likely_tests[:8]}); commands.extend(r["recommended_command"] for r in likely_tests[:5] if r.get("recommended_command"))
    if include_similar:
        s = find_similar_code(repo, targets[0], None, min(top_k, 100), "hybrid", "chunks", True, refresh_first, backend); similar = s.get("results", []); sections.append({"section": "similar_code", "items": similar[:8]})
    risk_items=[]
    if any(role_for(_target_file(t)) in {"cli", "daemon", "backend", "config"} for t in targets):
        risk_items.append({"risk": "public contract or routing change", "severity": "medium", "evidence": ["path_role"], "mitigation": "run CLI, daemon, and formatter tests"})
    if not likely_tests:
        risk_items.append({"risk": "no likely direct tests found", "severity": "medium", "evidence": ["test_heuristic"], "mitigation": "add or run broader regression tests"})
    sections.extend([{"section": "freshness", "items": []}, {"section": "risks", "items": risk_items}])
    for t in targets:
        if str(t).endswith(".ts") or str(t) == "index.ts":
            commands.extend(["npm run typecheck", "npm run test:ts"])
        if str(t).endswith(".py"):
            commands.append("uv run pytest")
    commands = list(dict.fromkeys(commands))
    sections.append({"section": "commands", "items": [{"command": c, "reason": "recommended validation"} for c in commands]})
    payload = _base(repo, "review_context", backend); payload.update({"targets": [str(t) for t in targets], "top_k": top_k, "summary": {"changed_files": len(targets), "resolved_targets": len(targets), "affected_symbols": 0, "likely_tests": len(likely_tests), "similar_code_hits": len(similar), "freshness_current": len(targets), "freshness_stale": 0, "risk_level": "medium" if risk_items else "low"}, "sections": sections, "recommended_commands": commands, "truncated": False, "truncation": {"section_budget": 20, "item_budget": top_k, "omitted_sections": 0, "omitted_items": 0}})
    return payload
