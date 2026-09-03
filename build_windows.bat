@echo off
setlocal EnableDelayedExpansion
title SnP Image Labeler - Build Script

echo.
echo ============================================================
echo   SnP Image Labeler - PyInstaller Build
echo ============================================================
echo.

:: All PyInstaller output gets saved here so you can review it
set LOGFILE=%~dp0build_log.txt
echo Build started > "%LOGFILE%"

:: -- 1. Python --------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.9+ and add to PATH.
    goto :fail
)
for /f "tokens=*" %%v in ('python --version') do set PY_VER=%%v
echo [OK] %PY_VER%

:: -- 2. PyQt5 ---------------------------------------------------
python -c "import PyQt5" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing PyQt5...
    python -m pip install PyQt5 --quiet
    if errorlevel 1 ( echo [ERROR] PyQt5 install failed. & goto :fail )
)
echo [OK] PyQt5 present

:: -- 3. PyInstaller ---------------------------------------------
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing PyInstaller...
    python -m pip install pyinstaller --quiet
    if errorlevel 1 ( echo [ERROR] PyInstaller install failed. & goto :fail )
)
for /f "tokens=*" %%v in ('python -m PyInstaller --version') do set PI_VER=%%v
echo [OK] PyInstaller %PI_VER%

:: -- 4. Clean ---------------------------------------------------
echo.
echo [INFO] Cleaning previous build artefacts...
if exist "dist\SnP_Image_Labeler" rd /s /q "dist\SnP_Image_Labeler"
if exist "build\SnP_Image_Labeler" rd /s /q "build\SnP_Image_Labeler"
if exist "dist\SnP_Image_Labeler" (
    echo [ERROR] Could not remove dist\SnP_Image_Labeler - close the app and retry.
    goto :fail
)
echo [OK] Clean complete

:: -- 5. Build - full output saved to log file -------------------
echo.
echo [INFO] Building EXE ^(1-3 minutes^)...
echo [INFO] Full build log: %LOGFILE%
echo.

python -m PyInstaller SnP_Image_Labeler.spec --noconfirm --clean > "%LOGFILE%" 2>&1

:: Capture the build result NOW - any command run after this (like the log
:: dump below) would overwrite errorlevel and hide a failed build.
set BUILD_RC=%ERRORLEVEL%

:: Show last 20 lines of log so errors are visible in console
echo.
echo ---- Last lines from build log ----
powershell -Command "Get-Content '%LOGFILE%' | Select-Object -Last 20"
echo ---- End of log ----
echo.

if not "%BUILD_RC%"=="0" (
    echo [ERROR] Build FAILED with exit code %BUILD_RC%. See full log: %LOGFILE%
    goto :fail
)

:: -- 6. Verify EXE ----------------------------------------------
if not exist "dist\SnP_Image_Labeler\SnP_Image_Labeler.exe" (
    echo [ERROR] EXE not created. See full log: %LOGFILE%
    goto :fail
)

:: -- 7. Done ----------------------------------------------------
for %%F in ("dist\SnP_Image_Labeler\SnP_Image_Labeler.exe") do (
    set SIZE=%%~zF
    set /a SIZE_MB=!SIZE! / 1048576
)
echo ============================================================
echo   BUILD SUCCESSFUL
echo ============================================================
echo   EXE    : dist\SnP_Image_Labeler\SnP_Image_Labeler.exe
echo   Size   : ~!SIZE_MB! MB
echo   Log    : %LOGFILE%
echo.
echo   Zip the "dist\SnP_Image_Labeler" folder to distribute.
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo ============================================================
echo   BUILD FAILED  -  see full log at:
echo   %LOGFILE%
echo ============================================================
echo.
pause
exit /b 1
