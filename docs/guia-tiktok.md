# Guía: conectar TikTok

Para publicar vídeos en TikTok, Planificador usa una «app» de TikTok que creas tú, gratis, en el portal de
desarrolladores de TikTok. Esta guía sigue el camino que funciona de verdad para una persona que publica en su
propia cuenta: todo se hace en el **Sandbox** (el modo de pruebas de la app) y no hace falta que TikTok revise
nada. Tardarás unos 20 minutos la primera vez.

> **Antes de empezar: lo importante**
>
> - Trabaja **siempre en el Sandbox**. La página *Production* no deja guardar sin rellenar la revisión de la app
>   (*App review*: explicación y vídeo de demostración) y no la necesitas. Nunca pulses **Save** en
>   *Production*: en el Sandbox se pulsa **Apply changes**.
> - TikTok pide **demostrar que tienes una web** (plataforma *Desktop*): hay que subir un archivo pequeño a tu
>   web. Si no tienes web, en el paso 5 te explicamos cómo hacer una gratis con GitHub Pages.
> - En el Sandbox la **publicación directa** (*Direct Post*) a veces no está disponible. No pasa nada: el vídeo
>   se envía como **borrador a tu app de TikTok** y lo terminas allí con un par de toques (paso 13).
> - En Planificador marcarás **exactamente los permisos (scopes) que tiene tu app**. Si pide uno que tu app no
>   tiene, TikTok rechaza la conexión con el error «scope».
> - Mientras la app no pase la auditoría de TikTok, la publicación directa solo puede ser **«Solo yo»**
>   (privada). Solo se pueden publicar **vídeos**, no fotos.
>
> El portal de TikTok está en inglés: los nombres de los botones van en inglés tal y como aparecen.

## 1. Date de alta en TikTok for Developers

1. Entra en <https://developers.tiktok.com/> y pulsa **Log in**. Entra con tu cuenta de TikTok (la misma en la
   que vas a publicar, u otra tuya).
2. Si te lo pide, acepta los términos para desarrolladores (*Developer Terms of Service*).

## 2. Crea la app

1. Arriba a la derecha, abre el menú de tu perfil y pulsa **Manage apps**.
2. Pulsa **Connect an app** (en algunas cuentas se llama **Create app**). Elige como propietario tu cuenta
   **individual** (no hace falta una organización) y ponle un nombre, por ejemplo `Planificador de Mi Marca`.
3. Se abre la página de la app en modo **Production**. **No rellenes nada aquí ni pulses Save ni Submit for
   review**: TikTok no deja guardar *Production* sin la revisión de la app, y no la necesitas. Sigue en el
   paso 3.

## 3. Cambia al Sandbox

1. Arriba, junto al nombre de la app, hay un selector **Production | Sandbox**. Pulsa **Sandbox**.
2. Pulsa **Create Sandbox**, ponle un nombre (por ejemplo `Planificador`) y confirma.
3. A partir de aquí todo se hace **dentro del Sandbox**: comprueba siempre que el selector marca **Sandbox**.
4. Los cambios del Sandbox no cuentan hasta que pulsas **Apply changes** (arriba a la derecha). Púlsalo al
   terminar cada paso; como mínimo, una vez al final (paso 9).
5. En la parte de datos básicos (*Basic information* o *App details*) pon el icono, la categoría y una
   descripción corta, por ejemplo «Herramienta personal para programar mis vídeos».

## 4. Qué vas a configurar

| Dónde, dentro del Sandbox | Qué | Paso |
|---|---|---|
| **Platforms** | **Desktop** con la dirección de tu web, verificada | 5 |
| **Login Kit** | La dirección de vuelta (**Redirect URI**) en **Desktop** | 6 |
| **Content Posting API** | Publicar vídeos (y **Direct Post**, si aparece) | 7 |
| **Scopes** y **Target users** | Qué permisos tiene la app y qué cuenta puede usarla | 8 |
| **Credentials** | La **Client key** y la **Client secret** del Sandbox | 9 |

## 5. Plataforma «Desktop» y verificación de tu web

Planificador funciona en tu ordenador (`http://localhost`) y TikTok solo acepta esas direcciones en la
plataforma **Desktop**. A cambio, TikTok pide la dirección de tu web y que demuestres que es tuya subiendo un
archivo que te da.

1. En **Platforms** marca **Desktop**. Puedes dejar **Web**, **Android** e **iOS** sin marcar.
2. En la dirección de la web que pide **Desktop**, escribe la dirección principal de tu web terminada en `/`.
   Ejemplos: `https://ascendiamx.com/` (web propia) o `https://tuusuario.github.io/` (GitHub Pages, gratis).
3. TikTok pide verificar esa dirección. Pulsa **Verify**, elige **URL prefix**, comprueba que la dirección es
   la misma del punto anterior y pulsa **Download**. Se descarga un archivo de texto con un nombre parecido a
   `tiktokAbC123xyz.txt`. **No le cambies el nombre ni lo abras para editarlo.**
4. Sube ese archivo a la **raíz** de tu web (la carpeta principal) con la opción A o la B de abajo. Tiene que
   abrirse en `https://tu-web/tiktokAbC123xyz.txt`, sin carpetas en medio.
5. Vuelve a TikTok y pulsa **Verify**. La dirección debe quedar como verificada. Pulsa **Apply changes**.

### Opción A: tienes web propia con hosting

1. Entra en el panel de tu hosting (cPanel, Hostinger, SiteGround, Plesk…) y abre el **Administrador de
   archivos** (*File Manager*).
2. Entra en la carpeta principal de tu web. Casi siempre se llama `public_html` (a veces `www` o `htdocs`).
3. Pulsa **Subir** (*Upload*) y sube el archivo `tiktok….txt` tal cual.
4. Abre en el navegador `https://tudominio.com/tiktok….txt` (con el nombre real): debe verse una línea de
   texto. Si se ve, vuelve a TikTok y pulsa **Verify**.

Si tu web está hecha con Wix, Squarespace o Shopify y no te deja subir archivos sueltos, usa la opción B.

### Opción B: web gratis con GitHub Pages

1. Crea una cuenta gratis en <https://github.com/> con **Sign up**. Apunta tu nombre de usuario (por ejemplo
   `mimarca`).
2. Arriba a la derecha pulsa **+** › **New repository**.
3. En **Repository name** escribe exactamente `tuusuario.github.io`: tu nombre de usuario, en minúsculas,
   seguido de `.github.io` (por ejemplo `mimarca.github.io`). Deja marcado **Public** y pulsa
   **Create repository**.
4. En la página del repositorio pulsa **uploading an existing file** (o **Add file** › **Upload files**),
   arrastra el archivo `tiktok….txt` y pulsa **Commit changes**.
5. Ve a **Settings** › **Pages**. En **Build and deployment** debe poner **Deploy from a branch**, la rama
   **main** y la carpeta **/ (root)**. Si no, elígelo y pulsa **Save**.
6. Espera uno o dos minutos y abre `https://tuusuario.github.io/tiktok….txt` (con el nombre real del archivo):
   debe verse una línea de texto.
7. En TikTok usa `https://tuusuario.github.io/` como dirección de **Desktop** y como **URL prefix**, y pulsa
   **Verify**.

Si TikTok te pide también una **Terms of Service URL** o una **Privacy Policy URL**, puedes crear esas páginas
en el mismo repositorio: **Add file** › **Create new file**, nombre `privacidad.html`, escribe un par de frases
(«Herramienta de uso personal; no recoge datos de terceros») y **Commit changes**. La dirección será
`https://tuusuario.github.io/privacidad.html`.

## 6. Login Kit y la dirección de vuelta (Redirect URI)

1. En el Sandbox pulsa **Add products** y añade **Login Kit**.
2. En la configuración de **Login Kit**, busca **Redirect URI** y, en el apartado **Desktop**, añade
   exactamente esta dirección:

   `http://localhost:8765/conexiones/callback/social1`

3. Cópiala con el botón **Copiar** de Planificador (**☰ › Ajustes › Credenciales › TikTok**) para que coincida
   letra por letra. Termina en `social1` a propósito: TikTok a veces rechaza direcciones que contienen la
   palabra «tiktok». Si cambiaste el puerto de Planificador, usa el número que muestra Ajustes.
4. Pulsa **Apply changes**.

## 7. Content Posting API (publicar vídeos)

1. Pulsa **Add products** y añade **Content Posting API**.
2. Dentro de **Content Posting API**, si aparece el interruptor **Direct Post**, actívalo. Es lo que da el
   permiso `video.publish` (publicación directa en tu perfil).
3. Si **Direct Post** no aparece o no se deja activar (pasa a menudo en el Sandbox), sigue igualmente: tu app
   tendrá `video.upload` y Planificador enviará los vídeos como **borrador a la app de TikTok** (paso 13).
4. Pulsa **Apply changes**.

## 8. Permisos (Scopes) y usuarios de prueba (Target users)

Abre la sección **Scopes** del Sandbox y **apunta qué permisos aparecen**: en el paso 10 los marcarás igual en
Planificador. Estos son los que usa Planificador:

| Scope | Para qué sirve en Planificador |
|---|---|
| `user.info.basic` | Iniciar sesión: tu nombre y tu foto. Lo tienen todas las apps. |
| `video.publish` | **Publicación directa**. Solo aparece si activaste **Direct Post**. |
| `video.upload` | **Enviar como borrador a la app de TikTok**. |
| `user.info.profile` | Estadísticas: tu @usuario y el enlace a tu perfil. Opcional. |
| `user.info.stats` | Estadísticas: seguidores, «me gusta» y número de vídeos. Opcional. |
| `video.list` | Estadísticas de tus vídeos públicos. Opcional. |

Después, en **Sandbox settings** › **Target users**, pulsa **Add account** e inicia sesión con la cuenta de
TikTok en la que vas a publicar. Solo esas cuentas (hasta 10, todas tuyas) podrán conectarse mientras la app
esté en el Sandbox.

## 9. Pulsa «Apply changes» y copia las credenciales del Sandbox

1. Pulsa **Apply changes**. Si no lo haces, TikTok sigue sin Login Kit ni Desktop y al conectar mostrará el
   error `client_key`.
2. En la sección **Credentials** del Sandbox copia la **Client key** y la **Client secret**. Son distintas de las
   de *Production*: usa siempre las del Sandbox.

## 10. Pega las credenciales y marca los permisos en Planificador

1. En Planificador ve a **☰ › Ajustes › Credenciales**, tarjeta **TikTok**, y pega la **Client Key** y la
   **Client Secret**.
2. En **Permisos (scopes) de tu app**, marca **exactamente** los que apuntaste en el paso 8:
   - `user.info.basic` va siempre marcado.
   - `video.publish` (**Publicación directa**): solo si tu app tiene **Direct Post**.
   - `video.upload` (**Enviar como borrador a la app de TikTok**).
   - `user.info.profile`, `user.info.stats` y `video.list` (**Estadísticas**): solo los que tenga tu app.
3. Tiene que haber al menos uno de los dos de publicación (`video.publish` o `video.upload`).
4. Pulsa **Guardar**.

Ejemplo habitual en el Sandbox: `user.info.basic`, `video.upload` y `video.list`, sin `video.publish`. Con eso
publicas como borrador en la app de TikTok y tienes estadísticas de tus vídeos públicos.

## 11. Conecta tu cuenta

1. Ve a **☰ › Cuentas conectadas** y, en la tarjeta de **TikTok**, pulsa **Conectar TikTok**.
2. TikTok te pide iniciar sesión (con una cuenta de **Target users**) y te enseña los permisos: acéptalos.
3. Vuelves a Planificador con la tarjeta de TikTok en verde. En **Modos de publicación disponibles** verás
   **Publicación directa**, **Borrador en la app** o los dos, según los permisos que tenga tu app.

Si más adelante cambias los permisos (en TikTok o en Ajustes), pulsa **Reconectar** en la tarjeta para que se
apliquen.

## 12. Publicación directa y «Solo yo»

Solo si tu app tiene `video.publish`. Con una app sin auditar, TikTok solo ofrece **«Solo yo»** en *¿Quién
puede ver la publicación?*. Después de publicar, abre el vídeo en la app de TikTok › **…** › **Ajustes de
privacidad** y cámbialo a **Todo el mundo**.

Si al publicar aparece el error «solo se puede publicar en cuentas privadas»
(`unaudited_client_can_only_post_to_private_accounts`), TikTok exige además que la **cuenta** sea privada
mientras la app no esté auditada: en la app de TikTok ve a **Perfil › ☰ › Ajustes y privacidad › Privacidad**
y activa **Cuenta privada**. Si no quieres tener la cuenta privada, usa el modo borrador.

## 13. Modo borrador: «Enviar a la app de TikTok como borrador»

En el editor de publicaciones, en **Configuración de TikTok**, activa **Enviar a la app de TikTok como
borrador**. Si tu app no tiene `video.publish`, este modo ya viene activado y bloqueado, con un aviso que lo
explica: es el único que TikTok te permite. A la hora programada:

1. Planificador sube el vídeo a tu bandeja de TikTok (todavía no se publica).
2. En el móvil, TikTok te avisa (o lo ves en la **Bandeja de entrada**). Ábrelo.
3. Termina en la app: añade música o efectos, pega el texto (TikTok **no** recibe el texto desde Planificador;
   cópialo con el botón **Copiar**, junto al texto, en la ficha de la publicación) y elige la visibilidad que
   quieras, incluida **Todo el mundo**.

En este modo no hace falta elegir visibilidad, comentarios ni aceptar la confirmación de música en
Planificador: todo eso se decide en la app de TikTok. Límite de TikTok: como máximo **5 borradores pendientes
cada 24 horas**. El borrador no tiene la limitación de «Solo yo».

## 14. Estadísticas de TikTok

Las estadísticas necesitan los permisos opcionales: `user.info.stats` para seguidores y «me gusta» de la cuenta
y `video.list` para las métricas de cada vídeo (TikTok solo las da de vídeos **públicos**). Planificador solo
consulta lo que tu app tiene concedido; si no tiene ninguno de los dos, **Estadísticas › TikTok** te lo explica
en lugar de mostrar gráficas vacías. Publicar no necesita estos permisos.

## 15. ¿Y Production y la auditoría?

Para salir del Sandbox, TikTok exige pasar a **Production** con revisión de la app (web, política de
privacidad, vídeo de demostración) y, para publicar en público sin pasos manuales, además una **auditoría de
Content Posting**. TikTok suele rechazar herramientas de uso personal, así que no lo recomendamos: el Sandbox
con «Solo yo» o con el modo borrador cubre el uso diario.

## 16. Problemas frecuentes

Cuando TikTok rechaza la conexión muestra su propia página de error (*Something went wrong*, *We couldn't log
in…*) y, en la dirección de esa página o en sus detalles, un `error_type`. Busca aquí lo que ves:

| Lo que ves | Qué pasa y cómo arreglarlo |
|---|---|
| `error_type=client_key` o `unauthorized_client` | TikTok no reconoce la Client Key para iniciar sesión. Casi siempre: en el **Sandbox** falta **Login Kit** o la plataforma **Desktop**, o no pulsaste **Apply changes** (pasos 5, 6 y 9). También pasa si pegaste la Client Key de **Production** en lugar de la del Sandbox. Revísalo, pulsa **Apply changes**, vuelve a pegar las credenciales del Sandbox en Ajustes y conecta otra vez. |
| `error_type=scope` | Planificador pidió un permiso que tu app no tiene (por ejemplo `video.publish` sin **Direct Post**, o los de estadísticas). En **Ajustes › Credenciales › TikTok** desmarca los permisos que no aparecen en **Scopes** de tu Sandbox, pulsa **Guardar** y conecta otra vez. |
| `error_type=redirect_uri` o un error de «redirect_uri» | La dirección de vuelta no está registrada en **Login Kit › Redirect URI › Desktop** del Sandbox, o no coincide letra por letra (paso 6). Si cambiaste el puerto de Planificador, regístrala con el puerto nuevo. |
| Error de `code_challenge` al conectar | En **Ajustes › Credenciales › TikTok › Opciones avanzadas**, cambia **Codificación PKCE** a `base64url` y prueba otra vez. |
| No puedo guardar la app: me pide **App review** | Estás en **Production**. Cambia el selector de arriba a **Sandbox** (paso 3) y configura allí; en el Sandbox se pulsa **Apply changes**, no **Save**. |
| La verificación de la web falla | El archivo `tiktok….txt` no está en la raíz o tiene otro nombre. Ábrelo en el navegador con la dirección exacta: debe verse el texto. La dirección debe empezar por `https://` y terminar en `/`. En GitHub Pages espera un par de minutos tras subirlo. |
| TikTok dice que la cuenta no está autorizada | La cuenta no está en **Target users** del Sandbox (paso 8). |
| En el editor no puedo desactivar «Enviar a la app de TikTok como borrador» | Tu app no tiene `video.publish` (sin **Direct Post**). Es normal en el Sandbox: el vídeo llega como borrador a tu app de TikTok. |
| La tarjeta dice que no hay ningún permiso de publicación | Tu app no tiene `video.publish` ni `video.upload`. Añade **Content Posting API** (paso 7), marca sus permisos en Ajustes y pulsa **Reconectar**. |
| Solo puedo elegir «Solo yo» | Normal sin auditoría (paso 12). |
| «Has alcanzado el límite de borradores» | Más de 5 borradores pendientes en 24 h: publica o borra los que tienes en la app de TikTok. |
| Estadísticas de TikTok vacías | Tu app no tiene `user.info.stats` ni `video.list`, o tus vídeos son privados (paso 14). |
| No veo los comentarios de TikTok en **Comentarios** | La API pública de TikTok no permite leerlos ni responderlos. Revísalos en la app de TikTok. |
| La tarjeta dice **Reconexión necesaria** | El acceso caduca si pasan muchos meses sin usarse o si lo revocas en TikTok. Pulsa **Reconectar**. |

Más guías: [Meta (Facebook e Instagram)](guia-meta.md) · [YouTube](guia-youtube.md) ·
[Cómo usar Planificador](guia-uso.md).
