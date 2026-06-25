from __future__ import annotations

import importlib.util
import os
import shutil
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .backend import VALID_BACKENDS, choose_backend
from .config import POSTGRES_LIFECYCLE_COMMAND, POSTGRES_VALIDATION_COMMAND, POSTGRES_EXPORT_COMMAND, global_config_path, load_global_config, load_project_config, postgres_url_config, project_config_path
from .indexer import iter_files, repo_root


def _check(check_id: str, ok: bool, severity: str, message: str, details: dict[str, Any] | None = None, suggested_command: str | None = None) -> dict[str, Any]:
    return {"id": check_id, "ok": bool(ok), "severity": severity, "message": message, "details": details or {}, "suggested_command": suggested_command}


def _postgres_url_details(url: str | None) -> tuple[bool, dict[str, Any], str | None]:
    if not url:
        return False, {}, "Postgres URL is not configured"
    parsed = urlsplit(url)
    details: dict[str, Any] = {"scheme": parsed.scheme, "host": parsed.hostname, "port": parsed.port or 5432, "database": parsed.path.lstrip("/") or None, "user": parsed.username}
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not details["database"]:
        return False, details, "Postgres URL must look like postgres://user:pass@host:5432/database"
    return True, details, None


def _postgres_reachable(host: str, port: int, timeout: float = 0.25) -> tuple[bool, str | None]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except OSError as exc:
        return False, str(exc)


def _runtime_stale(socket_path: Path, pid_path: Path) -> tuple[bool, dict[str, Any]]:
    facts: dict[str, Any] = {"socket_exists": socket_path.exists(), "pid_exists": pid_path.exists(), "socket_removed": False, "pid_removed": False}
    stale = False
    if socket_path.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(socket_path))
        except OSError:
            stale = True
            facts["reason"] = "connect_failed"
        finally:
            probe.close()
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
        except (FileNotFoundError, ValueError, ProcessLookupError):
            stale = True
            facts["reason"] = facts.get("reason") or "pid_not_running"
        except PermissionError:
            pass
    return stale, facts


def run_setup_checks(repo: Path | None = None, cleanup_facts: dict[str, Any] | None = None) -> dict[str, Any]:
    repo = repo_root(repo) if repo is not None else repo_root()
    checks: list[dict[str, Any]] = []
    global_cfg = load_global_config()
    project_cfg = load_project_config(repo)
    requested_backend = choose_backend(repo).requested
    coco_required = requested_backend == "cocoindex"
    coco_severity = "error" if coco_required else "warning"

    checks.append(_check("tool.uv", shutil.which("uv") is not None, "error", "uv is installed" if shutil.which("uv") else "uv is not installed", suggested_command="Install uv and rerun scripts/setup.sh"))
    checks.append(_check("tool.node_npm", shutil.which("node") is not None and shutil.which("npm") is not None, "warning", "node and npm are available" if shutil.which("node") and shutil.which("npm") else "node/npm not found", suggested_command="npm install"))
    checks.append(_check("python.import", importlib.util.find_spec("pi_code_index") is not None, "error", "import pi_code_index succeeds"))
    checks.append(_check("cli.help", shutil.which("uv") is not None, "error", "CLI can be run through uv" if shutil.which("uv") else "uv is required to run pi-code-index --help", suggested_command="uv run pi-code-index --help"))

    gpath = global_config_path()
    checks.append(_check("config.global", gpath.parent.exists() and os.access(gpath.parent, os.W_OK), "error", "global config parent is writable", {"path": str(gpath)}))
    ppath = project_config_path(repo)
    checks.append(_check("config.project", (ppath.exists() and os.access(ppath, os.R_OK)) or os.access(ppath.parent if ppath.parent.exists() else repo, os.W_OK), "warning", "project settings are readable or creatable", {"path": str(ppath)}))

    socket_path = Path(global_cfg.socket_path).expanduser()
    pid_path = Path(global_cfg.pid_path).expanduser()
    log_path = Path(global_cfg.log_path).expanduser()
    runtime_ok = all(path.parent.exists() or os.access(path.parent.parent if path.parent.parent.exists() else Path.home(), os.W_OK) for path in (socket_path, pid_path, log_path))
    checks.append(_check("runtime.paths", runtime_ok, "error", "runtime path parents are writable", {"socket_path": str(socket_path), "pid_path": str(pid_path), "log_path": str(log_path)}))
    checks.append(_check("repo.root", repo.exists(), "error", "repository root resolved", {"repo": str(repo)}))

    matched = sum(1 for _ in iter_files(repo, project_cfg)) if repo.exists() else 0
    empty_severity = "error" if global_cfg.setup_error_on_empty_globs else "warning"
    checks.append(_check("globs.non_empty", matched > 0, empty_severity, f"include/exclude globs match {matched} files", {"matched_files": matched}))
    checks.append(_check("backend.valid", requested_backend in VALID_BACKENDS, "error", f"backend is {requested_backend}", {"valid": sorted(VALID_BACKENDS)}))
    features_ok = (not project_cfg.enable_references or project_cfg.enable_symbols) and (not project_cfg.enable_test_links or project_cfg.enable_symbols)
    checks.append(_check("features.consistent", features_ok, "warning", "feature gates are consistent" if features_ok else "references/test links should be enabled with symbols"))

    stale, stale_facts = _runtime_stale(socket_path, pid_path)
    if cleanup_facts:
        stale_facts.update(cleanup_facts)
        stale = False if cleanup_facts.get("socket_removed") or cleanup_facts.get("pid_removed") else stale
    checks.append(_check("daemon.runtime_stale", not stale, "warning", "runtime files are healthy" if not stale else "stale runtime files detected", stale_facts, "pi-code-index stop --json"))

    coco_import_ok = importlib.util.find_spec("cocoindex") is not None
    checks.append(_check("cocoindex.optional_deps", coco_import_ok or not coco_required, coco_severity, "CocoIndex optional dependencies are available" if coco_import_ok else "CocoIndex optional dependencies are not installed", suggested_command="scripts/setup.sh --with-cocoindex"))
    url_source, postgres_url = postgres_url_config()
    pg_details = {"configured_url_source": url_source, "preferred_env": "PI_CODE_INDEX_POSTGRES_URL", "compat_env": "POSTGRES_URL", "lifecycle_command": POSTGRES_LIFECYCLE_COMMAND, "validation_command": POSTGRES_VALIDATION_COMMAND}
    if postgres_url:
        url_valid, url_details, url_error = _postgres_url_details(postgres_url)
        if url_valid:
            pg_ok, pg_severity, pg_message, pg_command = True, "info", f"Postgres URL is configured via {url_source}", POSTGRES_VALIDATION_COMMAND
        elif coco_required:
            pg_ok, pg_severity, pg_message, pg_command = False, "error", f"Postgres URL is malformed: {url_error}", POSTGRES_EXPORT_COMMAND
        else:
            pg_ok, pg_severity, pg_message, pg_command = False, "warning", f"Postgres URL is malformed: {url_error}", POSTGRES_EXPORT_COMMAND
        pg_details = {**pg_details, **url_details, "url_valid": url_valid}
    elif requested_backend == "cocoindex":
        pg_ok, pg_severity, pg_message, pg_command = False, "error", "Postgres URL is required for backend=cocoindex.", POSTGRES_EXPORT_COMMAND
        pg_details = {**pg_details, "url_valid": False}
    elif requested_backend == "auto":
        pg_ok, pg_severity, pg_message, pg_command = False, "warning", "Postgres URL is not configured; backend=auto is using lexical degraded mode.", POSTGRES_EXPORT_COMMAND
        pg_details = {**pg_details, "url_valid": False}
    else:
        pg_ok, pg_severity, pg_message, pg_command = True, "info", "Postgres URL is not configured; backend=lexical does not require Postgres.", POSTGRES_LIFECYCLE_COMMAND
        pg_details = {**pg_details, "url_valid": False}
    checks.append(_check("postgres.url", pg_ok, pg_severity, pg_message, pg_details, pg_command))
    reachable_ok = True
    reachable_message = "Postgres reachability was not checked by lightweight doctor/status."
    reachable_details = {**pg_details, "live_check_performed": False}
    if coco_required and postgres_url and pg_ok:
        host, port = url_details.get("host"), url_details.get("port", 5432)
        if host:
            reachable_ok, reachable_error = _postgres_reachable(str(host), int(port))
            reachable_details.update({"live_check_performed": True, "host": host, "port": port, "reachable": reachable_ok})
            reachable_severity = "warning" if reachable_ok else "error"
            reachable_message = f"Postgres at {host}:{port} is reachable" if reachable_ok else f"Postgres at {host}:{port} is not reachable: {reachable_error}"
        else:
            reachable_ok = False
            reachable_severity = "error"
            reachable_message = "Postgres reachability must be validated for backend=cocoindex."
    elif coco_required:
        reachable_ok = False
        reachable_severity = "error" if not pg_ok else "warning"
        reachable_message = "Postgres reachability must be validated for backend=cocoindex."
    else:
        reachable_severity = "warning"
    checks.append(_check("postgres.reachable", reachable_ok, reachable_severity, reachable_message, reachable_details, POSTGRES_VALIDATION_COMMAND))
    for check_id, message in [
        ("postgres.pgvector", "pgvector was not checked by lightweight doctor/status."),
        ("postgres.permissions", "Postgres permissions were not checked by lightweight doctor/status."),
        ("postgres.canonical_tables", "CocoIndex canonical table presence is checked after refresh."),
    ]:
        details = {**pg_details, "live_check_performed": False}
        checks.append(_check(check_id, not coco_required, "warning", message, details, POSTGRES_VALIDATION_COMMAND if check_id != "postgres.canonical_tables" else "pi-code-index refresh --json"))
    checks.append(_check("cocoindex.version", coco_import_ok or not coco_required, coco_severity, "CocoIndex version is available" if coco_import_ok else "CocoIndex version could not be checked", suggested_command="scripts/setup.sh --with-cocoindex"))

    summary = {
        "errors": sum(1 for check in checks if not check["ok"] and check["severity"] == "error"),
        "warnings": sum(1 for check in checks if not check["ok"] and check["severity"] == "warning"),
    }
    return {"checks": checks, "summary": summary}
