@echo off
rem Runs Blender in the background (no window) on render_showcase.py.
rem Nothing is installed; results go to the "results" folder next to this file.
setlocal
set "BLENDER="
if exist "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" set "BLENDER=C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
if not defined BLENDER for /d %%D in ("C:\Program Files\Blender Foundation\Blender*") do if exist "%%D\blender.exe" set "BLENDER=%%D\blender.exe"
if not defined BLENDER if exist "C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe" set "BLENDER=C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe"
if not defined BLENDER (
  echo Could not find blender.exe. Edit this file and set BLENDER to its full path.
  pause
  exit /b 1
)
echo Using "%BLENDER%"
if not exist "%~dp0results" mkdir "%~dp0results"
"%BLENDER%" -b --factory-startup --python "%~dp0render_showcase.py" > "%~dp0results\showcase_output.txt" 2>&1
echo.
echo Finished. Images are in "%~dp0results\showcase"
echo You can close this window and tell Claude it is done.
pause
