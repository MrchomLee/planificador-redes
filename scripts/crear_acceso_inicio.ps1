<#
.SYNOPSIS
    Hace que Planificador de Redes arranque solo al iniciar sesión en Windows (sin permisos de administrador).

.DESCRIPTION
    Crea el acceso directo "Planificador de Redes" en tu carpeta Inicio de Windows
    (shell:startup, la de tu usuario). Al iniciar sesión, Windows abre iniciar.bat en una ventana
    minimizada y sin abrir el navegador, así las publicaciones programadas salen a su hora aunque
    no hayas abierto la aplicación. Para usarla, abre http://localhost:8765 en el navegador.

    Con -Escritorio crea además un acceso directo en el Escritorio para abrirla con doble clic.
    Con -Quitar borra los accesos directos creados por este script.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\crear_acceso_inicio.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\crear_acceso_inicio.ps1 -Escritorio

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\crear_acceso_inicio.ps1 -Quitar
#>
[CmdletBinding()]
param(
    [switch]$Escritorio,
    [switch]$Quitar
)

$ErrorActionPreference = 'Stop'
$nombre = 'Planificador de Redes'
$raiz = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $raiz 'iniciar.bat'
$inicio = [Environment]::GetFolderPath('Startup')
$escritorioDir = [Environment]::GetFolderPath('Desktop')
$accesoInicio = Join-Path $inicio "$nombre.lnk"
$accesoEscritorio = Join-Path $escritorioDir "$nombre.lnk"

function Quitar-Acceso([string]$ruta, [string]$donde) {
    if (Test-Path -LiteralPath $ruta) {
        Remove-Item -LiteralPath $ruta -Force
        Write-Host "Quitado el acceso directo de $donde."
    } else {
        Write-Host "No había acceso directo en $donde."
    }
}

function Crear-Acceso([string]$ruta, [string]$argumentos, [int]$estiloVentana, [string]$descripcion) {
    $shell = New-Object -ComObject WScript.Shell
    $acceso = $shell.CreateShortcut($ruta)
    $acceso.TargetPath = $bat
    $acceso.Arguments = $argumentos
    $acceso.WorkingDirectory = $raiz
    $acceso.WindowStyle = $estiloVentana      # 1 = normal, 7 = minimizada
    $acceso.Description = $descripcion
    $acceso.Save()
}

if ($Quitar) {
    Quitar-Acceso $accesoInicio 'la carpeta Inicio'
    Quitar-Acceso $accesoEscritorio 'el Escritorio'
    Write-Host 'Planificador de Redes ya no arrancará solo al iniciar sesión.'
    return
}

if (-not (Test-Path -LiteralPath $bat)) {
    Write-Error "No encuentro iniciar.bat en $raiz. Ejecuta este script desde la carpeta scripts de Planificador de Redes."
    exit 1
}

Crear-Acceso $accesoInicio '--no-browser' 7 'Arranca Planificador de Redes al iniciar sesión para publicar a su hora'
Write-Host "Listo: Planificador de Redes arrancará minimizado cada vez que inicies sesión en Windows."
Write-Host "Acceso directo creado en: $accesoInicio"

if ($Escritorio) {
    Crear-Acceso $accesoEscritorio '' 1 'Abrir Planificador de Redes'
    Write-Host "Acceso directo creado en el Escritorio: $accesoEscritorio"
}

Write-Host ''
Write-Host 'Recuerda: el ordenador tiene que estar encendido (y con la sesión iniciada) para que se publique a su hora.'
Write-Host 'Para quitarlo: powershell -ExecutionPolicy Bypass -File scripts\crear_acceso_inicio.ps1 -Quitar'
