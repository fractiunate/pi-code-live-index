from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

from pi_code_index import PROTOCOL_VERSION, __version__
from pi_code_index import cli
from pi_code_index.daemon import BackendResourceCache, LiveWatcherRegistry, handle, send_request, serve


def test_cleanup_stale_runtime_files_removes_dead_pid_and_socket(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    spath = cli.socket_path()
    ppath = cli.pid_path()
    spath.parent.mkdir(parents=True, exist_ok=True)
    ppath.parent.mkdir(parents=True, exist_ok=True)

    stale_server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale_server.bind(str(spath))
    stale_server.close()
    ppath.write_text("999999999", encoding="utf-8")

    cli.cleanup_stale_runtime_files()

    assert not spath.exists()
    assert not ppath.exists()


def test_request_or_start_restarts_on_protocol_version_mismatch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    events: list[str] = []
    handshakes = iter([
        {"ok": False, "server_version": __version__, "protocol_version": PROTOCOL_VERSION - 1, "global_config_mtime": None},
        {"ok": True, "server_version": __version__, "protocol_version": PROTOCOL_VERSION, "global_config_mtime": None},
    ])

    def fake_send_request(payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        if payload["type"] == "handshake":
            events.append("handshake")
            return next(handshakes)
        if payload["type"] == "stop":
            events.append("stop")
            return {"ok": True, "stopping": True}
        events.append(payload["type"])
        return {"ok": True, "type": payload["type"]}

    monkeypatch.setattr(cli, "send_request", fake_send_request)
    monkeypatch.setattr(cli, "start_daemon", lambda: events.append("start"))

    result = cli.request_or_start({"type": "status", "repo": str(tmp_path)})

    assert result == {"ok": True, "type": "status"}
    assert events == ["handshake", "stop", "start", "handshake", "status"]


def test_request_or_start_restarts_on_global_config_mtime_mismatch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg = cli.global_config_path()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("backend: cocoindex\n", encoding="utf-8")
    current_mtime = cli.global_config_mtime()
    events: list[str] = []
    handshakes = iter([
        {"ok": False, "server_version": __version__, "protocol_version": PROTOCOL_VERSION, "global_config_mtime": (current_mtime or 0) - 1},
        {"ok": True, "server_version": __version__, "protocol_version": PROTOCOL_VERSION, "global_config_mtime": current_mtime},
    ])

    def fake_send_request(payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        if payload["type"] == "handshake":
            events.append("handshake")
            return next(handshakes)
        if payload["type"] == "stop":
            events.append("stop")
            return {"ok": True, "stopping": True}
        events.append(payload["type"])
        return {"ok": True}

    monkeypatch.setattr(cli, "send_request", fake_send_request)
    monkeypatch.setattr(cli, "start_daemon", lambda: events.append("start"))

    assert cli.request_or_start({"type": "refresh", "repo": str(tmp_path)}) == {"ok": True}
    assert events == ["handshake", "stop", "start", "handshake", "refresh"]


def test_serve_ignores_empty_socket_probe(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    monkeypatch.setattr("pi_code_index.daemon.backend_status", lambda repo, coco_resources=None: {"ok": True, "backend": "cocoindex", "counts": {}})

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    spath = cli.socket_path()
    deadline = time.time() + 2.0
    while not spath.exists() and time.time() < deadline:
        time.sleep(0.01)
    assert spath.exists()

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.connect(str(spath))
    probe.close()

    status = send_request({"type": "status", "repo": str(repo)})
    send_request({"type": "stop"})
    thread.join(timeout=2.0)

    assert status["daemon"]["lifecycle_state"] == "running"
    assert not thread.is_alive()


def test_daemon_status_reports_version_protocol_and_config_mtime(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    monkeypatch.setattr("pi_code_index.daemon.backend_status", lambda repo, coco_resources=None: {"ok": True, "backend": "cocoindex", "counts": {}})

    payload = handle({"type": "status", "repo": str(repo)}, config_mtime=12345, resource_cache=BackendResourceCache())

    assert payload["daemon"]["lifecycle_state"] == "running"
    assert payload["daemon"]["server_version"] == __version__
    assert payload["daemon"]["protocol_version"] == PROTOCOL_VERSION
    assert payload["daemon"]["global_config_mtime"] == 12345
    assert payload["daemon"]["socket_path"]
    assert payload["daemon_resource_cache"]["entries"] >= 1
    assert payload["daemon_resource_cache"]["resources"][0]["backend"] == "cocoindex"


def test_daemon_reuses_coco_resources_for_repeated_searches(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    monkeypatch.setenv("POSTGRES_URL", "postgres://example/test")
    seen_resources: list[object] = []

    def fake_search(repo: Path, query: str, top_k: int, refresh: bool, coco_resources: object | None = None) -> dict[str, object]:
        seen_resources.append(coco_resources)
        return {"ok": True, "backend": "cocoindex", "results": []}

    monkeypatch.setattr("pi_code_index.daemon.search", fake_search)
    cache = BackendResourceCache()

    first = handle({"type": "search", "repo": str(repo), "query": "one"}, resource_cache=cache)
    second = handle({"type": "search", "repo": str(repo), "query": "two"}, resource_cache=cache)

    assert first["ok"] is True
    assert second["ok"] is True
    assert seen_resources[0] is not None
    assert seen_resources[0] is seen_resources[1]
    assert cache.status()["entries"] == 1


def test_daemon_routes_symbol_operations_with_cached_resources(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    monkeypatch.setenv("POSTGRES_URL", "postgres://example/test")
    seen: list[tuple[str, object | None]] = []

    def fake_symbol_search(repo: Path, query: str, top_k: int, filters: dict[str, object] | None, refresh: bool, coco_resources: object | None = None) -> dict[str, object]:
        seen.append(("search", coco_resources))
        return {"ok": True, "operation": "symbol_search", "results": []}

    def fake_symbol_definition(repo: Path, target: object, filters: dict[str, object] | None, refresh: bool, coco_resources: object | None = None) -> dict[str, object]:
        seen.append(("definition", coco_resources))
        return {"ok": True, "operation": "symbol_definition", "definition": None}

    def fake_symbol_context(repo: Path, target: object, depth: int, filters: dict[str, object] | None, refresh: bool, coco_resources: object | None = None) -> dict[str, object]:
        seen.append(("context", coco_resources))
        return {"ok": True, "operation": "symbol_context", "depth": depth}

    monkeypatch.setattr("pi_code_index.daemon.symbol_search", fake_symbol_search)
    monkeypatch.setattr("pi_code_index.daemon.symbol_definition", fake_symbol_definition)
    monkeypatch.setattr("pi_code_index.daemon.symbol_context", fake_symbol_context)
    cache = BackendResourceCache()

    assert handle({"type": "symbol_search", "repo": str(repo), "query": "load"}, resource_cache=cache)["operation"] == "symbol_search"
    assert handle({"type": "symbol_definition", "repo": str(repo), "target": "load"}, resource_cache=cache)["operation"] == "symbol_definition"
    assert handle({"type": "symbol_context", "repo": str(repo), "target": "load", "depth": 9}, resource_cache=cache)["depth"] == 5
    assert seen[0][1] is not None
    assert seen[0][1] is seen[1][1] is seen[2][1]


def test_daemon_routes_graph_operations_with_cached_resources(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    monkeypatch.setenv("POSTGRES_URL", "postgres://example/test")
    seen: list[tuple[str, object | None, int, int]] = []

    def fake_find_callers(repo: Path, target: object, depth: int, top_k: int, include_indirect: bool, refresh: bool, coco_resources: object | None = None) -> dict[str, object]:
        seen.append(("callers", coco_resources, depth, top_k))
        return {"ok": True, "operation": "find_callers", "depth": depth, "top_k": top_k}

    def fake_find_callees(repo: Path, target: object, depth: int, top_k: int, include_indirect: bool, refresh: bool, coco_resources: object | None = None) -> dict[str, object]:
        seen.append(("callees", coco_resources, depth, top_k))
        return {"ok": True, "operation": "find_callees", "depth": depth, "top_k": top_k}

    def fake_impact(repo: Path, target: object, depth: int, top_k: int, include_tests: bool, include_files: bool, refresh: bool, coco_resources: object | None = None) -> dict[str, object]:
        seen.append(("impact", coco_resources, depth, top_k))
        return {"ok": True, "operation": "impact_analysis", "depth": depth, "top_k": top_k, "include_tests": include_tests, "include_files": include_files}

    monkeypatch.setattr("pi_code_index.daemon.find_callers", fake_find_callers)
    monkeypatch.setattr("pi_code_index.daemon.find_callees", fake_find_callees)
    monkeypatch.setattr("pi_code_index.daemon.impact_analysis", fake_impact)
    cache = BackendResourceCache()

    assert handle({"type": "find_callers", "repo": str(repo), "target": "x", "depth": 9, "top_k": 999}, resource_cache=cache)["depth"] == 5
    assert handle({"type": "find_callees", "repo": str(repo), "target": "x"}, resource_cache=cache)["operation"] == "find_callees"
    impact = handle({"type": "impact_analysis", "repo": str(repo), "target": "x", "include_tests": False}, resource_cache=cache)

    assert impact["include_tests"] is False
    assert seen[0][1] is not None
    assert seen[0][1] is seen[1][1] is seen[2][1]


def test_daemon_status_exposes_coco_resource_cache_state(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    monkeypatch.setenv("PI_CODE_INDEX_POSTGRES_URL", "postgres://user:secret@example/test")

    def fake_backend_status(repo: Path, coco_resources: object | None = None) -> dict[str, object]:
        resources = coco_resources.status() if coco_resources is not None else {}
        return {"ok": True, "backend": "cocoindex", "resource_state_seen_by_backend": resources}

    monkeypatch.setattr("pi_code_index.daemon.backend_status", fake_backend_status)
    cache = BackendResourceCache()

    payload = handle({"type": "status", "repo": str(repo)}, config_mtime=12345, resource_cache=cache)

    assert payload["ok"] is True
    assert payload["daemon_resource_cache"]["entries"] == 1
    entry = payload["daemon_resource_cache"]["resources"][0]
    assert entry["repo"] == str(repo.resolve())
    assert entry["backend"] == "cocoindex"
    assert entry["table_name"] == "code_embeddings"
    assert entry["resources"]["postgres_pool"] == "cold"
    assert entry["postgres"]["credentials_redacted"] is True
    assert "postgres://" not in str(entry["postgres"])
    assert payload["daemon"]["postgres_lifecycle_guidance"]["performed_by_daemon"] is False
    assert "pi-code-index stop --json" in payload["daemon"]["restart_reminder"]
    assert payload["resource_state_seen_by_backend"]["embedder"] == "cold"


def test_daemon_resource_cache_close_calls_resource_hooks(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    monkeypatch.setenv("POSTGRES_URL", "postgres://example/test")
    cache = BackendResourceCache()
    resource = cache.get(repo)
    closed: list[bool] = []

    assert resource is not None
    resource.close = lambda: closed.append(True)  # type: ignore[attr-defined]

    cache.close()

    assert closed == [True]
    assert cache.status() == {"entries": 0, "resources": []}


def test_live_start_status_stop_reports_repo_state(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "example.py").write_text("print('hello')\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    registry = LiveWatcherRegistry()

    started = handle({"type": "live_start", "repo": str(repo), "poll_interval": 0.05}, live_watchers=registry)
    status = handle({"type": "live_status", "repo": str(repo)}, live_watchers=registry)
    stopped = handle({"type": "live_stop", "repo": str(repo)}, live_watchers=registry)

    assert started["ok"] is True
    assert started["live"]["repo"] == str(repo.resolve())
    assert started["live"]["running"] is True
    assert status["live"]["running"] is True
    assert stopped["live"]["running"] is False
    assert stopped["live"]["stopped"] is True
    assert stopped["live"]["watcher_found"] is True


def test_live_stop_is_idempotent_after_watcher_removed(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    registry = LiveWatcherRegistry()

    first = handle({"type": "live_stop", "repo": str(repo)}, live_watchers=registry)
    handle({"type": "live_start", "repo": str(repo), "poll_interval": 0.05}, live_watchers=registry)
    second = handle({"type": "live_stop", "repo": str(repo)}, live_watchers=registry)
    third = handle({"type": "live_stop", "repo": str(repo)}, live_watchers=registry)

    assert first["live"]["running"] is False
    assert first["live"]["stopped"] is True
    assert first["live"]["watcher_found"] is False
    assert second["live"]["stopped"] is True
    assert second["live"]["watcher_found"] is True
    assert third["live"]["running"] is False
    assert third["live"]["stopped"] is True
    assert third["live"]["watcher_found"] is False


def test_daemon_status_includes_live_state(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("pi_code_index.daemon.backend_status", lambda repo, coco_resources=None: {"ok": True, "backend": "cocoindex", "counts": {}})
    registry = LiveWatcherRegistry()

    payload = handle({"type": "status", "repo": str(repo)}, live_watchers=registry)

    assert payload["ok"] is True
    assert payload["live"]["repo"] == str(repo.resolve())
    assert payload["live"]["running"] is False
    assert payload["live"]["stale"] is False


def test_live_watcher_refreshes_when_indexed_file_changes(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    source = repo / "example.py"
    source.write_text("print('hello')\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    calls: list[Path] = []

    def fake_refresh(path: Path) -> dict[str, object]:
        calls.append(path)
        return {"ok": True, "repo": str(path.resolve())}

    monkeypatch.setattr("pi_code_index.daemon.refresh", fake_refresh)
    registry = LiveWatcherRegistry()
    handle({"type": "live_start", "repo": str(repo), "poll_interval": 0.05}, live_watchers=registry)

    source.write_text("print('changed_live_token')\n", encoding="utf-8")
    deadline = time.time() + 2.0
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    stopped = handle({"type": "live_stop", "repo": str(repo)}, live_watchers=registry)

    assert calls == [repo.resolve()]
    assert stopped["live"]["last_refresh"] is not None
    assert stopped["live"]["last_error"] is None


def test_daemon_request_metrics_count_successes_and_errors(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("pi_code_index.daemon.backend_status", lambda repo, coco_resources=None: {"ok": True, "backend": "cocoindex", "counts": {}})

    handle({"type": "status", "repo": str(repo)})
    payload = handle({"type": "status", "repo": str(repo)})

    perf = payload["daemon"]["performance"]
    assert perf["requests"]["total"] >= 2
    assert perf["requests"]["by_type"]["status"] >= 2
    assert perf["durations_ms"]["max"] >= 0


def test_live_watcher_reports_refresh_errors_and_stale(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    source = repo / "example.py"
    source.write_text("print('hello')\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_LIVE_MAX_CONSECUTIVE_ERRORS_BEFORE_STALE", "1")

    def failing_refresh(path: Path) -> dict[str, object]:
        return {"ok": False, "error": "boom"}

    monkeypatch.setattr("pi_code_index.daemon.refresh", failing_refresh)
    registry = LiveWatcherRegistry()
    handle({"type": "live_start", "repo": str(repo), "poll_interval": 0.05}, live_watchers=registry)
    source.write_text("print('changed')\n", encoding="utf-8")
    deadline = time.time() + 2.0
    status: dict[str, Any] = {"live": {}}
    while time.time() < deadline:
        status = handle({"type": "live_status", "repo": str(repo)}, live_watchers=registry)
        if status["live"].get("last_error"):
            break
        time.sleep(0.05)
    handle({"type": "live_stop", "repo": str(repo)}, live_watchers=registry)

    assert status["live"]["consecutive_errors"] >= 1
    assert status["live"]["stale"] is True
    assert status["live"]["stale_reason"] == "refresh_failed"


def test_live_watcher_refreshes_cocoindex_after_file_edit(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    source = repo / "example.py"
    source.write_text("print('hello')\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    calls: list[Path] = []
    monkeypatch.setattr("pi_code_index.daemon.refresh", lambda path: calls.append(path) or {"ok": True, "backend": "cocoindex"})
    registry = LiveWatcherRegistry()

    handle({"type": "live_start", "repo": str(repo), "poll_interval": 0.05}, live_watchers=registry)
    source.write_text("print('changed_live_token')\n", encoding="utf-8")
    deadline = time.time() + 2.0
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    handle({"type": "live_stop", "repo": str(repo)}, live_watchers=registry)

    assert calls == [repo.resolve()]
