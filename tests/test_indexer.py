from pathlib import Path
from types import SimpleNamespace

from pi_code_index.backend import choose_backend, search as backend_search, status as backend_status
from pi_code_index.coco_backend import CocoIndexUnavailable, _effective_postgres_url, _rank_search_rows
from pi_code_index.config import GlobalConfig, ProjectConfig, load_global_config, load_project_config, project_config_path, global_config_path
from pi_code_index.indexer import build_index, iter_files, search, should_index, tokenize


def test_tokenize_normalizes_identifiers():
    tokens = tokenize("ConfigLoader loads config")
    assert "configloader" in tokens
    assert "config" in tokens


def test_iter_files_dedupes_symlinked_files(tmp_path: Path, monkeypatch):
    repo = tmp_path
    (repo / ".git").mkdir()
    (repo / "real.py").write_text("def f(): pass\n", encoding="utf-8")
    (repo / "link.py").symlink_to(repo / "real.py")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    names = sorted(p.name for p in iter_files(repo, ProjectConfig()))
    assert names == ["real.py"]


def test_iter_files_skips_binary_files(tmp_path: Path, monkeypatch):
    repo = tmp_path
    (repo / ".git").mkdir()
    (repo / "text.py").write_text("def f(): pass\n", encoding="utf-8")
    (repo / "hidden_binary.py").write_bytes(b"def g(): pass\n\x00\x01\x02\xff binary junk\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    paths = sorted(p.name for p in iter_files(repo, ProjectConfig()))
    assert "text.py" in paths
    assert "hidden_binary.py" not in paths


def test_should_index_respects_exclude(tmp_path: Path):
    repo = tmp_path
    node_file = repo / "node_modules" / "x.js"
    node_file.parent.mkdir()
    node_file.write_text("ignored", encoding="utf-8")
    assert not should_index(repo, node_file, ProjectConfig())


def test_build_and_search_index(tmp_path: Path, monkeypatch):
    repo = tmp_path
    (repo / ".git").mkdir()
    (repo / "settings.py").write_text("def load_config():\n    return {'debug': True}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    data = build_index(repo)
    assert data.files == 1
    payload = search(repo, "where is config loaded", top_k=3, refresh_first=True)
    assert payload["results"]
    assert payload["results"][0]["filename"] == "settings.py"


def test_search_flags_exact_identifier_match(tmp_path: Path, monkeypatch):
    repo = tmp_path
    (repo / ".git").mkdir()
    (repo / "settings.py").write_text("def load_config():\n    return {'debug': True}\n\nCONFIG_VALUE = 1\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    payload = search(repo, "load_config", top_k=5, refresh_first=True)
    top = payload["results"][0]
    assert top["exact_match"] is True


def test_search_returns_no_results_for_unmatchable_query(tmp_path: Path, monkeypatch):
    repo = tmp_path
    (repo / ".git").mkdir()
    (repo / "settings.py").write_text("def load_config():\n    return {'debug': True}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    payload = search(repo, "qzxjkv_939393_unmatchable_nonsense", top_k=5, refresh_first=True)
    assert payload["results"] == []


def test_coco_ranker_boosts_identifier_matches():
    rows = [
        {
            "score": 0.40,
            "filename": "docs/settings.md",
            "start_line": 1,
            "end_line": 2,
            "code": "Runtime settings are configurable.",
        },
        {
            "score": 0.35,
            "filename": "src/pi_code_index/config.py",
            "start_line": 50,
            "end_line": 70,
            "code": "def load_global_config():\n    return GlobalConfig()\ndef load_project_config(repo): ...",
        },
    ]

    results = _rank_search_rows(rows, "where is config loaded", top_k=1)

    assert results[0]["filename"] == "src/pi_code_index/config.py"


def test_backend_auto_uses_lexical_without_postgres_env(tmp_path: Path, monkeypatch):
    repo = tmp_path
    (repo / ".git").mkdir()
    (repo / "settings.py").write_text("def load_config():\n    return {'debug': True}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("PI_CODE_INDEX_POSTGRES_URL", raising=False)
    monkeypatch.delenv("PI_CODE_INDEX_BACKEND", raising=False)

    assert choose_backend(repo).name == "lexical"
    payload = backend_search(repo, "config", top_k=1, refresh_first=True)
    assert payload["backend"] == "lexical"
    assert payload["requested_backend"] == "auto"
    assert payload["backend_fallback"] is False
    assert "Lexical degraded mode" in payload["warnings"][0]
    assert payload["results"]


def test_backend_lexical_forced_even_with_postgres_env(tmp_path: Path, monkeypatch):
    repo = tmp_path
    (repo / ".git").mkdir()
    (repo / "settings.py").write_text("def load_config():\n    return {'debug': True}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "lexical")
    monkeypatch.setenv("PI_CODE_INDEX_POSTGRES_URL", "postgres://cocoindex:cocoindex@localhost:5432/cocoindex")

    assert choose_backend(repo).name == "lexical"
    payload = backend_status(repo)
    assert payload["backend"] == "lexical"
    assert payload["requested_backend"] == "lexical"
    assert payload["backend_fallback"] is False
    assert any("Lexical degraded mode" in warning for warning in payload["warnings"])


def test_backend_auto_fallback_and_cocoindex_required_errors(tmp_path: Path, monkeypatch):
    repo = tmp_path
    (repo / ".git").mkdir()
    (repo / "settings.py").write_text("def load_config():\n    return {'debug': True}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_POSTGRES_URL", "postgres://cocoindex:cocoindex@localhost:5432/cocoindex")

    def fail(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr("pi_code_index.coco_backend.search", fail)
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "auto")
    fallback = backend_search(repo, "config", top_k=1, refresh_first=True)
    assert fallback["backend"] == "lexical"
    assert fallback["backend_fallback"] is True
    assert "runtime/postgres/podman-pgvector.sh" in fallback["warnings"][-1]
    assert "scripts/setup.sh --with-cocoindex --postgres-check" in fallback["warnings"][-1]

    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    required = backend_search(repo, "config", top_k=1, refresh_first=True)
    assert required["ok"] is False
    assert required["backend"] == "cocoindex"
    assert "PI_CODE_INDEX_POSTGRES_URL" in required["error"]
    assert "runtime/postgres/podman-pgvector.sh" in required["error"]


def test_config_backend_precedence_env_project_global(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PI_CODE_INDEX_BACKEND", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("PI_CODE_INDEX_POSTGRES_URL", raising=False)

    global_config_path().parent.mkdir(parents=True, exist_ok=True)
    global_config_path().write_text("backend: lexical\n", encoding="utf-8")
    project_config_path(repo).parent.mkdir(parents=True, exist_ok=True)
    project_config_path(repo).write_text("backend: cocoindex\n", encoding="utf-8")

    assert load_global_config().backend == "lexical"
    assert load_project_config(repo).backend == "cocoindex"
    assert choose_backend(repo).name == "cocoindex"

    project_config_path(repo).write_text("backend: auto\n", encoding="utf-8")
    assert choose_backend(repo).name == "lexical"

    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    assert load_global_config().backend == "cocoindex"
    assert load_project_config(repo).backend == "cocoindex"
    assert choose_backend(repo).name == "cocoindex"


def test_backend_cocoindex_without_postgres_url_reports_setup_error_without_connecting(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("PI_CODE_INDEX_POSTGRES_URL", raising=False)
    monkeypatch.setattr("pi_code_index.coco_backend._require_coco", lambda: None)
    monkeypatch.setattr(
        "pi_code_index.coco_backend.asyncpg",
        SimpleNamespace(create_pool=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("create_pool called"))),
    )

    payload = backend_status(repo)

    assert payload["ok"] is False
    assert payload["backend"] == "cocoindex"
    assert "Postgres URL is required" in payload["error"]
    assert "PI_CODE_INDEX_POSTGRES_URL" in payload["error"]
    assert "runtime/postgres/podman-pgvector.sh" in payload["error"]
    assert "scripts/setup.sh --with-cocoindex --postgres-check" in payload["error"]
    assert "create_pool called" not in payload["error"]


def test_effective_postgres_url_requires_configured_url(monkeypatch):
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("PI_CODE_INDEX_POSTGRES_URL", raising=False)

    try:
        _effective_postgres_url(GlobalConfig())
    except CocoIndexUnavailable as exc:
        assert "PI_CODE_INDEX_POSTGRES_URL" in str(exc)
    else:
        raise AssertionError("missing Postgres URL should fail before asyncpg defaults to localhost")


def test_config_postgres_url_precedence(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("PI_CODE_INDEX_POSTGRES_URL", raising=False)
    global_config_path().parent.mkdir(parents=True, exist_ok=True)
    global_config_path().write_text("postgres_url: postgres://file/file\n", encoding="utf-8")

    cfg = load_global_config()
    assert cfg.postgres_url == "postgres://file/file"
    assert _effective_postgres_url(cfg) == "postgres://file/file"

    monkeypatch.setenv("PI_CODE_INDEX_POSTGRES_URL", "postgres://pi/env")
    cfg = load_global_config()
    assert cfg.postgres_url == "postgres://pi/env"
    assert _effective_postgres_url(GlobalConfig(postgres_url="postgres://file/file")) == "postgres://pi/env"

    monkeypatch.setenv("POSTGRES_URL", "postgres://plain/env")
    cfg = load_global_config()
    assert cfg.postgres_url == "postgres://pi/env"
    assert _effective_postgres_url(cfg) == "postgres://pi/env"
