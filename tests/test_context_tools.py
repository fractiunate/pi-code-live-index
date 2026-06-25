from __future__ import annotations

import json
from pathlib import Path

from pi_code_index import backend
from pi_code_index.cli import main
from pi_code_index.daemon import handle


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "pkg" / "config.py").write_text("def load_config():\n    return 1\n", encoding="utf-8")
    (repo / "src" / "pkg" / "cli.py").write_text("def main():\n    return load_config()\n", encoding="utf-8")
    (repo / "tests" / "test_config.py").write_text("from src.pkg.config import load_config\n\ndef test_load_config():\n    assert load_config() == 1\n", encoding="utf-8")
    return repo


def test_backend_lexical_context_payloads(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path)
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "lexical")
    backend.refresh(repo)

    repo_map = backend.repo_map(repo, "src", depth=9)
    assert repo_map["ok"] is True
    assert repo_map["depth"] == 5
    assert repo_map["capabilities"]["repo_map"] == "path_only"
    src_node = next(n for n in repo_map["nodes"] if n["path"] == "src")
    assert src_node["symbol_count"] >= 2
    assert {s["qualified_name"] for s in src_node["key_symbols"]} >= {"src.pkg.config.load_config", "src.pkg.cli.main"}

    tests = backend.find_tests(repo, ["src/pkg/config.py"], top_k=999)
    assert tests["top_k"] == 100
    assert tests["results"][0]["test_file"] == "tests/test_config.py"
    assert tests["results"][0]["metadata"]["source"] == "heuristic-v1"

    similar = backend.find_similar_code(repo, query="load config", top_k=5)
    assert similar["results"]
    assert similar["results"][0]["metadata"]["fallback_reason"] == "lexical_chunk_similarity"

    review = backend.review_context(repo, ["src/pkg/cli.py"])
    assert review["summary"]["risk_level"] in {"low", "medium"}
    assert "recommended_commands" in review


def make_retry_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "retry-repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "docs").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "retry.py").write_text(
        "import time\n\ndef retry_with_backoff(fn, attempts=3, base_delay=0.1):\n"
        "    for attempt in range(attempts):\n"
        "        try:\n"
        "            return fn()\n"
        "        except Exception:\n"
        "            if attempt == attempts - 1:\n"
        "                raise\n"
        "            time.sleep(base_delay * (2 ** attempt))\n",
        encoding="utf-8",
    )
    (repo / "src" / "http_retry.py").write_text(
        "import time\n\ndef retry_http_request(client, request, max_attempts=3):\n"
        "    for attempt in range(max_attempts):\n"
        "        response = client.send(request)\n"
        "        if response.ok:\n"
        "            return response\n"
        "        time.sleep(0.1 * (2 ** attempt))\n"
        "    return response\n",
        encoding="utf-8",
    )
    (repo / "src" / "unrelated.py").write_text("def parse_name(value):\n    return value.strip().lower()\n", encoding="utf-8")
    (repo / "README.md").write_text(("# Retry backoff helper\nretry backoff helper " * 20), encoding="utf-8")
    (repo / "docs" / "retry.md").write_text(("Retry backoff helper documentation guide. " * 20), encoding="utf-8")
    (repo / "tests" / "test_retry.py").write_text("def test_retry_with_backoff():\n    assert True\n", encoding="utf-8")
    return repo


def test_find_similar_code_lexical_source_role_prior_ranks_source_over_docs(tmp_path: Path, monkeypatch) -> None:
    repo = make_retry_repo(tmp_path)
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "lexical")
    backend.refresh(repo)
    payload = backend.find_similar_code(repo, query="retry backoff helper", mode="lexical", scope="chunks", top_k=8)
    filenames = [r["filename"] for r in payload["results"]]
    assert "src/retry.py" in filenames[:2] or "src/http_retry.py" in filenames[:2]
    first_doc = min((filenames.index(name) for name in filenames if name in {"README.md", "docs/retry.md"}), default=99)
    first_source = min(filenames.index(name) for name in filenames if name.startswith("src/"))
    assert first_source < first_doc


def test_find_similar_code_lexical_docs_target_inverts_prior(tmp_path: Path, monkeypatch) -> None:
    repo = make_retry_repo(tmp_path)
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "lexical")
    backend.refresh(repo)
    payload = backend.find_similar_code(repo, target="README.md", query="documentation guide retry backoff", mode="lexical", top_k=5)
    assert payload["results"]
    assert payload["results"][0]["metadata"]["content_role"] == "docs"
    assert "penalty:docs_for_source_query" not in payload["results"][0]["evidence"]


def test_find_similar_code_lexical_files_scope_aggregates_by_file(tmp_path: Path, monkeypatch) -> None:
    repo = make_retry_repo(tmp_path)
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "lexical")
    backend.refresh(repo)
    payload = backend.find_similar_code(repo, query="retry backoff helper", mode="hybrid", scope="files", top_k=8)
    filenames = [r["filename"] for r in payload["results"]]
    assert len(filenames) == len(set(filenames))
    assert payload["results"][0]["metadata"]["candidate_kind"] == "file"
    assert payload["results"][0]["metadata"]["aggregated_candidates"]


def test_find_similar_code_lexical_symbols_scope_warns_and_returns_chunks(tmp_path: Path, monkeypatch) -> None:
    repo = make_retry_repo(tmp_path)
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "lexical")
    backend.refresh(repo)
    payload = backend.find_similar_code(repo, query="retry backoff helper", mode="hybrid", scope="symbols", top_k=3)
    assert "symbols unavailable in lexical fallback" in payload["warning"]
    assert payload["results"][0]["metadata"]["candidate_kind"] == "chunk"


def test_find_tests_token_overlap_ranks_renamed_module_test(tmp_path: Path, monkeypatch) -> None:
    repo = make_retry_repo(tmp_path)
    # Simulate a rename of src/retry.py -> src/retry_policy.py (test stays tests/test_retry.py).
    (repo / "src" / "retry.py").rename(repo / "src" / "retry_policy.py")
    (repo / "tests" / "test_engine.py").write_text("def test_engine():\n    assert True\n", encoding="utf-8")
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "lexical")
    backend.refresh(repo)
    payload = backend.find_tests(repo, ["src/retry_policy.py"], top_k=10)
    results = payload["results"]
    assert results
    assert results[0]["test_file"].endswith("test_retry.py")


def test_find_similar_code_components_and_evidence_present(tmp_path: Path, monkeypatch) -> None:
    repo = make_retry_repo(tmp_path)
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "lexical")
    backend.refresh(repo)
    result = backend.find_similar_code(repo, query="retry backoff helper", mode="hybrid", top_k=3)["results"][0]
    assert "lexical" in result["similarity"]
    assert "structure" in result["similarity"]
    assert "role_prior" in result["similarity"]
    assert "final" in result["score_components"]
    assert result["evidence"]
    assert result["metadata"]["ranking_profile"] == "similar-code-v2-lexical-fallback"
    assert result["metadata"]["fallback_reason"] == "lexical_chunk_similarity"


def test_find_similar_code_explicit_cocoindex_semantic_symbols_errors_without_data(tmp_path: Path, monkeypatch) -> None:
    repo = make_retry_repo(tmp_path)
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("PI_CODE_INDEX_POSTGRES_URL", raising=False)

    payload = backend.find_similar_code(repo, query="retry backoff helper", mode="semantic", scope="symbols", top_k=3)

    assert payload["ok"] is False
    assert payload["backend"] == "cocoindex"
    assert "error" in payload
    assert any(hint in payload["error"] for hint in ("CocoIndex", "Postgres", "refresh", "connect"))


def test_context_refresh_updates_symbol_ranges_and_review_similar_content(tmp_path: Path, monkeypatch) -> None:
    repo = make_retry_repo(tmp_path)
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "lexical")
    backend.refresh(repo)
    updated_retry = """import time


class RetryError(Exception):
    pass


def retry_delay(attempt, base_delay=0.1):
    return base_delay * (2 ** attempt)


def should_retry(attempt, attempts):
    return attempt + 1 < attempts


def retry_operation(operation, attempts=3, base_delay=0.1):
    last_error = None
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if should_retry(attempt, attempts):
                time.sleep(retry_delay(attempt, base_delay))
    raise RetryError("operation failed") from last_error
"""
    updated_test = """from src.retry import retry_delay, retry_operation


def test_retry_delay():
    assert retry_delay(2, 0.5) == 2.0


def test_retry_operation_new():
    assert retry_operation(lambda: "fresh") == "fresh"
"""
    (repo / "src" / "retry.py").write_text(updated_retry, encoding="utf-8")
    (repo / "tests" / "test_retry.py").write_text(updated_test, encoding="utf-8")

    repo_map = backend.repo_map(repo, "src/retry.py", depth=2, refresh_first=True)
    symbols = {s["qualified_name"]: s for n in repo_map["nodes"] if n["path"] == "src/retry.py" for s in n["key_symbols"]}
    lines = updated_retry.splitlines()
    retry_start = next(i for i, line in enumerate(lines, 1) if line.startswith("def retry_operation"))
    retry_end = next(i for i, line in enumerate(lines, 1) if line.startswith("    raise RetryError"))
    assert "src.retry.retry_delay" in symbols
    assert symbols["src.retry.retry_operation"]["start_line"] == retry_start
    assert symbols["src.retry.retry_operation"]["end_line"] == retry_end

    review = backend.review_context(repo, ["src/retry.py"], top_k=30, refresh_first=True)
    similar_section = next(s for s in review["sections"] if s["section"] == "similar_code")
    test_hit = next(item for item in similar_section["items"] if item["filename"] == "tests/test_retry.py")
    assert "retry_delay" in test_hit["code"]
    assert "fresh" in test_hit["code"]
    assert "assert True" not in test_hit["code"]


def test_context_cli_direct_json_and_daemon_dispatch(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path)
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "lexical")
    assert main(["--no-daemon", "context", "repo-map", "--json", "--repo", str(repo), "--target", "src", "--depth", "5"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["depth"] == 5

    response = handle({"type": "find_tests", "repo": str(repo), "targets": ["src/pkg/config.py"], "top_k": 999})
    assert response["ok"] is True
    assert response["top_k"] == 100

    similar = handle({"type": "find_similar_code", "repo": str(repo), "target": "src/pkg/config.py", "query": "load config", "mode": "lexical", "scope": "files", "exclude_self": False, "top_k": 999})
    assert similar["ok"] is True
    assert similar["top_k"] == 100
    assert similar["scope"] == "files"
    assert similar["exclude_self"] is False

    bad = handle({"type": "find_similar_code", "repo": str(repo)})
    assert bad["ok"] is False
    assert "target or query" in bad["error"]
