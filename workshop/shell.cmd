@echo off
rem A workshop terminal on Windows: Git Bash in the console's copy of the repository, with what the day's commands
rem need (workshop/stage-shell.sh). From the repository root, in the VS Code terminal (PowerShell) or cmd:
rem
rem     workshop\shell
rem
rem It takes the Git Bash of the portable git the setup brought (.tools\git), else the installed Git's. "exit" leaves it.
setlocal
cd /d "%~dp0.."
set "TS_BASH="
if exist ".tools\git\bin\bash.exe" set "TS_BASH=%CD%\.tools\git\bin\bash.exe"
if not defined TS_BASH for /f "delims=" %%g in ('where git 2^>nul') do if not defined TS_BASH (
    if exist "%%~dpg..\bin\bash.exe" set "TS_BASH=%%~dpg..\bin\bash.exe"
    if exist "%%~dpgbash.exe" set "TS_BASH=%%~dpgbash.exe"
)
if not defined TS_BASH if exist "%ProgramFiles%\Git\bin\bash.exe" set "TS_BASH=%ProgramFiles%\Git\bin\bash.exe"
if not defined TS_BASH (
    echo No Git Bash here. Run the setup first: powershell -ExecutionPolicy Bypass -File setup.ps1
    exit /b 1
)
"%TS_BASH%" --rcfile workshop/stage-shell.sh -i
