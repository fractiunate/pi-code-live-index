# pi-code-index

Runbook for the Pi live code-index extension.

## What this is

`pi-code-index` gives Pi live semantic code search and code-intelligence tools for the current repo.

- Pi extension entrypoint: `index.ts`
- CLI: `pi-code-index`
- Backend: CocoIndex V1 + Postgres/pgvector
- Live mode: daemon-supervised polling watcher that refreshes the CocoIndex index after file changes
- Daemon: Unix socket auto-start with warm CocoIndex resources

Pi tools exposed:

- `code_search`
- `symbol_search`, `symbol_definition`, `symbol_context`
- `find_callers`, `find_callees`, `impact_analysis`
- `repo_map`, `find_tests`, `find_similar_code`, `review_context`

Pi slash commands exposed:

- `/code-index-status`
- `/code-index-refresh`
- `/code-index-stop`
- `/code-index-live-status`
- `/code-index-live-start`
- `/code-index-live-stop`
- `/code-index-doctor`

## Dependencies

Required:

- Pi extension directory: `~/.pi/agent/extensions/pi-code-index`
- `node` + `npm`
- Python 3.11+
- `uv`
- `git`
- `podman`
- CocoIndex extras: `uv sync --extra cocoindex`
- Postgres + pgvector via local Podman auto-start
- Default local URL: `postgres://cocoindex:cocoindex@localhost:5432/cocoindex`

Do not use Docker for local backend development; use Podman.

## Setup

```bash
cd ~/.pi/agent/extensions/pi-code-index
uv sync --extra dev --extra cocoindex
npm install

cd /path/to/repo
pi-code-index init
pi-code-index search --json --top-k 8 "where is config loaded"
pi-code-index status --json
```

The daemon uses the default local Postgres URL, starts the Podman pgvector runtime when needed, and starts live indexing for the repo on normal tool requests. It does not write the default database password to repo or global config files.

`PI_CODE_INDEX_BACKEND=auto` is accepted as a compatibility alias for `cocoindex`.

## Start and maintain the daemon

The daemon auto-starts on the first command that does not use `--no-daemon`.

Start/warm it:

```bash
cd /path/to/repo
pi-code-index status --json
pi-code-index search --json "where is config loaded"
```

Stop it:

```bash
pi-code-index stop --json
```

Restart it after changing backend env vars, config files, or extension code:

```bash
pi-code-index stop --json
pi-code-index status --json
```

Daemon files:

```text
~/.pi-code-index/daemon.sock
~/.pi-code-index/daemon.pid
~/.pi-code-index/daemon.log
```

## Live indexing

Live polling starts automatically for a repo on normal daemon-backed tool requests.

Inspect or stop it manually:

```bash
cd /path/to/repo
pi-code-index live status --json
pi-code-index live stop --json
```

`live stop` stops the watcher now; the next normal repo request may start it again. Live mode refreshes CocoIndex after matching files change. Rapid edits are debounced.

## Pi TUI runbook

After installing or changing the extension:

```text
/reload
/code-index-status
/code-index-doctor
```

Use `code_search` for broad questions, then `read` the listed files before editing.

Use:

- `repo_map` before broad edits
- `find_tests` before choosing validation
- `find_similar_code` before adding duplicate-prone logic
- `review_context` before handoff or closing work

## Validation

Run after code changes:

```bash
cd ~/.pi/agent/extensions/pi-code-index
uv run python -m compileall src tests
uv run --extra dev pytest -q
npm run typecheck
npm run test:ts
```

Run Postgres integration when pgvector is available:

```bash
runtime/postgres/podman-pgvector.sh
uv run --extra cocoindex pytest tests/test_cocoindex_postgres_integration.py -q
```

## Health checks

```bash
pi-code-index doctor --json
pi-code-index status --json
pi-code-index live status --json
```

Expected healthy basics:

- `effective_backend` is `cocoindex`
- Postgres URL is configured
- pgvector runtime is reachable
- `counts.files` and `counts.chunks` are non-zero after refresh
- live watcher is `running` after `pi-code-index live start --json`

## Troubleshooting

### Pi does not see the extension

```text
/reload
```

Then verify this folder exists:

```bash
ls ~/.pi/agent/extensions/pi-code-index/index.ts
```

### CLI not found

```bash
cd ~/.pi/agent/extensions/pi-code-index
uv tool install -e .
command -v pi-code-index
```

### Postgres/CocoIndex not active

```bash
cd ~/.pi/agent/extensions/pi-code-index
runtime/postgres/podman-pgvector.sh
pi-code-index stop --json
pi-code-index doctor --json
```

### Daemon wedged

```bash
pi-code-index stop --json || true
rm -f ~/.pi-code-index/daemon.sock ~/.pi-code-index/daemon.pid
pi-code-index status --json
cat ~/.pi-code-index/daemon.log
```

### Search stale after edits

```bash
pi-code-index live status --json
pi-code-index refresh --json
pi-code-index search --json "the thing that changed"
```

## Runtime assets

Canonical Postgres runtime assets live under:

```text
runtime/postgres/compose.pgvector.yml
runtime/postgres/init/01-vector.sql
runtime/postgres/podman-pgvector.sh
```

The `examples/` directory is compatibility/sample space only.
