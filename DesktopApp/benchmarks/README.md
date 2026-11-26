# Matrix transport benchmark

Run from `DesktopApp` with the pinned release Python, PyQt5 and an external
kubectl configured in the application preferences or PATH. No real clusters
are contacted: the benchmark generates a private local TLS endpoint,
synthetic kubeconfig and disposable UserData. Its certificate requires the
existing `cryptography` dependency. The selected kubectl is run as a normal
proxy against that endpoint; its implementation is not modified.

```powershell
./.venv-qt5/Scripts/python.exe benchmarks/matrix_transports.py --output reports/new-matrix-run --repetitions 2
```

The output directory must not exist. Each of the 51 thread settings is applied
through `configure_cluster_threads` in both transports, with two repetitions.
Allow about 13 minutes at the normal 2 requests/sec limit. Do not run builds
or tests concurrently. CSV and request traces are flushed after every trial;
`COMPLETE` is written only when all invariants pass. Failed runs are preserved.

The dataset contains 10 namespaces × 5 Deployment × 4 Pod. Deployment names
repeat across namespaces, so the real matrix contains five rows, ten scope
columns and 200 Pod markers. It is rendered with Qt offscreen at 1450×900.
Timing separates network collection from a fresh model build, paint and image
capture. This is not native Windows display latency, FPS or a production
network benchmark. Intermediate scope-progress display is excluded.

Optional `--counts 0,1,2 --repetitions 1` performs a quick pilot; `--delay-ms`
changes the synthetic server delay. Keep the defaults for the published
0–50 comparison. Zero means sequential work on the background collector,
without a dedicated I/O pool. One means a dedicated serial I/O worker.
Thread count never removes request-rate and byte limits.

For the publication plots, use an isolated plotting environment; do not add
matplotlib or its dependencies to the application's release environment/lock:

```powershell
./.venv-qt5/Scripts/python.exe -m venv reports/plot-env
./reports/plot-env/Scripts/python.exe -m pip install --only-binary=:all: matplotlib==3.10.8
./reports/plot-env/Scripts/python.exe benchmarks/plot_matrix_transports.py reports/new-matrix-run
```

The plotter expects the full default protocol: 51 points per mode, two
repetitions and a 50 ms endpoint delay. It writes PNG, SVG, median JSON and
a Markdown report alongside raw measurements. Source hashes, runtime
versions, conditions and cold proxy startup time are recorded in
`environment.json`. The TLS fixture's key files, kubeconfig and UserData are
temporary and are removed on exit; no login is attempted.

## Real laboratory load

`lab_load.py` targets only the configured `lab1`/`lab2` laboratory addresses;
the default is lab1. It copies an existing kubeconfig into disposable UserData,
never attempts password login, creates ten uniquely labelled namespaces with
five Deployment and four pause Pod each, and removes only that run's namespaces
using UID preconditions. Existing workloads remain in the aggregate API lists.
All 200 Pod objects must exist; their Running/Pending counts are recorded.

```powershell
./.venv-qt5/Scripts/python.exe benchmarks/lab_load.py --output reports/lab-load-2026-11-27/real-run
./reports/https-benchmark-2026-11-26/plot-env/Scripts/python.exe benchmarks/plot_lab_load.py reports/lab-load-2026-11-27/real-run --output reports/lab-load-2026-11-27/published/measured
```

Default protocol: 1,3,…,25 I/O workers × 2,4,6,8,10 requests/sec × both transports
× two repetitions (260 matrix updates). The second repetition reverses thread
order to reduce time drift. The limiter and shared endpoint bucket persist
between updates. Each rate has a warmup; every update invalidates the data cache.
Run without other benchmarks/builds/tests or other manual Quaber refreshes.
The benchmark has an isolated coordinator database which persists for its
entire run; other API clients are background traffic outside its QPS counters.

HTTP timing starts inside the physical opener after admission, and ends at the
last body read before JSON decoding. CSV separates matrix collection, model/
paint, admission wait and HTTP response. Reported API time includes network and
TLS/proxy overhead; it approximates client-perceived response, not server-only
execution. Actual achieved QPS is reported alongside the configured ceiling.
Ten QPS is not evidence of cluster overload by itself.

Use `--kubeconfig <path>` only for an existing static-token/certificate context
matching the configured lab server. Expired credentials require normal login in
Quaber before starting. `cleanup-state.json` records owned namespace UIDs during
creation; do not publish credentials or temporary UserData. `COMPLETE` is written
only after all measurements and verified cleanup. Failed attempts remain intact.
If creation or cleanup times out, recover using fresh lab credentials:
`python benchmarks/cleanup_lab.py --state <run>/cleanup-state.json`.

`--synthetic --rates 2,10 --threads 1,5 --repetitions 1 --warm-seconds 0` is an
instrumentation pilot. Its plots require explicit `--allow-synthetic` and are
prominently labelled as synthetic; they cannot replace the requested lab results.
