param([string]$SmokeRoot = (Join-Path $PSScriptRoot '../.build-windows-size-smoke'),
      [string]$Report = (Join-Path $PSScriptRoot '../reports/build-size-2026-11-18/startup.json'))
$ErrorActionPreference = 'Stop'
$rows = @()
# Alternate profiles to reduce cache/order bias. Measures input-idle, not data-ready.
foreach ($iteration in 1..10) {
    foreach ($profile in @('baseline', 'conservative')) {
        $exe = [IO.Path]::GetFullPath((Join-Path $SmokeRoot "$profile/Quaber/Quaber.exe"))
        $timer = [Diagnostics.Stopwatch]::StartNew()
        $proc = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden
        try {
            $ready = $proc.WaitForInputIdle(30000)
            $timer.Stop()
            if (-not $ready -or $proc.HasExited) { throw "Startup failed: $profile" }
            $rows += @{profile=$profile; iteration=$iteration; input_idle_ms=$timer.Elapsed.TotalMilliseconds}
        } finally {
            # Benchmark process only: functional graceful close is tested separately.
            if (-not $proc.HasExited) { $proc.Kill(); $proc.WaitForExit() }
        }
    }
}
[IO.File]::WriteAllText([IO.Path]::GetFullPath($Report), ($rows | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
