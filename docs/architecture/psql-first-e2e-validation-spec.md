# PSQL-first end-to-end validation spec

## Scope

This is the implementation-ready contract for step 4: validate that the completed psql-first workflow works through direct `pi-code-index` CLI calls and non-interactive Pi extension calls. Do not treat this spec as permission to run the full matrix during spec generation.

Run all commands from the extension root unless a command explicitly changes directory:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
```

Container work must use Podman only. Do not introduce Docker commands.

## Validation deliverables

The validation implementer must produce these artifacts under one timestamped run directory:

```text
artifacts/psql-first-e2e/<YYYYMMDD-HHMMSS>/
  env.txt
  versions.txt
  fixtures/
    create-fixtures.sh
    dummy-python-service/
    dummy-ts-utils/
  logs/
    static/
    runtime/
    config/
    cli/
    daemon/
    pi/
    metrics/
  json/
    status-auto-no-url.json
    status-lexical-url.json
    doctor-cocoindex-no-url.json
    search-auto-bad-url.json
    python-lexical-*.json
    python-cocoindex-*.json
    ts-lexical-*.json
    daemon-*.json
  report.md
```

`report.md` must include: pass/fail summary, blocked-live-validation notes if Podman is unavailable, linked bd bugs for failures, raw artifact paths, metrics table, and lexical vs CocoIndex/Postgres precision comparison.

## Prerequisites and environment capture

Before running validation, capture:

```bash
RUN_ID="$(date +%Y%m%d-%H%M%S)"
ARTIFACT_DIR="artifacts/psql-first-e2e/$RUN_ID"
mkdir -p "$ARTIFACT_DIR"/{fixtures,logs/{static,runtime,config,cli,daemon,pi,metrics},json}
{
  pwd
  git rev-parse --show-toplevel
  git rev-parse HEAD
  git status --short
  env | sort | grep -E '^(PI_CODE_INDEX|POSTGRES|COCOINDEX|PATH|HOME)=' || true
} > "$ARTIFACT_DIR/env.txt"
{
  uv --version || true
  node --version || true
  npm --version || true
  pi --version || true
  podman --version || true
  podman compose version || true
  podman-compose --version || true
  uv run pi-code-index --version || true
} > "$ARTIFACT_DIR/versions.txt" 2>&1
```

Live Postgres validation is required when Podman is installed. If Podman is missing, static shell/compose checks still run and the live rows are marked `BLOCKED: missing podman` in `report.md`.

## Exact dummy repo fixtures

Create both fixtures with this exact script and save a copy as `$ARTIFACT_DIR/fixtures/create-fixtures.sh`.

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="${1:?usage: create-fixtures.sh <fixture-root>}"
rm -rf "$ROOT"
mkdir -p "$ROOT"

PY="$ROOT/dummy-python-service"
mkdir -p "$PY/src/shop" "$PY/tests"
cat > "$PY/src/shop/__init__.py" <<'PYEOF'
"""Tiny shop service used by pi-code-index validation."""
PYEOF
cat > "$PY/src/shop/api.py" <<'PYEOF'
from shop.orders import process_order


def handle_request(payload: dict) -> dict:
    """Handle a checkout request and return an order summary."""
    items = payload.get("items", [])
    region = payload.get("region", "CA")
    return process_order(items, region)
PYEOF
cat > "$PY/src/shop/orders.py" <<'PYEOF'
from shop.pricing import calculate_total


def process_order(items: list[dict], region: str) -> dict:
    """Process checkout items into a priced order."""
    total = calculate_total(items, region)
    return {"status": "accepted", "total": total, "region": region}
PYEOF
cat > "$PY/src/shop/pricing.py" <<'PYEOF'
from shop.tax import compute_tax


def calculate_total(items: list[dict], region: str) -> float:
    """Calculate subtotal plus regional sales tax."""
    subtotal = sum(item["price"] * item.get("quantity", 1) for item in items)
    tax = compute_tax(subtotal, region)
    return round(subtotal + tax, 2)
PYEOF
cat > "$PY/src/shop/tax.py" <<'PYEOF'
TAX_RATES = {"CA": 0.0825, "NY": 0.04}


def compute_tax(subtotal: float, region: str) -> float:
    """Compute regional tax for an order subtotal."""
    rate = TAX_RATES.get(region, 0.0)
    return round(subtotal * rate, 2)
PYEOF
cat > "$PY/tests/test_orders.py" <<'PYEOF'
from shop.orders import process_order
from shop.pricing import calculate_total


def test_calculate_total_includes_tax():
    assert calculate_total([{"price": 10.0, "quantity": 2}], "CA") == 21.65


def test_process_order_returns_summary():
    order = process_order([{"price": 5.0, "quantity": 1}], "NY")
    assert order["status"] == "accepted"
    assert order["total"] == 5.2
PYEOF
cat > "$PY/README.md" <<'PYEOF'
# Dummy Python service

Checkout/order flow fixture. Requests enter `handle_request`, orders call
`process_order`, totals are calculated by `calculate_total`, and regional tax is
computed by `compute_tax`.
PYEOF
mkdir -p "$PY/.pi-code-index"
cat > "$PY/.pi-code-index/settings.yml" <<'PYEOF'
chunk_strategy: hybrid
enable_symbols: true
enable_references: true
ast_languages: [python]
PYEOF
( cd "$PY" && git init -q && git add . && git -c user.name='pi-code-index validation' -c user.email='validation@example.invalid' commit -qm 'fixture: dummy python service' )

TS="$ROOT/dummy-ts-utils"
mkdir -p "$TS/src"
cat > "$TS/src/retry.ts" <<'TSEOF'
export async function retryWithBackoff<T>(
  operation: () => Promise<T>,
  attempts = 3,
  delayMs = 25,
): Promise<T> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await operation();
    } catch (error) {
      lastError = error;
      if (attempt < attempts) {
        await new Promise((resolve) => setTimeout(resolve, delayMs * attempt));
      }
    }
  }
  throw lastError;
}
TSEOF
cat > "$TS/src/http.ts" <<'TSEOF'
import { retryWithBackoff } from "./retry";

export async function requestJson(url: string): Promise<unknown> {
  return retryWithBackoff(async () => {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`request failed: ${response.status}`);
    }
    return response.json();
  });
}
TSEOF
cat > "$TS/src/cache.ts" <<'TSEOF'
const cache = new Map<string, unknown>();

export function rememberValue(key: string, value: unknown): void {
  cache.set(key, value);
}

export function readValue(key: string): unknown {
  return cache.get(key);
}
TSEOF
cat > "$TS/src/retry.test.ts" <<'TSEOF'
import { retryWithBackoff } from "./retry";

test("retryWithBackoff retries failed operations", async () => {
  let calls = 0;
  const result = await retryWithBackoff(async () => {
    calls += 1;
    if (calls < 2) throw new Error("try again");
    return "ok";
  }, 2, 1);
  expect(result).toBe("ok");
  expect(calls).toBe(2);
});
TSEOF
cat > "$TS/package.json" <<'TSEOF'
{"name":"dummy-ts-utils","private":true,"type":"module","scripts":{"test":"echo fixture-only"},"devDependencies":{}}
TSEOF
( cd "$TS" && git init -q && git add . && git -c user.name='pi-code-index validation' -c user.email='validation@example.invalid' commit -qm 'fixture: dummy ts utils' )
```

Create fixtures with:

```bash
FIXTURE_ROOT="$ARTIFACT_DIR/fixtures"
bash "$ARTIFACT_DIR/fixtures/create-fixtures.sh" "$FIXTURE_ROOT"
PY_REPO="$FIXTURE_ROOT/dummy-python-service"
TS_REPO="$FIXTURE_ROOT/dummy-ts-utils"
```

## Rollout and compatibility contracts

The validation report must verify the user-visible rollout contract from prior psql-first steps:

- Canonical runtime lifecycle docs and setup output point to `runtime/postgres/podman-pgvector.sh` and `runtime/postgres/compose.pgvector.yml`.
- `examples/podman-pgvector.sh` remains executable for one migration window, prints a deprecation notice, and delegates to the canonical helper with all args preserved.
- `examples/compose.pgvector.yml` remains renderable with its old `./postgres-init` mount; it is compatibility only, not canonical documentation.
- `PI_CODE_INDEX_POSTGRES_URL` is preferred over `POSTGRES_URL`; `COCOINDEX_DATABASE_URL` is treated as internal CocoIndex compatibility, not primary user config.
- `PI_CODE_INDEX_BACKEND=auto` means Postgres only when an explicit URL source exists; `lexical` means forced degraded local JSON; `cocoindex` means required Postgres/CocoIndex with no silent lexical fallback.
- Daemon environment changes require `uv run pi-code-index stop --json || true` before parity or Pi integration checks.

## Static sanity commands

Run and save stdout/stderr for each command:

```bash
uv run python -m compileall src tests > "$ARTIFACT_DIR/logs/static/compileall.log" 2>&1
uv run --extra dev pytest > "$ARTIFACT_DIR/logs/static/pytest.log" 2>&1
npm run typecheck > "$ARTIFACT_DIR/logs/static/typecheck.log" 2>&1
npm run test:ts > "$ARTIFACT_DIR/logs/static/test-ts.log" 2>&1
bash -n scripts/setup.sh runtime/postgres/podman-pgvector.sh examples/podman-pgvector.sh > "$ARTIFACT_DIR/logs/static/bash-n.log" 2>&1
podman compose -f runtime/postgres/compose.pgvector.yml config > "$ARTIFACT_DIR/logs/static/podman-compose-config.log" 2>&1 || true
podman compose -f examples/compose.pgvector.yml config > "$ARTIFACT_DIR/logs/static/examples-compose-config.log" 2>&1 || true
```

Pass criteria:

- Non-Podman static commands exit 0.
- Compose config exits 0 when Podman compose is available.
- No static output points users to `examples/` as the canonical Postgres lifecycle path.

## Runtime startup and compatibility commands

When Podman is available:

```bash
runtime/postgres/podman-pgvector.sh > "$ARTIFACT_DIR/logs/runtime/podman-helper.log" 2>&1
podman ps --filter name=pi-code-index-postgres --format json > "$ARTIFACT_DIR/logs/runtime/podman-ps.json" 2>&1
scripts/setup.sh --with-cocoindex --postgres-check --skip-tests > "$ARTIFACT_DIR/logs/runtime/setup-postgres-check.log" 2>&1
podman compose -f runtime/postgres/compose.pgvector.yml up -d > "$ARTIFACT_DIR/logs/runtime/compose-up.log" 2>&1
examples/podman-pgvector.sh > "$ARTIFACT_DIR/logs/runtime/examples-shim.log" 2>&1
```

Expected runtime contracts:

- `runtime/postgres/podman-pgvector.sh` exits 0 and prints `PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex` as an intentional setup snippet.
- `podman ps` includes container name `pi-code-index-postgres` and a running state.
- `scripts/setup.sh --with-cocoindex --postgres-check --skip-tests` exits 0 and validates pgvector.
- `examples/podman-pgvector.sh` exits 0, prints `examples/podman-pgvector.sh is deprecated; use runtime/postgres/podman-pgvector.sh` to stderr/stdout, and delegates without duplicating lifecycle behavior.

## Config/status/doctor JSON assertions

Use `jq -e` for assertions. Save every payload exactly as emitted.

### `auto`, no Postgres URL

```bash
PI_CODE_INDEX_BACKEND=auto env -u PI_CODE_INDEX_POSTGRES_URL -u POSTGRES_URL \
  uv run pi-code-index --no-daemon status --json --repo "$PY_REPO" \
  > "$ARTIFACT_DIR/json/status-auto-no-url.json"
jq -e '
  .backend.backend == "lexical" and
  .effective_backend == "lexical" and
  .requested_backend == "auto" and
  .backend_fallback == false and
  (.postgres.configured_url_source == "none") and
  ((.warnings // []) | map(test("Lexical degraded|degraded|Postgres")) | any) and
  (.capabilities.graph == false or .capabilities.graph == null or .capabilities.call_graph == false)
' "$ARTIFACT_DIR/json/status-auto-no-url.json"
```

### Forced lexical with URL present

```bash
PI_CODE_INDEX_BACKEND=lexical PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex \
  uv run pi-code-index --no-daemon status --json --repo "$PY_REPO" \
  > "$ARTIFACT_DIR/json/status-lexical-url.json"
jq -e '
  .backend.backend == "lexical" and
  .effective_backend == "lexical" and
  .requested_backend == "lexical" and
  .backend_fallback == false and
  (.postgres.configured_url_source == "pi_code_index")
' "$ARTIFACT_DIR/json/status-lexical-url.json"
```

### Required CocoIndex with missing URL

```bash
PI_CODE_INDEX_BACKEND=cocoindex env -u PI_CODE_INDEX_POSTGRES_URL -u POSTGRES_URL \
  uv run --extra cocoindex pi-code-index --no-daemon doctor --json --repo "$PY_REPO" \
  > "$ARTIFACT_DIR/json/doctor-cocoindex-no-url.json" || true
jq -e '
  (.ok == false or .setup.summary.errors > 0) and
  (.backend.backend == "cocoindex" or .backend.requested_backend == "cocoindex" or .backend == "cocoindex") and
  (tostring | test("PI_CODE_INDEX_POSTGRES_URL")) and
  (tostring | test("runtime/postgres/podman-pgvector.sh|scripts/setup.sh --with-cocoindex --postgres-check"))
' "$ARTIFACT_DIR/json/doctor-cocoindex-no-url.json"
```

### `auto`, bad URL

```bash
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://bad:bad@localhost:6543/bad \
  uv run --extra cocoindex pi-code-index --no-daemon search --json --repo "$PY_REPO" "order tax" \
  > "$ARTIFACT_DIR/json/search-auto-bad-url.json" 2> "$ARTIFACT_DIR/logs/config/search-auto-bad-url.stderr" || true
jq -e '
  if .ok == false then
    (tostring | test("Postgres|CocoIndex|connection|PI_CODE_INDEX_POSTGRES_URL"))
  else
    .requested_backend == "auto" and .backend_fallback == true and
    ((.warnings // [.warning] // []) | tostring | test("Postgres|CocoIndex|fallback|connection"))
  end
' "$ARTIFACT_DIR/json/search-auto-bad-url.json"
```

Pass/fail note: this row must not look like a clean no-result search. It either fails clearly or marks fallback clearly.

## Direct CLI feature probes

Stop the daemon before no-daemon and daemon parity groups when environment changes:

```bash
uv run pi-code-index stop --json > "$ARTIFACT_DIR/logs/daemon/stop-before-cli.json" 2>&1 || true
```

### Lexical baseline, Python fixture

```bash
PI_CODE_INDEX_BACKEND=lexical uv run pi-code-index --no-daemon refresh --json --repo "$PY_REPO" > "$ARTIFACT_DIR/json/python-lexical-refresh.json"
PI_CODE_INDEX_BACKEND=lexical uv run pi-code-index --no-daemon search --json --top-k 5 --repo "$PY_REPO" "where is tax calculated" > "$ARTIFACT_DIR/json/python-lexical-search-tax.json"
PI_CODE_INDEX_BACKEND=lexical uv run pi-code-index --no-daemon context repo-map --json --include-symbols --include-tests --repo "$PY_REPO" > "$ARTIFACT_DIR/json/python-lexical-repo-map.json"
```

Assertions:

```bash
jq -e '.ok == true and .backend == "lexical"' "$ARTIFACT_DIR/json/python-lexical-refresh.json"
jq -e '.backend == "lexical" and ([.results[].filename] | any(. == "src/shop/tax.py" or . == "src/shop/pricing.py"))' "$ARTIFACT_DIR/json/python-lexical-search-tax.json"
jq -e '.backend == "lexical" and (tostring | test("degraded|unsupported|Lexical|symbols"))' "$ARTIFACT_DIR/json/python-lexical-repo-map.json"
```

### Lexical baseline, TypeScript fixture

```bash
PI_CODE_INDEX_BACKEND=lexical uv run pi-code-index --no-daemon refresh --json --repo "$TS_REPO" > "$ARTIFACT_DIR/json/ts-lexical-refresh.json"
PI_CODE_INDEX_BACKEND=lexical uv run pi-code-index --no-daemon search --json --top-k 5 --repo "$TS_REPO" "retry with backoff" > "$ARTIFACT_DIR/json/ts-lexical-search-retry.json"
PI_CODE_INDEX_BACKEND=lexical uv run pi-code-index --no-daemon context similar --json --mode hybrid --scope files --repo "$TS_REPO" src/retry.ts > "$ARTIFACT_DIR/json/ts-lexical-similar-files.json"
```

Assertions:

```bash
jq -e '.ok == true and .backend == "lexical"' "$ARTIFACT_DIR/json/ts-lexical-refresh.json"
jq -e '([.results[].filename] | any(. == "src/retry.ts"))' "$ARTIFACT_DIR/json/ts-lexical-search-retry.json"
jq -e '([.results[].filename] | index("src/http.ts") != null) and ([.results[].filename] | index("src/cache.ts") == null or index("src/http.ts") < index("src/cache.ts"))' "$ARTIFACT_DIR/json/ts-lexical-similar-files.json"
```

### CocoIndex/Postgres full backend, Python fixture

Requires live Postgres:

```bash
export PI_CODE_INDEX_BACKEND=auto
export PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex
uv run --extra cocoindex pi-code-index --no-daemon doctor --json --repo "$PY_REPO" > "$ARTIFACT_DIR/json/python-cocoindex-doctor.json"
uv run --extra cocoindex pi-code-index --no-daemon status --json --repo "$PY_REPO" > "$ARTIFACT_DIR/json/python-cocoindex-status-before-refresh.json"
uv run --extra cocoindex pi-code-index --no-daemon refresh --json --repo "$PY_REPO" > "$ARTIFACT_DIR/json/python-cocoindex-refresh.json"
uv run --extra cocoindex pi-code-index --no-daemon search --json --top-k 5 --repo "$PY_REPO" "where is tax calculated" > "$ARTIFACT_DIR/json/python-cocoindex-search-tax.json"
uv run --extra cocoindex pi-code-index --no-daemon context repo-map --json --include-symbols --include-tests --repo "$PY_REPO" > "$ARTIFACT_DIR/json/python-cocoindex-repo-map.json"
uv run --extra cocoindex pi-code-index --no-daemon context tests --json --repo "$PY_REPO" src/shop/pricing.py > "$ARTIFACT_DIR/json/python-cocoindex-tests-pricing.json"
uv run --extra cocoindex pi-code-index --no-daemon context similar --json --mode hybrid --query "order total tax" --repo "$PY_REPO" > "$ARTIFACT_DIR/json/python-cocoindex-similar-order-tax.json"
uv run --extra cocoindex pi-code-index --no-daemon context review --json --repo "$PY_REPO" src/shop/pricing.py > "$ARTIFACT_DIR/json/python-cocoindex-review-pricing.json"
uv run --extra cocoindex pi-code-index --no-daemon symbols search --json --top-k 5 --repo "$PY_REPO" "calculate_total" > "$ARTIFACT_DIR/json/python-cocoindex-symbol-search-calculate-total.json"
uv run --extra cocoindex pi-code-index --no-daemon symbols definition --json --repo "$PY_REPO" "shop.pricing.calculate_total" > "$ARTIFACT_DIR/json/python-cocoindex-symbol-definition-calculate-total.json"
uv run --extra cocoindex pi-code-index --no-daemon symbols context --json --depth 2 --repo "$PY_REPO" "shop.pricing.calculate_total" > "$ARTIFACT_DIR/json/python-cocoindex-symbol-context-calculate-total.json"
uv run --extra cocoindex pi-code-index --no-daemon graph callers --json --depth 2 --repo "$PY_REPO" "shop.tax.compute_tax" > "$ARTIFACT_DIR/json/python-cocoindex-graph-callers-compute-tax.json"
uv run --extra cocoindex pi-code-index --no-daemon graph callees --json --depth 2 --repo "$PY_REPO" "shop.api.handle_request" > "$ARTIFACT_DIR/json/python-cocoindex-graph-callees-handle-request.json"
uv run --extra cocoindex pi-code-index --no-daemon graph impact --json --depth 2 --repo "$PY_REPO" "shop.pricing.calculate_total" > "$ARTIFACT_DIR/json/python-cocoindex-graph-impact-calculate-total.json"
```

Assertions:

```bash
jq -e '.ok == true and (.backend.backend == "cocoindex" or .backend == "cocoindex")' "$ARTIFACT_DIR/json/python-cocoindex-doctor.json"
jq -e '.backend == "cocoindex" and .requested_backend == "auto" and .backend_fallback == false' "$ARTIFACT_DIR/json/python-cocoindex-status-before-refresh.json"
jq -e '.ok == true and .backend == "cocoindex" and (.counts.ast_chunks // 0) > 0 and (.counts.symbols // 0) >= 4 and (.counts.call_edges // 0) >= 3' "$ARTIFACT_DIR/json/python-cocoindex-refresh.json"
jq -e '([.results[].filename] | any(. == "src/shop/tax.py" or . == "src/shop/pricing.py"))' "$ARTIFACT_DIR/json/python-cocoindex-search-tax.json"
jq -e 'tostring | test("src/shop/pricing.py") and test("tests/test_orders.py")' "$ARTIFACT_DIR/json/python-cocoindex-tests-pricing.json"
jq -e '([.results[].filename] | any(. == "src/shop/pricing.py" or . == "src/shop/tax.py" or . == "src/shop/orders.py"))' "$ARTIFACT_DIR/json/python-cocoindex-similar-order-tax.json"
jq -e '([.results[].qualified_name] | any(. == "shop.pricing.calculate_total" or endswith("pricing.calculate_total") or . == "pricing.calculate_total"))' "$ARTIFACT_DIR/json/python-cocoindex-symbol-search-calculate-total.json"
jq -e 'tostring | test("src/shop/pricing.py") and test("calculate_total")' "$ARTIFACT_DIR/json/python-cocoindex-symbol-definition-calculate-total.json"
jq -e 'tostring | test("calculate_total")' "$ARTIFACT_DIR/json/python-cocoindex-symbol-context-calculate-total.json"
jq -e 'tostring | test("calculate_total")' "$ARTIFACT_DIR/json/python-cocoindex-graph-callers-compute-tax.json"
jq -e 'tostring | test("process_order") and test("calculate_total") and test("compute_tax")' "$ARTIFACT_DIR/json/python-cocoindex-graph-callees-handle-request.json"
jq -e 'tostring | test("handle_request|process_order|compute_tax|test_orders")' "$ARTIFACT_DIR/json/python-cocoindex-graph-impact-calculate-total.json"
```

If a symbol/graph/context feature is legitimately unsupported in the current backend, the JSON must include a warning/unavailable/degraded field. An empty result with `ok=true` and no warning fails validation.

## Daemon parity

Run direct no-daemon first, then daemon-backed commands with the same repo and env:

```bash
uv run pi-code-index stop --json > "$ARTIFACT_DIR/logs/daemon/stop-before-parity.json" 2>&1 || true
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex \
  uv run --extra cocoindex pi-code-index --no-daemon search --json --top-k 5 --repo "$PY_REPO" "order tax" \
  > "$ARTIFACT_DIR/json/daemon-direct-search.json"
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex \
  uv run --extra cocoindex pi-code-index search --json --top-k 5 --repo "$PY_REPO" "order tax" \
  > "$ARTIFACT_DIR/json/daemon-backed-search.json"
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex \
  uv run --extra cocoindex pi-code-index status --json --repo "$PY_REPO" \
  > "$ARTIFACT_DIR/json/daemon-backed-status.json"
```

Pass criteria:

- Direct and daemon-backed payloads both report `backend=cocoindex`, `requested_backend=auto`, and `backend_fallback=false`.
- Top-3 result filenames overlap by at least one expected file: `src/shop/tax.py`, `src/shop/pricing.py`, or `src/shop/orders.py`.
- Differences are limited to daemon metadata, timing, and incidental scores.

## Non-interactive Pi integration probes

Use `--no-session` so validation is independent of existing chat state. Always save stdout/stderr and compare with direct JSON from the same repo/env.

### Lexical Pi search

```bash
PI_CODE_INDEX_BACKEND=lexical pi -p --no-session --extension ./index.ts --approve \
  "Use pi-code-index tools on repo $PY_REPO. Run code_search for 'where is tax calculated'. Return only filenames and any degraded-backend warning." \
  > "$ARTIFACT_DIR/logs/pi/lexical-search.stdout" 2> "$ARTIFACT_DIR/logs/pi/lexical-search.stderr"
```

Pass criteria: stdout names `src/shop/tax.py` or `src/shop/pricing.py`; stdout does not claim full semantic/Postgres coverage in lexical mode.

### Postgres Pi symbol/tests/graph

```bash
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex pi -p --no-session --extension ./index.ts --approve \
  "Use pi-code-index tools on repo $PY_REPO. Find symbol calculate_total, likely tests for src/shop/pricing.py, and callers of shop.tax.compute_tax. Return filenames and symbol names only." \
  > "$ARTIFACT_DIR/logs/pi/cocoindex-symbol-tests-graph.stdout" 2> "$ARTIFACT_DIR/logs/pi/cocoindex-symbol-tests-graph.stderr"
```

Pass criteria: stdout includes `calculate_total`, `tests/test_orders.py`, and either `calculate_total` as caller of `compute_tax` or a clear graph-unavailable warning that matches the direct CLI JSON.

### Rendering edge cases

```bash
PI_CODE_INDEX_BACKEND=lexical pi -p --no-session --extension ./index.ts --approve \
  "Use pi-code-index tools on repo $TS_REPO. Run find_similar_code for query 'retry with backoff' with scope files. Return the number of hits and filenames only." \
  > "$ARTIFACT_DIR/logs/pi/similar-code-rendering.stdout" 2> "$ARTIFACT_DIR/logs/pi/similar-code-rendering.stderr"

PI_CODE_INDEX_BACKEND=lexical pi -p --no-session --extension ./index.ts --approve \
  "Use pi-code-index tools on repo $PY_REPO. Run repo_map with include_symbols true and callers for shop.tax.compute_tax. If unsupported, say unsupported/degraded, not no callers." \
  > "$ARTIFACT_DIR/logs/pi/fallback-rendering.stdout" 2> "$ARTIFACT_DIR/logs/pi/fallback-rendering.stderr"
```

Pass criteria:

- Pi must not summarize non-empty direct CLI results as `no hits`.
- Pi must not turn graph unsupported/degraded warnings into `no callers exist`.
- Pi must preserve a warning/fallback state when direct JSON includes one.

Known open bugs that this section may reproduce: `pi-code-index-zet`, `pi-code-index-bwe`, `pi-code-index-dzn`. If reproduced, link them in `report.md`; do not create duplicates unless the observed failure is materially different.

## Metrics collection

Collect timing and peak RSS with `/usr/bin/time -f '%e %M'`. Run each query three times per backend/repo and calculate median elapsed seconds and max RSS KiB.

Example command pattern:

```bash
/usr/bin/time -f '%e %M' -o "$ARTIFACT_DIR/logs/metrics/python-cocoindex-search-tax.time" \
  uv run --extra cocoindex pi-code-index --no-daemon search --json --top-k 5 --repo "$PY_REPO" "where is tax calculated" \
  > "$ARTIFACT_DIR/json/metrics-python-cocoindex-search-tax.json" 2> "$ARTIFACT_DIR/logs/metrics/python-cocoindex-search-tax.stderr"
```

Required metrics:

- Podman startup elapsed seconds until healthy.
- `doctor`, `status`, and `refresh` wall time and peak RSS for lexical and CocoIndex/Postgres.
- Query median wall time and peak RSS for: `search`, `context similar`, `symbols search`, and `graph callers` where supported.
- Precision@3 and precision@5 for expected file hits.
- Symbol exact-match rate for `calculate_total`.
- Graph expected edge/path presence for `handle_request -> process_order -> calculate_total -> compute_tax`.
- Pi integration preservation: direct result count, Pi-reported hit count, direct warning/fallback state, Pi-reported warning/fallback state.

Metric pass/fail thresholds:

- Static checks: 100% pass, except Podman compose config may be `BLOCKED` only when Podman compose is unavailable.
- Required CLI status/doctor/refresh/search rows: 100% pass for applicable environment.
- Fixture precision: `search "where is tax calculated"` has precision@5 >= 0.4 and at least one of `src/shop/tax.py`/`src/shop/pricing.py` in top 3; `search "retry with backoff"` has `src/retry.ts` in top 3.
- Symbol exact-match: `calculate_total` exact qualified-name match appears in top 5 for CocoIndex/Postgres, or the feature returns an explicit unsupported/degraded warning.
- Graph path: CocoIndex/Postgres includes at least two of the three expected edges, and specifically includes `calculate_total -> compute_tax`; fallback modes must warn rather than silently report no graph.
- Pi preservation: for every non-interactive Pi probe, if direct JSON has hits, Pi stdout must include at least one expected filename/symbol and must not say `no hits`/`no results` without qualification.
- Performance: record values but do not fail on absolute latency/RSS unless a command exceeds 120 seconds on the tiny fixture or is killed by OOM. Mark such a case failed and include logs.

## Pass/fail criteria for the whole validation

Validation passes only if all applicable criteria hold:

1. Canonical Podman runtime starts and validates pgvector, or live rows are explicitly blocked by missing Podman.
2. Canonical runtime paths are used in docs/setup output: `runtime/postgres/podman-pgvector.sh` and `runtime/postgres/compose.pgvector.yml`.
3. Compatibility paths still work for one migration window and emit a deprecation notice where required.
4. `auto`, `lexical`, and `cocoindex` variants expose requested/effective backend, fallback state, capabilities, warnings, URL source, and setup guidance according to the step-3 contract.
5. Required `cocoindex` mode does not silently fall back to lexical.
6. Semantic/context/symbol/graph probes return expected dummy-repo facts or clearly mark the feature unsupported/degraded.
7. Non-interactive Pi sessions invoke this extension and preserve direct tool hits/warnings.
8. Metrics and raw logs are sufficient for a reviewer to compare lexical vs CocoIndex/Postgres overhead and precision.
9. No Docker commands are added.

## Bug filing rules

For every failed pass/fail criterion, file or update a bd issue before closing the validation implementation issue.

Use these rules:

- If the failure matches an existing open issue, update that issue with artifact path, command, expected assertion, actual output, and validation run id. Known likely matches: `pi-code-index-zet`, `pi-code-index-bwe`, `pi-code-index-dzn`.
- If no existing issue matches, create a new `bug` with priority:
  - `1` for psql-first blocker: wrong backend selection, silent CocoIndex fallback, canonical runtime broken, Postgres credentials leaked outside setup snippets, or Pi loses non-empty tool hits.
  - `2` for feature correctness gaps: graph/symbol/context unexpectedly empty, daemon parity mismatch, bad setup guidance.
  - `3` for reporting/metrics/doc polish that does not block use.
- New bug labels must include `pi-code-index`, `psql-first`, `validation`; add more specific labels such as `postgres`, `pi-extension`, `graph`, `symbols`, or `repo-map`.
- Link new bugs to the validation issue with `discovered-from:pi-code-index-i49.4.3` if the implementation issue exists, otherwise mention `pi-code-index-i49.4` and this spec path in the description.
- Do not close validation as passed while untriaged failures remain. Known failures may be accepted only when linked in `report.md` with exact evidence and an explicit `KNOWN FAILURE` status.

## Reviewer checklist

A reviewer can accept the validation report if:

- The fixture script in artifacts matches this spec.
- All JSON assertion commands are present or equivalent stricter checks are documented.
- Artifact paths are relative to `artifacts/psql-first-e2e/<run-id>/` and are not overwritten by later runs.
- Pi probes use `pi -p --no-session --extension ./index.ts --approve`.
- All container commands use Podman.
- The final report clearly separates `PASS`, `FAIL`, `KNOWN FAILURE`, and `BLOCKED` rows.
