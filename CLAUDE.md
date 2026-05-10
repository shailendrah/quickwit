# Quickwit Development Guide

## About Quickwit

[Quickwit](https://github.com/quickwit-oss/quickwit) is a cloud-native search engine for observability data (logs, traces, metrics). Key components:

- **Tantivy + Parquet hybrid**: Full-text search via Tantivy, columnar analytics via Parquet
- **Parquet metrics pipeline** (`quickwit-parquet-engine`): DataFusion/Parquet-based analytics (under active development)
- **Three observability signals**: Metrics, traces, and logs — architectural decisions must generalize across all three

See `quickwit/CLAUDE.md` for architecture overview, crate descriptions, and build commands.

## Core Policies

- Execute given tasks fully, avoid TODOs or stubs.
- If TODOs or stubs are absolutely necessary, ensure user is made aware and they are recorded in any resulting plans, phases, or specs.
- Produce code and make decisions that are consistent across metrics, traces, and logs. Metrics is the current priority, then traces, then logs — but decisions should generalize to all three.
- Tests should be holistic: do not work around broken implementations by manipulating tests.
- Follow [CODE_STYLE.md](CODE_STYLE.md) for all coding conventions.

## Known Pitfalls (Update When Claude Misbehaves)

**Add rules here when Claude makes mistakes. This is a living document.**

| Mistake | Correct Behavior | Bug Reference |
|---------|------------------|---------------|
| Adds mock/fallback implementations | Use real dependencies, no error masking | User preference |
| Claims feature works without integration test | Run through the actual REST/gRPC stack | CLAUDE.md policy |
| Uses workarounds to avoid proper setup | **NEVER** workaround — follow the rigorous path (clone deps, fix env, run real tests) | User policy |
| Bypasses production path in tests | **MUST** test through HTTP/gRPC, not internal APIs | CLAUDE.md policy |
| Uses `Path::exists()` | Disallowed by `clippy.toml` — use fallible alternatives | clippy.toml |
| Uses `Option::is_some_and`, `is_none_or`, `xor`, `map_or`, `map_or_else` | Disallowed by `clippy.toml` — use explicit match/if-let instead | clippy.toml |
| Ignores clippy warnings | Run `cargo clippy --workspace --all-features --tests`. Fix warnings or add targeted `#[allow()]` with justification | Code quality |
| Uses `debug_assert` for user-facing validation | Use `Result` errors — debug_assert is silent in release | Code quality |
| Uses `unwrap()` in library code | Use `?` operator or proper error types | Quickwit style |
| File over 500 lines | Split into focused modules by responsibility | Code quality |
| Unnecessary `.clone()` in non-concurrent code | Return `&self` references or `Arc<T>` — cloning is OK in actor/async code for simplicity | Code quality |
| Raw String for new domain types | Prefer existing type aliases (`IndexId`, `SplitId`, `SourceId` from `quickwit-proto`) | Quickwit style |
| Shadowing variable names within a function | Avoid reusing the same variable name (see CODE_STYLE.md) | Quickwit style |
| Uses chained iterators with complex error handling | Use procedural for-loops when chaining hurts readability | Quickwit style |
| Uses `tokio::sync::Mutex` | **FORBIDDEN** — causes data corruption on cancel. Use actor model with message passing | GAP-002 |
| Uses `JoinHandle::abort()` | **FORBIDDEN** — arbitrary cancellation violates invariants. Use `CancellationToken` | GAP-002 |
| Recreates futures in `select!` loops | Use `&mut fut` to resume, not recreate — dropping loses data | GAP-002 |
| Holds locks across await points | Invariant violations on cancel. Use message passing or synchronous critical sections | GAP-002 |
| Silently swallows unexpected state | If a condition "shouldn't happen," return an error or assert — don't silently return Ok. Skipping optional/missing data is fine; pretending a bug didn't occur is not | Code quality |
| Assumes `sort_by=-field` means descending | **Quickwit inverts +/-**: bare or `+field` = Desc, `-field` = Asc. Inverse of ES/JSON:API/Django. See `quickwit/quickwit-serve/src/search_api/rest_handler.rs:93-119` | User-discovered, 2026-05-05 |
| Translates `OCI_*` env vars to `AWS_*` aliases | Reference `OCI_*` directly in compose/YAML/scripts. AWS-SDK-tuning env vars (e.g. `AWS_REQUEST_CHECKSUM_CALCULATION`) are exempt — those are read by name | User preference |

## Engineering Priority

**Safety > Performance > Developer Experience**

| Pillar | Location | Purpose |
|--------|----------|---------|
| **Code Quality** | [CODE_STYLE.md](CODE_STYLE.md) + this doc | Coding standards & reliability |

> For formal specs (TLA+, Stateright) and DST pillars, see the verification docs below. These describe the target workflow — implementation is in progress.

## Reliability Rules

```rust
// 1. Use debug_assert! to document invariants
// (Quickwit CODE_STYLE.md endorses this — helps reviewers proofread)
debug_assert!(offset >= HEADER_SIZE, "offset must include header");
debug_assert!(splits.is_sorted_by_key(|s| s.time_range.end));

// 2. Validate inputs at API boundaries (Result, not debug_assert)
if duration.as_nanos() == 0 {
    return Err(Error::InvalidParameter("duration must be positive"));
}

// 3. Define explicit limits as constants
const MAX_SEGMENT_SIZE: usize = 256 * 1024 * 1024;
if size > MAX_SEGMENT_SIZE {
    return Err(Error::LimitExceeded(...));
}

// 4. No unwrap() in library code — propagate errors
let timestamp = DateTime::from_timestamp(secs, nsecs)
    .ok_or_else(|| anyhow!("invalid timestamp: {}", nanos))?;
```

## Testing Through Production Path

**MUST NOT** claim a feature works unless tested through the actual network stack.

```bash
# 1. Start quickwit
cargo run -p quickwit-cli -- run --config ../config/quickwit.yaml

# 2. Ingest via OTLP
# (send logs/traces to localhost:4317)

# 3. Query via REST API
curl http://localhost:7280/api/v1/<index>/search -d '{"query": "*"}'
```

**Bypasses to AVOID**: Testing indexing pipeline without the HTTP/gRPC server, testing search without the REST API layer.

## Repository Layout

```
quickwit/                            # Repository root
├── quickwit/                        # Main Rust workspace (all crates live here)
│   ├── Cargo.toml                   # Workspace root
│   ├── CLAUDE.md                    # Build commands, architecture overview, crate guide
│   ├── Makefile                     # Build targets (fmt, fix, test-all, build)
│   ├── clippy.toml                  # Disallowed methods (enforced)
│   ├── rustfmt.toml                 # Nightly formatter config
│   ├── rust-toolchain.toml          # Pinned Rust toolchain
│   ├── scripts/                     # License header checks, log format checks
│   └── rest-api-tests/              # Python-based REST API integration tests
├── docs/
│   └── internals/                   # Architecture docs
│       ├── adr/                     # Architecture Decision Records
│       │   ├── README.md            # ADR index
│       │   ├── gaps/                # Design limitations from incidents
│       │   └── deviations/          # Intentional divergences from ADR intent
│       └── specs/
│           └── tla/                 # TLA+ specs for protocols and state machines
├── config/                          # Runtime YAML configs (quickwit.yaml, etc.)
├── Makefile                         # Outer orchestration (delegates to quickwit/)
└── docker-compose.yml               # Local services (localstack, postgres, kafka, jaeger, etc.)
```

## Architecture Evolution

Quickwit tracks architectural change through three lenses. See `docs/internals/adr/EVOLUTION.md` for the full process.

```
                    Architecture Evolution
                            │
       ┌────────────────────┼────────────────────┐
       ▼                    ▼                    ▼
 Characteristics          Gaps              Deviations
  (Proactive)          (Reactive)          (Pragmatic)
 "What we need"      "What we learned"   "What we accepted"
```

| Lens | Location | When to Use |
|------|----------|-------------|
| **Characteristics** | `docs/internals/adr/` | Track cloud-native requirements |
| **Gaps** | `docs/internals/adr/gaps/` | Design limitation from incident/production |
| **Deviations** | `docs/internals/adr/deviations/` | Intentional divergence from ADR intent |

## Common Commands

All Rust commands run from the `quickwit/` subdirectory.

```bash
# Build
cd quickwit && cargo build

# Run all tests (requires Docker services)
# From repo root:
make docker-compose-up
make test-all
# Or from quickwit/:
cargo nextest run --all-features --retries 5

# Run tests for a specific crate
cargo nextest run -p quickwit-indexing --all-features

# Run failpoint tests
cargo nextest run --test failpoints --features fail/failpoints

# Clippy (must pass before commit)
cargo clippy --workspace --all-features --tests

# Format (requires nightly)
cargo +nightly fmt --all

# Auto-fix clippy + format
make fix    # from quickwit/

# Check license headers
bash scripts/check_license_headers.sh

# Check log format
bash scripts/check_log_format.sh

# Spellcheck (from repo root)
make typos  # or: typos

# REST API integration tests (Python)
cd quickwit/rest-api-tests
pipenv shell && pipenv install
./run_tests.py --engine quickwit
```

## Testing Strategy

### Unit Tests
- Run fast, avoid IO when possible
- Testing private functions is encouraged
- Property-based tests (`proptest`) are welcome — narrow the search space
- Not always deterministic — proptests are fine

### Integration Tests
- `quickwit-integration-tests/`: Rust integration tests exercising the full stack
- `rest-api-tests/`: Python YAML-driven tests for Elasticsearch API compatibility

### Required for CI
- `cargo nextest run --all-features --retries 5` (with Docker services running)
- Failpoint tests: `cargo nextest run --test failpoints --features fail/failpoints`
- `RUST_MIN_STACK=67108864` is set for test runs (64MB stack)

## Docker Services for Testing

```bash
# Start all services (localstack, postgres, kafka, jaeger, etc.)
make docker-compose-up

# Start specific services
make docker-compose-up DOCKER_SERVICES='jaeger,localstack'

# Tear down
make docker-compose-down
```

Environment variables set during test-all:
- `AWS_ACCESS_KEY_ID=ignored`, `AWS_SECRET_ACCESS_KEY=ignored`
- `QW_S3_ENDPOINT=http://localhost:4566` (localstack)
- `QW_S3_FORCE_PATH_STYLE_ACCESS=1`
- `QW_TEST_DATABASE_URL=postgres://quickwit-dev:quickwit-dev@localhost:5432/quickwit-metastore-dev`

## Key Entry Points

| Port | Protocol | Purpose |
|------|----------|---------|
| 7280 | HTTP | Quickwit REST API |
| 7281 | gRPC | Quickwit gRPC services |
| 4317 | gRPC | OTLP ingest |

## Checklist Before Committing

**MUST** (required for merge):
- [ ] `cargo clippy --workspace --all-features --tests` passes with no warnings
- [ ] `cargo +nightly fmt --all -- --check` passes (run `cargo +nightly fmt --all` to fix; applies to **all** changed `.rs` files including tests — CI checks every file, not just lib code)
- [ ] `debug_assert!` for non-obvious invariants
- [ ] No `unwrap()` in library code
- [ ] No silent error ignoring (`let _ =`)
- [ ] New files under 500 lines (split by responsibility if larger)
- [ ] No unnecessary `.clone()` (OK in actor/async code for clarity)
- [ ] Tests through production path (HTTP/gRPC)
- [ ] License headers present (run `bash quickwit/scripts/check_license_headers.sh` — every `.rs`, `.proto`, and `.py` file needs the Apache 2.0 header)
- [ ] Log format correct (run `bash quickwit/scripts/check_log_format.sh`)
- [ ] `typos` passes (spellcheck)
- [ ] `cargo machete` passes (no unused dependencies in Cargo.toml)
- [ ] `cargo doc --no-deps` passes (each PR must compile independently, not just the final stack)
- [ ] Tests pass: `cargo nextest run --all-features`

**SHOULD** (expected unless justified):
- [ ] Functions under 70 lines
- [ ] Explanatory variables for complex expressions
- [ ] Documentation explains "why"
- [ ] Integration test for new API endpoints

## Detailed Documentation

| Topic | Location |
|-------|----------|
| Code style (Quickwit) | [CODE_STYLE.md](CODE_STYLE.md) |
| Rust style patterns | [docs/internals/RUST_STYLE.md](docs/internals/RUST_STYLE.md) |
| Verification & DST | [docs/internals/VERIFICATION.md](docs/internals/VERIFICATION.md) |
| Verification philosophy | [docs/internals/VERIFICATION_STACK.md](docs/internals/VERIFICATION_STACK.md) |
| Simulation workflow | [docs/internals/SIMULATION_FIRST_WORKFLOW.md](docs/internals/SIMULATION_FIRST_WORKFLOW.md) |
| Benchmarking | [docs/internals/BENCHMARKING.md](docs/internals/BENCHMARKING.md) |
| Contributing guide | [CONTRIBUTING.md](CONTRIBUTING.md) |
| ADR index | [docs/internals/adr/README.md](docs/internals/adr/README.md) |
| Architecture evolution | [docs/internals/adr/EVOLUTION.md](docs/internals/adr/EVOLUTION.md) |
| Compaction architecture | [docs/internals/compaction-architecture.md](docs/internals/compaction-architecture.md) |
| Tantivy + Parquet design | [docs/internals/tantivy-parquet-architecture.md](docs/internals/tantivy-parquet-architecture.md) |
| Locality compaction | [docs/internals/locality-compaction/](docs/internals/locality-compaction/) |
| Runtime config | [config/quickwit.yaml](config/quickwit.yaml) |

## References

- [Quickwit](https://github.com/quickwit-oss/quickwit)
- [Tantivy search engine](https://github.com/quickwit-oss/tantivy)
- [Apache DataFusion](https://datafusion.apache.org/)

## Local Demo Setup (User-Local Work, Not Upstream)

The `src/` directory at the repo root and several auxiliary additions are **user-local exploratory work**, not part of upstream Quickwit. They exist to demonstrate Quickwit end-to-end against the user's own infrastructure (OCI Object Storage, host Postgres). Future sessions should treat this as the running state of the demo, not as code to refactor or merge upstream.

### Architecture in one diagram

```
   Host (skmishra's laptop)                              OCI Object Storage
                                                        bucket: polaris-iceberg
   ┌─────────────────────────┐
   │ pgcompose stack         │      ┌── quickwit/articles/part-0000.jsonl.gz   ◄─── faker (gen)
   │ postgres:17 :5432       │      │   quickwit/app_logs/part-0000.jsonl.gz   ◄───
   │ db: quickwit_metastore  │      │
   │ user/pwd: skmishra      │      │   quickwit-indexes/articles/<uuid>.split ◄─── indexer (commit)
   └────────────┬────────────┘      │   quickwit-indexes/app_logs/<uuid>.split ◄───
                │ host.docker.internal:5432             ▲
                ▼                                       │ S3-compat (vhcompat endpoint)
   ┌─────────────────────────┐                          │
   │ docker-compose          │      ┌───────────────────┘
   │ profile=quickwit        │      │
   │ container: quickwit     │──────┘  reads NDJSON.gz at ingest, writes splits at commit
   │ image: quickwit/quickwit:latest                                                            
   │ ports: 7280 (REST/UI), 7281 (gRPC)
   │ data_dir: /quickwit/qwdata  ←—  host-mounted at ./qwdata/  (WAL, hotcache, scratch)
   │ config:   /quickwit/config/quickwit-node.yaml  ←— host-mounted ./config/
   └─────────────────────────┘
```

### Files added for the demo

| Path | Purpose |
|------|---------|
| `src/generate_quickwit_data.py` | Faker → gzipped NDJSON → OCI. Two datasets: `articles` (2k docs) + `app_logs` (20k docs spread over 14d) |
| `src/ingest_to_quickwit.py` | boto3 streams NDJSON.gz from OCI → POSTs to Quickwit ingest API in 1000-line batches; force-commits last batch |
| `src/index-articles.yaml` | Doc mapping for the `articles` index (full-text title/body, faceted tags/category, `published_at` timestamp) |
| `src/index-app-logs.yaml` | Doc mapping for `app_logs` (timestamp, level/service facets, IP type, JSON attributes, latency) |
| `src/test_quickwit.sh` | 11-section smoke-test / demo query suite — version, counts, full-text, time pruning, aggregations |
| `src/README.md` | User-facing usage notes for the generator |
| `config/quickwit-node.yaml` | Runtime config: `listen_address: 0.0.0.0`, `data_dir: /quickwit/qwdata`, `default_index_root_uri: s3://polaris-iceberg/quickwit-indexes`, `storage.s3` block pointed at OCI vhcompat endpoint with `${OCI_*}` env interpolation |
| `qwdata/` (host) | Bind-mounted to `/quickwit/qwdata` in the container. WAL, hotcache, indexer scratch. Splits do not live here — they go to OCI |
| `docker-compose.yml` | Added a `quickwit` service under the `quickwit` profile, with host-mount + `host.docker.internal` for Postgres + OCI env passthrough |
| `Makefile` | Added `quickwit-up`, `quickwit-down`, `quickwit-logs`, `quickwit-create-indexes`, `quickwit-ingest`, `quickwit-test` targets |

### Decisions baked in (do not re-litigate without a reason)

1. **Splits go to OCI**, not local disk. `default_index_root_uri: s3://polaris-iceberg/quickwit-indexes`. The host `qwdata/` is for transient indexer state only.
2. **Metastore is host Postgres**, not the dockerized `postgres` profile in `docker-compose.yml`. The user runs Postgres separately at `~/repo/databases/pgcompose.yml` with db `quickwit_metastore`. The Quickwit container reaches it via `host.docker.internal:5432`.
3. **OCI is reached via the `s3` storage backend** (Quickwit has no native OCI backend; OCI Object Storage exposes an S3-compatible API). The YAML field is `storage.s3` but the endpoint points at OCI's vhcompat host.
4. **Env var names are `OCI_*` throughout** — no `AWS_*` aliases. The two `AWS_REQUEST_CHECKSUM_CALCULATION` / `AWS_RESPONSE_CHECKSUM_VALIDATION` entries are SDK-tuning knobs (read by name), required because OCI rejects aws-chunked PutObject payloads.
5. **NDJSON, not Parquet**, for the source data. Quickwit has no Parquet ingestion path; one JSON object per line is the native format. The `quickwit-parquet-engine` crate is an internal metrics-pipeline detail with a fixed schema, not a general Parquet reader.
6. **Container image is upstream `quickwit/quickwit:latest`** (currently 0.8.2-nightly). Not built from this source tree. Fine for a demo; switch to a local build only if testing source changes.

### Resuming the demo from cold

```bash
# Prerequisites already done once and persistent:
#   - Postgres DB `quickwit_metastore` exists in pgcompose stack
#   - 53 Quickwit migrations have run into it
#   - NDJSON.gz files are in s3://polaris-iceberg/quickwit/{articles,app_logs}/
#   - Indexes `articles` and `app_logs` exist in the metastore
#   - Splits exist in s3://polaris-iceberg/quickwit-indexes/

cd /Users/skmishra/repo/frameworks/quickwit
make quickwit-up
make quickwit-test                       # runs the 11-section query suite
```

If the host Postgres or pgcompose stack has been torn down:

```bash
# In ~/repo/databases:
docker compose -f pgcompose.yml up -d
PGPASSWORD=skmishra createdb -h localhost -U skmishra quickwit_metastore  # if missing
# Then the standard sequence:
cd /Users/skmishra/repo/frameworks/quickwit
make quickwit-up                          # 53 migrations run on first start
make quickwit-create-indexes              # only if indexes are gone
make quickwit-ingest                      # only if no splits in OCI
make quickwit-test
```

### Open questions / where we stopped

- **No source attached to the indexes.** Ingest is one-shot via the REST API. If we want continuous ingestion of new NDJSON files dropped into OCI, we'd need to wire up a Quickwit `file` source pointing at the OCI prefix (or an SQS source if OCI emitted S3-compat events, which it does not natively). Not done.
- **Latency outliers.** The `latency_ms:>1000` query returns 0 hits because `expovariate(1/50)` produces P(>1s) ≈ e⁻²⁰ — essentially never. If we want some long-tail entries to query against, sprinkle ~0.5% `random.uniform(1000, 5000)` outliers into `gen_log()` and re-ingest.
- **Oracle metastore (orthogonal project).** User's polaris fork uses Oracle DB. They considered porting Quickwit's Postgres metastore to Oracle but explicitly **deferred** ("It's an orthogonal project. Let's stay with postgres."). If they bring it up again, the scope estimate (~5,300 lines of Rust + 53 migrations + leaving the SQLx ecosystem) is in the conversation history.

### Project-relevant memories

The following memory files persist across sessions and should be consulted when relevant:

- `python_setup.md` — central venv at `frameworks/.venv`, Python 3.14, plain `python`/`pip`
- `feedback_oci_env_names.md` — use `OCI_*` directly, do not translate to `AWS_*`
- `quickwit_sort_by_semantics.md` — the inverted `+/-` sort_by gotcha

## Forward Plan: Distributed Parquet → Quickwit Pipeline on OKE

The demo above is single-node and ingests via REST. The real target is a distributed pipeline on OKE (Oracle Kubernetes) that indexes Parquet files written by upstream applications. This section captures the design discussion so a future session can resume cold.

### Problem statement

Upstream applications land Parquet files in OCI Object Storage. Schema looks like FHIR `DocumentReference` (one row = one resource, the actual document is JSON inside one column):

| Column | Notes |
|---|---|
| `DOCUMENTREFERENCE_SO_ID` | source-system row id |
| `DOCUMENTREFERENCE_VALUE` | JSON-typed string column — the actual FHIR resource |
| `DOCUMENTREFERENCE_PATIENT_ID` | lifted reference for sharding/joins |
| `DOCUMENTREFERENCE_ENCOUNTER_ID` | lifted reference for sharding/joins |
| `DOCUMENTREFERENCE_WATERMARK` | upstream incremental marker (likely timestamp) |

Goal: searchable in Quickwit, running on OKE, incremental forever (no full rebuild ever).

### Architecture (decided)

```
Parquet (OCI) ──[Spark on OKE: SQL transform + JSONL emit]──► NDJSON.gz (OCI) ──[Quickwit file source]──► splits (OCI)
                  Spark Operator                                                   Quickwit indexer StatefulSet on OKE
                                                                                   (KubeRay/Ray NOT needed)
```

Two operators on OKE: **Spark Operator** for the transform stage, and Quickwit's own indexer pods for ingest. **No Ray in this pipeline.**

### Why Spark for the transform stage (and not Ray)

| Reason | Detail |
|---|---|
| Native Parquet reader | JVM vectorized reader is the gold standard; predicate pushdown + partition discovery free |
| SQL surface | Need `from_json`/`parse_json` on `VALUE`, projection, watermark filter — Spark SQL fits exactly |
| `df.write.json(...)` | One-line NDJSON emit with gzip compression |
| Ray would underperform | Ray Data's Parquet path uses PyArrow — workable but weaker than Spark's columnar reader, and we'd be writing more glue |
| Ray would only win if | Conversion grew non-trivial Python (e.g., an NLP model on `VALUE` to extract entities). Pure transcode → Spark |

### Why Quickwit's file source solves "what's new" — no external orchestrator needed

The Quickwit metastore (Postgres) stores a `SourceCheckpoint` per `(index_uid, source_id)` that maps **file URI → byte position** (not timestamp). See `quickwit/quickwit-indexing/src/source/file_source.rs:156-165`.

```
SourceCheckpoint {
  "s3://.../part-0000.jsonl.gz" → Position::Eof,
  "s3://.../part-0001.jsonl.gz" → Position::Eof,
  "s3://.../part-0002.jsonl.gz" → Position::offset(45_231_104),  // mid-file resume
}
```

- Updated **atomically with split publish** (same metastore txn) — exactly-once at commit boundary.
- Mid-file resume works on gzip too (`test_file_source_resume_from_checkpoint(gzip: true)` confirms it).
- URI-as-identity (not timestamp) is robust against late arrivals, clock skew, partial files, and re-runs.

### Spark SQL job (sketch)

```python
spark.read.parquet("s3a://.../documentreference/dt=2026-05-06/") \
     .createOrReplaceTempView("docref")

result = spark.sql("""
  SELECT
    DOCUMENTREFERENCE_SO_ID         AS so_id,
    DOCUMENTREFERENCE_PATIENT_ID    AS patient_id,
    DOCUMENTREFERENCE_ENCOUNTER_ID  AS encounter_id,
    DOCUMENTREFERENCE_WATERMARK     AS watermark_ts,
    from_json(DOCUMENTREFERENCE_VALUE, fhir_doc_ref_schema) AS value
  FROM docref
  WHERE DOCUMENTREFERENCE_WATERMARK > '<last_run_max>'
""")

(result
   .repartition(N)                                # N ≈ ceil(total_size / 300MB)
   .write
   .mode("overwrite")
   .option("compression", "gzip")
   .json("s3a://.../quickwit/documentreference/dt=2026-05-06/"))
```

### Three open decisions to resolve before implementation

1. **How to parse `DOCUMENTREFERENCE_VALUE`** (JSON-string column):
   - `from_json(col, schema)` — needs an explicit FHIR schema (~30 nested fields for DocumentReference). Cleanest output, painful to author.
   - `parse_json(col)` — Spark 3.4+ VARIANT type, schema-free, round-trips JSON. **Modern answer if our Spark version supports it.**
   - Leave as escaped string — zero work but loses `value.status:current` query capability in Quickwit. Not recommended.
2. **Output file sizing**: target **~200–500 MB per `.jsonl.gz`**. Spark's default (one file per output partition) often produces too-many tiny files. Use `repartition(N)`. Wrong sizing → either thousands of checkpoint rows (too small) or no indexer parallelism within a file (too large).
3. **Incremental input strategy** — pick one and stick with it:
   - **Date-partitioned input** (`dt=YYYY-MM-DD/`): cron daily on the latest partition. Simplest. Composes best with Quickwit (output prefix ↔ ingest slice). Idempotent re-runs via `mode("overwrite")` on the dated output prefix.
   - **Watermark filter**: `WHERE WATERMARK > last_max`. Store `last_max` somewhere small (a tiny manifest object, or read max from prior output).
   - **File manifest**: diff input URIs against a "done" set. Most defensive against late arrivals, most ceremony.

### Pipeline variants considered and rejected

| Variant | Why rejected |
|---|---|
| One-stage push: Ray reads Parquet, POSTs to `/ingest` directly | Re-introduces dedup burden the file source already solves for free. Ray adds nothing here. |
| Quickwit reads Parquet directly | Not available. `quickwit-parquet-engine` is metrics-only with a fixed schema, not a general Parquet reader (per Decision #5 of the demo setup above). |

### Questions still open (where we stopped)

- Is `DOCUMENTREFERENCE_WATERMARK` a timestamp? If yes → natural `timestamp_field` for the index (enables Quickwit's time-pruning, its killer feature).
- Are upstream Parquet files immutable (append-only) or do they get rewritten? Determines whether file URI is a stable identity for any converter-side checkpoint.
- Spark version available on the target OKE cluster — does it have VARIANT (3.4+)? This decides Decision #1 above.
- Quickwit doc mapping for the parsed `value` field — `json` field type with `expand_dots: true` is the likely shape, but worth a focused pass.
- OKE topology specifics: Spark Operator deployment, namespace separation, how Spark and Quickwit pods authenticate to OCI (instance principal vs. user creds), shared Postgres metastore access pattern.

### Resuming this discussion

Highest-leverage starting point is **Decision #1 (VALUE parsing)** — it determines the Quickwit doc mapping, the query capabilities, and whether we need to author a FHIR schema. Pick that, and the rest cascades.
