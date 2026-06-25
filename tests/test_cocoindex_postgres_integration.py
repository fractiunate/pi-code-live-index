from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

from pi_code_index.backend import find_callees as backend_find_callees
from pi_code_index.backend import find_callers as backend_find_callers
from pi_code_index.backend import find_similar_code as backend_find_similar_code
from pi_code_index.backend import impact_analysis as backend_impact_analysis
from pi_code_index.backend import refresh as backend_refresh
from pi_code_index.backend import search as backend_search
from pi_code_index.backend import status as backend_status
from pi_code_index.backend import symbol_search as backend_symbol_search
from pi_code_index.config import project_config_path
from pi_code_index.coco_backend import _quote_ident


pytestmark = pytest.mark.integration


def _postgres_url_or_skip() -> str:
    postgres_url = os.environ.get("POSTGRES_URL") or os.environ.get("PI_CODE_INDEX_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("set POSTGRES_URL to run CocoIndex/Postgres integration tests")
    return postgres_url


async def _drop_table(postgres_url: str, table_name: str) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    pool = await asyncpg.create_pool(postgres_url)
    try:
        async with pool.acquire() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {_quote_ident(table_name)}")
    finally:
        await pool.close()


@pytest.fixture
def cocoindex_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    postgres_url = _postgres_url_or_skip()
    pytest.importorskip("cocoindex")
    pytest.importorskip("sentence_transformers")

    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "settings.py").write_text(
        "def load_runtime_config(path):\n"
        "    '''Load YAML settings and merge environment overrides.'''\n"
        "    return {'debug': True, 'source': path}\n\n"
        "def start_app():\n"
        "    return load_runtime_config('settings.yml')\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "# Sample project\n\nThis repository exercises the integration test fixture.\n",
        encoding="utf-8",
    )

    suffix = uuid.uuid4().hex
    table_name = f"code_embeddings_{suffix}"
    table_prefix = f"pi_code_index_{suffix[:12]}"
    project_config_path(repo).parent.mkdir(parents=True, exist_ok=True)
    project_config_path(repo).write_text(
        "backend: cocoindex\n"
        f"table_name: {table_name}\n"
        f"table_prefix: {table_prefix}\n"
        "chunk_size: 240\n"
        "min_chunk_size: 1\n"
        "chunk_overlap: 20\n"
        "chunk_strategy: hybrid\n"
        "enable_symbols: true\n"
        "enable_references: true\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    monkeypatch.setenv("POSTGRES_URL", postgres_url)
    monkeypatch.delenv("PI_CODE_INDEX_POSTGRES_URL", raising=False)

    asyncio.run(_drop_table(postgres_url, table_name))
    try:
        yield repo, table_name
    finally:
        asyncio.run(_drop_table(postgres_url, table_name))


def test_refresh_then_semantic_search_uses_real_pgvector(cocoindex_repo):
    repo, table_name = cocoindex_repo

    refresh_payload = backend_refresh(repo)

    assert refresh_payload["ok"] is True
    assert refresh_payload["backend"] == "cocoindex"
    assert refresh_payload["table_name"] == table_name
    assert refresh_payload["chunk_strategy"] == "hybrid"
    assert refresh_payload["counts"]["ast_chunks"] >= 1
    assert refresh_payload["counts"]["recursive_chunks"] >= 1
    assert refresh_payload["counts"]["symbols"] >= 1

    search_payload = backend_search(repo, "where is runtime config loaded", top_k=3, refresh_first=False)

    assert search_payload["ok"] is True
    assert search_payload["backend"] == "cocoindex"
    assert search_payload["results"]
    assert search_payload["ranking_profile"] == "semantic_ast_v1"
    settings_matches = [result for result in search_payload["results"] if result["filename"] == "src/settings.py"]
    assert settings_matches
    assert "load_runtime_config" in settings_matches[0]["code"]
    metadata = settings_matches[0]["metadata"]
    assert metadata["chunk_id"] == settings_matches[0]["result_id"]
    assert metadata["chunk_strategy"] == "ast"
    assert metadata["lineage"]["parser"] == "python_ast"
    assert metadata["ranking"]["final_score"] == settings_matches[0]["score"]
    assert metadata["freshness_status"] == "current"

    status_payload = backend_status(repo)
    assert status_payload["counts"]["ast_chunks"] >= 1
    assert status_payload["counts"]["recursive_chunks"] >= 1
    assert status_payload["counts"]["symbols"] >= 1
    assert status_payload["counts"]["references"] >= 1
    assert status_payload["counts"]["call_edges"] >= 1
    assert status_payload["counts"]["parser_errors"] == 0


def test_refresh_populates_symbol_and_graph_tools(cocoindex_repo):
    repo, _table_name = cocoindex_repo

    refresh_payload = backend_refresh(repo)

    assert refresh_payload["ok"] is True
    assert refresh_payload["counts"]["symbols"] >= 2
    assert refresh_payload["counts"]["references"] >= 1
    assert refresh_payload["counts"]["call_edges"] >= 1

    symbol_payload = backend_symbol_search(repo, "load_runtime_config", top_k=5, refresh_first=False)
    assert symbol_payload["ok"] is True
    assert any(result["qualified_name"] == "settings.load_runtime_config" for result in symbol_payload["results"])

    callers_payload = backend_find_callers(repo, "settings.load_runtime_config", depth=1, top_k=5, refresh_first=False)
    assert callers_payload["ok"] is True
    assert any(result["symbol"]["qualified_name"] == "settings.start_app" for result in callers_payload["results"])

    callees_payload = backend_find_callees(repo, "settings.start_app", depth=1, top_k=5, refresh_first=False)
    assert callees_payload["ok"] is True
    assert any(result["symbol"]["qualified_name"] == "settings.load_runtime_config" for result in callees_payload["results"])

    impact_payload = backend_impact_analysis(repo, "settings.load_runtime_config", depth=1, top_k=5, include_tests=False, refresh_first=False)
    assert impact_payload["ok"] is True
    assert impact_payload["summary"]["direct_callers"] >= 1
    assert any(item["symbol"]["qualified_name"] == "settings.start_app" for item in impact_payload["affected_symbols"])


def test_refresh_removes_deleted_files_from_canonical_results(cocoindex_repo):
    repo, _table_name = cocoindex_repo
    assert backend_refresh(repo)["ok"] is True

    settings = repo / "src" / "settings.py"
    settings.unlink()
    refresh_payload = backend_refresh(repo)

    assert refresh_payload["ok"] is True
    assert refresh_payload["counts"]["deleted_files"] >= 1
    search_payload = backend_search(repo, "load_runtime_config", top_k=5, refresh_first=False)
    assert all(result["filename"] != "src/settings.py" for result in search_payload["results"])
    symbol_payload = backend_symbol_search(repo, "load_runtime_config", top_k=5, refresh_first=False)
    assert all(result["filename"] != "src/settings.py" for result in symbol_payload["results"])


def test_find_similar_code_symbols_scope_returns_symbol_candidates(cocoindex_repo):
    repo, _table_name = cocoindex_repo
    refresh_payload = backend_refresh(repo)
    assert refresh_payload["ok"] is True

    payload = backend_find_similar_code(repo, query="runtime config loader", mode="semantic", scope="symbols", top_k=5, refresh_first=False)

    assert payload["ok"] is True
    assert payload["backend"] == "cocoindex"
    assert payload["ranking_profile"] == "similar-code-v2"
    assert payload["results"]
    assert any(result["metadata"]["candidate_kind"] == "symbol" for result in payload["results"])
    best = payload["results"][0]
    assert best["symbol_id"]
    assert best["symbol"]
    assert best["similarity"]["semantic"] > 0
    assert best["score_components"]["semantic"] > 0
    assert any(item.startswith("semantic:symbol_vector=") for item in best["evidence"])


def test_find_similar_code_files_scope_aggregates_cocoindex_candidates(cocoindex_repo):
    repo, _table_name = cocoindex_repo
    refresh_payload = backend_refresh(repo)
    assert refresh_payload["ok"] is True

    payload = backend_find_similar_code(repo, query="runtime config loader", mode="hybrid", scope="files", top_k=5, refresh_first=False)

    assert payload["ok"] is True
    filenames = [result["filename"] for result in payload["results"]]
    assert filenames
    assert len(filenames) == len(set(filenames))
    assert payload["results"][0]["metadata"]["candidate_kind"] == "file"
    assert payload["results"][0]["metadata"]["aggregated_candidates"]


def test_review_context_computes_impact_for_file_target(cocoindex_repo):
    from pi_code_index.backend import review_context as backend_review_context

    repo, _table_name = cocoindex_repo
    assert backend_refresh(repo)["ok"] is True

    payload = backend_review_context(repo, ["src/settings.py"], top_k=20, include_impact=True, refresh_first=False)

    sections = {s.get("section"): s for s in payload.get("sections", [])}
    assert "impact" in sections
    impact = sections["impact"]
    assert impact.get("items")
    assert impact.get("warning") is None
    assert impact.get("impact_summary", {}).get("affected_files", 0) >= 1
