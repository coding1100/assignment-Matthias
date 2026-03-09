# SOLUTION_NOTES

## 1) What I changed

### Core pipeline hardening
- Added centralized runtime configuration in `src/config.py`.
- Added multi-turn conversation module in `src/conversation.py`:
  - session state persistence
  - follow-up intent detection
  - safe SQL follow-up rewrite support
  - ambiguity handling
- Added observability utilities in `src/observability.py`:
  - structured JSON logs
  - request/trace IDs
  - stage timing instrumentation
  - in-memory counters/latency metrics
- Added SQL and quality validation framework in `src/validation.py`:
  - SELECT-only policy
  - single-statement enforcement
  - blocked DML/DDL/comment tokens
  - schema/table validation
  - SQLite parse-plan checks (`EXPLAIN QUERY PLAN`)
  - row-shape and answer-text quality checks
  - SQLite compatibility sanitization for unsupported functions

### LLM client reliability + efficiency
- Implemented real token counting from OpenRouter usage metadata in `src/llm_client.py`.
- Hardened response parsing to handle non-trivial content formats.
- Added bounded retry with backoff for transient API failures.
- Added structured JSON response format for SQL generation.
- Tuned SQL generation prompt to emphasize SQLite-valid output and strict JSON.

### Pipeline behavior and status determinism
- Updated `src/pipeline.py` to:
  - use trace-wrapped stages
  - classify statuses deterministically (`success`, `unanswerable`, `invalid_sql`, `error`)
  - short-circuit destructive user intent as `invalid_sql`
  - short-circuit clearly unsupported domains (e.g., zodiac) as `unanswerable`
  - run one safe SQL regeneration attempt when validation indicates recoverable errors
  - enforce query-only execution and statement timeout during SQLite execution
  - best-effort bootstrap DB from CSV if DB is missing and CSV exists
  - support optional `session_id` in `run()` for multi-turn behavior
  - persist and read session history with bounded in-memory store
  - support follow-up explain/refine flows without breaking single-turn contract

### Benchmark correctness
- Fixed `scripts/benchmark.py` bug (`result["status"]` -> `result.status`).
- Added benchmark reporting for:
  - average tokens/request
  - average LLM calls/request

### Tests
- Added unit tests:
  - `tests/test_validation.py`
  - `tests/test_llm_client_unit.py`
  - `tests/test_pipeline_unit.py`
  - `tests/test_conversation_unit.py`
- Kept public tests untouched (`tests/test_public.py`).

## 2) Why I changed it

- The baseline lacked required token accounting, SQL safety, and observability.
- Production readiness needed explicit guardrails for:
  - safety (block unsafe SQL)
  - correctness (schema-aware validation)
  - resilience (API retries, deterministic status mapping)
  - operability (stage-level logs/metrics/tracing)
- I avoided prompt-specific hardcoded query templates as a final strategy because they overfit tests and are not production-grade.

## 3) Measured impact (before/after)

All numbers below are from actual runs on this machine with real OpenRouter calls.

### Checkpoint comparison (same benchmark command: `python scripts/benchmark.py --runs 3`)

#### Before reliability hardening checkpoint
- Success rate: `88.89 %`
- Avg latency: `3192.59 ms`
- p50 latency: `3213.19 ms`
- p95 latency: `3943.65 ms`
- Avg tokens/request: `528.64`
- Avg LLM calls/request: `2.00`

#### Final current solution
- Success rate: `97.22 %`
- Avg latency: `3475.72 ms`
- p50 latency: `3028.18 ms`
- p95 latency: `5279.91 ms`
- Avg tokens/request: `624.64`
- Avg LLM calls/request: `2.06`

### Multi-turn latency polish (focused benchmark, 5 sessions)

Scenario per session:
1) "How does gaming addiction level vary between genders?"
2) "What about males specifically?"
3) "Can you explain the highest value?"

Before local explain fast path:
- Follow-up explain avg latency: `1326.02 ms`
- Follow-up explain p95 latency: `1617.61 ms`

After local explain fast path:
- Follow-up explain avg latency: `0.95 ms`
- Follow-up explain p95 latency: `1.16 ms`

All 5/5 explain follow-up turns remained `success`.

### Test results
- Public integration tests:
  - `python -m unittest discover -s tests -p "test_public.py"`
  - Result: `Ran 5 tests` -> `OK`
- Full test suite:
  - `python -m unittest discover -s tests -p "test_*.py"`
  - Result: `Ran 23 tests` -> `OK`

## 4) Tradeoffs and next steps

### Tradeoffs
- Reliability improvements (retry + stricter validation + recovery logic) improved success rate, but increased latency and token usage in final measurements.
- External LLM variability/network conditions still materially impact tail latency and occasional benchmark variance.
- Multi-turn session persistence is currently in-memory and single-process.

### Next steps
1. Add response caching for repeated prompt patterns to reduce token/latency cost.
2. Add adaptive retry policy based on error class and stage criticality.
3. Add external metrics export (Prometheus/OpenTelemetry) for production monitoring.
4. Add offline SQL quality regression tests with deterministic mock model outputs for CI stability.
