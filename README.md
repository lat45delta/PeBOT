# PeBOT 🇵🇪

Bot de Discord con un personaje peruano, basado en el ejemplo proporcionado.
Se llama **PeBOT** y usa la API de OpenAI mediante Responses. Necesitas **dos tokens**:
el token del bot de Discord y una API key de OpenAI con acceso al modelo.
El modelo predeterminado es `gpt-4.1-mini`, configurable con `OPENAI_MODEL`.
Obtén la clave en https://platform.openai.com/api-keys y configura la facturación
de la API en tu cuenta. La ejecución usa la API de OpenAI, no una sesión de chatgpt.com.

## Iniciar en Windows (Python 3.11 o posterior)

```powershell
cd C:\Users\Alejandro\Documents\Github\PeBOT\PeBOT
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
notepad .env
.\.venv\Scripts\python.exe bot.py
```

El `.env` ya está preparado con las claves vacías. Completa `DISCORD_TOKEN`
y `OPENAI_API_KEY`, sin compartirlas ni subirlas a Git. Si clonas el repo,
copia `.env.example` a `.env`. No hace falta Client Secret, Public Key ni Client ID
para ejecutar el bot. El Application ID se usa para generar su invitación.

## Configurar Discord

1. Crea una aplicación en https://discord.com/developers/applications y entra a **Bot**.
2. Copia o restablece su token y colócalo en `DISCORD_TOKEN`.
3. En **Privileged Gateway Intents**, activa **Message Content Intent**.
4. En **OAuth2 > URL Generator**, selecciona el scope `bot` y los permisos
   **View Channels**, **Send Messages** y **Read Message History**.
5. Abre la URL generada e invita el bot a tu servidor.

## Cómo usarlo

Menciona al bot, escribe su nombre (`PeBOT`) o envíale un DM. También puede
intervenir de forma aleatoria y publicar hasta seis mensajes espontáneos al día,
en el último canal de servidor donde vio actividad, tras diez minutos de silencio
y con al menos 45 minutos entre intentos. `!rasgos` muestra sus rasgos en ese canal;
`!help` muestra los comandos disponibles. Ignora mensajes de otros bots.

`CHANNEL_IDS` limita los canales que escucha (con IDs separados por comas).
Vacío permite todos los canales accesibles, incluidos DM. Durante el sueño
(01:00–08:00 de Lima) solo responde si le hablan directamente. Usa
`MAX_SPONTANEOUS_PER_DAY=0` para desactivar publicaciones espontáneas;
esto no desactiva sus intervenciones aleatorias en conversaciones activas.

La personalidad se edita en `personalidad()` dentro de `bot.py`. Usa jerga
moderada como «causa», «pata» y «ya pe», sin repetirla en cada respuesta.
No pretende ser una persona real si alguien le pregunta.
Para que el nombre visible de la cuenta en Discord también sea PeBOT,
configura ese nombre en **Discord Developer Portal > Bot** y revisa su apodo
en el servidor. `BOT_NAME=PeBOT` configura el personaje y la detección del nombre.

## Memoria y datos

Guarda mensajes, recuerdos y rasgos en SQLite, con contexto separado por canal.
El historial del ejemplo no se elimina automáticamente. Los recuerdos antiguos
sin canal no se reutilizan. Para cada respuesta se envían al proveedor de IA
los últimos 20 mensajes de ese canal y hasta 15 recuerdos a OpenAI. Las llamadas
usan `store=False`; esto no elimina la base local ni cambia las políticas de
retención del proveedor. Avisa a quienes usen
el servidor de este comportamiento. La base queda excluida de Git.

Si faltan claves, el programa lo indica antes de conectarse. Los fallos del
modelo devuelven un aviso breve; comprueba saldo, modelo, conexión y claves.
El modelo se puede cambiar con `OPENAI_MODEL`.

## Pruebas locales

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```

Las catorce pruebas usan credenciales ficticias y respuestas simuladas, sin
conectarse a Discord ni a OpenAI. Cubren memoria por canal, límites de
recuerdos, historial, sueño, personalidad, respuestas, ausencia de claves,
solicitudes de Responses y fallos del proveedor.

Documentación: [Discord intents](https://discordpy.readthedocs.io/en/stable/intents.html)
y [SDK oficial de OpenAI para Python](https://developers.openai.com/api/reference/python).
