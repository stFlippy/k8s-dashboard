$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    # Install pip-audit in a separate tooling environment; do not silently
    # change application dependencies while checking a release.
    python -m pip_audit --no-deps -r requirements.lock --format json --output dependency-audit.json
    if ($LASTEXITCODE -ne 0) { throw 'Dependency audit failed or found vulnerabilities. Inspect dependency-audit.json.' }
} finally {
    Pop-Location
}
