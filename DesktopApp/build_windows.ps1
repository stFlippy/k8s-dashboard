$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    python release_manifest.py
    if ($LASTEXITCODE -ne 0) { throw 'Build environment differs from requirements.lock' }
    python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed' }
    python -m PyInstaller --noconfirm --clean --windowed --onedir --name Quaber --contents-directory . --add-data 'assets;assets' --add-data 'public;public' --distpath 'build-artifacts' --workpath '.build-windows' --specpath '.build-windows' main.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
    New-Item -ItemType Directory -Force 'build-artifacts\Quaber\addons' | Out-Null
    python release_manifest.py 'build-artifacts\Quaber'
    if ($LASTEXITCODE -ne 0) { throw 'Release inventory failed' }
    Write-Host 'Portable files: build-artifacts\Quaber'
    Write-Host 'Copy the folder to a new location; keep the existing UserData when updating.'
} finally {
    Pop-Location
}
