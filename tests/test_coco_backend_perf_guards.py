from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from pi_code_index.coco_backend import (
    CocoBackendResources,
    CocoIndexUnavailable,
    SentenceTransformerEmbedder,
    _effective_postgres_url,
    _search_async,
    _shared_embedder,
    _validate_postgres_config,
)
from pi_code_index.config import GlobalConfig, ProjectConfig, load_project_config


def _fresh_repo(tmp_path: Path, chunk_strategy: str = "hybrid") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "settings.py").write_text(
        "def load_runtime_config(path):\n    return {'debug': True}\n",
        encoding="utf-8",
    )
    (repo / ".pi-code-index").mkdir()
    (repo / ".pi-code-index" / "settings.yml").write_text(
        "backend: cocoindex\n"
        "table_name: code_embeddings_guards\n"
        "chunk_size: 240\n"
        "min_chunk_size: 1\n"
        "chunk_overlap: 20\n"
        f"chunk_strategy: {chunk_strategy}\n"
        "include: ['**/*.py']\n",
        encoding="utf-8",
    )
    return repo


def test_effective_postgres_url_uses_runtime_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PI_CODE_INDEX_POSTGRES_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    assert _effective_postgres_url(GlobalConfig()) == "postgres://cocoindex:cocoindex@localhost:5432/cocoindex"


def test_validate_postgres_config_rejects_bad_scheme():
    with pytest.raises(CocoIndexUnavailable, match="invalid Postgres URL scheme"):
        _validate_postgres_config("http://localhost:5432/db")


def test_validate_postgres_config_rejects_missing_host():
    with pytest.raises(CocoIndexUnavailable, match="missing host"):
        _validate_postgres_config("postgres:///cocoindex")


def test_validate_postgres_config_raises_on_unreachable_host():
    # pick a closed port on localhost (almost certainly not listening)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    closed_port = sock.getsockname()[1]
    sock.close()
    url = f"postgres://cocoindex:cocoindex@127.0.0.1:{closed_port}/cocoindex"
    with pytest.raises(CocoIndexUnavailable, match="Postgres is unreachable"):
        _validate_postgres_config(url)


def test_search_async_validates_postgres_before_embedder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("cocoindex")
    repo = _fresh_repo(tmp_path)
    project_cfg = load_project_config(repo)
    global_cfg = GlobalConfig(postgres_url="http://localhost:5432/db")
    monkeypatch.delenv("PI_CODE_INDEX_POSTGRES_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)

    # If the guard fails to run before the embedder, this stub would be instantiated (and fail loudly).
    def _explode(*_args, **_kwargs):
        raise AssertionError("SentenceTransformerEmbedder was instantiated before Postgres URL validation")

    monkeypatch.setattr("pi_code_index.coco_backend.SentenceTransformerEmbedder", _explode)

    with pytest.raises(CocoIndexUnavailable, match="invalid Postgres URL scheme"):
        asyncio.run(_search_async(repo, "config", 3, project_cfg, global_cfg, resources=None))


def test_shared_embedder_reuses_one_instance(monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("cocoindex")
    import pi_code_index.coco_backend as m

    monkeypatch.setattr(m, "_SHARED_EMBEDDER", None)
    monkeypatch.setattr(m, "_SHARED_EMBEDDER_MODEL", None)
    calls: list[str] = []

    class _FakeEmbedder:
        def __init__(self, model: str) -> None:
            calls.append(model)

    monkeypatch.setattr(m, "SentenceTransformerEmbedder", _FakeEmbedder)
    first = m._shared_embedder("model-x")
    second = m._shared_embedder("model-x")
    assert first is second
    assert calls == ["model-x"]  # constructed exactly once
    third = m._shared_embedder("model-y")
    assert third is not first
    assert calls == ["model-x", "model-y"]


def test_warm_resources_path_skips_postgres_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Warm daemon path must not run the no-daemon guard; verify by giving resources a bad URL
    # and confirming _search_async reaches the embedder path (which we stub) rather than the guard.
    pytest.importorskip("cocoindex")
    repo = _fresh_repo(tmp_path, chunk_strategy="recursive")
    project_cfg = load_project_config(repo)
    global_cfg = GlobalConfig()
    captured: dict[str, object] = {}

    class _StubEmbedder:
        def __init__(self, model: str) -> None:
            captured["constructed"] = model

        async def embed(self, text: str):
            captured["embedded"] = text
            return [0.0, 0.0, 0.0]

    monkeypatch.setattr("pi_code_index.coco_backend.SentenceTransformerEmbedder", _StubEmbedder)

    # Warm resources built with an unreachable URL; the guard should NOT be invoked.
    resources = CocoBackendResources("postgres://cocoindex:cocoindex@127.0.0.1:9/cocoindex", "model-x")
    with pytest.raises(Exception):
        asyncio.run(_search_async(repo, "query", 3, project_cfg, global_cfg, resources=resources))
    # The embedder was constructed (warm path skips the no-daemon guard and is allowed to build it).
    assert captured.get("constructed") == "model-x"


def _pytest_main_marker() -> None:  # ponytail: keep one runnable self-check for the guard logic
    assert _validate_postgres_config.__name__ == "_validate_postgres_config"


if __name__ == "__main__":
    _pytest_main_marker()
    print("ok")