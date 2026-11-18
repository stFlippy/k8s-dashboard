param(
    [string]$Interpreter = (Join-Path $PSScriptRoot '.venv-qt5\Scripts\python.exe'),
    [string]$OutputRoot = (Join-Path $PSScriptRoot 'build-artifacts-qt5')
)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $python = (Resolve-Path -LiteralPath $Interpreter -ErrorAction Stop).Path
    if (Test-Path -LiteralPath $OutputRoot) {
        throw "Output directory already exists; use a new empty path: $OutputRoot"
    }
    $workRoot = Join-Path $PSScriptRoot '.build-windows-qt5'
    $specRoot = Join-Path $workRoot 'spec'

    & $python release_manifest.py
    if ($LASTEXITCODE -ne 0) { throw 'Build environment differs from requirements.lock' }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed' }
    $assets = Join-Path $PSScriptRoot 'assets'
    $public = Join-Path $PSScriptRoot 'public'
    & $python -m PyInstaller --noconfirm --clean --windowed --onedir --name Quaber --contents-directory . --add-data "$assets;assets" --add-data "$public;public" --distpath $OutputRoot --workpath $workRoot --specpath $specRoot main.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
    $portableRoot = Join-Path $OutputRoot 'Quaber'
    $requiredQtFiles = @(
        'PyQt5\Qt5\bin\Qt5Core.dll',
        'PyQt5\Qt5\bin\Qt5Svg.dll',
        'PyQt5\Qt5\plugins\platforms\qwindows.dll',
        'PyQt5\Qt5\plugins\imageformats\qsvg.dll',
        'PyQt5\Qt5\plugins\imageformats\qjpeg.dll',
        'PyQt5\sip.cp311-win_amd64.pyd'
    )
    foreach ($relative in $requiredQtFiles) {
        $path = Join-Path $portableRoot $relative
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Portable Qt dependency is missing: $relative"
        }
    }
    $coreVersion = (Get-Item -LiteralPath (Join-Path $portableRoot 'PyQt5\Qt5\bin\Qt5Core.dll')).VersionInfo.FileVersion
    if ($coreVersion -ne '5.15.2.0') { throw "Unexpected bundled Qt runtime: $coreVersion" }
    $alternativeQt = @(Get-ChildItem -LiteralPath $portableRoot -Recurse -File | Where-Object Name -Match 'PyQt6|Qt6')
    if ($alternativeQt) { throw "Alternative Qt files entered the portable build: $($alternativeQt.FullName -join ', ')" }
    New-Item -ItemType Directory -Force (Join-Path $portableRoot 'addons') | Out-Null
    & $python release_manifest.py $portableRoot
    if ($LASTEXITCODE -ne 0) { throw 'Release inventory failed' }
    Write-Host "Portable files: $portableRoot"
    Write-Host 'Copy the folder to a new location; keep the existing UserData when updating.'
} finally {
    Pop-Location
}
