#Requires -Version 5.1
param()

$WORKSPACE  = $PSScriptRoot
$SNAP       = "$env:TEMP\ReaderRadar-Skeld-live.json"
$PUBLISHER  = "$WORKSPACE\skeld_snapshot.py"
$PROBE      = "$WORKSPACE\skeld_probe.py"
$WEB_SERVER = "$WORKSPACE\web_radar_server.py"
$WEB_PUSHER = "$WORKSPACE\web_radar_push.py"
$WEB_PORT   = 8080

# -- 1. Kill stale processes --------------------------------------------------
Write-Host "[1/5] Stopping any running publisher / web stack..."
foreach ($pattern in @("skeld_snapshot", "web_radar_server", "web_radar_push")) {
    $procs = Get-WmiObject Win32_Process -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -match $pattern }
    if ($procs) {
        $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Write-Host "      Stopped: $pattern ($($procs.Count))"
    }
}
Start-Sleep -Milliseconds 600

# -- 2. Find PID + check build ------------------------------------------------
Write-Host "[2/5] Probing game..."
$probe_out  = python -B "$PROBE" 2>&1
$probe_out  | ForEach-Object { Write-Host "      $_" }

$pid_line   = $probe_out | Where-Object { $_ -match "^PID="   } | Select-Object -First 1
$build_line = $probe_out | Where-Object { $_ -match "^BUILD=" } | Select-Object -First 1
$err_line   = $probe_out | Where-Object { $_ -match "^ERROR=" } | Select-Object -First 1

if ($err_line -or -not $pid_line) {
    Write-Error "Probe failed. Is the game running and DMA connected?"
    exit 1
}

$au_pid = [int]($pid_line -replace "PID=", "")
Write-Host "      PID = $au_pid"

if ($build_line -match "MISMATCH") {
    Write-Warning "GameAssembly.dll changed since last validated run. Offsets may be stale."
    $yn = Read-Host "      Continue anyway? (y/N)"
    if ($yn -notmatch '^[yY]') { exit 1 }
} elseif ($build_line -match "NEW_BASELINE") {
    Write-Host "      Build baseline saved."
}

# -- 3. Start snapshot publisher ----------------------------------------------
Write-Host "[3/5] Starting snapshot publisher for PID $au_pid ..."
Start-Process powershell.exe -ArgumentList "-NoExit", "-Command",
    "python -B '$PUBLISHER' $au_pid '$SNAP'; pause"

$deadline = (Get-Date).AddSeconds(8)
Write-Host -NoNewline "      Waiting for first snapshot"
while ((-not (Test-Path $SNAP)) -and ((Get-Date) -lt $deadline)) {
    Write-Host -NoNewline "."; Start-Sleep -Milliseconds 500
}
Write-Host ""
if (Test-Path $SNAP) {
    Write-Host "      Snapshot ready."
} else {
    Write-Warning "No snapshot yet — web radar will show offline until the feed starts."
}

# -- 4. Start web radar server + pusher ---------------------------------------
Write-Host "[4/5] Starting web radar server on port $WEB_PORT ..."
Start-Process powershell.exe -ArgumentList "-NoExit", "-Command",
    "python '$WEB_SERVER'; pause" -WindowStyle Minimized

Start-Sleep -Milliseconds 1500

Start-Process powershell.exe -ArgumentList "-NoExit", "-Command",
    "python '$WEB_PUSHER'; pause" -WindowStyle Minimized

Write-Host "      Web server started."

# -- 5. Open radar in browser -------------------------------------------------
Write-Host "[5/5] Opening radar in browser..."
Start-Sleep -Milliseconds 800
Start-Process "http://localhost:$WEB_PORT"

Write-Host ""
Write-Host "Done. Radar open at http://localhost:$WEB_PORT"
Write-Host "Login: admin / changeme  (change this in /admin)"
Write-Host "Close the minimised windows to stop the feed."
