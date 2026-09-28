"""User-facing translations.

Source strings are written in English inside ``_()``; ``ES`` maps each of them to Spanish. The language comes
from ``defaults.language`` in the config (``en`` by default) and can be overridden with ``PDMS_LANG``.
``tests/test_i18n.py`` checks that every ``_()`` string in the code has a Spanish translation.
"""

from __future__ import annotations

import os

LANGUAGES = {"en": "English", "es": "Español"}
DEFAULT_LANGUAGE = "en"

_language: str | None = None


def _configured_language() -> str | None:
    try:
        from .config import Config

        return Config.load().defaults.language
    except Exception:  # noqa: BLE001 - a broken config must not break the CLI texts
        return None


def current_language() -> str:
    global _language
    if _language is None:
        lang = os.environ.get("PDMS_LANG") or _configured_language() or DEFAULT_LANGUAGE
        _language = lang if lang in LANGUAGES else DEFAULT_LANGUAGE
    return _language


def set_language(lang: str) -> None:
    global _language
    _language = lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def _(text: str, **kwargs: object) -> str:
    """Translate ``text`` to the current language and fill its ``{placeholders}``."""
    translated = ES.get(text, text) if current_language() == "es" else text
    return translated.format(**kwargs) if kwargs else translated


ES: dict[str, str] = {
    # ------------------------------------------------------------------ general
    "Run PDMS services locally. Without arguments it opens the interactive menu.":
        "Levanta servicios de PDMS en local. Sin argumentos abre el menú interactivo.",
    "Manage databases.": "Gestionar bases de datos.",
    "Manage development users (DEV_*).": "Gestionar usuarios de desarrollo (DEV_*).",
    "General settings.": "Configuración general.",
    "Manage stacks (groups of services started together).":
        "Gestionar stacks (grupos de servicios que se levantan juntos).",
    "An interactive terminal is required (or pass the options as flags).":
        "Se necesita una terminal interactiva (o pasa las opciones por flags).",
    "Cancelled.": "Cancelado.",
    "user": "usuario",
    "database": "base de datos",
    "stack": "stack",
    "yes": "sí",
    "no": "no",
    "none": "ninguno",
    # ------------------------------------------------------------------ validation
    "Must be a number": "Debe ser un número",
    "Required field": "Campo obligatorio",
    "That name already exists": "Ya existe ese nombre",
    "Use only letters, numbers, '-' or '_'": "Usa solo letras, números, '-' o '_'",
    "Must be a number between 1 and 65535": "Debe ser un número entre 1 y 65535",
    "Port {port} is in use": "El puerto {port} está ocupado",
    "That folder does not exist": "No existe esa carpeta",
    # ------------------------------------------------------------------ prompts
    "Alias ({kind}):": "Alias ({kind}):",
    "Choose {kind}:": "Elige {kind}:",
    "Service port:": "Puerto del servicio:",
    "How should it run?": "¿Cómo lo levanto?",
    "Background (you can run several; logs with pdms logs)":
        "Segundo plano (puedes levantar varios; logs con pdms logs)",
    "Foreground (in this terminal)": "Primer plano (en esta terminal)",
    "Host:": "Host:",
    "Port:": "Puerto:",
    "Database name:": "Base de datos:",
    "User:": "Usuario:",
    "Password{hint}:": "Contraseña{hint}:",
    " (empty = keep the current one)": " (vacío = mantener la actual)",
    "Is it a shared/remote DB? (asks for confirmation before starting)":
        "¿Es una DB compartida/remota? (pedirá confirmación antes de levantar)",
    "DEV_ROLES (comma separated):": "DEV_ROLES (separados por coma):",
    "Language:": "Idioma:",
    "uvicorn host:": "Host de uvicorn:",
    "Default port:": "Puerto por defecto:",
    "Use --reload?": "¿Usar --reload?",
    "Run poetry lock && poetry install before starting?": "¿Hacer poetry lock && poetry install antes de levantar?",
    "Connection test timeout (seconds):": "Timeout del test de conexión (segundos):",
    "PDMS backend folder (to list services):": "Carpeta backend de PDMS (para listar servicios):",
    "Add/edit extra environment variables? (current: {current})":
        "¿Añadir/editar variables de entorno extra? (actuales: {current})",
    "Variable name:": "Nombre de la variable:",
    "Value of {key} (empty = delete):": "Valor de {key} (vacío = borrar):",
    # ------------------------------------------------------------------ tables
    "Name": "Nombre",
    "Port": "Puerto",
    "User": "Usuario",
    "Password": "Contraseña",
    "Protected": "Protegida",
    "Full name": "Nombre completo",
    "Service": "Servicio",
    "Running": "Corriendo",
    "Instance": "Instancia",
    "Status": "Estado",
    "Uptime": "Tiempo",
    "Services": "Servicios",
    "not set": "sin definir",
    "ask": "preguntar",
    "No databases configured.": "No hay bases de datos configuradas.",
    "Use [bold]pdms db add[/].": "Usa [bold]pdms db add[/].",
    "No users configured.": "No hay usuarios configurados.",
    "Use [bold]pdms user add[/].": "Usa [bold]pdms user add[/].",
    # ------------------------------------------------------------------ CRUD
    "'{name}' does not exist ({kind}). Available: {available}":
        "'{name}' no existe ({kind}). Disponibles: {available}",
    "Nothing configured ({kind}).": "No hay nada configurado ({kind}).",
    "Database '{name}' saved.": "Base de datos '{name}' guardada.",
    "Test the connection now?": "¿Probar la conexión ahora?",
    "User '{name}' saved.": "Usuario '{name}' guardado.",
    "Connecting to {name} ({host}:{port}, timeout {timeout}s)...":
        "Conectando a {name} ({host}:{port}, timeout {timeout}s)...",
    "Database '{name}' updated.": "Base de datos '{name}' actualizada.",
    "User '{name}' updated.": "Usuario '{name}' actualizado.",
    "Delete '{name}'?": "¿Eliminar '{name}'?",
    "'{name}' deleted.": "'{name}' eliminado.",
    # ------------------------------------------------------------------ services
    "⚠ The configured backend folder does not exist: {root}": "⚠ La carpeta backend configurada no existe: {root}",
    " Set the backend folder with [bold]pdms config[/].": " Configura la carpeta backend con [bold]pdms config[/].",
    "No services (pyproject.toml + main.py) found in {root}.{hint}":
        "No encontré servicios (pyproject.toml + main.py) en {root}.{hint}",
    "running on :{ports}": "corriendo en :{ports}",
    "{count} services match, narrow it down": "{count} servicios coinciden, afina más",
    "No service matches": "Ningún servicio coincide",
    "Service to run": "Servicio a levantar",
    "{message} (type to filter, Tab to see the list):": "{message} (escribe para filtrar, Tab para ver la lista):",
    "{path} is not (and is not inside) a service.": "{path} no es (ni está dentro de) un servicio.",
    "No service matches '{name}' in {root}.": "Ningún servicio coincide con '{name}' en {root}.",
    "Filter by text.": "Filtrar por texto.",
    "List the services available in the backend folder.": "Lista los servicios disponibles en la carpeta backend.",
    # ------------------------------------------------------------------ run
    "[yellow]No users configured, let's create one.[/]": "[yellow]No hay usuarios configurados, vamos a crear uno.[/]",
    "[yellow]No databases configured, let's create one.[/]":
        "[yellow]No hay bases de datos configuradas, vamos a crear una.[/]",
    "⚠ Database '{name}' has no password configured.": "⚠ La base de datos '{name}' no tiene contraseña configurada.",
    "Port {port} is in use (the next free one is {free}). Use --port.":
        "El puerto {port} está ocupado (el siguiente libre es {free}). Usa --port.",
    "Port {port} is in use. Use {free} instead?": "El puerto {port} está ocupado. ¿Usar el {free}?",
    "'{name}' is a protected DB. Continue?": "'{name}' es una DB protegida. ¿Continuar?",
    "Server": "Servidor",
    "Mode": "Modo",
    "Install": "Instalar",
    "background": "segundo plano",
    "foreground": "primer plano",
    "Starting {key}...": "Arrancando {key}...",
    "{key} exited while starting. Full log: {log}": "{key} terminó al arrancar. Log completo: {log}",
    "{key} is responding at http://localhost:{port} (pid {pid})":
        "{key} responde en http://localhost:{port} (pid {pid})",
    "⚠ {key} started with errors: {detail}": "⚠ {key} arrancó con errores: {detail}",
    "  Still alive: once you save the fix, --reload will reload it.":
        "  Sigue vivo: al guardar el arreglo, --reload lo recargará solo.",
    "⚠ {key} is not responding after {timeout}s; check the logs.":
        "⚠ {key} no responde tras {timeout}s; mira los logs.",
    "  Logs: [bold]pdms logs {key}[/]   Stop: [bold]pdms stop {key}[/]":
        "  Logs: [bold]pdms logs {key}[/]   Parar: [bold]pdms stop {key}[/]",
    "Service (name or path relative to the backend folder).":
        "Servicio (nombre o ruta relativa a la carpeta backend).",
    "Development user alias.": "Alias del usuario de desarrollo.",
    "Database alias.": "Alias de la base de datos.",
    "Background or foreground.": "Segundo o primer plano.",
    "Do not ask for confirmation on protected DBs.": "No pedir confirmación para DBs protegidas.",
    "Service folder (defaults to the current one).": "Carpeta del servicio (por defecto la actual).",
    "Install dependencies and run the service with uvicorn.": "Instala dependencias y levanta el servicio con uvicorn.",
    "'poetry' was not found in PATH.": "No se encontró 'poetry' en el PATH.",
    # ------------------------------------------------------------------ debug / env
    "Debug": "Depurar",
    "http://{host}:{port} (no --reload, so breakpoints work)":
        "http://{host}:{port} (sin --reload, para que los breakpoints funcionen)",
    "[yellow]The service has no virtualenv yet; installing dependencies.[/]":
        "[yellow]El servicio aún no tiene virtualenv; instalo dependencias.[/]",
    "Could not find the virtualenv python (poetry env info -e).":
        "No pude localizar el python del virtualenv (poetry env info -e).",
    "Configuration [bold]{name}[/] saved to {launch}": "Configuración [bold]{name}[/] guardada en {launch}",
    "⚠ launch.json had comments and they were lost; original copy at {backup}":
        "⚠ launch.json tenía comentarios y se han perdido; copia del original en {backup}",
    "  Variables (including the password) in {env_file} [dim](outside the repo, permissions 600)[/]":
        "  Variables (con la contraseña) en {env_file} [dim](fuera del repo, permisos 600)[/]",
    "  In VS Code: Run and Debug (Ctrl+Shift+D) → pick the configuration → F5.":
        "  En VS Code: Run and Debug (Ctrl+Shift+D) → elige la configuración → F5.",
    "  [dim]Note: common/ libraries are installed as a copy (develop = false); to stop inside them set the\n"
    "  breakpoint in the copy under .venv/lib/.../site-packages, or step in with F11 from the service.[/]":
        "  [dim]Ojo: las librerías de common/ se instalan como copia (develop = false); para parar en ellas pon el\n"
        "  breakpoint en la copia de .venv/lib/.../site-packages, o entra con F11 desde el servicio.[/]",
    "Create/update the VS Code configuration (launch.json) to debug the service with breakpoints.":
        "Crea/actualiza la configuración de VS Code (launch.json) para depurar el servicio con breakpoints.",
    ".env format (KEY=\"value\") instead of export.": "Formato .env (KEY=\"valor\") en lugar de export.",
    "Print the variables of a profile. Usage: eval \"$(pdms env -u supervisor -d local)\".":
        "Imprime las variables de un perfil. Uso: eval \"$(pdms env -u supervisor -d local)\".",
    # ------------------------------------------------------------------ instances
    "starting": "arrancando",
    "stopped": "parado",
    "running": "corriendo",
    "Choose an instance:": "Elige instancia:",
    "No background services. Start one with [bold]pdms run -b[/].":
        "No hay servicios en segundo plano. Levanta uno con [bold]pdms run -b[/].",
    "No instance matches '{key}'. See [bold]pdms ps[/].": "Ninguna instancia coincide con '{key}'. Mira [bold]pdms ps[/].",
    "Forget stopped instances.": "Olvida las instancias paradas.",
    "List background services.": "Lista los servicios en segundo plano.",
    "No background services.": "No hay servicios en segundo plano.",
    "[dim]Stopped ones keep their log (pdms logs <instance>). Remove them with pdms ps --clean.[/]":
        "[dim]Las paradas conservan su log (pdms logs <instancia>). Bórralas con pdms ps --clean.[/]",
    "Instance (or part of the service name).": "Instancia (o parte del nombre del servicio).",
    "Follow the output live.": "Seguir la salida en vivo.",
    "Previous lines to show.": "Líneas previas a mostrar.",
    "Show the console of a background service (Ctrl+C to exit).":
        "Muestra la consola de un servicio en segundo plano (Ctrl+C para salir).",
    "Which instance do you want to see the logs of?": "¿De qué instancia quieres ver los logs?",
    "{key} · {log} · Ctrl+C to exit": "{key} · {log} · Ctrl+C para salir",
    "Stop all.": "Parar todas.",
    "Stop background services.": "Para servicios en segundo plano.",
    "Which instances do you want to stop? (space to select)": "¿Qué instancias quieres parar? (espacio para marcar)",
    "Nothing to stop.": "No hay nada que parar.",
    "Stopping {key}...": "Parando {key}...",
    "{key} stopped.": "{key} parado.",
    "Switch to this user.": "Cambiar a este usuario.",
    "Switch to this database.": "Cambiar a esta base de datos.",
    "Ask which user and DB to use.": "Preguntar qué usuario y DB usar.",
    "Restart a background service (same port; same user and DB by default).":
        "Reinicia un servicio en segundo plano (mismo puerto; por defecto mismo usuario y DB).",
    "Which instance do you want to restart?": "¿Qué instancia quieres reiniciar?",
    "Database:": "Base de datos:",
    "app failed to load": "error al cargar la app",
    # ------------------------------------------------------------------ stacks
    "No stacks.": "No hay stacks.",
    "Create one with [bold]pdms stack add[/].": "Crea uno con [bold]pdms stack add[/].",
    "Stack services (uncheck to remove):": "Servicios del stack (desmarca para quitar):",
    "Add another service? (it has {count})": "¿Añadir otro servicio? (tiene {count})",
    "Service to add": "Servicio a añadir",
    "(ask when starting)": "(preguntar al levantar)",
    "Stack user:": "Usuario del stack:",
    "Stack database:": "Base de datos del stack:",
    "List stacks.": "Lista los stacks.",
    "Create a stack (wizard).": "Crea un stack (asistente).",
    "Stack '{name}' saved. Start it with [bold]pdms up {name}[/].":
        "Stack '{name}' guardado. Levántalo con [bold]pdms up {name}[/].",
    "Edit a stack.": "Edita un stack.",
    "Stack '{name}' updated.": "Stack '{name}' actualizado.",
    "Delete a stack.": "Elimina un stack.",
    "Delete stack '{name}'?": "¿Eliminar el stack '{name}'?",
    "Set the backend folder with [bold]pdms config[/] to use stacks.":
        "Configura la carpeta backend con [bold]pdms config[/] para usar stacks.",
    "'{svc}' is no longer a service in {root}. Edit the stack with [bold]pdms stack edit[/].":
        "'{svc}' ya no es un servicio en {root}. Edita el stack con [bold]pdms stack edit[/].",
    "Stack to start.": "Stack a levantar.",
    "User (defaults to the stack's).": "Usuario (por defecto el del stack).",
    "Database (defaults to the stack's).": "Base de datos (por defecto la del stack).",
    "Start all services of a stack in the background, each on a free port.":
        "Levanta en segundo plano todos los servicios de un stack, cada uno en un puerto libre.",
    "[dim]· {name} already running on :{ports}, skipping.[/]": "[dim]· {name} ya corre en :{ports}, lo salto.[/]",
    "The whole stack '{name}' is running.": "Todo el stack '{name}' está corriendo.",
    "poetry lock && poetry install (each)": "poetry lock && poetry install (cada uno)",
    "{failed} of {total} services of stack '{name}' did not start.":
        "{failed} de {total} servicios del stack '{name}' no arrancaron.",
    "Stack to stop.": "Stack a parar.",
    "Stop all services of a stack.": "Para todos los servicios de un stack.",
    "Nothing from stack '{name}' is running.": "Nada del stack '{name}' está corriendo.",
    # ------------------------------------------------------------------ db / user commands
    "List databases.": "Lista las bases de datos.",
    "Add a database (wizard).": "Añade una base de datos (asistente).",
    "Edit a database.": "Edita una base de datos.",
    "Delete a database.": "Elimina una base de datos.",
    "Empty = test all.": "Vacío = probar todas.",
    "Seconds to wait (defaults to the one in pdms config).": "Segundos de espera (por defecto el de pdms config).",
    "Test the connection to one or all databases.": "Prueba la conexión a una o todas las bases de datos.",
    "List users.": "Lista los usuarios.",
    "Add a user (wizard).": "Añade un usuario (asistente).",
    "Edit a user.": "Edita un usuario.",
    "Delete a user.": "Elimina un usuario.",
    # ------------------------------------------------------------------ config commands
    "Edit the defaults (language, port, log, reload, extra env...).":
        "Edita los valores por defecto (idioma, puerto, log, reload, env extra...).",
    "Defaults saved.": "Valores por defecto guardados.",
    "Show the path of the configuration file.": "Muestra la ruta del fichero de configuración.",
    "Open the configuration file in $EDITOR.": "Abre el fichero de configuración en $EDITOR.",
    "Language code: {codes}. Empty = ask.": "Código de idioma: {codes}. Vacío = preguntar.",
    "Change the CLI language.": "Cambia el idioma del CLI.",
    "Unknown language '{lang}'. Available: {codes}": "Idioma desconocido '{lang}'. Disponibles: {codes}",
    "Language set to {name}.": "Idioma cambiado a {name}.",
    # ------------------------------------------------------------------ menus
    "← Back": "← Volver",
    "Databases:": "Bases de datos:",
    "List": "Listar",
    "Add": "Añadir",
    "Edit": "Editar",
    "Test connection": "Probar conexión",
    "Delete": "Eliminar",
    "Users:": "Usuarios:",
    "Background services:": "Servicios en segundo plano:",
    "View logs (console)": "Ver logs (consola)",
    "Stop": "Parar",
    "Restart": "Reiniciar",
    "Restart with another user/DB": "Reiniciar cambiando usuario/DB",
    "Stop all": "Parar todos",
    "Stacks:": "Stacks:",
    "Start stack": "Levantar stack",
    "Stop stack": "Parar stack",
    "Create": "Crear",
    "[dim]Configuration created at {path}[/]": "[dim]Configuración creada en {path}[/]",
    "What do you want to do?": "¿Qué quieres hacer?",
    "▶  Run a service": "▶  Levantar servicio",
    "📋 Background services ({count} running)": "📋 Servicios en segundo plano ({count} corriendo)",
    "🧩 Stacks (groups of services)": "🧩 Stacks (grupos de servicios)",
    "🗄  Databases": "🗄  Bases de datos",
    "👤 Users": "👤 Usuarios",
    "⚙  Settings": "⚙  Configuración",
    "✕  Exit": "✕  Salir",
}
