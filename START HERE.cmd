@echo off
setlocal DisableDelayedExpansion
if exist "%~dp0Support\Setup.ps1" goto ready
if not "%~1"=="" goto missing
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -STA -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('Keep START HERE.cmd and the Support folder together, then try again.','Location Map setup') | Out-Null"
:missing
echo The Support folder is missing. Keep it beside START HERE.cmd, then try again.
exit /b 1
:ready
if not "%~1"=="" goto arguments
start "" "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File "%~dp0Support\Setup.ps1"
exit /b
:arguments
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0Support\Setup.ps1" %*
exit /b %ERRORLEVEL%
