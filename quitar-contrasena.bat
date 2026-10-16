@echo off
setlocal EnableExtensions
rem ===========================================================================================
rem  Planificador de Redes - quitar la contraseña de acceso (si la has olvidado).
rem  Doble clic en este archivo. Después entra en la aplicación sin contraseña y, si quieres,
rem  pon otra en Ajustes > Seguridad. Si usas otra carpeta de datos: quitar-contrasena.bat --data-dir RUTA
rem ===========================================================================================
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"

if exist ".venv\Scripts\planificador.exe" goto :run
echo.
echo  Primero arranca Planificador de Redes una vez con iniciar.bat.
echo.
pause
exit /b 1

:run
echo.
".venv\Scripts\planificador.exe" --quitar-contrasena %*
echo.
pause
