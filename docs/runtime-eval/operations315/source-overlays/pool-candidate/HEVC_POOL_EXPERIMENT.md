# Candidate-only whole HTTP HEVC pool

Predeclared 2026-10-13 before this candidate's performance series. This backend
is a copy of `/private/tmp/altt-operation-eval-20261013/optimized-final/backend`.
The working repository and the optimized-final snapshot are unchanged.

Only full HEVC filter evaluation in `list_release_groups` uses the candidate
adapter `app.services.hevc_parallel.find_unpaired_avc_parallel`. SQL loading,
file-key loading, filter/sort projections, page badge pairing, Jinja and the HTTP
response remain the same. There is no data cache, new dependency or runtime flag.

The request owner constructs frozen tuple/JSON DTO snapshots after SQL/file-key
loading. No Session, ORM object or shared mutable JSON reaches a worker. One
captured UTC clock is passed to every chunk. The adapter takes the dynamic
`hevc_pairing.utcnow` clock, preserving the benchmark harness's early fixed-clock
override and the existing clock regression tests. SLA/ignored/history/file-key
semantics and full output order are retained through the existing tested helper.

The process owns a guarded, lazily created `ThreadPoolExecutor(max_workers=4)`.
Its construction and first thread submissions occur inside the first non-empty
HTTP request. Warm requests reuse this executor; each request creates a fresh
snapshot. Atexit joins the executor; tests may shut it down only when quiescent.

Chunk policy is **max(1, ceil(release_count / 4)) releases per task** for all three
runtime modes. Hence 128 releases make four chunks of32 and 1024 make four
chunks of256. Chunks are contiguous in first-release order, and a release's full
history is never split. The formula was selected from the independent per-release
boundary and four-worker limit, not from intermediate timings. Each request
submits at most four tasks; the executor has at most four worker threads.

The whole HTTP timer must include snapshot/JSON copying, lazy pool/thread setup
for the first request, submissions, compute, ordered merge, SQL, sort, Jinja and
the ready response. Correctness/status/regex/SHA comparisons occur outside the
timer. Import time is excluded. The same fixed-clock full HTML must match the
optimized sequential variant in regular/t-GIL-on/t-GIL-off modes before timing.

`pool_state()` is harness-only evidence outside the request timer: pool_created,
created_count, workers, actual_thread_count and the fixed chunk_policy string.
It holds no archive data. Expected before first request: no pool/zero actual threads;
after the first request: created_count +1 and1–4 threads; after warm requests:
the same created_count and at most four threads.

Candidate correctness tests copy the existing62 snapshot/evaluation cases to
`tests/test_hevc_pool_candidate.py` against the actual candidate module, and add
lazy creation/reuse/clock, empty-input and predeclared chunk-policy regressions.
No tests, compileall or timings were run while supplement benchmarking occupied
the clean measurement window. Validation and timing wait for explicit free slot.
