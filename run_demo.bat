@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

rem ==========================================================================
rem  run_demo.bat - one-command driver for the Mirth stakeholder demo.
rem  (Batch version - no PowerShell required.)
rem
rem  PHASES
rem    run_demo.bat            (console) preflight + show BEFORE state + launch UI + open browser
rem    run_demo.bat fix        run the OOM agent to APPLY the fix (self-elevates to Administrator)
rem    run_demo.bat reset      restore the broken state (-Xmx256m, service stopped) (self-elevates)
rem    run_demo.bat stop       stop the web console server
rem    run_demo.bat check      preflight checks only (no changes, no server)
rem
rem  'fix' and 'reset' edit files under C:\Program Files and control the Windows
rem  service, so they relaunch themselves elevated (approve the UAC prompt).
rem  If elevation is blocked, right-click this file -> "Run as administrator",
rem  then run:  run_demo.bat fix
rem ==========================================================================

set "PY=%~dp0.venv\Scripts\python.exe"
set "VM=C:\Program Files\Mirth Connect\mcservice.vmoptions"
set "LOG=C:\Program Files\Mirth Connect\logs\mirth.log"
set "SVC=Mirth Connect Service"
set "PORT=8000"
set "URL=http://localhost:%PORT%"

set "PHASE=%~1"
if "%PHASE%"=="" set "PHASE=console"

if /i "%PHASE%"=="check"   goto :check
if /i "%PHASE%"=="console" goto :console
if /i "%PHASE%"=="fix"     goto :fix
if /i "%PHASE%"=="reset"   goto :reset
if /i "%PHASE%"=="stop"    goto :stop
echo Unknown phase "%PHASE%".  Use: check ^| console ^| fix ^| reset ^| stop
goto :end

rem ------------------------------------------------------------------ check
:check
call :preflight
echo.
if defined PFAIL (echo   Fix the items above before presenting.) else (echo   All checks passed. You are ready to present.)
goto :end

rem ---------------------------------------------------------------- console
:console
call :preflight
call :showstate "BEFORE (the broken state your audience sees)"
echo.
echo ====================================================================
echo   Launching the web console
echo ====================================================================
call :stopconsole
start "MirthConsole" /min "%PY%" mirth_console.py --port %PORT%
echo   Waiting for the console to come up...
ping -n 5 127.0.0.1 >nul
start "" "%URL%"
echo.
echo   Console should be open at %URL%
echo   Next, for the recovery demo, run:   run_demo.bat fix
goto :end

rem -------------------------------------------------------------------- fix
:fix
net session >nul 2>&1
if %errorlevel%==0 goto :fix_admin
echo This step edits C:\Program Files and restarts a service, so it needs admin rights.
echo Approve the UAC prompt that appears...
call :elevate fix
goto :end
:fix_admin
call :loadkey
call :showstate "BEFORE the fix"
echo.
echo ====================================================================
echo   Running the OOM recovery agent   (mirth_oom_agent.py --apply)
echo ====================================================================
echo   It will ask before EACH change (heap edit, restart). Type y to approve.
echo.
"%PY%" mirth_oom_agent.py --apply
call :showstate "AFTER the fix"
echo.
echo   A timestamped backup of the original .vmoptions was written next to it.
echo.
pause
goto :end

rem ------------------------------------------------------------------ reset
:reset
net session >nul 2>&1
if %errorlevel%==0 goto :reset_admin
echo This step edits C:\Program Files and the service, so it needs admin rights.
echo Approve the UAC prompt that appears...
call :elevate reset
goto :end
:reset_admin
echo ====================================================================
echo   Resetting to the broken state (for a repeat demo)
echo ====================================================================
> "%VM%" echo -server
>>"%VM%" echo -Xmx256m
>>"%VM%" echo -Djava.awt.headless=true
>>"%VM%" echo -Dapple.awt.UIElement=true
echo   Restored "%VM%"  -^>  -Xmx256m
net stop "%SVC%"
call :showstate "Reset complete"
echo.
pause
goto :end

rem ------------------------------------------------------------------- stop
:stop
call :stopconsole
echo   Console stopped (if it was running).
goto :end

rem ================================ subroutines ============================

:preflight
echo ====================================================================
echo   Preflight checks
echo ====================================================================
set "PFAIL="
if exist "%PY%" (echo   [OK]  Python venv present) else (echo   [!!]  Python venv missing & set PFAIL=1)
"%PY%" -c "import flask" 2>nul && (echo   [OK]  Flask installed) || (echo   [!!]  Flask missing - run: "%PY%" -m pip install flask & set PFAIL=1)
"%PY%" -c "import anthropic" 2>nul && (echo   [OK]  anthropic installed) || (echo   [!!]  anthropic missing - run: "%PY%" -m pip install anthropic & set PFAIL=1)
findstr /b "ANTHROPIC_API_KEY=sk-" "%~dp0.env" >nul 2>&1 && (echo   [OK]  ANTHROPIC_API_KEY in .env) || (echo   [!!]  ANTHROPIC_API_KEY missing in .env & set PFAIL=1)
if exist "%VM%" (echo   [OK]  Mirth vmoptions found) else (echo   [!!]  vmoptions missing & set PFAIL=1)
if exist "%LOG%" (echo   [OK]  Mirth log found) else (echo   [!!]  Mirth log missing & set PFAIL=1)
sc query "%SVC%" >nul 2>&1 && (echo   [OK]  Windows service present) || (echo   [!!]  service not found & set PFAIL=1)
goto :eof

:showstate
echo.
echo ====================================================================
echo   %~1  ::  Mirth server state
echo ====================================================================
call :getheap
call :getsvc
call :getoom
echo    Server max heap (-Xmx) : !HEAP!
echo    Service status         : !SVCSTATE!
echo    OOM lines in mirth.log : !OOM!
goto :eof

:getheap
set "HEAP=(no -Xmx set / vmoptions missing)"
if not exist "%VM%" goto :eof
for /f "usebackq tokens=*" %%h in (`findstr /c:"-Xmx" "%VM%"`) do set "HEAP=%%h"
goto :eof

:getsvc
set "SVCSTATE=Stopped / not running"
sc query "%SVC%" 2>nul | find "RUNNING" >nul && set "SVCSTATE=Running"
goto :eof

:getoom
set "OOM=0"
if not exist "%LOG%" goto :eof
for /f %%c in ('findstr /c:"OutOfMemoryError" "%LOG%" 2^>nul ^| find /c /v ""') do set "OOM=%%c"
goto :eof

:loadkey
if defined ANTHROPIC_API_KEY goto :loadkey_done
for /f "usebackq tokens=1,* delims==" %%a in ("%~dp0.env") do (
  if /i "%%a"=="ANTHROPIC_API_KEY" set "ANTHROPIC_API_KEY=%%b"
)
:loadkey_done
set "PYTHONIOENCODING=utf-8"
goto :eof

:stopconsole
taskkill /fi "WINDOWTITLE eq MirthConsole*" /f /t >nul 2>&1
goto :eof

:elevate
rem  %1 = phase to run elevated
> "%TEMP%\mirth_elev.vbs" echo Set U = CreateObject("Shell.Application")
>>"%TEMP%\mirth_elev.vbs" echo U.ShellExecute "%~f0", "%1", "", "runas", 1
cscript //nologo "%TEMP%\mirth_elev.vbs" >nul 2>&1
del "%TEMP%\mirth_elev.vbs" >nul 2>&1
goto :eof

:end
endlocal
exit /b 0
