from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pi_code_index.config import load_global_config, load_project_config, project_config_path
from pi_code_index.coco_backend import (
    CANONICAL_PIPELINE_VERSION,
    CANONICAL_SCHEMA_VERSION,
    _rank_search_rows,
    _resolve_symbol,
    branch_id_for,
    chunk_id_for,
    ensure_canonical_schema,
    extract_ast_chunks,
    file_id_for,
    repo_id_for,
    repo_identity,
    symbol_id_for,
    worktree_id_for,
)
from pi_code_index.daemon import BackendResourceCache, daemon_metadata
from pi_code_index.backend import find_callers as backend_find_callers, impact_analysis as backend_impact_analysis, symbol_context as backend_symbol_context, symbol_definition as backend_symbol_definition, symbol_search as backend_symbol_search


def test_config_defaults_and_env_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PI_CODE_INDEX_SCHEMA_NAME", "codeidx")
    monkeypatch.setenv("PI_CODE_INDEX_TABLE_PREFIX", "tenant_one")
    monkeypatch.setenv("PI_CODE_INDEX_PIPELINE_VERSION", "canonical-v1-test")

    global_cfg = load_global_config()
    project_cfg = load_project_config(repo)

    assert global_cfg.schema_name == "codeidx"
    assert global_cfg.table_prefix == "tenant_one"
    assert global_cfg.pipeline_version == "canonical-v1-test"
    assert project_cfg.table_name == "code_embeddings"
    assert project_cfg.branch_mode == "current"
    assert project_cfg.compatibility_view is True
    assert project_cfg.chunk_strategy == "recursive"
    assert project_cfg.ast_languages is None
    assert project_cfg.max_ast_chunk_bytes > 0
    assert project_cfg.max_result_code_bytes > 0
    assert project_cfg.ast_context_lines > 0
    assert CANONICAL_SCHEMA_VERSION == 1
    assert CANONICAL_PIPELINE_VERSION == "canonical-v1-ast-v1"


def test_invalid_chunk_config_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_CHUNK_STRATEGY", "bogus")

    with pytest.raises(ValueError, match="chunk_strategy"):
        load_project_config(repo)


def test_ast_env_overrides_are_normalized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_CHUNK_STRATEGY", "HYBRID")
    monkeypatch.setenv("PI_CODE_INDEX_AST_LANGUAGES", "Python")
    monkeypatch.setenv("PI_CODE_INDEX_MAX_AST_CHUNK_BYTES", "2048")

    cfg = load_project_config(repo)

    assert cfg.chunk_strategy == "hybrid"
    assert cfg.ast_languages == ["python"]
    assert cfg.max_ast_chunk_bytes == 2048


def test_psql_e2e_python_fixture_enables_symbols_and_references():
    spec = (Path(__file__).parents[1] / "docs/architecture/psql-first-e2e-validation-spec.md").read_text(encoding="utf-8")

    assert 'cat > "$PY/.pi-code-index/settings.yml"' in spec
    assert "chunk_strategy: hybrid" in spec
    assert "enable_symbols: true" in spec
    assert "enable_references: true" in spec


def test_invalid_identifiers_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project_config_path(repo).parent.mkdir(parents=True)
    project_config_path(repo).write_text("table_name: 'bad;drop'\n", encoding="utf-8")

    with pytest.raises(ValueError, match="table_name"):
        load_project_config(repo)


def test_stable_canonical_ids_are_deterministic_and_scoped(tmp_path: Path):
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()

    repo_a_id = repo_id_for(repo_a)
    assert repo_a_id == repo_id_for(repo_a)
    assert len(repo_a_id) == 32
    assert repo_a_id != repo_id_for(repo_b)

    worktree_id = worktree_id_for(repo_a, "/tmp/common.git")
    branch_id = branch_id_for(repo_a_id, "main", "abc123")
    file_id = file_id_for(repo_a_id, branch_id, "src/app.py")
    chunk_id = chunk_id_for(file_id, 0, 10, "print(1)")

    assert worktree_id == worktree_id_for(repo_a, "/tmp/common.git")
    assert file_id == file_id_for(repo_a_id, branch_id, "src/app.py")
    assert chunk_id == chunk_id_for(file_id, 0, 10, "print(1)")
    assert chunk_id != chunk_id_for(file_id, 1, 10, "print(1)")


class _RecordingConn:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, query: str, *params: object) -> None:
        self.statements.append(query)


def test_canonical_references_schema_avoids_postgres_reserved_column(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project_cfg = load_project_config(repo)
    global_cfg = load_global_config()
    conn = _RecordingConn()

    asyncio.run(ensure_canonical_schema(conn, project_cfg, global_cfg))

    ddl = "\n".join(conn.statements)
    assert "column_number integer NOT NULL DEFAULT 0" in ddl
    assert " column integer NOT NULL DEFAULT 0" not in ddl


def test_python_ast_extraction_creates_stable_symbol_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_CHUNK_STRATEGY", "hybrid")
    cfg = load_project_config(repo)
    file_id = "f" * 32
    source = "@decorator\ndef load_config(path: str):\n    \"\"\"Load settings.\"\"\"\n    return path\n"

    extraction = extract_ast_chunks("src/app.py", source, file_id, "hash", cfg)

    function_chunk = next(chunk for chunk in extraction.chunks if chunk.chunk_kind == "function")
    assert function_chunk.start_line == 1
    assert function_chunk.symbol == "load_config"
    assert function_chunk.qualified_name == "app.load_config"
    assert function_chunk.symbol_id == symbol_id_for(file_id, "app.load_config", "function", 1)
    assert "@decorator" in function_chunk.code
    assert function_chunk.metadata["lineage"]["parser"] == "python_ast"
    function_symbol = next(symbol for symbol in extraction.symbols if symbol.name == "load_config")
    assert function_symbol.signature.startswith("def load_config")
    assert function_symbol.metadata["parent_symbol_id"] == symbol_id_for(file_id, "app", "module", 1)
    assert function_symbol.metadata["decorators"] == ["@decorator"]


def test_python_ast_class_method_and_nested_function_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_CHUNK_STRATEGY", "hybrid")
    cfg = load_project_config(repo)
    source = "class Loader:\n    def load(self):\n        def inner():\n            return 1\n        return inner()\n"

    extraction = extract_ast_chunks("src/loader.py", source, "f" * 32, "hash", cfg)

    chunks = {chunk.qualified_name: chunk for chunk in extraction.chunks if chunk.qualified_name}
    assert chunks["loader.Loader"].chunk_kind == "class"
    assert chunks["loader.Loader.load"].chunk_kind == "method"
    assert chunks["loader.Loader.load.inner"].chunk_kind == "method"
    assert chunks["loader.Loader.load"].parent_symbol_id == chunks["loader.Loader"].symbol_id


def test_python_ast_parse_error_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_CHUNK_STRATEGY", "ast")
    cfg = load_project_config(repo)

    extraction = extract_ast_chunks("bad.py", "def broken(:\n", "f" * 32, "hash", cfg)

    assert extraction.fallback_reason == "parse_error"
    assert extraction.parser_error


def test_unsupported_language_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_CHUNK_STRATEGY", "ast")
    cfg = load_project_config(repo)

    extraction = extract_ast_chunks("README.md", "# docs\n", "f" * 32, "hash", cfg)

    assert extraction.fallback_reason == "unsupported_language"


def test_search_ranker_preserves_required_fields_and_metadata():
    rows = [
        {
            "filename": "src/app.py",
            "start_line": 1,
            "end_line": 2,
            "code": "def load_config(): pass",
            "score": 0.8,
            "metadata": {"backend": "cocoindex", "chunk_id": "abc"},
        }
    ]

    result = _rank_search_rows(rows, "load config", 3)[0]

    assert result["filename"] == "src/app.py"
    assert result["start_line"] == 1
    assert result["end_line"] == 2
    assert result["code"] == "def load_config(): pass"
    assert result["result_id"] == "abc"
    assert result["metadata"]["backend"] == "cocoindex"
    assert result["metadata"]["chunk_id"] == "abc"
    assert result["metadata"]["ranking"]["final_score"] == result["score"]
    assert result["metadata"]["truncation"]["code_truncated"] is False


def test_ranker_parses_asyncpg_jsonb_metadata_strings():
    rows = [
        {
            "filename": "src/config.py",
            "start_line": 1,
            "end_line": 2,
            "code": "def load_config(): pass",
            "score": 0.7,
            "metadata": json.dumps({"chunk_id": "chunk-1", "chunk_kind": "function", "symbol": "load_config"}),
        }
    ]

    result = _rank_search_rows(rows, "load_config", 1)[0]

    assert result["result_id"] == "chunk-1"
    assert result["metadata"]["chunk_id"] == "chunk-1"
    assert result["metadata"]["backend"] == "cocoindex"


def test_ranker_prefers_symbol_name_match_and_deduplicates_by_chunk_id():
    rows = [
        {
            "filename": "src/config.py",
            "start_line": 10,
            "end_line": 12,
            "code": "def load_global_config(): return {}",
            "score": 0.2,
            "metadata": {"backend": "cocoindex", "chunk_id": "owned", "chunk_kind": "function", "symbol": "load_global_config", "qualified_name": "config.load_global_config", "freshness_status": "current"},
        },
        {
            "filename": "docs/config.md",
            "start_line": 1,
            "end_line": 2,
            "code": "load global config load global config",
            "score": 0.2,
            "metadata": {"backend": "cocoindex", "chunk_id": "text", "chunk_kind": "text", "freshness_status": "current"},
        },
        {
            "filename": "src/config.py",
            "start_line": 10,
            "end_line": 12,
            "code": "duplicate lower score",
            "score": 0.1,
            "metadata": {"backend": "cocoindex", "chunk_id": "owned", "chunk_kind": "function", "symbol": "load_global_config"},
        },
    ]

    ranked = _rank_search_rows(rows, "load_global_config", 5)

    assert [result["result_id"] for result in ranked] == ["owned", "text"]
    assert ranked[0]["metadata"]["ranking"]["symbol_score"] > 0


def test_resolve_symbol_short_method_suffix_and_file_line_innermost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ident = repo_identity(repo)
    fid = file_id_for(repo_id_for(repo), ident["branch_id"], "src/dummy_retry/client.py")

    def row(qname: str, name: str, kind: str, start: int, end: int) -> dict[str, object]:
        return {
            "symbol_id": symbol_id_for(fid, qname, kind, start),
            "file_id": fid,
            "repo_id": ident["repo_id"],
            "branch_id": ident["branch_id"],
            "name": name,
            "qualified_name": qname,
            "kind": kind,
            "start_line": start,
            "end_line": end,
            "signature": None,
            "docstring": None,
            "metadata": {"language": "python", "module": "dummy_retry.client"},
            "filename": "src/dummy_retry/client.py",
            "start_byte": 0,
            "end_byte": 0,
            "code": "",
        }

    rows = [
        row("dummy_retry.client", "client", "module", 1, 30),
        row("dummy_retry.client.HTTPClient", "HTTPClient", "class", 10, 20),
        row("dummy_retry.client.HTTPClient.request", "request", "method", 12, 14),
        row("dummy_retry.client.Other.request", "request", "method", 22, 24),
    ]

    class Conn:
        def __init__(self, symbol_rows: list[dict[str, object]]):
            self.symbol_rows = symbol_rows

        async def fetch(self, sql: str, *params: object):
            if "f.path = $3 AND s.start_line <= $4" in sql:
                path, line = params[2], int(params[3])
                return [r for r in self.symbol_rows if r["filename"] == path and int(r["start_line"]) <= line <= int(r["end_line"])]
            if "right(s.qualified_name" in sql:
                target = str(params[2])
                return [r for r in self.symbol_rows if str(r["qualified_name"]).endswith("." + target)]
            if "s.qualified_name = $3 OR s.name = $3" in sql:
                target = params[2]
                return [r for r in self.symbol_rows if r["qualified_name"] == target or r["name"] == target]
            if "s.kind = 'module'" in sql:
                return [r for r in self.symbol_rows if r["kind"] == "module" and r["filename"] == params[2]][:1]
            return []

    cfg = load_project_config(repo)
    global_cfg = load_global_config()
    definition, matches, warning = asyncio.run(_resolve_symbol(Conn(rows), repo, "HTTPClient.request", cfg, global_cfg))
    assert definition and definition["qualified_name"] == "dummy_retry.client.HTTPClient.request"
    assert matches == []
    assert warning is None

    definition, matches, warning = asyncio.run(_resolve_symbol(Conn(rows), repo, "src/dummy_retry/client.py:12", cfg, global_cfg))
    assert definition and definition["qualified_name"] == "dummy_retry.client.HTTPClient.request"
    assert matches == []
    assert warning is None

    ambiguous_rows = [*rows, row("vendor.HTTPClient.request", "request", "method", 1, 2)]
    definition, matches, warning = asyncio.run(_resolve_symbol(Conn(ambiguous_rows), repo, "HTTPClient.request", cfg, global_cfg))
    assert definition is None
    assert {match["qualified_name"] for match in matches} == {"dummy_retry.client.HTTPClient.request", "vendor.HTTPClient.request"}
    assert warning == "ambiguous target; retry with symbol_id or qualified_name"


def test_unknown_backend_is_rejected_for_symbols_and_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "json")

    for call in (
        lambda: backend_symbol_search(repo, "load config", 3, {"kind": "function"}),
        lambda: backend_symbol_definition(repo, "load_config"),
        lambda: backend_symbol_context(repo, "load_config", 2),
        lambda: backend_find_callers(repo, "compute_tax"),
        lambda: backend_impact_analysis(repo, "compute_tax"),
    ):
        with pytest.raises(ValueError, match="invalid backend 'json'"):
            call()


def test_daemon_metadata_and_resource_key_include_canonical_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    monkeypatch.setenv("POSTGRES_URL", "postgres://example/test")
    monkeypatch.setenv("PI_CODE_INDEX_TABLE_PREFIX", "prefix_one")

    metadata = daemon_metadata(config_mtime=123)
    key_one = BackendResourceCache()._key(repo)
    monkeypatch.setenv("PI_CODE_INDEX_TABLE_PREFIX", "prefix_two")
    key_two = BackendResourceCache()._key(repo)

    assert metadata["schema_version"] == 1
    assert metadata["pipeline_version"] == "canonical-v1-ast-v1"
    assert metadata["ranking_profile"] == "semantic_ast_v1"
    assert key_one != key_two
