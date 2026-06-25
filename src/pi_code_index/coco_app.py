"""CocoIndex V1 app entrypoint for pi-code-index.

This module is kept as the stable import target for `cocoindex update` and for
manual inspection. Runtime CLI/daemon integration lives in `coco_backend.py`.
"""

from __future__ import annotations

from pathlib import Path

from .coco_backend import CodeEmbedding, build_app
from .config import load_global_config, load_project_config
from .indexer import repo_root


def app_for_repo(repo: Path | None = None):
    repo = repo_root(repo or Path.cwd())
    return build_app(repo, load_project_config(repo), load_global_config())


app = app_for_repo()

__all__ = ["CodeEmbedding", "app", "app_for_repo", "build_app"]
