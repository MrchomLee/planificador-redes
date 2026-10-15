# Guía: conectar Facebook e Instagram (app de Meta)

Planificador publica en tu **Página de Facebook** y en tu **cuenta profesional de Instagram** a través de una
«app» de Meta que creas tú, gratis, en unos 20 minutos. Es tuya: nadie más la usa y no hace falta que Meta la
revise. Solo se hace una vez; después basta con pulsar **Conectar**.

> **Antes de empezar necesitas**
>
> - Una cuenta personal de Facebook que sea **administradora** (con control total) de la Página.
> - Para Instagram: una cuenta **profesional** (de empresa o de creador) **vinculada a esa Página** (paso 7).
> - Planificador abierto en tu ordenador (<http://localhost:8765>).
>
> Los nombres de los menús de Meta cambian a menudo. Aquí van en español y, entre paréntesis, en inglés por si
> tienes el panel en inglés. Si algo no coincide exactamente, busca el texto más parecido.

## 1. Date de alta como desarrollador de Meta

1. Entra en <https://developers.facebook.com/> con tu cuenta de Facebook.
2. Arriba a la derecha pulsa **Empezar** (*Get Started*) o **Mis apps** (*My Apps*).
3. Si es la primera vez, Meta te pide aceptar las condiciones, verificar tu cuenta (normalmente con un código
   al móvil o al correo) y elegir tu ocupación. Cualquier opción vale (por ejemplo «Desarrollador»).

## 2. Crea la app (tipo «Negocios»)

1. En **Mis apps** pulsa **Crear app** (*Create App*).
2. **Detalles de la app**: escribe un nombre (por ejemplo `Planificador de Mi Marca`; no uses las palabras
   «Facebook», «Instagram» ni «Meta») y tu correo de contacto. Pulsa **Siguiente**.
3. **Casos de uso** (*Use cases*): baja hasta el final y elige **Otro** (*Other*). Pulsa **Siguiente**.
4. **Tipo de app**: elige **Negocios** (*Business*). Pulsa **Siguiente**.
5. Si te pide una **cartera comercial** (*Business portfolio*), puedes dejar «No quiero conectar una cartera
   comercial todavía». Pulsa **Crear app** y escribe tu contraseña de Facebook si la pide.

Ahora estás en el **Panel** (*Dashboard*) de la app.

## 3. Añade los productos «Inicio de sesión con Facebook para empresas» e «Instagram»

1. En el panel, busca la sección **Agregar productos a tu app** (*Add products to your app*). Si no la ves,
   en el menú de la izquierda pulsa **Agregar producto** (*Add Product*).
2. En **Inicio de sesión con Facebook para empresas** (*Facebook Login for Business*) pulsa **Configurar**
   (*Set up*).
3. Vuelve a la lista de productos y en **Instagram** pulsa **Configurar**. Dentro elige **Configuración de la
   API con inicio de sesión con Facebook** (*API setup with Facebook login*). No hace falta rellenar nada más
   ahí: Planificador usa el inicio de sesión de Facebook también para Instagram.

## 4. Registra la dirección de vuelta (URI de redirección)

Cuando conectes tus cuentas, Facebook tiene que devolverte a Planificador. Para eso hay que darle la dirección
exacta:

1. En el menú izquierdo abre **Inicio de sesión con Facebook para empresas › Configuración**
   (*Facebook Login for Business › Settings*).
2. En **URI de redireccionamiento de OAuth válidos** (*Valid OAuth Redirect URIs*) pega exactamente
   `http://localhost:8765/conexiones/callback/meta`

   (También la tienes con un botón **Copiar** en Planificador: **☰ › Ajustes › Credenciales › Meta**.)
3. Comprueba que **Inicio de sesión con OAuth de cliente** y **Inicio de sesión con OAuth web** están en **Sí**
   y que **Usar modo estricto para URI de redireccionamiento** también está en **Sí**.
4. Pulsa **Guardar cambios** abajo del todo.

> Si cambiaste el puerto de Planificador (por ejemplo `--port 8799`), la dirección cambia igual:
> `http://localhost:8799/conexiones/callback/meta`. Lo más fácil es dejar siempre el 8765.

## 5. Copia el identificador y la clave secreta a Planificador

1. En el menú izquierdo abre **Configuración de la app › Básica** (*App settings › Basic*).
2. Copia el **Identificador de la app** (*App ID*), un número largo.
3. Junto a **Clave secreta de la app** (*App secret*) pulsa **Mostrar**, escribe tu contraseña si la pide, y
   cópiala. Es una contraseña: no la compartas con nadie.
4. En Planificador ve a **☰ › Ajustes › Credenciales**, tarjeta **Meta (Facebook e Instagram)**: pega el
   número en **App ID**, la clave en **App Secret** y pulsa **Guardar**. La tarjeta pasa a «Configurado».

La clave se guarda cifrada en tu ordenador y nunca se vuelve a mostrar.

## 6. Permisos que pedirá Planificador

No tienes que activar nada a mano: mientras la app esté en modo **Desarrollo**, todos los permisos funcionan
para ti porque eres el administrador de la app (Meta lo llama *acceso estándar*). Al conectar, Facebook te
mostrará esta lista; **acepta todos** o algunas funciones no irán:

| Permiso | Para qué lo usa Planificador |
|---|---|
| `pages_show_list` | Ver la lista de tus Páginas para elegir cuál conectar |
| `pages_manage_posts` | Publicar en la Página (textos, fotos, vídeos, reels e historias) |
| `pages_read_engagement` | Leer tus publicaciones y sus reacciones |
| `read_insights` | Estadísticas de la Página y de cada publicación |
| `pages_manage_engagement` | Responder comentarios desde **Comentarios** |
| `pages_read_user_content` | Leer los comentarios con el nombre de quien los escribe |
| `instagram_basic` | Ver tu cuenta de Instagram vinculada |
| `instagram_content_publish` | Publicar en Instagram (fotos, carruseles, reels e historias) |
| `instagram_manage_insights` | Estadísticas de Instagram |
| `instagram_manage_comments` | Leer y responder comentarios de Instagram, primer comentario |
| `business_management` | Encontrar Páginas que gestionas desde una cartera comercial (Meta Business Suite) |

No hace falta pedir **acceso avanzado** ni enviar la app a **revisión** (*App Review*): eso solo es para apps
que usan otras personas.

> Si tu app de tipo Negocios te obliga a usar una **configuración** del inicio de sesión para empresas, crea una
> con el tipo de token **Token de acceso de usuario** (*User access token*), marca los permisos de la tabla y
> pega su número en **Ajustes › Credenciales › Meta › Opciones avanzadas › ID de configuración**. Si no sabes
> qué es esto, déjalo vacío.

## 7. Prepara Instagram: cuenta profesional vinculada a la Página

Solo se pueden programar publicaciones en cuentas **profesionales** de Instagram que estén **vinculadas a una
Página de Facebook**. Las cuentas personales no funcionan.

**Pasar a cuenta profesional** (en la app de Instagram del móvil):

1. Ve a tu perfil y toca **☰** (arriba a la derecha).
2. **Configuración y actividad › Tipo de cuenta y herramientas › Cambiar a cuenta profesional**.
3. Elige una categoría y después **Empresa** o **Creador**. Te recomendamos **Empresa**: las **historias** solo
   se pueden publicar desde apps en cuentas de empresa.

**Vincular la cuenta a tu Página** (desde un ordenador):

1. Entra en Facebook **con el perfil de la Página** (pulsa tu foto arriba a la derecha y cambia a la Página).
2. Abre **Configuración › Cuentas vinculadas** (*Linked accounts*) y en **Instagram** pulsa **Conectar cuenta**.
3. Inicia sesión en Instagram y acepta. También puedes hacerlo desde **Meta Business Suite › Configuración ›
   Cuentas de Instagram**.

## 8. Conecta tus cuentas (con la app en modo Desarrollo)

1. Comprueba que tu app de Meta está en modo **Desarrollo** (*Development*): lo ves en la parte superior del
   panel de la app.
2. En Planificador abre **☰ › Cuentas conectadas** y, en la tarjeta de **Facebook** (o de
   **Instagram**), pulsa **Conectar Facebook**.
3. Facebook te pregunta con qué cuenta continuar y qué Páginas y cuentas de Instagram compartir: **marca tu
   Página y tu cuenta de Instagram** y acepta todos los permisos.
4. De vuelta en Planificador eliges la Página (y su Instagram vinculado). Listo: verás las tarjetas en verde
   («Conectada»).

Si la Página aparece con un aviso de que **no tienes permiso para publicar** (`CREATE_CONTENT`), pide a quien
la administra que te dé **control total** de la Página y vuelve a conectar.

## 9. Pasa la app a modo «Activo» (muy importante para Facebook)

Mientras la app está en **Desarrollo**, las publicaciones que haga en tu Página de Facebook **solo las ves tú**
(y las demás personas con rol en la app). Para que las vea todo el mundo hay que pasarla a **Activo** (*Live*).
Hazlo **después** de conectar las cuentas (paso 8):

1. Necesitas una **URL de política de privacidad** pública. Si no tienes web, sirve una página gratuita: por
   ejemplo un documento de Google Docs publicado (**Archivo › Compartir › Publicar en la web**) o una página de
   Notion o de GitHub Pages con un texto como: «Esta herramienta solo la uso yo para publicar en mis propias
   redes. No recopila datos de otras personas».
2. En **Configuración de la app › Básica** rellena:
   - **URL de la política de privacidad** (*Privacy Policy URL*): la dirección del punto anterior.
   - **Eliminación de datos de usuarios** (*User data deletion*): elige «URL de instrucciones para la
     eliminación de datos» y pon la misma dirección.
   - **Icono de la app** (una imagen cuadrada de 1024 × 1024 px), **Categoría** (por ejemplo «Empresas y
     páginas») y **Correo electrónico de contacto**.
   - Pulsa **Guardar cambios**.
3. Cambia el **Modo de la app** de **Desarrollo** a **Activo**: es un interruptor en la barra superior del
   panel o la opción **Publicar** (*Publish*) del menú de la izquierda, según la versión del panel.

Tus conexiones siguen funcionando: los tokens de Página no caducan. Meta no exige verificar tu negocio para una
app que solo usas tú.

> **Si más adelante tienes que reconectar** (por ejemplo, cambiaste la contraseña de Facebook) y Facebook
> muestra un error de **URL bloqueada** o de redirección no válida, es porque en modo **Activo** Meta puede
> rechazar direcciones `http://localhost`. Solución: pon la app un momento en **Desarrollo**, reconecta en
> Planificador y vuelve a ponerla en **Activo**.

## 10. Fotos de Instagram: activa la URL pública

Instagram no acepta fotos enviadas directamente desde tu ordenador: las **descarga** desde una dirección
`https` pública. Tu ordenador no tiene una, así que Planificador puede abrir una de forma temporal:

1. Ve a **☰ › Ajustes › URL pública para Instagram**.
2. Elige **Túnel temporal de Cloudflare** (recomendado), lee cómo funciona, marca la casilla de conformidad y
   pulsa **Guardar**.
3. La primera vez descarga el programa oficial `cloudflared` (unas decenas de MB). A partir de ahí, cuando
   Instagram necesita una foto, se abre un enlace `https://….trycloudflare.com` que sirve **solo esa foto**,
   caduca en 1 hora y se cierra a los pocos minutos. El resto de la aplicación nunca queda accesible desde
   Internet.

Si tienes tu propio dominio o túnel, elige **URL pública propia**. Los **vídeos y reels** de Instagram, y todo
lo de Facebook, **no** necesitan la URL pública: se suben directamente desde tu ordenador. Sin URL pública,
las fotos de Instagram se quedan en **Publicación asistida** (te avisamos y las publicas tú).

## 11. Problemas frecuentes

| Lo que ves | Qué pasa y cómo arreglarlo |
|---|---|
| «URL bloqueada» o «El URI de redireccionamiento no está en la lista» | La dirección del paso 4 no coincide. Cópiala otra vez desde **Ajustes › Credenciales › Meta** (sin espacios ni barra final) y guarda. Si la app está en **Activo**, ponla un momento en **Desarrollo** (paso 9). |
| «Falta configurar tu app de Meta» | No has guardado el App ID y el App Secret (paso 5). |
| Tus publicaciones de Facebook solo las ves tú | La app sigue en **Desarrollo**. Pásala a **Activo** (paso 9). |
| No aparece tu cuenta de Instagram al conectar | La cuenta no es profesional o no está vinculada a la Página (paso 7). Vuelve a conectar y márcala en la pantalla de Facebook. |
| «The user is not an Instagram Business» al publicar una historia | Las historias solo funcionan en cuentas de **empresa**. Cambia el tipo de cuenta en Instagram. |
| «Activa la URL pública en Ajustes para publicar fotos en Instagram» | Paso 10. |
| La tarjeta dice **Requiere reconexión** | Facebook invalidó el acceso (cambio de contraseña, permisos quitados…). Pulsa **Reconectar** en **Cuentas conectadas**. |
| Un permiso aparece como no concedido | Desconecta, vuelve a conectar y, en la pantalla de Facebook, pulsa **Editar acceso** y marca todo. |

Más guías: [TikTok](guia-tiktok.md) · [YouTube](guia-youtube.md) · [Cómo usar Planificador](guia-uso.md).
