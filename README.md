# Planificador de Redes

Aplicación web **local y de un solo usuario** para planificar y publicar contenido en Facebook, Instagram,
TikTok y YouTube desde tu ordenador (Windows, Linux o macOS). Se abre en el navegador en
`http://localhost:8765` y tus datos se guardan solo en tu equipo.

> La guía de uso completa (instalación en Windows, creación de las apps de Meta, TikTok y Google) llegará más adelante.

Licencia: [MIT](LICENSE).

## Desarrollo

Requisitos: **Python 3.12 o superior** y [uv](https://docs.astral.sh/uv/). No hace falta Node: las
librerías del navegador (htmx, Alpine.js, FullCalendar, Chart.js, iconos) ya están incluidas en
`src/planificador/static/vendor/`.

```bash
uv sync                      # crea .venv e instala dependencias (incluidas las de desarrollo)
uv run planificador          # arranca en http://localhost:8765 y abre el navegador
```

Opciones de `planificador`: `--port 8799`, `--no-browser`, `--data-dir <carpeta>`. Solo se ejecuta una
instancia por carpeta de datos: si ya está abierta, un segundo arranque solo abre el navegador.

Carpeta de datos (base de datos SQLite, medios, registros en `logs/app.log`): `PLANIFICADOR_DATA_DIR` o
`--data-dir`; por defecto `%LOCALAPPDATA%\PlanificadorRedes` en Windows y `~/.local/share/planificador-redes`
en Linux/macOS. Para probar sin tocar tus datos:

```bash
PLANIFICADOR_DATA_DIR=/tmp/planificador-dev uv run planificador --no-browser --port 8799
```

Otras variables útiles: `PLANIFICADOR_NO_SCHEDULER=1` (no arranca el programador en segundo plano) y
`PLANIFICADOR_NO_KEYRING=1` (guarda la clave de cifrado en `secret.key` en vez del almacén del sistema).

### Pruebas y calidad

```bash
uv run pytest                # pruebas unitarias y de rutas (sin red, cada prueba con su carpeta temporal)
uv run ruff check            # lint
uv run ruff format --check   # formato
```

Pruebas de navegador (Playwright, marcadas `e2e`, no se ejecutan por defecto):

```bash
uv run playwright install chromium     # una vez
uv run pytest -m e2e
```

Si ya tienes Chromium instalado, puedes indicarlo con `PLAYWRIGHT_CHROMIUM_EXECUTABLE=/ruta/a/chrome`.
`PLANIFICADOR_E2E_SHOTS=<carpeta>` guarda capturas (tema claro y oscuro).

### Librerías del navegador

`python scripts/vendor_assets.py --check` comprueba sin red que los archivos incluidos coinciden con las
versiones fijadas; sin `--check` los vuelve a descargar de registry.npmjs.org verificando su integridad.

### Documentación técnica

- `docs/dev/SPEC.md`: especificación (contrato entre módulos).
- `docs/research/`: datos verificados de las API de Meta, TikTok y YouTube y de la arquitectura local.
