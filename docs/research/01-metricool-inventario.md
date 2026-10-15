# 01 — Inventario UX de Metricool (app.metricool.com)

> Fecha: 2026-10-15 · Proyecto: **Planificador de Redes** (app local, monousuario, UI en español)
> Objetivo: inventario exhaustivo de pantallas y comportamientos de la web app de Metricool para replicar su **UX** (flujos, estructura, estados), **nunca** su marca, logotipo, nombres de producto registrados ni su identidad visual.

---

## 0. Alcance, método y fiabilidad

- **Sin acceso directo a la app.** En este entorno `WebFetch` falla por DNS para `help.metricool.com` y `metricool.com` (`getaddrinfo ENOTFOUND help.metricool.com`), y este subagente **no dispone de herramientas de navegador** (el usuario indica que activó la extensión de Claude en su navegador, pero sus herramientas no estaban expuestas en esta sesión). Toda la información procede de **WebSearch** sobre el centro de ayuda oficial (ES/EN/PT/IT/NL), el blog/changelog de Metricool y reseñas de terceros, más una lectura cruzada de los repos de referencia (postiz-app, openpost, brightbean-studio) para comprobar qué opciones del compositor existen realmente en las APIs.
- **Recomendación:** hacer una segunda pasada **con Claude in Chrome** sobre una cuenta Free de Metricool para verificar todo lo marcado como **UNVERIFIED** (etiquetas exactas, orden de pestañas, layout del compositor, contador de posts del plan Free).
- Convenciones: `[ES]`/`[EN]` = idioma del artículo de ayuda citado. **UNVERIFIED** = no confirmado por fuente oficial o fuentes en conflicto. "Help" = help.metricool.com.
- Metricool cambia la UI casi cada mes (changelog mensual en `metricool.com/product-updates-<mes>-<año>/`). Este inventario refleja el estado conocido hasta **septiembre 2026**.

---

## 1. Layout global

### 1.1 Barra superior (menú principal)

Según Help "Get to know Metricool" (https://help.metricool.com/get-to-know-metricool-p0zdh) el menú superior tiene **6 áreas principales**:

| Sección (EN) | Etiqueta ES probable | Qué contiene |
|---|---|---|
| **Analytics** | Analítica | Métricas por red conectada; incluye **Hashtag Tracker**, **Competidores** y generación de informes rápidos. Sidebar izquierda con "Resumen" + redes conectadas. |
| **Reporting** | Informes / Reporting | Desde mayo 2026 agrupa **Reports**, **Campaign Dashboards**, **Metricool Studio** (vistas IA) y **Data Studio/Looker** (https://metricool.com/product-updates-may-2026/). |
| **Inbox** | Bandeja de entrada | Comentarios, DMs y reseñas. |
| **Planning** | Planificación | Calendario, lista, biblioteca, autolistas, publicaciones eliminadas. |
| **SmartLinks** | SmartLinks | Página "link in bio". |
| **Ads** | Ads | Campañas Meta/Google (gestión) y TikTok Ads (monitorización). |

- **Selector de marca**: desplegable junto al **nombre de la marca** (arriba). "To change the Brand, click on the drop-down next to the brand name." Incluye atajo **"Añadir marca"** (solo planes de pago).
- **Menú ☰ (arriba a la derecha)**: Conexiones · Ajustes de marca · Gestión de usuarios · Planes y facturación · **Solicitudes de revisión** (Review requests, antes "My Tasks") · Idioma · Ajustes de cuenta (Help "Get to know Metricool"; https://help.metricool.com/how-to-manage-content-approval-from-review-requests-formerly-my-tasks-c1xn8).
- Entrada **"What's new / Novedades"** en el menú web (feb 2026, https://metricool.com/product-updates-february-2026/).
- **Visibilidad por permisos**: en marcas compartidas, si el usuario no tiene permiso de Inbox/Ads/SmartLinks la sección **desaparece** del menú (Help "Shared Brands", https://help.metricool.com/shared-brands-in-metricool-uf4oz).
- **Competidores** y **Hashtags** NO son secciones de primer nivel: viven dentro de Analítica (Help "Competitor Analysis", https://help.metricool.com/how-to-add-competitors-in-metricool-sgjjg).
- Septiembre 2025: la sidebar de Analítica dejó de mostrar "+" para redes no conectadas; **solo lista redes conectadas** (https://metricool.com/product-updates-september-2025/).
- Reseñas: Capterra menciona "áreas poco intuitivas" y "diseño algo anticuado" (https://www.capterra.com/p/203702/Metricool/).

### 1.2 Concepto de "Marca"

- Una **marca** es el contenedor de un negocio/cliente/proyecto: "una carpeta mágica donde agrupas todo lo digital de un cliente" (Help [ES] https://help.metricool.com/es/que-es-una-marca-en-metricool-y-como-funciona-5l78b).
- **Una cuenta por red por marca** (1 Página de Facebook, 1 Instagram profesional, 1 TikTok, …) + 1 web + 1 cuenta de Google Ads, Facebook Ads y TikTok Ads. Si hay una Página de FB por país, cada una necesita su propia marca (Help [ES] "Cómo conectar las redes sociales", https://help.metricool.com/es/como-conectar-las-redes-sociales-a-metricool-qrkvn).
- **Ajustes de marca**: nombre + logo (imagen). **Zona horaria por marca** se fija desde el planificador (ver §2.9).
- Plan Free: **1 marca**. Nº de marcas depende del plan, no del nº de conexiones.
- Roles en marcas compartidas: Admin, Manager, Editor, Analyst (solo lectura), Client (revisor) (Help "User management, team roles", https://help.metricool.com/user-management-team-roles-and-permissions-in-metricool-ovjqe; Help aprobación [ES]). Permiso "Analítica resumida" añadido oct 2025 (https://metricool.com/product-updates-october-2025/).
- Funcionalidad multi-marca: **duplicar en bloque publicaciones a otras marcas** desde lista o calendario ("Duplicate to another brand", ene 2026, https://metricool.com/product-updates-january-2026/); importar CSV a varias marcas a la vez (columna "Brand Name").

### 1.3 Panel de Conexiones (por marca)

- Acceso: **clic en el nombre de la marca (arriba a la derecha) > Conexiones** (Help [ES] qrkvn). Lista de plataformas con botón **"Conectar"**; se elige la red, se hace login OAuth y se aceptan permisos. Metricool remarca que no pide contraseñas.
- **TikTok**: al conectar se elige **tipo de cuenta (personal / business)** y se pulsa "Conectar una cuenta de TikTok" (Help [ES] https://help.metricool.com/es/como-conectar-tiktok-a-metricool-0ea7e). Cambiar luego de personal↔business puede ocultar datos previos.
- **Instagram — dos tipos de conexión** (Help "Access to Instagram from Metricool: connection types and differences", https://help.metricool.com/access-to-instagram-from-metricool-connection-types-and-differences-kcsrb):

  | Capacidad | Vía Facebook (recomendada) | Vía login de Instagram |
  |---|---|---|
  | Analítica completa | Sí | Básica |
  | Competidores, etiquetado de productos | Sí | No |
  | Mejores horas | Datos de la audiencia | Estimación genérica |
  | Programar Stories, primer comentario, Inbox | Sí | Sí |
  | Boost (requiere Meta Ads) | Sí | No |

  Requisito: cuenta profesional vinculada a una Página de FB. Cambiar de método crea **otra conexión con historial separado**. (Nota: Metricool describe la conexión directa como "Instagram Basic Display API", lo cual está **desfasado**: esa API se retiró el **4‑dic‑2024** — VERIFICADO: docs de Meta "Instagram Basic Display API" y brightbean `providers/instagram_login.py:8`; hoy es "Instagram API with Instagram Login" (scopes `instagram_business_*`, cuentas Business o Creator). Cómo lo etiqueta hoy la UI de Metricool: UNVERIFIED.)
- **Duración de tokens** (Help [ES] https://help.metricool.com/es/duracion-de-las-conexiones-y-tokens-en-redes-sociales-con-metricool-lnfvm): Meta (FB/IG/Threads) **60 días**, TikTok **365 días**. *(Verificación — lo que hay debajo en las APIs: Meta: token de usuario de larga duración ≈60 días vía `grant_type=fb_exchange_token`; el **token de Página** obtenido con él (`/me/accounts`) **no tiene caducidad programada** (`expires_at: 0`) pero se invalida si el usuario cambia la contraseña, pierde el rol en la Página o revoca la app. IG con Instagram Login: token largo ≈60 días, **renovable** con `GET graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token` (brightbean `providers/instagram_login.py:215-278`). TikTok: **access token 24 h** + **refresh token 365 días**, renovable sin intervención del usuario; el refresh token puede **rotar** y hay que guardar el nuevo (developers.tiktok.com "oauth-user-access-token-management"; openpost `platform/tiktok.go:124-176`). Los "365 días" de Metricool son, por tanto, la vida del refresh token.)* Al caducar o ser revocado (cambio de contraseña, login sospechoso, spam) hay que **reconectar** desde Conexiones > "Conectar". Se envía **notificación de desconexión** (una por marca si la cuenta está en varias). Existe artículo "refrescar la conexión" para cuentas que aparecen conectadas pero necesitan renovarse (Help [ES] https://help.metricool.com/es/se-han-desconectado-las-redes-sociales-como-reconectarlas-en-metricool-thnrd).
- Oct 2025: el cliente puede conectar sus redes a una marca mediante **enlace compartido sin registrarse** (plan Advanced).
- Errores de conexión documentados: "400 Session Invalid", `OAuthException` por "future activity" desactivada en el Centro de cuentas de Meta, "Application does not have permission for this action" (Help "Instagram posting errors & troubleshooting", https://help.metricool.com/en/article/instagram-posting-errors-troubleshooting-1j9yv87/).

**Implicaciones para Planificador de Redes**
- Mantener el concepto "marca" como **espacio de trabajo** opcional (MVP: una marca implícita; el modelo de datos ya con `brand_id` para no migrar después).
- Pantalla **Conexiones** con una tarjeta por red: avatar + nombre de cuenta, tipo (Página FB / IG Business-Creator / TikTok personal-business), **estado** (Conectada · Caduca en N días · Caducada · Error de permisos), fecha de expiración del token, permisos concedidos, botones **Reconectar / Desconectar / Probar**. Banner global rojo si alguna conexión ha caducado y hay posts pendientes que dependen de ella.
- En nuestra app las credenciales de la app de desarrollador (Meta/TikTok client id/secret) van en **Ajustes > Apps de desarrollador**, separado de Conexiones.

---

## 2. Planificación (Planner)

### 2.1 Estructura de la sección

- Pestañas superiores dentro de Planificación (nombres aproximados; **UNVERIFIED** orden exacto): **Calendario**, **Lista** (con "Acciones en bloque"), **Biblioteca** (pestaña añadida encima del planificador en mayo 2026; antes icono de bombilla), **Autolistas**, **Publicaciones eliminadas** (ago 2025). Fuentes: Help aprobación [ES] ("Planificación > Lista > Acciones masivas", https://help.metricool.com/es/como-aprobar-o-rechazar-una-publicacion-programada-en-metricool-ku1nw); blog [ES] "pestaña Listado… botón Acciones en bloque" (https://metricool.com/es/publicacion-masiva-redes-sociales/); https://metricool.com/product-updates-may-2026/; https://metricool.com/product-updates-august-2025/; tutorial dualthink ("la bombilla es la biblioteca de publicaciones").
- Barra superior del calendario: botón **"+ Crear publicación"**, navegación ← → con **salto a día/mes/año** (feb 2025, https://metricool.com/product-updates-february-2025/), **hora actual + zona horaria (desplegable)**, botón **"Mejores horas"** (arriba a la derecha), **filtros** (con badge numérico de filtros activos) y **menú ⋯** con: Vista del calendario (Día/Semana/Mes), Zoom del calendario, Vista previa del feed (Instagram), **Importar CSV**, configuración de notificaciones (Help "How to manage your calendar", https://help.metricool.com/how-to-manage-your-calendar-in-metricool-xhlbw; Help CSV, https://help.metricool.com/en/article/how-to-schedule-posts-in-batch-with-a-csv-file-in-metricool-3wihqx/).

### 2.2 Vistas de calendario

- **Día / Semana / Mes** desde ⋯ > *Calendar view*. No hay una vista "cuadrícula" aparte: la cuadrícula de Instagram es solo **Vista previa del feed** (Help xhlbw).
- **Lista** = pestaña separada, para revisión y acciones masivas (aprobar, rechazar, enviar a revisión; borrar pendientes — UNVERIFIED si incluye borrar).
- **Kanban** (ago/sep 2026): tablero en la **Biblioteca**, una columna por estado de workflow; arrastrar tarjeta = cambiar estado; Ctrl/Cmd para mover varias; cada cambio queda en el **historial del post**; posts asignables a compañeros (https://metricool.com/product-updates-august-2026/).
- **Zoom del calendario**: Condensado / Normal (por defecto) / Expandido.
- **Primer día de la semana** (lunes/domingo): en Ajustes de cuenta, no en la barra del planificador.
- Vista semana/día = rejilla **columnas = días, filas = franjas horarias**; clic en una franja abre el compositor con esa fecha/hora (Help "How to schedule content from the calendar", https://help.metricool.com/how-to-schedule-content-from-the-calendar-q8ymv).
- **Arrastrar archivos** desde el PC sobre una franja del calendario crea un post con esos medios (Help "How to add images and videos", https://help.metricool.com/how-to-add-images-and-videos-to-your-posts-in-metricool-b26gi).

### 2.3 "Mejores horas para publicar" (sombreado)

- Botón **"Mejores horas"** arriba a la derecha → elegir red → el calendario se **colorea por intensidad** (más oscuro = mejor). Columnas = días, filas = horas; opción de ver **porcentaje** (Help [ES] https://help.metricool.com/es/mejores-horas-para-publicar-en-metricool-w7ll9; Help EN https://help.metricool.com/best-time-to-post-on-social-media-in-metricool-w7ll9).
- Cálculo: franjas en que **los seguidores** están activos (no tus propios posts); solo datos recientes ⇒ todas las semanas (pasadas y futuras) muestran el mismo patrón.
- Requisitos: IG vía Facebook ≥100 seguidores; TikTok **Business** ≥100; X ≥300 activos (solo Advanced). Hasta **7 días** en aparecer.
- Facebook: según Metricool, Meta **retiró el soporte oficial en su API el 24‑sep‑2024**. *(CORREGIDO/precisado: en la API, las métricas `page_fans_online` / "fans online por hora" se deprecaron para **todas las versiones el 16‑sep‑2024** — changelog de Pages API de Meta y aviso de Coupler.io; el 24‑sep sería la fecha en que Metricool lo aplicó, UNVERIFIED.)* Cuentas conectadas antes usan histórico; después, **estudios genéricos** de Metricool (Help [ES] https://help.metricool.com/es/no-se-muestran-las-mejores-horas-para-publicar-en-metricool-jzgen).
- Troubleshooting: probar en incógnito (bloqueadores pueden impedir que cargue).
- En la programación recurrente se puede activar "mejores horas" para que asigne automáticamente la mejor hora por red (Help r63b1).

### 2.4 Tarjetas de publicación en el calendario

- Cada tarjeta muestra **icono de red** + **icono de tipo de post** (Post / Reel / Story) (Help xhlbw), miniatura/texto (UNVERIFIED exactamente qué se muestra), y estado.
- Colores de aprobación (blog antiguo, **UNVERIFIED** vigente): amarillo = pendiente de aprobación, verde = aprobado, azul = esperando publicación (https://metricool.com/content-approval-process/).
- Acciones por post (menú/hover; **UNVERIFIED** etiquetas): Editar, **Duplicar**, Eliminar (papelera), Vista previa, ver en la red. Borrar en Metricool **no** borra lo ya publicado en la red.
- **Drag & drop**: solo posts **Pendientes o Borradores**, y solo a fechas **futuras**. Para republicar algo publicado ⇒ **Duplicar**. Roles Manager/Editor pueden arrastrar; Analyst solo ve (Help xhlbw).

### 2.5 Estados de publicación y filtros

Estados documentados (Help xhlbw; https://help.metricool.com/why-does-my-post-appear-as-sent-in-metricool-and-not-auto-published-160f374/):

| Estado | Significado |
|---|---|
| **Publicado** (Published) | Publicado con éxito y visible en la red. |
| **Enviado** (Sent) | Solo IG/TikTok con *autopublicar* desactivado: se envió la notificación para publicar a mano. |
| **Pendiente** (Pending) | Programado, esperando su hora. |
| **Borrador** (Draft) | Guardado sin programar (no se publicará). |
| **Con errores** (With errors) | Falló; el mensaje de error está en el detalle del post. |
| **De autolista** (From autolist) | Generado por una autolista. |
| Aprobación | Pendiente de revisión · Aprobado · Rechazado. |

Filtros: **redes** (con interruptor "seleccionar todas"), **estado de publicación**, **estado de aprobación**, **notas** (con notas / con notas no leídas), **dashboards de campaña** y **buscador** de texto. El botón Filtros muestra el **nº de filtros activos**.

### 2.6 Borradores, Biblioteca e "Ideas"

- **Guardar borrador**: el post queda en el calendario sin programar; se puede arrastrar/editar luego.
- **Guardar en la biblioteca** (*Posts Library*): borrador **sin fecha**, reutilizable (Help "Posts Library", https://help.metricool.com/posts-library-u05cj). Para meter en la biblioteca un post publicado/programado hay que **duplicarlo** primero. Límite: **250 posts por marca** (antes 50; ago 2026). Desde ago 2026 la biblioteca tiene vista **Kanban** por estados, historial y asignación.
- **"Ideas"**: Metricool **no** tiene una función llamada "Ideas". El papel de "banco de ideas" lo cumplen: la **Biblioteca** (icono bombilla), los **Textos guardados**, los **Calendarios sociales** (efemérides) y una galería de inspiración externa (marketing). (Búsqueda sin resultados de "Ideas" como feature.) → En nuestra app podemos llamar "Ideas" a los posts sin fecha.
- **Textos guardados**: icono de **carpeta** en el compositor → "+" → título, etiquetas, descripción (= texto); buscador por palabra y filtro por etiquetas; compartidos entre marcas (https://metricool.com/save-text-metricool/).
- **Publicaciones eliminadas**: pestaña con lo borrado en los **últimos 45 días**; botón restaurar (vuelve con la misma fecha y estado) (Help https://help.metricool.com/how-to-recover-deleted-posts-in-metricool-byy42).
- Aviso al **cerrar el compositor con cambios sin guardar**; **autoguardado** de la configuración del post (ene 2026; intervalo de 30 s según tercero, **UNVERIFIED**).

### 2.7 Autolistas (colas evergreen)

- Crear en Planificación > Autolistas: **nombre**, **días y horas** de publicación (slots semanales), contenido en **orden de cola** (https://metricool.com/es/autolistas-redes-sociales).
- **"Repetir"**: convierte la lista en circular (el post publicado vuelve al final). Sin repetir, el post se elimina de la lista tras publicarse.
- Acciones: **activar / pausar / circular**; añadir posts a mano, por **CSV** o desde **RSS**. El asistente IA también funciona en autolistas.

### 2.8 Importación CSV masiva

- Calendario > **⋯ junto a "Mejores horas" > Importar CSV** → **descargar plantilla** (o plantilla Google Sheets). También desde Autolistas. Puede importar a **una o varias marcas** (Help [ES] https://help.metricool.com/es/como-planificar-publicaciones-en-lote-con-un-fichero-csv-en-metricool-3zwxj).
- Reglas: **no añadir, quitar ni reordenar columnas**; fila 1 = cabeceras, fila 2 = ejemplo; **UTF‑8** (emojis); al importar, elegir en un **desplegable el formato de fecha/hora** usado; sin límite duro pero se recomiendan **≤50 posts por fichero**.
- Columnas (parcial; nombres exactos **UNVERIFIED**): texto (1ª columna), fecha, hora, **una columna booleana por red (TRUE/FALSE)**, URLs públicas de medios (hasta 10), **Draft**, **Brand Name** (vacío = marca actual), tipo de post de Instagram, **privacidad TikTok** (`PUBLIC_TO_EVERYONE`, `MUTUAL_FOLLOW_FRIENDS`, `SELF_ONLY`, `FOLLOWER_OF_CREATOR`), **TikTok Auto Add Music**, **título TikTok (≤90 caracteres, solo fotos)**, **título Facebook (solo vídeo y Reel)**, **URL de miniatura de vídeo** (FB post/Reel), **frame de portada en ms** (IG Reels, Pinterest, TikTok). Un artículo antiguo habla de **18 columnas** (Help EN 3wihqx; https://metricool.com/import-csv-for-scheduling/).
- Hay artículo de errores comunes de importación (https://help.metricool.com/common-troubleshooting-when-importing-csv-into-metricool-m2lcv).

### 2.9 Zona horaria

- El planificador muestra la **zona activa junto a la hora actual**; por defecto la de la cuenta; se cambia **por marca** con el desplegable junto a la hora (Help [ES] https://help.metricool.com/es/como-configurar-tu-zona-horaria-en-metricool-hzl2d).
- Zona de la cuenta: ☰ > Ajustes de cuenta > Cuenta > Preferencias.
- **Un post = una única hora para todas las redes seleccionadas** (para horas distintas por red, Metricool sugiere su MCP) (Help [ES] 8tmhd).
- Hora "incorrecta" típica: reloj del sistema desincronizado.

### 2.10 Programación recurrente

- Flecha junto a **"Programar"** > Recurrencia. Modo guiado: **recurrente** o **personalizada**, **días de la semana**, **duración** (número + unidad, p. ej. 3 semanas). Máx. **180 días** (IA: 90 días), tope **200 posts** (artículo PT), no permite modificar el contenido de cada copia. Zona: la de la marca o una común. Plan Advanced. Rediseñada en ago 2026 (Help https://help.metricool.com/how-to-schedule-recurring-posts-in-metricool-r63b1).
- Programar manualmente en el calendario **no tiene límite de antelación** (Help q8ymv).

### 2.11 Notas, calendarios sociales, campañas

- **Notas** en publicaciones (recordatorios/comentarios internos para equipo o cliente), editables/eliminables; filtro "con notas / no leídas"; **texto enriquecido** desde ago 2026 (https://metricool.com/es/notas-de-metricool/).
- **Calendarios sociales**: añadir calendarios **predefinidos** (efemérides: San Valentín, Black Friday, Día del Perro…) o **importar iCal (.ics)** por URL (Google, Apple; Outlook no). Alcance **por marca** o **por usuario**. Se muestran junto a los posts en el calendario (Help [ES] https://help.metricool.com/es/como-usar-los-calendarios-sociales-en-metricool-5uy70).
- **Campañas**: los posts pueden agruparse en "Campaign dashboards" y el calendario filtra por campaña (Help xhlbw; changelog 2026).

### 2.12 Aprobación de contenido (equipos/clientes)

- En el compositor: **"Enviar a revisión"** en lugar de "Programar" → modal para elegir **revisores** (miembros existentes o **email externo**). Revisores suelen tener rol **Client** (Help [ES] https://help.metricool.com/es/como-enviar-publicaciones-a-revision-con-el-sistema-de-aprobacion-de-metricool-yvdr0).
- Aprobar/rechazar desde el **email** (sin cuenta) o dentro de Metricool; con varios revisores, el post queda "Pendiente de revisión" hasta tener suficientes aprobaciones o un rechazo. **Editar ⇒ hay que reenviar a revisión.** Acciones masivas en Lista. Plan Advanced/Custom. Desde ago 2026 los revisores externos pueden **comentar sin rechazar**.
- "Solicitudes de revisión" (☰) agrupa lo pendiente del usuario.

### 2.13 Vista previa del feed de Instagram

- ⋯ > **Vista previa del feed**: cuadrícula del perfil con los posts programados para ver cómo encaja (Help xhlbw; https://metricool.com/instagram-feed-preview-with-metricool/). Reordenar arrastrando en la cuadrícula: **UNVERIFIED/no documentado**.

**Implicaciones para el planificador**
- FullCalendar cubre Mes/Semana/Día/Lista nativamente; añadir una **vista Kanban** (Ideas → Borrador → Listo → Programado → Publicado/Error) como en brightbean-studio (`templates/composer/partials/kanban_board.html`, `templates/calendar/partials/month_grid.html`, `week_grid.html`, `list_view.html` — patrones HTMX equivalentes a nuestro stack; leer, no copiar).
- **Heatmap de mejores horas** como *background events* de FullCalendar (franjas coloreadas por intensidad), calculado **localmente** a partir de nuestro histórico de rendimiento (no depender de endpoints de Meta retirados).
- Drag & drop solo para `draft`/`scheduled` y solo hacia futuro; validar en servidor.
- Estados mínimos: `idea`, `borrador`, `programado`, `publicando`, `publicado`, `pendiente_manual` (≈"Enviado"), `error`, `cancelado` (+ `eliminado` blando 45 días). brightbean-studio usa un set similar (`apps/composer/models.py:341-352`: draft, pending_review, approved, scheduled, publishing, published, failed…). Aprobaciones: **fuera de alcance** (monousuario).
- CSV: ofrecer **plantilla descargable** + **pantalla de mapeo/validación previa** (brightbean: `csv_mapping.html`, `csv_validation.html`, `csv_progress.html`) — mejora sobre Metricool que importa "a ciegas".
- Zona horaria: guardar todo en UTC; mostrar la zona activa junto a la hora actual como hace Metricool.

---

## 3. Compositor "Crear publicación"

### 3.1 Puntos de entrada

- Botón **"+ Crear publicación"** (Planificación).
- **Clic en una franja** del calendario (precarga fecha/hora).
- **Soltar archivos** sobre el calendario o el compositor.
- Desde **Biblioteca**, **Duplicar**, **Autolista**, **CSV**.

### 3.2 Estructura del compositor (de arriba a abajo; layout exacto **UNVERIFIED**)

1. **Selector de redes**: iconos/avatares de las cuentas conectadas de la marca; se activan/desactivan individualmente. La primera acción del flujo es elegir redes (https://metricool.com/planning-social-media-content-with-metricool/).
2. **Pestañas "Plantilla" + una por red** tras pulsar **"Editar por red"** (*Multiposting*): la plantilla contiene el contenido común; en la pestaña de cada red, **"Editar contenido"** permite cambiar **texto, menciones, imágenes, emojis, hashtags, primer comentario, enlaces, alt text, etiquetas, portada, ubicación**. Editar una red **desvincula** de la plantilla; se puede **volver a la plantilla** (Help https://help.metricool.com/multiposting-edit-per-social-network-85out).
3. **Editor de texto**: **texto plano** (sin negrita/cursiva/viñetas). Herramientas en la barra inferior: emojis, hashtags (buscador), **textos guardados** (carpeta), **asistente IA**, multimedia. Contador de caracteres por red: **UNVERIFIED** (no documentado, pero los límites sí lo están, ver tabla 3.4).
4. **Zona multimedia** (icono multimedia): **subir desde el dispositivo o por URL**, **Google Drive** (planes de pago; links con permiso Editor), **Canva** (jpg, pdf, png, pptx, gif, mp4), **Biblioteca de publicaciones**, **Pexels** (banco de imágenes/vídeo), **Giphy** (GIFs). Cada archivo tiene **menú ⋯ > Editar** (editor de imagen/vídeo, §3.8). Conversión automática de imágenes (ene 2026) (Help b26gi; https://help.metricool.com/import-content-from-canva-to-your-posts-w5xbq).
5. **Ajustes globales** (*Global presets*): se **mantienen para los siguientes posts** hasta que se cambien. Incluyen **AUTOPUBLICAR** (toggle) y **Acortador de URL** (Mega tutorial, https://metricool.com/metricool-mega-tutorial/; Help TikTok https://help.metricool.com/schedule-and-post-on-tiktok-bkepi).
6. **Ajustes por red** (*Network presets*): debajo de los globales, un bloque por red seleccionada: **formato** (Post/Reel/Story, Vídeo/Foto…) y opciones específicas (§3.5–3.7). En TikTok varían según cuenta personal o business.
7. **Fecha y hora** (+ zona de la marca). Sugerencia de mejores horas: vía heatmap del calendario y en recurrencia; selector de "mejor hora" dentro del compositor **UNVERIFIED**.
8. **Notas** del post.
9. **Vista previa**: botón **"Mostrar vista previa"** arriba a la derecha; preview **en tiempo real por red** y conmutador **móvil / escritorio** (https://metricool.com/social-media-preview-metricool/).
10. **Botón principal "Programar"** con flecha desplegable: **Guardar borrador**, **Guardar en la biblioteca**, **Enviar a revisión** (Advanced), **Publicar ahora**, **Recurrencia** (Help q8ymv; r63b1).

Al guardar, **se crean tantas publicaciones como redes seleccionadas** (cada una independiente en el calendario) (Help 85out). Los posts **publicados no se pueden editar** (las APIs no lo permiten).

### 3.3 Funciones de apoyo en el compositor

| Función | Comportamiento documentado | Fuente |
|---|---|---|
| Emojis | Selector de emojis (sin detalle) | https://metricool.com/planner/ |
| Hashtags | Botón hashtags → escribir palabra clave → sugerencias + variantes con **nº de visualizaciones/usos**; clic para insertar (TikTok e IG). Grupos de hashtags guardados: **UNVERIFIED** (solo tercero socialk.it). | https://metricool.com/es/buscador-hashtags-tiktok-metricool/ |
| Acortador de URL | Ajuste global; acorta **todos** los enlaces del post **en el momento de publicar**; el acortador de Metricool **no rastrea clics** (privacidad). | Mega tutorial |
| Asistente IA | Genera copy desde **tema + tono** (profesional, cercano, inspirador…) **+ red**; CTA, hashtags, ajustar longitud, **traducir, corregir**; prompt de marca personalizable; también genera **ALT text**. Créditos: Free 5/mes; Starter 20/marca/mes; Advanced 35/marca/mes. | https://metricool.com/es/generador-de-contenidos-ia-para-redes-sociales/ ; Help [ES] sv0j8 |
| Textos guardados | Carpeta → título, etiquetas, texto; búsqueda | https://metricool.com/save-text-metricool/ |
| Menciones | Se pueden personalizar por red (Multiposting) | Help 85out |
| Ubicación | Búsqueda contra la API de Meta; si no aparece, desactivar autopublicar y añadirla en la app | https://help.metricool.com/how-to-add-a-location-to-my-post-ocjt1 |

### 3.4 Límites de texto y medios por red (según Metricool)

| | Facebook | Instagram | TikTok |
|---|---|---|---|
| Caracteres | 16.192 (Help FB; límite propio de Metricool — la API admite **63.206**, openpost `capabilities/capabilities.go:449`) | 2.200 | **2.200** caption de vídeo (UTF‑16; VERIFICADO: openpost `platform/tiktok.go:28`, brightbean `providers/tiktok.py:144`; postiz usa 2.000: `tiktok.provider.tsx:390`). Foto: título ≤90 + **descripción ≤4.000** (openpost `tiktok.go:478-484`) |
| Imagen | JPEG/PNG ≤30 MB | Metricool: JPEG/PNG ≤8 MB, AR 4:5–1,91:1 (otra fuente 3:4). **API de Meta: solo JPEG**, ≤8 MB, 320–1440 px de ancho, sRGB ⇒ **convertir PNG/WebP/HEIC a JPEG antes de publicar** (ref. "IG User Media") | **JPEG/WebP** ≤20 MB, ≤1080p (openpost `tiktok.go:471-473` confirma JPEG/WebP; tamaño UNVERIFIED) |
| Nº imágenes | hasta **35** por post según Help (UNVERIFIED vía API) — el tutorial dice 10; brightbean limita `attached_media` a **10** (`providers/facebook.py:94-96`) ⇒ **usar 10** en nuestra app | carrusel **2–10** vía API (la app nativa permite 20; openpost `capabilities.go` `publicMedia.MaxCount=10`, brightbean `instagram.py:335`) | foto/carrusel **1–35** (VERIFICADO openpost `tiktok.go:468-469`) |
| Vídeo | MP4/MOV/GIF ≤500 MB, 1 s–240 min | Reels **3 s–15 min, ≤300 MB**, MOV/MP4, H.264 **o HEVC**, 23–60 fps (VERIFICADO ref. "IG User Media" de Meta); vídeo en carrusel 3–60 s, AR 4:5–16:9 | Metricool (con autopublicar): MP4/MOV ≤500 MB, ≥540p, **3 s–10 min**. **API TikTok** (más permisiva): MP4/MOV/WebM, H.264/H.265/VP8/VP9, 23–60 fps, 360–4096 px, **≤4 GB** (openpost `capabilities.go:339`), duración máx. = `max_video_post_duration_sec` de `creator_info` |
| Reel | **3–90 s** (VERIFICADO: guía "Reels Publishing API" de Meta; brightbean `facebook.py:90-92`); máx. **30 Reels/24 h** por API | — | — |
| Story | ≤60 s (VERIFICADO, misma guía de Meta) | ver §3.6 | — |
| Comunes | Audio 128 kbps AAC; vídeo 5.000–25.000 kbps; **HEVC y HDR no compatibles con autopublicar** *(restricción de Metricool: la API de Reels de IG acepta HEVC; para nosotros, recomendar H.264 SDR y avisar, no bloquear)* | | |

Fuentes: https://help.metricool.com/publishing-requirements-for-images-and-videos-from-metricool-vfc8n ; https://help.metricool.com/schedule-and-publish-on-facebook-ycu1b ; https://help.metricool.com/schedule-and-post-on-instagram-6b6q5 ; Help TikTok bkepi. Verificación de límites de API: https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media ; https://developers.facebook.com/docs/video-api/guides/reels-publishing ; https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide.

### 3.5 Facebook (Página)

- **Formato**: **Post / Reel / Story** (se elige al crear) (Help ycu1b).
- **Título**: para posts con vídeo y Reels (no Stories).
- **Alt text**; **etiquetar hasta 3 Páginas** (UNVERIFIED vía API); **portada/miniatura** (URL de miniatura de vídeo en CSV).
- Varias fotos vía API: subir cada foto como no publicada a `/{page-id}/photos` y crear el post en `/{page-id}/feed` con `attached_media[i]={"media_fbid":…}` (openpost `platform/facebook.go:606-625`); brightbean limita a **10** fotos y borra las fotos ya subidas si falla (`providers/facebook.py:94-96, 417-430`). Los "35" de Metricool no se pudieron confirmar en la API.
- Un vídeo puede quedar "en cola" en Facebook y publicarse aunque la API devuelva error → **comprobar antes de reintentar** (Help "Facebook posting errors", https://help.metricool.com/en/article/facebook-posting-errors-troubleshooting-1ubfmry/).
- Fondos de color para posts de solo texto: no documentado en Metricool; postiz lo implementa con `text_format_preset_id` y ~130 caracteres (`facebook.dto.ts:1-20, 96-118`) — **UNVERIFIED** en Metricool, opcional para nosotros.
- Referencia de opciones reales en API: openpost `setting_contract.go:38-40` (`url`, `video_title`, `video_description`, `share_to_feed`, `first_comment`).

### 3.6 Instagram (cuenta profesional)

Formatos: **Post** (imagen única o **carrusel** ≤10; un vídeo suelto se publica **como Reel**), **Reel**, **Trial Reel**, **Story** (Help 6b6q5; https://help.metricool.com/how-to-publish-instagram-trial-reels-in-metricool-q27tj).

| Opción | Post | Reel | Story | Notas |
|---|---|---|---|---|
| Ubicación | Sí | Sí | No | Búsqueda vía API de Meta |
| Etiquetar personas | Sí (no en carruseles con vídeo) | Sí | No | Cuentas etiquetadas deben ser públicas |
| Etiquetar productos | Sí* | Sí* | No | *Requiere conexión vía FB y catálogo |
| Alt text | Sí | No | No | |
| **Primer comentario** | Sí | No (según tabla de Metricool) | No | La API sí lo permite en Reels: `POST /{ig-media-id}/comments` funciona sobre cualquier media publicada (openpost `platform/first_comment_instagram.go:11-16` y lo ofrece en todos los formatos IG, `capabilities/capabilities.go:2626`). No en Stories. |
| **Colaboradores** | Sí | Sí | No | Metricool dice "hasta 5". **Meta API (CORREGIDO):** la referencia "IG Media › Collaborators" (actualizada 18‑ago‑2025) dice **hasta 5** cuentas; una copia antigua (developers.secure) y Ayrshare dicen **3** ⇒ validar 5 y tratar el error de la API. Parámetro `collaborators` en contenedores de imagen, Reel y carrusel (en el contenedor padre, no en los hijos: openpost `instagram.go:360, 521`); **no en Stories**. Postiz traduce el error "param collaborators is not allowed" (`instagram.provider.ts:435-442`). |
| **Portada** | — | Elegir fotograma del vídeo **o** subir miniatura propia | — | |
| **Mostrar Reel en el feed** | — | Toggle (share to feed) | — | |
| Audio en tendencia | — | Sí (may 2026) | — | Audio fuera de la biblioteca ⇒ publicación manual |

- **Trial Reel**: requiere **≥1.000 seguidores**; sin colaboradores; opción **"Compartir automáticamente con todos"** ⇒ Instagram evalúa 72 h y lo "gradúa" si funciona. **(CORREGIDO)** En la API el parámetro real del contenedor `REELS` es **`trial_params={"graduation_strategy":"MANUAL"|"SS_PERFORMANCE"}`** (JSON); `is_trial_reel` es solo un flag interno de la UI de postiz/openpost (openpost `platform/instagram.go:366-372`; postiz `instagram.provider.ts:774-777`). La graduación manual no está expuesta en la API (se hace en la app). La ventana de 72 h y el mínimo de 1.000 seguidores no constan en docs de Meta (UNVERIFIED; terceros citan ~1.000, a veces 200). Si Instagram no lo ha habilitado en la cuenta, la publicación falla.
- **Story**: autopublicación solo **business** (EN) / business o creator (otras versiones). **(CORREGIDO — resolver a "solo Business")**: la guía oficial "Content Publishing" de Meta dice que *solo las cuentas business* pueden publicar Stories por la API, y en el foro de Meta una cuenta Creator recibe `(#10) The user is not an Instagram Business` al crear un contenedor `STORIES` (aplica seguro a la conexión vía Facebook; vía Instagram Login sigue UNVERIFIED). Desde jul‑2025 la API admite `user_tags` en Stories (changelog de Meta; UNVERIFIED en detalle). Stories con **enlaces, menciones o stickers interactivos** (encuestas, preguntas, quiz, sliders, cuenta atrás) ⇒ **publicación manual por notificación**. Activar archivo de historias en la app para conservarlas.
- Límite API: **50 publicaciones (feed + Reels + Stories) por 24 h** — VERIFICADO en la guía "Content Publishing" y en la referencia `content_publishing_limit` (`quota_total: 50`, `quota_duration: 86400`); un carrusel cuenta como 1. Una página de Meta aún dice 100 ⇒ leer siempre `GET /{ig-user-id}/content_publishing_limit?fields=config,quota_usage` en vez de codificar el número. (Ayrshare cita 25: obsoleto.)
- Claves reales de API para comparar: openpost `setting_contract.go:45-47` (`is_trial_reel` [UI → `trial_params`], `graduation_strategy`, `collaborators`, `location_id`, `user_tags`, `product_tags`, `cover_media_id`, `thumbnail_timestamp_ms`, `share_to_feed`, `alt_text`, `first_comment`).

### 3.7 TikTok

Formatos: **Vídeo** (1 por post) o **Foto/Carrusel** (≤35 imágenes) (Help bkepi; https://metricool.com/how-to-post-on-tiktok/).

| Opción | Detalle en Metricool | Notas API / referencia |
|---|---|---|
| **AUTO PUBLISH** | Toggle en Ajustes globales. Si se desactiva ⇒ notificación a la hora programada para terminar en la app de TikTok; sin límite de duración. Una vez enviada la notificación **no se puede reactivar** autopublicar ⇒ duplicar. | Equivale a `DIRECT_POST` vs `UPLOAD` (borrador en la bandeja de TikTok; ignora el resto de opciones salvo título/descripción): postiz `tiktok.dto.ts:61-70, 166-178`. VERIFICADO con openpost: vídeo `UPLOAD` ⇒ `POST https://open.tiktokapis.com/v2/post/publish/inbox/video/init/`; foto `UPLOAD` ⇒ `/v2/post/publish/content/init/` con `post_mode: "MEDIA_UPLOAD"` y sin `privacy_level` (`platform/tiktok.go:25-27, 290, 502-503`). "Caduca en 24 h": **UNVERIFIED** (solo lo dice postiz; no aparece en docs de TikTok). **Límite: máx. 5 envíos pendientes en bandeja por 24 h** (error `spam_risk_too_many_pending_share`; postiz `tiktok.provider.ts:195-200`; docs TikTok upload). Init limitado a **6 req/min por token**. |
| **Título** | ≤90 caracteres, **solo fotos** | postiz `tiktok.dto.ts:72-78` |
| **Privacidad** ("Quién puede ver este vídeo") | Valores `PUBLIC_TO_EVERYONE`, `MUTUAL_FOLLOW_FRIENDS`, `FOLLOWER_OF_CREATOR`, `SELF_ONLY`; opciones dependen de la privacidad de la cuenta | Valores VERIFICADOS en openpost/brightbean/postiz; deben estar en `privacy_level_options` de `POST /v2/post/publish/creator_info/query/` (error `privacy_level_option_mismatch`). Select **sin valor por defecto** ("Select") en postiz `tiktok.provider.tsx:68-85, 138-145`; openpost también exige selección explícita (`tiktok.go:600-604`), pero brightbean usa `PUBLIC_TO_EVERYONE` por defecto (`providers/tiktok.py:36`) ⇒ las referencias discrepan; la regla "sin defecto" de las guías de TikTok no se pudo leer textualmente en esta pasada (UNVERIFIED). **Contenido de marca no puede ser privado** (`SELF_ONLY`): deshabilitar "Contenido de marca" o forzar público y avisar (Content Sharing Guidelines de TikTok). |
| **Permitir comentarios** | Checkbox | |
| **Permitir Dúo / Stitch** | Checkboxes; solo vídeo | `tiktok.dto.ts:97-109` |
| **Contenido generado por IA** | Checkbox; solo vídeo; tipos de cuenta en conflicto (personal+business vs solo personal) — **UNVERIFIED** | `is_aigc` / `video_made_with_ai`. "Solo vídeo" VERIFICADO a nivel API: el `post_info` de foto no tiene `is_aigc` (openpost `tiktok.go:305-315` vs `486-514`; comentario postiz `tiktok.dto.ts:63-65`). En la API Content Posting (no Business) no depende del tipo de cuenta. |
| **Contenido comercial** | Toggle de divulgación → **"Tu marca"** (Brand Organic: te promocionas a ti/tu negocio) y/o **"Contenido de marca"** (Branded Content: promocionas a un tercero) | `brand_organic_toggle`, `brand_content_toggle`; aviso "Your video will be labeled 'Promotional content'… cannot be changed once posted" + consentimiento "Music Usage Confirmation"/"Branded Content Policy" (`tiktok.provider.tsx:268-372`) |
| **Música** | Business: Top 100 en tendencia (feb 2026); personal: música aleatoria solo en fotos/carruseles; solo con autopublicar | `autoAddMusic` solo fotos (`tiktok.dto.ts:118-125`) |
| Portada | Frame de portada en ms (CSV) | `cover_timestamp_ms` / `cover_index` (openpost `setting_contract.go:56-58`) |

- Duración con autopublicar: **3 s–10 min**; fallo si demasiado corto, grande o formato no soportado (Help "TikTok Publishing and Upload Troubleshooting", https://help.metricool.com/tiktok-publishing-and-upload-troubleshooting-guide-3ywdt). *(Para nuestra app: validar contra `max_video_post_duration_sec` de `creator_info`, no contra un 10 min fijo.)*
- **Apps no auditadas (VERIFICADO, más estricto de lo que decía este informe):** referencia "Direct Post" de TikTok: todo lo publicado por un cliente no auditado queda en **`SELF_ONLY`**; además la **cuenta de TikTok destino debe estar en modo privado** en el momento de publicar (si no: error `unaudited_client_can_only_post_to_private_accounts`, bloqueante) y como máximo **5 usuarios** pueden publicar con el cliente por 24 h. brightbean lo trata como error permanente no reintentable (`providers/tiktok.py:49-65`).

### 3.8 Editor de imagen y vídeo integrado

- Acceso: compositor > medio > **⋯ > Editar** (Help "Edit images and videos while scheduling", https://help.metricool.com/en/article/edit-images-and-videos-while-scheduling-your-content-12l9tjq).
- **Imagen**: recortar con relaciones de aspecto predefinidas (p. ej. Story), rotar, zoom, voltear; ajustes finos (brillo, saturación, exposición, temperatura, gamma, nitidez, viñeta); filtros (sepia, noir, blue…); stickers; dibujo; redimensionar; marcos y censura/pixelado (según una versión).
- **Vídeo**: recortar/dividir fragmentos, quitar audio al guardar, redimensionar, ajustes, filtros, stickers, dibujo. La edición de vídeo no está en la app móvil.

### 3.9 Programación y acciones finales

- Fecha/hora en zona de la marca; **sin límite** de antelación; misma hora para todas las redes del post.
- **Publicar ahora**, **Programar**, **Guardar borrador**, **Guardar en biblioteca**, **Enviar a revisión**, **Recurrencia**.
- **Duplicar** (incluido "Duplicar a otra marca").
- Boost (promocionar) al programar para FB/IG con Meta Ads conectado (§7.6).

**Implicaciones para el compositor**
- Modelo: `post_group` (lo que el usuario edita) → N `post_target` (uno por cuenta/red) con `override` de texto/medios/opciones y estado propio. Replica "Plantilla + Editar por red" con indicador visual "personalizado" y botón "Volver a la plantilla".
- Validación **en vivo** por red (caracteres, nº de medios, AR, duración, códec HEVC/HDR, tamaño) con mensajes en el panel de cada red, y bloqueo de "Programar" si hay errores — Metricool solo informa tras fallar.
- TikTok: cumplir las **guías UX obligatorias de TikTok** para apps auditadas (privacidad sin valor por defecto, toggles de interacción **desmarcados por defecto**, divulgación comercial **apagada por defecto** con casillas "Tu marca"/"Contenido de marca", texto de consentimiento "By posting, you agree to TikTok's Music Usage Confirmation" — o "…Branded Content Policy and Music Usage Confirmation" si hay contenido de marca —, contenido de marca incompatible con privado, vista previa del contenido, sin marcas de agua propias, mostrar el nombre de la cuenta destino). Opciones de privacidad y desactivación de comentarios/dúo/stitch siempre desde `creator_info`. Modo "Autopublicar OFF" = `UPLOAD` a la bandeja de TikTok (lo más parecido a la notificación de Metricool en una app local).
- Vista previa por red (móvil) a la derecha; en pantallas estrechas, pestaña.
- Ajustes globales "pegajosos" (guardar el último valor de autopublicar/acortador en preferencias).

---

## 4. Publicación manual por notificación, estados y errores

### 4.1 Flujo de publicación manual

- **Cuándo**: IG Stories con enlaces/menciones/stickers; Reels con audio no disponible; TikTok con música fuera del Top 100/efectos; cualquier post con **Autopublicar desactivado** (solo **Instagram y TikTok**) (Help "How to manually publish via notification", https://help.metricool.com/en/article/how-to-manually-publish-via-notification-1npgf9m/).
- **Configuración**: instalar app móvil (iOS ≥14 / Android ≥11), iniciar sesión; en Planificación > **Notificaciones** elegir **dispositivo móvil** (recomendado) o **email**; guardar y **enviar prueba**. Cada usuario configura lo suyo (Help [ES] https://help.metricool.com/es/how-to-set-up-notifications-for-manual-publishing-ybhgw).
- **A la hora programada**: llega la notificación → al tocarla **descarga los medios a la galería** y **copia el texto al portapapeles** → abre Instagram/TikTok → el usuario pega, añade stickers/música y publica.
- El post queda como **"Enviado"** (no "Publicado"). Por email, el navegador puede bloquear la descarga automática.
- Tras enviarse la notificación **no se puede reactivar el autopublicar** ⇒ duplicar (Help "Why can't I enable auto-publishing?", https://help.metricool.com/why-cant-i-enable-auto-publishing-66gg0).

### 4.2 Errores y reintentos

- Estado **"Con errores"**: abrir el post para ver el **mensaje exacto** y cotejarlo con la lista de errores comunes de cada red.
- **No hay botón "Reintentar" documentado**: la guía oficial es **duplicar y reprogramar**, y **no republicar repetidamente** (Instagram puede tratarlo como spam; un post aún "procesando" puede duplicarse) (Help IG errors 1j9yv87).
- Errores típicos: sesión inválida (400), OAuthException por actividad futura desactivada, "Application does not have permission", "video format is not supported" (bitrate), HDR, TikTok fichero corto/grande/formato.
- "Aparece publicado pero no se ve en la red" tiene artículo propio (https://help.metricool.com/why-a-post-appears-as-published-but-is-not-visible-on-the-social-network-cnxh3).
- Sep 2026: **nuevo sistema de notificaciones**; las notificaciones de error **llevan directamente a la vista afectada** (https://releasebot.io/updates/metricool).
- Tipos de notificación: artículo "Types of notifications in Metricool" (https://help.metricool.com/types-of-notifications-in-metricool-ynqf8) — detalle **UNVERIFIED**.

**Implicaciones**
- App local sin móvil: "publicación manual" = **notificación de escritorio (Windows toast) + modal** con botones **Copiar texto**, **Descargar medios (ZIP o carpeta)**, **Abrir en el navegador/app**, **Marcar como publicado** (pide URL opcional). Para TikTok, ofrecer además "Enviar a la bandeja de TikTok" (`UPLOAD`).
- **Botón Reintentar** propio, pero **idempotente**: antes de reintentar, consultar la red (último media/post) para detectar si el primer intento sí se publicó (riesgo documentado en FB/IG).
- Centro de notificaciones en la app (campana) con enlaces profundos al post fallido; contador en el menú Planificación.

---

## 5. Biblioteca / medios

- Metricool **no** ofrece una mediateca clásica tipo DAM por marca (no se encontró documentación de carpetas de medios propias). Fuentes de medios en el compositor: **dispositivo/URL**, **Google Drive** (navegar carpetas, buscar, selección múltiple para carruseles; también .pdf/.ppt/.doc), **Canva** (diseños y carpetas, exportación directa sin descargar), **Biblioteca de publicaciones**, **Pexels**, **Giphy** (Help b26gi; https://metricool.com/es/google-drive-con-metricool/; https://metricool.com/canva/). Dropbox: sin integración nativa (solo URLs públicas).
- La "galería" conserva los resultados de búsqueda hasta una nueva búsqueda; vídeos subidos se procesan más rápido (ago 2025).
- La unidad de reutilización es la **Biblioteca de publicaciones** (posts sin fecha, 250/marca, Kanban, historial, asignación).

**Implicaciones**: nuestra **Biblioteca de medios** local es una mejora: carpetas, etiquetas, búsqueda, favoritos, miniaturas, metadatos (dimensiones, duración, códec, bitrate, HDR), **insignias de compatibilidad por red** (✓ IG Reel, ✗ TikTok >10 min…), "usado en N posts" y bloqueo de borrado si está en posts programados (brightbean: `templates/media_library/_delete_blocked.html`, `_asset_detail_panel.html`, `_folder_tree.html`). Riesgo: Instagram/Facebook requieren a menudo **URL pública** de los medios (ver riesgos).

---

## 6. Analítica (Facebook, Instagram, TikTok)

### 6.1 Navegación y controles comunes

- Analítica → **sidebar izquierda**: **Resumen** + redes conectadas (+ Competidores, Hashtags) (Help "How to use and explore the Analytics tab", https://help.metricool.com/how-to-use-and-explore-the-analytics-tab-lmd52; https://help.metricool.com/summary-metrics-6zo4n).
- **Periodo principal** (selector de rango arriba a la derecha) que gobierna todos los gráficos y tablas.
- **% de variación** vs **periodo anterior de igual duración** (no configurable en Analítica; en Informes se compara con el mes natural anterior) (https://help.metricool.com/how-comparison-periods-work-in-your-metrics-yj8tx).
- **"Crear vista de comparación"** → lo construye en Metricool Studio sin cambiar el periodo.
- **Listas de posts**: botón **Columnas** (mostrar/ocultar métricas), **Descargar CSV**, **Buscar**. Ordenar por columna: **UNVERIFIED**.
- Distinción clave: **"Publicados en el periodo"** (posts creados en el rango, métricas acumuladas hasta ayer) vs **"Vistos en el periodo"** (cualquier post con actividad en el rango) (https://help.metricool.com/difference-between-published-posts-and-viewed-posts-in-the-period-2pwtf).
- PDFs/PPT: **no** en Analítica; en Reporting.
- **Resumen**: agrega todas las redes de la marca; totales + desglose por red; pestañas **Cuenta**, **Publicaciones**, **Cuentas publicitarias**.
- **Engagement**: interacciones / alcance × **100** por defecto (antes ×1000; opción configurable). En IG siempre con datos orgánicos. Competidores: interacciones medias / seguidores × 100 (https://help.metricool.com/how-engagement-is-calculated-in-metricool-i0x20).
- Plan Free: **30 días de histórico**.

### 6.2 Facebook (Help "Facebook Metrics", https://help.metricool.com/en/article/facebook-metrics-1tllrsb/; https://metricool.com/es/analitica-para-facebook/)

- **Comunidad/crecimiento**: evolución de **seguidores** (Page Likes **discontinuado el 1‑nov‑2025** según Metricool; **CORREGIDO a nivel API:** Meta deprecó `page_fans` y las métricas `page_impressions*` para todas las versiones el **15‑nov‑2025** (blog de Meta "Page Insights API updates", 15‑ago‑2025) ⇒ usar `page_follows`/seguidores y `page_media_view` ("Views"); las variantes viral/no viral de impresiones no tienen sustituto; el balance de likes pasa a **Followers**), media diaria de nuevos seguidores, **balance de seguidores** (ganados/perdidos por día relacionado con días con publicaciones), **visitas a la página**.
- **Cuenta**: **Visualizaciones (Views)** sustituyen a *Page impressions*; **alcance** mostrado como **media** (Meta lo da diario; sumar duplicaría usuarios); clics.
- **Contenido**: nº de publicaciones = posts + reels + stories; tabla de posts; Reels; **Stories** (requiere archivo de historias; los reposts no son accesibles por API).
- **Interacciones**: reacciones, compartidos, clics (comentarios: la ayuda indica que **no** se incluyen en el total — fuentes en conflicto).
- Datos **orgánico + pagado combinados**, salvo *views* de posts (desglosadas).
- **Audiencia**: edad, género, país, ciudad, idioma.

### 6.3 Instagram (Help "Instagram Metrics", https://help.metricool.com/en/article/instagram-metrics-12vpkyb/; https://help.metricool.com/instagram-account-metrics-breakdowns-and-charts-is1su)

- **Cuenta** (subpestañas): **Evolución general**, **Alcance / Visualizaciones**, **Interacciones**, **Actividad del perfil**; desgloses por **tipo de contenido**, **seguidor/no seguidor** y **toques en botón de contacto** (mar 2026). Datos de cuenta = orgánico + pagado.
- **Comunidad**: seguidores (al final del periodo), seguidos, contenido total (posts + reels + stories), **balance de seguidores** (requiere ≥100 seguidores).
- **Views** sustituyen a impressions desde **abril 2025** (incluyen repeticiones). VERIFICADO: `impressions`, `plays`, `clips_replays_count` e `ig_reels_aggregated_all_plays_count` deprecadas en Graph API v22.0 (21‑ene‑2025) y para todas las versiones el **21‑abr‑2025**; nueva métrica `views` (con desgloses `follower_type`, `media_product_type`) — changelog v22.0 de Meta y referencia "Instagram Media Insights". (La ayuda de Metricool dice "abril 2024": errata.)
- **Posts**: resumen orgánico; detalle de promocionados si se conecta Meta Ads. Interacciones = likes, comentarios, compartidos, guardados.
- **Reels**: engagement, alcance medio, views, **tiempo medio y total de visualización** (nov 2025), **duración, % retención, % view rate, reposts** (ene 2026); análisis detallado de los **primeros 15 días** (Advanced).
- **Stories**: solo orgánico; alcance, impresiones, tabla por story (datos de 24 h).
- **Hashtags**: uso por hashtag (nº posts, likes y comentarios totales) — https://metricool.com/instagram-analytics-tool/.
- **Demografía**: **UNVERIFIED** en la UI (solo blog/tercero).
- Analítica completa requiere conexión vía Facebook.

### 6.4 TikTok (Help "TikTok Metrics", https://help.metricool.com/en/article/tiktok-metrics-1hnbc9r/)

- **Comunidad**: seguidores (fin de periodo); **balance**: Business = ganados y perdidos diarios en dos series + tarjeta de posts del día; Personal = solo variación neta.
- **Cuenta**: **visitas al perfil**; impresiones.
- **"Posts vistos en el periodo"**: views, likes, comentarios, compartidos.
- **"Posts publicados en el periodo"**: engagement, interacciones, alcance medio; lista por vídeo con fecha, views, likes, comentarios, compartidos, alcance, duración, engagement (tiempo medio de visualización / % completados: **UNVERIFIED**).
- Business tiene más datos (demografía, métricas de cuenta y de vídeo adicionales). Análisis detallado de los **primeros 15 días** solo Advanced (ene 2026).
- Gotchas: vídeos sin actividad >7 días pueden no traer alcance o tiempo total; vídeos filtrados por TikTok (copyright musical) no se recuperan por API.

**Implicaciones**
- Guardar **snapshots diarios** (cuenta y post) en SQLite: las APIs dan ventanas limitadas y Metricool construye la evolución con su propia recogida diaria (balance de seguidores, etc.).
- Pantalla Analítica: sidebar (Resumen + red), selector de rango con presets (7/28/30/90 días, mes actual/anterior, personalizado), tarjetas KPI con % vs periodo anterior, gráficos Chart.js (línea de evolución, barras de balance ganados/perdidos, barras por tipo de contenido), tabla de posts con columnas configurables, búsqueda, ordenación y **CSV**. Modal de detalle de post con sus métricas.
- Implementar ambas vistas "publicados en el periodo" y "vistos en el periodo" solo si las APIs lo permiten; MVP: "publicados".
- Mostrar avisos de disponibilidad ("Métrica no disponible para cuentas personales de TikTok", "Requiere ≥100 seguidores").

---

## 7. Otros módulos (alto nivel)

### 7.1 Bandeja de entrada (Inbox)
- FB: comentarios + mensajes privados; IG: comentarios + DMs (bandeja principal); **TikTok: solo comentarios y solo business**; reseñas de Google. Filtros por **tipo** (comentario/DM/reseña) y **red**, **buscador de usuario**; **notas** por conversación. No permite **borrar ni ocultar**; no hay autorrespuestas en el Inbox; **no guarda historial** (carga en vivo). Ventanas de respuesta de Meta: comentarios últimas 24 h (según Metricool) y DMs 7 días (Help [ES] https://help.metricool.com/es/inbox-manager-how-to-manage-messages-and-comments-from-metricool-s9zze).
- **Flows** (pago): automatizaciones IG comentario→DM, DM por palabra clave, respuesta a story→DM; activador (cualquier post/uno publicado/uno del planificador), palabras clave separadas por comas, respuesta comentario/DM/ambas, esperas (máx 24 h), vista previa móvil, borrador/activar (https://help.metricool.com/es/flows-en-instagram-v4nx3).

### 7.2 Informes (Reporting)
- **Reports**: PDF o PPT, logo propio/cliente, **plantillas personalizadas** (fondo, colores, páginas, logo; por marca; Advanced), **envío automático por email** (destinatarios, día, texto, envío de prueba). No en Free (https://help.metricool.com/es/how-to-generate-reports-szc5k).
- **Campaign Dashboards**, **Metricool Studio** (vistas IA en lenguaje natural: informes, calendarios, comparativas; enlace público; no en móvil; add-on de pago tras periodo promocional), **Data Studio/Looker Studio** connector (https://help.metricool.com/what-is-metricool-studio-o4pac).

### 7.3 Competidores
- Analítica > Competidores > **Añadir** (buscar cuenta). Redes: FB, IG (requiere conexión vía FB), Threads, X (add-on), Bluesky, YouTube, Twitch — **no TikTok**. Solo cuentas business en FB/IG. Free: demo **5 por red, no se pueden borrar**; Premium **100 por red** (YouTube 10). Favoritos con estrella; tabla general + tabla comparativa + informe; se recomienda añadir la propia cuenta (https://help.metricool.com/how-to-add-competitors-in-metricool-sgjjg).

### 7.4 Hashtag Tracker
- Seguimiento de hashtag en **Instagram y X**: uso, conversaciones, origen geográfico, usuarios activos, influencia (alcance/engagement). IG desde hoy en adelante; X hasta 7 días atrás. Export PDF/PPT y envío automático (https://metricool.com/how-to-measure-hashtags/).

### 7.5 SmartLinks (link in bio)
- Página con **botones** (texto + URL), imágenes/vídeos y **posts de Instagram enlazables**; URL única para la bio; analítica de **clics y CTR** por botón/imagen. Rediseño completo feb 2026 (https://metricool.com/metricool-smartlinks/; https://help.metricool.com/how-to-analyze-the-performance-of-your-smartlinks-in-metricool-0cu3k).

### 7.6 Ads
- Tabla de campañas (búsqueda, rango, refrescar, filtros por estado/plataforma/objetivo, columnas). **Crear/gestionar** en Meta y Google; **TikTok Ads solo monitorizar**. **Boost** de posts FB/IG (≈2 días, mínimo 2 USD; en UE campos beneficiario/pagador) desde lista de posts o al programar (https://help.metricool.com/how-to-boost-your-posts-fr90h).

### 7.7 App móvil
- Barra inferior: calendario, analítica, **nueva publicación**, Inbox, cambio de marca. Planificar/editar/duplicar, analítica, Inbox, informes, aprobaciones, biblioteca, Drive y Canva. **No**: calendarios sociales, SmartLinks, Ads, gestión de usuarios, % de mejores horas, edición de vídeo (Help [ES] https://help.metricool.com/es/funcionalidades-disponibles-en-la-app-movil-213ni).

**Implicaciones**: fuera del MVP salvo, quizá, un **Inbox de comentarios** (fase 2). Competidores/Hashtag tracker dependen de APIs de descubrimiento (Business Discovery de IG) — evaluar más tarde. SmartLinks/Ads: no.

---

## 8. Plan Free: límites y cómo se ven

- Tabla oficial (Help "Plans, add-ons, and API access explained", https://help.metricool.com/plans-add-ons-and-api-access-explained-xux1u): **1 marca**, **hasta 20 posts/mes**, **30 días de analítica**, asistente IA, MCP de Metricool; **sin LinkedIn ni X**. API solo Advanced/Custom.
- El límite bajó de **50 a 20 posts/mes en 2026** (−60%) (que.es 19‑sep‑2026; SocialPilot). Fuentes antiguas aún citan 50 o 100. VERIFICADO de nuevo en Help xux1u y en "Main differences between free and paid plans" (el límite cuenta posts del planificador; sin Make/Zapier); la página comercial "Metricool vs Buffer" de Metricool aún dice 50 (desactualizada).
- Otros límites Free: 5 créditos IA/mes; competidores en modo demo (5/red, no borrables); sin informes PDF/PPT; Drive en planes de pago; sin aprobación, recurrencia ni plantillas de informe (Advanced); "Añadir marca" solo de pago; SmartLinks/Flows en planes de pago (según terceros).
- **Cómo se muestra en la UI** (contador de posts restantes, candados, modales de upgrade): **UNVERIFIED** — no se encontró documentación. Verificar con navegador.

**Implicaciones**: sin planes en nuestra app. En su lugar, un widget **"Cuotas de las APIs"**: publicaciones IG usadas en 24 h (límite 50, consultable vía `content_publishing_limit`), límites diarios de TikTok (`creator_info`), caducidad de tokens. Mismo patrón visual (barra de uso) que Metricool usaría para su cuota de plan.

---

## 9. Mapa Metricool → Planificador de Redes (nombres propios, sin marca ajena)

| Metricool | Nuestra app (ES) | Prioridad |
|---|---|---|
| Marca | Marca / Espacio (implícito en MVP) | MVP (modelo) |
| Conexiones | **Cuentas conectadas** | MVP |
| Planificación > Calendario (Día/Semana/Mes) | **Calendario** (Mes/Semana/Día) | MVP |
| Lista + acciones en bloque | **Lista** (filtros, selección múltiple: reprogramar, duplicar, borrar) | MVP |
| Biblioteca (Kanban) | **Ideas** (posts sin fecha) + vista **Tablero** | MVP / F2 |
| Mejores horas | **Mejores horas** (heatmap local) | F2 |
| Autolistas | **Colas** (slots semanales, repetir) | F2 |
| Importar CSV | **Importar CSV** (con mapeo/validación) | F2 |
| Recurrencia | **Repetir publicación** | F3 |
| Publicaciones eliminadas | **Papelera** (30–45 días) | MVP |
| Calendarios sociales | **Fechas clave** (iCal + efemérides ES) | F3 |
| Notas | **Notas** del post | F2 |
| Crear publicación + Editar por red | **Nueva publicación** con pestañas "Común" + red | MVP |
| Ajustes globales (autopublicar, acortador) | **Opciones generales** | MVP (autopublicar), F3 (acortador) |
| Notificación móvil | **Publicación asistida** (aviso de escritorio + copiar/descargar) / bandeja TikTok | MVP |
| Galería/Drive/Canva/Pexels | **Biblioteca de medios** local | MVP |
| Analítica por red | **Estadísticas** | MVP básico / F2 |
| Inbox | **Comentarios** | F3 |
| Informes PDF | **Exportar CSV/PDF** | F3 |
| Competidores, Hashtag tracker, SmartLinks, Ads, Studio, Flows | — | Fuera de alcance |

---

## 10. Preguntas abiertas y riesgos

1. **Verificación visual pendiente**: etiquetas exactas, orden de pestañas, layout del compositor, contador de caracteres, selector de "mejor hora" en el compositor, UI de límites del plan Free — todo UNVERIFIED sin navegador.
2. **Medios locales vs URL pública**: Instagram (y en parte Facebook/TikTok `PULL_FROM_URL`) requieren URLs públicas o subidas reanudables; una app en `localhost` necesita estrategia (subida binaria donde exista, túnel temporal, o hosting intermedio). Afecta al diseño de la Biblioteca y del publicador.
3. **TikTok sin auditoría** (VERIFICADO, ver §3.7): apps no auditadas solo publican en **`SELF_ONLY`**, la **cuenta destino debe estar en privado** al publicar y máx. **5 usuarios/24 h**. El modo "bandeja"/`UPLOAD` puede ser el camino principal hasta auditar, pero tiene su propio tope de **5 envíos pendientes/24 h**; si el modo bandeja también exige auditoría o cuenta privada: UNVERIFIED.
4. **Colaboradores IG**: Metricool dice "hasta 5"; la referencia actual de Meta dice 5 (versiones antiguas, 3). Sí en carruseles (contenedor padre), **no en Stories**.
5. **Primer comentario en Reels**: Metricool lo limita a Posts; la API de comentarios (`POST /{ig-media-id}/comments`) **sí** funciona sobre Reels (openpost lo ofrece en todos los formatos IG) — podemos ofrecerlo en Post y Reel, no en Story.
6. **Stories IG**: la doc de Meta dice **solo Business** (tratar Creator como no soportado salvo prueba en contrario). **Etiqueta IA de TikTok**: en la API es solo vídeo y no depende del tipo de cuenta; la discrepancia personal/business es solo de la UI de Metricool.
7. **Mejores horas para Facebook**: Meta retiró el dato en sep‑2024; nuestro heatmap debe basarse en rendimiento histórico propio (necesita semanas de datos).
8. **Reintentos**: riesgo de duplicados (FB/IG pueden publicar pese a error) ⇒ reintento con verificación previa.
9. **Analítica histórica**: sin snapshots diarios desde el día 1 no habrá evolución de seguidores/balance; programar la recogida desde que se conecta la cuenta.
10. **Licencias**: postiz/openpost/brightbean son AGPL — solo leídos como referencia de comportamiento; ningún código copiado.

---

## 11. Verificación (pasada adversarial, 2026-10-15)

Método: se eligieron las afirmaciones con más impacto en publicación, autenticación, analítica y UX, y se contrastaron con **una fuente distinta** de la citada originalmente (código de openpost/brightbean cuando el informe citaba postiz o Metricool; docs oficiales de Meta/TikTok vía WebSearch cuando citaba la ayuda de Metricool). Esta pasada **tampoco tuvo acceso a herramientas de navegador** (la extensión Claude in Chrome que activó el usuario no estaba expuesta en esta sesión), así que lo que depende de ver la UI de Metricool sigue UNVERIFIED. Rutas de código relativas a cada repo de referencia (solo lectura, AGPL, nada copiado).

| # | Afirmación | Veredicto | Fuente usada para verificar |
|---|---|---|---|
| 1 | Tokens: Meta 60 días, TikTok 365 días | **CONFIRMED + precisado** — TikTok: access 24 h, refresh 365 d (rota); FB: token de Página derivado no caduca por tiempo; IG Login: 60 d renovable | developers.tiktok.com "oauth-user-access-token-management"; developers.facebook.com "Long-Lived Access Tokens"; openpost `platform/tiktok.go:124-176`; brightbean `providers/instagram_login.py:215-278`, `providers/facebook.py:216-237` |
| 2 | IG: 50 publicaciones/24 h por API | **CONFIRMED** (carrusel = 1; una página de Meta dice 100 ⇒ consultar `content_publishing_limit`) | Meta "Content Publishing" y referencia `content_publishing_limit` |
| 3 | IG carrusel ≤10 | **CONFIRMED** (API 2–10; app nativa 20) | openpost `capabilities/capabilities.go` (`publicMedia.MaxCount: 10`); brightbean `providers/instagram.py:335` |
| 4 | IG Reels hasta 15 min | **CONFIRMED** (3 s–15 min, ≤300 MB, H.264/HEVC) | Meta "IG User Media" (especificaciones de Reels) |
| 5 | IG imágenes JPEG/PNG ≤8 MB | **CORRECTED** — la API solo admite **JPEG** (convertir PNG) | Meta "IG User Media" (especificaciones de imagen) |
| 6 | IG colaboradores "hasta 5" (API sin confirmar) | **CORRECTED** — la referencia actual de Meta dice 5 (antes 3); no en Stories | Meta "IG Media › Collaborators" (ago‑2025) vs. copia developers.secure; openpost `platform/instagram.go:360, 521` |
| 7 | Trial Reel en API = `is_trial_reel` + `graduation_strategy` | **CORRECTED** — parámetro real `trial_params={"graduation_strategy":…}` | openpost `platform/instagram.go:366-372`; postiz `instagram.provider.ts:774-777`; Ayrshare/bundle.social |
| 8 | Stories IG: business vs creator (conflicto) | **CORRECTED → solo Business** (Instagram Login: UNVERIFIED) | Meta "Content Publishing"; hilo del foro de desarrolladores de Meta (error #10) |
| 9 | Primer comentario en Reels: "podría" permitirlo la API | **CONFIRMED** que la API lo permite | openpost `platform/first_comment_instagram.go:11-16`, `capabilities/capabilities.go:2626` |
| 10 | Instagram Basic Display API retirada en dic‑2024 | **CONFIRMED** (4‑dic‑2024) | Meta docs "Instagram Basic Display API"; Zapier; brightbean `providers/instagram_login.py:8` |
| 11 | IG "Views" sustituyen a impressions desde abril 2025 | **CONFIRMED** (v22.0; todas las versiones 21‑abr‑2025) | Meta changelog v22.0; "Instagram Media Insights" |
| 12 | FB Page Likes discontinuado 1‑nov‑2025 | **CORRECTED** — deprecación en la API: **15‑nov‑2025** (`page_fans`, `page_impressions*`) | Blog de Meta "Page Insights API updates" (15‑ago‑2025); Supermetrics; Sprout |
| 13 | FB mejores horas: Meta lo retiró el 24‑sep‑2024 | **CORRECTED** — `page_fans_online` deprecado **16‑sep‑2024** (todas las versiones) | Changelog de Pages API de Meta; Coupler.io |
| 14 | FB Reel 3–90 s, Story ≤60 s | **CONFIRMED** (+ máx. 30 Reels/24 h por API) | Meta "Reels Publishing API"; brightbean `providers/facebook.py:90-92` |
| 15 | FB hasta 35 imágenes por post | **UNVERIFIED** en la API; brightbean limita `attached_media` a 10 ⇒ usar 10 | brightbean `providers/facebook.py:94-96`, `tests/providers/test_facebook.py:171-182` |
| 16 | FB 16.192 caracteres | **CORRECTED** para nuestra app — límite propio de Metricool; la API admite 63.206 | openpost `capabilities/capabilities.go:449` |
| 17 | TikTok privacidad: 4 valores, sin valor por defecto | **CONFIRMED** los valores; "sin defecto" coincide con openpost/postiz pero brightbean usa PUBLIC por defecto (UNVERIFIED en las guías de TikTok); añadido: contenido de marca no puede ser privado | openpost `platform/tiktok.go:600-604`; brightbean `providers/tiktok.py:36-44`; TikTok "Content Sharing Guidelines" |
| 18 | TikTok foto: título ≤90, ≤35 imágenes | **CONFIRMED** (+ descripción ≤4.000; solo JPEG/WebP) | openpost `platform/tiktok.go:466-484` |
| 19 | TikTok caption 2.200 | **CONFIRMED** | openpost `platform/tiktok.go:28`; brightbean `providers/tiktok.py:144` |
| 20 | TikTok vídeo MP4/MOV ≤500 MB, ≥540p, 3 s–10 min | **CORRECTED** para nuestra app — es el límite de Metricool; la API admite MP4/MOV/WebM ≤4 GB, ≥360 px, duración según `creator_info` | TikTok "Media Transfer Guide"; openpost `capabilities/capabilities.go:339` |
| 21 | TikTok etiqueta IA solo vídeo | **CONFIRMED** a nivel API (sin dependencia de tipo de cuenta) | openpost `platform/tiktok.go:305-315` vs `486-514`; postiz `tiktok.dto.ts:63-65` |
| 22 | TikTok `UPLOAD` = borrador en bandeja, ignora opciones, caduca en 24 h | **CONFIRMED** endpoints y que se ignoran opciones; "caduca en 24 h" **UNVERIFIED**; añadido tope de 5 pendientes/24 h | openpost `platform/tiktok.go:25-27, 290, 502-503`; TikTok docs "Upload Content"; postiz `tiktok.provider.ts:195-200` |
| 23 | TikTok sin auditoría ⇒ solo privado | **CORRECTED/ampliado** — `SELF_ONLY` **y** cuenta destino en privado **y** máx. 5 usuarios/24 h | TikTok "Direct Post" reference; brightbean `providers/tiktok.py:49-65` |
| 24 | Metricool Free = 20 posts/mes | **CONFIRMED** (una página comercial de Metricool aún dice 50) | Help xux1u y "Main differences between free and paid plans" |

**Sigue UNVERIFIED tras esta pasada:** todo lo de la UI de Metricool (etiquetas, orden de pestañas, layout del compositor, contador de caracteres, aspecto de los límites Free); caducidad de 24 h de los borradores de bandeja de TikTok; si `UPLOAD` también exige auditoría o cuenta privada; Stories por Instagram Login con cuentas Creator; límite de 35 fotos en Facebook; texto literal de "sin privacidad por defecto" en las guías de TikTok; umbral de 1.000 seguidores y ventana de 72 h de los Trial Reels; formato de `user_tags` en Reels/Stories.
