$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    python -m PyInstaller --noconfirm --clean --windowed --onedir --name Quaber --contents-directory . --add-data 'assets;assets' --distpath 'build-artifacts' --workpath '.build-windows' --specpath '.build-windows' main.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
    New-Item -ItemType Directory -Force 'build-artifacts\Quaber\addons' | Out-Null
    Write-Host 'Portable files: build-artifacts\Quaber'
    Write-Host 'Copy the folder to a new location; keep the existing UserData when updating.'
} finally {
    Pop-Location
}
