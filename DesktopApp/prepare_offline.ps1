param(
    [string]$Interpreter = (Join-Path $PSScriptRoot '.venv-qt5\Scripts\python.exe'),
    [string]$Wheelhouse = (Join-Path $PSScriptRoot 'offline-deps\wheelhouse-qt5')
)

$ErrorActionPreference = 'Stop'
$offlineRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'offline-deps'))
$wheelhousePath = [IO.Path]::GetFullPath($Wheelhouse)
if (-not $wheelhousePath.StartsWith($offlineRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Wheelhouse must be inside $offlineRoot"
}
if (Test-Path -LiteralPath $wheelhousePath) {
    throw "Wheelhouse already exists; use a new empty path: $wheelhousePath"
}

$python = (Resolve-Path -LiteralPath $Interpreter -ErrorAction Stop).Path
$archive = Join-Path $offlineRoot 'dependencies.tar.xz'
$newArchive = Join-Path $offlineRoot 'dependencies-qt5.new.tar.xz'
$manifest = Join-Path $offlineRoot 'wheelhouse-qt5.sha256.json'
$rollback = Join-Path $offlineRoot 'qt6-rollback\dependencies-qt6.tar.xz'

New-Item -ItemType Directory -Path $wheelhousePath | Out-Null
& $python -m pip download --only-binary=:all: --dest $wheelhousePath -r (Join-Path $PSScriptRoot 'requirements.lock')
if ($LASTEXITCODE -ne 0) { throw 'Downloading the locked wheelhouse failed' }

$files = @{}
Get-ChildItem -LiteralPath $wheelhousePath -File | Sort-Object Name | ForEach-Object {
    $files[$_.Name] = @{sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant(); size=$_.Length}
}
$payload = @{python='CPython 3.11 x64'; pyqt='5.15.9'; qt='5.15.2'; sip='12.18.0'; files=$files}
[IO.File]::WriteAllText($manifest, ($payload | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))

if ((Test-Path -LiteralPath $archive) -and -not (Test-Path -LiteralPath $rollback)) {
    New-Item -ItemType Directory -Path (Split-Path $rollback) -Force | Out-Null
    Copy-Item -LiteralPath $archive -Destination $rollback
}
if (Test-Path -LiteralPath $newArchive) {
    throw "Temporary archive already exists: $newArchive"
}
tar -cJf $newArchive -C $offlineRoot (Split-Path $wheelhousePath -Leaf) (Split-Path $manifest -Leaf)
if ($LASTEXITCODE -ne 0) { throw 'Creating dependencies.tar.xz failed' }
Move-Item -LiteralPath $newArchive -Destination $archive -Force
Write-Host "Wheelhouse: $wheelhousePath"
Write-Host "Archive: $archive"
Write-Host "Manifest: $manifest"
