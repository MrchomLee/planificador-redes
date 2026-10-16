@echo off
setlocal EnableExtensions
rem ===========================================================================================
rem  Planificador de Redes - arranque en Windows (doble clic en este archivo).
rem  Opciones: iniciar.bat --no-browser   (no abre el navegador)
rem            iniciar.bat --port 8799    (otro puerto; las apps de Meta/TikTok/Google usan 8765)
rem  La primera vez descarga Python y las librerias (unos minutos, necesita Internet).
rem ===========================================================================================
cd /d "%~dp0"
chcp 65001 >nul
title Planificador de Redes
set "PYTHONUTF8=1"

rem Sin los archivos del programa al lado (p. ej. doble clic dentro del ZIP sin extraerlo) no se puede seguir.
if not exist "pyproject.toml" goto :not_extracted
if not exist "src\planificador" goto :not_extracted

rem Usa siempre el Python que descarga uv: un Python de Microsoft Store guardaria los datos en una carpeta
rem virtualizada (y se borrarian al desinstalarlo) y el de Anaconda sin activar suele fallar al arrancar.
if not defined UV_PYTHON_PREFERENCE set "UV_PYTHON_PREFERENCE=only-managed"

rem "uv" se instala en %USERPROFILE%\.local\bin y puede no estar todavia en el PATH de esta ventana.
where uv >nul 2>nul
if errorlevel 1 if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul
if errorlevel 1 goto :no_uv

rem Windows en ARM: la libreria "cryptography" no publica paquetes ARM64, asi que se usa un Python x86_64
rem (Windows lo ejecuta emulado). UV_PYTHON_ARCH es el nombre que proponen las guias; UV_PYTHON pide lo mismo
rem de forma explicita y funciona con cualquier version de uv.
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" goto :arm64
if /i "%PROCESSOR_ARCHITEW6432%"=="ARM64" goto :arm64
goto :sync

:arm64
set "UV_PYTHON_ARCH=x86_64"
set "UV_PYTHON=cpython-3.13-windows-x86_64-none"
echo  Equipo Windows ARM64 detectado: se usará Python x86_64 (emulado).

:sync
echo.
echo  Preparando Planificador de Redes... (la primera vez tarda unos minutos)
rem --extra heic: soporte de fotos HEIC del iPhone (se anuncia en la Biblioteca).
uv sync --frozen --no-dev --extra heic
if errorlevel 1 goto :sync_failed

echo.
echo  ===================================================================
echo   Arrancando Planificador de Redes... se abrirá en tu navegador.
echo   NO CIERRES esta ventana mientras lo uses: si la cierras, las
echo   publicaciones programadas no saldrán. Para pararlo: Ctrl+C.
echo  ===================================================================
echo.
uv run --frozen --no-dev --extra heic planificador %*
if errorlevel 1 goto :run_failed
exit /b 0

:no_uv
echo.
echo  Falta "uv", el programa que instala y arranca Planificador de Redes.
echo  Solo hay que instalarlo una vez:
echo.
echo   1. Pulsa la tecla Windows, escribe PowerShell y ábrelo.
echo   2. Copia esta orden, pégala en PowerShell y pulsa Intro:
echo.
echo        winget install --id=astral-sh.uv -e
echo.
echo      Si te dice que "winget" no se reconoce, usa esta otra:
echo.
echo        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
echo.
echo   3. Cierra PowerShell y esta ventana, y vuelve a hacer doble clic en iniciar.bat.
echo.
pause
exit /b 1

:not_extracted
echo.
echo  No se encuentran los archivos de Planificador de Redes junto a iniciar.bat.
echo  Si lo has abierto desde dentro del ZIP descargado, primero extráelo: clic derecho en el
echo  archivo ZIP, "Extraer todo...", y luego haz doble clic en iniciar.bat dentro de la carpeta extraída.
echo.
pause
exit /b 1

:sync_failed
echo.
echo  No se ha podido preparar la aplicación. Revisa el mensaje de error de arriba.
echo  Si habla de descargas o de la red, comprueba tu conexión a Internet y vuelve a abrir iniciar.bat.
echo  Si el problema sigue, copia el texto de esta ventana y consúltalo en "Preguntas frecuentes" del README.
echo.
pause
exit /b 1

:run_failed
echo.
echo  Planificador de Redes se ha detenido por un error (el mensaje está arriba).
echo  Más detalles en el registro: logs\app.log dentro de la carpeta de datos
echo  (por defecto %LOCALAPPDATA%\PlanificadorRedes; otra si arrancaste con --data-dir).
echo.
pause
exit /b 1
