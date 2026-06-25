from __future__ import annotations

import os
from dataclasses import dataclass, field
import re
from pathlib import Path
from typing import Any

POSTGRES_LIFECYCLE_COMMAND = "runtime/postgres/podman-pgvector.sh"
POSTGRES_COMPOSE_COMMAND = "podman compose -f runtime/postgres/compose.pgvector.yml up -d"
POSTGRES_VALIDATION_COMMAND = "scripts/setup.sh --with-cocoindex --postgres-check"
POSTGRES_EXPORT_COMMAND = "export PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex"
DAEMON_RESTART_REMINDER = "After changing PI_CODE_INDEX_BACKEND or PI_CODE_INDEX_POSTGRES_URL, run pi-code-index stop --json so the daemon inherits the new environment on the next request."

import yaml

DEFAULT_INCLUDE = [
    "**/*.py", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx", "**/*.rs", "**/*.go",
    "**/*.java", "**/*.md", "**/*.mdx", "**/*.toml", "**/*.json", "**/*.yaml", "**/*.yml",
]
DEFAULT_EXCLUDE = [
    "**/.*", "**/.git/**", "**/node_modules/**", "**/target/**", "**/dist/**", "**/build/**",
    "**/__pycache__/**", "**/.venv/**", "**/.pi-code-index/**",
]

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_identifier(name: str, field_name: str = "identifier") -> str:
    if not IDENTIFIER_RE.match(name):
        raise ValueError(f"invalid {field_name} {name!r}; use an unqualified PostgreSQL identifier")
    return name

@dataclass
class GlobalConfig:
    backend: str = "auto"
    postgres_url: str = ""
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    socket_path: str = "~/.pi-code-index/daemon.sock"
    pid_path: str = "~/.pi-code-index/daemon.pid"
    log_path: str = "~/.pi-code-index/daemon.log"
    schema_name: str = "public"
    table_prefix: str = "pi_code_index"
    pipeline_version: str = "canonical-v1-ast-v1"
    daemon_request_timeout_seconds: float = 120.0
    daemon_start_timeout_seconds: float = 4.0
    daemon_handshake_retry_interval_seconds: float = 0.1
    daemon_max_cached_repos: int | None = None
    daemon_resource_idle_ttl_seconds: float | None = None
    live_poll_interval_seconds: float = 1.0
    live_refresh_debounce_seconds: float = 0.25
    live_stale_after_seconds: float = 300.0
    live_max_consecutive_errors_before_stale: int = 3
    setup_error_on_empty_globs: bool = False
    status_latest_errors_limit: int = 5

@dataclass
class ProjectConfig:
    backend: str = "auto"
    table_name: str = "code_embeddings"
    chunk_size: int = 1000
    min_chunk_size: int = 120
    chunk_overlap: int = 120
    include: list[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE))
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    schema_name: str | None = None
    table_prefix: str | None = None
    branch_mode: str = "current"
    compatibility_view: bool = True
    enable_symbols: bool = False
    enable_references: bool = False
    enable_test_links: bool = False
    chunk_strategy: str = "recursive"
    ast_languages: list[str] | None = None
    max_ast_chunk_bytes: int = 12000
    max_result_code_bytes: int = 12000
    ast_context_lines: int = 3
    symbol_languages: list[str] | None = None
    symbol_kinds: list[str] | None = None
    symbol_embedding_model: str | None = None
    max_symbol_docstring_bytes: int = 4000
    max_symbol_signature_bytes: int = 1000
    max_graph_depth: int = 5
    max_graph_edges: int = 5000
    reference_languages: list[str] | None = None
    min_call_edge_confidence: float = 0.35

def data_home() -> Path:
    path = Path.home() / ".pi-code-index"
    path.mkdir(parents=True, exist_ok=True)
    return path

def global_config_path() -> Path:
    return data_home() / "config.yml"

def project_dir(repo: Path) -> Path:
    return repo / ".pi-code-index"

def project_config_path(repo: Path) -> Path:
    return project_dir(repo) / "settings.yml"

def index_path(repo: Path) -> Path:
    safe = str(repo.resolve()).strip("/").replace("/", "__") or "root"
    return data_home() / "indexes" / f"{safe}.json"

def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def os_environ(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def postgres_url_config() -> tuple[str, str | None]:
    if url := os_environ("PI_CODE_INDEX_POSTGRES_URL"):
        return "pi_code_index", url
    if url := os_environ("POSTGRES_URL"):
        return "postgres_url", url
    if url := _load_yaml(global_config_path()).get("postgres_url"):
        return "config", str(url)
    return "none", None

def _known_dataclass_values(cls: type[GlobalConfig] | type[ProjectConfig], values: dict[str, Any]) -> dict[str, Any]:
    known = set(cls.__dataclass_fields__)
    return {key: value for key, value in values.items() if key in known}


def load_global_config() -> GlobalConfig:
    data = {**GlobalConfig().__dict__, **_load_yaml(global_config_path())}
    cfg = GlobalConfig(**_known_dataclass_values(GlobalConfig, data))
    if backend := os_environ("PI_CODE_INDEX_BACKEND"):
        cfg.backend = backend
    if postgres_url := (os_environ("PI_CODE_INDEX_POSTGRES_URL") or os_environ("POSTGRES_URL")):
        cfg.postgres_url = postgres_url
    if embedding_model := os_environ("PI_CODE_INDEX_EMBEDDING_MODEL"):
        cfg.embedding_model = embedding_model
    if schema_name := os_environ("PI_CODE_INDEX_SCHEMA_NAME"):
        cfg.schema_name = schema_name
    if table_prefix := os_environ("PI_CODE_INDEX_TABLE_PREFIX"):
        cfg.table_prefix = table_prefix
    if pipeline_version := os_environ("PI_CODE_INDEX_PIPELINE_VERSION"):
        cfg.pipeline_version = pipeline_version
    for env_name, field_name, parser in [
        ("PI_CODE_INDEX_DAEMON_REQUEST_TIMEOUT_SECONDS", "daemon_request_timeout_seconds", _positive_float),
        ("PI_CODE_INDEX_LIVE_POLL_INTERVAL_SECONDS", "live_poll_interval_seconds", _positive_float),
        ("PI_CODE_INDEX_LIVE_REFRESH_DEBOUNCE_SECONDS", "live_refresh_debounce_seconds", _positive_float),
        ("PI_CODE_INDEX_LIVE_STALE_AFTER_SECONDS", "live_stale_after_seconds", _positive_float),
        ("PI_CODE_INDEX_LIVE_MAX_CONSECUTIVE_ERRORS_BEFORE_STALE", "live_max_consecutive_errors_before_stale", _positive_int),
        ("PI_CODE_INDEX_STATUS_LATEST_ERRORS_LIMIT", "status_latest_errors_limit", _positive_int),
    ]:
        if raw := os_environ(env_name):
            setattr(cfg, field_name, parser(raw, field_name))
    cfg.daemon_request_timeout_seconds = _positive_float(cfg.daemon_request_timeout_seconds, "daemon_request_timeout_seconds")
    cfg.daemon_start_timeout_seconds = _positive_float(cfg.daemon_start_timeout_seconds, "daemon_start_timeout_seconds")
    cfg.daemon_handshake_retry_interval_seconds = _positive_float(cfg.daemon_handshake_retry_interval_seconds, "daemon_handshake_retry_interval_seconds")
    cfg.daemon_max_cached_repos = _optional_positive_int(cfg.daemon_max_cached_repos, "daemon_max_cached_repos")
    cfg.daemon_resource_idle_ttl_seconds = _optional_positive_float(cfg.daemon_resource_idle_ttl_seconds, "daemon_resource_idle_ttl_seconds")
    cfg.live_poll_interval_seconds = _positive_float(cfg.live_poll_interval_seconds, "live_poll_interval_seconds")
    cfg.live_refresh_debounce_seconds = _positive_float(cfg.live_refresh_debounce_seconds, "live_refresh_debounce_seconds")
    cfg.live_stale_after_seconds = _positive_float(cfg.live_stale_after_seconds, "live_stale_after_seconds")
    cfg.live_max_consecutive_errors_before_stale = _positive_int(cfg.live_max_consecutive_errors_before_stale, "live_max_consecutive_errors_before_stale")
    cfg.status_latest_errors_limit = _positive_int(cfg.status_latest_errors_limit, "status_latest_errors_limit")
    validate_identifier(cfg.schema_name, "schema_name")
    validate_identifier(cfg.table_prefix, "table_prefix")
    return cfg

def _positive_float(value: Any, field_name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be a positive number")
    return parsed


def _optional_positive_float(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    return _positive_float(value, field_name)


def _optional_positive_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field_name)


def _positive_int(value: Any, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return parsed


def _normalize_ast_languages(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    else:
        items = [str(item).strip() for item in value]
    languages = [item.lower() for item in items if item]
    if not languages:
        raise ValueError("ast_languages must be null or a non-empty list")
    return languages


def load_project_config(repo: Path) -> ProjectConfig:
    data = {**ProjectConfig().__dict__, **_load_yaml(project_config_path(repo))}
    cfg = ProjectConfig(**_known_dataclass_values(ProjectConfig, data))
    if backend := os_environ("PI_CODE_INDEX_BACKEND"):
        cfg.backend = backend
    if chunk_strategy := os_environ("PI_CODE_INDEX_CHUNK_STRATEGY"):
        cfg.chunk_strategy = chunk_strategy
    if ast_languages := os_environ("PI_CODE_INDEX_AST_LANGUAGES"):
        cfg.ast_languages = _normalize_ast_languages(ast_languages)
    if max_ast_chunk_bytes := os_environ("PI_CODE_INDEX_MAX_AST_CHUNK_BYTES"):
        cfg.max_ast_chunk_bytes = _positive_int(max_ast_chunk_bytes, "max_ast_chunk_bytes")
    if max_result_code_bytes := os_environ("PI_CODE_INDEX_MAX_RESULT_CODE_BYTES"):
        cfg.max_result_code_bytes = _positive_int(max_result_code_bytes, "max_result_code_bytes")
    if ast_context_lines := os_environ("PI_CODE_INDEX_AST_CONTEXT_LINES"):
        cfg.ast_context_lines = _positive_int(ast_context_lines, "ast_context_lines")
    if symbol_languages := os_environ("PI_CODE_INDEX_SYMBOL_LANGUAGES"):
        cfg.symbol_languages = _normalize_ast_languages(symbol_languages)
    if symbol_kinds := os_environ("PI_CODE_INDEX_SYMBOL_KINDS"):
        cfg.symbol_kinds = _normalize_ast_languages(symbol_kinds)
    if symbol_embedding_model := os_environ("PI_CODE_INDEX_SYMBOL_EMBEDDING_MODEL"):
        cfg.symbol_embedding_model = symbol_embedding_model
    if max_symbol_docstring_bytes := os_environ("PI_CODE_INDEX_MAX_SYMBOL_DOCSTRING_BYTES"):
        cfg.max_symbol_docstring_bytes = _positive_int(max_symbol_docstring_bytes, "max_symbol_docstring_bytes")
    if max_symbol_signature_bytes := os_environ("PI_CODE_INDEX_MAX_SYMBOL_SIGNATURE_BYTES"):
        cfg.max_symbol_signature_bytes = _positive_int(max_symbol_signature_bytes, "max_symbol_signature_bytes")
    if max_graph_depth := os_environ("PI_CODE_INDEX_MAX_GRAPH_DEPTH"):
        cfg.max_graph_depth = _positive_int(max_graph_depth, "max_graph_depth")
    if max_graph_edges := os_environ("PI_CODE_INDEX_MAX_GRAPH_EDGES"):
        cfg.max_graph_edges = _positive_int(max_graph_edges, "max_graph_edges")
    if reference_languages := os_environ("PI_CODE_INDEX_REFERENCE_LANGUAGES"):
        cfg.reference_languages = _normalize_ast_languages(reference_languages)
    if min_call_edge_confidence := os_environ("PI_CODE_INDEX_MIN_CALL_EDGE_CONFIDENCE"):
        cfg.min_call_edge_confidence = float(min_call_edge_confidence)
    validate_identifier(cfg.table_name, "table_name")
    if cfg.schema_name is not None:
        validate_identifier(cfg.schema_name, "schema_name")
    if cfg.table_prefix is not None:
        validate_identifier(cfg.table_prefix, "table_prefix")
    if cfg.branch_mode != "current":
        raise ValueError("branch_mode must be 'current' for this foundation release")
    cfg.chunk_strategy = str(cfg.chunk_strategy).lower()
    if cfg.chunk_strategy not in {"recursive", "ast", "hybrid"}:
        raise ValueError("chunk_strategy must be one of: recursive, ast, hybrid")
    cfg.ast_languages = _normalize_ast_languages(cfg.ast_languages)
    cfg.max_ast_chunk_bytes = _positive_int(cfg.max_ast_chunk_bytes, "max_ast_chunk_bytes")
    cfg.max_result_code_bytes = _positive_int(cfg.max_result_code_bytes, "max_result_code_bytes")
    cfg.ast_context_lines = _positive_int(cfg.ast_context_lines, "ast_context_lines")
    cfg.symbol_languages = _normalize_ast_languages(cfg.symbol_languages) if cfg.symbol_languages is not None else cfg.ast_languages
    cfg.symbol_kinds = _normalize_ast_languages(cfg.symbol_kinds)
    cfg.max_symbol_docstring_bytes = _positive_int(cfg.max_symbol_docstring_bytes, "max_symbol_docstring_bytes")
    cfg.max_symbol_signature_bytes = _positive_int(cfg.max_symbol_signature_bytes, "max_symbol_signature_bytes")
    cfg.max_graph_depth = min(5, _positive_int(cfg.max_graph_depth, "max_graph_depth"))
    cfg.max_graph_edges = _positive_int(cfg.max_graph_edges, "max_graph_edges")
    cfg.reference_languages = _normalize_ast_languages(cfg.reference_languages) if cfg.reference_languages is not None else (cfg.symbol_languages or cfg.ast_languages or ["python"])
    cfg.min_call_edge_confidence = float(cfg.min_call_edge_confidence)
    if not 0.0 <= cfg.min_call_edge_confidence <= 1.0:
        raise ValueError("min_call_edge_confidence must be between 0.0 and 1.0")
    return cfg

def write_default_configs(repo: Path) -> None:
    data_home().mkdir(parents=True, exist_ok=True)
    if not global_config_path().exists():
        global_data = GlobalConfig().__dict__ | {"postgres_url": ""}
        global_config_path().write_text(yaml.safe_dump(global_data, sort_keys=False), encoding="utf-8")
    pdir = project_dir(repo)
    pdir.mkdir(parents=True, exist_ok=True)
    pconf = project_config_path(repo)
    if not pconf.exists():
        pconf.write_text(yaml.safe_dump(ProjectConfig().__dict__, sort_keys=False), encoding="utf-8")
    gitignore = repo / ".gitignore"
    marker = ".pi-code-index/index*"
    if gitignore.exists():
        text = gitignore.read_text(encoding="utf-8")
        if marker not in text:
            gitignore.write_text(text.rstrip() + f"\n{marker}\n", encoding="utf-8")
    else:
        gitignore.write_text(f"{marker}\n", encoding="utf-8")
