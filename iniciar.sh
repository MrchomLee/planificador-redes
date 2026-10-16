#!/usr/bin/env sh
# Planificador de Redes — arranque en Linux y macOS.
#   ./iniciar.sh                 abre http://localhost:8765 en el navegador
#   ./iniciar.sh --no-browser    no abre el navegador
#   ./iniciar.sh --port 8799     otro puerto (las apps de Meta, TikTok y Google están configuradas con 8765)
# La primera vez descarga Python y las librerías (unos minutos, necesita Internet).
set -eu

cd "$(dirname "$0")"
export PYTHONUTF8=1

if [ ! -f pyproject.toml ] || [ ! -d src/planificador ]; then
  echo ""
  echo "  No se encuentran los archivos de Planificador de Redes junto a iniciar.sh."
  echo "  Extrae primero el ZIP descargado y ejecuta ./iniciar.sh dentro de la carpeta extraída."
  exit 1
fi

# Usa siempre el Python que descarga uv (no un Python del sistema o de Anaconda): resultado predecible.
: "${UV_PYTHON_PREFERENCE:=only-managed}"
export UV_PYTHON_PREFERENCE

# uv se instala en ~/.local/bin, que puede no estar todavía en el PATH de esta terminal.
if ! command -v uv >/dev/null 2>&1 && [ -x "$HOME/.local/bin/uv" ]; then
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi

if ! command -v uv >/dev/null 2>&1; then
  echo ""
  echo "  Falta \"uv\", el programa que instala y arranca Planificador de Redes."
  echo "  Instálalo una sola vez con esta orden y vuelve a ejecutar ./iniciar.sh:"
  echo ""
  echo "      curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo ""
  exit 1
fi

echo "  Preparando Planificador de Redes... (la primera vez tarda unos minutos)"
# --extra heic: soporte de fotos HEIC del iPhone (se anuncia en la Biblioteca).
if ! uv sync --frozen --no-dev --extra heic; then
  echo ""
  echo "  No se ha podido preparar la aplicación. Revisa el mensaje de error de arriba; si habla de descargas"
  echo "  o de la red, comprueba tu conexión a Internet y vuelve a intentarlo."
  exit 1
fi

echo "  Arrancando Planificador de Redes..."
exec uv run --frozen --no-dev --extra heic planificador "$@"
