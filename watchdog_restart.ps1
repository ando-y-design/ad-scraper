# ad_scraper watchdog restart script v3
# Uses PID lock file to prevent duplicate instances
# v3: indefinite repetition fix + profile lock cleanup + pythonw detection

$ProjectDir       = "C:\Users\amdwt\ad_scraper"
$PythonExe        = "$ProjectDir\venv\Scripts\python.exe"
$LogFile          = "$ProjectDir\logs\watchdog_restart.log"
$MainLog          = "$ProjectDir\logs\main.log"
$PidLockFile      = "$ProjectDir\logs\scraper.pid"
$HangThresholdMin = 15

function Write-Log {
    param($msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts $msg"
    $line | Out-File -Append -Encoding utf8 $LogFile
    Write-Host $line
}

Write-Log "[WatchdogRestart] Starting check"

# -- Check PID lock file --
$lockPid = 0
if (Test-Path $PidLockFile) {
    try {
        $lockPid = [int](Get-Content $PidLockFile -Raw -ErrorAction Stop).Trim()
    } catch {
        Write-Log "[WatchdogRestart] Failed to read PID lock file (ignored)"
    }
}

if ($lockPid -gt 0) {
    $lockProc = Get-Process -Id $lockPid -ErrorAction SilentlyContinue
    if ($lockProc) {
        Write-Log "[WatchdogRestart] Lock PID=$lockPid is alive"

        # Check log freshness
        if (Test-Path $MainLog) {
            $lastWrite = (Get-Item $MainLog).LastWriteTime
            $diffMin   = ((Get-Date) - $lastWrite).TotalMinutes
            $diffRound = [math]::Round($diffMin, 1)
            Write-Log "[WatchdogRestart] Log last updated: $lastWrite (${diffRound}min ago)"

            if ($diffMin -le $HangThresholdMin) {
                Write-Log "[WatchdogRestart] Healthy - nothing to do"
                exit 0
            } else {
                Write-Log "[WatchdogRestart] Log stalled ${diffRound}min - restarting"
            }
        } else {
            Write-Log "[WatchdogRestart] No log file yet - probably still starting up"
            exit 0
        }
    } else {
        Write-Log "[WatchdogRestart] Lock PID=$lockPid not found - starting fresh"
    }
} else {
    Write-Log "[WatchdogRestart] No PID lock file - starting"
}

# -- Kill all existing main.py processes to avoid duplicates (python.exe AND pythonw.exe) --
$existingProcs = Get-CimInstance Win32_Process |
    Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and
                   $_.CommandLine -like "*ad_scraper*main.py*" }

if ($existingProcs) {
    $cnt = $existingProcs.Count
    Write-Log "[WatchdogRestart] Stopping $cnt existing process(es)"
    foreach ($p in $existingProcs) {
        Write-Log "[WatchdogRestart]   Kill PID=$($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 5
}

# Clear stale PID lock
Remove-Item $PidLockFile -ErrorAction SilentlyContinue

# -- Clean up browser profile locks to prevent launch failures --
$ProfileDirs = @(
    "$ProjectDir\browser_profile",
    "$ProjectDir\browser_profile_yahoo",
    "$ProjectDir\browser_profile_yahoo2"
)
$LockNames = @('LOCK', 'lockfile', 'SingletonLock', 'SingletonSocket', 'SingletonCookie')
foreach ($dir in $ProfileDirs) {
    foreach ($lname in $LockNames) {
        foreach ($sub in @('', '\Default')) {
            $lpath = "$dir$sub\$lname"
            if (Test-Path $lpath) {
                Remove-Item $lpath -Force -ErrorAction SilentlyContinue
                Write-Log "[WatchdogRestart] Removed lock: $lpath"
            }
        }
    }
}

# -- Start exactly one new instance --
$newProc = Start-Process -FilePath $PythonExe -ArgumentList "main.py" `
    -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru
Write-Log "[WatchdogRestart] Started: PID=$($newProc.Id)"
