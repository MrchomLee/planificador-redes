# Planificador de Redes

Aplicación **local y de un solo usuario** para planificar y publicar contenido en **Facebook, Instagram,
TikTok y YouTube** desde tu ordenador (Windows, Linux o macOS), al estilo de Metricool pero sin cuotas ni
suscripciones. Se usa desde el navegador en `http://localhost:8765` y **tus datos se quedan en tu equipo**.

- **Planificación**: calendario (mes, semana, día y lista), arrastrar para reprogramar, «Mejores horas»,
  tablero de **Ideas** y **Papelera**.
- **Editor de publicaciones** con vista previa por red, formatos (publicación, reel, historia, vídeo, Short),
  texto distinto por red, validación antes de programar y **Publicación asistida** cuando prefieres publicar tú.
- **Biblioteca** de fotos y vídeos con comprobación de compatibilidad por red.
- **Estadísticas** diarias, **Comentarios** de Facebook, Instagram y YouTube en un solo buzón, e
  **importación/exportación CSV**.

Licencia: [MIT](LICENSE).

## Índice

- [Requisitos](#requisitos)
- [Instalación en Windows en 3 pasos](#instalación-en-windows-en-3-pasos)
- [Primer uso](#primer-uso)
- [Conectar tus redes](#conectar-tus-redes)
- [Publicar](#publicar)
- [Publicación asistida](#publicación-asistida)
- [Límites importantes de cada red](#límites-importantes-de-cada-red)
- [Iniciar con Windows](#iniciar-con-windows)
- [Dónde están tus datos](#dónde-están-tus-datos)
- [Copias de seguridad](#copias-de-seguridad)
- [Preguntas frecuentes](#preguntas-frecuentes)
- [Linux y macOS](#linux-y-macos)
- [Desarrollo](#desarrollo)

## Requisitos

- Windows 10 u 11 (también funciona en Linux y macOS).
- Conexión a Internet (para instalar la primera vez y, claro, para publicar).
- **No hace falta instalar Python** ni nada más a mano: el programa **uv** descarga lo necesario.
- Para publicar en cada red necesitas crear una «app» gratuita de desarrollador (Meta, TikTok o Google). Las
  guías paso a paso están en `docs/` y también dentro de la aplicación (**Cuentas conectadas › Guía paso a
  paso**).
- El ordenador tiene que estar **encendido** y Planificador **abierto** a la hora de cada publicación (salvo
  las que delegues en Facebook o YouTube, ver más abajo).

## Instalación en Windows en 3 pasos

**1. Instala «uv»** (solo la primera vez). Pulsa la tecla Windows, escribe **PowerShell**, ábrelo, pega esta
orden y pulsa Intro:

```powershell
winget install --id=astral-sh.uv -e
```

Si te dice que `winget` no se reconoce, usa esta otra:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Cierra PowerShell al terminar.

**2. Descarga Planificador.** Descarga el proyecto como ZIP (en GitHub: botón **Code › Download ZIP**) y
descomprímelo en una carpeta tuya, por ejemplo `Documentos\PlanificadorRedes`. Antes de descomprimir, haz
clic derecho en el ZIP › **Propiedades** y, si aparece, marca **Desbloquear** y pulsa **Aceptar** (así Windows
no pondrá pegas al abrir los archivos).

**3. Haz doble clic en `iniciar.bat`.** La primera vez tarda unos minutos (descarga Python y las librerías);
las siguientes, unos segundos. Se abrirá el navegador en <http://localhost:8765>.

- Si Windows muestra **«Windows protegió su PC»**, pulsa **Más información › Ejecutar de todas formas**.
- **No cierres la ventana negra** mientras uses Planificador: si la cierras, la aplicación se detiene y las
  publicaciones programadas no saldrán. Puedes minimizarla.
- En ordenadores **Windows con procesador ARM** (por ejemplo, algunos Surface o portátiles Snapdragon),
  `iniciar.bat` lo detecta y usa automáticamente una versión de Python x86_64 (emulada), porque una de las
  librerías no existe para ARM.

## Primer uso

1. Al abrir Planificador se crea una marca llamada «Mi marca». Cámbiale el nombre, el color y la **zona
   horaria** en **☰ › Marcas** (las horas de publicación siempre son las de la zona de la marca).
2. ¿Quieres probar antes de conectar nada? En **☰ › Ajustes › Publicación** activa **Modo prueba** y conecta la
   red **Demo** en **☰ › Cuentas conectadas**: simula publicaciones, estadísticas y comentarios.
3. Conecta tus redes (siguiente apartado) y sube algunas fotos o vídeos a la **Biblioteca**.
4. Pulsa **Nueva publicación** (o la tecla **N**) y programa tu primera publicación.

Recorrido completo por la aplicación: [docs/guia-uso.md](docs/guia-uso.md).

## Conectar tus redes

Cada red necesita una app de desarrollador **tuya** (gratis, se hace una vez). Copia sus credenciales en
**☰ › Ajustes › Credenciales** y pulsa **Conectar** en **☰ › Cuentas conectadas**.

| Red | Guía | Qué necesitas | Dirección de redirección que hay que registrar |
|---|---|---|---|
| Facebook e Instagram | [docs/guia-meta.md](docs/guia-meta.md) | App de Meta tipo **Negocios** | `http://localhost:8765/conexiones/callback/meta` |
| TikTok | [docs/guia-tiktok.md](docs/guia-tiktok.md) | App de TikTok en **Sandbox** con plataforma **Desktop** (pide verificar una web tuya; vale una gratis de GitHub Pages) | `http://localhost:8765/conexiones/callback/social1` |
| YouTube | [docs/guia-youtube.md](docs/guia-youtube.md) | Proyecto de Google Cloud con cliente **Aplicación web** | `http://localhost:8765/conexiones/callback/google` |

En TikTok, marca en **Ajustes › Credenciales › TikTok** exactamente los permisos (scopes) que tiene tu app: si
Planificador pide uno que tu app no tiene, TikTok rechaza la conexión con el error «scope».

Las contraseñas de tus redes nunca pasan por Planificador: te conectas en la página oficial de cada red. Los
tokens de acceso y las claves secretas se guardan **cifrados** en tu ordenador.

## Publicar

1. **Nueva publicación**: elige las redes (y el formato de cada una), escribe el texto, añade fotos o vídeos de
   la Biblioteca y revisa la vista previa.
2. Si hay algún problema (texto demasiado largo, vídeo no compatible, falta un dato obligatorio…), aparece en
   rojo y el botón **Programar** se desactiva hasta que lo corrijas.
3. Elige la fecha y la hora y pulsa **Programar**. También puedes **Publicar ahora**, **Guardar borrador** o
   **Guardar como idea** (flecha junto a Programar).
4. A la hora indicada Planificador publica solo y la publicación pasa a **Publicado** en el calendario.

Para cargar muchas publicaciones de golpe usa **☰ › Importar o exportar CSV**.

## Publicación asistida

Es el modo «te aviso y publicas tú» (en Metricool, «modo manual»). Se usa cuando apagas **Autopublicar** en una
red, para las fotos de Instagram si no activaste la URL pública, y por defecto en YouTube hasta que hagas la
prueba de bloqueo. A la hora indicada:

1. Recibes un aviso en el escritorio y en la campana de Planificador.
2. Abres la ventana de **Publicación asistida**: **Copiar texto**, **Descargar** los archivos y un botón para
   abrir la red.
3. Publicas en la red y pulsas **Marcar como publicado**.

En TikTok tienes además **Enviar a la app de TikTok como borrador**: el vídeo llega a tu app de TikTok y lo
terminas allí (música, efectos, visibilidad).

## Límites importantes de cada red

**Facebook**

- Solo **Páginas** de Facebook (no perfiles personales ni grupos), y necesitas **control total** de la Página.
- Tu app de Meta debe estar en modo **Activo** (*Live*); si sigue en **Desarrollo**, las publicaciones **solo
  las ves tú**. Ver [guía de Meta, paso 9](docs/guia-meta.md#9-pasa-la-app-a-modo-activo-muy-importante-para-facebook).
- **Programar en Facebook** (opcional) deja la publicación programada en Facebook: sale aunque tu ordenador
  esté apagado.

**Instagram**

- Solo cuentas **profesionales** (empresa o creador) **vinculadas a una Página de Facebook**. Las historias,
  solo en cuentas de **empresa**.
- Las **fotos** necesitan la **URL pública** (**Ajustes › URL pública para Instagram**): Instagram las descarga
  desde Internet. Los vídeos y reels no la necesitan.
- Instagram no permite dejar publicaciones programadas en sus servidores: el ordenador tiene que estar
  encendido a la hora. Máximo 100 publicaciones cada 24 horas por cuenta.

**TikTok**

- Mientras tu app no pase la auditoría de TikTok, solo se puede publicar como **«Solo yo»** (privado); luego
  cambias la visibilidad en la app de TikTok. Puede que TikTok exija además que la cuenta sea privada. La
  alternativa es **Enviar a la app de TikTok como borrador** (máximo 5 borradores pendientes cada 24 horas).
- Si tu app no tiene **Direct Post** (permiso `video.publish`, a menudo no disponible en el Sandbox), los vídeos
  solo se pueden enviar como borrador a la app de TikTok; si no tiene `video.upload`, solo publicación directa.
  La tarjeta de **Cuentas conectadas** muestra qué modos tienes.
- Las estadísticas de TikTok necesitan los permisos opcionales `user.info.stats` y `video.list`.
- Solo **vídeos** (las fotos de TikTok necesitan un dominio público verificado).
- La API de TikTok **no** permite leer ni responder comentarios.

**YouTube**

- Los proyectos de Google sin auditar pueden ver sus subidas **bloqueadas como privadas** (sin posibilidad de
  hacerlas públicas). Por eso YouTube empieza en **Publicación asistida**. La **Prueba de bloqueo** (**Cuentas
  conectadas › YouTube**) comprueba con un clip de 2 segundos si tu proyecto está afectado; si sale
  «desbloqueado», puedes pasar a publicación **Automática (API)**.
- Publica tu app de Google **«En producción»**: en modo **Prueba** tendrías que reconectar cada 7 días.
- Cuota diaria de unas 100 subidas por proyecto (más que suficiente para una persona).

## Iniciar con Windows

Para que Planificador arranque solo al iniciar sesión (minimizado y sin abrir el navegador) y las publicaciones
salgan aunque olvides abrirlo:

1. Abre la carpeta de Planificador, entra en `scripts`, haz clic derecho en `crear_acceso_inicio.ps1` y elige
   **Ejecutar con PowerShell**. O, desde PowerShell en la carpeta de Planificador:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\crear_acceso_inicio.ps1 -Escritorio
   ```

   (`-Escritorio` crea además un icono en el Escritorio para abrirlo con doble clic.)
2. No necesita permisos de administrador: crea un acceso directo en tu carpeta **Inicio** de Windows
   (`shell:startup`).
3. Para quitarlo: el mismo script con `-Quitar`.

Con el arranque automático, abre <http://localhost:8765> en el navegador cuando quieras usarlo (añádelo a
favoritos). El ordenador sigue teniendo que estar **encendido** a la hora de publicar: si se suspende, la
publicación sale al despertar si no ha pasado la **Ventana de gracia** (**Ajustes › Publicación**, 120
minutos por defecto).

## Dónde están tus datos

Todo se guarda en una carpeta de tu usuario (no en la carpeta del programa):

- **Windows**: `%LOCALAPPDATA%\PlanificadorRedes` (escríbelo en la barra de direcciones del Explorador).
- **Linux/macOS**: `~/.local/share/planificador-redes`.
- La ruta exacta aparece en **☰ › Ajustes › Acerca de** (botón **Abrir carpeta**).

Dentro están la base de datos (`planificador.sqlite3`), tus fotos y vídeos (`media`), los registros (`logs`) y,
si se usa, el programa `cloudflared` (`bin`). La clave que cifra tus tokens se guarda en el **Administrador de
credenciales de Windows** (entrada `planificador-redes`); si no está disponible, en el archivo `secret.key` de
la carpeta de datos.

## Copias de seguridad

1. **Cierra Planificador** (cierra la ventana negra o pulsa Ctrl+C en ella).
2. Copia **toda la carpeta de datos** (ver el apartado anterior) a un disco externo o a la nube.

Para restaurar, cierra Planificador y vuelve a poner la carpeta en su sitio. Si restauras en **otro
ordenador**, la clave de cifrado no viaja con la copia (está en el Administrador de credenciales): tus
publicaciones, ideas, medios y estadísticas se conservan, pero tendrás que volver a pegar las claves secretas en
**Ajustes › Credenciales** y **reconectar** tus cuentas.

Para probar cosas sin tocar tus datos, puedes arrancar con otra carpeta:
`iniciar.bat --data-dir C:\ruta\a\otra\carpeta`.

## Preguntas frecuentes

**¿Tengo que dejar el ordenador encendido?** Sí, a la hora de cada publicación, con Planificador abierto. Las
excepciones son **Programar en Facebook** y **Programar en YouTube** (con publicación automática), que dejan la
publicación programada en la propia red.

**¿Qué pasa si el ordenador estaba apagado a la hora programada?** Al volver a abrir Planificador, la
publicación sale si no ha pasado la **Ventana de gracia** (120 minutos por defecto). Si ha pasado más tiempo,
queda como **Perdido** y te avisamos para que elijas **Publicar ahora**, **Reprogramar** o **Descartar**.

**Mis publicaciones de Facebook solo las veo yo.** Tu app de Meta está en modo **Desarrollo**: pásala a
**Activo** ([guía de Meta](docs/guia-meta.md)).

**Instagram no me deja programar fotos.** Activa **Ajustes › URL pública para Instagram › Túnel temporal de
Cloudflare**.

**TikTok publica mis vídeos en privado.** Es la norma de TikTok para apps sin auditar. Cambia la visibilidad en
la app de TikTok o usa **Enviar a la app de TikTok como borrador**.

**TikTok muestra un error «client_key» o «scope» al conectar.** «client_key»: en el **Sandbox** de tu app falta
Login Kit o la plataforma Desktop, o no pulsaste **Apply changes**. «scope»: desmarca en **Ajustes ›
Credenciales › TikTok** los permisos que tu app no tiene. Detalles en la
[guía de TikTok](docs/guia-tiktok.md#16-problemas-frecuentes).

**Un vídeo de YouTube se quedó privado y no puedo cambiarlo.** YouTube lo bloqueó porque tu proyecto no está
auditado. Bórralo, súbelo desde YouTube Studio y mantén YouTube en **Publicación asistida**.

**¿Puedo usarlo desde el móvil o desde otro ordenador?** No: por seguridad solo responde en el propio ordenador
(`localhost`). Desde el móvil recibirás las notificaciones de las propias redes.

**¿Es seguro?** La aplicación no es accesible desde Internet ni desde tu red local. Tus claves y tokens se
guardan cifrados. Si compartes el ordenador, pon una contraseña en **Ajustes › Seguridad**. La URL pública de
Instagram solo expone, durante unos minutos, la foto que Instagram tiene que descargar.

**He olvidado la contraseña de la aplicación.** Haz doble clic en `quitar-contrasena.bat` (en la carpeta del
programa; si usas otra carpeta de datos, `quitar-contrasena.bat --data-dir RUTA`). Funciona aunque Planificador
esté abierto: en unos segundos podrás entrar sin contraseña y poner otra en **Ajustes › Seguridad**. Tus
publicaciones, cuentas y ajustes no se tocan. En Linux/macOS: `uv run planificador --quitar-contrasena`.

**Sale «puerto ocupado» o no se abre.** Puede que Planificador ya esté abierto (mira la barra de tareas) o que
otro programa use el puerto 8765. Puedes arrancar en otro puerto con `iniciar.bat --port 8799`, pero entonces
las direcciones de redirección de tus apps cambian (`http://localhost:8799/...`) y tendrás que registrarlas
también.

**¿Cómo actualizo Planificador?** Cierra la aplicación, descarga la versión nueva y sustituye los archivos de la
carpeta del programa (tus datos están en otra carpeta y no se tocan). Vuelve a abrir `iniciar.bat`.

**¿Cómo lo desinstalo?** Quita el arranque automático (`crear_acceso_inicio.ps1 -Quitar`), borra la carpeta del
programa y, si quieres borrar también tus datos, la carpeta de datos y, en el Administrador de credenciales
(Credenciales de Windows), la entrada de `planificador-redes`. Desconecta antes tus cuentas en **Cuentas conectadas** para revocar los permisos.

**Algo ha fallado.** Mira el detalle de la publicación (pulsa sobre ella) y **Ajustes › Acerca de › Ver
registros**. Los errores de cada red vienen explicados en español.

## Linux y macOS

Instala uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`), descarga el proyecto y ejecuta:

```bash
./iniciar.sh
```

Admite las mismas opciones (`--no-browser`, `--port`, `--data-dir`). Para arrancarlo al iniciar sesión, añade
`iniciar.sh --no-browser` a las aplicaciones de inicio de tu escritorio.

## Desarrollo

Requisitos: **Python 3.12 o superior** y [uv](https://docs.astral.sh/uv/). No hace falta Node: las
librerías del navegador (htmx, Alpine.js, FullCalendar, Chart.js, iconos) ya están incluidas en
`src/planificador/static/vendor/`.

```bash
uv sync                      # crea .venv e instala dependencias (incluidas las de desarrollo)
uv run planificador          # arranca en http://localhost:8765 y abre el navegador
```

Opciones de `planificador`: `--port 8799`, `--no-browser`, `--data-dir <carpeta>`. Solo se ejecuta una
instancia por carpeta de datos: si ya está abierta, un segundo arranque solo abre el navegador. `iniciar.bat` e
`iniciar.sh` hacen `uv sync --frozen --no-dev --extra heic` y `uv run --frozen --no-dev --extra heic planificador`
con `UV_PYTHON_PREFERENCE=only-managed` (siempre el Python que descarga uv, nunca el de Microsoft Store).

Carpeta de datos (base de datos SQLite, medios, registros en `logs/app.log`): `PLANIFICADOR_DATA_DIR` o
`--data-dir`; por defecto `%LOCALAPPDATA%\PlanificadorRedes` en Windows y `~/.local/share/planificador-redes`
en Linux/macOS. Para probar sin tocar tus datos:

```bash
PLANIFICADOR_DATA_DIR=/tmp/planificador-dev uv run planificador --no-browser --port 8799
```

Otras variables útiles: `PLANIFICADOR_NO_SCHEDULER=1` (no arranca el programador en segundo plano) y
`PLANIFICADOR_NO_KEYRING=1` (guarda la clave de cifrado en `secret.key` en vez del almacén del sistema; solo en
una instalación nueva). Si el almacén de credenciales no responde y ya hay secretos guardados, Planificador no
arranca en vez de crear otra clave (los perderías); `PLANIFICADOR_RESET_MASTER_KEY=1` empieza con una clave nueva
si la original se ha perdido. `PLANIFICADOR_TIMEZONE=America/Mexico_City` fija la zona horaria que se usa en el
primer arranque (por defecto, la de este ordenador; `off` usa Europe/Madrid).

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

### Documentación

- `docs/guia-uso.md`, `docs/guia-meta.md`, `docs/guia-tiktok.md`, `docs/guia-youtube.md`: guías de usuario
  (también se muestran dentro de la aplicación en `/ayuda/<nombre>`).
- `docs/dev/SPEC.md`: especificación técnica (contrato entre módulos).
- `docs/research/`: datos verificados de las API de Meta, TikTok y YouTube y de la arquitectura local.
