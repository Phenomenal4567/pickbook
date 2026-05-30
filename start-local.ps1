$ErrorActionPreference = "Stop"

$Port = if ($env:PORT) { [int]$env:PORT } else { 8000 }
$HostName = if ($env:HOST) { $env:HOST } else { "127.0.0.1" }
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$OutLog = Join-Path $Root "uvicorn.out.log"
$ErrLog = Join-Path $Root "uvicorn.err.log"
$PidFile = Join-Path $Root "uvicorn.pid"
$LocalDb = (Join-Path $Root "pickbook_local.db").Replace("\", "/")

Set-Location $Root

if (-not $env:APP_ENV) {
    $env:APP_ENV = "development"
}

if (-not $env:DATABASE_URL) {
    $env:DATABASE_URL = "sqlite:///$LocalDb"
}

$env:LOCAL_DATABASE_FALLBACK = "true"

$existing = netstat -ano |
    Select-String ":$Port\s+.*LISTENING" |
    ForEach-Object {
        if ($_.Line -match "\s+(\d+)\s*$") { [int]$Matches[1] }
    } |
    Select-Object -Unique

if ($existing) {
    $existing | Select-Object -First 1 | Set-Content -NoNewline $PidFile
    Write-Host "PickBook is already live at http://$HostName`:$Port/ (PID $($existing -join ', '))"
    exit 0
}

$Python = (Get-Command python).Source
$Arguments = @("-m", "uvicorn", "app.main:app", "--host", $HostName, "--port", "$Port")

try {
    $process = Start-Process `
        -FilePath $Python `
        -ArgumentList $Arguments `
        -WorkingDirectory $Root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog `
        -PassThru
} catch [System.ArgumentException] {
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $Python
    $psi.UseShellExecute = $true
    $psi.Arguments = ($Arguments | ForEach-Object {
        if ($_ -match "\s") { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
    }) -join " "
    $psi.WorkingDirectory = $Root
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $psi.CreateNoWindow = $true
    $process = [System.Diagnostics.Process]::Start($psi)
}

$deadline = (Get-Date).AddSeconds(45)
$healthUrl = "http://$HostName`:$Port/health"

do {
    Start-Sleep -Seconds 1

    $listener = netstat -ano |
        Select-String ":$Port\s+.*LISTENING" |
        ForEach-Object {
            if ($_.Line -match "\s+(\d+)\s*$") { [int]$Matches[1] }
        } |
        Select-Object -Unique -First 1

    if ($listener) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing $healthUrl -TimeoutSec 5
            if ($response.StatusCode -eq 200) {
                $listener | Set-Content -NoNewline $PidFile
                Write-Host "PickBook is live at http://$HostName`:$Port/ (PID $listener)"
                exit 0
            }
        } catch {
            # Startup can take a few seconds while the database initializes.
        }
    }
} while ((Get-Date) -lt $deadline -and -not $process.HasExited)

if ($process.HasExited) {
    Write-Error "PickBook failed to start. See $ErrLog"
}

Write-Error "PickBook started but did not pass health checks before timeout. See $ErrLog"
