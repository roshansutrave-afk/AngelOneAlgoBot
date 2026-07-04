@echo off
:: Force the script to use its actual folder path when run as Administrator
cd /d "%~dp0"

title AngelOneAlgoBot Dependency Installer
echo Checking your setup...
echo.

:: Check if Python is genuinely installed by testing a common directory
if exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
    set "PYTHON_CMD=%LocalAppData%\Programs\Python\Python311\python.exe"
    goto install_reqs
)
if exist "%ProgramFiles%\Python311\python.exe" (
    set "PYTHON_CMD=%ProgramFiles%\Python311\python.exe"
    goto install_reqs
)

:: If not found in default paths, check current PATH but avoid execution aliases
where python >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON_CMD=python"
    goto install_reqs
)

echo [!] Python 3.11 is not detected in standard folders.
echo [*] Downloading Python official installer...
powershell -Command "(New-Object Net.WebClient).DownloadFile('https://www.python.org/ftp/python/3.11.4/python-3.11.4-amd64.exe', '%TEMP%\python_installer.exe')"

echo [*] Installing Python silently...
echo [!] If a Windows User Account Control prompt appears, click YES.
start /wait "" "%TEMP%\python_installer.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0

:: Define the newly installed path
set "PYTHON_CMD=%LocalAppData%\Programs\Python\Python311\python.exe"

:install_reqs
echo.
echo [*] Target Python executable: %PYTHON_CMD%
echo [*] Upgrading pip...
"%PYTHON_CMD%" -m pip install --upgrade pip

echo [*] Installing requirements...
if exist "requirements.txt" (
    "%PYTHON_CMD%" -m pip install -r requirements.txt
) else (
    echo [X] Error: requirements.txt not found in this folder!
    echo Current folder path is: %cd%
)

echo.
echo ====================================================
echo Setup Process Finished.
echo ====================================================
pause