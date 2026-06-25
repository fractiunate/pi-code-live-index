from __future__ import annotations

from pathlib import Path

from pi_code_index import cli
import pytest

def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def test_refresh_returns_nonzero_when_payload_not_ok(tmp_path: Path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(cli, "refresh_index", lambda repo_arg: {"ok": False, "operation": "refresh", "error": "missing postgres"})

    code = cli.main(["--no-daemon", "refresh", "--json", "--repo", str(repo)])

    assert code == 1
    assert '"ok": false' in capsys.readouterr().out


def test_search_returns_nonzero_when_payload_not_ok(tmp_path: Path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(cli, "search_index", lambda repo_arg, query, top_k, refresh: {"ok": False, "operation": "search", "error": "missing postgres"})

    code = cli.main(["--no-daemon", "search", "--json", "--repo", str(repo), "query"])

    assert code == 1
    assert '"ok": false' in capsys.readouterr().out


def test_invalid_top_k_is_rejected(tmp_path: Path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(cli, "search_index", lambda repo_arg, query, top_k, refresh: {"ok": True, "operation": "search", "results": []})

    with pytest.raises(SystemExit):
        cli.main(["--no-daemon", "search", "--json", "--repo", str(repo), "--top-k", "0", "query"])
    err = capsys.readouterr().err
    assert "between" in err


def test_search_success_exit_code_stays_zero(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(cli, "search_index", lambda repo_arg, query, top_k, refresh: {"ok": True, "operation": "search", "results": []})

    assert cli.main(["--no-daemon", "search", "--json", "--repo", str(repo), "query"]) == 0
