# 05 — Metricool real screens (user screenshots, 2026-10-15)

The user shared six screenshots of their own logged-in Metricool account (free plan, brand "ascendiamex",
timezone America/Mexico_City). Files 1–5 are available to agents at
`/tmp/claude-0/-home-user-ascendia/e91bf9d5-eee6-5506-a9c5-a075f2256e79/images/{1..5}.webp` (look at them with
the Read tool). They are NOT committed to the repo (third-party UI + private account data). Screenshot 6 (Inbox)
was not saved as a file; it is described below in words.

**Rule:** replicate the *layout, information architecture and interaction flow* closely — that is what the user
asked for ("quiero una igual"). Do **not** copy Metricool's logo, name, watermark, illustrations or exact colour
scheme (dark aubergine top bar `≈#2b1a2b` + lime accent `≈#e4f25b`). Our identity: dark navy/indigo top bar,
violet primary (`--primary #5b4cf0`), our own logo. Also skip everything plan-related ("Mejora tu plan" button,
"¿Necesitas un plan superior?" banners, premium diamond badges, watermarks) — our app has no plans or limits.

---

## 1. Global chrome (screens 1, 2)

- **Top bar** (≈64 px, dark background, thin coloured accent line under it): logo at far left; main nav as
  text items with generous spacing: Analítica · Reporting · Inbox · **Planificación** · SmartLinks · Anuncios.
  The active item has a lighter rounded "pill" background. Right side: brand avatar (square, rounded) + brand
  name + chevron (brand switcher dropdown).
  → Ours: logo · **Planificación · Estadísticas · Comentarios · Biblioteca** (active = pill) … right side:
  notifications bell, brand switcher (avatar + name + chevron), ☰ menu. The "Nueva publicación" CTA lives in the
  page toolbar (like Metricool's "Crear publicación"), not necessarily in the top bar (keeping the `N` shortcut).
- Page background is a very light grey (`≈#f4f4f6`); content sits in white cards with large radius (≈16 px)
  and soft shadow.

## 2. Planificación — week view (screen 1)

- **Sub-tabs row** under the top bar (plain text tabs, active one darker/underlined):
  `Calendario · Listado · Biblioteca de publicaciones · Autolistas · Publicaciones eliminadas · Flows`.
  At the far right of the same row: clock icon + **"9:18 - America/Mexico_City"** + chevron (current time in the
  brand timezone; dropdown to change it).
  → Ours: `Calendario · Lista · Ideas · Papelera` + the same timezone indicator/dropdown on the right.
- **Toolbar** (one row, white controls with 1 px borders, 44 px tall):
  - wide search input "Buscar" (magnifier icon);
  - **"Esta semana"** button (go to today);
  - `‹` button · date-range label with calendar icon **"12 oct 2026 - 18 oct 2026"** (opens a date picker) · `›`;
  - filter button (funnel icon) and a kebab `⋮` (view options: Mes/Semana/Día, import CSV…);
  - **"Mejores horas"** dropdown showing the selected network's logo (choose which network's best times shade
    the grid: Facebook / Instagram / TikTok / ninguna);
  - icon button (Instagram feed grid preview — optional for us);
  - "Crear vista" (premium — skip);
  - primary CTA **"+ Crear publicación"** at the far right (dark filled button). → Ours: "+ Crear publicación"
    with our primary colour.
- **Week grid**: columns "Lunes 12", "Martes 13", … each header is a rounded tab-like cell; **today's header is
  filled dark with white text**; weekend headers have a grey background; time gutter on the left with hourly
  labels ("9:00", "10:00"…), half-hour divider lines; a **current-time line** with a small ▶ marker in the gutter.
  Default scroll position starts around the current hour / 9:00.
- **Best-times shading**: every hour slot is tinted in shades of **blue** — darker = better time; the shading
  covers the whole grid (all days, all hours) while "Mejores horas" is active. → Ours: same idea with our
  computed matrix; use one hue (blue/violet scale), 5 steps, with a legend ("Peor ↔ Mejor") and a tooltip on
  hover with the score.
- Empty week: no placeholder text; clicking a slot opens the composer with that date/time.

## 3. Crear publicación — composer (screens 3, 4, 5)

Opens as a **full-screen overlay page** (not a small modal): grey header bar with title **"Crear nueva
publicación"** + brand chip (avatar + brand name) on the left and **"× Cerrar"** on the right. Below, two big
white cards side by side: **editor (≈58 % width) left, preview (≈42 %) right**.

### 3.1 Editor card (left)

1. **Network selector row** (top-left): one round network logo per available account (Facebook, Instagram,
   TikTok) — unselected logos are grey/outlined, selected ones show their brand colour. Each selected network
   that has formats shows a small **format chip with dropdown** next to it: `POST ⌄`, `REEL ⌄` (and a tiny
   format badge on the logo's corner — grid icon for post, play icon for reel). TikTok shows no format chip
   (video only). A **"+"** at the end adds/connects more accounts. Far right of the row: **"Notas"** button
   (note icon) for internal notes.
2. When ≥ 2 networks are selected a button **"✎ EDITAR POR RED SOCIAL"** appears under the row (switches the
   editor to per-network tabs; ours: "Común" + one tab per network, each tab with "Volver al texto común").
3. **Text area**: large borderless editor inside a bordered box (min ≈ 300 px tall). Attached media render as
   **thumbnails inside the box** (≈ 96 px rounded squares; videos with a ▶ overlay; a "•••" menu on each thumb:
   ver, mover, quitar, portada).
4. **Editor toolbar** at the bottom of the box (icon buttons, no labels): image/video (add media), emoji,
   **#** (hashtags; appears when Instagram is selected), speech bubble (**first comment**), location pin
   (post only), link, magic wand (AI — skip for us), bookmark (save as idea / to library).
   Right side of the toolbar: **counters** — for Instagram a hashtag counter **"0 / 30 #"**, and a character
   counter **"0 / 16192"** followed by the logo of the network whose limit is the strictest among the selected
   ones (screen 5 shows "0 / 2000" + TikTok logo when FB+IG+TikTok are selected).
5. **"Configuración global"** collapsible section (gear icon, chevron): its header row also carries the
   **"Auto publicar (?)" toggle** when Instagram/TikTok are selected (screen 5). Inside: "Utilizar el acortador de
   URL" toggle and SmartLinks (both skip for us).
6. **Per-network collapsible sections** below, e.g. **"Configuración de Facebook"** (FB logo) containing
   "Título" (for reels/videos). Ours: "Configuración de Facebook / Instagram / TikTok" sections with each
   network's options (SPEC §7), TikTok section implementing TikTok's mandatory UX.
7. **Error panel** (pink background, red ⚠ icon, collapsible): header **"1 error"**, items with ⓘ icon, e.g.
   **"La fecha de publicación está en el pasado."**. While there are errors the "Programar" button is disabled
   (grey). Ours: same panel listing all `error` issues (and a separate amber panel for warnings).
8. **Footer bar**: **"Cancelar"** at the left; at the right a date-time button **"📅 15 oct 2026 9:20"** (opens
   picker) and the split button **"Programar" | ⌄** (dropdown: Publicar ahora, Guardar borrador, Guardar como
   idea). Disabled state = light grey with grey text.

### 3.2 Preview card (right)

- Top-left: **round network tabs** (one per selected network; active one filled dark). Top-right: segmented
  toggle **eye** (show/hide preview), **mobile** (active, dark) and **desktop** icons.
- Centered realistic mock:
  - Facebook post: white card with avatar, page name "Ascendia", date "15 October · 🌐", "•••", text, media, and
    a row of three rounded grey buttons (like / comment / share icons).
  - Facebook reel & TikTok: **phone-frame vertical video** (dark), play controls overlay, right-side action
    column (avatar, ♥ 0, 💬 0, ↗ 0, •••), page name + "Seguir" button and a music pill at the bottom.
    TikTok adds the "Siguiendo | Para ti" header and the bottom nav (Home, Friends, +, Inbox, Profile).
  - (Instagram post/reel/story mocks analogous.)
- Bottom: info note (light-blue background, blue ⓘ): **"Las previsualizaciones son una aproximación de cómo se
  verá la publicación. El resultado final puede ser diferente."**

## 4. Analítica (screen 2)

- **Left sidebar** (≈ 290 px, white): "Resumen" (clipboard icon) then one row per network with its logo and
  name — Facebook, Instagram, **TikTok (active: dark filled pill, white text)**, YouTube, LinkedIn; connected
  rows show the brand avatar on the right, unconnected rows a **"(+)"** circle; below, an outlined button
  **"+ Más conexiones"**. A separator, then secondary links: Reporting, Informes, Hashtag Tracker,
  Ajustes de marca. → Ours: Resumen, Facebook, Instagram, TikTok (+ "Conectar" for missing ones), then
  "Exportar CSV" and "Cuentas conectadas".
- **Content header**: small uppercase tabs **COMUNIDAD · CUENTA · POSTS** at the left; at the right two labelled
  pickers: **"Periodo principal"** (calendar icon, "15 sep 2026 - 14 oct 2026") and **"Periodo de comparación"**
  (dropdown).
- **Info alert** (light blue box, blue border, ⓘ): e.g. "Es posible que algunas métricas de TikTok no estén
  disponibles durante los últimos 1-2 días. El número de seguidores solo estará disponible desde el día en que la
  cuenta se conectó." → Ours: equivalent per-network notes (explain data starts accumulating at connection).
- Section title **"Comunidad"** with the account avatar + username at the right.
- **"Crecimiento" card**: title at top-left; at top-right **coloured KPI tiles** (≈ 190×85 px, filled colour,
  big number + label): **"0 Seguidores"** (periwinkle/blue tile) and **"4 Posts"** (amber tile); below, a
  combined chart over the period (line for followers, amber bars for posts per day), x-axis dates "15 sep … 12
  oct". Ours: same pattern following the dataviz skill (our palette, accessible contrast).

## 5. Inbox (screen 6 — described, no file)

Two white cards side by side:
- **Left list panel (≈ 1/3 width)**: top row of round network filter logos (Facebook, Instagram — with a small
  red "!" badge when the connection has a problem —, TikTok, "+"); search input **"Escribe al menos 3
  caracteres"** + funnel filter button; tabs **"No resueltos"** (active, underlined) · **"No leídos"** ·
  **"Todos"** · kebab `⋮`. Empty state centred: outline inbox-tray icon, **"No hemos encontrado conversaciones
  recientes"** and the hint "Por favor, asegúrate de que el filtro configurado no esté descartando las
  conversaciones disponibles."
- **Right conversation panel (≈ 2/3 width)**: empty state with a soft illustration (speech bubble with "!") and
  **"Por favor, para empezar selecciona una conversación a la izquierda"**. When a conversation is selected:
  the post/media context on top, the comment thread, and a reply box at the bottom.
- → Ours (`/comentarios`): same two-panel layout; tabs "Sin responder · No leídos · Todos"; network filter
  logos with error badge when the account needs reconnecting; search (≥ 3 chars); right panel shows the post
  (thumbnail + caption + link "Ver en Instagram/Facebook") and the comment with replies, reply box, "Marcar como
  resuelto".

## 6. Global visual cues to reproduce (with our colours)

- Rounded everything (buttons 10–12 px, cards 16 px, pills fully rounded), 1 px light borders, soft shadows.
- Font: geometric sans (Metricool uses a Nunito-like rounded font) → ours: system UI stack (offline), weights
  400/600/700.
- Dark filled primary CTA; secondary buttons white with border; disabled = light grey.
- Info alerts light blue; error panels light pink with red icon; KPI tiles colour-filled.
- Lots of white space; toolbar controls aligned in one row with consistent 44 px height.
