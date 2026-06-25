from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import socket
import subprocess
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from . import context_tools
from .config import GlobalConfig, ProjectConfig, data_home, load_global_config, load_project_config, validate_identifier
from .indexer import chunk_text, iter_files, refresh as refresh_lexical_index, score_tokens, tokenize

try:  # pragma: no cover - optional dependency boundary
    import asyncpg
    import cocoindex as coco
    from cocoindex.connectors import localfs, postgres
    from cocoindex.ops.sentence_transformers import SentenceTransformerEmbedder
    from cocoindex.ops.text import RecursiveSplitter, detect_code_language
    from cocoindex.resources.file import FileLike, PatternFilePathMatcher
    from numpy.typing import NDArray
except Exception:  # pragma: no cover - exercised by unavailable-backend tests/calls
    asyncpg = None  # type: ignore[assignment]
    coco = None  # type: ignore[assignment]
    localfs = None  # type: ignore[assignment]
    postgres = None  # type: ignore[assignment]
    SentenceTransformerEmbedder = None  # type: ignore[assignment]
    RecursiveSplitter = None  # type: ignore[assignment]
    detect_code_language = None  # type: ignore[assignment]
    FileLike = Any  # type: ignore[assignment]
    PatternFilePathMatcher = None  # type: ignore[assignment]
    NDArray = Any  # type: ignore[assignment]


class CocoIndexUnavailable(RuntimeError):
    """Raised when the optional CocoIndex backend cannot run."""


class CocoBackendResources:
    """Warm resources reused by the daemon across CocoIndex requests."""

    def __init__(self, postgres_url: str, embedding_model: str) -> None:
        self.postgres_url = postgres_url
        self.embedding_model = embedding_model
        self.loop = asyncio.new_event_loop()
        self.pool: Any | None = None
        self.embedder: Any | None = None
        self.pool_creations = 0
        self.embedder_creations = 0
        self.closed = False

    async def get_pool(self) -> Any:
        _require_coco()
        if self.closed:
            raise CocoIndexUnavailable("CocoIndex resources are closed")
        if self.pool is None:
            self.pool = await asyncpg.create_pool(_require_postgres_url(self.postgres_url))
            self.pool_creations += 1
            async with self.pool.acquire() as conn:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        return self.pool

    def get_embedder(self) -> Any:
        _require_coco()
        if self.closed:
            raise CocoIndexUnavailable("CocoIndex resources are closed")
        if self.embedder is None:
            self.embedder = SentenceTransformerEmbedder(self.embedding_model)
            self.embedder_creations += 1
        return self.embedder

    def run(self, coro: Any) -> Any:
        if self.closed:
            raise CocoIndexUnavailable("CocoIndex resources are closed")
        return self.loop.run_until_complete(coro)

    async def aclose(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
        self.embedder = None
        self.closed = True

    def close(self) -> None:
        if not self.closed:
            self.loop.run_until_complete(self.aclose())
        self.loop.close()

    def status(self) -> dict[str, object]:
        return {
            "postgres_pool": "warm" if self.pool is not None and not self.closed else "cold",
            "embedder": "warm" if self.embedder is not None and not self.closed else "cold",
            "pool_creations": self.pool_creations,
            "embedder_creations": self.embedder_creations,
            "closed": self.closed,
        }


_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CANONICAL_SCHEMA_VERSION = 1
CANONICAL_PIPELINE_VERSION = "canonical-v1-ast-v1"
_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_COCO_POSTGRES_URL = "postgres://cocoindex:cocoindex@localhost/cocoindex"
_COCO_EMBEDDING_MODEL = _DEFAULT_MODEL

if coco is not None:  # pragma: no branch - definition-time optional integration
    PG_DB = coco.ContextKey[asyncpg.Pool]("pg_db")
    EMBEDDER = coco.ContextKey[SentenceTransformerEmbedder]("embedder")
else:  # pragma: no cover
    PG_DB = "pg_db"
    EMBEDDER = "embedder"

# ponytail: one SentenceTransformerEmbedder per model for the whole process; the legacy
# CocoIndex pipeline (app.update_blocking lifespan) and the canonical migration both
# embed, so a shared instance loads HuggingFace weights once instead of twice per refresh.
_SHARED_EMBEDDER: Any = None
_SHARED_EMBEDDER_MODEL: str | None = None


def _shared_embedder(model: str) -> Any:
    global _SHARED_EMBEDDER, _SHARED_EMBEDDER_MODEL
    if _SHARED_EMBEDDER is None or _SHARED_EMBEDDER_MODEL != model:
        _SHARED_EMBEDDER = SentenceTransformerEmbedder(model)
        _SHARED_EMBEDDER_MODEL = model
    return _SHARED_EMBEDDER


@dataclass
class CodeEmbedding:
    id: str
    repo: str
    filename: str
    start_line: int
    end_line: int
    code: str
    embedding: Annotated[NDArray, EMBEDDER]


@dataclass
class RepoRow:
    repo_id: str
    root_path: str
    worktree_id: str
    vcs_kind: str = "git"
    default_branch: str | None = None
    metadata: dict[str, object] | None = None


@dataclass
class BranchRow:
    branch_id: str
    repo_id: str
    name: str
    head_sha: str | None
    is_default: bool = False
    metadata: dict[str, object] | None = None


@dataclass
class FileRow:
    file_id: str
    repo_id: str
    branch_id: str
    path: str
    language: str | None
    sha256: str
    mtime_ns: int | None
    size_bytes: int
    metadata: dict[str, object] | None = None


@dataclass
class ChunkRow:
    chunk_id: str
    file_id: str
    repo_id: str
    branch_id: str
    path: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    code: str
    embedding: Annotated[NDArray, EMBEDDER]
    chunk_kind: str = "text"
    symbol_id: str | None = None
    token_count: int | None = None
    metadata: dict[str, object] | None = None


@dataclass
class SymbolRow:
    symbol_id: str
    file_id: str
    repo_id: str
    branch_id: str
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    signature: str | None = None
    docstring: str | None = None
    metadata: dict[str, object] | None = None


@dataclass
class AstChunk:
    path: str
    code: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    chunk_kind: str
    chunk_role: str
    symbol_id: str | None
    symbol: str | None
    qualified_name: str | None
    symbol_kind: str | None
    parent_symbol_id: str | None
    signature: str | None
    docstring: str | None
    metadata: dict[str, object]


@dataclass
class ExtractedSymbol:
    symbol_id: str
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    signature: str | None
    docstring: str | None
    parent_symbol_id: str | None
    metadata: dict[str, object]


@dataclass
class AstExtraction:
    chunks: list[AstChunk]
    symbols: list[ExtractedSymbol]
    fallback_reason: str | None = None
    parser_error: str | None = None


@dataclass
class ReferenceRow:
    reference_id: str
    repo_id: str
    branch_id: str
    file_id: str
    symbol_id: str | None
    name: str
    kind: str
    line: int
    column_number: int = 0
    metadata: dict[str, object] | None = None


@dataclass
class CallEdgeRow:
    edge_id: str
    repo_id: str
    branch_id: str
    caller_symbol_id: str
    callee_symbol_id: str
    confidence: float
    source: str
    metadata: dict[str, object] | None = None


@dataclass
class RepoHierarchyRow:
    node_id: str
    repo_id: str
    branch_id: str
    parent_id: str | None
    path: str
    node_kind: str
    name: str
    metadata: dict[str, object] | None = None


@dataclass
class TestLinkRow:
    test_link_id: str
    repo_id: str
    branch_id: str
    test_file_id: str
    source_file_id: str
    test_symbol_id: str | None = None
    source_symbol_id: str | None = None
    confidence: float = 0.5
    metadata: dict[str, object] | None = None


@dataclass
class FreshnessRow:
    freshness_id: str
    repo_id: str
    branch_id: str
    file_id: str | None
    source_hash: str
    pipeline_version: str
    status: str
    error: str | None = None
    metadata: dict[str, object] | None = None


def _require_coco() -> None:
    if coco is None or asyncpg is None or SentenceTransformerEmbedder is None:
        raise CocoIndexUnavailable(
            "CocoIndex backend requires optional dependencies. Install with `uv sync --extra cocoindex` "
            "or `uv tool install -e .[cocoindex]`."
        )


def _require_postgres_url(url: str) -> str:
    if not url:
        raise CocoIndexUnavailable("Postgres URL is required for backend=cocoindex; set PI_CODE_INDEX_POSTGRES_URL")
    return url


def _effective_postgres_url(cfg: GlobalConfig) -> str:
    return _require_postgres_url(os.environ.get("PI_CODE_INDEX_POSTGRES_URL") or os.environ.get("POSTGRES_URL") or cfg.postgres_url)


def _validate_postgres_config(url: str) -> str:
    """Fail fast on missing/malformed/unreachable Postgres before any embedder work."""
    _require_postgres_url(url)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise CocoIndexUnavailable(f"invalid Postgres URL scheme {parsed.scheme!r}; expected postgres or postgresql")
    host = parsed.hostname
    if not host:
        raise CocoIndexUnavailable("invalid Postgres URL: missing host")
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=2.0):
            pass
    except OSError as exc:
        raise CocoIndexUnavailable(f"Postgres is unreachable at {host}:{port}: {exc}")
    return url


async def _pool_for(global_cfg: GlobalConfig, resources: CocoBackendResources | None = None) -> Any:
    if resources is not None:
        return await resources.get_pool()
    return await asyncpg.create_pool(_validate_postgres_config(_effective_postgres_url(global_cfg)))


def _validate_table_name(name: str) -> str:
    return validate_identifier(name, "table_name")


def effective_schema_name(project_cfg: ProjectConfig, global_cfg: GlobalConfig) -> str:
    return validate_identifier(project_cfg.schema_name or global_cfg.schema_name, "schema_name")


def effective_table_prefix(project_cfg: ProjectConfig, global_cfg: GlobalConfig) -> str:
    return validate_identifier(project_cfg.table_prefix or global_cfg.table_prefix, "table_prefix")


def effective_pipeline_version(global_cfg: GlobalConfig) -> str:
    return global_cfg.pipeline_version or CANONICAL_PIPELINE_VERSION


def _quote_ident(name: str) -> str:
    validate_identifier(name)
    return '"' + name.replace('"', '""') + '"'


def _qualified(schema_name: str, table_name: str) -> str:
    return f"{_quote_ident(schema_name)}.{_quote_ident(table_name)}"


def canonical_table(prefix: str, suffix: str) -> str:
    return validate_identifier(f"{prefix}_{suffix}", f"{suffix} table name")


def _stable_id(kind: str, *parts: object) -> str:
    material = kind + "\0" + "\0".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def repo_id_for(repo: Path) -> str:
    return _stable_id("repo", repo.resolve().as_posix())


def worktree_id_for(repo: Path, git_common_dir: str | None = None) -> str:
    root = repo.resolve().as_posix()
    return _stable_id("worktree", root, git_common_dir or "")


def branch_id_for(repo_id: str, branch_name: str, head_sha: str | None) -> str:
    return _stable_id("branch", repo_id, branch_name, head_sha or "")


def file_id_for(repo_id: str, branch_id: str, path: str) -> str:
    return _stable_id("file", repo_id, branch_id, path)


def chunk_id_for(file_id: str, start_byte: int, end_byte: int, code: str) -> str:
    return _stable_id("chunk", file_id, start_byte, end_byte, hashlib.sha256(code.encode("utf-8")).hexdigest())


def symbol_id_for(file_id: str, qualified_name: str, kind: str, start_line: int) -> str:
    return _stable_id("symbol", file_id, qualified_name, kind, start_line)


def symbol_embedding_id_for(symbol_id: str, embedding_model: str, embedding_text: str) -> str:
    return _stable_id("symbol_embedding", symbol_id, embedding_model, hashlib.sha256(embedding_text.encode("utf-8")).hexdigest())


def reference_id_for(repo_id: str, branch_id: str, file_id: str, name: str, kind: str, line: int, column: int) -> str:
    return _stable_id("reference", repo_id, branch_id, file_id, name, kind, line, column)


def call_edge_id_for(repo_id: str, branch_id: str, caller_symbol_id: str, callee_symbol_id: str, source_span: dict[str, int]) -> str:
    return _stable_id("call_edge", repo_id, branch_id, caller_symbol_id, callee_symbol_id, source_span.get("line"), source_span.get("column"))


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    running = 0
    for line in text.splitlines(keepends=True):
        running += len(line.encode("utf-8"))
        offsets.append(running)
    return offsets


def _byte_offset_for_line_col(offsets: list[int], line: int, col: int = 0) -> int:
    if line <= 1:
        return max(0, col)
    index = min(line - 1, len(offsets) - 1)
    return offsets[index] + max(0, col)


def _source_segment(text: str, start_line: int, end_line: int) -> str:
    lines = text.splitlines(keepends=True)
    return "".join(lines[max(0, start_line - 1): max(0, end_line)]).rstrip("\n")


def _clip_utf8(text: str | None, max_bytes: int) -> str | None:
    if text is None:
        return None
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[:max_bytes].decode("utf-8", errors="ignore")


def _module_name_for_path(path: str) -> str:
    no_suffix = str(Path(path).with_suffix(""))
    parts = [part for part in no_suffix.replace("\\", "/").split("/") if part]
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else Path(path).stem


def _visibility(name: str) -> str:
    return "private" if name.startswith("_") and not (name.startswith("__") and name.endswith("__")) else "public"


def _decorators(text: str, node: ast.AST) -> list[str]:
    values: list[str] = []
    for dec in getattr(node, "decorator_list", []):
        segment = ast.get_source_segment(text, dec) or ""
        if segment:
            values.append("@" + segment.strip().lstrip("@"))
    return values


def _signature_for_node(text: str, node: ast.AST) -> str | None:
    line = getattr(node, "lineno", None)
    if line is None:
        return None
    first = _source_segment(text, int(line), int(line)).strip()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return first.split(":", 1)[0] + (":" if ":" in first else "")
    if isinstance(node, ast.ClassDef):
        return first.split(":", 1)[0] + (":" if ":" in first else "")
    return None


def extract_ast_chunks(path: str, text: str, file_id: str, source_hash: str, cfg: ProjectConfig) -> AstExtraction:
    language = detect_code_language(filename=path) if detect_code_language is not None else None
    supported = language == "python" or path.endswith(".py")
    allowed = cfg.ast_languages is None or "python" in cfg.ast_languages
    if not supported or not allowed:
        return AstExtraction([], [], "unsupported_language" if not supported else "disabled")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return AstExtraction([], [], "parse_error", str(exc).split("\n", 1)[0][:240])

    offsets = _line_offsets(text)
    chunks: list[AstChunk] = []
    symbols: list[ExtractedSymbol] = []
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    module_name = _module_name_for_path(path)
    lineage = {
        "source": "ast_parser",
        "parser": "python_ast",
        "parser_version": "py-ast-v1",
        "extractor_version": "symbol-extractor-v1",
        "source_hash": source_hash,
        "generated_at": generated_at,
    }
    module_end = max(1, len(text.splitlines()))
    module_sid = symbol_id_for(file_id, module_name, "module", 1)
    module_meta: dict[str, object] = {
        "language": "python",
        "parser": "python_ast",
        "parser_version": "py-ast-v1",
        "extractor_version": "symbol-extractor-v1",
        "source_hash": source_hash,
        "module": module_name,
        "parent_symbol_id": None,
        "visibility": "public",
        "decorators": [],
        "is_async": False,
        "lineage": lineage,
        "freshness_status": "current",
        "confidence": 1.0,
    }
    module_doc = _clip_utf8(ast.get_docstring(tree), cfg.max_symbol_docstring_bytes)
    symbols.append(ExtractedSymbol(module_sid, Path(path).stem, module_name, "module", 1, module_end, None, module_doc, None, module_meta))

    def add_node(node: ast.AST, parents: list[tuple[str, str]], parent_symbol_id: str | None) -> None:
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        name = node.name
        in_class = any(parent_kind == "class" for _, parent_kind in parents)
        kind = "class" if isinstance(node, ast.ClassDef) else ("method" if in_class else "function")
        qname = ".".join([module_name, *[parent_name for parent_name, _ in parents], name])
        start_line = min([getattr(dec, "lineno", node.lineno) for dec in getattr(node, "decorator_list", [])] + [node.lineno])
        end_line = int(getattr(node, "end_lineno", node.lineno))
        start_byte = _byte_offset_for_line_col(offsets, start_line, 0)
        end_byte = _byte_offset_for_line_col(offsets, end_line + 1, 0)
        code = _source_segment(text, start_line, end_line)
        sid = symbol_id_for(file_id, qname, kind, start_line)
        docstring = _clip_utf8(ast.get_docstring(node), cfg.max_symbol_docstring_bytes)
        signature = _clip_utf8(_signature_for_node(text, node), cfg.max_symbol_signature_bytes)
        symbol_meta: dict[str, object] = {
            "language": "python",
            "parser": "python_ast",
            "parser_version": "py-ast-v1",
            "extractor_version": "symbol-extractor-v1",
            "source_hash": source_hash,
            "module": module_name,
            "parent_symbol_id": parent_symbol_id,
            "visibility": _visibility(name),
            "decorators": _decorators(text, node),
            "is_async": isinstance(node, ast.AsyncFunctionDef),
            "lineage": lineage,
            "freshness_status": "current",
            "confidence": 1.0,
        }
        if isinstance(node, ast.ClassDef):
            symbol_meta["bases"] = [ast.get_source_segment(text, base) or getattr(base, "id", "") for base in node.bases]
        symbols.append(ExtractedSymbol(sid, name, qname, kind, start_line, end_line, signature, docstring, parent_symbol_id, symbol_meta))
        chunk_role = "primary"
        chunk_strategy = "ast"
        if len(code.encode("utf-8")) > cfg.max_ast_chunk_bytes:
            code = "\n".join(code.splitlines()[: min(40, max(1, cfg.max_ast_chunk_bytes // 80))])
            end_line = start_line + max(0, len(code.splitlines()) - 1)
            end_byte = start_byte + len(code.encode("utf-8"))
            chunk_role = "body_fragment"
            chunk_strategy = "hybrid"
        metadata: dict[str, object] = {
            "language": "python",
            "chunk_strategy": chunk_strategy,
            "chunk_kind": kind,
            "chunk_role": chunk_role,
            "symbol_id": sid,
            "symbol": name,
            "qualified_name": qname,
            "symbol_kind": kind,
            "parent_symbol_id": parent_symbol_id,
            "definition_start_line": start_line,
            "definition_end_line": end_line,
            "context_start_line": max(1, start_line - cfg.ast_context_lines),
            "context_end_line": end_line + cfg.ast_context_lines,
            "lineage": lineage,
        }
        chunks.append(AstChunk(path, code, start_line, end_line, start_byte, end_byte, kind, chunk_role, sid, name, qname, kind, parent_symbol_id, signature, docstring, metadata))
        child_parents = [*parents, (name, kind)]
        for child in ast.iter_child_nodes(node):
            add_node(child, child_parents, sid)

    module_doc = ast.get_docstring(tree)
    if module_doc:
        first = (tree.body[0].lineno if tree.body else 1)
        end = int(getattr(tree.body[0], "end_lineno", first)) if tree.body else first
        code = _source_segment(text, first, end)
        chunks.append(AstChunk(path, code, first, end, _byte_offset_for_line_col(offsets, first, 0), _byte_offset_for_line_col(offsets, end + 1, 0), "module", "context", None, None, None, None, None, None, module_doc, {"language": "python", "chunk_strategy": "ast", "chunk_kind": "module", "chunk_role": "context", "lineage": lineage}))
    for child in tree.body:
        add_node(child, [], module_sid)
    return AstExtraction(chunks, symbols)


def _dotted_name(text: str, node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(text, node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return (ast.get_source_segment(text, node) or "").strip()


def _call_name(text: str, node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return _dotted_name(text, node.func)
    return _dotted_name(text, node)


def _build_import_maps(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    imports: dict[str, str] = {}
    stars: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                imports[alias.asname or root] = alias.name
                imports[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = "." * int(node.level or 0) + (node.module or "")
            for alias in node.names:
                if alias.name == "*":
                    stars.add(module)
                    continue
                imports[alias.asname or alias.name] = f"{module}.{alias.name}" if module else alias.name
    return imports, stars


async def populate_graph_canonical(conn: Any, repo: Path, project_cfg: ProjectConfig, global_cfg: GlobalConfig) -> dict[str, int]:
    if not (project_cfg.enable_symbols and project_cfg.enable_references and project_cfg.chunk_strategy in {"ast", "hybrid"}):
        return {"references": 0, "call_edges": 0}
    if "python" not in (project_cfg.reference_languages or ["python"]):
        return {"references": 0, "call_edges": 0}
    names = _canonical_names(project_cfg, global_cfg)
    q = lambda suffix: _qualified(names["schema"], names[suffix])
    ident = repo_identity(repo)
    rid = str(ident["repo_id"])
    bid = str(ident["branch_id"])
    symbol_rows = await conn.fetch(f"SELECT s.*, f.path AS filename FROM {q('symbols')} s JOIN {q('files')} f ON f.file_id=s.file_id WHERE s.repo_id=$1 AND s.branch_id=$2", rid, bid)
    by_qname: dict[str, list[object]] = {}
    by_name: dict[str, list[object]] = {}
    by_file: dict[str, list[object]] = {}
    for row in symbol_rows:
        by_qname.setdefault(str(row["qualified_name"]), []).append(row)
        by_name.setdefault(str(row["name"]), []).append(row)
        by_file.setdefault(str(row["filename"]), []).append(row)
    references = call_edges = 0

    def resolve_call(name: str, caller: object | None, path: str, imports: dict[str, str]) -> tuple[object | None, str, float, list[str]]:
        candidates: list[object] = []
        strategy = "unresolved"
        base = 0.0
        target_qname = imports.get(name, name)
        if target_qname in by_qname:
            candidates = by_qname[target_qname]
            strategy, base = "qualified_name", 0.88
        elif "." in name:
            head, tail = name.split(".", 1)
            imported = imports.get(head)
            if imported and f"{imported}.{tail}" in by_qname:
                candidates = by_qname[f"{imported}.{tail}"]
                strategy, base = "import_alias", 0.85
            elif name.startswith(("self.", "cls.")) and caller is not None:
                meta = _json_object(caller["metadata"])
                module = str(meta.get("module") or "")
                parts = str(caller["qualified_name"]).split(".")
                cls = next((part for part in reversed(parts[:-1]) if f"{module}.{part}.{name.split('.',1)[1]}" in by_qname), None)
                if cls:
                    candidates = by_qname[f"{module}.{cls}.{name.split('.',1)[1]}"]
                    strategy, base = ("self_method" if name.startswith("self.") else "cls_method"), (0.90 if name.startswith("self.") else 0.85)
        else:
            imported = imports.get(name)
            if imported and imported in by_qname:
                candidates = by_qname[imported]
                strategy, base = "import_alias", 0.80
            elif caller is not None:
                meta = _json_object(caller["metadata"])
                module = str(meta.get("module") or "")
                scoped = [f"{module}.{name}"]
                parent = meta.get("parent_symbol_id")
                if parent:
                    parent_rows = [row for row in by_file.get(path, []) if row["symbol_id"] == parent]
                    if parent_rows:
                        scoped.insert(0, f"{parent_rows[0]['qualified_name']}.{name}")
                for qn in scoped:
                    if qn in by_qname:
                        candidates = by_qname[qn]
                        strategy, base = "same_scope", 0.95
                        break
            if not candidates:
                same_file = [row for row in by_file.get(path, []) if row["name"] == name]
                if len(same_file) == 1:
                    candidates = same_file
                    strategy, base = "same_file_bare_name", 0.70
                elif len(by_name.get(name, [])) == 1:
                    candidates = by_name[name]
                    strategy, base = "bare_name", 0.65
        candidate_ids = [str(row["symbol_id"]) for row in candidates]
        if len(candidates) != 1:
            return None, "ambiguous" if candidates else strategy, max(0.0, base - 0.10 if candidates else 0.0), candidate_ids
        confidence = base + (0.03 if str(candidates[0]["qualified_name"]) == target_qname else 0.0) + (0.02 if str(candidates[0]["filename"]) == path else 0.0)
        return candidates[0], strategy, min(1.0, confidence), candidate_ids

    for file_path in iter_files(repo, project_cfg):
        path = file_path.relative_to(repo).as_posix()
        if not path.endswith(".py"):
            continue
        try:
            data = file_path.read_bytes()
            text = data.decode("utf-8", errors="replace")
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            continue
        fid = file_id_for(rid, bid, path)
        await conn.execute(f"DELETE FROM {q('references')} WHERE repo_id=$1 AND branch_id=$2 AND file_id=$3", rid, bid, fid)
        await conn.execute(f"DELETE FROM {q('call_edges')} WHERE repo_id=$1 AND branch_id=$2 AND metadata->'callsite'->>'file_id'=$3", rid, bid, fid)
        imports, stars = _build_import_maps(tree)
        file_symbols = sorted(by_file.get(path, []), key=lambda r: (int(r["start_line"]), -(int(r["end_line"]) - int(r["start_line"]))))

        def caller_for(node: ast.AST) -> object | None:
            line = int(getattr(node, "lineno", 1))
            containing = [row for row in file_symbols if int(row["start_line"]) <= line <= int(row["end_line"])]
            return sorted(containing, key=lambda r: int(r["end_line"]) - int(r["start_line"]))[0] if containing else None

        ref_nodes: list[tuple[ast.AST, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                ref_nodes.append((node, "call"))
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                ref_nodes.append((node, "name"))
            elif isinstance(node, ast.Attribute):
                ref_nodes.append((node, "attribute"))
            elif isinstance(node, ast.Import):
                ref_nodes.append((node, "import"))
            elif isinstance(node, ast.ImportFrom):
                ref_nodes.append((node, "import_from"))
        for node, kind in ref_nodes:
            line = int(getattr(node, "lineno", 1)); col = int(getattr(node, "col_offset", 0))
            name = _call_name(text, node) if kind == "call" else (getattr(node, "name", None) or _dotted_name(text, node) or kind)
            caller = caller_for(node)
            callee = None; strategy = "star_import" if stars and kind in {"import", "import_from"} else "unresolved"; confidence = 0.0; candidate_ids: list[str] = []
            if kind == "call":
                callee, strategy, confidence, candidate_ids = resolve_call(name, caller, path, imports)
            rid_ref = reference_id_for(rid, bid, fid, name, kind, line, col)
            span = {"start_line": line, "end_line": int(getattr(node, "end_lineno", line)), "start_col": col, "end_col": int(getattr(node, "end_col_offset", col))}
            metadata = {"language": "python", "parser": "python_ast", "parser_version": "py-ast-v1", "extractor_version": "reference-extractor-v1", "source_hash": hashlib.sha256(data).hexdigest(), "caller_symbol_id": caller["symbol_id"] if caller else None, "target_qualified_name": callee["qualified_name"] if callee else imports.get(name), "dotted_name": name, "span": span, "resolution": {"status": "resolved" if callee else ("ambiguous" if candidate_ids else "unresolved"), "strategy": strategy, "candidate_symbol_ids": candidate_ids, "confidence": confidence}, "freshness_status": "current"}
            await conn.execute(f"""
                INSERT INTO {q('references')}(reference_id, repo_id, branch_id, file_id, symbol_id, name, kind, line, column_number, metadata)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)
                ON CONFLICT (reference_id) DO UPDATE SET symbol_id=EXCLUDED.symbol_id, metadata=EXCLUDED.metadata
            """, rid_ref, rid, bid, fid, callee["symbol_id"] if callee else None, name, kind, line, col, json.dumps(metadata))
            references += 1
            if kind == "call" and caller is not None and callee is not None and confidence >= project_cfg.min_call_edge_confidence:
                source_span = {"line": line, "column": col}
                edge_id = call_edge_id_for(rid, bid, str(caller["symbol_id"]), str(callee["symbol_id"]), source_span)
                edge_meta = {"language": "python", "reference_id": rid_ref, "callsite": {"file_id": fid, "path": path, "filename": path, "line": line, "column": col}, "resolution_strategy": strategy, "edge_kind": "call", "direct": True, "recursive": caller["symbol_id"] == callee["symbol_id"], "freshness_status": "current", "confidence_factors": {"base": confidence, "qualified_name_bonus": 0.0, "same_file_bonus": 0.02 if caller["file_id"] == callee["file_id"] else 0.0, "ambiguity_penalty": 0.0, "freshness_penalty": 0.0}}
                await conn.execute(f"""
                    INSERT INTO {q('call_edges')}(edge_id, repo_id, branch_id, caller_symbol_id, callee_symbol_id, confidence, source, metadata)
                    VALUES($1,$2,$3,$4,$5,$6,'python_ast',$7::jsonb)
                    ON CONFLICT (edge_id) DO UPDATE SET confidence=EXCLUDED.confidence, metadata=EXCLUDED.metadata
                """, edge_id, rid, bid, caller["symbol_id"], callee["symbol_id"], confidence, json.dumps(edge_meta))
                call_edges += 1
    return {"references": references, "call_edges": call_edges}


def _line_for_offset(text: str, offset: int) -> int:
    offset = max(0, min(len(text), offset))
    return text.count("\n", 0, offset) + 1


def _chunk_offset(value: object, default: int) -> int:
    return int(getattr(value, "char_offset", value if isinstance(value, int) else default))


def _chunk_id(repo: str, filename: str, start: int, end: int, code: str) -> str:
    digest = hashlib.sha256(f"{repo}\0{filename}\0{start}\0{end}\0{code}".encode("utf-8")).hexdigest()
    return digest[:32]


def _as_pgvector(value: object) -> str:
    if hasattr(value, "tolist"):
        value = value.tolist()  # type: ignore[assignment]
    return "[" + ",".join(str(float(item)) for item in value) + "]"  # type: ignore[union-attr]


def _json_object(value: object) -> dict[str, object]:
    if hasattr(value, "items"):
        return dict(value)  # type: ignore[arg-type]
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if hasattr(decoded, "items"):
            return dict(decoded)
    return {}


def _symbol_embedding_text(sym: ExtractedSymbol, filename: str, cfg: ProjectConfig) -> str:
    doc = _clip_utf8(sym.docstring or "", cfg.max_symbol_docstring_bytes) or ""
    parts = [f"{sym.kind} {sym.qualified_name}", sym.signature or "", doc, filename]
    normalized = "\n".join(line.rstrip() for line in "\n".join(parts).replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return _clip_utf8(normalized, 8000) or ""


def _git_output(repo: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except Exception:  # noqa: BLE001 - git metadata is best-effort
        return None
    return result.stdout.strip() or None


def repo_identity(repo: Path) -> dict[str, object]:
    repo = repo.resolve()
    rid = repo_id_for(repo)
    git_common = _git_output(repo, "rev-parse", "--git-common-dir")
    common_real = str((repo / git_common).resolve()) if git_common and not Path(git_common).is_absolute() else (git_common or "")
    branch = _git_output(repo, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    head_sha = _git_output(repo, "rev-parse", "HEAD")
    default_ref = _git_output(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    default_branch = default_ref.rsplit("/", 1)[-1] if default_ref else None
    bid = branch_id_for(rid, branch, head_sha)
    return {
        "repo_id": rid,
        "worktree_id": worktree_id_for(repo, common_real),
        "branch": branch,
        "branch_id": bid,
        "head_sha": head_sha,
        "default_branch": default_branch,
    }


def _canonical_names(project_cfg: ProjectConfig, global_cfg: GlobalConfig) -> dict[str, str]:
    schema = effective_schema_name(project_cfg, global_cfg)
    prefix = effective_table_prefix(project_cfg, global_cfg)
    names = {"schema": schema, "prefix": prefix, "compat": _validate_table_name(project_cfg.table_name)}
    for suffix in ["repos", "branches", "files", "chunks", "symbols", "symbol_embeddings", "references", "call_edges", "repo_hierarchy", "test_links", "freshness", "schema_migrations"]:
        names[suffix] = canonical_table(prefix, suffix)
    return names


async def ensure_canonical_schema(conn: Any, project_cfg: ProjectConfig, global_cfg: GlobalConfig) -> None:
    names = _canonical_names(project_cfg, global_cfg)
    schema = _quote_ident(names["schema"])
    prefix = names["prefix"]
    q = lambda suffix: _qualified(names["schema"], names[suffix])
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    await conn.execute(f"""
    CREATE TABLE IF NOT EXISTS {q('repos')} (
      repo_id text PRIMARY KEY, root_path text NOT NULL, worktree_id text NOT NULL,
      vcs_kind text NOT NULL DEFAULT 'git', default_branch text,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb
    );
    CREATE UNIQUE INDEX IF NOT EXISTS {prefix}_repos_root_path_idx ON {q('repos')}(root_path);
    CREATE TABLE IF NOT EXISTS {q('branches')} (
      branch_id text PRIMARY KEY, repo_id text NOT NULL REFERENCES {q('repos')}(repo_id) ON DELETE CASCADE,
      name text NOT NULL, head_sha text, is_default boolean NOT NULL DEFAULT false,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb
    );
    CREATE UNIQUE INDEX IF NOT EXISTS {prefix}_branches_repo_name_head_idx ON {q('branches')}(repo_id, name, coalesce(head_sha, ''));
    CREATE INDEX IF NOT EXISTS {prefix}_branches_repo_idx ON {q('branches')}(repo_id);
    CREATE TABLE IF NOT EXISTS {q('files')} (
      file_id text PRIMARY KEY, repo_id text NOT NULL REFERENCES {q('repos')}(repo_id) ON DELETE CASCADE,
      branch_id text NOT NULL REFERENCES {q('branches')}(branch_id) ON DELETE CASCADE,
      path text NOT NULL, language text, sha256 text NOT NULL, mtime_ns bigint,
      size_bytes bigint NOT NULL DEFAULT 0, indexed_at timestamptz NOT NULL DEFAULT now(),
      metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb, UNIQUE(repo_id, branch_id, path)
    );
    CREATE INDEX IF NOT EXISTS {prefix}_files_repo_path_idx ON {q('files')}(repo_id, path);
    CREATE INDEX IF NOT EXISTS {prefix}_files_branch_idx ON {q('files')}(branch_id);
    CREATE TABLE IF NOT EXISTS {q('chunks')} (
      chunk_id text PRIMARY KEY, file_id text NOT NULL REFERENCES {q('files')}(file_id) ON DELETE CASCADE,
      repo_id text NOT NULL REFERENCES {q('repos')}(repo_id) ON DELETE CASCADE,
      branch_id text NOT NULL REFERENCES {q('branches')}(branch_id) ON DELETE CASCADE,
      path text NOT NULL, start_line integer NOT NULL, end_line integer NOT NULL,
      start_byte integer NOT NULL, end_byte integer NOT NULL, code text NOT NULL,
      embedding vector NOT NULL, chunk_kind text NOT NULL DEFAULT 'text', symbol_id text,
      token_count integer, metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      CHECK (start_line >= 1), CHECK (end_line >= start_line), CHECK (start_byte >= 0), CHECK (end_byte >= start_byte)
    );
    CREATE INDEX IF NOT EXISTS {prefix}_chunks_repo_path_idx ON {q('chunks')}(repo_id, path);
    CREATE INDEX IF NOT EXISTS {prefix}_chunks_file_idx ON {q('chunks')}(file_id);
    CREATE INDEX IF NOT EXISTS {prefix}_chunks_branch_idx ON {q('chunks')}(branch_id);
    CREATE TABLE IF NOT EXISTS {q('symbols')} (
      symbol_id text PRIMARY KEY, file_id text NOT NULL REFERENCES {q('files')}(file_id) ON DELETE CASCADE,
      repo_id text NOT NULL REFERENCES {q('repos')}(repo_id) ON DELETE CASCADE,
      branch_id text NOT NULL REFERENCES {q('branches')}(branch_id) ON DELETE CASCADE,
      name text NOT NULL, qualified_name text NOT NULL, kind text NOT NULL, start_line integer NOT NULL,
      end_line integer NOT NULL, signature text, docstring text, metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      UNIQUE(file_id, qualified_name, kind, start_line)
    );
    CREATE INDEX IF NOT EXISTS {prefix}_symbols_repo_name_idx ON {q('symbols')}(repo_id, name);
    CREATE INDEX IF NOT EXISTS {prefix}_symbols_qualified_idx ON {q('symbols')}(repo_id, qualified_name);
    CREATE INDEX IF NOT EXISTS {prefix}_symbols_repo_branch_kind_idx ON {q('symbols')}(repo_id, branch_id, kind);
    CREATE INDEX IF NOT EXISTS {prefix}_symbols_file_range_idx ON {q('symbols')}(file_id, start_line, end_line);
    CREATE TABLE IF NOT EXISTS {q('symbol_embeddings')} (
      symbol_embedding_id text PRIMARY KEY,
      symbol_id text NOT NULL REFERENCES {q('symbols')}(symbol_id) ON DELETE CASCADE,
      repo_id text NOT NULL REFERENCES {q('repos')}(repo_id) ON DELETE CASCADE,
      branch_id text NOT NULL REFERENCES {q('branches')}(branch_id) ON DELETE CASCADE,
      embedding vector NOT NULL,
      embedding_text text NOT NULL,
      metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(symbol_id)
    );
    CREATE INDEX IF NOT EXISTS {prefix}_symbol_embeddings_symbol_idx ON {q('symbol_embeddings')}(symbol_id);
    CREATE INDEX IF NOT EXISTS {prefix}_symbol_embeddings_repo_branch_idx ON {q('symbol_embeddings')}(repo_id, branch_id);
    CREATE TABLE IF NOT EXISTS {q('references')} (
      reference_id text PRIMARY KEY, repo_id text NOT NULL REFERENCES {q('repos')}(repo_id) ON DELETE CASCADE,
      branch_id text NOT NULL REFERENCES {q('branches')}(branch_id) ON DELETE CASCADE,
      file_id text NOT NULL REFERENCES {q('files')}(file_id) ON DELETE CASCADE,
      symbol_id text REFERENCES {q('symbols')}(symbol_id) ON DELETE SET NULL, name text NOT NULL,
      kind text NOT NULL, line integer NOT NULL, column_number integer NOT NULL DEFAULT 0, metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb
    );
    CREATE INDEX IF NOT EXISTS {prefix}_references_repo_name_idx ON {q('references')}(repo_id, name);
    CREATE INDEX IF NOT EXISTS {prefix}_references_symbol_idx ON {q('references')}(symbol_id);
    CREATE TABLE IF NOT EXISTS {q('call_edges')} (
      edge_id text PRIMARY KEY, repo_id text NOT NULL REFERENCES {q('repos')}(repo_id) ON DELETE CASCADE,
      branch_id text NOT NULL REFERENCES {q('branches')}(branch_id) ON DELETE CASCADE,
      caller_symbol_id text NOT NULL REFERENCES {q('symbols')}(symbol_id) ON DELETE CASCADE,
      callee_symbol_id text NOT NULL REFERENCES {q('symbols')}(symbol_id) ON DELETE CASCADE,
      confidence real NOT NULL DEFAULT 1.0, source text NOT NULL, metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      CHECK (confidence >= 0.0 AND confidence <= 1.0)
    );
    CREATE INDEX IF NOT EXISTS {prefix}_call_edges_repo_idx ON {q('call_edges')}(repo_id);
    CREATE INDEX IF NOT EXISTS {prefix}_call_edges_caller_idx ON {q('call_edges')}(caller_symbol_id);
    CREATE INDEX IF NOT EXISTS {prefix}_call_edges_callee_idx ON {q('call_edges')}(callee_symbol_id);
    CREATE TABLE IF NOT EXISTS {q('repo_hierarchy')} (
      node_id text PRIMARY KEY, repo_id text NOT NULL REFERENCES {q('repos')}(repo_id) ON DELETE CASCADE,
      branch_id text NOT NULL REFERENCES {q('branches')}(branch_id) ON DELETE CASCADE,
      parent_id text REFERENCES {q('repo_hierarchy')}(node_id) ON DELETE CASCADE,
      path text NOT NULL, node_kind text NOT NULL, name text NOT NULL, metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      UNIQUE(repo_id, branch_id, path, node_kind)
    );
    CREATE INDEX IF NOT EXISTS {prefix}_repo_hierarchy_parent_idx ON {q('repo_hierarchy')}(parent_id);
    CREATE TABLE IF NOT EXISTS {q('test_links')} (
      test_link_id text PRIMARY KEY, repo_id text NOT NULL REFERENCES {q('repos')}(repo_id) ON DELETE CASCADE,
      branch_id text NOT NULL REFERENCES {q('branches')}(branch_id) ON DELETE CASCADE,
      test_file_id text NOT NULL REFERENCES {q('files')}(file_id) ON DELETE CASCADE,
      source_file_id text NOT NULL REFERENCES {q('files')}(file_id) ON DELETE CASCADE,
      test_symbol_id text REFERENCES {q('symbols')}(symbol_id) ON DELETE SET NULL,
      source_symbol_id text REFERENCES {q('symbols')}(symbol_id) ON DELETE SET NULL,
      confidence real NOT NULL DEFAULT 0.5, metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      CHECK (confidence >= 0.0 AND confidence <= 1.0)
    );
    CREATE INDEX IF NOT EXISTS {prefix}_test_links_repo_idx ON {q('test_links')}(repo_id);
    CREATE INDEX IF NOT EXISTS {prefix}_test_links_source_idx ON {q('test_links')}(source_file_id);
    CREATE TABLE IF NOT EXISTS {q('freshness')} (
      freshness_id text PRIMARY KEY, repo_id text NOT NULL REFERENCES {q('repos')}(repo_id) ON DELETE CASCADE,
      branch_id text NOT NULL REFERENCES {q('branches')}(branch_id) ON DELETE CASCADE,
      file_id text REFERENCES {q('files')}(file_id) ON DELETE CASCADE, source_hash text NOT NULL,
      pipeline_version text NOT NULL, last_seen_at timestamptz NOT NULL DEFAULT now(), last_indexed_at timestamptz,
      status text NOT NULL, error text, metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
      CHECK (status IN ('current', 'stale', 'deleted', 'error', 'pending'))
    );
    CREATE INDEX IF NOT EXISTS {prefix}_freshness_repo_status_idx ON {q('freshness')}(repo_id, status);
    CREATE INDEX IF NOT EXISTS {prefix}_freshness_file_idx ON {q('freshness')}(file_id);
    CREATE TABLE IF NOT EXISTS {q('schema_migrations')} (
      version integer PRIMARY KEY, pipeline_version text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now(),
      metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb
    );
    """)
    try:
        await conn.execute(f"CREATE INDEX IF NOT EXISTS {prefix}_chunks_embedding_idx ON {q('chunks')} USING ivfflat (embedding vector_cosine_ops)")
    except Exception:  # noqa: BLE001 - pgvector cannot index dimensionless vectors on some versions until rows define dimensions
        pass
    await conn.execute(
        f"INSERT INTO {q('schema_migrations')}(version, pipeline_version) VALUES($1, $2) ON CONFLICT (version) DO UPDATE SET pipeline_version = EXCLUDED.pipeline_version",
        CANONICAL_SCHEMA_VERSION,
        effective_pipeline_version(global_cfg),
    )


if coco is not None:

    @coco.lifespan
    async def coco_lifespan(builder: coco.EnvironmentBuilder):
        builder.settings.db_path = data_home() / "cocoindex.db"
        async with await asyncpg.create_pool(_COCO_POSTGRES_URL) as pool:
            async with pool.acquire() as conn:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            builder.provide(PG_DB, pool)
            builder.provide(EMBEDDER, _shared_embedder(_COCO_EMBEDDING_MODEL))
            yield

    _splitter = RecursiveSplitter()

    @coco.fn
    async def process_chunk(
        chunk: object,
        repo: str,
        filename: str,
        full_text: str,
        min_chunk_size: int,
        table: postgres.TableTarget[CodeEmbedding],
    ) -> None:
        code = str(getattr(chunk, "text", ""))
        if min_chunk_size > 0 and len(code.strip()) < min_chunk_size:
            return
        start_offset = _chunk_offset(getattr(chunk, "start", None), 0)
        end_offset = _chunk_offset(getattr(chunk, "end", None), start_offset + len(code))
        table.declare_row(
            row=CodeEmbedding(
                id=_chunk_id(repo, filename, start_offset, end_offset, code),
                repo=repo,
                filename=filename,
                start_line=_line_for_offset(full_text, start_offset),
                end_line=_line_for_offset(full_text, max(start_offset, end_offset - 1)),
                code=code,
                embedding=await coco.use_context(EMBEDDER).embed(code),
            )
        )

    @coco.fn(memo=True)
    async def process_file(
        file: FileLike,
        table: postgres.TableTarget[CodeEmbedding],
        repo: str,
        chunk_size: int,
        min_chunk_size: int,
        chunk_overlap: int,
    ) -> None:
        text = await file.read_text()
        raw_path = Path(str(file.file_path.path))
        try:
            filename = raw_path.relative_to(Path(repo)).as_posix() if raw_path.is_absolute() else raw_path.as_posix()
        except ValueError:
            filename = str(file.file_path.path).replace(os.sep, "/")
        language = detect_code_language(filename=filename)
        chunks = _splitter.split(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap, language=language)
        await coco.map(process_chunk, chunks, repo, filename, text, min_chunk_size, table)

    @coco.fn
    async def app_main(
        sourcedir: Path,
        repo: str,
        table_name: str,
        include: list[str],
        exclude: list[str],
        chunk_size: int,
        min_chunk_size: int,
        chunk_overlap: int,
    ) -> None:
        table = await postgres.mount_table_target(
            PG_DB,
            table_name=_validate_table_name(table_name),
            table_schema=await postgres.TableSchema.from_class(CodeEmbedding, primary_key=["id"]),
        )
        table.declare_vector_index(column="embedding")
        files = localfs.walk_dir(
            sourcedir,
            recursive=True,
            path_matcher=PatternFilePathMatcher(included_patterns=include, excluded_patterns=exclude),
            live=True,
        )
        await coco.mount_each(process_file, files.items(), table, repo, chunk_size, min_chunk_size, chunk_overlap)


async def populate_canonical_from_legacy(conn: Any, repo: Path, project_cfg: ProjectConfig, global_cfg: GlobalConfig, embedder: Any | None = None) -> dict[str, int]:
    names = _canonical_names(project_cfg, global_cfg)
    q = lambda suffix: _qualified(names["schema"], names[suffix])
    compat = _qualified(names["schema"], names["compat"])
    ident = repo_identity(repo)
    rid = str(ident["repo_id"])
    bid = str(ident["branch_id"])
    pipeline_version = effective_pipeline_version(global_cfg)
    await conn.execute(
        f"""
        INSERT INTO {q('repos')}(repo_id, root_path, worktree_id, default_branch, metadata)
        VALUES($1, $2, $3, $4, $5::jsonb)
        ON CONFLICT (repo_id) DO UPDATE SET root_path = EXCLUDED.root_path, worktree_id = EXCLUDED.worktree_id,
          default_branch = EXCLUDED.default_branch, updated_at = now()
        """,
        rid,
        str(repo),
        str(ident["worktree_id"]),
        ident["default_branch"],
        "{}",
    )
    await conn.execute(
        f"""
        INSERT INTO {q('branches')}(branch_id, repo_id, name, head_sha, is_default, metadata)
        VALUES($1, $2, $3, $4, $5, $6::jsonb)
        ON CONFLICT (branch_id) DO UPDATE SET name = EXCLUDED.name, head_sha = EXCLUDED.head_sha,
          is_default = EXCLUDED.is_default, updated_at = now()
        """,
        bid,
        rid,
        ident["branch"],
        ident["head_sha"],
        bool(ident["default_branch"] and ident["branch"] == ident["default_branch"]),
        "{}",
    )
    await conn.execute(f"DELETE FROM {q('branches')} WHERE repo_id=$1 AND name=$2 AND branch_id<>$3", rid, ident["branch"], bid)
    legacy_exists = await conn.fetchval("SELECT to_regclass($1)", f"{names['schema']}.{names['compat']}")
    if not legacy_exists:
        return {"files": 0, "chunks": 0, "freshness_current": 0, "freshness_error": 0}
    rows = await conn.fetch(f"SELECT id, filename, start_line, end_line, code, embedding FROM {compat} WHERE repo = $1", str(repo))
    files_seen: set[str] = set()
    chunks = 0
    for row in rows:
        path = str(row["filename"])
        file_path = repo / path
        try:
            data = file_path.read_bytes()
            text = data.decode("utf-8", errors="replace")
            stat = file_path.stat()
            file_hash = hashlib.sha256(data).hexdigest()
            mtime_ns = stat.st_mtime_ns
            size_bytes = stat.st_size
        except OSError:
            text = ""
            file_hash = hashlib.sha256(str(row["code"]).encode("utf-8")).hexdigest()
            mtime_ns = None
            size_bytes = len(str(row["code"]).encode("utf-8"))
        fid = file_id_for(rid, bid, path)
        language = detect_code_language(filename=path) if detect_code_language is not None else None
        await conn.execute(
            f"""
            INSERT INTO {q('files')}(file_id, repo_id, branch_id, path, language, sha256, mtime_ns, size_bytes, metadata)
            VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            ON CONFLICT (file_id) DO UPDATE SET language = EXCLUDED.language, sha256 = EXCLUDED.sha256,
              mtime_ns = EXCLUDED.mtime_ns, size_bytes = EXCLUDED.size_bytes, indexed_at = now()
            """,
            fid,
            rid,
            bid,
            path,
            language,
            file_hash,
            mtime_ns,
            size_bytes,
            "{}",
        )
        files_seen.add(fid)
        code = str(row["code"])
        start_line = int(row["start_line"])
        end_line = int(row["end_line"])
        start_byte = len("\n".join(text.splitlines()[: max(0, start_line - 1)]).encode("utf-8"))
        if start_line > 1:
            start_byte += 1
        end_byte = start_byte + len(code.encode("utf-8"))
        cid = chunk_id_for(fid, start_byte, end_byte, code)
        metadata = {
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "pipeline_version": pipeline_version,
            "source": "recursive_splitter",
            "language": language,
        }
        await conn.execute(
            f"""
            INSERT INTO {q('chunks')}(chunk_id, file_id, repo_id, branch_id, path, start_line, end_line, start_byte, end_byte, code, embedding, chunk_kind, symbol_id, token_count, metadata)
            VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'text', NULL, $12, $13::jsonb)
            ON CONFLICT (chunk_id) DO UPDATE SET updated_at = now(), metadata = EXCLUDED.metadata
            """,
            cid,
            fid,
            rid,
            bid,
            path,
            start_line,
            end_line,
            start_byte,
            end_byte,
            code,
            row["embedding"],
            len(tokenize(code)),
            __import__("json").dumps(metadata),
        )
        chunks += 1
        freshness_id = _stable_id("freshness", rid, bid, fid, pipeline_version)
        await conn.execute(
            f"""
            INSERT INTO {q('freshness')}(freshness_id, repo_id, branch_id, file_id, source_hash, pipeline_version, last_indexed_at, status, metadata)
            VALUES($1, $2, $3, $4, $5, $6, now(), 'current', $7::jsonb)
            ON CONFLICT (freshness_id) DO UPDATE SET source_hash = EXCLUDED.source_hash,
              last_seen_at = now(), last_indexed_at = now(), status = 'current', error = NULL
            """,
            freshness_id,
            rid,
            bid,
            fid,
            file_hash,
            pipeline_version,
            "{}",
        )
    counts = {"files": len(files_seen), "chunks": chunks, "freshness_current": len(files_seen), "freshness_error": 0, "ast_chunks": 0, "recursive_chunks": chunks, "symbols": 0, "parser_errors": 0}
    if project_cfg.chunk_strategy in {"ast", "hybrid"} and embedder is not None:
        ast_counts = await populate_ast_canonical(conn, repo, project_cfg, global_cfg, embedder)
        counts.update(ast_counts)
    counts["deleted_files"] = await prune_missing_canonical_files(conn, repo, project_cfg, global_cfg)
    graph_counts = await populate_graph_canonical(conn, repo, project_cfg, global_cfg)
    counts.update(graph_counts)
    counts.update(await populate_quality_context_canonical(conn, repo, project_cfg, global_cfg))
    return counts


async def prune_missing_canonical_files(conn: Any, repo: Path, project_cfg: ProjectConfig, global_cfg: GlobalConfig) -> int:
    names = _canonical_names(project_cfg, global_cfg)
    ident = repo_identity(repo)
    rid = str(ident["repo_id"])
    bid = str(ident["branch_id"])
    files_table = _qualified(names["schema"], names["files"])
    current_paths = sorted(file.relative_to(repo).as_posix() for file in iter_files(repo, project_cfg))
    if current_paths:
        rows = await conn.fetch(f"DELETE FROM {files_table} WHERE repo_id=$1 AND branch_id=$2 AND NOT (path = ANY($3::text[])) RETURNING file_id", rid, bid, current_paths)
    else:
        rows = await conn.fetch(f"DELETE FROM {files_table} WHERE repo_id=$1 AND branch_id=$2 RETURNING file_id", rid, bid)
    return len(rows)


async def populate_ast_canonical(conn: Any, repo: Path, project_cfg: ProjectConfig, global_cfg: GlobalConfig, embedder: Any) -> dict[str, int]:
    names = _canonical_names(project_cfg, global_cfg)
    q = lambda suffix: _qualified(names["schema"], names[suffix])
    ident = repo_identity(repo)
    rid = str(ident["repo_id"])
    bid = str(ident["branch_id"])
    pipeline_version = effective_pipeline_version(global_cfg)
    ast_chunks = recursive_chunks = symbols_count = parser_errors = files_count = 0
    for file_path in iter_files(repo, project_cfg):
        path = file_path.relative_to(repo).as_posix()
        try:
            data = file_path.read_bytes()
            text = data.decode("utf-8", errors="replace")
            stat = file_path.stat()
        except OSError:
            continue
        source_hash = hashlib.sha256(data).hexdigest()
        fid = file_id_for(rid, bid, path)
        language = detect_code_language(filename=path) if detect_code_language is not None else None
        await conn.execute(
            f"""
            INSERT INTO {q('files')}(file_id, repo_id, branch_id, path, language, sha256, mtime_ns, size_bytes, metadata)
            VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            ON CONFLICT (file_id) DO UPDATE SET language = EXCLUDED.language, sha256 = EXCLUDED.sha256,
              mtime_ns = EXCLUDED.mtime_ns, size_bytes = EXCLUDED.size_bytes, indexed_at = now(), metadata = EXCLUDED.metadata
            """,
            fid, rid, bid, path, language, source_hash, stat.st_mtime_ns, stat.st_size,
            json.dumps({"pipeline_version": pipeline_version}),
        )
        await conn.execute(f"DELETE FROM {q('symbols')} WHERE file_id = $1", fid)
        await conn.execute(f"DELETE FROM {q('chunks')} WHERE file_id = $1", fid)
        extraction = extract_ast_chunks(path, text, fid, source_hash, project_cfg)
        if extraction.fallback_reason:
            if extraction.fallback_reason == "parse_error":
                parser_errors += 1
            chunks_to_insert = []
            for fallback in chunk_text(repo, file_path, project_cfg):
                start_byte = len("\n".join(text.splitlines()[: max(0, fallback.start_line - 1)]).encode("utf-8"))
                if fallback.start_line > 1:
                    start_byte += 1
                end_byte = start_byte + len(fallback.code.encode("utf-8"))
                metadata = {
                    "language": language,
                    "chunk_strategy": "recursive",
                    "chunk_kind": "text",
                    "chunk_role": "fallback",
                    "ast_fallback_reason": extraction.fallback_reason,
                    "parser_error": extraction.parser_error,
                    "lineage": {"source": "recursive_splitter", "parser": None, "parser_version": None, "extractor_version": "recursive-v1", "source_hash": source_hash, "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
                }
                chunks_to_insert.append((fallback.code, fallback.start_line, fallback.end_line, start_byte, end_byte, "text", None, metadata))
            recursive_chunks += len(chunks_to_insert)
        else:
            if project_cfg.enable_symbols:
                embedding_model = project_cfg.symbol_embedding_model or global_cfg.embedding_model
                for sym in extraction.symbols:
                    await conn.execute(
                        f"""
                        INSERT INTO {q('symbols')}(symbol_id, file_id, repo_id, branch_id, name, qualified_name, kind, start_line, end_line, signature, docstring, metadata)
                        VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
                        ON CONFLICT (symbol_id) DO UPDATE SET signature = EXCLUDED.signature, docstring = EXCLUDED.docstring, metadata = EXCLUDED.metadata
                        """,
                        sym.symbol_id, fid, rid, bid, sym.name, sym.qualified_name, sym.kind, sym.start_line, sym.end_line, sym.signature, sym.docstring, json.dumps(sym.metadata),
                    )
                    embedding_text = _symbol_embedding_text(sym, path, project_cfg)
                    vector = _as_pgvector(await embedder.embed(embedding_text))
                    await conn.execute(
                        f"""
                        INSERT INTO {q('symbol_embeddings')}(symbol_embedding_id, symbol_id, repo_id, branch_id, embedding, embedding_text, metadata)
                        VALUES($1, $2, $3, $4, $5::vector, $6, $7::jsonb)
                        ON CONFLICT (symbol_id) DO UPDATE SET embedding = EXCLUDED.embedding, embedding_text = EXCLUDED.embedding_text, metadata = EXCLUDED.metadata, updated_at = now()
                        """,
                        symbol_embedding_id_for(sym.symbol_id, embedding_model, embedding_text), sym.symbol_id, rid, bid, vector, embedding_text, json.dumps({"embedding_model": embedding_model}),
                    )
                symbols_count += len(extraction.symbols)
            chunks_to_insert = [(chunk.code, chunk.start_line, chunk.end_line, chunk.start_byte, chunk.end_byte, chunk.chunk_kind, chunk.symbol_id, chunk.metadata) for chunk in extraction.chunks]
            ast_chunks += len(chunks_to_insert)
        for code, start_line, end_line, start_byte, end_byte, chunk_kind, symbol_id, metadata in chunks_to_insert:
            cid = chunk_id_for(fid, int(start_byte), int(end_byte), str(code))
            vector = _as_pgvector(await embedder.embed(str(code)))
            await conn.execute(
                f"""
                INSERT INTO {q('chunks')}(chunk_id, file_id, repo_id, branch_id, path, start_line, end_line, start_byte, end_byte, code, embedding, chunk_kind, symbol_id, token_count, metadata)
                VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::vector, $12, $13, $14, $15::jsonb)
                ON CONFLICT (chunk_id) DO UPDATE SET updated_at = now(), metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding
                """,
                cid, fid, rid, bid, path, int(start_line), int(end_line), int(start_byte), int(end_byte), str(code), vector, str(chunk_kind), symbol_id, len(tokenize(str(code))), json.dumps(metadata),
            )
        files_count += 1
        freshness_id = _stable_id("freshness", rid, bid, fid, pipeline_version)
        freshness_status = "error" if extraction.fallback_reason == "parse_error" else "current"
        freshness_error = extraction.parser_error if extraction.fallback_reason == "parse_error" else None
        freshness_meta = {"parser_errors": parser_errors}
        if extraction.fallback_reason == "parse_error":
            freshness_meta.update({"parser": "python_ast", "ast_fallback_reason": "parse_error"})
        await conn.execute(
            f"""
            INSERT INTO {q('freshness')}(freshness_id, repo_id, branch_id, file_id, source_hash, pipeline_version, last_indexed_at, status, error, metadata)
            VALUES($1, $2, $3, $4, $5, $6, now(), $7, $8, $9::jsonb)
            ON CONFLICT (freshness_id) DO UPDATE SET source_hash = EXCLUDED.source_hash, last_seen_at = now(), last_indexed_at = now(), status = EXCLUDED.status, error = EXCLUDED.error, metadata = EXCLUDED.metadata
            """,
            freshness_id, rid, bid, fid, source_hash, pipeline_version, freshness_status, freshness_error, json.dumps(freshness_meta),
        )
    return {"files": files_count, "chunks": ast_chunks + recursive_chunks, "ast_chunks": ast_chunks, "recursive_chunks": recursive_chunks, "symbols": symbols_count, "parser_errors": parser_errors, "freshness_current": files_count, "freshness_error": 0}


async def populate_quality_context_canonical(conn: Any, repo: Path, project_cfg: ProjectConfig, global_cfg: GlobalConfig) -> dict[str, int]:
    names = _canonical_names(project_cfg, global_cfg)
    q = lambda suffix: _qualified(names["schema"], names[suffix])
    ident = repo_identity(repo); rid = str(ident["repo_id"]); bid = str(ident["branch_id"])
    rows = await conn.fetch(f"SELECT file_id, path, language FROM {q('files')} WHERE repo_id=$1 AND branch_id=$2", rid, bid)
    paths = sorted(str(r["path"]) for r in rows)
    file_ids = {str(r["path"]): str(r["file_id"]) for r in rows}
    nodes: dict[str, dict[str, object]] = {"": {"path": "", "node_kind": "root", "name": repo.name}}
    for path in paths:
        parts = Path(path).parts
        for i in range(1, len(parts) + 1):
            p = "/".join(parts[:i])
            is_file = p == path
            kind = "test_file" if is_file and context_tools.is_test_path(p) else "module" if is_file else "test_directory" if any(part.lower() in context_tools.TEST_DIRS for part in Path(p).parts) else "docs_directory" if Path(p).name.lower() in {"docs", "doc", "documentation"} else "directory"
            nodes[p] = {"path": p, "node_kind": kind, "name": Path(p).name}
    await conn.execute(f"DELETE FROM {q('repo_hierarchy')} WHERE repo_id=$1 AND branch_id=$2", rid, bid)
    ids: dict[str, str] = {}
    for p, node in sorted(nodes.items(), key=lambda item: (str(item[0]).count('/'), str(item[0]))):
        node_id = _stable_id("repo_hierarchy", rid, bid, str(node["node_kind"]), p)
        ids[p] = node_id
        parent_path = "/".join(Path(p).parts[:-1]) if p else ""
        parent_id = ids.get(parent_path) if p else None
        related = [f for f in paths if not p or f == p or f.startswith(p.rstrip('/') + '/')]
        metadata = {"language_counts": {}, "file_count": len(related), "symbol_count": 0, "test_count": sum(1 for f in related if context_tools.is_test_path(f)), "public_symbol_count": 0, "top_symbol_kinds": {}, "role": context_tools.role_for(p), "summary": f"{context_tools.role_for(p)} {node['node_kind']} with {len(related)} indexed file(s)", "confidence": 0.55, "evidence": ["path", "file_scan"], "source": "repo-map-v1"}
        await conn.execute(f"INSERT INTO {q('repo_hierarchy')}(node_id, repo_id, branch_id, parent_id, path, node_kind, name, metadata) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb) ON CONFLICT (node_id) DO UPDATE SET metadata=EXCLUDED.metadata", node_id, rid, bid, parent_id, p, node["node_kind"], node["name"], json.dumps(metadata))
    test_links = 0
    if project_cfg.enable_test_links:
        await conn.execute(f"DELETE FROM {q('test_links')} WHERE repo_id=$1 AND branch_id=$2", rid, bid)
        tests = [p for p in paths if context_tools.is_test_path(p)]
        sources = [p for p in paths if not context_tools.is_test_path(p)]
        for src in sources:
            stem = Path(src).stem
            candidates = [t for t in tests if stem in Path(t).stem or Path(t).name == f"test_{Path(src).name}"][:20]
            for test in candidates:
                tid = _stable_id("test_link", rid, bid, file_ids[test], file_ids[src], "", "")
                metadata = {"source": "test-link-heuristic-v1", "evidence": ["path_pattern", "name_overlap"], "score_components": {"path_proximity": 0.6, "import_or_reference_evidence": 0.0, "name_overlap": 0.6, "graph_reachability": 0.0, "freshness": 1.0}, "recommended_command": f"uv run pytest {test}" if test.endswith('.py') else "npm run test:ts", "framework": "pytest" if test.endswith('.py') else "unknown", "confidence_label": "medium"}
                await conn.execute(f"INSERT INTO {q('test_links')}(test_link_id, repo_id, branch_id, test_file_id, source_file_id, confidence, metadata) VALUES($1,$2,$3,$4,$5,$6,$7::jsonb) ON CONFLICT (test_link_id) DO UPDATE SET metadata=EXCLUDED.metadata, confidence=EXCLUDED.confidence", tid, rid, bid, file_ids[test], file_ids[src], 0.55, json.dumps(metadata))
                test_links += 1
    return {"repo_hierarchy_nodes": len(nodes), "test_files": sum(1 for p in paths if context_tools.is_test_path(p)), "test_links": test_links, "similarity_candidates": len(paths)}


def build_app(repo: Path, project_cfg: ProjectConfig | None = None, global_cfg: GlobalConfig | None = None):
    """Build the CocoIndex V1 app for a repository."""
    _require_coco()
    global _COCO_POSTGRES_URL, _COCO_EMBEDDING_MODEL
    repo = repo.resolve()
    project_cfg = project_cfg or load_project_config(repo)
    global_cfg = global_cfg or load_global_config()
    _COCO_POSTGRES_URL = _effective_postgres_url(global_cfg)
    _COCO_EMBEDDING_MODEL = global_cfg.embedding_model or _DEFAULT_MODEL
    table_name = _validate_table_name(project_cfg.table_name)
    app_name = "PiCodeIndex_" + hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:12]
    return coco.App(
        coco.AppConfig(name=app_name),
        app_main,
        sourcedir=repo,
        repo=str(repo),
        table_name=table_name,
        include=list(project_cfg.include),
        exclude=list(project_cfg.exclude),
        chunk_size=project_cfg.chunk_size,
        min_chunk_size=project_cfg.min_chunk_size,
        chunk_overlap=project_cfg.chunk_overlap,
    )


def refresh(repo: Path, project_cfg: ProjectConfig | None = None, global_cfg: GlobalConfig | None = None) -> dict[str, object]:
    repo = repo.resolve()
    project_cfg = project_cfg or load_project_config(repo)
    global_cfg = global_cfg or load_global_config()
    app = build_app(repo, project_cfg, global_cfg)
    with coco.runtime():
        try:
            app.update_blocking()
        except Exception as exc:  # noqa: BLE001 - CocoIndex may register state before seeing an existing target table
            if "already exists" not in str(exc):
                raise
            app.update_blocking()
    async def _migrate() -> dict[str, int]:
        pool = await _pool_for(global_cfg)
        try:
            async with pool.acquire() as conn:
                await ensure_canonical_schema(conn, project_cfg, global_cfg)
                embedder = _shared_embedder(global_cfg.embedding_model) if project_cfg.chunk_strategy in {"ast", "hybrid"} else None
                return await populate_canonical_from_legacy(conn, repo, project_cfg, global_cfg, embedder)
        finally:
            await pool.close()
    counts = asyncio.run(_migrate())
    refresh_lexical_index(repo)
    counts.setdefault("ast_chunks", 0)
    counts.setdefault("recursive_chunks", 0)
    counts.setdefault("symbols", 0)
    counts.setdefault("references", 0)
    counts.setdefault("call_edges", 0)
    counts.setdefault("test_links", 0)
    counts.setdefault("parser_errors", 0)
    return {
        "ok": True,
        "backend": "cocoindex",
        "repo": str(repo),
        "table_name": project_cfg.table_name,
        "schema_name": effective_schema_name(project_cfg, global_cfg),
        "table_prefix": effective_table_prefix(project_cfg, global_cfg),
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "pipeline_version": effective_pipeline_version(global_cfg),
        "chunk_strategy": project_cfg.chunk_strategy,
        "ast_languages": project_cfg.ast_languages,
        "repo_id": repo_id_for(repo),
        "branch": repo_identity(repo)["branch"],
        "branch_id": repo_identity(repo)["branch_id"],
        "embedding_model": global_cfg.embedding_model,
        "live": False,
        "counts": counts,
        "message": "CocoIndex catch-up refresh complete",
    }


def _rank_search_rows(rows: list[object], query: str, top_k: int, max_result_code_bytes: int = 12000) -> list[dict[str, object]]:
    """Rank vector candidates with semantic, lexical, symbol/path, and freshness components."""
    query_tokens = tokenize(query)
    deduped: dict[object, dict[str, object]] = {}
    for row in rows:
        filename = str(row["filename"])
        start_line = int(row["start_line"])
        end_line = int(row["end_line"])
        original_code = str(row["code"])
        code_bytes = original_code.encode("utf-8")
        code_truncated = len(code_bytes) > max_result_code_bytes
        code = code_bytes[:max_result_code_bytes].decode("utf-8", errors="ignore") if code_truncated else original_code
        semantic_score = float(row["score"])
        try:
            raw_metadata = row["metadata"]
        except (KeyError, TypeError):
            raw_metadata = None
        metadata = _json_object(raw_metadata)
        row_tokens = tokenize(f"{filename}\n{original_code}")
        lexical_score = score_tokens(query_tokens, row_tokens)
        symbol_text = " ".join(str(metadata.get(key) or "") for key in ("symbol", "qualified_name", "symbol_kind"))
        symbol_score = score_tokens(query_tokens, tokenize(symbol_text))
        path_score = score_tokens(query_tokens, tokenize(filename))
        chunk_kind = str(metadata.get("chunk_kind") or "text")
        chunk_kind_boost = {"function": 0.10, "method": 0.10, "class": 0.08, "module": 0.04, "docstring": 0.03, "text": 0.0}.get(chunk_kind, 0.0)
        freshness = str(metadata.get("freshness_status") or "unknown")
        freshness_penalty = {"stale": 0.25, "error": 0.35, "deleted": 1.0}.get(freshness, 0.0)
        matched_tokens = sorted(token for token in query_tokens if token in row_tokens)
        score = semantic_score + (0.50 * lexical_score) + (0.35 * symbol_score) + (0.15 * path_score) + chunk_kind_boost - freshness_penalty
        score = round(score, 6)
        chunk_id = metadata.get("chunk_id")
        key: object = chunk_id or (filename, start_line, end_line, original_code)
        previous = deduped.get(key)
        if previous is None or score > float(previous["score"]):
            metadata.setdefault("backend", "cocoindex")
            metadata.setdefault("freshness_status", freshness)
            metadata.setdefault("chunk_strategy", metadata.get("compatibility_mode") or "legacy")
            metadata.setdefault("lineage", {"source": "unknown", "parser": None, "parser_version": None, "extractor_version": None, "source_hash": None, "generated_at": None})
            metadata["ranking"] = {
                "semantic_score": round(semantic_score, 6),
                "lexical_score": round(lexical_score, 6),
                "symbol_score": round(symbol_score, 6),
                "path_score": round(path_score, 6),
                "chunk_kind_boost": round(chunk_kind_boost, 6),
                "freshness_penalty": round(freshness_penalty, 6),
                "final_score": score,
                "matched_tokens": matched_tokens,
            }
            metadata["truncation"] = {
                "code_truncated": code_truncated,
                "original_code_bytes": len(code_bytes),
                "returned_code_bytes": len(code.encode("utf-8")),
                "max_result_code_bytes": max_result_code_bytes,
            }
            item: dict[str, object] = {
                "score": score,
                "filename": filename,
                "start_line": start_line,
                "end_line": end_line,
                "code": code,
                "metadata": metadata,
            }
            if chunk_id:
                item["result_id"] = chunk_id
            deduped[key] = item
    return sorted(deduped.values(), key=lambda item: float(item["score"]), reverse=True)[: max(1, int(top_k))]


async def _search_async(
    repo: Path,
    query: str,
    top_k: int,
    project_cfg: ProjectConfig,
    global_cfg: GlobalConfig,
    resources: CocoBackendResources | None = None,
) -> dict[str, object]:
    _require_coco()
    table = _qualified(effective_schema_name(project_cfg, global_cfg), project_cfg.table_name)
    names = _canonical_names(project_cfg, global_cfg)
    chunks_table = _qualified(names["schema"], names["chunks"])
    freshness_table = _qualified(names["schema"], names["freshness"])
    ident = repo_identity(repo)
    if resources is None:
        _validate_postgres_config(_effective_postgres_url(global_cfg))
    embedder = resources.get_embedder() if resources is not None else _shared_embedder(global_cfg.embedding_model)
    embedding = _as_pgvector(await embedder.embed(query))
    token_patterns = [f"%{token}%" for token in list(tokenize(query))[:8]]
    pool = await _pool_for(global_cfg, resources)
    table_missing = False
    try:
        async with pool.acquire() as conn:
            if resources is None:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            canonical_exists = await conn.fetchval("SELECT to_regclass($1)", f"{names['schema']}.{names['chunks']}")
            canonical_count = 0
            if canonical_exists:
                canonical_count = int(await conn.fetchval(f"SELECT count(*) FROM {chunks_table} WHERE repo_id = $1 AND branch_id = $2", ident["repo_id"], ident["branch_id"]) or 0)
            compatibility_mode = "legacy"
            if canonical_count:
                strategies = await conn.fetch(f"SELECT DISTINCT coalesce(metadata->>'chunk_strategy', 'recursive') AS strategy FROM {chunks_table} WHERE repo_id = $1 AND branch_id = $2", ident["repo_id"], ident["branch_id"])
                strategy_values = {str(row["strategy"]) for row in strategies}
                if strategy_values == {"ast"}:
                    compatibility_mode = "ast"
                elif "ast" in strategy_values or "hybrid" in strategy_values:
                    compatibility_mode = "hybrid"
                else:
                    compatibility_mode = "recursive"
            try:
                if canonical_count:
                    vector_rows = await conn.fetch(
                        f"""
                        SELECT c.path AS filename, c.start_line, c.end_line, c.code,
                               1 - (c.embedding <=> $1::vector) AS score,
                               c.metadata || jsonb_build_object(
                                 'backend', 'cocoindex', 'schema_version', $4::int, 'pipeline_version', $5::text,
                                 'repo_id', c.repo_id, 'branch_id', c.branch_id, 'branch', $6::text, 'head_sha', $7::text,
                                 'file_id', c.file_id, 'chunk_id', c.chunk_id, 'language', coalesce(c.metadata->>'language', fi.language),
                                 'chunk_strategy', coalesce(c.metadata->>'chunk_strategy', 'recursive'),
                                 'chunk_kind', c.chunk_kind, 'symbol_id', c.symbol_id, 'symbol', s.name,
                                 'qualified_name', s.qualified_name, 'symbol_kind', s.kind, 'parent_symbol_id', c.metadata->>'parent_symbol_id',
                                 'start_byte', c.start_byte, 'end_byte', c.end_byte,
                                 'freshness_status', coalesce(f.status, 'current'), 'lineage', coalesce(c.metadata->'lineage', '{{}}'::jsonb)
                               ) AS metadata
                        FROM {chunks_table} c
                        LEFT JOIN {freshness_table} f ON f.file_id = c.file_id AND f.pipeline_version = $5
                        LEFT JOIN {_qualified(names['schema'], names['symbols'])} s ON s.symbol_id = c.symbol_id
                        LEFT JOIN {_qualified(names['schema'], names['files'])} fi ON fi.file_id = c.file_id
                        WHERE c.repo_id = $2 AND c.branch_id = $3 AND coalesce(f.status, 'current') <> 'deleted'
                        ORDER BY c.embedding <=> $1::vector
                        LIMIT $8
                        """,
                        embedding,
                        ident["repo_id"],
                        ident["branch_id"],
                        CANONICAL_SCHEMA_VERSION,
                        effective_pipeline_version(global_cfg),
                        ident["branch"],
                        ident["head_sha"],
                        max(100, int(top_k) * 20),
                    )
                    lexical_rows = await conn.fetch(
                        f"""
                        SELECT c.path AS filename, c.start_line, c.end_line, c.code,
                               1 - (c.embedding <=> $1::vector) AS score,
                               c.metadata || jsonb_build_object(
                                 'backend', 'cocoindex', 'schema_version', $5::int, 'pipeline_version', $6::text,
                                 'repo_id', c.repo_id, 'branch_id', c.branch_id, 'branch', $7::text, 'head_sha', $8::text,
                                 'file_id', c.file_id, 'chunk_id', c.chunk_id, 'language', coalesce(c.metadata->>'language', fi.language),
                                 'chunk_strategy', coalesce(c.metadata->>'chunk_strategy', 'recursive'),
                                 'chunk_kind', c.chunk_kind, 'symbol_id', c.symbol_id, 'symbol', s.name,
                                 'qualified_name', s.qualified_name, 'symbol_kind', s.kind, 'parent_symbol_id', c.metadata->>'parent_symbol_id',
                                 'start_byte', c.start_byte, 'end_byte', c.end_byte,
                                 'freshness_status', coalesce(f.status, 'current'), 'lineage', coalesce(c.metadata->'lineage', '{{}}'::jsonb)
                               ) AS metadata
                        FROM {chunks_table} c
                        LEFT JOIN {freshness_table} f ON f.file_id = c.file_id AND f.pipeline_version = $6
                        LEFT JOIN {_qualified(names['schema'], names['symbols'])} s ON s.symbol_id = c.symbol_id
                        LEFT JOIN {_qualified(names['schema'], names['files'])} fi ON fi.file_id = c.file_id
                        WHERE c.repo_id = $2 AND c.branch_id = $3 AND coalesce(f.status, 'current') <> 'deleted'
                          AND ($4::text[] = '{{}}'::text[] OR c.path ILIKE ANY($4::text[]) OR c.code ILIKE ANY($4::text[]) OR s.name ILIKE ANY($4::text[]) OR s.qualified_name ILIKE ANY($4::text[]))
                        LIMIT $9
                        """,
                        embedding,
                        ident["repo_id"],
                        ident["branch_id"],
                        token_patterns,
                        CANONICAL_SCHEMA_VERSION,
                        effective_pipeline_version(global_cfg),
                        ident["branch"],
                        ident["head_sha"],
                        5000,
                    )
                else:
                    vector_rows = await conn.fetch(
                        f"""
                        SELECT filename, start_line, end_line, code,
                               1 - (embedding <=> $1::vector) AS score,
                               jsonb_build_object('backend', 'cocoindex', 'compatibility_mode', 'legacy', 'chunk_strategy', 'legacy', 'freshness_status', 'unknown') AS metadata
                        FROM {table}
                        WHERE repo = $2
                        ORDER BY embedding <=> $1::vector
                        LIMIT $3
                        """,
                        embedding,
                        str(repo),
                        max(100, int(top_k) * 20),
                    )
                    lexical_rows = await conn.fetch(
                        f"""
                        SELECT filename, start_line, end_line, code,
                               1 - (embedding <=> $1::vector) AS score,
                               jsonb_build_object('backend', 'cocoindex', 'compatibility_mode', 'legacy', 'chunk_strategy', 'legacy', 'freshness_status', 'unknown') AS metadata
                        FROM {table}
                        WHERE repo = $2
                          AND ($3::text[] = '{{}}'::text[] OR filename ILIKE ANY($3::text[]) OR code ILIKE ANY($3::text[]))
                        LIMIT $4
                        """,
                        embedding,
                        str(repo),
                        token_patterns,
                        5000,
                    )
                rows = list(vector_rows) + list(lexical_rows)
            except asyncpg.UndefinedTableError:
                rows = []
                table_missing = True
                compatibility_mode = "fallback"
    finally:
        if resources is None:
            await pool.close()
    candidate_limit = max(100, int(top_k) * 20)
    results = _rank_search_rows(rows, query, top_k, project_cfg.max_result_code_bytes)
    omitted_candidates = max(0, len(rows) - len(results))
    return {
        "ok": True,
        "backend": "cocoindex",
        "query": query,
        "top_k": top_k,
        "refresh": False,
        "repo": str(repo),
        "results": results,
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "pipeline_version": effective_pipeline_version(global_cfg),
        "repo_id": ident["repo_id"],
        "branch": ident["branch"],
        "branch_id": ident["branch_id"],
        "compatibility_mode": compatibility_mode,
        "ranking_profile": "semantic_ast_v1",
        "truncated": any(bool(result.get("metadata", {}).get("truncation", {}).get("code_truncated")) for result in results) or omitted_candidates > 0,
        "truncation": {
            "candidate_limit": candidate_limit,
            "result_code_bytes_limit": project_cfg.max_result_code_bytes,
            "omitted_candidates": omitted_candidates,
        },
        "warning": None if results else (
            "CocoIndex table not found; run `pi-code-index refresh --json`" if table_missing
            else "no CocoIndex matches found; run `pi-code-index refresh --json` if the index is stale"
        ),
    }


def search(repo: Path, query: str, top_k: int = 8, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]:
    repo = repo.resolve()
    project_cfg = load_project_config(repo)
    global_cfg = load_global_config()
    if refresh_first:
        refresh(repo, project_cfg, global_cfg)
    payload = resources.run(_search_async(repo, query, top_k, project_cfg, global_cfg, resources)) if resources is not None else asyncio.run(_search_async(repo, query, top_k, project_cfg, global_cfg))
    payload["refresh"] = refresh_first
    return payload

_VALID_SYMBOL_KINDS = {"function", "class", "method", "module"}


def _symbol_base_payload(repo: Path, operation: str, project_cfg: ProjectConfig, global_cfg: GlobalConfig, warning: str | None = None) -> dict[str, object]:
    ident = repo_identity(repo)
    return {
        "ok": True,
        "backend": "cocoindex",
        "operation": operation,
        "repo": str(repo),
        "repo_id": ident["repo_id"],
        "branch": ident["branch"],
        "branch_id": ident["branch_id"],
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "pipeline_version": effective_pipeline_version(global_cfg),
        "capabilities": {"symbols": project_cfg.enable_symbols, "symbol_search": project_cfg.enable_symbols, "symbol_definition": project_cfg.enable_symbols, "symbol_context": project_cfg.enable_symbols, "symbol_embeddings": project_cfg.enable_symbols, "references": project_cfg.enable_references, "call_graph": project_cfg.enable_symbols and project_cfg.enable_references, "impact_analysis": project_cfg.enable_symbols and project_cfg.enable_references, "test_links": project_cfg.enable_test_links, "languages": ["python"]},
        "warning": warning,
    }


def _symbol_item(row: object, score: float | None = None, ranking: dict[str, object] | None = None) -> dict[str, object]:
    metadata = _json_object(row["metadata"])
    if ranking is not None:
        metadata["ranking"] = ranking
    item: dict[str, object] = {
        "symbol_id": row["symbol_id"],
        "name": row["name"],
        "qualified_name": row["qualified_name"],
        "kind": row["kind"],
        "language": metadata.get("language") or "python",
        "filename": row["filename"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
        "signature": row["signature"],
        "docstring": row["docstring"],
        "metadata": metadata,
    }
    if "start_byte" in row:
        item["start_byte"] = row["start_byte"]
        item["end_byte"] = row["end_byte"]
    if score is not None:
        item["score"] = round(score, 6)
    if "code" in row:
        item["code"] = row["code"]
    return item


def _rank_symbol_rows(rows: list[object], query: str, top_k: int) -> list[dict[str, object]]:
    q = query.lower().strip()
    qtokens = tokenize(query)
    ranked: list[dict[str, object]] = []
    for row in rows:
        name = str(row["name"])
        qname = str(row["qualified_name"])
        filename = str(row["filename"])
        sig = str(row["signature"] or "")
        doc = str(row["docstring"] or "")
        semantic = float(row["semantic_score"] or 0.0) if "semantic_score" in row else 0.0
        exact = 1.0 if q == name.lower() else (0.8 if q == qname.lower().rsplit('.', 1)[-1] else 0.0)
        qualified = 1.0 if q == qname.lower() else (0.7 if q and q in qname.lower() else 0.0)
        token_score = score_tokens(qtokens, tokenize(f"{name} {qname} {sig} {filename}"))
        signature_score = score_tokens(qtokens, tokenize(sig))
        docstring_score = score_tokens(qtokens, tokenize(doc))
        path_score = score_tokens(qtokens, tokenize(filename))
        meta = _json_object(row["metadata"])
        freshness = str(meta.get("freshness_status") or "current")
        freshness_penalty = {"stale": 0.25, "error": 0.35, "deleted": 1.0}.get(freshness, 0.0)
        matched = sorted(token for token in qtokens if token in tokenize(f"{name} {qname} {sig} {doc} {filename}"))
        final = max(0.0, 2.0 * exact + 1.5 * qualified + token_score + 1.2 * semantic + 0.4 * signature_score + 0.3 * docstring_score + 0.2 * path_score - freshness_penalty)
        ranking = {"exact_name_score": exact, "qualified_name_score": qualified, "token_score": round(token_score, 6), "semantic_score": round(semantic, 6), "signature_score": round(signature_score, 6), "docstring_score": round(docstring_score, 6), "path_score": round(path_score, 6), "freshness_penalty": freshness_penalty, "final_score": round(final, 6), "matched_tokens": matched}
        if not semantic:
            ranking["semantic_unavailable"] = True
        ranked.append(_symbol_item(row, final, ranking))
    return sorted(ranked, key=lambda item: float(item.get("score", 0.0)), reverse=True)[: max(1, min(int(top_k), 50))]


def _validate_symbol_filters(filters: dict[str, object] | None) -> dict[str, object]:
    result = dict(filters or {})
    kinds = result.get("kind")
    if kinds is not None:
        values = [str(kinds)] if isinstance(kinds, str) else [str(item) for item in kinds]  # type: ignore[union-attr]
        invalid = [value for value in values if value not in _VALID_SYMBOL_KINDS]
        if invalid:
            raise ValueError(f"unsupported symbol kind filter: {', '.join(invalid)}")
        result["kind"] = values
    return result


async def _symbol_search_async(repo: Path, query: str, top_k: int, filters: dict[str, object] | None, project_cfg: ProjectConfig, global_cfg: GlobalConfig, resources: CocoBackendResources | None = None) -> dict[str, object]:
    _require_coco()
    if top_k < 1:
        raise ValueError("top_k must be >= 1")
    top_k = min(top_k, 50)
    filters = _validate_symbol_filters(filters)
    payload = _symbol_base_payload(repo, "symbol_search", project_cfg, global_cfg)
    payload.update({"query": query, "top_k": top_k, "filters": filters, "results": [], "truncated": False, "truncation": {"candidate_limit": max(top_k * 20, 100), "omitted_candidates": 0}})
    if not project_cfg.enable_symbols:
        payload["warning"] = "symbol intelligence is disabled; set enable_symbols: true"
        return payload
    names = _canonical_names(project_cfg, global_cfg)
    ident = repo_identity(repo)
    pool = await _pool_for(global_cfg, resources)
    try:
        async with pool.acquire() as conn:
            if not await conn.fetchval("SELECT to_regclass($1)", f"{names['schema']}.{names['symbols']}"):
                payload["warning"] = "symbol tables are not available; run pi-code-index refresh with enable_symbols=true"
                return payload
            clauses = ["s.repo_id = $1", "s.branch_id = $2"]
            params: list[object] = [ident["repo_id"], ident["branch_id"]]
            if filters.get("kind"):
                params.append(filters["kind"])
                clauses.append(f"s.kind = ANY(${len(params)}::text[])")
            if filters.get("language"):
                params.append(str(filters["language"]))
                clauses.append(f"s.metadata->>'language' = ${len(params)}")
            patterns = [f"%{token}%" for token in list(tokenize(query))[:8]]
            params.append(patterns)
            pattern_idx = len(params)
            limit = min(500, max(top_k * 20, 100))
            params.append(limit)
            rows = await conn.fetch(f"""
                SELECT s.*, f.path AS filename, c.start_byte, c.end_byte,
                       coalesce(1 - (se.embedding <=> NULL::vector), 0) AS semantic_score
                FROM { _qualified(names['schema'], names['symbols']) } s
                JOIN { _qualified(names['schema'], names['files']) } f ON f.file_id = s.file_id
                LEFT JOIN { _qualified(names['schema'], names['chunks']) } c ON c.symbol_id = s.symbol_id
                LEFT JOIN { _qualified(names['schema'], names['symbol_embeddings']) } se ON se.symbol_id = s.symbol_id
                WHERE {' AND '.join(clauses)}
                  AND (${pattern_idx}::text[] = '{{}}'::text[] OR s.name ILIKE ANY(${pattern_idx}::text[]) OR s.qualified_name ILIKE ANY(${pattern_idx}::text[]) OR coalesce(s.signature, '') ILIKE ANY(${pattern_idx}::text[]) OR f.path ILIKE ANY(${pattern_idx}::text[]))
                ORDER BY s.qualified_name
                LIMIT ${len(params)}
            """, *params)
    finally:
        if resources is None:
            await pool.close()
    payload["results"] = _rank_symbol_rows(list(rows), query, top_k)
    payload["truncation"] = {"candidate_limit": limit, "omitted_candidates": max(0, len(rows) - len(payload["results"]))}
    if not rows:
        payload["warning"] = "no symbols found; run pi-code-index refresh with enable_symbols=true"
    return payload


def _parse_target(target: object) -> dict[str, object]:
    if isinstance(target, dict):
        return target
    text = str(target)
    if text.strip().startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSON target: {exc}") from exc
    match = re.match(r"^(?P<filename>.+?):(?P<line>\d+)(?::(?P<column>\d+))?$", text)
    if match:
        return {"filename": match.group("filename"), "line": int(match.group("line")), "column": int(match.group("column") or 0)}
    if re.fullmatch(r"[0-9a-f]{32}", text):
        return {"symbol_id": text}
    return {"qualified_name": text}


async def _resolve_symbol(conn: Any, repo: Path, target: object, project_cfg: ProjectConfig, global_cfg: GlobalConfig, filters: dict[str, object] | None = None) -> tuple[dict[str, object] | None, list[dict[str, object]], str | None]:
    names = _canonical_names(project_cfg, global_cfg)
    ident = repo_identity(repo)
    parsed = _parse_target(target)
    base = f"FROM {_qualified(names['schema'], names['symbols'])} s JOIN {_qualified(names['schema'], names['files'])} f ON f.file_id = s.file_id LEFT JOIN {_qualified(names['schema'], names['chunks'])} c ON c.symbol_id = s.symbol_id WHERE s.repo_id = $1 AND s.branch_id = $2"
    select = "SELECT s.*, f.path AS filename, c.start_byte, c.end_byte, c.code "
    if parsed.get("symbol_id"):
        rows = await conn.fetch(select + base + " AND s.symbol_id = $3", ident["repo_id"], ident["branch_id"], parsed["symbol_id"])
    elif parsed.get("filename"):
        rows = await conn.fetch(select + base + " AND f.path = $3 AND s.start_line <= $4 AND s.end_line >= $4 ORDER BY (s.end_line - s.start_line) ASC LIMIT 5", ident["repo_id"], ident["branch_id"], parsed["filename"], int(parsed["line"]))
        if rows:
            smallest_span = min(int(row["end_line"]) - int(row["start_line"]) for row in rows)
            rows = [row for row in rows if int(row["end_line"]) - int(row["start_line"]) == smallest_span]
        else:
            rows = await conn.fetch(select + base + " AND f.path = $3 AND s.kind = 'module' LIMIT 1", ident["repo_id"], ident["branch_id"], parsed["filename"])
    else:
        qname = str(parsed.get("qualified_name") or parsed.get("name") or "")
        rows = await conn.fetch(select + base + " AND (s.qualified_name = $3 OR s.name = $3) ORDER BY CASE WHEN s.qualified_name = $3 THEN 0 ELSE 1 END, s.qualified_name LIMIT 20", ident["repo_id"], ident["branch_id"], qname)
        if not rows and "." in qname:
            rows = await conn.fetch(select + base + " AND right(s.qualified_name, length($3::text) + 1) = '.' || $3::text ORDER BY s.qualified_name LIMIT 20", ident["repo_id"], ident["branch_id"], qname)
    items = [_symbol_item(row) for row in rows]
    if len(items) == 1:
        return items[0], [], None
    if len(items) > 1:
        return None, items, "ambiguous target; retry with symbol_id or qualified_name"
    return None, [], "symbol target not found"


async def _symbol_definition_async(repo: Path, target: object, filters: dict[str, object] | None, project_cfg: ProjectConfig, global_cfg: GlobalConfig, resources: CocoBackendResources | None = None) -> dict[str, object]:
    _require_coco()
    payload = _symbol_base_payload(repo, "symbol_definition", project_cfg, global_cfg)
    payload.update({"target": target, "definition": None, "matches": []})
    if not project_cfg.enable_symbols:
        payload["warning"] = "symbol intelligence is disabled; set enable_symbols: true"
        return payload
    names = _canonical_names(project_cfg, global_cfg)
    pool = await _pool_for(global_cfg, resources)
    try:
        async with pool.acquire() as conn:
            if not await conn.fetchval("SELECT to_regclass($1)", f"{names['schema']}.{names['symbols']}"):
                payload["warning"] = "symbol tables are not available; run pi-code-index refresh with enable_symbols=true"
                return payload
            definition, matches, warning = await _resolve_symbol(conn, repo, target, project_cfg, global_cfg, filters)
    finally:
        if resources is None:
            await pool.close()
    payload["definition"] = definition
    payload["matches"] = matches
    payload["warning"] = warning
    return payload


async def _symbol_context_async(repo: Path, target: object, depth: int, filters: dict[str, object] | None, project_cfg: ProjectConfig, global_cfg: GlobalConfig, resources: CocoBackendResources | None = None) -> dict[str, object]:
    _require_coco()
    depth = max(0, min(int(depth), 5))
    payload = _symbol_base_payload(repo, "symbol_context", project_cfg, global_cfg)
    payload.update({"target": target, "target_symbol_id": None, "symbol": None, "parents": [], "children": [], "siblings": [], "module_symbols": [], "chunks": [], "references_available": False})
    if not project_cfg.enable_symbols:
        payload["warning"] = "symbol intelligence is disabled; set enable_symbols: true"
        return payload
    names = _canonical_names(project_cfg, global_cfg)
    pool = await _pool_for(global_cfg, resources)
    try:
        async with pool.acquire() as conn:
            symbol, matches, warning = await _resolve_symbol(conn, repo, target, project_cfg, global_cfg, filters)
            if not symbol:
                payload["matches"] = matches
                payload["warning"] = warning
                return payload
            sid = str(symbol["symbol_id"])
            payload["symbol"] = symbol
            payload["target_symbol_id"] = sid
            parent_id = symbol.get("metadata", {}).get("parent_symbol_id") if isinstance(symbol.get("metadata"), dict) else None
            async def rows(sql: str, *params: object) -> list[dict[str, object]]:
                return [_symbol_item(row) for row in await conn.fetch(sql, *params)]
            common = f"SELECT s.*, f.path AS filename, c.start_byte, c.end_byte FROM {_qualified(names['schema'], names['symbols'])} s JOIN {_qualified(names['schema'], names['files'])} f ON f.file_id=s.file_id LEFT JOIN {_qualified(names['schema'], names['chunks'])} c ON c.symbol_id=s.symbol_id"
            if parent_id:
                payload["parents"] = await rows(common + " WHERE s.symbol_id = $1", parent_id)
                payload["siblings"] = await rows(common + " WHERE s.metadata->>'parent_symbol_id' = $1 AND s.symbol_id <> $2 ORDER BY s.start_line LIMIT 50", parent_id, sid)
            payload["children"] = await rows(common + " WHERE s.metadata->>'parent_symbol_id' = $1 ORDER BY s.start_line LIMIT 200", sid)
            module = str(symbol.get("metadata", {}).get("module") if isinstance(symbol.get("metadata"), dict) else "")
            payload["module_symbols"] = await rows(common + " WHERE s.qualified_name LIKE $1 AND s.symbol_id <> $2 ORDER BY s.start_line LIMIT 100", module + ".%", sid) if module else []
            chunk_rows = await conn.fetch(f"SELECT chunk_id, path AS filename, start_line, end_line, chunk_kind, metadata FROM {_qualified(names['schema'], names['chunks'])} WHERE symbol_id = $1 ORDER BY start_line LIMIT 20", sid)
            payload["chunks"] = [{"chunk_id": r["chunk_id"], "filename": r["filename"], "start_line": r["start_line"], "end_line": r["end_line"], "chunk_kind": r["chunk_kind"], "chunk_role": _json_object(r["metadata"]).get("chunk_role", "primary"), "metadata": _json_object(r["metadata"])} for r in chunk_rows]
    finally:
        if resources is None:
            await pool.close()
    return payload


def symbol_search(repo: Path, query: str, top_k: int = 8, filters: dict[str, object] | None = None, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]:
    repo = repo.resolve()
    project_cfg = load_project_config(repo)
    global_cfg = load_global_config()
    if refresh_first:
        refresh(repo, project_cfg, global_cfg)
    return resources.run(_symbol_search_async(repo, query, top_k, filters, project_cfg, global_cfg, resources)) if resources is not None else asyncio.run(_symbol_search_async(repo, query, top_k, filters, project_cfg, global_cfg))


def symbol_definition(repo: Path, target: object, filters: dict[str, object] | None = None, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]:
    repo = repo.resolve()
    project_cfg = load_project_config(repo)
    global_cfg = load_global_config()
    if refresh_first:
        refresh(repo, project_cfg, global_cfg)
    return resources.run(_symbol_definition_async(repo, target, filters, project_cfg, global_cfg, resources)) if resources is not None else asyncio.run(_symbol_definition_async(repo, target, filters, project_cfg, global_cfg))


def symbol_context(repo: Path, target: object, depth: int = 1, filters: dict[str, object] | None = None, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]:
    repo = repo.resolve()
    project_cfg = load_project_config(repo)
    global_cfg = load_global_config()
    if refresh_first:
        refresh(repo, project_cfg, global_cfg)
    return resources.run(_symbol_context_async(repo, target, depth, filters, project_cfg, global_cfg, resources)) if resources is not None else asyncio.run(_symbol_context_async(repo, target, depth, filters, project_cfg, global_cfg))


def _graph_base_payload(repo: Path, operation: str, target: object, depth: int, top_k: int, project_cfg: ProjectConfig, global_cfg: GlobalConfig, warning: str | None = None) -> dict[str, object]:
    ident = repo_identity(repo)
    return {"ok": True, "backend": "cocoindex", "operation": operation, "repo": str(repo), "repo_id": ident["repo_id"], "branch": ident["branch"], "branch_id": ident["branch_id"], "schema_version": CANONICAL_SCHEMA_VERSION, "pipeline_version": effective_pipeline_version(global_cfg), "target": target, "target_kind": "unresolved", "target_symbol": None, "matches": [], "depth": depth, "top_k": top_k, "capabilities": {"symbols": project_cfg.enable_symbols, "references": project_cfg.enable_references, "call_graph": project_cfg.enable_symbols and project_cfg.enable_references, "impact_analysis": project_cfg.enable_symbols and project_cfg.enable_references, "test_links": project_cfg.enable_test_links, "languages": ["python"]}, "warning": warning}


async def _graph_symbol_rows(conn: Any, names: dict[str, str], symbol_ids: list[str]) -> dict[str, dict[str, object]]:
    if not symbol_ids:
        return {}
    rows = await conn.fetch(f"SELECT s.*, f.path AS filename FROM {_qualified(names['schema'], names['symbols'])} s JOIN {_qualified(names['schema'], names['files'])} f ON f.file_id=s.file_id WHERE s.symbol_id = ANY($1::text[])", symbol_ids)
    return {str(row["symbol_id"]): _symbol_item(row) for row in rows}


def _score_graph_result(symbol: dict[str, object], target_symbol: dict[str, object], distance: int, confidence: float) -> tuple[float, dict[str, object]]:
    directness = 1.0 / max(1, distance)
    relevance = 0.5
    if symbol.get("filename") == target_symbol.get("filename"):
        relevance += 0.2
    if not str(symbol.get("name", "")).startswith("_"):
        relevance += 0.1
    freshness = 1.0
    final = (0.45 * confidence) + (0.25 * directness) + (0.15 * min(1.0, relevance)) + (0.10 * freshness)
    ranking = {"directness_score": round(directness, 6), "confidence_score": round(confidence, 6), "symbol_relevance": round(min(1.0, relevance), 6), "freshness_score": freshness, "test_or_entrypoint_boost": 0.0, "final_score": round(final, 6)}
    return round(final, 6), ranking


async def _graph_traverse_async(repo: Path, operation: str, target: object, depth: int, top_k: int, include_indirect: bool, project_cfg: ProjectConfig, global_cfg: GlobalConfig, resources: CocoBackendResources | None = None) -> dict[str, object]:
    _require_coco()
    depth = max(1, min(int(depth), 5)); top_k = max(1, min(int(top_k), 100)); effective_depth = depth if include_indirect else 1
    payload = _graph_base_payload(repo, operation, target, effective_depth, top_k, project_cfg, global_cfg)
    payload.update({"include_indirect": include_indirect, "results": [], "truncated": False, "truncation": {"edge_budget": project_cfg.max_graph_edges, "omitted_paths": 0, "omitted_results": 0}})
    if not (project_cfg.enable_symbols and project_cfg.enable_references):
        payload["warning"] = "call graph requires enable_symbols=true and enable_references=true with CocoIndex/Postgres"
        return payload
    names = _canonical_names(project_cfg, global_cfg); ident = repo_identity(repo)
    pool = await _pool_for(global_cfg, resources)
    try:
        async with pool.acquire() as conn:
            symbol, matches, warning = await _resolve_symbol(conn, repo, target, project_cfg, global_cfg)
            if not symbol:
                payload["target_kind"] = "ambiguous" if matches else "unresolved"; payload["matches"] = matches; payload["warning"] = warning; return payload
            target_id = str(symbol["symbol_id"]); payload["target_kind"] = "symbol"; payload["target_symbol"] = symbol
            forward = operation == "find_callees"
            frontier: list[tuple[str, list[str], list[str], float]] = [(target_id, [target_id], [], 1.0)]
            best: dict[str, dict[str, object]] = {}
            seen_edges = 0
            for dist in range(1, effective_depth + 1):
                next_frontier: list[tuple[str, list[str], list[str], float]] = []
                for current, path_symbols, path_edges, path_conf in frontier:
                    if seen_edges >= project_cfg.max_graph_edges:
                        payload["truncated"] = True; break
                    if forward:
                        rows = await conn.fetch(f"SELECT * FROM {_qualified(names['schema'], names['call_edges'])} WHERE repo_id=$1 AND branch_id=$2 AND caller_symbol_id=$3 ORDER BY confidence DESC LIMIT 500", ident["repo_id"], ident["branch_id"], current)
                    else:
                        rows = await conn.fetch(f"SELECT * FROM {_qualified(names['schema'], names['call_edges'])} WHERE repo_id=$1 AND branch_id=$2 AND callee_symbol_id=$3 ORDER BY confidence DESC LIMIT 500", ident["repo_id"], ident["branch_id"], current)
                    seen_edges += len(rows)
                    for edge in rows:
                        related = str(edge["callee_symbol_id"] if forward else edge["caller_symbol_id"])
                        if related in path_symbols and related != target_id:
                            continue
                        conf = path_conf * float(edge["confidence"])
                        if conf < project_cfg.min_call_edge_confidence:
                            continue
                        new_symbols = [*path_symbols, related]
                        new_edges = [*path_edges, str(edge["edge_id"])]
                        previous = best.get(related)
                        if previous is None or conf > float(previous["path_confidence"]):
                            best[related] = {"distance": dist, "path_confidence": conf, "edge_count": len(new_edges), "paths": [{"symbols": new_symbols, "edges": new_edges, "callsite": _json_object(edge["metadata"]).get("callsite", {}), "confidence": round(conf, 6)}]}
                        if include_indirect:
                            next_frontier.append((related, new_symbols, new_edges, conf))
                frontier = next_frontier
            symbols = await _graph_symbol_rows(conn, names, list(best.keys()))
            results = []
            for sid, data in best.items():
                sym = symbols.get(sid)
                if not sym:
                    continue
                score, ranking = _score_graph_result(sym, symbol, int(data["distance"]), float(data["path_confidence"]))
                results.append({"relationship": "callee" if forward else "caller", "distance": data["distance"], "score": score, "path_confidence": round(float(data["path_confidence"]), 6), "edge_count": data["edge_count"], "symbol": sym, "paths": data["paths"], "ranking": ranking})
            results.sort(key=lambda r: (-float(r["score"]), int(r["distance"]), -float(r["path_confidence"]), str(r["symbol"].get("qualified_name"))))
            payload["truncation"]["omitted_results"] = max(0, len(results) - top_k)  # type: ignore[index]
            payload["results"] = results[:top_k]
    finally:
        if resources is None:
            await pool.close()
    return payload


async def _impact_analysis_async(repo: Path, target: object, depth: int, top_k: int, include_tests: bool, include_files: bool, project_cfg: ProjectConfig, global_cfg: GlobalConfig, resources: CocoBackendResources | None = None) -> dict[str, object]:
    depth = max(1, min(int(depth), 5)); top_k = max(1, min(int(top_k), 200))
    target_path = Path(str(target))
    if not target_path.is_absolute() and (repo / target_path).exists() and (repo / target_path).is_file():
        names = _canonical_names(project_cfg, global_cfg); ident = repo_identity(repo)
        payload = _graph_base_payload(repo, "impact_analysis", target, depth, top_k, project_cfg, global_cfg)
        payload["target_kind"] = "file"
        pool = await _pool_for(global_cfg, resources)
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(f"SELECT s.*, f.path AS filename FROM {_qualified(names['schema'], names['symbols'])} s JOIN {_qualified(names['schema'], names['files'])} f ON f.file_id=s.file_id WHERE s.repo_id=$1 AND s.branch_id=$2 AND f.path=$3 ORDER BY s.start_line LIMIT $4", ident["repo_id"], ident["branch_id"], target_path.as_posix(), top_k)
                symbols = [{"relationship": "target_file_symbol", "distance": 0, "score": 1.0, "path_confidence": 1.0, "symbol": _symbol_item(row), "paths": [], "ranking": {"final_score": 1.0}} for row in rows]
        finally:
            if resources is None:
                await pool.close()
        payload["affected_symbols"] = symbols
        payload["affected_files"] = [{"filename": target_path.as_posix(), "score": 1.0, "relationship_counts": {"direct_callers": 0, "indirect_callers": 0, "direct_callees": 0, "indirect_callees": 0}, "highest_confidence_path": 1.0, "freshness_status": "current", "reasons": ["target_file"]}] if include_files else []
        payload["affected_tests"] = []
        payload["summary"] = {"direct_callers": 0, "indirect_callers": 0, "direct_callees": 0, "indirect_callees": 0, "affected_symbols": len(symbols), "affected_files": len(payload["affected_files"]), "affected_tests": 0, "truncated": False}
        if not symbols:
            payload["warning"] = "no symbols indexed for target file; run refresh with enable_symbols=true"
        return payload
    callers = await _graph_traverse_async(repo, "find_callers", target, depth, top_k, True, project_cfg, global_cfg, resources)
    callees = await _graph_traverse_async(repo, "find_callees", target, 1, top_k, False, project_cfg, global_cfg, resources)
    payload = _graph_base_payload(repo, "impact_analysis", target, depth, top_k, project_cfg, global_cfg, callers.get("warning") if callers.get("warning") else None)
    payload["target_kind"] = callers.get("target_kind", "unresolved"); payload["target_symbol"] = callers.get("target_symbol"); payload["matches"] = callers.get("matches", [])
    affected_by_key: dict[str, dict[str, object]] = {}
    for item in list(callers.get("results", [])) + list(callees.get("results", [])):
        sym = item.get("symbol", {}) if isinstance(item, dict) else {}
        key = str(sym.get("symbol_id") or sym.get("qualified_name") or item.get("relationship") or id(item))
        current = affected_by_key.get(key)
        if current is None or float(item.get("score", 0.0)) > float(current.get("score", 0.0)):
            affected_by_key[key] = item
    affected_symbols = sorted(affected_by_key.values(), key=lambda r: -float(r.get("score", 0.0)))
    payload["affected_symbols"] = affected_symbols[:top_k]
    files: dict[str, dict[str, object]] = {}
    if include_files:
        target_symbol = payload.get("target_symbol") if isinstance(payload.get("target_symbol"), dict) else None
        if target_symbol and target_symbol.get("filename"):
            files[str(target_symbol["filename"])] = {"filename": target_symbol["filename"], "score": 1.0, "relationship_counts": {"direct_callers": 0, "indirect_callers": 0, "direct_callees": 0, "indirect_callees": 0}, "highest_confidence_path": 1.0, "freshness_status": "current", "reasons": ["target_symbol"]}
        for item in affected_symbols:
            sym = item.get("symbol", {}) if isinstance(item, dict) else {}
            filename = str(sym.get("filename") or "")
            if not filename:
                continue
            entry = files.setdefault(filename, {"filename": filename, "score": 0.0, "relationship_counts": {"direct_callers": 0, "indirect_callers": 0, "direct_callees": 0, "indirect_callees": 0}, "highest_confidence_path": 0.0, "freshness_status": "current", "reasons": []})
            rel = str(item.get("relationship")); dist = int(item.get("distance", 1)); key = ("direct_" if dist == 1 else "indirect_") + ("callers" if rel == "caller" else "callees")
            entry["relationship_counts"][key] += 1  # type: ignore[index]
            entry["score"] = min(1.0, max(float(entry["score"]), float(item.get("score", 0.0))))
            entry["highest_confidence_path"] = max(float(entry["highest_confidence_path"]), float(item.get("path_confidence", 0.0)))
            reason = f"{'direct' if dist == 1 else 'indirect'}_{rel}"
            if reason not in entry["reasons"]: entry["reasons"].append(reason)  # type: ignore[union-attr]
    tests: list[dict[str, object]] = []
    if include_tests:
        for filename, entry in files.items():
            p = Path(filename)
            candidate = (Path("tests") / f"test_{p.name}").as_posix()
            if (repo / candidate).exists():
                tests.append({"filename": candidate, "score": round(min(0.60, float(entry["score"]) * 0.60), 6), "confidence": 0.60, "reason": "path_convention", "test_symbols": []})
    payload["affected_files"] = sorted(files.values(), key=lambda f: -float(f["score"]))[:top_k] if include_files else []
    payload["affected_tests"] = tests[:top_k]
    payload["summary"] = {"direct_callers": sum(1 for r in callers.get("results", []) if int(r.get("distance", 0)) == 1), "indirect_callers": sum(1 for r in callers.get("results", []) if int(r.get("distance", 0)) > 1), "direct_callees": sum(1 for r in callees.get("results", []) if int(r.get("distance", 0)) == 1), "indirect_callees": 0, "affected_symbols": len(payload["affected_symbols"]), "affected_files": len(payload["affected_files"]), "affected_tests": len(payload["affected_tests"]), "truncated": bool(callers.get("truncated") or callees.get("truncated"))}
    return payload


def find_callers(repo: Path, target: object, depth: int = 1, top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]:
    repo = repo.resolve(); project_cfg = load_project_config(repo); global_cfg = load_global_config()
    if refresh_first: refresh(repo, project_cfg, global_cfg)
    return resources.run(_graph_traverse_async(repo, "find_callers", target, depth, top_k, include_indirect, project_cfg, global_cfg, resources)) if resources is not None else asyncio.run(_graph_traverse_async(repo, "find_callers", target, depth, top_k, include_indirect, project_cfg, global_cfg))


def find_callees(repo: Path, target: object, depth: int = 1, top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]:
    repo = repo.resolve(); project_cfg = load_project_config(repo); global_cfg = load_global_config()
    if refresh_first: refresh(repo, project_cfg, global_cfg)
    return resources.run(_graph_traverse_async(repo, "find_callees", target, depth, top_k, include_indirect, project_cfg, global_cfg, resources)) if resources is not None else asyncio.run(_graph_traverse_async(repo, "find_callees", target, depth, top_k, include_indirect, project_cfg, global_cfg))


def impact_analysis(repo: Path, target: object, depth: int = 2, top_k: int = 50, include_tests: bool = True, include_files: bool = True, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]:
    repo = repo.resolve(); project_cfg = load_project_config(repo); global_cfg = load_global_config()
    if refresh_first: refresh(repo, project_cfg, global_cfg)
    return resources.run(_impact_analysis_async(repo, target, depth, top_k, include_tests, include_files, project_cfg, global_cfg, resources)) if resources is not None else asyncio.run(_impact_analysis_async(repo, target, depth, top_k, include_tests, include_files, project_cfg, global_cfg))


def _coco_context_payload(repo: Path, payload: dict[str, object], project_cfg: ProjectConfig, global_cfg: GlobalConfig) -> dict[str, object]:
    ident = repo_identity(repo)
    payload.update({"backend": "cocoindex", "repo_id": ident["repo_id"], "branch": ident["branch"], "branch_id": ident["branch_id"], "schema_version": CANONICAL_SCHEMA_VERSION, "pipeline_version": effective_pipeline_version(global_cfg)})
    caps = dict(payload.get("capabilities") or {})
    caps.update({"repo_hierarchy": True, "repo_map": True, "symbols": project_cfg.enable_symbols, "references": project_cfg.enable_references, "call_graph": project_cfg.enable_symbols and project_cfg.enable_references, "test_links": project_cfg.enable_test_links, "find_tests": True, "similar_code": True, "review_context": True, "languages": ["python"]})
    payload["capabilities"] = caps
    return payload


def repo_map(repo: Path, target: object | None = None, depth: int = 2, include_symbols: bool = True, include_tests: bool = False, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]:
    repo = repo.resolve(); project_cfg = load_project_config(repo); global_cfg = load_global_config()
    if refresh_first: refresh(repo, project_cfg, global_cfg)
    return _coco_context_payload(repo, context_tools.repo_map(repo, target, depth, include_symbols, include_tests, False, "cocoindex"), project_cfg, global_cfg)


def find_tests(repo: Path, targets: list[object], top_k: int = 20, include_indirect: bool = False, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]:
    repo = repo.resolve(); project_cfg = load_project_config(repo); global_cfg = load_global_config()
    if refresh_first: refresh(repo, project_cfg, global_cfg)
    payload = context_tools.find_tests(repo, targets, top_k, include_indirect, False, "cocoindex")
    if not project_cfg.enable_test_links:
        payload["warning"] = "enable_test_links is false; returned heuristic candidates only"
    return _coco_context_payload(repo, payload, project_cfg, global_cfg)


def _similar_freshness_score(status: str) -> float:
    return {"current": 1.0, "stale": 0.75, "error": 0.50, "pending": 0.50}.get(status, 1.0)


def _similar_score_components(
    *,
    mode: str,
    semantic: float,
    lexical_score: float,
    symbol_score: float,
    ast: float,
    structure: float,
    freshness: float,
    role_prior: float,
    penalty: float,
) -> tuple[float, dict[str, float]]:
    semantic_available = semantic > 0.0
    if mode == "semantic" and semantic_available:
        weights = {"semantic": 0.70, "lexical": 0.10, "symbol": 0.10, "ast": 0.05, "structure": 0.0, "freshness": 0.05}
    elif mode == "hybrid" and semantic_available:
        weights = {"semantic": 0.40, "lexical": 0.30, "symbol": 0.10, "ast": 0.08, "structure": 0.07, "freshness": 0.05}
    elif mode == "lexical" or mode == "semantic":
        weights = {"semantic": 0.0, "lexical": 0.70, "symbol": 0.10, "ast": 0.08, "structure": 0.07, "freshness": 0.05}
    else:
        weights = {"semantic": 0.0, "lexical": 0.50, "symbol": 0.17, "ast": 0.13, "structure": 0.12, "freshness": 0.08}
    base = (
        weights["semantic"] * semantic
        + weights["lexical"] * lexical_score
        + weights["symbol"] * symbol_score
        + weights["ast"] * ast
        + weights["structure"] * structure
        + weights["freshness"] * freshness
    )
    final = max(0.0, min(1.0, base * role_prior - penalty))
    return round(final, 6), {
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


def _similar_query_text(target: object | None, query: str | None, target_code: str) -> str:
    parts = [query or ""]
    if target:
        parts.append(str(target))
    if target_code:
        parts.append(target_code[:4000])
    return "\n".join(part for part in parts if part).strip() or str(target or query or "")


def _similar_target_file(target: object | None) -> str | None:
    if not target:
        return None
    return str(target).split(":", 1)[0].strip("/") or None


def _similar_kind_rank(result: dict[str, object]) -> int:
    metadata = result.get("metadata", {}) if isinstance(result.get("metadata"), dict) else {}
    kind = str(metadata.get("candidate_kind") or "chunk")
    chunk_kind = str(metadata.get("chunk_kind") or "text")
    role = str(metadata.get("content_role") or "unknown")
    if chunk_kind in {"function", "method"}:
        return 0
    if chunk_kind == "class":
        return 1
    if kind == "symbol":
        return 2
    if role == "source":
        return 3
    if role == "config":
        return 4
    if role == "test":
        return 5
    if role == "docs":
        return 6
    return 7


def _aggregate_coco_file_results(results: list[dict[str, object]], top_k: int) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for result in results:
        grouped.setdefault(str(result["filename"]), []).append(result)
    files: list[dict[str, object]] = []
    for filename, items in grouped.items():
        items.sort(key=lambda r: -float(r["score"]))
        best = dict(items[0])
        support = len(items)
        kinds = [str((item.get("metadata") or {}).get("candidate_kind")) for item in items if isinstance(item.get("metadata"), dict)]
        best["score"] = round(min(1.0, float(best["score"]) + min(0.08, 0.02 * (support - 1))), 6)
        best["chunk_id"] = None
        best["symbol_id"] = None
        best["evidence"] = ["file_aggregate:max_candidate", *list(best.get("evidence", []))[:5]]
        metadata = dict(best.get("metadata", {})) if isinstance(best.get("metadata"), dict) else {}
        metadata["candidate_kind"] = "file"
        metadata["aggregated_candidates"] = {
            "chunks": sum(1 for kind in kinds if kind == "chunk"),
            "symbols": sum(1 for kind in kinds if kind == "symbol"),
            "supporting_evidence_count": support,
            "best_candidate_key": (items[0].get("metadata") or {}).get("candidate_key") if isinstance(items[0].get("metadata"), dict) else None,
            "top_candidate_keys": [(item.get("metadata") or {}).get("candidate_key") for item in items[:3] if isinstance(item.get("metadata"), dict)],
        }
        best["metadata"] = metadata
        files.append(best)
    files.sort(key=lambda r: (-float(r["score"]), str(r["filename"])))
    return files[:top_k]


def _normalize_coco_similar_chunk(row: object, query_text: str, target_file: str | None, mode: str, ranking_profile: str, max_result_code_bytes: int) -> dict[str, object] | None:
    filename = str(row["filename"])
    code = str(row["code"] or "")
    code_bytes = code.encode("utf-8")
    returned_code = code_bytes[:max_result_code_bytes].decode("utf-8", errors="ignore") if len(code_bytes) > max_result_code_bytes else code
    qtok = tokenize(query_text)
    row_tokens = tokenize(f"{filename}\n{code}")
    lexical_score = score_tokens(qtok, row_tokens)
    semantic = max(0.0, min(1.0, float(row["semantic_score"] or 0.0)))
    if mode == "semantic" and semantic <= 0.0:
        return None
    symbol = row["symbol"] if "symbol" in row else None
    qualified_name = row["qualified_name"] if "qualified_name" in row else None
    symbol_kind = row["symbol_kind"] if "symbol_kind" in row else None
    symbol_text = " ".join(str(value or "") for value in (symbol, qualified_name, symbol_kind))
    symbol_score = score_tokens(qtok, tokenize(symbol_text))
    if symbol and symbol_score > 0:
        symbol_score = min(1.0, symbol_score + 0.20)
    candidate_role = context_tools.content_role_for(filename)
    query_role = context_tools._query_content_role(target_file, query_text)  # noqa: SLF001 - shared normalization helper
    chunk_kind = str(row["chunk_kind"] or "text")
    ast = context_tools._ast_score(chunk_kind, query_role, candidate_role)  # noqa: SLF001
    structure = context_tools._structure_score(filename, target_file, qtok)  # noqa: SLF001
    freshness_status = str(row["freshness_status"] or "current")
    freshness = _similar_freshness_score(freshness_status)
    role_prior = context_tools._role_prior(query_role, candidate_role)  # noqa: SLF001
    penalty = 0.0
    evidence: list[str] = []
    if semantic > 0:
        evidence.append(f"semantic:chunk_vector={semantic:.3f}")
    shared = context_tools._shared_token_evidence(qtok, row_tokens)  # noqa: SLF001
    if shared:
        evidence.append(shared)
    if symbol_score > 0.20 and symbol:
        evidence.append(f"symbol:name_stem_match={str(symbol).split('_')[0].lower()}")
    if symbol_kind:
        evidence.append(f"symbol:same_kind={symbol_kind}")
    evidence.append(f"ast:{chunk_kind}_chunk" if chunk_kind != "text" else "ast:text_chunk")
    if structure >= 0.65:
        evidence.append("structure:same_path_role")
    evidence.append(f"role:{query_role}_query_{candidate_role}_candidate")
    evidence.append(f"freshness:{freshness_status}")
    if candidate_role == "generated":
        penalty += 0.10; evidence.append("penalty:generated_or_vendor")
    if query_role == "source" and candidate_role == "docs" and lexical_score < 0.30 and semantic < 0.60:
        penalty += 0.05; evidence.append("penalty:docs_for_source_query")
    if query_role == "source" and candidate_role == "test" and lexical_score < 0.30 and semantic < 0.60:
        penalty += 0.03; evidence.append("penalty:test_for_source_query")
    score, components = _similar_score_components(mode=mode, semantic=semantic, lexical_score=lexical_score, symbol_score=symbol_score, ast=ast, structure=structure, freshness=freshness, role_prior=role_prior, penalty=penalty)
    if score <= 0 or not any(item.startswith(("semantic:", "lexical:")) for item in evidence):
        return None
    metadata = _json_object(row["metadata"] if "metadata" in row else None)
    metadata.update({
        "excluded_self": False,
        "ranking_profile": ranking_profile,
        "candidate_kind": "chunk",
        "content_role": candidate_role,
        "chunk_kind": chunk_kind,
        "source": "cocoindex-hybrid-similar-code-v2" if mode == "hybrid" else f"cocoindex-{mode}-similar-code-v2",
        "candidate_key": f"chunk:{row['chunk_id']}",
        "freshness_status": freshness_status,
        "semantic_available": semantic > 0,
        "lexical_available": lexical_score > 0,
        "symbol_available": bool(symbol),
        "ast_available": chunk_kind != "text",
    })
    return {
        "score": score,
        "confidence": round(min(0.95, score + (0.10 if semantic > 0 else 0.02)), 6),
        "similarity": {"semantic": round(semantic, 6), "lexical": round(lexical_score, 6), "structure": round(structure, 6), "symbol": round(symbol_score, 6), "ast": round(ast, 6), "role_prior": round(role_prior, 6), "freshness": round(freshness, 6), "penalty": round(penalty, 6)},
        "score_components": components,
        "filename": filename,
        "start_line": int(row["start_line"]),
        "end_line": int(row["end_line"]),
        "code": returned_code,
        "symbol": qualified_name or symbol,
        "symbol_id": row["symbol_id"] if "symbol_id" in row else None,
        "chunk_id": row["chunk_id"],
        "risk": context_tools._risk_label(candidate_role, chunk_kind, lexical_score, symbol_score, context_tools.role_for(filename)),  # noqa: SLF001
        "evidence": evidence,
        "metadata": metadata,
    }


def _normalize_coco_similar_symbol(row: object, query_text: str, target_file: str | None, mode: str, ranking_profile: str, max_result_code_bytes: int) -> dict[str, object] | None:
    filename = str(row["filename"])
    code = str(row["code"] or row["signature"] or row["docstring"] or row["qualified_name"])
    code_bytes = code.encode("utf-8")
    returned_code = code_bytes[:max_result_code_bytes].decode("utf-8", errors="ignore") if len(code_bytes) > max_result_code_bytes else code
    qtok = tokenize(query_text)
    lexical_text = f"{row['name']} {row['qualified_name']} {row['kind']} {row['signature'] or ''} {row['docstring'] or ''} {filename}\n{code}"
    lexical_score = score_tokens(qtok, tokenize(lexical_text))
    semantic = max(0.0, min(1.0, float(row["semantic_score"] or 0.0)))
    if mode == "semantic" and semantic <= 0.0:
        return None
    symbol_score = min(1.0, score_tokens(qtok, tokenize(f"{row['name']} {row['qualified_name']}")) + 0.25)
    candidate_role = context_tools.content_role_for(filename)
    query_role = context_tools._query_content_role(target_file, query_text)  # noqa: SLF001
    ast = context_tools._ast_score(str(row["kind"]), query_role, candidate_role)  # noqa: SLF001
    structure = context_tools._structure_score(filename, target_file, qtok)  # noqa: SLF001
    freshness_status = str(row["freshness_status"] or "current")
    freshness = _similar_freshness_score(freshness_status)
    role_prior = context_tools._role_prior(query_role, candidate_role)  # noqa: SLF001
    penalty = 0.10 if candidate_role == "generated" else 0.0
    evidence = [f"semantic:symbol_vector={semantic:.3f}"] if semantic > 0 else []
    shared = context_tools._shared_token_evidence(qtok, tokenize(lexical_text))  # noqa: SLF001
    if shared:
        evidence.append(shared)
    if symbol_score > 0:
        evidence.append(f"symbol:name_stem_match={str(row['name']).split('_')[0].lower()}")
        evidence.append(f"symbol:same_kind={row['kind']}")
    evidence.append(f"ast:{row['kind']}_chunk")
    evidence.append(f"role:{query_role}_query_{candidate_role}_candidate")
    evidence.append(f"freshness:{freshness_status}")
    if penalty:
        evidence.append("penalty:generated_or_vendor")
    score, components = _similar_score_components(mode=mode, semantic=semantic, lexical_score=lexical_score, symbol_score=symbol_score, ast=ast, structure=structure, freshness=freshness, role_prior=role_prior, penalty=penalty)
    if score <= 0 or not any(item.startswith(("semantic:", "lexical:")) for item in evidence):
        return None
    symbol_id = str(row["symbol_id"])
    best_chunk_id = row["chunk_id"] if "chunk_id" in row else None
    metadata = _json_object(row["metadata"] if "metadata" in row else None)
    metadata.update({
        "excluded_self": False,
        "ranking_profile": ranking_profile,
        "candidate_kind": "symbol",
        "content_role": candidate_role,
        "chunk_kind": str(row["kind"]),
        "source": "cocoindex-symbol-similar-code-v2",
        "candidate_key": f"symbol:{symbol_id}",
        "freshness_status": freshness_status,
        "semantic_available": semantic > 0,
        "lexical_available": lexical_score > 0,
        "symbol_available": True,
        "ast_available": True,
        "best_chunk_id": best_chunk_id,
        "best_chunk_score": round(semantic, 6) if best_chunk_id else None,
        "related_chunks": [best_chunk_id] if best_chunk_id else [],
    })
    return {
        "score": score,
        "confidence": round(min(0.95, score + (0.10 if semantic > 0 else 0.02)), 6),
        "similarity": {"semantic": round(semantic, 6), "lexical": round(lexical_score, 6), "structure": round(structure, 6), "symbol": round(symbol_score, 6), "ast": round(ast, 6), "role_prior": round(role_prior, 6), "freshness": round(freshness, 6), "penalty": round(penalty, 6)},
        "score_components": components,
        "filename": filename,
        "start_line": int(row["start_line"]),
        "end_line": int(row["end_line"]),
        "code": returned_code,
        "symbol": row["qualified_name"],
        "symbol_id": symbol_id,
        "chunk_id": best_chunk_id,
        "risk": context_tools._risk_label(candidate_role, str(row["kind"]), lexical_score, symbol_score, context_tools.role_for(filename)),  # noqa: SLF001
        "evidence": evidence,
        "metadata": metadata,
    }


async def _find_similar_code_async(repo: Path, target: object | None, query: str | None, top_k: int, mode: str, scope: str, exclude_self: bool, project_cfg: ProjectConfig, global_cfg: GlobalConfig, resources: CocoBackendResources | None = None) -> dict[str, object]:
    _require_coco()
    ident = repo_identity(repo)
    names = _canonical_names(project_cfg, global_cfg)
    ranking_profile = "similar-code-v2"
    target_file = _similar_target_file(target)
    vector_chunk_limit = max(100, top_k * 10)
    vector_symbol_limit = max(100, top_k * 10)
    lexical_limit = max(200, top_k * 20)
    pool = await _pool_for(global_cfg, resources)
    try:
        async with pool.acquire() as conn:
            if resources is None:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await ensure_canonical_schema(conn, project_cfg, global_cfg)
            chunks_table = _qualified(names["schema"], names["chunks"])
            files_table = _qualified(names["schema"], names["files"])
            symbols_table = _qualified(names["schema"], names["symbols"])
            symbol_embeddings_table = _qualified(names["schema"], names["symbol_embeddings"])
            freshness_table = _qualified(names["schema"], names["freshness"])
            chunk_count = int(await conn.fetchval(f"SELECT count(*) FROM {chunks_table} WHERE repo_id=$1 AND branch_id=$2", ident["repo_id"], ident["branch_id"]) or 0)
            if chunk_count <= 0:
                raise CocoIndexUnavailable("CocoIndex canonical chunks are unavailable; run `pi-code-index refresh --json --repo <repo>` before semantic similar-code search")
            target_code = ""
            if target_file:
                target_code = str(await conn.fetchval(f"SELECT string_agg(code, E'\\n' ORDER BY start_line) FROM (SELECT code, start_line FROM {chunks_table} WHERE repo_id=$1 AND branch_id=$2 AND path=$3 ORDER BY start_line LIMIT 8) q", ident["repo_id"], ident["branch_id"], target_file) or "")
            query_text = _similar_query_text(target, query, target_code)
            embedder = resources.get_embedder() if resources is not None else _shared_embedder(global_cfg.embedding_model)
            embedding = _as_pgvector(await embedder.embed(query_text))
            qtokens = list(tokenize(query_text))[:8]
            token_patterns = [f"%{token}%" for token in qtokens]
            exclude_same_file = bool(exclude_self and target_file and not query)
            chunk_rows = await conn.fetch(
                f"""
                SELECT c.chunk_id, c.symbol_id, s.name AS symbol, s.qualified_name, s.kind AS symbol_kind,
                       c.path AS filename, c.start_line, c.end_line, c.code, c.chunk_kind, c.metadata,
                       coalesce(fr.status, 'current') AS freshness_status,
                       1 - (c.embedding <=> $1::vector) AS semantic_score
                FROM {chunks_table} c
                LEFT JOIN {symbols_table} s ON s.symbol_id = c.symbol_id
                LEFT JOIN {files_table} fi ON fi.file_id = c.file_id
                LEFT JOIN {freshness_table} fr ON fr.file_id = c.file_id AND fr.pipeline_version = $4
                WHERE c.repo_id=$2 AND c.branch_id=$3 AND coalesce(fr.status, 'current') <> 'deleted'
                  AND (NOT $5::boolean OR c.path <> $6)
                ORDER BY c.embedding <=> $1::vector
                LIMIT $7
                """,
                embedding,
                ident["repo_id"],
                ident["branch_id"],
                effective_pipeline_version(global_cfg),
                exclude_same_file,
                target_file or "",
                vector_chunk_limit,
            )
            if mode == "hybrid" and token_patterns:
                lexical_rows = await conn.fetch(
                    f"""
                    SELECT c.chunk_id, c.symbol_id, s.name AS symbol, s.qualified_name, s.kind AS symbol_kind,
                           c.path AS filename, c.start_line, c.end_line, c.code, c.chunk_kind, c.metadata,
                           coalesce(fr.status, 'current') AS freshness_status,
                           0.0 AS semantic_score
                    FROM {chunks_table} c
                    LEFT JOIN {symbols_table} s ON s.symbol_id = c.symbol_id
                    LEFT JOIN {freshness_table} fr ON fr.file_id = c.file_id AND fr.pipeline_version = $4
                    WHERE c.repo_id=$2 AND c.branch_id=$3 AND coalesce(fr.status, 'current') <> 'deleted'
                      AND (NOT $5::boolean OR c.path <> $6)
                      AND (c.path ILIKE ANY($1::text[]) OR c.code ILIKE ANY($1::text[]) OR s.name ILIKE ANY($1::text[]) OR s.qualified_name ILIKE ANY($1::text[]))
                    LIMIT $7
                    """,
                    token_patterns,
                    ident["repo_id"],
                    ident["branch_id"],
                    effective_pipeline_version(global_cfg),
                    exclude_same_file,
                    target_file or "",
                    lexical_limit,
                )
                chunk_rows = [*chunk_rows, *lexical_rows]
            symbol_rows: list[object] = []
            symbol_warning = None
            if scope == "symbols":
                if not project_cfg.enable_symbols:
                    symbol_warning = "symbols disabled by project config; returned chunk candidates"
                else:
                    symbol_count = int(await conn.fetchval(f"SELECT count(*) FROM {symbols_table} WHERE repo_id=$1 AND branch_id=$2", ident["repo_id"], ident["branch_id"]) or 0)
                    embedding_count = int(await conn.fetchval(f"SELECT count(*) FROM {symbol_embeddings_table} WHERE repo_id=$1 AND branch_id=$2", ident["repo_id"], ident["branch_id"]) or 0)
                    if symbol_count <= 0 or embedding_count <= 0:
                        raise CocoIndexUnavailable("CocoIndex symbol embeddings are unavailable for scope=symbols; run `pi-code-index refresh --json --repo <repo>` with enable_symbols: true")
                    symbol_rows = await conn.fetch(
                        f"""
                        SELECT s.symbol_id, s.name, s.qualified_name, s.kind, s.signature, s.docstring, s.metadata,
                               fi.path AS filename, s.start_line, s.end_line,
                               bc.chunk_id, bc.code,
                               coalesce(fr.status, 'current') AS freshness_status,
                               1 - (se.embedding <=> $1::vector) AS semantic_score
                        FROM {symbol_embeddings_table} se
                        JOIN {symbols_table} s ON s.symbol_id = se.symbol_id
                        JOIN {files_table} fi ON fi.file_id = s.file_id
                        LEFT JOIN LATERAL (
                            SELECT c.chunk_id, c.code FROM {chunks_table} c
                            WHERE c.symbol_id = s.symbol_id
                            ORDER BY c.embedding <=> $1::vector
                            LIMIT 1
                        ) bc ON true
                        LEFT JOIN {freshness_table} fr ON fr.file_id = s.file_id AND fr.pipeline_version = $4
                        WHERE se.repo_id=$2 AND se.branch_id=$3 AND coalesce(fr.status, 'current') <> 'deleted'
                          AND (NOT $5::boolean OR fi.path <> $6)
                        ORDER BY se.embedding <=> $1::vector
                        LIMIT $7
                        """,
                        embedding,
                        ident["repo_id"],
                        ident["branch_id"],
                        effective_pipeline_version(global_cfg),
                        exclude_same_file,
                        target_file or "",
                        vector_symbol_limit,
                    )
    finally:
        if resources is None:
            await pool.close()
    normalized: dict[str, dict[str, object]] = {}
    raw_results: list[dict[str, object]] = []
    if scope == "symbols" and symbol_rows:
        raw_results = [result for row in symbol_rows if (result := _normalize_coco_similar_symbol(row, query_text, target_file, mode, ranking_profile, project_cfg.max_result_code_bytes)) is not None]
    else:
        raw_results = [result for row in chunk_rows if (result := _normalize_coco_similar_chunk(row, query_text, target_file, mode, ranking_profile, project_cfg.max_result_code_bytes)) is not None]
    for result in raw_results:
        metadata = result.get("metadata", {}) if isinstance(result.get("metadata"), dict) else {}
        key = str(metadata.get("candidate_key") or f"{result.get('filename')}:{result.get('start_line')}:{result.get('end_line')}")
        previous = normalized.get(key)
        if previous is None or float(result["score"]) > float(previous["score"]):
            normalized[key] = result
    all_results = list(normalized.values())
    all_results.sort(key=lambda r: (-float(r["score"]), _similar_kind_rank(r), -float((r.get("similarity") or {}).get("lexical", 0.0)) if isinstance(r.get("similarity"), dict) else 0.0, len(str(r.get("code") or "")), str((r.get("metadata") or {}).get("candidate_key") if isinstance(r.get("metadata"), dict) else "")))
    results = _aggregate_coco_file_results(all_results, top_k) if scope == "files" else all_results[:top_k]
    warning = symbol_warning
    payload = context_tools._base(repo, "find_similar_code", "cocoindex")  # noqa: SLF001
    if warning:
        payload["warning"] = warning
    payload.update({
        "target": target,
        "query": query,
        "mode": mode,
        "scope": scope,
        "exclude_self": exclude_self,
        "top_k": top_k,
        "ranking_profile": ranking_profile,
        "results": results,
        "truncated": len(all_results) > top_k,
        "truncation": {"candidate_limit": 500, "lexical_candidate_limit": lexical_limit, "vector_chunk_candidate_limit": vector_chunk_limit, "vector_symbol_candidate_limit": vector_symbol_limit if scope == "symbols" else 0, "omitted_candidates": max(0, len(raw_results) - 500), "omitted_results": max(0, len(all_results) - top_k)},
        "warnings": [warning] if warning else [],
    })
    return _coco_context_payload(repo, payload, project_cfg, global_cfg)


def find_similar_code(repo: Path, target: object | None = None, query: str | None = None, top_k: int = 12, mode: str = "hybrid", scope: str = "chunks", exclude_self: bool = True, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]:
    repo = repo.resolve(); project_cfg = load_project_config(repo); global_cfg = load_global_config(); top_k = max(1, min(int(top_k), 100))
    if refresh_first: refresh(repo, project_cfg, global_cfg)
    if mode == "lexical":
        return _coco_context_payload(repo, context_tools.find_similar_code(repo, target, query, top_k, mode, scope, exclude_self, False, "cocoindex"), project_cfg, global_cfg)
    return resources.run(_find_similar_code_async(repo, target, query, top_k, mode, scope, exclude_self, project_cfg, global_cfg, resources)) if resources is not None else asyncio.run(_find_similar_code_async(repo, target, query, top_k, mode, scope, exclude_self, project_cfg, global_cfg))


def review_context(repo: Path, targets: list[object], top_k: int = 30, include_map: bool = True, include_tests: bool = True, include_similar: bool = True, include_impact: bool = True, refresh_first: bool = False, resources: CocoBackendResources | None = None) -> dict[str, object]:
    repo = repo.resolve(); project_cfg = load_project_config(repo); global_cfg = load_global_config()
    if refresh_first: refresh(repo, project_cfg, global_cfg)
    payload = _coco_context_payload(repo, context_tools.review_context(repo, targets, top_k, include_map, include_tests, include_similar, include_impact, False, "cocoindex"), project_cfg, global_cfg)
    if include_impact and targets:
        agg_files: dict[str, dict[str, object]] = {}
        agg_tests: dict[str, dict[str, object]] = {}
        agg_symbols: list[dict[str, object]] = []
        for target in targets:
            try:
                impact = impact_analysis(repo, target, 2, min(top_k, 100), True, True, False, resources)
            except Exception:  # noqa: BLE001 - review context stays best-effort
                continue
            for sym in (impact.get("affected_symbols") or []):
                agg_symbols.append(sym)
            for f in (impact.get("affected_files") or []):
                fn = str(f.get("filename") or "")
                if fn:
                    agg_files.setdefault(fn, f)
            for t in (impact.get("affected_tests") or []):
                fn = str(t.get("filename") or "")
                if fn:
                    agg_tests.setdefault(fn, t)
        impact_items = [*sorted(agg_files.values(), key=lambda f: -float(f.get("score", 0.0)))[: min(top_k, 20)], *list(agg_tests.values())[:8], *agg_symbols[:20]]
        for section in payload.get("sections", []):
            if section.get("section") == "impact":
                section["items"] = impact_items
                section["warning"] = None
                section["impact_summary"] = {"affected_files": len(agg_files), "affected_tests": len(agg_tests), "affected_symbols": len(agg_symbols)}
                break
        else:
            payload.setdefault("sections", []).append({"section": "impact", "items": impact_items, "warning": None, "impact_summary": {"affected_files": len(agg_files), "affected_tests": len(agg_tests), "affected_symbols": len(agg_symbols)}})
    return payload


async def _status_async(
    repo: Path,
    project_cfg: ProjectConfig,
    global_cfg: GlobalConfig,
    resources: CocoBackendResources | None = None,
) -> dict[str, object]:
    _require_coco()
    table = _qualified(effective_schema_name(project_cfg, global_cfg), project_cfg.table_name)
    names = _canonical_names(project_cfg, global_cfg)
    ident = repo_identity(repo)
    pool = await _pool_for(global_cfg, resources)
    try:
        async with pool.acquire() as conn:
            if resources is None:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await ensure_canonical_schema(conn, project_cfg, global_cfg)
            exists = await conn.fetchval("SELECT to_regclass($1)", f"{effective_schema_name(project_cfg, global_cfg)}.{project_cfg.table_name}")
            rows = 0
            if exists:
                rows = await conn.fetchval(f"SELECT count(*) FROM {table} WHERE repo = $1", str(repo))
            canonical_exists = bool(await conn.fetchval("SELECT to_regclass($1)", f"{names['schema']}.{names['chunks']}"))
            counts = {"files": 0, "chunks": 0, "ast_chunks": 0, "recursive_chunks": 0, "symbols": 0, "symbols_by_language": {}, "symbols_by_kind": {}, "symbol_parser_errors": 0, "symbols_stale": 0, "references": 0, "call_edges": 0, "repo_hierarchy_nodes": 0, "test_links": 0, "test_files": 0, "similarity_candidates": 0, "freshness_current": 0, "freshness_stale": 0, "freshness_error": 0, "parser_errors": 0}
            if canonical_exists:
                for key, suffix in [("files", "files"), ("chunks", "chunks"), ("symbols", "symbols"), ("references", "references"), ("call_edges", "call_edges"), ("test_links", "test_links")]:
                    counts[key] = int(await conn.fetchval(f"SELECT count(*) FROM {_qualified(names['schema'], names[suffix])} WHERE repo_id = $1 AND branch_id = $2", ident["repo_id"], ident["branch_id"]) or 0)
                counts["repo_hierarchy_nodes"] = int(await conn.fetchval(f"SELECT count(*) FROM {_qualified(names['schema'], names['repo_hierarchy'])} WHERE repo_id = $1 AND branch_id = $2", ident["repo_id"], ident["branch_id"]) or 0)
                counts["test_files"] = int(await conn.fetchval(f"SELECT count(*) FROM {_qualified(names['schema'], names['files'])} WHERE repo_id = $1 AND branch_id = $2 AND (path LIKE 'tests/%' OR path LIKE 'test/%' OR path LIKE '%/test_%' OR path LIKE '%_test.py' OR path LIKE '%.test.ts' OR path LIKE '%.spec.ts')", ident["repo_id"], ident["branch_id"]) or 0)
                counts["similarity_candidates"] = counts["chunks"]
                counts["resolved_references"] = int(await conn.fetchval(f"SELECT count(*) FROM {_qualified(names['schema'], names['references'])} WHERE repo_id = $1 AND branch_id = $2 AND symbol_id IS NOT NULL", ident["repo_id"], ident["branch_id"]) or 0)
                counts["unresolved_references"] = int(await conn.fetchval(f"SELECT count(*) FROM {_qualified(names['schema'], names['references'])} WHERE repo_id = $1 AND branch_id = $2 AND symbol_id IS NULL", ident["repo_id"], ident["branch_id"]) or 0)
                counts["low_confidence_call_edges"] = int(await conn.fetchval(f"SELECT count(*) FROM {_qualified(names['schema'], names['call_edges'])} WHERE repo_id = $1 AND branch_id = $2 AND confidence < $3", ident["repo_id"], ident["branch_id"], project_cfg.min_call_edge_confidence) or 0)
                counts["ast_chunks"] = int(await conn.fetchval(f"SELECT count(*) FROM {_qualified(names['schema'], names['chunks'])} WHERE repo_id = $1 AND branch_id = $2 AND metadata->>'chunk_strategy' = 'ast'", ident["repo_id"], ident["branch_id"]) or 0)
                counts["recursive_chunks"] = int(await conn.fetchval(f"SELECT count(*) FROM {_qualified(names['schema'], names['chunks'])} WHERE repo_id = $1 AND branch_id = $2 AND coalesce(metadata->>'chunk_strategy', 'recursive') IN ('recursive', 'legacy')", ident["repo_id"], ident["branch_id"]) or 0)
                counts["parser_errors"] = int(await conn.fetchval(f"SELECT count(*) FROM {_qualified(names['schema'], names['chunks'])} WHERE repo_id = $1 AND branch_id = $2 AND metadata->>'ast_fallback_reason' = 'parse_error'", ident["repo_id"], ident["branch_id"]) or 0)
                freshness_rows = await conn.fetch(f"SELECT status, count(*) AS count FROM {_qualified(names['schema'], names['freshness'])} WHERE repo_id = $1 AND branch_id = $2 GROUP BY status", ident["repo_id"], ident["branch_id"])
                for row in freshness_rows:
                    counts[f"freshness_{row['status']}"] = int(row["count"])
                kind_rows = await conn.fetch(f"SELECT kind, count(*) AS count FROM {_qualified(names['schema'], names['symbols'])} WHERE repo_id = $1 AND branch_id = $2 GROUP BY kind", ident["repo_id"], ident["branch_id"])
                counts["symbols_by_kind"] = {str(row["kind"]): int(row["count"]) for row in kind_rows}
                language_rows = await conn.fetch(f"SELECT coalesce(metadata->>'language', 'unknown') AS language, count(*) AS count FROM {_qualified(names['schema'], names['symbols'])} WHERE repo_id = $1 AND branch_id = $2 GROUP BY language", ident["repo_id"], ident["branch_id"])
                counts["symbols_by_language"] = {str(row["language"]): int(row["count"]) for row in language_rows}
                counts["symbol_parser_errors"] = counts["parser_errors"]
                counts["symbols_stale"] = int(await conn.fetchval(f"SELECT count(*) FROM {_qualified(names['schema'], names['symbols'])} WHERE repo_id = $1 AND branch_id = $2 AND coalesce(metadata->>'freshness_status', 'current') <> 'current'", ident["repo_id"], ident["branch_id"]) or 0)
    finally:
        if resources is None:
            await pool.close()
    return {
        "ok": True,
        "backend": "cocoindex",
        "repo": str(repo),
        "repo_id": ident["repo_id"],
        "branch": ident["branch"],
        "branch_id": ident["branch_id"],
        "table_name": project_cfg.table_name,
        "schema_name": effective_schema_name(project_cfg, global_cfg),
        "table_prefix": effective_table_prefix(project_cfg, global_cfg),
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "pipeline_version": effective_pipeline_version(global_cfg),
        "chunk_strategy": project_cfg.chunk_strategy,
        "ast_languages": project_cfg.ast_languages,
        "table_exists": bool(exists),
        "canonical_tables_exist": canonical_exists,
        "repo_chunks": int(counts.get("chunks") or rows or 0),
        "repo_files": int(counts.get("files") or 0),
        "counts": counts,
        "embedding_model": global_cfg.embedding_model,
        "capabilities": {"symbols": project_cfg.enable_symbols, "symbol_search": project_cfg.enable_symbols, "symbol_definition": project_cfg.enable_symbols, "symbol_context": project_cfg.enable_symbols, "symbol_embeddings": project_cfg.enable_symbols, "references": project_cfg.enable_references, "call_graph": project_cfg.enable_symbols and project_cfg.enable_references, "impact_analysis": project_cfg.enable_symbols and project_cfg.enable_references, "test_links": project_cfg.enable_test_links, "repo_map": True, "find_tests": True, "find_similar_code": True, "review_context": True, "languages": ["python"]},
        "quality_context": {"ready": canonical_exists, "warnings": [] if canonical_exists else ["canonical tables are not ready"]},
        "live": False,
    }


def status(repo: Path, resources: CocoBackendResources | None = None) -> dict[str, object]:
    repo = repo.resolve()
    return resources.run(_status_async(repo, load_project_config(repo), load_global_config(), resources)) if resources is not None else asyncio.run(_status_async(repo, load_project_config(repo), load_global_config()))
