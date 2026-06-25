from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .config import ProjectConfig, index_path, load_project_config

TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+")
CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")

@dataclass
class CodeChunk:
    id: str
    repo: str
    filename: str
    start_line: int
    end_line: int
    code: str
    tokens: dict[str, float]

@dataclass
class IndexData:
    repo: str
    chunks: list[CodeChunk]
    files: int
    version: int = 1

def repo_root(path: Path | None = None) -> Path:
    start = (path or Path.cwd()).resolve()
    if start.is_file():
        start = start.parent
    cur = start
    while cur != cur.parent:
        if (cur / ".git").exists():
            return cur
        cur = cur.parent
    return start

def rel_name(repo: Path, path: Path) -> str:
    return path.absolute().relative_to(repo.absolute()).as_posix()

def _matches(patterns: Iterable[str], name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch("/" + name, pat) for pat in patterns)

def should_index(repo: Path, path: Path, cfg: ProjectConfig) -> bool:
    name = rel_name(repo, path)
    if _matches(cfg.exclude, name):
        return False
    return _matches(cfg.include, name)

def looks_binary(path: Path, sample_bytes: int = 8192) -> bool:
    try:
        head = path.read_bytes()[:sample_bytes]
    except OSError:
        return False
    return b"\x00" in head

def iter_files(repo: Path, cfg: ProjectConfig) -> Iterable[Path]:
    seen: set[str] = set()
    for path in sorted(repo.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if (not should_index(repo, path, cfg)) or looks_binary(path):
            continue
        real = str(path.resolve())
        if real in seen:
            continue
        seen.add(real)
        yield path

def tokenize(text: str) -> dict[str, float]:
    counts: dict[str, float] = {}
    for raw in TOKEN_RE.findall(text):
        pieces = [raw]
        pieces.extend(part for segment in raw.split("_") for part in CAMEL_RE.sub(" ", segment).split())
        for piece in pieces:
            token = piece.lower()
            if len(token) < 2:
                continue
            counts[token] = counts.get(token, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in counts.values())) or 1.0
    return {k: v / norm for k, v in counts.items()}

def score_tokens(query_tokens: dict[str, float], chunk_tokens: dict[str, float]) -> float:
    if not query_tokens or not chunk_tokens:
        return 0.0
    return sum(qv * chunk_tokens.get(tok, 0.0) for tok, qv in query_tokens.items())

def chunk_text(repo: Path, path: Path, cfg: ProjectConfig) -> list[CodeChunk]:
    if looks_binary(path):
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    chunks: list[CodeChunk] = []
    current: list[str] = []
    current_start = 1
    current_chars = 0
    overlap_lines = max(0, min(40, cfg.chunk_overlap // 40))
    filename = rel_name(repo, path)

    def flush(end_line: int) -> None:
        nonlocal current, current_start, current_chars
        code = "\n".join(current).strip("\n")
        if len(code) >= cfg.min_chunk_size or not chunks:
            digest = hashlib.sha256(f"{filename}:{current_start}:{end_line}:{code}".encode()).hexdigest()[:24]
            chunks.append(CodeChunk(digest, str(repo), filename, current_start, end_line, code, tokenize(code)))
        tail = current[-overlap_lines:] if overlap_lines else []
        current = list(tail)
        current_start = max(1, end_line - len(tail) + 1)
        current_chars = sum(len(line) + 1 for line in current)

    for idx, line in enumerate(lines, start=1):
        if current_chars >= cfg.chunk_size and current:
            flush(idx - 1)
        current.append(line)
        current_chars += len(line) + 1
    if current:
        flush(len(lines) or 1)
    return chunks

def build_index(repo: Path, cfg: ProjectConfig | None = None) -> IndexData:
    repo = repo.resolve()
    cfg = cfg or load_project_config(repo)
    chunks: list[CodeChunk] = []
    file_count = 0
    for path in iter_files(repo, cfg):
        file_count += 1
        chunks.extend(chunk_text(repo, path, cfg))
    return IndexData(str(repo), chunks, file_count)

def save_index(data: IndexData, repo: Path) -> Path:
    path = index_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"repo": data.repo, "files": data.files, "version": data.version, "chunks": [asdict(c) for c in data.chunks]}
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    tmp_path.replace(path)
    return path

def load_index(repo: Path) -> IndexData | None:
    path = index_path(repo)
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return IndexData(raw["repo"], [CodeChunk(**c) for c in raw.get("chunks", [])], raw.get("files", 0), raw.get("version", 1))

def refresh(repo: Path) -> dict[str, object]:
    data = build_index(repo)
    path = save_index(data, repo)
    return {"repo": str(repo.resolve()), "index_path": str(path), "files": data.files, "chunks": len(data.chunks)}

def search(repo: Path, query: str, top_k: int = 8, refresh_first: bool = False) -> dict[str, object]:
    repo = repo.resolve()
    warning = None
    if refresh_first or load_index(repo) is None:
        refresh(repo)
    data = load_index(repo)
    if data is None:
        return {"query": query, "top_k": top_k, "refresh": refresh_first, "repo": str(repo), "results": [], "warning": "index is empty; run pi-code-index refresh"}
    q = tokenize(query)
    # Detect single-identifier queries to apply an exact-match floor and signal.
    query_idents = {query.strip().lower()} if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", query.strip()) else set()
    ranked = sorted(((score_tokens(q, chunk.tokens), chunk) for chunk in data.chunks), key=lambda item: item[0], reverse=True)
    min_score = 0.20 if query_idents else 0.0
    results = []
    for score, chunk in ranked[:top_k]:
        if score <= 0:
            break
        exact = bool(query_idents and (query_idents & set(chunk.tokens.keys()) or any(ident in chunk.code for ident in query_idents)))
        if query_idents and not exact and score < min_score:
            continue
        results.append({
            "score": round(float(score), 6),
            "filename": chunk.filename,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
            "code": chunk.code,
            "exact_match": exact,
        })
    if not data.chunks:
        warning = "index is empty; check include/exclude patterns"
    return {"query": query, "top_k": top_k, "refresh": refresh_first, "repo": str(repo), "results": results, "warning": warning}
