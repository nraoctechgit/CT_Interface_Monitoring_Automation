<#
  run_demo.ps1 - one-command driver for the Mirth stakeholder demo.

  PHASES
    .\run_demo.ps1            # (console) preflight + show BEFORE state + launch the web console + open browser
    .\run_demo.ps1 fix        # run the OOM agent to APPLY the fix (self-elevates to Administrator)
    .\run_demo.ps1 reset      # restore the broken state (-Xmx256m, service stopped) for a repeat run (self-elevates)
    .\run_demo.ps1 stop       # stop the web console server
    .\run_demo.ps1 check      # preflight checks only (no changes, no server)

  The 'fix' and 'reset' phases edit files under C:\Program Files and control the
  Windows service, so they relaunch themselves in an elevated window automatically.
#>
[CmdletBinding()]
param([Parameter(Position = 0)][ValidateSet('console', 'fix', 'reset', 'stop', 'check')][string]$Phase = 'console')

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

# --- environment specifics (verified on this machine) ---------------------
$Py   = Join-Path $root '.venv\Scripts\python.exe'
$Vm   = 'C:\Program Files\Mirth Connect\mcservice.vmoptions'
$Log  = 'C:\Program Files\Mirth Connect\logs\mirth.log'
$Svc  = 'Mirth Connect Service'
$Port = 8000
$Url  = "http://localhost:$Port"

# --- helpers --------------------------------------------------------------
function Say([string]$t, [string]$c = 'Gray') { Write-Host $t -ForegroundColor $c }
function Rule([string]$t) { Write-Host ''; Write-Host ('=' * 68) -ForegroundColor DarkCyan; Write-Host "  $t" -ForegroundColor Cyan; Write-Host ('=' * 68) -ForegroundColor DarkCyan }

function Test-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

function Ensure-Admin([string]$forPhase) {
  if (Test-Admin) { return $true }
  Say 'This step edits C:\Program Files and the Windows service, so it needs Administrator rights.' Yellow
  Say 'Relaunching in an elevated window...' Yellow
  Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList @(
    '-NoExit', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", $forPhase
  )
  return $false
}

function Load-Key {
  if ($env:ANTHROPIC_API_KEY) { return }
  $line = Get-Content (Join-Path $root '.env') -ErrorAction SilentlyContinue | Where-Object { $_ -match '^ANTHROPIC_API_KEY=' } | Select-Object -First 1
  if ($line) { $env:ANTHROPIC_API_KEY = ($line -replace '^ANTHROPIC_API_KEY=', '').Trim() }
  $env:PYTHONIOENCODING = 'utf-8'
}

function Get-Heap {
  if (-not (Test-Path $Vm)) { return '(vmoptions not found)' }
  $x = (Select-String -Path $Vm -Pattern '-Xmx\S+' -ErrorAction SilentlyContinue | Select-Object -First 1).Matches.Value
  if ($x) { return $x } else { return '(no -Xmx set)' }
}
function Get-OomCount {
  if (-not (Test-Path $Log)) { return 0 }
  (Select-String -Path $Log -Pattern 'OutOfMemoryError|Java heap space' -ErrorAction SilentlyContinue | Measure-Object).Count
}
function Get-SvcStatus { try { (Get-Service -Name $Svc -ErrorAction Stop).Status } catch { 'NOT FOUND' } }

function Show-State([string]$title) {
  Rule "$title  ::  Mirth server state"
  $h = Get-Heap; $s = Get-SvcStatus; $o = Get-OomCount
  $hc = 'Green'; if ($h -match '256m|128m|no -Xmx') { $hc = 'Red' }
  $sc = 'Red';   if ($s -eq 'Running') { $sc = 'Green' }
  $oc = 'Green'; if ($o -gt 0) { $oc = 'Red' }
  Write-Host '   Server max heap (-Xmx) : ' -NoNewline; Write-Host $h -ForegroundColor $hc
  Write-Host '   Service status         : ' -NoNewline; Write-Host $s -ForegroundColor $sc
  Write-Host '   OOM lines in mirth.log : ' -NoNewline; Write-Host $o -ForegroundColor $oc
}

function Test-PyImport([string]$m) {
  if (-not (Test-Path $Py)) { return $false }
  & $Py -c "import $m" 2>$null
  return ($LASTEXITCODE -eq 0)
}

function Preflight {
  Rule 'Preflight checks'
  $script:pf = $true
  function Chk($name, $cond, $hint) {
    if ($cond) { Write-Host "   [OK]  $name" -ForegroundColor Green }
    else { Write-Host "   [!!]  $name  ->  $hint" -ForegroundColor Red; $script:pf = $false }
  }
  Chk 'Python venv present' (Test-Path $Py) 'create the venv and pip install -r requirements.txt'
  Chk 'Flask installed' (Test-PyImport 'flask') 'run: .\.venv\Scripts\python.exe -m pip install flask'
  Chk 'anthropic installed' (Test-PyImport 'anthropic') 'run: pip install anthropic'
  $key = (Get-Content (Join-Path $root '.env') -ErrorAction SilentlyContinue | Where-Object { $_ -match '^ANTHROPIC_API_KEY=sk-' })
  Chk 'ANTHROPIC_API_KEY in .env' ([bool]$key) 'add your key to .env'
  Chk 'Mirth vmoptions found' (Test-Path $Vm) 'check the Mirth install path'
  Chk 'Mirth log found' (Test-Path $Log) 'check MIRTH_LOG_PATH'
  Chk 'Windows service present' ($null -ne (Get-Service -Name $Svc -ErrorAction SilentlyContinue)) 'confirm the service name'
  if ($script:pf) { Say "`n   All checks passed. You are ready to present." Green } else { Say "`n   Fix the items above before presenting." Yellow }
}

function Stop-Console {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*mirth_console*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

# --- phases ---------------------------------------------------------------
switch ($Phase) {

  'check' { Preflight }

  'console' {
    Preflight
    Show-State 'BEFORE (the broken state your audience sees)'
    Rule 'Launching the web console'
    Stop-Console
    $env:PYTHONIOENCODING = 'utf-8'
    Start-Process -FilePath $Py -ArgumentList @('mirth_console.py', '--port', "$Port") -WindowStyle Hidden `
      -RedirectStandardOutput "$env:TEMP\mirth_console.out" -RedirectStandardError "$env:TEMP\mirth_console.err"
    Start-Sleep -Seconds 3
    try { $null = Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 8; Say "   Console is up at $Url" Green }
    catch { Say "   Console did not answer yet; check $env:TEMP\mirth_console.err" Yellow }
    Start-Process $Url
    Rule 'Next'
    Say '   In the browser: pick a scenario -> Run diagnosis (the agent investigates live).' Gray
    Say '   Then, for the recovery demo, run:   .\run_demo.ps1 fix' Gray
  }

  'fix' {
    if (-not (Ensure-Admin 'fix')) { return }
    Load-Key
    Show-State 'BEFORE the fix'
    Rule 'Running the OOM recovery agent  (python mirth_oom_agent.py --apply)'
    Say '   It will ask before EACH change (heap edit, restart). Type y to approve.' Yellow
    Write-Host ''
    & $Py 'mirth_oom_agent.py' '--apply'
    Show-State 'AFTER the fix'
    Say "`n   A timestamped backup of the original .vmoptions was written next to it." DarkGray
  }

  'reset' {
    if (-not (Ensure-Admin 'reset')) { return }
    Rule 'Resetting to the broken state (for a repeat demo)'
    $broken = @('-server', '-Xmx256m', '-Djava.awt.headless=true', '-Dapple.awt.UIElement=true')
    Set-Content -Path $Vm -Value $broken -Encoding Ascii
    Say "   Restored $Vm  ->  -Xmx256m" Green
    try { Stop-Service -Name $Svc -Force -ErrorAction Stop; Say "   Stopped '$Svc'." Green }
    catch { Say "   Could not stop service: $($_.Exception.Message)" Yellow }
    Show-State 'Reset complete'
  }

  'stop' {
    Rule 'Stopping the web console'
    Stop-Console
    Say '   Console stopped (if it was running).' Green
  }
}
