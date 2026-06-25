# Pi Code Index Extension Specification

## Summary

Build a Pi extension plus local daemon/CLI that gives Pi access to a continuously fresh semantic code index for the current repository. CocoIndex V1 maintains the index incrementally and live; Pi accesses it through a small `code_search` tool.

The user experience should be invisible:

```text
User asks Pi: "where is config loaded?"
Pi calls: code_search({ query: "where is config loaded?" })
Extension calls: pi-code-index search --json "where is config loaded?"
CLI auto-starts daemon if needed
Daemon searches live CocoIndex/Postgres index
Pi receives ranked file/line/code chunks
```

## Goals

- Provide a Pi tool for semantic code search over the active repo.
- Keep the index fresh as files change, using CocoIndex V1 live mode.
- Avoid requiring users to manually start/restart a daemon.
- Keep Pi extension code small: Pi should be a client, not the indexer.
- Support human CLI usage outside Pi.
- Make daemon lifecycle robust: auto-start, version handshake, clean shutdown.
- Use Podman for local container development.

## Non-goals

- Do not replace Pi's built-in `read`, `grep`, `find`, or `bash` tools.
- Do not build an MCP server first. Pi extension support is the primary integration.
- Do not keep persistent Pi-to-daemon sessions.
- Do not implement full call graph/symbol graph in v1 unless CocoIndex-code APIs make it trivial.
- Do not require cloud embedding APIs for the default setup.

## Architecture

```text
Pi Extension: ~/.pi/agent/extensions/pi-code-index/index.ts
  - registers code_search tool
  - optional commands: /code-index-status, /code-index-refresh
  - shells out to local CLI: pi-code-index

pi-code-index CLI
  - init
  - search --json [--refresh] <query>
  - refresh
  - status
  - stop
  - auto-starts daemon on first real request

Daemon
  - Unix socket server
  - per-request connections
  - version/config handshake
  - owns loaded embedding model
  - owns Postgres pool
  - starts/coordinates CocoIndex live updater per project
  - serves search/refresh/status requests

CocoIndex V1 App
  - localfs.walk_dir(..., live=True)
  - Tree-sitter-aware RecursiveSplitter
  - SentenceTransformerEmbedder
  - postgres.mount_table_target(...)
  - pgvector vector index

Postgres + pgvector
  - stores code chunks, metadata, embeddings
  - queried with pgvector cosine distance
```

## Components

### 1. Pi extension

Location:

```text
~/.pi/agent/extensions/pi-code-index/index.ts
```

Responsibilities:

- Register `code_search` tool.
- Accept query, `top_k`, and optional `refresh`.
- Call `pi-code-index search --json` via `pi.exec`.
- Return compact text to the model and structured details for rendering/debugging.
- Truncate overly large results.
- Add prompt guidance so the model uses semantic search for conceptual repo questions.

Tool schema:

```ts
{
  query: string;
  top_k?: number;       // default 8
  refresh?: boolean;    // default false
}
```

Tool result shape:

```ts
{
  content: [{ type: "text", text: string }],
  details: {
    query: string,
    top_k: number,
    refresh: boolean,
    results: Array<{
      score: number,
      filename: string,
      start_line: number,
      end_line: number,
      code: string
    }>
  }
}
```

Prompt guidance:

- Use `code_search` when the user asks where a behavior/concept is implemented.
- Use `code_search` before broad grep for semantic/conceptual queries.
- Use `read` after `code_search` to inspect full files around matched locations.

Optional commands:

- `/code-index-status` -> `pi-code-index status --json`
- `/code-index-refresh` -> `pi-code-index refresh`
- `/code-index-stop` -> `pi-code-index stop`

### 2. CLI

Proposed executable name:

```text
pi-code-index
```

Commands:

```bash
pi-code-index init [--repo <path>]
pi-code-index search [--json] [--top-k 8] [--refresh] <query>
pi-code-index refresh [--repo <path>]
pi-code-index status [--json]
pi-code-index stop
```

Behavior:

- `init` creates config files and updates local `.gitignore`.
- `search`, `refresh`, and `status` connect to daemon.
- If socket connect fails, CLI starts daemon detached and retries.
- Every request opens a new Unix socket connection, handshakes, sends request, receives response, closes.

### 3. Daemon

Use ideas from CocoIndex's invisible daemon architecture:

- Auto-start on first use via Unix socket probe.
- Version handshake on every connection.
- Per-request connections only.
- Close listener for shutdown; avoid polling thread events.
- PID file removal is the exit signal.
- Global settings mtime in handshake; restart if changed.
- Project settings read fresh per operation where possible.

Daemon paths:

```text
~/.pi-code-index/daemon.sock
~/.pi-code-index/daemon.pid
~/.pi-code-index/daemon.log
~/.pi-code-index/config.yml
```

Project paths:

```text
<repo>/.pi-code-index/settings.yml
<repo>/.pi-code-index/main.py
<repo>/.pi-code-index/query.py
```

Daemon request protocol, JSON over Unix socket:

```json
{
  "type": "handshake",
  "client_version": "0.1.0",
  "protocol_version": 1,
  "global_config_mtime": 123456789
}
```

```json
{
  "type": "search",
  "repo": "/path/to/repo",
  "query": "where is auth configured?",
  "top_k": 8,
  "refresh": false
}
```

```json
{
  "type": "refresh",
  "repo": "/path/to/repo"
}
```

```json
{
  "type": "status",
  "repo": "/path/to/repo"
}
```

```json
{
  "type": "stop"
}
```

### 4. CocoIndex app

Use CocoIndex V1 APIs only.

Index schema:

```python
@dataclass
class CodeEmbedding:
    id: int
    repo: str
    filename: str
    code: str
    embedding: Annotated[NDArray, EMBEDDER]
    start_line: int
    end_line: int
```

Core pipeline:

- `localfs.walk_dir(repo, recursive=True, live=True, path_matcher=...)`
- `detect_code_language(filename=...)`
- `RecursiveSplitter().split(..., language=language)`
- `SentenceTransformerEmbedder("sentence-transformers/all-MiniLM-L6-v2")`
- `postgres.mount_table_target(...)`
- `target_table.declare_vector_index(column="embedding", metric="cosine", method="hnsw")`

Default include patterns:

```yaml
include:
  - "**/*.py"
  - "**/*.ts"
  - "**/*.tsx"
  - "**/*.js"
  - "**/*.jsx"
  - "**/*.rs"
  - "**/*.go"
  - "**/*.java"
  - "**/*.md"
  - "**/*.mdx"
  - "**/*.toml"
  - "**/*.json"
  - "**/*.yaml"
  - "**/*.yml"
```

Default exclude patterns:

```yaml
exclude:
  - "**/.*"
  - "**/.git/**"
  - "**/node_modules/**"
  - "**/target/**"
  - "**/dist/**"
  - "**/build/**"
  - "**/__pycache__/**"
  - "**/.venv/**"
```

## Configuration

Global config:

```yaml
postgres_url: "postgres://cocoindex:cocoindex@localhost/cocoindex"
embedding_model: "sentence-transformers/all-MiniLM-L6-v2"
socket_path: "~/.pi-code-index/daemon.sock"
pid_path: "~/.pi-code-index/daemon.pid"
log_path: "~/.pi-code-index/daemon.log"
```

Project config:

```yaml
table_name: "code_embeddings"
chunk_size: 1000
min_chunk_size: 300
chunk_overlap: 300
include:
  - "**/*.py"
  - "**/*.ts"
exclude:
  - "**/.git/**"
  - "**/node_modules/**"
```

## Local infrastructure

Postgres + pgvector should be started with Podman:

```bash
podman run -d \
  --name pi-code-index-postgres \
  -e POSTGRES_USER=cocoindex \
  -e POSTGRES_PASSWORD=cocoindex \
  -e POSTGRES_DB=cocoindex \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

## Lifecycle behavior

### First search

1. Pi calls `code_search`.
2. Extension runs `pi-code-index search --json ...`.
3. CLI tries socket connect.
4. If unavailable, CLI starts daemon detached.
5. CLI waits for socket.
6. CLI connects, handshakes, sends search.
7. Daemon ensures project is indexed or indexing.
8. Daemon returns ranked results.

### Upgrade or config change

- CLI sends version and global config mtime during handshake.
- If daemon version/protocol/config is stale, CLI stops daemon and starts a fresh one.
- Project settings are read fresh on indexing/search operations.

### Shutdown

- `pi-code-index stop` sends stop request.
- Daemon removes PID file and closes listener.
- Client treats PID file removal as the exit signal.

## Search query

Postgres vector query:

```sql
SELECT filename, code, embedding <=> $1 AS distance, start_line, end_line
FROM "code_embeddings"
WHERE repo = $2
ORDER BY distance ASC
LIMIT $3
```

Similarity score:

```python
score = 1.0 - float(distance)
```

## Error handling

- If Postgres is unavailable, return actionable error with Podman start hint.
- If index is empty, suggest `pi-code-index refresh`.
- If daemon cannot start, show daemon log path.
- If embedding model download fails, show model/cache error and global config path.
- If result output is too large, truncate code chunks and include metadata.

## Testing plan

### Unit tests

- CLI request/response encoding.
- Version handshake mismatch behavior.
- Config mtime restart decision.
- Result formatting/truncation.
- Pi extension command construction.

### Integration tests

- Start Postgres via Podman.
- Run `pi-code-index init` in a temp repo.
- Index sample files.
- Search returns expected file/line chunk.
- Editing a file updates search results after live refresh.
- Daemon auto-starts after socket missing.
- Daemon restarts after version/config mismatch.
- `stop` removes PID file and socket.

### Pi manual tests

- `/reload` loads extension.
- Ask: "where is X implemented?"
- Confirm Pi calls `code_search`.
- Confirm Pi follows up with `read` around returned line ranges.

## Implementation phases

### Phase 1: spec and skeleton

- Create extension folder.
- Add `SPEC.md`.
- Add minimal `package.json` and `index.ts` registering a stub `code_search` tool.

### Phase 2: standalone CocoIndex app

- Add `.pi-code-index/main.py` and `query.py` prototype.
- Verify catch-up indexing.
- Verify direct query.

### Phase 3: CLI without daemon

- Implement `pi-code-index search --json` that loads embedder per call.
- Wire Pi extension to CLI.
- Validate user experience.

### Phase 4: daemon

- Add Unix socket daemon.
- Auto-start on first request.
- Add version/config handshake.
- Add per-request handling.
- Keep embedder and Postgres pool warm.

### Phase 5: live indexing

- Run CocoIndex in live mode under daemon supervision.
- Track per-project status.
- Add refresh/status commands.

### Phase 6: polish

- Custom rendering for search results in Pi.
- Better truncation and line snippets.
- Optional symbol/call-graph extensions if CocoIndex-code APIs are adopted.

## Open questions

- Should the CLI and daemon live inside this Pi extension folder, or be packaged as a separate Python project installed with `uv tool`/`pipx`?
- Should each repo get its own Postgres table, or should all repos share one table with a `repo` column?
- Should the daemon run one CocoIndex live task per repo or start/stop project watchers on demand?
- Should query results include neighboring context lines from disk, or should Pi use `read` for that?
- Should `refresh: true` block until catch-up completes, or start refresh and return current results?

## References

- https://cocoindex.io/
- https://cocoindex.io/blogs/index-codebase-v1/
- https://cocoindex.io/docs/examples/index-codebase/
- https://cocoindex.io/docs/examples/text-embedding/
- https://cocoindex.io/docs/connectors/postgres/
- https://cocoindex.io/blogs/building-an-invisible-daemon/
