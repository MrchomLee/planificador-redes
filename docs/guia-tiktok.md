# Guía: conectar TikTok

Para publicar vídeos en TikTok, Planificador usa una app de TikTok que creas tú en el portal de
desarrolladores (gratis). Esta guía usa el **modo Sandbox** (de pruebas) de TikTok: no necesita web propia ni
revisión, y es suficiente para una persona que publica en su propia cuenta.

> **Lo más importante antes de empezar**
>
> - Mientras tu app no pase la **auditoría** de TikTok (algo pensado para empresas que ofrecen el servicio a
>   otras personas), TikTok **solo permite publicar en privado** («Solo yo»). Después puedes cambiar la
>   visibilidad del vídeo a «Todo el mundo» desde la app de TikTok.
> - Alternativa sin esa limitación: **Enviar a la app de TikTok como borrador**. El vídeo llega a tu app de
>   TikTok y tú lo terminas allí (música, efectos, visibilidad) con un par de toques. Ver el paso 8.
> - Las **fotos** (carruseles) de TikTok no se pueden publicar desde una app local; solo **vídeos**.
>
> El portal de TikTok está en inglés: los nombres de los botones van en inglés tal cual aparecen.

## 1. Date de alta en TikTok for Developers

1. Entra en <https://developers.tiktok.com/> y pulsa **Log in**. Entra con la misma cuenta de TikTok en la que
   vas a publicar (o con otra tuya; luego la añadirás como usuario de prueba).
2. Acepta los términos para desarrolladores (*Developer Terms of Service*) si te los muestra.

## 2. Crea la app

1. Arriba a la derecha abre tu menú y pulsa **Manage apps** (o ve a **My apps**).
2. Pulsa **Connect an app** (o **Create app**). Elige como propietario tu cuenta **individual** (no hace falta
   una organización) y escribe un nombre, por ejemplo `Planificador de Mi Marca`.
3. En la página de la app, sección **Basic information** (*App details*), rellena lo que pida: icono, nombre,
   categoría, descripción corta (por ejemplo «Herramienta personal para programar mis vídeos»). Los campos de
   **Terms of Service URL** y **Privacy Policy URL** solo son obligatorios para enviar la app a revisión; si te
   los pide, sirve una página gratuita tuya (por ejemplo un documento de Google Docs publicado en la web).

## 3. Activa la plataforma «Desktop»

TikTok solo acepta direcciones `http://localhost` en la plataforma **Desktop** (la plataforma *Web* exige
`https`).

1. En **Platforms** marca **Desktop**. Si te pide una URL de escritorio o web oficial, pon la de tu página o
   perfil.
2. Puedes dejar **Web** sin marcar.

## 4. Añade los productos Login Kit y Content Posting API

1. En la página de la app pulsa **Add products** (*Products*).
2. Añade **Login Kit** y **Content Posting API**.
3. Dentro de **Content Posting API**, activa **Direct Post** (el interruptor de publicación directa) si
   aparece.
4. Dentro de **Login Kit**, en **Redirect URI**, en la sección **Desktop**, añade exactamente
   `http://localhost:8765/conexiones/callback/social1`

   La tienes con botón **Copiar** en Planificador: **☰ › Ajustes › Credenciales › TikTok**. (La dirección
   termina en `social1` a propósito: TikTok a veces rechaza direcciones que contienen la palabra «tiktok».)

## 5. Permisos (scopes)

En **Scopes** (o al añadir los productos) deben quedar activados:

| Scope | Para qué |
|---|---|
| `user.info.basic` | Tu nombre y foto de perfil al conectar |
| `video.publish` | Publicar vídeos directamente |
| `video.upload` | Enviar vídeos como borrador a la app de TikTok |

Opcionales, solo si tu app los tiene disponibles: `user.info.profile`, `user.info.stats` y `video.list` (para
las **Estadísticas** de TikTok). TikTok ya no los ofrece a todas las apps nuevas. Si los tienes, activa además
en Planificador **Ajustes › Credenciales › TikTok › Opciones avanzadas › Pedir permisos de estadísticas**. Si
no, deja ese interruptor apagado o TikTok mostrará un error al conectar.

## 6. Crea el Sandbox y añade tu cuenta como usuario de prueba

1. En la parte superior de la página de la app, cambia de **Production** a **Sandbox** y pulsa
   **Create Sandbox** (ponle un nombre, por ejemplo `Planificador`).
2. Dentro del Sandbox repite, si hace falta, los pasos 3 a 5: plataforma **Desktop**, productos **Login Kit**
   y **Content Posting API**, la **Redirect URI** y los **scopes**. Cada sandbox tiene su propia configuración.
3. En **Target users** (*Sandbox settings › Target Users*) pulsa **Add account** e inicia sesión con la cuenta
   de TikTok en la que vas a publicar. Puedes añadir hasta 10 cuentas tuyas.
4. Copia la **Client key** y la **Client secret** que aparecen en el Sandbox (sección *Credentials*). Ojo: son
   distintas de las de *Production*.

## 7. Pega las credenciales y conecta

1. En Planificador ve a **☰ › Ajustes › Credenciales**, tarjeta **TikTok**: pega la **Client Key** y la
   **Client Secret** y pulsa **Guardar**.
2. Ve a **☰ › Cuentas conectadas** y, en la tarjeta de **TikTok**, pulsa **Conectar TikTok**.
3. TikTok te pide iniciar sesión y autorizar: **marca todos los permisos** y acepta.
4. Vuelves a Planificador con la tarjeta de TikTok en verde.

### Tu cuenta y la visibilidad «Solo yo»

Con una app sin auditar, TikTok solo ofrece **«Solo yo»** en *¿Quién puede ver la publicación?*. Planificador
lo selecciona solo y te avisa. Después de publicar, abre el vídeo en la app de TikTok › **…** › **Ajustes de
privacidad** y cámbialo a **Todo el mundo**.

Si al publicar aparece el error «solo se puede publicar en cuentas privadas»
(`unaudited_client_can_only_post_to_private_accounts`), TikTok exige además que la **cuenta** sea privada
mientras la app no esté auditada: en la app de TikTok ve a **Perfil › ☰ › Ajustes y privacidad › Privacidad**
y activa **Cuenta privada**. Si no quieres tener la cuenta privada, usa el modo borrador del paso 8.

## 8. Modo borrador: «Enviar a la app de TikTok como borrador»

En el editor de publicaciones, en **Configuración de TikTok**, activa **Enviar a la app de TikTok como
borrador**. A la hora programada:

1. Planificador sube el vídeo a tu bandeja de TikTok (no se publica todavía).
2. En tu móvil, TikTok te muestra una notificación (o lo ves en la **Bandeja de entrada**). Ábrela.
3. Termina en la app: añade música o efectos, pega el texto (TikTok **no** recibe el texto desde la app;
   Planificador te lo deja para **Copiar texto** en la publicación) y elige la visibilidad que quieras,
   incluida **Todo el mundo**.

Límite de TikTok: como máximo **5 borradores pendientes cada 24 horas**. El borrador no tiene la limitación de
«Solo yo».

## 9. ¿Y la auditoría?

Para publicar directamente en público sin pasos manuales, TikTok exige pasar la app a **Production** (revisión
con web propia, política de privacidad y vídeos de demostración) y además una **auditoría de Content Posting**.
TikTok suele rechazar herramientas de uso personal, así que no lo recomendamos: el modo «Solo yo» o el modo
borrador cubren el uso diario.

## 10. Problemas frecuentes

| Lo que ves | Qué pasa y cómo arreglarlo |
|---|---|
| «redirect_uri» o «Something went wrong» al conectar | La Redirect URI no coincide o está en la plataforma *Web*. Regístrala en **Desktop** (paso 4) dentro del Sandbox que estás usando. |
| Error de `code_challenge` al conectar | En **Ajustes › Credenciales › TikTok › Opciones avanzadas**, cambia **Codificación PKCE** a `base64url` y prueba otra vez. |
| «scope» o «permission» al conectar | Pediste permisos que tu app no tiene. Apaga **Pedir permisos de estadísticas** o activa esos scopes en tu app. |
| «Este usuario no está autorizado» | La cuenta no está en **Target users** del Sandbox (paso 6). |
| Solo puedo elegir «Solo yo» | Normal sin auditoría (ver arriba). |
| «Has alcanzado el límite de borradores» | Más de 5 borradores pendientes en 24 h: publica o borra los que tienes en la app. |
| No veo los comentarios de TikTok en **Comentarios** | La API pública de TikTok no permite leerlos ni responderlos. Revísalos en la app de TikTok. |
| La tarjeta dice **Requiere reconexión** | El acceso caduca si pasan muchos meses sin usarse o si lo revocas en TikTok. Pulsa **Reconectar**. |

Más guías: [Meta (Facebook e Instagram)](guia-meta.md) · [YouTube](guia-youtube.md) ·
[Cómo usar Planificador](guia-uso.md).
