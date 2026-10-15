# Guía: conectar YouTube (proyecto de Google Cloud)

Planificador sube vídeos y Shorts a tu canal, lee estadísticas y comentarios con un **proyecto de Google
Cloud** que creas tú (gratis, sin tarjeta). Se hace una vez y tarda unos 15 minutos.

> **Antes de empezar, lo importante**
>
> - Google puede **bloquear como privados** los vídeos subidos por proyectos que no han pasado la auditoría de
>   YouTube (y un vídeo bloqueado no se puede hacer público después). Por eso, hasta que hagas la **prueba de
>   bloqueo** (paso 9), Planificador usa la **Publicación asistida** para YouTube: prepara todo, te avisa a la
>   hora y tú subes el vídeo en YouTube Studio en un minuto.
> - Las estadísticas y los comentarios funcionan siempre, con o sin auditoría.
> - Los menús de Google Cloud aparecen en español si tu cuenta de Google está en español. Entre paréntesis va el
>   nombre en inglés.

## 1. Crea un proyecto

1. Entra en <https://console.cloud.google.com/> con la cuenta de Google que es propietaria (o administradora)
   de tu canal de YouTube. Acepta las condiciones si es la primera vez.
2. Arriba a la izquierda pulsa el **selector de proyectos** › **Proyecto nuevo** (*New project*).
3. Nombre: `Planificador`. Ubicación: **Sin organización**. Pulsa **Crear** y, cuando termine, selecciónalo en
   el selector de proyectos.

## 2. Habilita las dos API de YouTube

1. Menú **☰ › APIs y servicios › Biblioteca** (*APIs & Services › Library*).
2. Busca **YouTube Data API v3**, ábrela y pulsa **Habilitar** (*Enable*).
3. Vuelve a la biblioteca, busca **YouTube Analytics API** y pulsa **Habilitar**.

## 3. Configura la pantalla de consentimiento (Google Auth Platform)

1. Menú **☰ › APIs y servicios › Pantalla de consentimiento de OAuth**. Google te lleva a **Google Auth
   Platform**; pulsa **Comenzar** (*Get started*).
2. **Información de la app**: nombre `Planificador` y tu correo como **correo de asistencia**. **Siguiente**.
3. **Público** (*Audience*): elige **Externo** (*External*). **Siguiente**.
4. **Información de contacto**: tu correo. **Siguiente**, acepta la política y pulsa **Crear**.
5. En el menú de Google Auth Platform abre **Acceso a datos** (*Data access*) › **Agregar o quitar permisos**
   (*Add or remove scopes*). En **Agregar manualmente permisos** pega las cuatro líneas de abajo, pulsa
   **Agregar a la tabla**, **Actualizar** y **Guardar**.

```
https://www.googleapis.com/auth/youtube.upload
https://www.googleapis.com/auth/youtube.readonly
https://www.googleapis.com/auth/youtube.force-ssl
https://www.googleapis.com/auth/yt-analytics.readonly
```

## 4. Publica la app «En producción» (sin verificar)

Si la dejas en **Prueba** (*Testing*), Google borra el permiso cada **7 días** y tendrías que reconectar YouTube
cada semana. Para evitarlo:

1. En Google Auth Platform abre **Público** (*Audience*).
2. En **Estado de publicación** pulsa **Publicar aplicación** (*Publish app*) y confirma. El estado pasa a
   **En producción** (*In production*).
3. **No** pidas la verificación de Google: no hace falta para uso personal (menos de 100 usuarios). La única
   consecuencia es un aviso «Google no ha verificado esta aplicación» al conectar (paso 7).

> Si el botón **Publicar aplicación** está gris, Google pide antes una **página principal** y una **política de
> privacidad**. En **Desarrollo de la marca** (*Branding*) rellena *Página principal de la aplicación* y
> *Vínculo a la política de privacidad* (sirve una página gratuita, por ejemplo de GitHub Pages o un documento
> de Google Docs publicado en la web) y añade su dominio en **Dominios autorizados**. Luego vuelve a **Público**.
>
> Si prefieres dejarla en **Prueba**, añade tu correo en **Público › Usuarios de prueba** y acepta reconectar
> cada 7 días: Planificador te avisará cuando haya que reconectar.

## 5. Crea el cliente OAuth («Aplicación web»)

1. En Google Auth Platform abre **Clientes** (*Clients*) › **Crear cliente** (*Create client*).
2. **Tipo de aplicación**: **Aplicación web** (*Web application*). Nombre: `Planificador`.
3. En **URI de redireccionamiento autorizados** (*Authorized redirect URIs*) pulsa **Agregar URI** y pega
   exactamente `http://localhost:8765/conexiones/callback/google`

   (Con botón **Copiar** en Planificador: **☰ › Ajustes › Credenciales › Google (YouTube)**.) No hace falta
   rellenar *Orígenes de JavaScript autorizados*.
4. Pulsa **Crear**. Google muestra el **ID de cliente** (termina en `.apps.googleusercontent.com`) y el
   **Secreto del cliente**. **Cópialos ahora** (o pulsa **Descargar JSON**): Google puede no volver a mostrar el
   secreto completo. Si lo pierdes, abre el cliente y pulsa **Agregar secreto** para crear uno nuevo.

## 6. Pega las credenciales en Planificador

1. **☰ › Ajustes › Credenciales**, tarjeta **Google (YouTube)**.
2. Pega el **ID de cliente** y el **Secreto de cliente** y pulsa **Guardar**.

## 7. Conecta tu canal

1. **☰ › Cuentas conectadas** › tarjeta **YouTube** › **Conectar YouTube**.
2. Elige tu cuenta de Google. Si tu canal es de una **cuenta de marca**, Google te deja elegir el canal.
3. Aparece **«Google no ha verificado esta aplicación»**: es tu propia app. Pulsa **Configuración avanzada**
   (*Advanced*) › **Ir a Planificador (no seguro)**.
4. **Marca todas las casillas** de permisos y pulsa **Continuar**. Si desmarcas alguna, esa función
   (subir, estadísticas o comentarios) no funcionará.
5. Si tienes varios canales, elige cuál conectar. La tarjeta de YouTube queda en verde.

## 8. Cómo se publica en YouTube: asistida o automática

En la tarjeta de YouTube de **Cuentas conectadas**, **Cómo se publican los vídeos**:

- **Asistida** (por defecto): a la hora programada recibes un aviso. Abres la publicación (campana o
  **Planificación**) y en la ventana de **Publicación asistida** tienes **Copiar** título, descripción y
  etiquetas, **Descargar** el vídeo y **Abrir YouTube Studio**. Subes el vídeo allí, pegas los datos y pulsas
  **Marcar como publicado**. En la siguiente actualización Planificador enlaza tu vídeo por el título.
- **Automática (API)**: Planificador sube el vídeo él solo, incluso programado en YouTube con **Programar en
  YouTube** (YouTube lo publica a su hora aunque tu ordenador esté apagado). Úsala solo si la prueba de
  bloqueo dice **desbloqueado** o si tu proyecto pasó la auditoría.

## 9. La prueba de bloqueo

Desde 2020 Google bloqueaba como **privados** los vídeos subidos por proyectos sin auditar. En octubre de 2026
la documentación de Google dice que ya no, pero la ayuda de YouTube sigue describiendo el bloqueo. La prueba lo
comprueba con tu proyecto:

1. **☰ › Cuentas conectadas** › tarjeta **YouTube** › **Prueba de bloqueo**.
2. Planificador sube un clip de 2 segundos como **oculto**, espera a que YouTube lo procese, mira qué
   visibilidad le ha dejado y lo borra. Tarda unos minutos y gasta muy poca cuota.
3. Resultado:
   - **Desbloqueado**: los vídeos subidos por la API mantienen su visibilidad. Puedes elegir **Automática
     (API)**.
   - **Bloqueado**: YouTube los deja en privado. Sigue con **Asistida**. Si algún día tu proyecto pasa la
     auditoría, marca **Mi proyecto de Google ha superado la auditoría de YouTube**.

Si recibes un correo de YouTube diciendo que un vídeo se ha «bloqueado como privado», ese vídeo no se puede
recuperar: bórralo y súbelo desde YouTube Studio.

## 10. Límites de YouTube que conviene saber

- **Cuota diaria** por proyecto: unas **100 subidas al día** y 10.000 unidades para el resto (estadísticas,
  comentarios…). Una persona no se acerca a esos límites. Se reinicia a medianoche (hora del Pacífico).
- **Shorts**: un vídeo vertical o cuadrado de **3 minutos o menos** es un Short automáticamente. En el editor
  elige el formato **Short** y Planificador comprueba la duración y la forma.
- **Miniatura personalizada** y vídeos de **más de 15 minutos** necesitan un canal **verificado** (con número de
  teléfono, en <https://www.youtube.com/verify>).
- **Contenido para niños**: es obligatorio indicarlo en cada vídeo («Sí» o «No»).

## 11. Problemas frecuentes

| Lo que ves | Qué pasa y cómo arreglarlo |
|---|---|
| «Error 400: redirect_uri_mismatch» | La URI del paso 5 no coincide exactamente, o creaste un cliente de tipo *Escritorio*. Crea un cliente **Aplicación web** con la URI exacta. |
| «Error 403: access_denied» | La app está en **Prueba** y tu correo no está en **Usuarios de prueba**, o no aceptaste los permisos. Paso 4. |
| «YouTube Data API v3 has not been used in project…» | Falta habilitar la API (paso 2). Espera unos minutos tras habilitarla. |
| «Falta el permiso para… YouTube» | Desmarcaste alguna casilla al conectar. Pulsa **Reconectar** y márcalas todas. |
| Tengo que reconectar cada semana | La app de Google sigue en **Prueba**. Publícala (paso 4). |
| «quotaExceeded» | Se acabó la cuota del día. Planificador lo reintenta después de medianoche (hora del Pacífico). |
| No aparece mi canal | Conecta con la cuenta de Google que gestiona el canal; en canales de marca, elige el canal en la pantalla de Google. |

Más guías: [Meta (Facebook e Instagram)](guia-meta.md) · [TikTok](guia-tiktok.md) ·
[Cómo usar Planificador](guia-uso.md).
