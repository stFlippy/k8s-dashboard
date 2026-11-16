$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    & "$PSScriptRoot\..\venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --console --name kubectl --add-data "$PSScriptRoot\kubectl;." --distpath "$PSScriptRoot" --workpath "$PSScriptRoot\..\.build-windows\kubectl" --specpath "$PSScriptRoot\..\.build-windows" "$PSScriptRoot\kubectl_windows.py"
    if ($LASTEXITCODE -ne 0) { throw "kubectl build failed: $LASTEXITCODE" }
} finally {
    Pop-Location
}
