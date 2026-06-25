from __future__ import annotations

from pathlib import Path
from typing import Any

from pi_code_index import cli


def test_cli_symbols_search_no_daemon_calls_backend(tmp_path: Path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    seen: dict[str, Any] = {}

    def fake_symbol_search(repo_arg: Path, query: str, top_k: int, filters: dict[str, object], refresh: bool) -> dict[str, object]:
        seen.update(repo=repo_arg, query=query, top_k=top_k, filters=filters, refresh=refresh)
        return {"ok": True, "operation": "symbol_search", "results": []}

    monkeypatch.setattr(cli, "symbol_search_index", fake_symbol_search)

    assert cli.main(["--no-daemon", "symbols", "search", "--json", "--top-k", "3", "--kind", "function", "--language", "python", "--repo", str(repo), "config loader"]) == 0

    assert seen == {"repo": repo.resolve(), "query": "config loader", "top_k": 3, "filters": {"kind": "function", "language": "python"}, "refresh": False}
    assert '"operation": "symbol_search"' in capsys.readouterr().out


def test_cli_graph_callers_no_daemon_calls_backend_with_clamps(tmp_path: Path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    seen: dict[str, Any] = {}

    def fake_find_callers(repo_arg: Path, target: object, depth: int, top_k: int, include_indirect: bool, refresh: bool) -> dict[str, object]:
        seen.update(repo=repo_arg, target=target, depth=depth, top_k=top_k, include_indirect=include_indirect, refresh=refresh)
        return {"ok": True, "operation": "find_callers", "results": []}

    monkeypatch.setattr(cli, "find_callers_index", fake_find_callers)

    assert cli.main(["--no-daemon", "graph", "callers", "--json", "--top-k", "100", "--depth", "4", "--repo", str(repo), "pkg.target"]) == 0

    assert seen == {"repo": repo.resolve(), "target": "pkg.target", "depth": 1, "top_k": 100, "include_indirect": False, "refresh": False}
    assert '"operation": "find_callers"' in capsys.readouterr().out


def test_cli_graph_impact_uses_daemon_request(tmp_path: Path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    requests: list[dict[str, object]] = []

    def fake_request(payload: dict[str, object]) -> dict[str, object]:
        requests.append(payload)
        return {"ok": True, "operation": payload["type"], "summary": {}}

    monkeypatch.setattr(cli, "request_or_start", fake_request)

    assert cli.main(["graph", "impact", "--json", "--top-k", "200", "--depth", "5", "--no-include-tests", "--repo", str(repo), "pkg.target"]) == 0

    assert requests == [{"type": "impact_analysis", "repo": str(repo.resolve()), "target": "pkg.target", "depth": 5, "top_k": 200, "include_tests": False, "include_files": True, "refresh": False}]
    assert '"operation": "impact_analysis"' in capsys.readouterr().out


def test_cli_symbols_definition_and_context_use_daemon_requests(tmp_path: Path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    requests: list[dict[str, object]] = []

    def fake_request(payload: dict[str, object]) -> dict[str, object]:
        requests.append(payload)
        return {"ok": True, "operation": payload["type"]}

    monkeypatch.setattr(cli, "request_or_start", fake_request)

    assert cli.main(["symbols", "definition", "--json", "--repo", str(repo), "config.load"]) == 0
    assert cli.main(["symbols", "context", "--json", "--depth", "2", "--repo", str(repo), "config.load"]) == 0

    assert requests[0]["type"] == "symbol_definition"
    assert requests[0]["target"] == "config.load"
    assert requests[1]["type"] == "symbol_context"
    assert requests[1]["depth"] == 2
    assert '"operation": "symbol_definition"' in capsys.readouterr().out
