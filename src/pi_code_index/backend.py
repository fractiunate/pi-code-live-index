from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import POSTGRES_COMPOSE_COMMAND, POSTGRES_LIFECYCLE_COMMAND, POSTGRES_VALIDATION_COMMAND, postgres_url_config, load_global_config, load_project_config

VALID_BACKENDS = {"auto", "cocoindex"}
COCOINDEX_REQUIRED_WARNING = "CocoIndex/Postgres live indexing is required. Configure PI_CODE_INDEX_POSTGRES_URL and start Postgres with runtime/postgres/podman-pgvector.sh."


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


def _run_cocoindex(repo: Path, operation: str, func: Callable[[], dict[str, object]], choice: BackendChoice) -> dict[str, object]:
    if postgres_url_config()[1] is None:
        return {"ok": False, "backend": "cocoindex", "requested_backend": choice.requested, "backend_fallback": False, "operation": operation, "repo": str(repo.resolve()), "error": _required_error("Postgres URL is required")}
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 - backend boundary returns JSON-safe errors
        return {"ok": False, "backend": "cocoindex", "requested_backend": choice.requested, "backend_fallback": False, "operation": operation, "repo": str(repo.resolve()), "error": _required_error(exc)}


def refresh(repo: Path) -> dict[str, object]:
    repo = repo.resolve()
    choice = choose_backend(repo)
    from . import coco_backend
    return _run_cocoindex(repo, "refresh", lambda: coco_backend.refresh(repo), choice)


def search(repo: Path, query: str, top_k: int = 8, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve()
    choice = choose_backend(repo)
    from . import coco_backend
    return _run_cocoindex(repo, "search", lambda: coco_backend.search(repo, query, top_k, refresh_first, coco_resources), choice)


def status(repo: Path, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve()
    choice = choose_backend(repo)
    from . import coco_backend
    return _run_cocoindex(repo, "status", lambda: coco_backend.status(repo, coco_resources), choice)


def symbol_search(repo: Path, query: str, top_k: int = 8, filters: dict[str, object] | None = None, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    if top_k < 1:
        return {"ok": False, "backend": "cocoindex", "repo": str(repo.resolve()), "operation": "symbol_search", "error": "top_k must be >= 1"}
    repo = repo.resolve()
    choice = choose_backend(repo)
    from . import coco_backend
    return _run_cocoindex(repo, "symbol_search", lambda: coco_backend.symbol_search(repo, query, top_k, filters, refresh_first, coco_resources), choice)


def symbol_definition(repo: Path, target: object, filters: dict[str, object] | None = None, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve()
    choice = choose_backend(repo)
    from . import coco_backend
    return _run_cocoindex(repo, "symbol_definition", lambda: coco_backend.symbol_definition(repo, target, filters, refresh_first, coco_resources), choice)


def symbol_context(repo: Path, target: object, depth: int = 1, filters: dict[str, object] | None = None, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    if depth < 0:
        return {"ok": False, "backend": "cocoindex", "repo": str(repo.resolve()), "operation": "symbol_context", "error": "depth must be >= 0"}
    repo = repo.resolve()
    choice = choose_backend(repo)
    from . import coco_backend
    return _run_cocoindex(repo, "symbol_context", lambda: coco_backend.symbol_context(repo, target, depth, filters, refresh_first, coco_resources), choice)


def find_callers(repo: Path, target: object, depth: int = 1, top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve(); depth = max(1, min(int(depth), 5)); top_k = max(1, min(int(top_k), 100)); depth = depth if include_indirect else 1
    choice = choose_backend(repo)
    from . import coco_backend
    return _run_cocoindex(repo, "find_callers", lambda: coco_backend.find_callers(repo, target, depth, top_k, include_indirect, refresh_first, coco_resources), choice)


def find_callees(repo: Path, target: object, depth: int = 1, top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve(); depth = max(1, min(int(depth), 5)); top_k = max(1, min(int(top_k), 100)); depth = depth if include_indirect else 1
    choice = choose_backend(repo)
    from . import coco_backend
    return _run_cocoindex(repo, "find_callees", lambda: coco_backend.find_callees(repo, target, depth, top_k, include_indirect, refresh_first, coco_resources), choice)


def impact_analysis(repo: Path, target: object, depth: int = 2, top_k: int = 50, include_tests: bool = True, include_files: bool = True, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve(); depth = max(1, min(int(depth), 5)); top_k = max(1, min(int(top_k), 200))
    choice = choose_backend(repo)
    from . import coco_backend
    return _run_cocoindex(repo, "impact_analysis", lambda: coco_backend.impact_analysis(repo, target, depth, top_k, include_tests, include_files, refresh_first, coco_resources), choice)


def repo_map(repo: Path, target: object | None = None, depth: int = 2, include_symbols: bool = True, include_tests: bool = False, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve(); depth = max(0, min(int(depth), 5)); choice = choose_backend(repo)
    from . import coco_backend
    return _run_cocoindex(repo, "repo_map", lambda: coco_backend.repo_map(repo, target, depth, include_symbols, include_tests, refresh_first, coco_resources), choice)


def find_tests(repo: Path, targets: list[object], top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    repo = repo.resolve(); top_k = max(1, min(int(top_k), 100)); choice = choose_backend(repo)
    from . import coco_backend
    return _run_cocoindex(repo, "find_tests", lambda: coco_backend.find_tests(repo, targets, top_k, include_indirect, refresh_first, coco_resources), choice)


def find_similar_code(repo: Path, target: object | None = None, query: str | None = None, top_k: int = 12, mode: str = "hybrid", scope: str = "chunks", exclude_self: bool = True, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    if not target and not query:
        return {"ok": False, "backend": "cocoindex", "repo": str(repo.resolve()), "operation": "find_similar_code", "error": "find_similar_code requires target or query"}
    if mode not in {"semantic", "hybrid"}:
        return {"ok": False, "backend": "cocoindex", "repo": str(repo.resolve()), "operation": "find_similar_code", "error": "mode must be semantic or hybrid"}
    if scope not in {"chunks", "symbols", "files"}:
        return {"ok": False, "backend": "cocoindex", "repo": str(repo.resolve()), "operation": "find_similar_code", "error": "scope must be chunks, symbols, or files"}
    repo = repo.resolve(); top_k = max(1, min(int(top_k), 100)); choice = choose_backend(repo)
    from . import coco_backend
    return _run_cocoindex(repo, "find_similar_code", lambda: coco_backend.find_similar_code(repo, target, query, top_k, mode, scope, exclude_self, refresh_first, coco_resources), choice)


def review_context(repo: Path, targets: list[object], top_k: int = 30, include_map: bool = True, include_tests: bool = True, include_similar: bool = True, include_impact: bool = True, refresh_first: bool = False, coco_resources: object | None = None) -> dict[str, object]:
    if not targets:
        return {"ok": False, "backend": "cocoindex", "repo": str(repo.resolve()), "operation": "review_context", "error": "review_context requires at least one target"}
    repo = repo.resolve(); top_k = max(1, min(int(top_k), 200)); choice = choose_backend(repo)
    from . import coco_backend
    return _run_cocoindex(repo, "review_context", lambda: coco_backend.review_context(repo, targets, top_k, include_map, include_tests, include_similar, include_impact, refresh_first, coco_resources), choice)
