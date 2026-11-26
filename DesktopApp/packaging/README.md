# Windows build size profiles

The supported release stack remains CPython 3.11 x64 / PyQt5 5.15.9 /
Qt 5.15.2 / SIP 12.18.0, with exact `requirements.lock` pins.
Create a normal venv from a full Python installation, install that lock, then run:

```powershell
.\build_windows.ps1 -Interpreter .\.venv-release-size\Scripts\python.exe -Profile baseline
.\build_windows.ps1 -Interpreter .\.venv-release-size\Scripts\python.exe -Profile conservative-candidate
```

Each invocation requires a new output directory (or generates a dated one),
with independent `work/`, `reports/`, `Quaber/` and a DEFLATE level 9 ZIP.
Only `Quaber/` enters the archive. Addons, legacy maintenance `dumps` and
UserData are excluded, including empty directories. The external kubectl is
selected in settings or found in PATH; the portable does not create `addons`.
Default remains baseline until the acceptance matrix in
`reports/build-size-2026-11-18/SPEC.md` is signed off.
Raster is deliberately unavailable in this implementation.

`Quaber.spec` is the versioned source of build settings. CLI options with a spec
are restricted by [PyInstaller](https://pyinstaller.org/en/stable/spec-files.html).
No application modules or installed PyInstaller hooks are modified.

`public/icon.ico` is a build input embedded into `Quaber.exe`; it is not shipped
as a separate runtime asset. `assets/fonts/README.md` is development documentation;
Roboto.ttf and its OFL license remain bundled. The former duplicate
`assets/logo.png` was removed; the sidebar uses `public/logo.png`.

CronJob Next run uses only the manually configured constant UTC offset. Calendar
matching uses the Python standard library; no external calendar data is shipped.

`policy-files.json` freezes the reviewed Qt inventory and exclusion hashes from
the audit CSV/measurements, plus the lock hash. `policy.py` removes exact
destination paths after Analysis. Any lock change, missing/new Qt path or
changed excluded content stops the build. Review a new baseline inventory and
PE closure before updating this policy; never auto-learn exclusions during build.
Minimal retains translations; conservative retains `qtbase_ru.qm`,
`qt_help_ru.qm`, `qt_ru.qm` and built-in English. All image plugins, font and
graphics fallbacks remain. Qt5Network and TUIO remain.

`release_check.py` verifies final resources, Qt composition and PE import/delay
imports against bundled files or Windows System32/API sets. It writes hashes,
sizes and the PE graph, then verifies clean ZIP extraction with Cyrillic/spaces.
Static closure records matching bundle destinations; it does not prove Windows
dynamic loader search order. Inspect loaded process modules during scenarios.
Manifest records source hashes (including uncommitted source), profile, runtime,
build environment and exclusions. SBOM identifies bundled runtime separately.

The lab preparation helper accepts baseline ZIP, candidate ZIP and a new work
directory as positional arguments. Pass a JSON object with `servers`, `username`
and `password` through stdin; never store it in a report or command history.
It uses existing login/transport, reads resources/events/logs/metrics and seeds
isolated encrypted UserData for GUI checks. It does not modify cluster objects.
Delete these temporary UserData directories after closing smoke processes.

`benchmark_start.ps1` alternates 10 launches per profile using prepared smoke
directories. It measures first Windows input-idle, then terminates only its own
process. This is a warm-start indicator, not cold-start or GUI latency acceptance.
Graceful shutdown/reopen, full DPI/RDP/VM/GPU matrix and the 60-minute soak remain
separate acceptance checks. Do not promote a candidate based on a single smoke.

CronJob schedules use a bounded day search and the configured fixed UTC offset.
A packaged EXE supports `--verify-schedule-runtime`: it checks positive/negative
offsets and leap dates, exits 0 on success, and opens no GUI/UserData/connection.
This check supplements ordinary qwindows startup and functional acceptance.

Legacy `../build/windows/Quaber` relative to the repository root (i.e.
`C:/Projects/k8s-dashboard/build/windows/Quaber`) is the old **Qt6** artifact.
Old Qt5 outputs under DesktopApp were recycled during the 2026-11-20 cleanup.
The retained complete Qt5 release is
`DesktopApp/Builds/build-20261119-141804-ef056b2d/baseline/Quaber`.
The legacy Qt6 output outside DesktopApp is outside that cleanup scope.
Never overlay Qt versions/profiles. Roll back by returning to the complete
previous release, with controlled reuse of UserData. See
`reports/implementation-v2-2026-11-20/CLEANUP.md` for the cleanup record.
