from __future__ import annotations

import json
from pathlib import Path

from pi_code_index import cli, setup_checks
from pi_code_index.setup_checks import run_setup_checks


REQUIRED_CHECK_IDS = {
    "tool.uv",
    "tool.node_npm",
    "python.import",
    "cli.help",
    "config.global",
    "config.project",
    "runtime.paths",
    "repo.root",
    "globs.non_empty",
    "backend.valid",
    "features.consistent",
    "daemon.runtime_stale",
    "cocoindex.optional_deps",
    "postgres.url",
    "postgres.reachable",
    "postgres.pgvector",
    "postgres.permissions",
    "postgres.canonical_tables",
    "cocoindex.version",
}


def test_setup_checks_include_required_ids_and_summary(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "example.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")

    payload = run_setup_checks(repo)

    ids = {check["id"] for check in payload["checks"]}
    assert REQUIRED_CHECK_IDS <= ids
    assert payload["summary"]["errors"] >= 0
    assert all(check["severity"] in {"info", "warning", "error"} for check in payload["checks"])


def test_doctor_command_returns_setup_payload(tmp_path: Path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "example.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")

    code = cli.main(["doctor", "--json", "--repo", str(repo)])
    out = capsys.readouterr().out

    assert "setup" in out
    assert "backend" in out
    assert "postgres" in out
    assert code in {0, 1}


def test_status_json_keeps_backend_object_and_adds_normalized_fields(tmp_path: Path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "example.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "auto")
    monkeypatch.delenv("PI_CODE_INDEX_POSTGRES_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)

    code = cli.main(["--no-daemon", "status", "--json", "--repo", str(repo)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["backend"]["backend"] == "cocoindex"
    assert payload["effective_backend"] == "cocoindex"
    assert payload["requested_backend"] == "auto"
    assert payload["backend_fallback"] is False


def test_status_non_json_prints_required_live_summary(tmp_path: Path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "example.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "auto")
    monkeypatch.delenv("PI_CODE_INDEX_POSTGRES_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)

    code = cli.main(["--no-daemon", "status", "--repo", str(repo)])
    out = capsys.readouterr().out

    assert code == 0
    assert "Backend: cocoindex (requested: auto)" in out
    assert "Postgres: not configured (required)" in out
    assert "Start Postgres: runtime/postgres/podman-pgvector.sh" in out


def test_empty_globs_are_warning_by_default(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")

    payload = run_setup_checks(repo)
    glob_check = next(check for check in payload["checks"] if check["id"] == "globs.non_empty")

    assert glob_check["ok"] is False
    assert glob_check["severity"] == "warning"


def test_postgres_checks_are_psql_first_by_backend(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "example.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PI_CODE_INDEX_POSTGRES_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)

    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "auto")
    auto = run_setup_checks(repo)
    auto_url = next(check for check in auto["checks"] if check["id"] == "postgres.url")
    assert auto_url["severity"] == "error"
    assert auto_url["details"]["configured_url_source"] == "none"
    assert "PI_CODE_INDEX_POSTGRES_URL" in auto_url["suggested_command"]

    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    required = run_setup_checks(repo)
    by_id = {check["id"]: check for check in required["checks"]}
    assert by_id["postgres.url"]["severity"] == "error"
    assert by_id["postgres.reachable"]["details"]["live_check_performed"] is False
    assert by_id["postgres.reachable"]["suggested_command"] == "scripts/setup.sh --with-cocoindex --postgres-check"


def test_postgres_url_source_prefers_pi_env(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "example.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "auto")
    monkeypatch.setenv("POSTGRES_URL", "postgres://compat/example")
    monkeypatch.setenv("PI_CODE_INDEX_POSTGRES_URL", "postgres://pi/example")

    payload = run_setup_checks(repo)
    url_check = next(check for check in payload["checks"] if check["id"] == "postgres.url")

    assert url_check["details"]["configured_url_source"] == "pi_code_index"


def test_doctor_flags_malformed_postgres_url(tmp_path: Path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "example.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    monkeypatch.setenv("PI_CODE_INDEX_POSTGRES_URL", "not-a-url")
    monkeypatch.setattr(setup_checks.importlib.util, "find_spec", lambda name: object() if name in {"pi_code_index", "cocoindex"} else None)
    monkeypatch.setattr(cli, "backend_status", lambda repo: {"ok": False, "backend": "cocoindex", "canonical_tables_exist": True})

    code = cli.main(["--no-daemon", "doctor", "--json", "--repo", str(repo)])
    payload = json.loads(capsys.readouterr().out)
    by_id = {check["id"]: check for check in payload["setup"]["checks"]}

    assert code == 1
    assert payload["ok"] is False
    assert by_id["postgres.url"]["ok"] is False
    assert by_id["postgres.url"]["severity"] == "error"
    assert by_id["postgres.reachable"]["ok"] is False


def test_doctor_flags_unreachable_postgres_url(tmp_path: Path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "example.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    monkeypatch.setenv("PI_CODE_INDEX_POSTGRES_URL", "postgres://user:pass@127.0.0.1:9/tcp")
    monkeypatch.setattr(setup_checks.importlib.util, "find_spec", lambda name: object() if name in {"pi_code_index", "cocoindex"} else None)
    monkeypatch.setattr(cli, "backend_status", lambda repo: {"ok": False, "backend": "cocoindex", "canonical_tables_exist": True})

    code = cli.main(["--no-daemon", "doctor", "--json", "--repo", str(repo)])
    payload = json.loads(capsys.readouterr().out)
    by_id = {check["id"]: check for check in payload["setup"]["checks"]}

    assert code == 1
    assert by_id["postgres.reachable"]["ok"] is False
    assert by_id["postgres.reachable"]["severity"] == "error"
    assert by_id["postgres.reachable"]["details"]["live_check_performed"] is True


def test_doctor_does_not_error_for_unperformed_postgres_live_checks(tmp_path: Path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "example.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PI_CODE_INDEX_BACKEND", "cocoindex")
    monkeypatch.setenv("PI_CODE_INDEX_POSTGRES_URL", "postgres://user:pass@localhost:5432/db")
    monkeypatch.setattr(setup_checks.importlib.util, "find_spec", lambda name: object() if name in {"pi_code_index", "cocoindex"} else None)
    monkeypatch.setattr(cli, "backend_status", lambda repo: {"ok": True, "backend": "cocoindex", "canonical_tables_exist": True})
    monkeypatch.setattr(setup_checks, "_postgres_reachable", lambda host, port, timeout=0.25: (True, None))

    code = cli.main(["--no-daemon", "doctor", "--json", "--repo", str(repo)])
    payload = json.loads(capsys.readouterr().out)
    by_id = {check["id"]: check for check in payload["setup"]["checks"]}

    assert code == 0
    assert payload["setup"]["summary"]["errors"] == 0
    for check_id in ["postgres.pgvector", "postgres.permissions", "postgres.canonical_tables"]:
        assert by_id[check_id]["severity"] == "warning"
        assert by_id[check_id]["details"]["live_check_performed"] is False
    assert by_id["postgres.reachable"]["ok"] is True
    assert by_id["postgres.reachable"]["details"]["live_check_performed"] is True
