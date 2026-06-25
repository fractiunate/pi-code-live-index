from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import PROTOCOL_VERSION, __version__
from .backend import choose_backend, find_callees, find_callers, find_similar_code, find_tests, impact_analysis, refresh, repo_map, review_context, search, status as backend_status, symbol_context, symbol_definition, symbol_search
from .config import DAEMON_RESTART_REMINDER, POSTGRES_COMPOSE_COMMAND, POSTGRES_LIFECYCLE_COMMAND, POSTGRES_VALIDATION_COMMAND, global_config_path, load_global_config, load_project_config
from .indexer import iter_files, repo_root
from .setup_checks import run_setup_checks


def socket_path() -> Path:
    return Path(load_global_config().socket_path).expanduser()


def pid_path() -> Path:
    return Path(load_global_config().pid_path).expanduser()


def global_config_mtime() -> int | None:
    path = global_config_path()
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def daemon_metadata(config_mtime: int | None = None) -> dict[str, Any]:
    cfg = load_global_config()
    return {
        "server_version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "global_config_mtime": global_config_mtime() if config_mtime is None else config_mtime,
        "schema_version": 1,
        "pipeline_version": cfg.pipeline_version,
        "ranking_profile": "semantic_ast_v1",
        "lifecycle_state": "running",
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _postgres_identity(url: str) -> dict[str, object]:
    parsed = urlsplit(url)
    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "database": parsed.path.lstrip("/") or None,
        "user": parsed.username,
        "credentials_redacted": bool(parsed.password),
    }


def _branch_identity(repo: Path, branch_mode: str) -> str | None:
    if branch_mode != "current":
        return None
    head = repo / ".git" / "HEAD"
    try:
        text = head.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text.startswith("ref:"):
        return text.removeprefix("ref:").strip()
    return text[:12]


class BackendResourceCache:
    """Daemon-owned warm resources keyed by repository and backend configuration."""

    def __init__(self) -> None:
        self._entries: dict[tuple[object, ...], object] = {}
        self._last_used: dict[tuple[object, ...], str] = {}

    def _key(self, repo: Path) -> tuple[object, ...] | None:
        repo = repo.resolve()
        choice = choose_backend(repo)
        if choice.name != "cocoindex":
            return None
        global_cfg = load_global_config()
        project_cfg = load_project_config(repo)
        schema_name = project_cfg.schema_name or global_cfg.schema_name
        table_prefix = project_cfg.table_prefix or global_cfg.table_prefix
        branch_id = _branch_identity(repo, project_cfg.branch_mode)
        return (
            str(repo), choice.name, choice.requested, global_cfg.postgres_url, global_cfg.embedding_model,
            schema_name, table_prefix, project_cfg.table_name, global_cfg.pipeline_version,
            project_cfg.branch_mode, branch_id, project_cfg.enable_symbols, project_cfg.enable_references,
            project_cfg.enable_test_links, project_cfg.chunk_strategy, tuple(project_cfg.ast_languages or ()),
            project_cfg.chunk_size, project_cfg.min_chunk_size, project_cfg.chunk_overlap,
            project_cfg.max_ast_chunk_bytes, project_cfg.max_result_code_bytes, project_cfg.ast_context_lines,
            tuple(project_cfg.symbol_languages or ()), tuple(project_cfg.symbol_kinds or ()), project_cfg.symbol_embedding_model,
            project_cfg.max_graph_depth, project_cfg.max_graph_edges, tuple(project_cfg.reference_languages or ()),
            project_cfg.min_call_edge_confidence, tuple(project_cfg.include), tuple(project_cfg.exclude),
        )

    def get(self, repo: Path) -> object | None:
        key = self._key(repo)
        if key is None:
            return None
        resource = self._entries.get(key)
        if resource is None:
            from .coco_backend import CocoBackendResources

            resource = CocoBackendResources(postgres_url=str(key[3]), embedding_model=str(key[4]))
            self._entries[key] = resource
        self._last_used[key] = _utc_now()
        return resource

    def status(self) -> dict[str, object]:
        entries: list[dict[str, object]] = []
        for key, resource in self._entries.items():
            resource_status = resource.status() if hasattr(resource, "status") else {"state": "unknown"}
            entries.append({
                "repo": key[0], "backend": key[1], "requested_backend": key[2],
                "postgres": _postgres_identity(str(key[3])), "embedding_model": key[4],
                "schema_name": key[5], "table_prefix": key[6], "table_name": key[7],
                "pipeline_version": key[8], "branch_mode": key[9], "branch_id": key[10],
                "chunk_strategy": key[14],
                "features": {"symbols": key[11], "references": key[12], "test_links": key[13]},
                "resources": resource_status, "last_used_at": self._last_used.get(key),
            })
        return {"entries": len(entries), "resources": entries}

    def close(self) -> None:
        for resource in list(self._entries.values()):
            close = getattr(resource, "close", None)
            if callable(close):
                close()
        self._entries.clear()
        self._last_used.clear()


class RequestMetrics:
    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.total = 0
        self.errors = 0
        self.by_type: dict[str, int] = {}
        self.last_ms = 0.0
        self.average_ms = 0.0
        self.max_ms = 0.0
        self.last_error: dict[str, object] | None = None
        self._lock = threading.Lock()

    def record(self, typ: str, duration_ms: float, error: str | None = None) -> None:
        with self._lock:
            self.total += 1
            self.by_type[typ] = self.by_type.get(typ, 0) + 1
            self.last_ms = round(duration_ms, 3)
            self.average_ms = round(((self.average_ms * (self.total - 1)) + duration_ms) / self.total, 3)
            self.max_ms = round(max(self.max_ms, duration_ms), 3)
            if error:
                self.errors += 1
                self.last_error = {"component": typ, "error": error, "at": _utc_now()}

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "uptime_seconds": round(time.monotonic() - self.started_at, 3),
                "requests": {"total": self.total, "by_type": dict(self.by_type), "errors": self.errors},
                "durations_ms": {"last": self.last_ms, "average": self.average_ms, "max": self.max_ms},
                "last_error": self.last_error,
            }


class LiveWatcher:
    """Polling live indexer for one repository, supervised by the daemon."""

    def __init__(self, repo: Path, poll_interval: float | None = None) -> None:
        cfg = load_global_config()
        self.repo = repo.resolve()
        self.poll_interval = max(0.05, poll_interval if poll_interval is not None else cfg.live_poll_interval_seconds)
        self.debounce_seconds = cfg.live_refresh_debounce_seconds
        self.stale_after_seconds = cfg.live_stale_after_seconds
        self.max_errors_before_stale = cfg.live_max_consecutive_errors_before_stale
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._snapshot: dict[str, tuple[int, int]] = {}
        self.last_update: str | None = None
        self.last_refresh: str | None = None
        self.last_scan_started_at: str | None = None
        self.last_scan_finished_at: str | None = None
        self.last_refresh_duration_ms: float | None = None
        self.last_error: str | None = None
        self.stale_reason: str | None = None
        self.refresh_count = 0
        self.debounced_refresh_count = 0
        self.consecutive_errors = 0
        self.pending_changes = False
        self._pending_since: float | None = None
        self._stop_timeout = False

    def _scan(self) -> dict[str, tuple[int, int]]:
        cfg = load_project_config(self.repo)
        snapshot: dict[str, tuple[int, int]] = {}
        for path in iter_files(self.repo, cfg):
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            snapshot[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    def _refresh(self) -> None:
        with self._refresh_lock:
            start = time.monotonic()
            try:
                payload = refresh(self.repo)
                error = None if payload.get("ok", True) else str(payload.get("error") or payload)
            except Exception as exc:  # noqa: BLE001 - daemon boundary reports status
                error = str(exc)
            now = _utc_now()
            duration = round((time.monotonic() - start) * 1000, 3)
            with self._lock:
                self.last_update = now
                self.last_refresh = now
                self.last_refresh_duration_ms = duration
                if error:
                    self.last_error = error
                    self.consecutive_errors += 1
                    self.stale_reason = "refresh_failed"
                else:
                    self.refresh_count += 1
                    self.last_error = None
                    self.consecutive_errors = 0
                    self.pending_changes = False
                    self._pending_since = None
                    self.stale_reason = None

    def check_once(self) -> bool:
        try:
            with self._lock:
                self.last_scan_started_at = _utc_now()
            snapshot = self._scan()
            with self._lock:
                changed = snapshot != self._snapshot
                if changed:
                    self._snapshot = snapshot
                    self.pending_changes = True
                    self._pending_since = self._pending_since or time.monotonic()
                self.last_scan_finished_at = _utc_now()
                self.last_update = self.last_scan_finished_at
            if changed:
                if self._stop.wait(self.debounce_seconds):
                    return changed
                with self._lock:
                    self.debounced_refresh_count += 1
                self._refresh()
            return changed
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.last_update = _utc_now()
                self.last_error = str(exc)
                self.consecutive_errors += 1
                self.stale_reason = "scan_failed"
            return False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            snapshot = self._scan()
            with self._lock:
                self._snapshot = snapshot
                self.last_scan_started_at = _utc_now()
                self.last_scan_finished_at = self.last_scan_started_at
                self.consecutive_errors = 0
                self.last_error = None
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.last_error = str(exc)
                self.consecutive_errors += 1
                self.stale_reason = "scan_failed"
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"pi-code-index-live:{self.repo}", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval):
            self.check_once()

    def stop(self) -> bool:
        self._stop.set()
        timeout = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            timeout = self._thread.is_alive()
        with self._lock:
            self._stop_timeout = timeout
        return not timeout

    def status(self) -> dict[str, object]:
        thread = self._thread
        running = bool(thread and thread.is_alive())
        with self._lock:
            stale = False
            reason = self.stale_reason
            if running is False and thread is not None and not self._stop.is_set():
                stale, reason = True, "watcher_dead"
            elif self.consecutive_errors >= self.max_errors_before_stale:
                stale, reason = True, reason or "refresh_failed"
            elif self.pending_changes and self._pending_since and time.monotonic() - self._pending_since > self.stale_after_seconds:
                stale, reason = True, "pending_too_long"
            elif self.last_error:
                stale, reason = True, reason or "refresh_failed"
            return {
                "repo": str(self.repo), "running": running, "poll_interval": self.poll_interval,
                "watched_files": len(self._snapshot), "last_scan_started_at": self.last_scan_started_at,
                "last_scan_finished_at": self.last_scan_finished_at, "last_update": self.last_update,
                "last_refresh": self.last_refresh, "last_refresh_duration_ms": self.last_refresh_duration_ms,
                "refresh_count": self.refresh_count, "debounced_refresh_count": self.debounced_refresh_count,
                "pending_changes": self.pending_changes, "last_error": self.last_error,
                "consecutive_errors": self.consecutive_errors, "stale": stale, "stale_reason": reason,
                "stop_timeout": self._stop_timeout,
            }


class LiveWatcherRegistry:
    def __init__(self) -> None:
        self._watchers: dict[str, LiveWatcher] = {}
        self._lock = threading.Lock()

    def start(self, repo: Path, poll_interval: float | None = None) -> dict[str, object]:
        key = str(repo.resolve())
        with self._lock:
            watcher = self._watchers.get(key)
            if watcher is None:
                watcher = LiveWatcher(repo, poll_interval)
                self._watchers[key] = watcher
            watcher.start()
            return watcher.status()

    def stop(self, repo: Path) -> dict[str, object]:
        key = str(repo.resolve())
        with self._lock:
            watcher = self._watchers.pop(key, None)
        if watcher is None:
            return {
                "repo": key,
                "running": False,
                "stopped": True,
                "watcher_found": False,
                "last_update": None,
                "last_refresh": None,
                "last_error": None,
                "stale": False,
                "stale_reason": None,
            }
        stopped = watcher.stop()
        payload = watcher.status()
        payload["stopped"] = stopped
        payload["watcher_found"] = True
        return payload

    def status(self, repo: Path | None = None) -> dict[str, object]:
        with self._lock:
            if repo is not None:
                key = str(repo.resolve())
                watcher = self._watchers.get(key)
                if watcher is None:
                    return {"repo": key, "running": False, "last_update": None, "last_refresh": None, "last_error": None, "pending_changes": False, "consecutive_errors": 0, "stale": False, "stale_reason": None}
                return watcher.status()
            return {"watchers": [watcher.status() for watcher in self._watchers.values()]}

    def stop_all(self) -> None:
        with self._lock:
            watchers = list(self._watchers.values())
            self._watchers.clear()
        for watcher in watchers:
            watcher.stop()


_DEFAULT_RESOURCE_CACHE = BackendResourceCache()
_DEFAULT_LIVE_WATCHERS = LiveWatcherRegistry()
_DEFAULT_METRICS = RequestMetrics()
_AUTO_LIVE_TYPES = {"search", "refresh", "symbol_search", "symbol_definition", "symbol_context", "find_callers", "find_callees", "impact_analysis", "repo_map", "find_tests", "find_similar_code", "review_context", "status"}


def send_request(payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path()))
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode("utf-8"))
    finally:
        sock.close()


def _daemon_status(resource_cache: BackendResourceCache, live_watchers: LiveWatcherRegistry, metrics: RequestMetrics, config_mtime: int | None) -> dict[str, Any]:
    cfg = load_global_config()
    return {
        "lifecycle_state": "running",
        "pid": os.getpid(),
        "socket_path": str(socket_path()),
        "pid_path": str(pid_path()),
        "log_path": str(Path(cfg.log_path).expanduser()),
        "daemon_resource_cache": resource_cache.status(),
        "postgres_lifecycle_guidance": {"lifecycle_command": POSTGRES_LIFECYCLE_COMMAND, "compose_command": POSTGRES_COMPOSE_COMMAND, "validation_command": POSTGRES_VALIDATION_COMMAND, "performed_by_daemon": True},
        "restart_reminder": DAEMON_RESTART_REMINDER,
        "performance": metrics.status(),
        **daemon_metadata(config_mtime),
    }


def handle(
    payload: dict[str, Any],
    config_mtime: int | None = None,
    resource_cache: BackendResourceCache | None = None,
    live_watchers: LiveWatcherRegistry | None = None,
    metrics: RequestMetrics | None = None,
) -> dict[str, Any]:
    resource_cache = resource_cache or _DEFAULT_RESOURCE_CACHE
    live_watchers = live_watchers or _DEFAULT_LIVE_WATCHERS
    metrics = metrics or _DEFAULT_METRICS
    typ = str(payload.get("type") or "unknown")
    start = time.monotonic()
    error: str | None = None
    try:
        if typ == "handshake":
            meta = daemon_metadata(config_mtime)
            restart_required = payload.get("protocol_version") != PROTOCOL_VERSION or payload.get("client_version") != __version__ or payload.get("global_config_mtime") != meta["global_config_mtime"]
            return {"ok": not restart_required, "restart_required": restart_required, **meta, "lifecycle_state": "restart_required" if restart_required else "running"}
        if typ in _AUTO_LIVE_TYPES:
            live_watchers.start(repo_root(Path(payload.get("repo") or ".")), load_global_config().live_poll_interval_seconds)
        if typ == "search":
            repo = repo_root(Path(payload.get("repo") or ".")); return search(repo, str(payload.get("query") or ""), int(payload.get("top_k") or 8), bool(payload.get("refresh")), resource_cache.get(repo))
        if typ == "refresh":
            repo = repo_root(Path(payload.get("repo") or ".")); return refresh(repo)
        if typ == "symbol_search":
            repo = repo_root(Path(payload.get("repo") or ".")); return symbol_search(repo, str(payload.get("query") or ""), int(payload.get("top_k") or 8), payload.get("filters") if isinstance(payload.get("filters"), dict) else None, bool(payload.get("refresh")), resource_cache.get(repo))
        if typ == "symbol_definition":
            repo = repo_root(Path(payload.get("repo") or ".")); return symbol_definition(repo, payload.get("target") or "", payload.get("filters") if isinstance(payload.get("filters"), dict) else None, bool(payload.get("refresh")), resource_cache.get(repo))
        if typ == "symbol_context":
            repo = repo_root(Path(payload.get("repo") or ".")); depth = max(0, min(int(payload.get("depth") or 1), 5)); return symbol_context(repo, payload.get("target") or "", depth, payload.get("filters") if isinstance(payload.get("filters"), dict) else None, bool(payload.get("refresh")), resource_cache.get(repo))
        if typ == "find_callers":
            repo = repo_root(Path(payload.get("repo") or ".")); depth = max(1, min(int(payload.get("depth") or 1), 5)); top_k = max(1, min(int(payload.get("top_k") or 20), 100)); return find_callers(repo, payload.get("target") or "", depth, top_k, bool(payload.get("include_indirect")), bool(payload.get("refresh")), resource_cache.get(repo))
        if typ == "find_callees":
            repo = repo_root(Path(payload.get("repo") or ".")); depth = max(1, min(int(payload.get("depth") or 1), 5)); top_k = max(1, min(int(payload.get("top_k") or 20), 100)); return find_callees(repo, payload.get("target") or "", depth, top_k, bool(payload.get("include_indirect")), bool(payload.get("refresh")), resource_cache.get(repo))
        if typ == "impact_analysis":
            repo = repo_root(Path(payload.get("repo") or ".")); depth = max(1, min(int(payload.get("depth") or 2), 5)); top_k = max(1, min(int(payload.get("top_k") or 50), 200)); return impact_analysis(repo, payload.get("target") or "", depth, top_k, bool(payload.get("include_tests", True)), bool(payload.get("include_files", True)), bool(payload.get("refresh")), resource_cache.get(repo))
        if typ == "repo_map":
            repo = repo_root(Path(payload.get("repo") or ".")); depth = max(0, min(int(payload.get("depth") or 2), 5)); return repo_map(repo, payload.get("target"), depth, bool(payload.get("include_symbols", True)), bool(payload.get("include_tests", False)), bool(payload.get("refresh")), resource_cache.get(repo))
        if typ == "find_tests":
            repo = repo_root(Path(payload.get("repo") or ".")); top_k = max(1, min(int(payload.get("top_k") or 20), 100)); targets = payload.get("targets") if isinstance(payload.get("targets"), list) else [payload.get("target") or ""]; return find_tests(repo, targets, top_k, bool(payload.get("include_indirect")), bool(payload.get("refresh")), resource_cache.get(repo))
        if typ == "find_similar_code":
            repo = repo_root(Path(payload.get("repo") or ".")); top_k = max(1, min(int(payload.get("top_k") or 12), 100)); mode = str(payload.get("mode") or "hybrid"); scope = str(payload.get("scope") or "chunks"); return find_similar_code(repo, payload.get("target"), payload.get("query"), top_k, mode, scope, bool(payload.get("exclude_self", True)), bool(payload.get("refresh")), resource_cache.get(repo))
        if typ == "review_context":
            repo = repo_root(Path(payload.get("repo") or ".")); top_k = max(1, min(int(payload.get("top_k") or 30), 200)); targets = payload.get("targets") if isinstance(payload.get("targets"), list) else []; return review_context(repo, targets, top_k, bool(payload.get("include_map", True)), bool(payload.get("include_tests", True)), bool(payload.get("include_similar", True)), bool(payload.get("include_impact", True)), bool(payload.get("refresh")), resource_cache.get(repo))
        if typ == "live_start":
            repo = repo_root(Path(payload.get("repo") or ".")); interval = float(payload.get("poll_interval") or load_global_config().live_poll_interval_seconds); return {"ok": True, "live": live_watchers.start(repo, interval)}
        if typ == "live_stop":
            repo = repo_root(Path(payload.get("repo") or ".")); return {"ok": True, "live": live_watchers.stop(repo)}
        if typ == "live_status":
            if payload.get("all"):
                return {"ok": True, **live_watchers.status(None)}
            repo = repo_root(Path(payload.get("repo") or ".")); return {"ok": True, "live": live_watchers.status(repo)}
        if typ == "status":
            repo = repo_root(Path(payload.get("repo") or ".")); resources = resource_cache.get(repo); out = backend_status(repo, resources); daemon_info = _daemon_status(resource_cache, live_watchers, metrics, config_mtime); out.update({"daemon": daemon_info, "pid": os.getpid(), "daemon_resource_cache": daemon_info["daemon_resource_cache"], "performance": daemon_info["performance"], "live": live_watchers.status(repo), "setup": run_setup_checks(repo)}); return out
        if typ == "doctor":
            repo = repo_root(Path(payload.get("repo") or ".")); return {"ok": True, "repo": str(repo), "setup": run_setup_checks(repo)}
        if typ == "stop":
            live_watchers.stop_all(); resource_cache.close(); return {"ok": True, "stopping": True, "daemon": {"lifecycle_state": "stopping"}}
        return {"ok": False, "error": f"unknown request type: {typ}"}
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        raise
    finally:
        metrics.record(typ, (time.monotonic() - start) * 1000, error)


def serve() -> None:
    spath = socket_path(); ppath = pid_path(); spath.parent.mkdir(parents=True, exist_ok=True); ppath.parent.mkdir(parents=True, exist_ok=True)
    if spath.exists():
        spath.unlink()
    started_config_mtime = global_config_mtime(); ppath.write_text(str(os.getpid()), encoding="utf-8")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); server.bind(str(spath)); server.listen(128)
    stopping = False; resource_cache = BackendResourceCache(); live_watchers = LiveWatcherRegistry(); metrics = RequestMetrics()
    try:
        while not stopping and ppath.exists():
            conn, _ = server.accept()
            with conn:
                raw = b""
                while not raw.endswith(b"\n"):
                    part = conn.recv(65536)
                    if not part:
                        break
                    raw += part
                if not raw:
                    continue
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    response = handle(payload, started_config_mtime, resource_cache, live_watchers, metrics)
                    stopping = payload.get("type") == "stop"
                except Exception as exc:  # noqa: BLE001 - daemon boundary returns JSON errors
                    response = {"ok": False, "error": str(exc)}
                    metrics.record(str(payload.get("type") if "payload" in locals() else "unknown"), 0.0, str(exc))
                try:
                    conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
                except BrokenPipeError:
                    pass
    finally:
        live_watchers.stop_all(); resource_cache.close(); server.close()
        if spath.exists():
            spath.unlink()
        if ppath.exists():
            ppath.unlink()
