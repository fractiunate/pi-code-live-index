from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import context_tools, indexer as lexical
from .config import POSTGRES_COMPOSE_COMMAND, POSTGRES_LIFECYCLE_COMMAND, POSTGRES_VALIDATION_COMMAND, postgres_url_config, load_global_config, load_project_config

VALID_BACKENDS = {"auto", "cocoindex"}
COCOINDEX_REQUIRED_WARNING = "CocoIndex/Postgres live indexing is required. Configure PI_CODE_INDEX_POSTGRES_URL and start Postgres with runtime/postgres/podman-pgvector.sh."


def lexical_capabilities() -> dict[str, object]:
    return {"semantic_search": False, "lexical_search": True, "symbols": False, "references": False, "call_graph": False, "impact_analysis": False, "repo_map": "path_only", "find_tests": "path_heuristic", "find_similar_code": "lexical_only", "review_context": "lexical_composition"}


def _redact_postgres_url(url: str | None) -> str | None:
    if not url or "@" not in url:
        return url
    prefix, rest = url.rsplit("@", 1)
    return f"{prefix.split(':', 2)[0]}://{prefix.split('://', 1)[1].split(':', 1)[0]}:***@{rest}" if "://" in prefix else None


def postgres_summary() -> dict[str, object]:
    source, url = postgres_url_config()
    return {"configured": source != "none", "configured_url_source": source, "url": _redact_postgres_url(url), "credentials_redacted": True, "lifecycle_command": POSTGRES_LIFECYCLE_COMMAND, "compose_command": POSTGRES_COMPOSE_COMMAND, "validation_command": POSTGRES_VALIDATION_COMMAND}


def _required_error(error: object) -> str:
    return f"CocoIndex/Postgres live indexing is required but unavailable: {error}. Configure PI_CODE_INDEX_POSTGRES_URL, start Postgres with {POSTGRES_LIFECYCLE_COMMAND}, then validate with {POSTGRES_VALIDATION_COMMAND}."


def _with_backend_metadata(payload: dict[str, object], choice: "BackendChoice", *, fallback: bool = False, warning: str | None = None) -> dict[str, object]:
    payload["requested_backend"] = choice.requested
    payload["backend_fallback"] = fallback
    if warning:
        payload["warnings"] = [*(payload.get("warnings") if isinstance(payload.get("warnings"), list) else []), warning]
    return payload


@dataclass(frozen=True)
class BackendChoice:
    name: str
    requested: str
    auto: bool


def choose_backend(repo: Path) -> BackendChoice:
    global_cfg = load_global_config()
    project_cfg = load_project_config(repo)
    env_backend = os.environ.get("PI_CODE_INDEX_BACKEND")
    if env_backend:
        requested = env_backend.lower()
    elif project_cfg.backend and project_cfg.backend.lower() != "auto":
        requested = project_cfg.backend.lower()
    else:
        requested = (global_cfg.backend or "auto").lower()
    if requested not in VALID_BACKENDS:
        raise ValueError(f"invalid backend {requested!r}; expected one of {sorted(VALID_BACKENDS)}")
    if requested == "auto":
        return BackendChoice("cocoindex", requested, auto=True)
    return BackendChoice(requested, requested, auto=False)


def _with_auto_fallback(repo: Path, operation: str, func: Callable[[], dict[str, object]], fallback: Callable[[], dict[str, object]], choice: BackendChoice) -> dict[str, object]:
    del fallback  # compatibility with call sites while legacy lexical routing is retired
    if postgres_url_config()[1] is None:
        return {"ok": False, "backend": "cocoindex", "requested_backend": choice.requested, "backend_fallback": False, "operation": operation, "repo": str(repo.resolve()), "error": _required_error("Postgres URL is required")}
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 - backend boundary returns JSON-safe errors
        return {"ok": False, "backend": "cocoindex", "requested_backend": choice.requested, "backend_fallback": False, "operation": operation, "repo": str(repo.resolve()), "error": _required_error(exc)}


def refresh(repo: Path) -> dict[str, object]:
    repo = repo.resolve()
    choice = choose_backend(repo)
    if choice.name == "lexical":
        payload = lexical.refresh(repo)
        payload["ok"] = True
        payload["backend"] = "lexical"
        return _with_backend_metadata(payload, choice)
    from . import coco_backend

    return _with_auto_fallback(repo, "refresh", lambda: coco_backend.refresh(repo), lambda: refresh_lexical(repo), choice)


def refresh_lexical(repo: Path) -> dict[str, object]:
    payload = lexical.refresh(repo)
    payload["ok"] = True
    payload["backend"] = "lexical"
    return payload


def search(repo: Path, query: str, top_k: int = 8, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve()
    choice = choose_backend(repo)
    if choice.name == "lexical":
        payload = lexical.search(repo, query, top_k, refresh_first)
        payload["ok"] = True
        payload["backend"] = "lexical"
        return _with_backend_metadata(payload, choice)
    from . import coco_backend

    return _with_auto_fallback(
        repo,
        "search",
        lambda: coco_backend.search(repo, query, top_k, refresh_first, coco_resources),
        lambda: search_lexical(repo, query, top_k, refresh_first),
        choice,
    )


def search_lexical(repo: Path, query: str, top_k: int = 8, refresh_first: bool = False) -> dict[str, object]:
    payload = lexical.search(repo, query, top_k, refresh_first)
    payload["ok"] = True
    payload["backend"] = "lexical"
    return payload


def status(repo: Path, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve()
    choice = choose_backend(repo)
    if choice.name == "lexical":
        idx = lexical.load_index(repo)
        current_files = idx.files if idx else 0
        chunks = len(idx.chunks) if idx else 0
        freshness = {"source": "lexical_index", "counts": {"current": current_files, "pending": 0, "stale": 0, "deleted": 0, "error": 0}, "latest_errors": []}
        return _with_backend_metadata({
            "ok": True,
            "backend": "lexical",
            "repo": str(repo),
            "index_path": str(lexical.index_path(repo)),
            "index_exists": lexical.index_path(repo).exists(),
            "chunks": chunks,
            "files": current_files,
            "freshness": freshness,
            "counts": {"files": current_files, "chunks": chunks, "symbols": 0, "references": 0, "call_edges": 0, "test_links": 0, "repo_hierarchy_nodes": 0, "test_files": 0, "similarity_candidates": chunks, "freshness_current": current_files, "freshness_pending": 0, "freshness_stale": 0, "freshness_deleted": 0, "freshness_error": 0},
            "capabilities": {"search": True, "symbols": False, "references": False, "graph": False, "quality_context": True, "live": True, "repo_map": "path_only", "find_tests": "path_heuristic", "find_similar_code": "lexical_only", "review_context": "lexical_composition"},
            "quality_context": {"ready": bool(idx), "warnings": ["lexical heuristics only"]},
            "warnings": ["lexical heuristics only"],
        }, choice)
    from . import coco_backend

    return _with_auto_fallback(
        repo,
        "status",
        lambda: coco_backend.status(repo, coco_resources),
        lambda: status_lexical(repo),
        choice,
    )


def _symbol_lexical_payload(repo: Path, operation: str, warning: str) -> dict[str, object]:
    payload: dict[str, object] = {"ok": True, "backend": "lexical", "operation": operation, "repo": str(repo.resolve()), "repo_id": None, "branch": None, "branch_id": None, "schema_version": 1, "pipeline_version": None, "capabilities": {"symbols": False}, "warning": warning}
    if operation == "symbol_search":
        payload.update({"results": [], "truncated": False, "truncation": {"candidate_limit": 0, "omitted_candidates": 0}})
    elif operation == "symbol_definition":
        payload.update({"definition": None, "matches": []})
    else:
        payload.update({"symbol": None, "parents": [], "children": [], "siblings": [], "module_symbols": [], "chunks": [], "references_available": False})
    return payload


def symbol_search(repo: Path, query: str, top_k: int = 8, filters: dict[str, object] | None = None, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    if top_k < 1:
        return {"ok": False, "backend": "lexical", "repo": str(repo.resolve()), "operation": "symbol_search", "error": "top_k must be >= 1"}
    repo = repo.resolve()
    choice = choose_backend(repo)
    if choice.name == "lexical":
        payload = _symbol_lexical_payload(repo, "symbol_search", "symbol_search requires CocoIndex/Postgres symbol indexing; lexical backend cannot prove symbol absence")
        payload.update({"query": query, "top_k": min(top_k, 50), "filters": filters or {}})
        return _with_backend_metadata(payload, choice, warning=payload["warning"])
    from . import coco_backend
    return _with_auto_fallback(repo, "symbol_search", lambda: coco_backend.symbol_search(repo, query, top_k, filters, refresh_first, coco_resources), lambda: _symbol_lexical_payload(repo, "symbol_search", "symbol_search requires CocoIndex/Postgres symbol indexing; lexical backend cannot prove symbol absence"), choice)


def symbol_definition(repo: Path, target: object, filters: dict[str, object] | None = None, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve()
    choice = choose_backend(repo)
    if choice.name == "lexical":
        payload = _symbol_lexical_payload(repo, "symbol_definition", "symbol_definition requires CocoIndex/Postgres symbol indexing; lexical backend cannot prove definition absence")
        payload["target"] = target
        return _with_backend_metadata(payload, choice, warning=payload["warning"])
    from . import coco_backend
    return _with_auto_fallback(repo, "symbol_definition", lambda: coco_backend.symbol_definition(repo, target, filters, refresh_first, coco_resources), lambda: _symbol_lexical_payload(repo, "symbol_definition", "symbol_definition requires CocoIndex/Postgres symbol indexing; lexical backend cannot prove definition absence"), choice)


def symbol_context(repo: Path, target: object, depth: int = 1, filters: dict[str, object] | None = None, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    if depth < 0:
        return {"ok": False, "backend": "lexical", "repo": str(repo.resolve()), "operation": "symbol_context", "error": "depth must be >= 0"}
    repo = repo.resolve()
    choice = choose_backend(repo)
    if choice.name == "lexical":
        payload = _symbol_lexical_payload(repo, "symbol_context", "symbol_context requires CocoIndex/Postgres symbol indexing; lexical backend cannot build symbol relationships")
        payload.update({"target": target, "depth": min(depth, 5)})
        return _with_backend_metadata(payload, choice, warning=payload["warning"])
    from . import coco_backend
    return _with_auto_fallback(repo, "symbol_context", lambda: coco_backend.symbol_context(repo, target, depth, filters, refresh_first, coco_resources), lambda: _symbol_lexical_payload(repo, "symbol_context", "symbol_context requires CocoIndex/Postgres symbol indexing; lexical backend cannot build symbol relationships"), choice)


def _graph_lexical_payload(repo: Path, operation: str, target: object, depth: int, top_k: int, warning: str) -> dict[str, object]:
    message = f"Unsupported on lexical backend: {warning}"
    payload: dict[str, object] = {"ok": True, "available": False, "unsupported": True, "status": "unsupported", "message": message, "backend": "lexical", "operation": operation, "repo": str(repo.resolve()), "repo_id": None, "branch": None, "branch_id": None, "schema_version": 1, "pipeline_version": None, "target": target, "target_kind": "unresolved", "target_symbol": None, "matches": [], "depth": depth, "top_k": top_k, "capabilities": {"symbols": False, "references": False, "call_graph": False, "impact_analysis": False, "test_links": False, "languages": ["python"]}, "warning": message}
    if operation in {"find_callers", "find_callees"}:
        payload.update({"results": [], "truncated": False, "truncation": {"edge_budget": 0, "omitted_paths": 0, "omitted_results": 0}})
    else:
        payload.update({"affected_symbols": [], "affected_files": [], "affected_tests": [], "summary": {"direct_callers": 0, "indirect_callers": 0, "direct_callees": 0, "indirect_callees": 0, "affected_symbols": 0, "affected_files": 0, "affected_tests": 0, "truncated": False}})
    return payload


def find_callers(repo: Path, target: object, depth: int = 1, top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve(); depth = max(1, min(int(depth), 5)); top_k = max(1, min(int(top_k), 100)); depth = depth if include_indirect else 1
    choice = choose_backend(repo); warning = "call graph tools require CocoIndex/Postgres reference indexing; lexical backend cannot prove caller/callee absence"
    if choice.name == "lexical":
        return _with_backend_metadata(_graph_lexical_payload(repo, "find_callers", target, depth, top_k, warning), choice, warning=warning)
    from . import coco_backend
    return _with_auto_fallback(repo, "find_callers", lambda: coco_backend.find_callers(repo, target, depth, top_k, include_indirect, refresh_first, coco_resources), lambda: _graph_lexical_payload(repo, "find_callers", target, depth, top_k, warning), choice)


def find_callees(repo: Path, target: object, depth: int = 1, top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve(); depth = max(1, min(int(depth), 5)); top_k = max(1, min(int(top_k), 100)); depth = depth if include_indirect else 1
    choice = choose_backend(repo); warning = "call graph tools require CocoIndex/Postgres reference indexing; lexical backend cannot prove caller/callee absence"
    if choice.name == "lexical":
        return _with_backend_metadata(_graph_lexical_payload(repo, "find_callees", target, depth, top_k, warning), choice, warning=warning)
    from . import coco_backend
    return _with_auto_fallback(repo, "find_callees", lambda: coco_backend.find_callees(repo, target, depth, top_k, include_indirect, refresh_first, coco_resources), lambda: _graph_lexical_payload(repo, "find_callees", target, depth, top_k, warning), choice)


def impact_analysis(repo: Path, target: object, depth: int = 2, top_k: int = 50, include_tests: bool = True, include_files: bool = True, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve(); depth = max(1, min(int(depth), 5)); top_k = max(1, min(int(top_k), 200))
    choice = choose_backend(repo); warning = "impact_analysis requires CocoIndex/Postgres reference indexing; lexical backend cannot compute blast radius"
    if choice.name == "lexical":
        return _with_backend_metadata(_graph_lexical_payload(repo, "impact_analysis", target, depth, top_k, warning), choice, warning=warning)
    from . import coco_backend
    return _with_auto_fallback(repo, "impact_analysis", lambda: coco_backend.impact_analysis(repo, target, depth, top_k, include_tests, include_files, refresh_first, coco_resources), lambda: _graph_lexical_payload(repo, "impact_analysis", target, depth, top_k, warning), choice)


def repo_map(repo: Path, target: object | None = None, depth: int = 2, include_symbols: bool = True, include_tests: bool = False, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve(); depth = max(0, min(int(depth), 5)); choice = choose_backend(repo)
    if choice.name == "lexical":
        payload = context_tools.repo_map(repo, target, depth, include_symbols, include_tests, refresh_first, "lexical")
        return _with_backend_metadata(payload, choice, warning="repo_map requires CocoIndex/Postgres for supported operation")
    from . import coco_backend
    return _with_auto_fallback(repo, "repo_map", lambda: coco_backend.repo_map(repo, target, depth, include_symbols, include_tests, refresh_first, coco_resources), lambda: context_tools.repo_map(repo, target, depth, include_symbols, include_tests, refresh_first, "lexical"), choice)


def find_tests(repo: Path, targets: list[object], top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve(); top_k = max(1, min(int(top_k), 100)); choice = choose_backend(repo)
    if choice.name == "lexical":
        payload = context_tools.find_tests(repo, targets, top_k, include_indirect, refresh_first, "lexical")
        return _with_backend_metadata(payload, choice, warning="find_tests requires CocoIndex/Postgres for supported operation")
    from . import coco_backend
    return _with_auto_fallback(repo, "find_tests", lambda: coco_backend.find_tests(repo, targets, top_k, include_indirect, refresh_first, coco_resources), lambda: context_tools.find_tests(repo, targets, top_k, include_indirect, refresh_first, "lexical"), choice)


def find_similar_code(repo: Path, target: object | None = None, query: str | None = None, top_k: int = 12, mode: str = "hybrid", scope: str = "chunks", exclude_self: bool = True, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    if not target and not query:
        return {"ok": False, "backend": "cocoindex", "repo": str(repo.resolve()), "operation": "find_similar_code", "error": "find_similar_code requires target or query"}
    if mode not in {"semantic", "hybrid"}:
        return {"ok": False, "backend": "cocoindex", "repo": str(repo.resolve()), "operation": "find_similar_code", "error": "mode must be semantic or hybrid"}
    if scope not in {"chunks", "symbols", "files"}:
        return {"ok": False, "backend": "cocoindex", "repo": str(repo.resolve()), "operation": "find_similar_code", "error": "scope must be chunks, symbols, or files"}
    repo = repo.resolve(); top_k = max(1, min(int(top_k), 100)); choice = choose_backend(repo)
    if choice.name == "lexical":
        payload = context_tools.find_similar_code(repo, target, query, top_k, mode, scope, exclude_self, refresh_first, "lexical")
        return _with_backend_metadata(payload, choice, warning="find_similar_code requires CocoIndex/Postgres for supported operation")
    from . import coco_backend
    return _with_auto_fallback(repo, "find_similar_code", lambda: coco_backend.find_similar_code(repo, target, query, top_k, mode, scope, exclude_self, refresh_first, coco_resources), lambda: context_tools.find_similar_code(repo, target, query, top_k, mode, scope, exclude_self, refresh_first, "lexical"), choice)


def review_context(repo: Path, targets: list[object], top_k: int = 30, include_map: bool = True, include_tests: bool = True, include_similar: bool = True, include_impact: bool = True, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    if not targets:
        return {"ok": False, "backend": "cocoindex", "repo": str(repo.resolve()), "operation": "review_context", "error": "review_context requires at least one target"}
    repo = repo.resolve(); top_k = max(1, min(int(top_k), 200)); choice = choose_backend(repo)
    if choice.name == "lexical":
        payload = context_tools.review_context(repo, targets, top_k, include_map, include_tests, include_similar, include_impact, refresh_first, "lexical")
        return _with_backend_metadata(payload, choice, warning="review_context requires CocoIndex/Postgres for supported operation")
    from . import coco_backend
    return _with_auto_fallback(repo, "review_context", lambda: coco_backend.review_context(repo, targets, top_k, include_map, include_tests, include_similar, include_impact, refresh_first, coco_resources), lambda: context_tools.review_context(repo, targets, top_k, include_map, include_tests, include_similar, include_impact, refresh_first, "lexical"), choice)


def status_lexical(repo: Path) -> dict[str, object]:
    idx = lexical.load_index(repo)
    return {
        "ok": True,
        "backend": "lexical",
        "repo": str(repo.resolve()),
        "index_path": str(lexical.index_path(repo)),
        "index_exists": lexical.index_path(repo).exists(),
        "chunks": len(idx.chunks) if idx else 0,
        "files": idx.files if idx else 0,
        "counts": {"repo_hierarchy_nodes": 0, "test_links": 0, "test_files": 0, "similarity_candidates": len(idx.chunks) if idx else 0, "freshness_current": idx.files if idx else 0, "freshness_stale": 0, "freshness_error": 0},
        "capabilities": lexical_capabilities(),
        "quality_context": {"ready": bool(idx), "warnings": ["lexical heuristics only"]},
        "warnings": [COCOINDEX_REQUIRED_WARNING],
    }
