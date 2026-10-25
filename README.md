# PeBOT 🇵🇪

Bot de Discord con un personaje peruano, basado en el ejemplo proporcionado.
Se llama **PeBOT** y puede usar OpenAI, DeepSeek y Google Gemini con cambio
automático de proveedor. Necesitas el token de Discord y la clave de al menos
uno de estos proveedores. Mantiene la personalidad y la memoria del canal al cambiar.

## Iniciar en Windows (Python 3.11 o posterior)

```powershell
cd \PeBOT\PeBOT
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
notepad .env
.\.venv\Scripts\python.exe bot.py
```

Completa `DISCORD_TOKEN` y al menos una clave de IA en `.env`, sin compartirlas
ni subirlas a Git. Si clonas el repo,
copia `.env.example` a `.env`. No hace falta Client Secret, Public Key ni Client ID
para ejecutar el bot. El Application ID se usa para generar su invitación.

## Proveedores de IA y respaldo automático

```dotenv
LLM_PROVIDERS=google,deepseek,openai
OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-flash
GOOGLE_API_KEY=
GOOGLE_MODEL=gemini-3.1-flash-lite
```

Solo se habilitan proveedores con una clave no vacía. Puedes cambiar la prioridad,
por ejemplo `LLM_PROVIDERS=openai,deepseek,google`, o usar solo uno. Para Google
también se acepta `GEMINI_API_KEY`. Obtén las claves en
[OpenAI](https://platform.openai.com/api-keys),
[DeepSeek](https://platform.deepseek.com/) o
[Google AI Studio](https://aistudio.google.com/apikey).
Cada proveedor aplica su propia facturación, disponibilidad y cuotas.

El bot hace una petición normal al proveedor activo. No consulta balances ni
gasta peticiones comprobando cada proveedor con cada mensaje. Si la petición
falla por saldo/cuota, límite, autenticación, modelo no disponible o caída del
servicio, pausa ese proveedor y prueba el siguiente con el mismo contexto.
El proveedor que funciona queda activo para los mensajes siguientes.
Las salidas incompletas o rechazadas por contenido no provocan cambio de proveedor.

Pausas predeterminadas, configurables en segundos:

| Variable | Valor | Cuándo se aplica |
| --- | --- | --- |
| `LLM_QUOTA_COOLDOWN_SECONDS` | 3600 | Saldo/cuota agotados o problema de clave, permiso o modelo |
| `LLM_RATE_COOLDOWN_SECONDS` | 60 | Límite temporal de solicitudes |
| `LLM_ERROR_COOLDOWN_SECONDS` | 30 | Conexión o servicio no disponible |
| `LLM_TIMEOUT_SECONDS` | 30 | Tiempo máximo de cada intento |

Respeta `Retry-After` si pide una espera mayor. Si Gemini identifica cuota diaria,
usa la pausa de cuota. No promete detectar el saldo restante antes de un error.
No intenta saltarse cuotas: los límites de cada proveedor siguen aplicándose.

Las pausas y el proveedor activo quedan guardados en SQLite y sobreviven al reinicio.
Cuando todos están en pausa, no envía peticiones a ninguno. Cuando una pausa termina,
ese proveedor queda disponible para una petición futura, sin sondeos en segundo plano.
Mientras el proveedor activo funcione, seguirá con él aunque expire la pausa de otro.
Después de recargar saldo, el dueño del bot puede usar `!reiniciar_apis` para
limpiar las pausas y recuperar el orden inicial. `!apis` muestra el estado local
sin claves ni consultas externas. Reinicia PeBOT tras editar las claves o el orden;
una clave nueva no hereda la pausa de una clave anterior.

OpenAI utiliza Responses con `store=False`. DeepSeek y Google utilizan sus
endpoints compatibles con Chat Completions; no hace falta instalar SDKs adicionales.
No hay reintentos automáticos internos del SDK: cada proveedor recibe como máximo
un intento por generación, y los mensajes de distintos canales se coordinan
para no volver a golpear un proveedor que acaba de agotar cuota.

## Configurar Discord

1. Crea una aplicación en https://discord.com/developers/applications y entra a **Bot**.
2. Copia o restablece su token y colócalo en `DISCORD_TOKEN`.
3. En **Privileged Gateway Intents**, activa **Message Content Intent**.
4. En **OAuth2 > URL Generator**, selecciona el scope `bot` y los permisos
   **View Channels**, **Send Messages** y **Read Message History**.
5. Abre la URL generada e invita el bot a tu servidor.

## Cómo usarlo

Puedes mencionarlo con `@PeBOT`, responder a uno de sus mensajes (incluso sin ping),
escribir su nombre o enviarle un DM. No necesita que escribas su nombre en cada turno.
Al responderle, incluye el mensaje original como contexto, aunque ya no esté en los
últimos 20 mensajes. Si el mensaje original no está en la caché, lo recupera de Discord;
no hace consultas de IA adicionales para decidir a quién le hablan.

Después de una interacción directa, sigue hasta dos mensajes de la misma persona
en ese canal durante 90 segundos. La ventana no se renueva con cada seguimiento;
una nueva mención o respuesta directa abre otra. Los mensajes dirigidos a otra
persona no se consideran seguimientos. Ajusta `CONVERSATION_WINDOW_SECONDS` o
ponlo en `0` para desactivar esta continuidad. No se conserva al reiniciar.

Puede sacar temas por iniciativa propia en el último canal de servidor donde vio
actividad: una ocurrencia, una opinión, una idea de juego o algo relacionado con
los recuerdos y la charla reciente. `!rasgos` muestra sus rasgos en ese canal;
`!help` muestra los comandos disponibles. Ignora mensajes de otros bots.

`CHANNEL_IDS` limita los canales que escucha (con IDs separados por comas).
Vacío permite todos los canales accesibles, incluidos DM. Durante el sueño
(01:00–08:00 de Lima) solo responde si le hablan directamente. Usa
`MAX_SPONTANEOUS_PER_DAY=0` para desactivar publicaciones espontáneas;
Las respuestas en conversaciones activas se disparan por una interacción directa
o por el seguimiento breve descrito arriba.

La personalidad se edita en `personalidad()` dentro de `bot.py`. Usa jerga
moderada como «causa», «pata» y «ya pe», sin repetirla en cada respuesta.
Conversa con frases cortas, humor, gustos y opiniones de personaje, evitando saludos
repetidos, frases de atención al cliente y preguntas obligatorias al final de cada turno.
No pretende ser una persona real si alguien le pregunta.

## Ritmo de chat e iniciativa propia

Una respuesta puede salir como una tanda de uno a tres mensajes cortos, generados
en una sola llamada a la IA. Entre mensajes muestra que está escribiendo y espera
aproximadamente de uno a tres segundos, según su longitud. Cada mensaje enviado
se guarda en el historial. `MAX_REPLY_MESSAGES` configura el máximo (1–5).

El estilo pide minúsculas, abreviaturas, omisión natural de tildes y puntuación,
reacciones y frases incompletas cuando encajen. No introduce errores aleatorios
en enlaces o código. El modelo decide cómo expresarse; un ajuste local baja
mayúsculas de comienzos habituales y retira puntos finales de frases corrientes.

Para la iniciativa propia, el reloj comprueba condiciones locales cada minuto,
sin consultar la IA con cada mensaje recibido. Por defecto:

- Espera al menos 30 segundos desde el último mensaje humano y 15 minutos desde
  su última intervención en ese canal o el intento espontáneo anterior.
- En una oportunidad válida tiene un 35 % de probabilidad de consultar al modelo.
- Solo intenta una vez por contexto humano nuevo y necesita actividad humana
  en las últimas seis horas; no conversa consigo mismo indefinidamente.
- Puede elegir quedarse callado. Como máximo inicia seis tandas al día; una
  tanda de varios mensajes cuenta como una sola iniciativa.
- Respeta los canales permitidos y el horario de sueño.

Configura `SPONTANEOUS_INTERVAL_MINUTES`, `SPONTANEOUS_IDLE_SECONDS`,
`SPONTANEOUS_CHANCE` y `MAX_SPONTANEOUS_PER_DAY` en `.env`. Los intervalos son
límites mínimos, no una promesa de publicar a una hora fija. Las iniciativas
usan el mismo sistema de proveedores y consumen la cuota de una generación.
Para que el nombre visible de la cuenta en Discord también sea PeBOT,
configura ese nombre en **Discord Developer Portal > Bot** y revisa su apodo
en el servidor. `BOT_NAME=PeBOT` configura el personaje y la detección del nombre.

## Memoria y datos

Guarda mensajes, recuerdos y rasgos en SQLite, con contexto separado por canal.
El historial del ejemplo no se elimina automáticamente. Los recuerdos antiguos
sin canal no se reutilizan. Para cada respuesta se envían al proveedor de IA
los últimos 20 mensajes de ese canal y hasta 15 recuerdos al proveedor activo.
En caso de cambio, ese contexto también se envía al proveedor de respaldo.
La retención de datos depende de cada proveedor; `store=False` en OpenAI no
elimina la base local ni cambia sus otras políticas de retención. Avisa a quienes usen
el servidor de este comportamiento. La base queda excluida de Git.

Si faltan claves, el programa lo indica antes de conectarse. Los fallos del
modelo devuelven un aviso breve; comprueba saldo, modelo, conexión y claves.
Los modelos se pueden cambiar con `OPENAI_MODEL`, `DEEPSEEK_MODEL` y `GOOGLE_MODEL`.

## Pruebas locales

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```

Las pruebas usan credenciales ficticias y respuestas simuladas, sin
conectarse a Discord ni a los proveedores. Cubren memoria por canal, límites de
recuerdos, historial, sueño, personalidad, respuestas, ausencia de claves,
solicitudes de Responses y Chat Completions, cambios de proveedor, pausas,
persistencia, concurrencia y fallos de los proveedores.
También cubren respuestas sin ping, mensajes referenciados fuera de caché,
referencias eliminadas, seguimientos limitados y contexto de la respuesta.
También verifican tandas con una sola generación, pausas, conservación del historial,
estilo de chat, límites diarios e iniciativas sin repetir contexto.

Documentación: [Discord intents](https://discordpy.readthedocs.io/en/stable/intents.html)
y [SDK oficial de OpenAI para Python](https://developers.openai.com/api/reference/python),
[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
y [compatibilidad de Google Gemini](https://ai.google.dev/gemini-api/docs/openai).
