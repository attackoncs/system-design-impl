# Contributing and engineering checks

Each implemented chapter is an independently installable project. Preserve its
public API, examples and documented consistency/durability guarantees when refactoring.

## Current status and chapter review workflow

Start with the [review coverage inventory](docs/review-coverage_cn.md) and the
chapter's own review, requirements, design and tasks. The inventory separates
current evidence from historical runs and lists remaining deployment limits.
[DEEP_REVIEW_PROGRESS.md](DEEP_REVIEW_PROGRESS.md) records the review chronology;
[ENGINEERING_REVIEW.md](ENGINEERING_REVIEW.md) retains the earlier repository
audit and its dated results.

Read the relevant chapter in `System Design Interview.md` before changing its
implementation. Compare actual upstream source files, recording the repository,
fixed revision, functions read and scope; distinguish source inspection from
installing or testing that upstream. Use CodeGraph to trace the relevant entry
points and state owners, checking index freshness. Graph relations supplement
source contracts and executable tests; missing edges do not prove no impact.
Make small changes for confirmed gaps, preserve unrelated work, and verify the
changed behavior with regression tests and appropriate runnable examples.

## Organizing code

- Keep domain decisions separate from HTTP adapters, persistence and worker
  scheduling. Extract cohesive behavior rather than splitting by line count.
- Inject storage, clocks and external-service clients where multiple callers or
  deterministic tests need them. Depend on explicit interfaces, not private
  attribute detection.
- Share code within a project first. Cross-chapter runtime packages require a
  stable contract and an installation strategy; copying superficially similar
  persistence code into one helper can change correctness guarantees.
- Name functions after behavior, document units and invariants, validate inputs
  at boundaries, and log an exception at the layer that handles it.

## Concurrency changes

State the protected invariant, ownership and lock order. Keep network I/O and
slow computation outside publication locks; preserve atomicity around financial
state, idempotency, sequencing and leases. Use immutable snapshots for read-heavy
state where replacement cost is acceptable. Bound queues, executor capacity and
network deadlines; avoid synchronous I/O on an async event loop.

Regression tests should force the relevant interleaving with events/barriers,
propagate worker exceptions, and use bounded waits. Measure realistic workloads
before claiming throughput improvements. Record latency distribution, errors,
concurrency, data size, hardware/runtime and durability settings.

## Checks

Run `python tools/audit_code.py --output .test-tmp/engineering-audit.json` from
the repository root. Syntax failures are blocking; size/duplication/exception signals require
review, not automatic rewriting.

Run tests from the changed chapter, with the test dependencies documented in its
README installed:

```sh
python -W error -m pytest tests -q -p no:cacheprovider
```

Use `python -m pip install -e '.[dev]'` only when that project declares a `dev`
extra; other projects use their documented setup. Keep real-service
integration tests explicit and separately report skipped/unavailable tests.
Avoid combining different chapters' test trees in one Python process: repeated
module names and different import roots can hide defects.

Use the [coverage inventory](docs/review-coverage_cn.md) and linked chapter
records for completed fixes, measurements and outstanding work. Preserve dated
baselines instead of presenting old counts as a newly rerun repository total.
Report skips and separately completed cases without counting overlaps twice.
Before committing, inspect `git diff --check`
and the actual diff; preserve unrelated local changes and keep credentials and
runtime artifacts out of commits.

## Synchronizing documentation

- Update the changed chapter's English/Chinese README, requirements, design and
  tasks wherever behavior, guarantees, commands or completion status changed.
  Record chapter coverage, upstream-source comparisons and actual verification
  in its review/report files; keep new dated outputs separate from old samples.
- Keep both root READMEs, the coverage inventory, progress record and GitHub
  index consistent when chapter status changes. Add current summaries to
  historical reports without rewriting their original measurements. Update
  this guide when the contribution or validation commands change.
- Update the authored reading guide when its example, call path, tests or
  diagram semantics change, then regenerate and check the published guides.
  Book translations describe the source text; implementation status belongs in
  navigation and review documents, rather than rewriting the translated book.

## Code reading guides

The Chinese learning guides start with a concrete example, then explain state
ownership, architecture, ordered source reading stops, annotated code and failure
behavior. Edit the authored Markdown in `tools/learning_guides/<project>.md` when
changing the example's entry points, state transitions or guarantees. Keep the
example and diagram semantics in sync with the implementation and cited tests.

```sh
python tools/build_learning_guides.py
python tools/build_learning_guides.py --check
```

The builder resolves source/test symbols and excerpts without importing chapter
packages. Its check validates references, excerpt ranges, links and publication
freshness; it does not prove the diagrams' runtime semantics or run business tests.
Review diagram layout in a Mermaid-capable viewer after editing the diagrams.
The `file` and `symbol` macros already expand to complete Markdown links; do not
put a macro inside another Markdown link destination.

## Repository-wide regression commands

After installing the selected chapters' dependencies in the active interpreter,
run from the repository root:

```sh
python tools/run_project_tests.py --jobs 2 --output .test-tmp/review
python tools/run_project_tests.py rate-limiter key-value-store --jobs 2 --output .test-tmp/review
python -m pytest tools/tests -q -p no:cacheprovider
```

The runner discovers all 27 chapter suites. Each runs in a separate interpreter
with its own source path and a short temporary path (important on Windows).
It does not add a warnings-as-errors flag to chapter subprocesses; set
`PYTHONWARNINGS=error` in its environment when that validation mode is intended.
`results.json` and per-chapter logs contain the evidence. A selective rerun
replaces selected entries and retains other entries from that output directory;
use a new output directory when a completely fresh report is needed. Do not run
two runners against the same output directory or chapter simultaneously.
For standalone pytest on restricted Windows setups, set `TEMP` and `TMP` to an
existing writable directory under `.test-tmp`.

Real-service tests retain their documented prerequisites. In particular, the
leaderboard lab requires `LB_REDIS_BINARY` or `LB_WSL_REDIS_BINARY`; optional
`LB_WSL_DISTRO` and `LB_WSL_REDIS_LIB` select its WSL environment. Video requires
ffmpeg/ffprobe for media tests; `VIDEO_TEST_DATABASE_DSN` enables PostgreSQL tests.
The chat Sentinel test needs its documented `CHAT_TEST_REDIS_LAB` configuration.

For chat and nearby-friends Redis coverage:

```sh
python tools/run_redis_checks.py
python tools/run_redis_checks.py chat-system
```

This starts and stops an owned loopback Redis lab. Set `NEARBY_REDIS_BINARY`, or
`NEARBY_WSL_REDIS_BINARY` with optional `NEARBY_WSL_DISTRO` and
`NEARBY_WSL_REDIS_LIB`. It supplies `CHAT_TEST_REDIS_URL` and writes its results
to `scale-from-zero/.runtime/review-redis`. It does not enable Sentinel tests.

Run Ruff against each chapter's `src` directory, excluding generated protobuf:

```sh
ruff check --select E9,F --exclude '*_pb2.py,*_pb2_grpc.py' path/to/chapter/src
ruff format --check --exclude '*_pb2.py,*_pb2_grpc.py' path/to/chapter/src
ruff check --select E9,F tools/audit_code.py tools/run_project_tests.py tools/run_redis_checks.py tools/tests
```

The repository resource-contract tests complement chapter suites: they
exercise partial HTTP headers, SQLite setup failures and independent cache
hits during slow loads. They require the relevant chapter dependencies too.
