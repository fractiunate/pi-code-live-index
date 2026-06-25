# PSQL-first end-to-end validation plan

## Goal

Validate the completed psql-first workflow before any final integration report: direct `pi-code-index` CLI and non-interactive Pi sessions must both show the intended Postgres/pgvector path, clear degraded lexical fallback, and usable semantic/context/symbol/graph behavior on small dummy repos.

This is a planning document only. Do not run the full matrix in this issue.

## Scope and prerequisites

Run from the extension root:

```bash
cd /home/fractiunate/.pi/agent/extensions/pi-code-index
```

Prerequisites:

- Podman and `podman compose` or `podman-compose` are available for live Postgres checks.
- Python dependencies are installed through `uv`; use `uv run --extra cocoindex` for CocoIndex/Postgres cases.
- TypeScript dependencies are installed through npm.
- Non-interactive Pi can load this extension with `pi -p --no-session --extension ./index.ts ...`.
- Dummy repos are temporary directories outside this repo and can be deleted after validation.

## Dummy repos

Use two tiny repositories so expected results are obvious and repeatable.

### `dummy-python-service`

Create a git repo with:

```text
src/shop/api.py          # handle_request() calls process_order()
src/shop/orders.py       # process_order() calls calculate_total()
src/shop/pricing.py      # calculate_total() calls compute_tax()
src/shop/tax.py          # compute_tax()
tests/test_orders.py     # tests process_order/calculate_total
README.md                # mentions checkout/order flow
```

Expected features:

- `search "where is tax calculated"` finds `tax.py` or `pricing.py`.
- `symbols search "calculate_total"` resolves the function.
- `symbols definition "shop.pricing.calculate_total"` returns `src/shop/pricing.py`.
- `graph callers "shop.tax.compute_tax"` includes `calculate_total` when CocoIndex/Postgres graph data is available.
- `context tests src/shop/pricing.py` includes `tests/test_orders.py`.
- `context similar --query "order total tax"` returns pricing/tax/order code.

### `dummy-ts-utils`

Create a git repo with:

```text
src/retry.ts             # retryWithBackoff()
src/http.ts              # requestJson() uses retryWithBackoff()
src/cache.ts             # unrelated cache helper
src/retry.test.ts        # retry tests
package.json
```

Expected features:

- `search "retry with backoff"` finds `retry.ts`.
- `context repo-map --include-symbols` reports source files and, where supported, symbols.
- `context similar src/retry.ts --scope files` ranks `http.ts` above unrelated files.
- Fallback mode can still find lexical matches even if symbols/graph are degraded.

## Validation matrix

| Area | Variant | Commands | Pass criteria | Failure evidence to capture |
| --- | --- | --- | --- | --- |
| Static sanity | No live services | `uv run python -m compileall src tests`; `uv run --extra dev pytest`; `npm run typecheck`; `npm run test:ts`; `bash -n scripts/setup.sh runtime/postgres/podman-pgvector.sh examples/podman-pgvector.sh` | Commands exit 0. | Command, exit code, failing test or syntax line. |
| Podman runtime startup | Canonical helper | `runtime/postgres/podman-pgvector.sh`; `podman ps --filter name=pi-code-index-postgres`; `scripts/setup.sh --with-cocoindex --postgres-check --skip-tests` | Container is running/healthy, pgvector extension validates, helper prints `PI_CODE_INDEX_POSTGRES_URL`. | Podman version, compose provider, container logs, setup output. |
| Podman runtime startup | Canonical compose | `podman compose -f runtime/postgres/compose.pgvector.yml config`; `podman compose -f runtime/postgres/compose.pgvector.yml up -d` | Compose resolves `./init`, preserves container/volume/env defaults, service starts. | Rendered compose config and startup logs. |
| Compatibility runtime | Old examples shim | `examples/podman-pgvector.sh`; `podman compose -f examples/compose.pgvector.yml config` | Shim delegates with deprecation notice; compatibility compose still renders. | Stderr notice, exit code, compose config error if any. |
| Env/config selection | `auto`, no URL | `PI_CODE_INDEX_BACKEND=auto env -u PI_CODE_INDEX_POSTGRES_URL -u POSTGRES_URL uv run pi-code-index --no-daemon status --json --repo <dummy>` | Effective backend is lexical, requested backend is auto, fallback is false, capabilities/warnings say degraded. | Full JSON status and any missing warning fields. |
| Env/config selection | Forced lexical with URL present | `PI_CODE_INDEX_BACKEND=lexical PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex uv run pi-code-index --no-daemon status --json --repo <dummy>` | Effective backend remains lexical; no Postgres required for success. | Full JSON status. |
| Env/config selection | `cocoindex`, missing URL | `PI_CODE_INDEX_BACKEND=cocoindex env -u PI_CODE_INDEX_POSTGRES_URL -u POSTGRES_URL uv run --extra cocoindex pi-code-index --no-daemon doctor --json --repo <dummy>` | `ok=false` or error severity; no lexical fallback; guidance names `PI_CODE_INDEX_POSTGRES_URL` and `runtime/postgres/podman-pgvector.sh`. | Full JSON doctor. |
| Env/config selection | `auto`, URL, Postgres stopped/bad URL | `PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://bad:bad@localhost:6543/bad uv run --extra cocoindex pi-code-index --no-daemon search --json --repo <dummy> "order tax"` | Operation either falls back only where supported with `backend_fallback=true` and warning, or fails clearly without pretending no results. | Full JSON payload and stderr. |
| Direct CLI baseline | Lexical dummy repos | `uv run pi-code-index --no-daemon refresh --json --repo <dummy>`; `uv run pi-code-index --no-daemon search --json --repo <dummy> "order tax"`; `uv run pi-code-index --no-daemon context repo-map --json --include-symbols --repo <dummy>` | Refresh and search succeed; payload reports lexical/degraded capability where relevant. | Refresh/search/status JSON. |
| Direct CLI full backend | CocoIndex/Postgres dummy repos | `PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex uv run --extra cocoindex pi-code-index --no-daemon refresh --json --repo <dummy>`; repeat `status`, `doctor`, `search` | Effective backend is CocoIndex/Postgres where available; no fallback warnings; indexed counts are non-zero. | Full JSON, timing, row/index counts. |
| Semantic/context tools | CocoIndex preferred, lexical fallback recorded | `context repo-map --json --include-symbols`; `context tests --json <target>`; `context similar --json --mode hybrid --query <query>`; `context review --json <target>` | Results identify relevant files; warnings distinguish lexical heuristics from full semantic behavior. | Result ranking, warning fields, unexpected empty results. |
| Symbol tools | CocoIndex/Postgres where possible | `symbols search --json --top-k 5 "calculate_total" --repo <dummy>`; `symbols definition --json "shop.pricing.calculate_total" --repo <dummy>`; `symbols context --json --depth 2 "shop.pricing.calculate_total" --repo <dummy>` | Symbol search/definition/context find expected functions/classes or explicitly report unsupported/degraded backend. | Full JSON and expected-vs-actual symbol names. |
| Graph/impact tools | CocoIndex/Postgres where possible | `graph callers --json --depth 2 "shop.tax.compute_tax" --repo <dummy>`; `graph callees --json --depth 2 "shop.api.handle_request" --repo <dummy>`; `graph impact --json --depth 2 "shop.pricing.calculate_total" --repo <dummy>` | Graph paths include expected caller/callee chain, or fallback warnings cannot be confused with “no callers exist”. | Edge/path counts, warnings, target resolution details. |
| Daemon parity | Direct vs daemon CLI | Stop daemon, run no-daemon refresh/search/status, then run daemon-backed refresh/search/status with same env/repo. | Core backend, warning, count, and top-result behavior match except daemon metadata. | Both JSON payloads and daemon status. |
| Non-interactive Pi | Extension tool calls in lexical mode | `PI_CODE_INDEX_BACKEND=lexical pi -p --no-session --extension ./index.ts --approve "Using the repo <dummy>, run code_search for order tax and summarize filenames."` | Pi invokes the extension, reports matching filenames, and does not overstate semantic coverage. | Prompt, stdout, session disabled, tool summary. |
| Non-interactive Pi | Extension tool calls in Postgres mode | `PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex pi -p --no-session --extension ./index.ts --approve "Using the repo <dummy>, find symbol calculate_total and likely tests."` | Pi tool output agrees with direct CLI for symbol/test results. | Prompt, stdout, relevant direct CLI comparison. |
| Non-interactive Pi | Known rendering edge cases | Prompt for `find_similar_code`, graph fallback, and `repo_map --include-symbols` scenarios. | Pi summaries preserve tool hits/warnings and do not turn non-empty results into “no hits”. | stdout plus direct JSON payload comparison; link to follow-up bugs if reproduced. |
| Metrics | Overhead and precision | Time refresh/search/status with `/usr/bin/time -f '%e %M'`; record top-k precision for expected files/symbols. | Report startup time, refresh time, median query latency, peak RSS, and precision@3/5 for each backend/repo. | Raw timing logs and result rankings. |

## Command groups for the implementer

### Cheap discovery and static checks

```bash
uv run pi-code-index --help
uv run pi-code-index context --help
uv run pi-code-index symbols --help
uv run pi-code-index graph --help
uv run python -m compileall src tests
uv run --extra dev pytest
npm run typecheck
npm run test:ts
bash -n scripts/setup.sh runtime/postgres/podman-pgvector.sh examples/podman-pgvector.sh
podman compose -f runtime/postgres/compose.pgvector.yml config
```

### Fallback checks without live Postgres

```bash
PI_CODE_INDEX_BACKEND=auto env -u PI_CODE_INDEX_POSTGRES_URL -u POSTGRES_URL uv run pi-code-index --no-daemon status --json --repo <dummy-python-service>
PI_CODE_INDEX_BACKEND=lexical uv run pi-code-index --no-daemon doctor --json --repo <dummy-python-service>
PI_CODE_INDEX_BACKEND=cocoindex env -u PI_CODE_INDEX_POSTGRES_URL -u POSTGRES_URL uv run --extra cocoindex pi-code-index --no-daemon doctor --json --repo <dummy-python-service>
PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://bad:bad@localhost:6543/bad uv run --extra cocoindex pi-code-index --no-daemon search --json --repo <dummy-python-service> "order tax"
```

### Live Postgres checks

```bash
runtime/postgres/podman-pgvector.sh
scripts/setup.sh --with-cocoindex --postgres-check --skip-tests
export PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex
export PI_CODE_INDEX_BACKEND=auto
uv run --extra cocoindex pi-code-index --no-daemon doctor --json --repo <dummy-python-service>
uv run --extra cocoindex pi-code-index --no-daemon status --json --repo <dummy-python-service>
uv run --extra cocoindex pi-code-index --no-daemon refresh --json --repo <dummy-python-service>
uv run --extra cocoindex pi-code-index --no-daemon search --json --top-k 5 --repo <dummy-python-service> "where is tax calculated"
```

### Feature probes

```bash
uv run --extra cocoindex pi-code-index --no-daemon context repo-map --json --include-symbols --include-tests --repo <dummy-python-service>
uv run --extra cocoindex pi-code-index --no-daemon context tests --json --repo <dummy-python-service> src/shop/pricing.py
uv run --extra cocoindex pi-code-index --no-daemon context similar --json --mode hybrid --query "order total tax" --repo <dummy-python-service>
uv run --extra cocoindex pi-code-index --no-daemon context review --json --repo <dummy-python-service> src/shop/pricing.py
uv run --extra cocoindex pi-code-index --no-daemon symbols search --json --top-k 5 --repo <dummy-python-service> "calculate_total"
uv run --extra cocoindex pi-code-index --no-daemon symbols definition --json --repo <dummy-python-service> "shop.pricing.calculate_total"
uv run --extra cocoindex pi-code-index --no-daemon symbols context --json --depth 2 --repo <dummy-python-service> "shop.pricing.calculate_total"
uv run --extra cocoindex pi-code-index --no-daemon graph callers --json --depth 2 --repo <dummy-python-service> "shop.tax.compute_tax"
uv run --extra cocoindex pi-code-index --no-daemon graph callees --json --depth 2 --repo <dummy-python-service> "shop.api.handle_request"
uv run --extra cocoindex pi-code-index --no-daemon graph impact --json --depth 2 --repo <dummy-python-service> "shop.pricing.calculate_total"
```

### Non-interactive Pi probes

Use `--no-session` so validation does not depend on prior chat state. Keep prompts narrow and compare with the direct CLI JSON from the same repo/env.

```bash
PI_CODE_INDEX_BACKEND=lexical pi -p --no-session --extension ./index.ts --approve \
  "Use pi-code-index tools on <dummy-python-service>. Search for where tax is calculated. Return filenames and any degraded-backend warning."

PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://cocoindex:cocoindex@localhost:5432/cocoindex pi -p --no-session --extension ./index.ts --approve \
  "Use pi-code-index tools on <dummy-python-service>. Find the calculate_total symbol, its likely tests, and callers of compute_tax. Return filenames/symbol names only."

PI_CODE_INDEX_BACKEND=auto PI_CODE_INDEX_POSTGRES_URL=postgres://bad:bad@localhost:6543/bad pi -p --no-session --extension ./index.ts --approve \
  "Use pi-code-index tools on <dummy-python-service>. Try similar-code for order total tax and explain whether the backend fell back or had no hits."
```

## Metrics to record

Record raw logs in the eventual validation report rather than editing this plan.

- Podman startup: elapsed seconds until healthy, compose provider used, container image, port, existing-container vs new-container path.
- Setup overhead: `doctor`, `status`, and `refresh` wall time plus peak RSS using `/usr/bin/time -f '%e %M'`.
- Query overhead: median of three runs for direct CLI search, context similar, symbols search, and graph callers in lexical and CocoIndex modes.
- Precision: precision@3 and precision@5 for expected file hits; symbol exact-match rate; graph expected edge/path presence.
- Pi integration: whether non-interactive Pi output preserves direct tool result count, warning/fallback state, and top filenames/symbols.

## Pass/fail criteria

The validation passes only if all of these are true:

- Canonical Podman runtime starts or, on a host without Podman, static compose/shell checks pass and live validation is explicitly marked blocked by missing Podman.
- `doctor`, `status`, `refresh`, and `search` succeed in expected lexical and CocoIndex cases.
- `auto`, `lexical`, and `cocoindex` variants expose requested/effective backend, fallback state, capabilities, warnings, and setup guidance according to the step-3 contract.
- `cocoindex` required mode does not silently fall back to lexical.
- Semantic/context/symbol/graph probes either return expected dummy-repo facts or clearly mark the specific feature as unsupported/degraded.
- Non-interactive Pi sessions invoke the extension and summarize tool output without losing hits or hiding fallback warnings.
- Metrics are reported with enough raw evidence to compare lexical vs CocoIndex overhead and precision.
- No Docker commands are introduced; all container commands use Podman.

Fail the validation and file linked bd bugs for any of these:

- Empty graph/symbol/context results lack an explicit unavailable/degraded warning.
- Pi says “no hits” when direct CLI returns non-empty tool results.
- Status/doctor imply Postgres is configured from an implicit default URL.
- Runtime docs or setup output point users to `examples/` as the canonical Postgres lifecycle path.
- Postgres credential URLs are printed in daemon/status logs without redaction outside intentional copy-paste setup snippets.

## Risks

- CocoIndex/Postgres tests may be slow or environment-sensitive. Mitigate by separating static, fallback, and live-Podman evidence.
- Existing open validation bugs may reproduce during this matrix. Treat them as expected known failures only if linked in the final report with direct evidence.
- Non-interactive Pi output depends on model summarization. Mitigate by using constrained prompts and comparing stdout to direct CLI JSON.
- Dummy repos that are too small can produce ties. Keep expected assertions broad enough for ranking variance but strict enough to catch no-hit regressions.
- Daemon environment can be stale after env changes. Always run `uv run pi-code-index stop --json || true` before daemon parity and Pi Postgres checks.

## Deliverables for the implementation/review step

- Temporary dummy repo creation script or documented fixture contents.
- Raw command transcript or machine-readable logs for every matrix row.
- Final overhead/precision report summarizing lexical vs CocoIndex/Postgres behavior.
- Linked bd bugs for any newly discovered regressions.
