@echo off
REM Convenience wrapper so you can type `etfa ...` instead of the full venv path.
REM Usage:  scripts\etfa.cmd doctor
setlocal
set "ROOT=%~dp0.."
"%ROOT%\.venv\Scripts\python.exe" -m etfa %*
