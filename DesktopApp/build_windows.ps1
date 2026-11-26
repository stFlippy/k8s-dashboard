param(
    [string]$Interpreter = (Join-Path $PSScriptRoot '.venv-qt5\Scripts\python.exe'),
    [string]$OutputRoot = '',
    [ValidateSet('baseline', 'minimal-candidate', 'conservative-candidate')]
    [string]$Profile = 'baseline'
)

$ErrorActionPreference = 'Stop'
$previousProfile = $env:QUABER_BUILD_PROFILE
$previousReport = $env:QUABER_BUILD_REPORT
Push-Location $PSScriptRoot
try {
    $python = (Resolve-Path -LiteralPath $Interpreter -ErrorAction Stop).Path
    if (-not $OutputRoot) {
        $OutputRoot = Join-Path $PSScriptRoot ("build-artifacts-size-{0}-{1}" -f $Profile, (Get-Date -Format 'yyyyMMdd-HHmmss'))
    }
    $OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
    if (Test-Path -LiteralPath $OutputRoot) {
        throw "Output directory already exists; use a new empty path: $OutputRoot"
    }
    $workRoot = Join-Path $OutputRoot 'work'
    $reportRoot = Join-Path $OutputRoot 'reports'

    & $python release_manifest.py
    if ($LASTEXITCODE -ne 0) { throw 'Build environment differs from requirements.lock' }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed' }
    New-Item -ItemType Directory -Path $reportRoot -Force | Out-Null
    $env:QUABER_BUILD_PROFILE = $Profile
    $env:QUABER_BUILD_REPORT = $reportRoot
    # Windows PowerShell treats native stderr as errors; PyInstaller logs there.
    $ErrorActionPreference = 'Continue'
    & $python -m PyInstaller --noconfirm --clean --distpath $OutputRoot --workpath $workRoot packaging/Quaber.spec *> (Join-Path $reportRoot 'pyinstaller.log')
    $buildExit = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($buildExit -ne 0) { throw "PyInstaller failed with exit code $buildExit; see $reportRoot/pyinstaller.log" }
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
    & $python release_manifest.py $portableRoot
    if ($LASTEXITCODE -ne 0) { throw 'Release inventory failed' }
    & $python packaging/release_check.py $portableRoot $reportRoot $Profile
    if ($LASTEXITCODE -ne 0) { throw 'Release verification/ZIP failed' }
    Write-Host "Portable files: $portableRoot"
    Write-Host 'Copy the folder to a new location; keep the existing UserData when updating.'
} finally {
    $env:QUABER_BUILD_PROFILE = $previousProfile
    $env:QUABER_BUILD_REPORT = $previousReport
    Pop-Location
}
