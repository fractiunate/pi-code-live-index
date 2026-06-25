from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION, __version__
from .backend import find_callees as find_callees_index, find_callers as find_callers_index, find_similar_code as find_similar_code_index, find_tests as find_tests_index, impact_analysis as impact_analysis_index, postgres_summary, refresh as refresh_index, repo_map as repo_map_index, review_context as review_context_index, search as search_index, status as backend_status, symbol_context as symbol_context_index, symbol_definition as symbol_definition_index, symbol_search as symbol_search_index
from .config import POSTGRES_EXPORT_COMMAND, POSTGRES_LIFECYCLE_COMMAND, POSTGRES_VALIDATION_COMMAND, global_config_path, index_path, load_global_config, postgres_url_config, project_config_path, write_default_configs
from .setup_checks import ensure_runtime_postgres_started, run_setup_checks
from .daemon import global_config_mtime, pid_path, send_request, serve, socket_path
from .indexer import repo_root


def _bounded_int(name: str, lo: int, hi: int):
    def convert(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} must be an integer")
        if value < lo or value > hi:
            raise argparse.ArgumentTypeError(f"{name} must be between {lo} and {hi}")
        return value
    return convert


def _print_status_summary(payload: dict[str, Any]) -> None:
    backend = payload.get("backend") if isinstance(payload.get("backend"), dict) else payload
    postgres = payload.get("postgres") if isinstance(payload.get("postgres"), dict) else {}
    effective = backend.get("backend") or backend.get("effective_backend")
    requested = backend.get("requested_backend") or effective
    print(f"Backend: {effective} (requested: {requested})")
    if postgres.get("configured"):
        source = "PI_CODE_INDEX_POSTGRES_URL" if postgres.get("configured_url_source") == "pi_code_index" else postgres.get("configured_url_source")
        print(f"Postgres: configured via {source}")
        if postgres.get("configured_url_source") == "runtime_default":
            print(f"Postgres auto-start: local Podman runtime ({POSTGRES_LIFECYCLE_COMMAND})")
        print("Full semantic/symbol/graph features: available when index feature gates are enabled")
    else:
        print("Postgres: not configured (required)")
        print("Live semantic/symbol/graph features are unavailable until Postgres is configured")
    print(f"Manual Postgres start/troubleshooting: {POSTGRES_LIFECYCLE_COMMAND}")
    print(f"Validate: {POSTGRES_VALIDATION_COMMAND}")


def _print_doctor_summary(payload: dict[str, Any]) -> None:
    _print_status_summary(payload)
    checks = payload.get("setup", {}).get("checks", []) if isinstance(payload.get("setup"), dict) else []
    for check in checks:
        if check.get("severity") == "error" and not check.get("ok"):
            print(f"Error: {check.get('message')}")
            if check.get("id") == "postgres.url":
                print(f"Default URL: {POSTGRES_EXPORT_COMMAND}")
            break


def _backend_summary_fields(backend_payload: dict[str, Any]) -> dict[str, Any]:
    effective = backend_payload.get("backend") or backend_payload.get("effective_backend")
    return {
        "effective_backend": effective,
        "requested_backend": backend_payload.get("requested_backend") or effective,
        "backend_fallback": bool(backend_payload.get("backend_fallback", False)),
    }


def print_result(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        if payload.get("operation") == "status":
            _print_status_summary(payload)
            return
        if payload.get("operation") == "doctor":
            _print_doctor_summary(payload)
            return
        if payload.get("operation") == "symbol_search":
            for result in payload.get("results", []):
                lang = result.get("language") or result.get("metadata", {}).get("language")
                score = result.get("score")
                score_text = f" score={float(score):.3f}" if score is not None else ""
                print(f"{result['filename']}:{result['start_line']}-{result['end_line']} {result['kind']} {result['qualified_name']} language={lang}{score_text}")
                if result.get("signature"):
                    print(result["signature"])
                print("---")
            if payload.get("warning"):
                print(f"Warning: {payload['warning']}")
        elif payload.get("operation") == "symbol_definition":
            definition = payload.get("definition")
            if definition:
                print(f"{definition['filename']}:{definition['start_line']}-{definition['end_line']} {definition['kind']} {definition['qualified_name']}")
                if definition.get("signature"):
                    print(definition["signature"])
            else:
                if payload.get("warning"):
                    print(f"Warning: {payload['warning']}")
                for result in payload.get("matches", []):
                    print(f"{result['filename']}:{result['start_line']}-{result['end_line']} {result['kind']} {result['qualified_name']}")
        elif payload.get("operation") == "symbol_context":
            print(json.dumps({k: payload.get(k) for k in ["symbol", "parents", "children", "siblings", "module_symbols", "chunks", "warning"]}, indent=2))
        elif payload.get("operation") in {"find_callers", "find_callees"}:
            if payload.get("warning"):
                print(f"Warning: {payload['warning']}")
            for result in payload.get("results", []):
                sym = result.get("symbol", {})
                print(f"{sym.get('filename')}:{sym.get('start_line')}-{sym.get('end_line')} {sym.get('kind')} {sym.get('qualified_name')} distance={result.get('distance')} score={float(result.get('score', 0.0)):.3f} confidence={float(result.get('path_confidence', 0.0)):.3f}")
                for path in result.get("paths", [])[:1]:
                    callsite = path.get("callsite", {})
                    print(f"  callsite: {callsite.get('filename') or callsite.get('path')}:{callsite.get('line')}:{callsite.get('column')}")
        elif payload.get("operation") == "impact_analysis":
            print(json.dumps({k: payload.get(k) for k in ["summary", "affected_files", "affected_tests", "warning"]}, indent=2))
        elif payload.get("operation") == "repo_map":
            if payload.get("warning"):
                print(f"Warning: {payload['warning']}")
            for node in payload.get("nodes", [])[:20]:
                print(f"{node.get('path') or '.'} {node.get('node_kind')} files={node.get('file_count')} symbols={node.get('symbol_count')} tests={node.get('test_count')}")
        elif payload.get("operation") == "find_tests":
            if payload.get("warning"):
                print(f"Warning: {payload['warning']}")
            for result in payload.get("results", []):
                print(f"{result.get('test_file')} score={float(result.get('score', 0.0)):.3f} confidence={float(result.get('confidence', 0.0)):.3f} command={result.get('recommended_command')}")
        elif payload.get("operation") == "find_similar_code":
            if payload.get("warning"):
                print(f"Warning: {payload['warning']}")
            for result in payload.get("results", []):
                print(f"{result.get('filename')}:{result.get('start_line')}-{result.get('end_line')} score={float(result.get('score', 0.0)):.3f} risk={result.get('risk')}")
        elif payload.get("operation") == "review_context":
            print(json.dumps({k: payload.get(k) for k in ["summary", "recommended_commands", "warning"]}, indent=2))
        elif "results" in payload:
            for result in payload["results"]:
                print(f"{result['filename']}:{result['start_line']}-{result['end_line']} score={result['score']:.3f}")
                print(result["code"][:1200])
                print("---")
        else:
            print(json.dumps(payload, indent=2))


def _pid_is_running(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cleanup_stale_runtime_files() -> dict[str, Any]:
    spath = socket_path()
    ppath = pid_path()
    facts: dict[str, Any] = {"socket_removed": False, "pid_removed": False, "reason": None}
    if spath.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(spath))
        except OSError:
            spath.unlink(missing_ok=True)
            facts.update({"socket_removed": True, "reason": "connect_failed"})
        finally:
            probe.close()
    if ppath.exists() and not _pid_is_running(ppath):
        ppath.unlink(missing_ok=True)
        facts.update({"pid_removed": True, "reason": facts.get("reason") or "pid_not_running"})
    return facts


def start_daemon() -> None:
    cleanup_stale_runtime_files()
    cfg = load_global_config()
    log_path = Path(cfg.log_path).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("PI_CODE_INDEX_POSTGRES_URL", postgres_url_config()[1])
    bootstrap = ensure_runtime_postgres_started()
    with log_path.open("ab") as log:
        if not bootstrap.get("ok"):
            log.write(("Postgres auto-start warning: " + json.dumps(bootstrap, ensure_ascii=False) + "\n").encode("utf-8"))
        subprocess.Popen(
            [sys.executable, "-m", "pi_code_index.cli", "daemon"],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )


def handshake_payload() -> dict[str, Any]:
    return {
        "type": "handshake",
        "client_version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "global_config_mtime": global_config_mtime(),
    }


def handshake_matches(hello: dict[str, Any]) -> bool:
    return (
        hello.get("ok") is True
        and "server_version" in hello
        and "protocol_version" in hello
        and "global_config_mtime" in hello
        and hello.get("server_version") == __version__
        and hello.get("protocol_version") == PROTOCOL_VERSION
        and hello.get("global_config_mtime") == global_config_mtime()
    )


def stop_daemon_quietly() -> None:
    try:
        send_request({"type": "stop"}, timeout=2.0)
    except Exception:  # noqa: BLE001 - best-effort restart cleanup
        pass
    for _ in range(20):
        if not socket_path().exists():
            break
        time.sleep(0.05)
    cleanup_stale_runtime_files()


def request_or_start(payload: dict[str, Any]) -> dict[str, Any]:
    needs_start = False
    try:
        hello = send_request(handshake_payload())
        if not handshake_matches(hello):
            stop_daemon_quietly()
            needs_start = True
    except Exception:
        cleanup_stale_runtime_files()
        needs_start = True

    if needs_start:
        start_daemon()
        last_error: Exception | None = None
        for _ in range(40):
            try:
                hello = send_request(handshake_payload())
                if handshake_matches(hello):
                    break
                last_error = RuntimeError(f"daemon handshake mismatch: {hello}")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.1)
        else:
            raise RuntimeError(f"daemon did not start; see {Path(load_global_config().log_path).expanduser()}: {last_error}")
    return send_request(payload, timeout=120.0)


def add_common_repo(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, default=None, help="Repository path. Defaults to nearest git root from cwd.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pi-code-index")
    parser.add_argument("--no-daemon", action="store_true", help="Run operation directly in this process.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Create default global/project configuration.")
    add_common_repo(p_init)

    p_search = sub.add_parser("search", help="Search indexed code.")
    p_search.add_argument("query")
    p_search.add_argument("--json", action="store_true", dest="as_json")
    p_search.add_argument("--top-k", type=_bounded_int("top-k",1,50), default=8)
    p_search.add_argument("--refresh", action="store_true")
    add_common_repo(p_search)

    p_refresh = sub.add_parser("refresh", help="Refresh the index.")
    p_refresh.add_argument("--json", action="store_true", dest="as_json")
    add_common_repo(p_refresh)

    p_status = sub.add_parser("status", help="Show index/daemon status.")
    p_status.add_argument("--json", action="store_true", dest="as_json")
    add_common_repo(p_status)

    p_doctor = sub.add_parser("doctor", help="Run setup and troubleshooting checks.")
    p_doctor.add_argument("--json", action="store_true", dest="as_json")
    add_common_repo(p_doctor)

    p_stop = sub.add_parser("stop", help="Stop daemon.")
    p_stop.add_argument("--json", action="store_true", dest="as_json")

    p_live = sub.add_parser("live", help="Manage daemon-supervised live indexing.")
    live_sub = p_live.add_subparsers(dest="live_command", required=True)
    p_live_start = live_sub.add_parser("start", help="Start live indexing for a repository.")
    p_live_start.add_argument("--json", action="store_true", dest="as_json")
    p_live_start.add_argument("--poll-interval", type=float, default=1.0, help="Polling interval in seconds.")
    add_common_repo(p_live_start)
    p_live_stop = live_sub.add_parser("stop", help="Stop live indexing for a repository.")
    p_live_stop.add_argument("--json", action="store_true", dest="as_json")
    add_common_repo(p_live_stop)
    p_live_status = live_sub.add_parser("status", help="Show live indexing state for a repository.")
    p_live_status.add_argument("--json", action="store_true", dest="as_json")
    p_live_status.add_argument("--all", action="store_true", dest="all_repos", help="Show all daemon live watchers.")
    add_common_repo(p_live_status)

    p_graph = sub.add_parser("graph", help="Navigate call graph and impact analysis.")
    graph_sub = p_graph.add_subparsers(dest="graph_command", required=True)
    for name in ("callers", "callees"):
        p_graph_nav = graph_sub.add_parser(name, help=f"Find {name} for a symbol.")
        p_graph_nav.add_argument("target")
        p_graph_nav.add_argument("--json", action="store_true", dest="as_json")
        p_graph_nav.add_argument("--top-k", type=_bounded_int("top-k",1,100), default=20)
        p_graph_nav.add_argument("--depth", type=_bounded_int("depth",1,5), default=1)
        p_graph_nav.add_argument("--include-indirect", action="store_true")
        p_graph_nav.add_argument("--refresh", action="store_true")
        add_common_repo(p_graph_nav)
    p_graph_impact = graph_sub.add_parser("impact", help="Analyze call graph blast radius for a symbol.")
    p_graph_impact.add_argument("target")
    p_graph_impact.add_argument("--json", action="store_true", dest="as_json")
    p_graph_impact.add_argument("--top-k", type=_bounded_int("top-k",1,200), default=50)
    p_graph_impact.add_argument("--depth", type=_bounded_int("depth",1,5), default=2)
    p_graph_impact.add_argument("--include-tests", action=argparse.BooleanOptionalAction, default=True)
    p_graph_impact.add_argument("--include-files", action=argparse.BooleanOptionalAction, default=True)
    p_graph_impact.add_argument("--refresh", action="store_true")
    add_common_repo(p_graph_impact)

    p_context = sub.add_parser("context", help="Repository understanding and quality context.")
    context_sub = p_context.add_subparsers(dest="context_command", required=True)
    p_repo_map = context_sub.add_parser("repo-map", help="Show a compact repository map.")
    p_repo_map.add_argument("--json", action="store_true", dest="as_json")
    p_repo_map.add_argument("--target")
    p_repo_map.add_argument("--depth", type=_bounded_int("depth",0,5), default=2)
    p_repo_map.add_argument("--include-symbols", action=argparse.BooleanOptionalAction, default=True)
    p_repo_map.add_argument("--include-tests", action=argparse.BooleanOptionalAction, default=False)
    p_repo_map.add_argument("--refresh", action="store_true")
    add_common_repo(p_repo_map)
    p_tests = context_sub.add_parser("tests", help="Find likely tests for files or symbols.")
    p_tests.add_argument("target", nargs="+")
    p_tests.add_argument("--json", action="store_true", dest="as_json")
    p_tests.add_argument("--top-k", type=_bounded_int("top-k",1,100), default=20)
    p_tests.add_argument("--include-indirect", action="store_true")
    p_tests.add_argument("--refresh", action="store_true")
    add_common_repo(p_tests)
    p_similar = context_sub.add_parser("similar", help="Find similar code chunks.")
    p_similar.add_argument("target", nargs="?")
    p_similar.add_argument("--json", action="store_true", dest="as_json")
    p_similar.add_argument("--top-k", type=_bounded_int("top-k",1,100), default=12)
    p_similar.add_argument("--mode", choices=["semantic", "hybrid"], default="hybrid")
    p_similar.add_argument("--scope", choices=["chunks", "symbols", "files"], default="chunks")
    p_similar.add_argument("--exclude-self", action=argparse.BooleanOptionalAction, default=True)
    p_similar.add_argument("--query")
    p_similar.add_argument("--refresh", action="store_true")
    add_common_repo(p_similar)
    p_review = context_sub.add_parser("review", help="Compose review context for changed targets.")
    p_review.add_argument("target", nargs="+")
    p_review.add_argument("--json", action="store_true", dest="as_json")
    p_review.add_argument("--top-k", type=_bounded_int("top-k",1,200), default=30)
    p_review.add_argument("--include-map", action=argparse.BooleanOptionalAction, default=True)
    p_review.add_argument("--include-tests", action=argparse.BooleanOptionalAction, default=True)
    p_review.add_argument("--include-similar", action=argparse.BooleanOptionalAction, default=True)
    p_review.add_argument("--include-impact", action=argparse.BooleanOptionalAction, default=True)
    p_review.add_argument("--refresh", action="store_true")
    add_common_repo(p_review)

    p_symbols = sub.add_parser("symbols", help="Search and navigate indexed symbols.")
    symbols_sub = p_symbols.add_subparsers(dest="symbols_command", required=True)
    p_sym_search = symbols_sub.add_parser("search", help="Search symbols by name or intent.")
    p_sym_search.add_argument("query")
    p_sym_search.add_argument("--json", action="store_true", dest="as_json")
    p_sym_search.add_argument("--top-k", type=_bounded_int("top-k",1,50), default=8)
    p_sym_search.add_argument("--kind")
    p_sym_search.add_argument("--language")
    p_sym_search.add_argument("--refresh", action="store_true")
    add_common_repo(p_sym_search)
    p_sym_definition = symbols_sub.add_parser("definition", help="Resolve a symbol definition.")
    p_sym_definition.add_argument("target")
    p_sym_definition.add_argument("--json", action="store_true", dest="as_json")
    p_sym_definition.add_argument("--refresh", action="store_true")
    add_common_repo(p_sym_definition)
    p_sym_context = symbols_sub.add_parser("context", help="Show parents, children, siblings, module symbols, and chunks for a symbol.")
    p_sym_context.add_argument("target")
    p_sym_context.add_argument("--json", action="store_true", dest="as_json")
    p_sym_context.add_argument("--depth", type=_bounded_int("depth", 0, 5), default=1)
    p_sym_context.add_argument("--refresh", action="store_true")
    add_common_repo(p_sym_context)

    sub.add_parser("daemon", help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    if args.command == "daemon":
        serve()
        return 0

    repo = repo_root(args.repo) if getattr(args, "repo", None) else repo_root()

    if args.command == "init":
        write_default_configs(repo)
        print_result({"ok": True, "repo": str(repo), "global_config": str(global_config_path()), "project_config": str(project_config_path(repo))}, True)
        return 0

    if args.command == "search":
        if args.no_daemon:
            payload = search_index(repo, args.query, args.top_k, args.refresh)
        else:
            payload = request_or_start({"type": "search", "repo": str(repo), "query": args.query, "top_k": args.top_k, "refresh": args.refresh})
        print_result(payload, args.as_json)
        return 0 if payload.get("ok", True) else 1

    if args.command == "refresh":
        payload = refresh_index(repo) if args.no_daemon else request_or_start({"type": "refresh", "repo": str(repo)})
        print_result(payload, args.as_json)
        return 0 if payload.get("ok", True) else 1

    if args.command == "graph":
        if args.graph_command in {"callers", "callees"}:
            top_k = max(1, min(int(args.top_k), 100))
            depth = max(1, min(int(args.depth), 5))
            if not args.include_indirect:
                depth = 1
            request_type = "find_callers" if args.graph_command == "callers" else "find_callees"
            if args.no_daemon:
                func = find_callers_index if args.graph_command == "callers" else find_callees_index
                payload = func(repo, args.target, depth, top_k, args.include_indirect, args.refresh)
            else:
                payload = request_or_start({"type": request_type, "repo": str(repo), "target": args.target, "depth": depth, "top_k": top_k, "include_indirect": args.include_indirect, "refresh": args.refresh})
        else:
            top_k = max(1, min(int(args.top_k), 200))
            depth = max(1, min(int(args.depth), 5))
            payload = impact_analysis_index(repo, args.target, depth, top_k, args.include_tests, args.include_files, args.refresh) if args.no_daemon else request_or_start({"type": "impact_analysis", "repo": str(repo), "target": args.target, "depth": depth, "top_k": top_k, "include_tests": args.include_tests, "include_files": args.include_files, "refresh": args.refresh})
        print_result(payload, args.as_json)
        return 0 if payload.get("ok", True) else 1

    if args.command == "context":
        if args.context_command == "repo-map":
            depth = max(0, min(int(args.depth), 5))
            payload = repo_map_index(repo, args.target, depth, args.include_symbols, args.include_tests, args.refresh) if args.no_daemon else request_or_start({"type": "repo_map", "repo": str(repo), "target": args.target, "depth": depth, "include_symbols": args.include_symbols, "include_tests": args.include_tests, "refresh": args.refresh})
        elif args.context_command == "tests":
            top_k = max(1, min(int(args.top_k), 100))
            payload = find_tests_index(repo, args.target, top_k, args.include_indirect, args.refresh) if args.no_daemon else request_or_start({"type": "find_tests", "repo": str(repo), "targets": args.target, "top_k": top_k, "include_indirect": args.include_indirect, "refresh": args.refresh})
        elif args.context_command == "similar":
            if not args.target and not args.query:
                parser.error("context similar requires TARGET or --query")
            top_k = max(1, min(int(args.top_k), 100))
            payload = find_similar_code_index(repo, args.target, args.query, top_k, args.mode, args.scope, args.exclude_self, args.refresh) if args.no_daemon else request_or_start({"type": "find_similar_code", "repo": str(repo), "target": args.target, "query": args.query, "top_k": top_k, "mode": args.mode, "scope": args.scope, "exclude_self": args.exclude_self, "refresh": args.refresh})
        else:
            top_k = max(1, min(int(args.top_k), 200))
            payload = review_context_index(repo, args.target, top_k, args.include_map, args.include_tests, args.include_similar, args.include_impact, args.refresh) if args.no_daemon else request_or_start({"type": "review_context", "repo": str(repo), "targets": args.target, "top_k": top_k, "include_map": args.include_map, "include_tests": args.include_tests, "include_similar": args.include_similar, "include_impact": args.include_impact, "refresh": args.refresh})
        print_result(payload, args.as_json)
        return 0 if payload.get("ok", True) else 1

    if args.command == "symbols":
        if args.symbols_command == "search":
            top_k = max(1, min(int(args.top_k), 50))
            filters = {key: value for key, value in {"kind": args.kind, "language": args.language}.items() if value}
            payload = symbol_search_index(repo, args.query, top_k, filters, args.refresh) if args.no_daemon else request_or_start({"type": "symbol_search", "repo": str(repo), "query": args.query, "top_k": top_k, "filters": filters, "refresh": args.refresh})
        elif args.symbols_command == "definition":
            payload = symbol_definition_index(repo, args.target, None, args.refresh) if args.no_daemon else request_or_start({"type": "symbol_definition", "repo": str(repo), "target": args.target, "refresh": args.refresh})
        else:
            depth = max(0, min(int(args.depth), 5))
            payload = symbol_context_index(repo, args.target, depth, None, args.refresh) if args.no_daemon else request_or_start({"type": "symbol_context", "repo": str(repo), "target": args.target, "depth": depth, "refresh": args.refresh})
        print_result(payload, args.as_json)
        return 0 if payload.get("ok", True) else 1

    if args.command == "status":
        cleanup_facts = cleanup_stale_runtime_files()
        backend_payload = backend_status(repo)
        payload: dict[str, Any] = {
            "ok": True,
            "operation": "status",
            "repo": str(repo),
            "index_path": str(index_path(repo)),
            "index_exists": index_path(repo).exists(),
            "backend": backend_payload,
            **_backend_summary_fields(backend_payload),
            "postgres": postgres_summary(),
            "socket_path": str(socket_path()),
            "socket_exists": socket_path().exists(),
            "pid_path": str(pid_path()),
            "pid_exists": pid_path().exists(),
            "client_version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "global_config_mtime": global_config_mtime(),
            "freshness": backend_payload.get("freshness"),
            "counts": backend_payload.get("counts"),
            "capabilities": backend_payload.get("capabilities"),
            "setup": run_setup_checks(repo, cleanup_facts),
            "runtime_cleanup": cleanup_facts,
            "warnings": [backend_payload["warning"]] if backend_payload.get("warning") else [],
        }
        if not args.no_daemon and socket_path().exists():
            try:
                daemon_payload = request_or_start({"type": "status", "repo": str(repo)})
                payload["daemon"] = daemon_payload.get("daemon", daemon_payload)
                payload["live"] = daemon_payload.get("live")
                payload["freshness"] = daemon_payload.get("freshness", payload.get("freshness"))
                payload["counts"] = daemon_payload.get("counts", payload.get("counts"))
                payload["capabilities"] = daemon_payload.get("capabilities", payload.get("capabilities"))
                payload["setup"] = daemon_payload.get("setup", payload.get("setup"))
            except Exception as exc:  # noqa: BLE001
                payload["daemon_error"] = str(exc)
                payload["daemon"] = {"lifecycle_state": "degraded", "socket_path": str(socket_path()), "pid_path": str(pid_path()), "log_path": str(Path(load_global_config().log_path).expanduser()), "error": str(exc)}
        else:
            payload["daemon"] = {"lifecycle_state": "not_running", "socket_path": str(socket_path()), "pid_path": str(pid_path()), "log_path": str(Path(load_global_config().log_path).expanduser())}
            payload["live"] = {"repo": str(repo), "running": False, "last_update": None, "last_refresh": None, "last_error": None, "stale": False, "stale_reason": None}
        print_result(payload, args.as_json)
        return 0

    if args.command == "doctor":
        cleanup_facts = cleanup_stale_runtime_files()
        backend_payload = backend_status(repo)
        setup = run_setup_checks(repo, cleanup_facts)
        payload = {"ok": setup["summary"]["errors"] == 0, "operation": "doctor", "repo": str(repo), "backend": backend_payload, **_backend_summary_fields(backend_payload), "postgres": postgres_summary() | {"reachable_checked": any(check.get("details", {}).get("live_check_performed") for check in setup["checks"] if check["id"] == "postgres.reachable")}, "setup": setup, "runtime_cleanup": cleanup_facts}
        print_result(payload, args.as_json)
        return 0 if payload["setup"]["summary"]["errors"] == 0 else 1

    if args.command == "live":
        if args.no_daemon:
            payload = {"ok": False, "error": "live mode requires the daemon"}
        elif args.live_command == "start":
            payload = request_or_start({"type": "live_start", "repo": str(repo), "poll_interval": args.poll_interval})
        elif args.live_command == "stop":
            payload = request_or_start({"type": "live_stop", "repo": str(repo)})
        else:
            payload = request_or_start({"type": "live_status", "repo": str(repo), "all": args.all_repos})
        print_result(payload, args.as_json)
        return 0 if payload.get("ok", True) else 1

    if args.command == "stop":
        try:
            payload = send_request({"type": "stop"}) if socket_path().exists() else {"ok": True, "stopping": False, "message": "daemon is not running"}
        except Exception as exc:  # noqa: BLE001
            payload = {"ok": False, "error": str(exc)}
        print_result(payload, args.as_json)
        return 0 if payload.get("ok", True) else 1

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
