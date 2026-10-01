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


def configured(lang: str) -> str:
    """The language in use when the configuration says ``lang``: ``PDMS_LANG`` still wins, as in the CLI."""
    env = os.environ.get("PDMS_LANG", "")
    if env in LANGUAGES:
        return env
    return lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def set_language(lang: str) -> None:
    global _language
    _language = lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def _(text: str, /, **kwargs: object) -> str:
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
    "Must be a number between {low} and {high}": "Debe ser un número entre {low} y {high}",
    "Must be one of: {choices}": "Debe ser uno de: {choices}",
    "The window needs pywebview; installing it (only this once)...":
        "La ventana necesita pywebview; instalándolo (solo esta vez)...",
    "Could not install pywebview ({error}). Install it with:\n  {command}\nor use pdms ui to open it in the browser.":
        "No se pudo instalar pywebview ({error}). Instálalo con:\n  {command}\no usa pdms ui para abrirla en el navegador.",
    "pywebview installed.": "pywebview instalado.",
    "Qt needs the system library libxcb-cursor to open windows on X11; install it with sudo apt install libxcb-cursor0 (Fedora, Arch: xcb-util-cursor)":
        "Qt necesita la librería del sistema libxcb-cursor para abrir ventanas en X11; instálala con "
        "sudo apt install libxcb-cursor0 (Fedora, Arch: xcb-util-cursor)",
    "Unknown roles: {roles}. Available: {available}": "Roles desconocidos: {roles}. Disponibles: {available}",
    "{role} (not a role of this repo)": "{role} (no es un rol de este repo)",
    "DEV_ROLES (space to select):": "DEV_ROLES (espacio para marcar):",
    "'{name}' is not a valid variable name": "'{name}' no es un nombre de variable válido",
    "These stacks will ask for it again when they start: {names}":
        "Estos stacks lo volverán a preguntar al levantarse: {names}",
    "Port {port} is in use": "El puerto {port} está ocupado",
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
    "Skip the install when nothing changed since the last one (smart install)?":
        "¿Saltar la instalación si nada cambió desde la última (instalación inteligente)?",
    "only if something changed (-i to force)": "solo si algo cambió (-i para forzar)",
    "Force (-i) or skip (-n) the install; by default only if something changed.":
        "Forzar (-i) o saltar (-n) la instalación; por defecto solo si algo cambió.",
    "{name}: dependencies up to date (nothing changed since the last install), skipping.":
        "{name}: dependencias al día (nada cambió desde la última instalación), me la salto.",
    "Connection test timeout (seconds):": "Timeout del test de conexión (segundos):",
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
    # ------------------------------------------------------------------ test / migrate
    "Run the service's tests (poetry run pytest). Extra arguments go to pytest, e.g. pdms test -- -k name -x.":
        "Ejecuta los tests del servicio (poetry run pytest). Los argumentos extra van a pytest, p. ej. pdms test -- -k nombre -x.",
    "Inject this user's DEV_* variables.": "Inyectar las variables DEV_* de este usuario.",
    "Inject this database's DB_PG_CONNECTION_STR.": "Inyectar el DB_PG_CONNECTION_STR de esta base de datos.",
    "Profile: {user} @ {db}": "Perfil: {user} @ {db}",
    # ------------------------------------------------------------------ instances
    "starting": "arrancando",
    "stopped": "parado",
    "running": "corriendo",
    "Choose an instance:": "Elige instancia:",
    "No background services. Start one with [bold]pdms run -b[/].":
        "No hay servicios en segundo plano. Levanta uno con [bold]pdms run -b[/].",
    "No instance matches '{key}'. See [bold]pdms ps[/].": "Ninguna instancia coincide con '{key}'. Mira [bold]pdms ps[/].",
    "Forget stopped instances.": "Olvida las instancias paradas.",
    "the service itself": "el propio servicio",
    "installed code changed since it started ({names}); --reload does not pick it up → pdms restart {key}":
        "el código instalado cambió desde que arrancó ({names}); --reload no lo recoge → pdms restart {key}",
    "List background services.": "Lista los servicios en segundo plano.",
    "No background services.": "No hay servicios en segundo plano.",
    "[dim]Stopped ones keep their log (pdms logs <instance>). Remove them with pdms ps --clean.[/]":
        "[dim]Las paradas conservan su log (pdms logs <instancia>). Bórralas con pdms ps --clean.[/]",
    "Instance (or part of the service name).": "Instancia (o parte del nombre del servicio).",
    "Instance (or part of the service name), or proxy.": "Instancia (o parte del nombre del servicio), o proxy.",
    "Follow the output live.": "Seguir la salida en vivo.",
    "Show the console of background services (Ctrl+C to exit).":
        "Muestra la consola de servicios en segundo plano (Ctrl+C para salir).",
    "Instances (or parts of the service name).": "Instancias (o partes del nombre del servicio).",
    "Instances (or parts of the service name), proxy, or sns (what was published to SNS locally).":
        "Instancias (o partes del nombre del servicio), proxy, o sns (lo publicado a SNS en local).",
    "All running instances, including ones started later.":
        "Todas las instancias corriendo, incluidas las que se levanten después.",
    "All instances of a stack.": "Todas las instancias de un stack.",
    "Previous lines to show per instance (100, or 20 with several).":
        "Líneas previas a mostrar por instancia (100, o 20 si son varias).",
    "All running instances": "Todas las instancias corriendo",
    "(waiting for instances)": "(esperando instancias)",
    "{names} · Ctrl+C to exit": "{names} · Ctrl+C para salir",
    "View all logs together": "Ver todos los logs juntos",
    "Show the log of the previous run (kept when restarting).":
        "Muestra el log de la ejecución anterior (se conserva al reiniciar).",
    "{key} has no previous log.": "{key} no tiene log anterior.",
    "{key} · previous run": "{key} · ejecución anterior",
    "View the previous run's log": "Ver el log de la ejecución anterior",
    "Open a background service in the browser (Swagger /docs by default).":
        "Abre un servicio en segundo plano en el navegador (Swagger /docs por defecto).",
    "Path to open, e.g. /redoc or /.": "Ruta a abrir, p. ej. /redoc o /.",
    "Which instance do you want to open?": "¿Qué instancia quieres abrir?",
    "Opening {url}": "Abriendo {url}",
    "[yellow]Could not open a browser; open the URL manually.[/]":
        "[yellow]No pude abrir un navegador; abre la URL a mano.[/]",
    "Open in the browser (/docs)": "Abrir en el navegador (/docs)",
    "Could not read {url}/openapi.json.": "No pude leer {url}/openapi.json.",
    "No endpoint contains '{text}'.": "Ningún endpoint contiene '{text}'.",
    "+{count} more: pdms urls {key}": "+{count} más: pdms urls {key}",
    "Docs: {url}": "Docs: {url}",
    "Show the endpoints (method and full URL) of background services.":
        "Muestra los endpoints (método y URL completa) de los servicios en segundo plano.",
    "Only endpoints whose path contains this text.": "Solo los endpoints cuya ruta contenga este texto.",
    "Show endpoints (URLs)": "Ver endpoints (URLs)",
    "Which instance do you want to see the logs of?": "¿De qué instancia quieres ver los logs?",
    "Stop all, the proxy included.": "Parar todas, el proxy incluido.",
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
    "Stack services ({count} in the repo):": "Servicios del stack ({count} en el repo):",
    "(type to filter, ↑↓ to move, space to check or uncheck, Enter to save)":
        "(escribe para filtrar, ↑↓ para moverte, espacio para marcar o desmarcar, Enter para guardar)",
    "(running on :{ports})": "(en ejecución en :{ports})",
    "(running)": "(en ejecución)",
    "A stack needs at least one service.": "Un stack necesita al menos un servicio.",
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
    "Create user profiles from the pdms_user table of a database.":
        "Crea perfiles de usuario a partir de la tabla pdms_user de una base de datos.",
    "Database to read the users from.": "Base de datos de la que leer los usuarios.",
    "Filter by email or name.": "Filtrar por email o nombre.",
    "Filter by role (e.g. TPR.Supervisor).": "Filtrar por rol (p. ej. TPR.Supervisor).",
    "Include inactive users.": "Incluir usuarios inactivos.",
    "Maximum number of users to read.": "Número máximo de usuarios a leer.",
    "Import every match without asking.": "Importar todas las coincidencias sin preguntar.",
    "Search by email or name (empty = all):": "Buscar por email o nombre (vacío = todos):",
    "Reading users from {name}...": "Leyendo usuarios de {name}...",
    "Could not read the users from {name}: {error}": "No pude leer los usuarios de {name}: {error}",
    "No users match.": "Ningún usuario coincide.",
    "[dim]Showing the first {limit}; narrow it down with --search or --role.[/]":
        "[dim]Mostrando los primeros {limit}; afina con --search o --role.[/]",
    "already imported": "ya importado",
    "inactive": "inactivo",
    "Which users do you want to import? (space to select)": "¿Qué usuarios quieres importar? (espacio para marcar)",
    "new": "nuevo",
    "updated": "actualizado",
    "{added} added, {updated} updated, {unchanged} unchanged.":
        "{added} añadidos, {updated} actualizados, {unchanged} sin cambios.",
    "⚠ Unknown roles kept as they are (the services will ignore them): {roles}":
        "⚠ Roles desconocidos, se dejan tal cual (los servicios los ignorarán): {roles}",
    "Import from a database": "Importar desde una base de datos",
    "[dim]Roles mapped with MAP_INTERNAL_ROLES from {source}.[/]": "[dim]Roles traducidos con MAP_INTERNAL_ROLES de {source}.[/]",
    "[yellow]⚠ Could not read the roles of the current repo; using the built-in copy.[/]":
        "[yellow]⚠ No pude leer los roles del repo actual; uso la copia incluida.[/]",
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
    # ------------------------------------------------------------------ export / import
    "defaults": "valores por defecto",
    "users": "usuarios",
    "databases": "bases de datos",
    "stacks": "stacks",
    "Unknown sections: {unknown}. Available: {codes}": "Secciones desconocidas: {unknown}. Disponibles: {codes}",
    "Comma-separated sections: defaults, users, dbs, stacks. Default: all.":
        "Secciones separadas por coma: defaults, users, dbs, stacks. Por defecto: todas.",
    "Export the configuration (users, databases, stacks, defaults) to a TOML file.":
        "Exporta la configuración (usuarios, bases de datos, stacks, valores por defecto) a un fichero TOML.",
    "Output file ('-' = stdout). Default: pdms-config-<date>.toml.":
        "Fichero de salida ('-' = stdout). Por defecto: pdms-config-<fecha>.toml.",
    "Include database passwords (asked if omitted; no by default).":
        "Incluir las contraseñas de las DBs (se pregunta si se omite; no por defecto).",
    "Overwrite the file if it exists.": "Sobrescribir el fichero si existe.",
    "What do you want to export?": "¿Qué quieres exportar?",
    "Nothing selected.": "No has elegido nada.",
    "Include database passwords? (only if the file stays private)":
        "¿Incluir las contraseñas de las DBs? (solo si el fichero va a quedar privado)",
    "{file} already exists. Overwrite it?": "{file} ya existe. ¿Sobrescribirlo?",
    "{file} already exists (use --force).": "{file} ya existe (usa --force).",
    "Exported {parts} to {file}.": "Exportado: {parts} en {file}.",
    "  [yellow]The file includes database passwords: do not share it or commit it.[/]":
        "  [yellow]El fichero incluye las contraseñas de las DBs: no lo compartas ni lo subas a un repo.[/]",
    "  [dim]Database passwords were left out.[/]": "  [dim]Las contraseñas de las DBs no se han incluido.[/]",
    "Import a configuration exported with pdms config export.":
        "Importa una configuración exportada con pdms config export.",
    "File to import.": "Fichero a importar.",
    "Replace the selected sections entirely instead of merging.":
        "Reemplazar por completo las secciones elegidas en lugar de combinarlas.",
    "Overwrite existing entries without asking.": "Sobrescribir las entradas existentes sin preguntar.",
    "Apply without asking for confirmation.": "Aplicar sin pedir confirmación.",
    "File to import:": "Fichero a importar:",
    "File not found": "No existe el fichero",
    "File not found: {file}": "No existe el fichero: {file}",
    "What do you want to import?": "¿Qué quieres importar?",
    "Nothing to import.": "No hay nada que importar.",
    "File exported on {date} (pdms {version}).": "Fichero exportado el {date} (pdms {version}).",
    "Section": "Sección",
    "New": "Nuevas",
    "Changed": "Distintas",
    "Unchanged": "Iguales",
    "Only local": "Solo en local",
    "Removed": "Se borran",
    "[dim]The file has no passwords: databases you already have keep their password.[/]":
        "[dim]El fichero no trae contraseñas: las DBs que ya tienes conservan la suya.[/]",
    "⚠ --replace will delete {count} local entries not in the file.":
        "⚠ --replace borrará {count} entradas locales que no están en el fichero.",
    "These entries differ from yours. Which ones do you want to overwrite? (unchecked = keep yours)":
        "Estas entradas son distintas a las tuyas. ¿Cuáles quieres sobrescribir? (sin marcar = conservar la tuya)",
    "[dim]Existing entries are kept (use --overwrite to replace them).[/]":
        "[dim]Se conservan las entradas existentes (usa --overwrite para reemplazarlas).[/]",
    "Nothing changes.": "No cambia nada.",
    "Use --yes to import without an interactive terminal.": "Usa --yes para importar sin terminal interactiva.",
    "Apply the import?": "¿Aplicar la importación?",
    "Configuration imported.": "Configuración importada.",
    "  [dim]Previous configuration saved to {backup}[/]": "  [dim]Configuración anterior guardada en {backup}[/]",
    "⚠ Databases without password: {names}. Set it with pdms db edit <name>.":
        "⚠ Bases de datos sin contraseña: {names}. Configúrala con pdms db edit <nombre>.",
    "The file is not valid TOML: {error}": "El fichero no es un TOML válido: {error}",
    "This is not a pdms export (the \\[pdms] header is missing).":
        "No es una exportación de pdms (falta la cabecera \\[pdms]).",
    "The file was exported by a newer pdms version; update pdms first.":
        "El fichero se exportó con una versión más nueva de pdms; actualiza pdms primero.",
    "The file has an invalid structure: {error}": "El fichero tiene una estructura no válida: {error}",
    "Settings:": "Configuración:",
    "Defaults": "Valores por defecto",
    "Language": "Idioma",
    "Export configuration": "Exportar configuración",
    "Import configuration": "Importar configuración",
    "Show configuration file path": "Ver la ruta del fichero de configuración",
    # ------------------------------------------------------------------ repos
    "repo": "repo",
    "Repos": "Repos",
    "Repos:": "Repos:",
    "Path": "Ruta",
    "missing": "no existe",
    " Register a repo with [bold]pdms repo add <path>[/].": " Registra un repo con [bold]pdms repo add <ruta>[/].",
    "No current repo. Register one with [bold]pdms repo add <path>[/].":
        "No hay repo actual. Registra uno con [bold]pdms repo add <ruta>[/].",
    "Manage PDMS repos (checkouts) and choose the current one.":
        "Gestionar los repos de PDMS (copias del repo) y elegir el actual.",
    "Current repo: {alias} ({path})": "Repo actual: {alias} ({path})",
    "{count} instances are running from '{old}': {keys}. What should I do with them?":
        "Hay {count} instancias corriendo desde '{old}': {keys}. ¿Qué hago con ellas?",
    "Keep them running (they coexist, each on its port)": "Dejarlas corriendo (conviven, cada una en su puerto)",
    "Stop them": "Pararlas",
    "Restart them from '{new}' (same user, DB and port)":
        "Reiniciarlas desde '{new}' (mismo usuario, DB y puerto)",
    "⚠ {key}: the service does not exist in '{new}'; left running.":
        "⚠ {key}: el servicio no existe en '{new}'; lo dejo corriendo.",
    "[dim]Using {path} as the current repo '{alias}'.[/]": "[dim]Uso {path} como repo actual '{alias}'.[/]",
    "You are in {here}, but the current repo is '{current}' ({path}). What should I do?":
        "Estás en {here}, pero el repo actual es '{current}' ({path}). ¿Qué hago?",
    "Switch to {name} (it becomes the default)": "Cambiar a {name} (pasa a ser el de por defecto)",
    "Use it only for this command": "Usarlo solo para este comando",
    "Don't ask again in this repo": "No volver a preguntar en este repo",
    "List the registered repos.": "Lista los repos registrados.",
    "'{name}' saved.": "'{name}' guardado.",
    "Change the name, the migrations repo or the proxy's remote API of a repo.":
        "Cambia el nombre, el repo de migraciones o la API remota del proxy de un repo.",
    "Flyway migrations checkout (pdms-db-migrations); '' to forget it.":
        "Checkout de migraciones Flyway (pdms-db-migrations); '' para olvidarlo.",
    "Migrations repo (empty = look next to the repo):": "Repo de migraciones (vacío = buscar junto al repo):",
    "Must be a URL starting with http:// or https://": "Debe ser una URL que empiece con http:// o https://",
    "New name for the repo.": "Nuevo nombre del repo.",
    "Remote API for the proxy (empty = from frontend/.env):": "API remota del proxy (vacío = la de frontend/.env):",
    "Remote API for the proxy; '' to forget it.": "API remota del proxy; '' para olvidarla.",
    "[dim]The proxy still routes to '{old}': restart it (pdms proxy) to use '{new}'.[/]":
        "[dim]El proxy sigue enrutando a '{old}': reinícialo (pdms proxy) para usar '{new}'.[/]",
    "No repos registered.": "No hay repos registrados.",
    "Use [bold]pdms repo add <path>[/].": "Usa [bold]pdms repo add <ruta>[/].",
    "Register a PDMS repo (defaults to the current folder).": "Registra un repo de PDMS (por defecto la carpeta actual).",
    "Folder inside the repo.": "Carpeta dentro del repo.",
    "Name for the repo.": "Nombre para el repo.",
    "{path} is not inside a PDMS repo (no backend/snakesdk folder).":
        "{path} no está dentro de un repo de PDMS (no hay carpeta backend/snakesdk).",
    "{path} is already registered as '{alias}'.": "{path} ya está registrado como '{alias}'.",
    "Repo '{alias}' registered ({path}).": "Repo '{alias}' registrado ({path}).",
    "Make it the current repo?": "¿Hacerlo el repo actual?",
    "Choose the current repo.": "Elige el repo actual.",
    "'{alias}' is already the current repo.": "'{alias}' ya es el repo actual.",
    "Forget a registered repo (nothing is deleted from disk).": "Olvida un repo registrado (no se borra nada del disco).",
    "Choose the current one": "Elegir el actual",
    # ------------------------------------------------------------------ ui
    "Open the pdms web interface (local only; Ctrl+C to stop it).":
        "Abre la interfaz web de pdms (solo en local; Ctrl+C para pararla).",
    "Port to listen on (the next free one if it is in use).": "Puerto en el que escuchar (el siguiente libre si está en uso).",
    "Open it in the browser.": "Abrirla en el navegador.",
    "[dim]Port {port} is in use; using {free}.[/]": "[dim]El puerto {port} está en uso; uso el {free}.[/]",
    "pdms ui is running at {url}": "pdms ui está corriendo en {url}",
    "[dim]Only this machine can open it, and only with this link. Ctrl+C to stop it.[/]":
        "[dim]Solo se puede abrir desde esta máquina y solo con este enlace. Ctrl+C para pararla.[/]",
    "pdms ui stopped.": "pdms ui parada.",
    "Open it in a window of its own instead of the browser (needs the desktop extra).":
        "Abrirla en una ventana propia en vez del navegador (necesita el extra desktop).",
    "The window needs pywebview, which comes with the desktop extra. Install it with:\n  {command}\n"
    "or use pdms ui to open it in the browser.":
        "La ventana necesita pywebview, que viene con el extra desktop. Instálalo con:\n  {command}\n"
        "o usa pdms ui para abrirla en el navegador.",
    "[dim]Close the window (or Ctrl+C) to stop it; the link also opens it in a browser.[/]":
        "[dim]Cierra la ventana (o Ctrl+C) para pararla; el enlace también la abre en un navegador.[/]",
    "Could not open the window: {error}. pdms ui opens it in the browser.":
        "No se pudo abrir la ventana: {error}. pdms ui la abre en el navegador.",
    "{key} is busy ({phase}).": "{key} está ocupada ({phase}).",
    "{key} is busy.": "{key} está ocupada.",
    "There is no instance {key}.": "No hay ninguna instancia {key}.",
    "{key} is running; stop it first.": "{key} está corriendo; párala primero.",
    "{cmd} failed (exit code {code}).": "{cmd} falló (código de salida {code}).",
    "Not services of the current repo: {names}": "No son servicios del repo actual: {names}",
    "local SNS": "SNS local",
    "Nothing was published to the local SNS yet.": "Todavía no se publicó nada en el SNS local.",
    "every SNS publish · pdms events peek {queue}": "todo lo publicado a SNS · pdms events peek {queue}",
    # ------------------------------------------------------------------ proxy
    "Local API gateway: one port for every service, local instances first, the remote API otherwise.":
        "Gateway local: un solo puerto para todos los servicios; primero las instancias locales, si no la API remota.",
    "Port to listen on.": "Puerto en el que escuchar.",
    "Act as this user on local services (X-Dev-* headers).":
        "Actuar como este usuario en los servicios locales (cabeceras X-Dev-*).",
    "Remote API for what is not running locally (saved for the repo).":
        "API remota para lo que no corre en local (se guarda para el repo).",
    "Never forward to the remote API.": "No reenviar nunca a la API remota.",
    "Terraform environment to read the routes from.": "Entorno de Terraform del que leer las rutas.",
    "Point frontend/.env.local to the proxy (asked if omitted).":
        "Apuntar frontend/.env.local al proxy (se pregunta si se omite).",
    "Point the frontend to the proxy? (writes VITE_APP_API_URL in frontend/.env.local, undone when it stops)":
        "¿Apunto el frontend al proxy? (escribe VITE_APP_API_URL en frontend/.env.local; se deshace al pararlo)",
    "{path} points to the proxy until it stops; restart yarn dev to apply it.":
        "{path} apunta al proxy hasta que se pare; reinicia yarn dev para aplicarlo.",
    "{path} restored; restart yarn dev to apply it.": "{path} restaurado; reinicia yarn dev para aplicarlo.",
    "{path} restored (left over by the previous proxy).": "{path} restaurado (lo dejó el proxy anterior).",
    "The proxy is already running on port {port} (pid {pid}).": "El proxy ya está corriendo en el puerto {port} (pid {pid}).",
    "The proxy exited while starting: {line}": "El proxy terminó mientras arrancaba: {line}",
    "No Terraform for '{env}' in {path}.": "No hay Terraform para '{env}' en {path}.",
    "Reading the API routes from Terraform...": "Leyendo las rutas de la API desde Terraform...",
    "[dim]Remote API taken from frontend/.env and saved for '{alias}': {url}[/]":
        "[dim]API remota tomada de frontend/.env y guardada para '{alias}': {url}[/]",
    "Proxy": "Proxy",
    "Remote": "Remota",
    "routes": "rutas",
    "none (only local services)": "ninguna (solo servicios locales)",
    "Acting as": "Actuando como",
    "each service's own profile": "el perfil de cada servicio",
    "Requests · Ctrl+C to stop": "Peticiones · Ctrl+C para parar",
    "Proxy stopped.": "Proxy parado.",
    "Background (pdms ps, logs proxy, stop proxy) or foreground (asked if omitted).":
        "En segundo plano (pdms ps, logs proxy, stop proxy) o en primer plano (se pregunta si se omite).",
    "How should the proxy run?": "¿Cómo quieres correr el proxy?",
    "Foreground (in this terminal, with every request live)": "En primer plano (en esta terminal, con cada petición en vivo)",
    "Background (keeps running; requests with pdms logs proxy)":
        "En segundo plano (sigue corriendo; las peticiones con pdms logs proxy)",
    "Starting the proxy...": "Arrancando el proxy...",
    "The proxy exited while starting. Full log: {log}": "El proxy terminó mientras arrancaba. Log completo: {log}",
    "The proxy is responding at {url} (pid {pid})": "El proxy responde en {url} (pid {pid})",
    "⚠ The proxy is not responding yet; check its log.": "⚠ El proxy todavía no responde; revisa su log.",
    "  Requests: [bold]pdms logs proxy[/]   Stop: [bold]pdms stop proxy[/]":
        "  Peticiones: [bold]pdms logs proxy[/]   Parar: [bold]pdms stop proxy[/]",
    "The proxy is not running.": "El proxy no está corriendo.",
    "The proxy has no log. Start it in the background with [bold]pdms proxy -b[/].":
        "El proxy no tiene log. Arráncalo en segundo plano con [bold]pdms proxy -b[/].",
    "Show which service handles each route and where the proxy would send it.":
        "Muestra qué servicio atiende cada ruta y adónde la enviaría el proxy.",
    "Only routes whose path or service contains this text.": "Solo las rutas cuya ruta o servicio contenga este texto.",
    "Only routes served by a local instance.": "Solo las rutas que atiende una instancia local.",
    "Method": "Método",
    "Target": "Destino",
    "remote": "remota",
    "in another repo": "en otro repo",
    "not available": "no disponible",
    "{shown} of {total} routes.": "{shown} de {total} rutas.",
    "Through the proxy: {url} + the same paths": "Por el proxy: {url} + las mismas rutas",
    "{service} is running from another repo ({key}); not mixing versions.":
        "{service} está corriendo desde otro repo ({key}); no mezclo versiones.",
    "No route for {method} {path} in the repo's Terraform.": "No hay ruta para {method} {path} en el Terraform del repo.",
    "{service} is not running locally. Start it with: pdms run {service} -b":
        "{service} no está corriendo en local. Levántalo con: pdms run {service} -b",
    "All local services": "Todos los servicios locales",
    "No local services running. Start one with pdms run -b.":
        "No hay servicios locales corriendo. Levanta uno con pdms run -b.",
    "live specs of the services running locally": "specs en vivo de los servicios que corren en local",
    "remote docs": "docs remotas",
    # ------------------------------------------------------------------ versions / updates
    "Update pdms to the latest release (or to --version).": "Actualiza pdms a la última versión (o a --version).",
    "Install this version instead of the latest.": "Instalar esta versión en lugar de la última.",
    "Only tell whether there is a newer version.": "Solo decir si hay una versión más nueva.",
    "pdms {version} runs from a local checkout (editable install): update it with git pull.":
        "pdms {version} se ejecuta desde una copia local (instalación editable): actualízalo con git pull.",
    "Looking for the latest release...": "Buscando la última versión...",
    "Could not reach GitHub: {error}": "No pude conectar con GitHub: {error}",
    "pdms {version} is the latest version.": "pdms {version} es la última versión.",
    "Current version: {current} · available: {target}": "Versión actual: {current} · disponible: {target}",
    "Run this to update:": "Ejecuta esto para actualizar:",
    "The update failed (exit code {code}).": "La actualización falló (código de salida {code}).",
    "pdms updated to {version}.": "pdms actualizado a {version}.",
    "Show the version and exit.": "Muestra la versión y sale.",
    "Local PDMS services, made easy": "Los servicios de PDMS en local, sin complicaciones",
    "Show the PDMS banner when the menu opens?": "¿Mostrar el banner de PDMS al abrir el menú?",
    "New pdms version available: {current} → {latest} · update with: pdms self-update":
        "Hay una versión nueva de pdms: {current} → {latest} · actualiza con: pdms self-update",
    "Tell me when a new pdms version is available?": "¿Avisarme cuando haya una versión nueva de pdms?",
    "Include alpha/beta pre-releases (automatic if you run one).":
        "Incluir versiones previas alpha/beta (automático si ya usas una).",
    # ------------------------------------------------------------------ doctor
    "Check that everything pdms needs is in place (tools, configuration, databases, repo, ports).":
        "Comprueba que está todo lo que pdms necesita (herramientas, configuración, bases de datos, repo, puertos).",
    "Do not test the database connections.": "No probar las conexiones a las bases de datos.",
    "Seconds to wait for each database.": "Segundos de espera para cada base de datos.",
    "Checking the environment...": "Revisando el entorno...",
    "{ok} ok · {warn} warnings · {fail} problems": "{ok} bien · {warn} avisos · {fail} problemas",
    "Check the environment (doctor)": "Revisar el entorno (doctor)",
    "Version": "Versión",
    "Updates": "Actualizaciones",
    "{latest} is available": "hay una nueva: {latest}",
    "up to date": "al día",
    "pdms needs Python 3.10 or newer.": "pdms necesita Python 3.10 o superior.",
    "Shell": "Terminal",
    "Tab completion": "Autocompletado",
    "not installed": "no instalado",
    "Tools": "Herramientas",
    "not found in PATH": "no está en el PATH",
    "Install it: https://python-poetry.org/docs/#installation": "Instálalo: https://python-poetry.org/docs/#installation",
    "PDMS services need Poetry 1.2 or newer (dependency groups).":
        "Los servicios de PDMS necesitan Poetry 1.2 o superior (grupos de dependencias).",
    "Python for the services": "Python para los servicios",
    "no Python 3.10 / 3.11 found": "no encontré Python 3.10 / 3.11",
    "Services need Python 3.10 or 3.11, e.g.: uv python install 3.11":
        "Los servicios necesitan Python 3.10 o 3.11, p. ej.: uv python install 3.11",
    "Configuration": "Configuración",
    "File": "Fichero",
    "{path} does not exist yet": "{path} aún no existe",
    "Run pdms setup to create it.":
        "Ejecuta pdms setup para crearlo.",
    "Permissions": "Permisos",
    "Users": "Usuarios",
    "Databases": "Bases de datos",
    "without password": "sin contraseña",
    "Database connections": "Conexiones a bases de datos",
    "Check host/port/credentials with pdms db edit {name}, or the VPN.":
        "Revisa host/puerto/credenciales con pdms db edit {name}, o la VPN.",
    "Current repo": "Repo actual",
    "Run pdms inside a PDMS checkout, or: pdms repo add <path>":
        "Ejecuta pdms dentro de una copia de PDMS, o: pdms repo add <ruta>",
    "pdms repo remove {alias}, then pdms repo add <path>": "pdms repo remove {alias}, y después pdms repo add <ruta>",
    "No services found in {path}.": "No encontré servicios en {path}.",
    "Proxy routes (Terraform dev)": "Rutas del proxy (Terraform dev)",
    "not found": "no encontrado",
    "pdms proxy needs infra/infra_auto/environments/dev.": "pdms proxy necesita infra/infra_auto/environments/dev.",
    "Remote API for the proxy": "API remota del proxy",
    "not configured": "no configurada",
    "pdms proxy --remote <url> (or set VITE_APP_API_URL in frontend/.env)":
        "pdms proxy --remote <url> (o define VITE_APP_API_URL en frontend/.env)",
    "Ports": "Puertos",
    "Default service port": "Puerto por defecto de los servicios",
    "Proxy port": "Puerto del proxy",
    "{port} · used by the pdms proxy": "{port} · lo usa el proxy de pdms",
    "{port} · free": "{port} · libre",
    "{port} · in use": "{port} · ocupado",
    "pdms will offer the next free port.": "pdms propondrá el siguiente puerto libre.",
    "Background services": "Servicios en segundo plano",
    "Instances": "Instancias",
    "none running": "ninguna corriendo",
    "With errors": "Con errores",
    "Stopped": "Paradas",
    "Outdated installed code": "Código instalado desactualizado",
    # ------------------------------------------------------------------ events
    "Local SQS events: the repo's event map and a local ElasticMQ with all its queues.":
        "Eventos SQS en local: el mapa de eventos del repo y un ElasticMQ local con todas sus colas.",
    "Reading the event map (Terraform and backend/common/event)...":
        "Leyendo el mapa de eventos (Terraform y backend/common/event)...",
    "No SQS queues found in {path}.": "No encontré colas SQS en {path}.",
    "Show every event type, the queue the broker sends it to and its consumer.":
        "Muestra cada tipo de evento, la cola a la que lo envía el broker y su consumidor.",
    "Only rows containing this text.": "Solo las filas que contengan este texto.",
    "Event type": "Tipo de evento",
    "Queue": "Cola",
    "Consumer": "Consumidor",
    "Source": "Origen",
    "Queues not routed by the broker": "Colas que no enruta el broker",
    "{types} event types · {queues} queues · {consumers} consumers · broker: {broker}":
        "{types} tipos de evento · {queues} colas · {consumers} consumidores · broker: {broker}",
    "Start a local ElasticMQ (Docker) with every queue of the repo, and the broker.":
        "Levanta un ElasticMQ local (Docker) con todas las colas del repo, y el broker.",
    "Also run the broker (broker-sqs-event) locally.": "Levantar también el broker (broker-sqs-event) en local.",
    "Broker": "Broker",
    "[dim]The broker is already running.[/]": "[dim]El broker ya está corriendo.[/]",
    "⚠ The broker ({service}) was not found in the repo; events stay in the broker queue.":
        "⚠ No encontré el broker ({service}) en el repo; los eventos se quedarán en la cola del broker.",
    "⚠ Configure a user and a database to run the broker (pdms user add, pdms db add), then: pdms run {service} -b":
        "⚠ Configura un usuario y una base de datos para levantar el broker (pdms user add, pdms db add), y luego: "
        "pdms run {service} -b",
    "Configure a user and a database to run the broker (pdms user add, pdms db add), or start the events without it.":
        "Configura un usuario y una base de datos para levantar el broker (pdms user add, pdms db add), o arranca los "
        "eventos sin él.",
    "⚠ The broker did not start; see pdms logs {name}": "⚠ El broker no arrancó; mira pdms logs {name}",
    "{key} is consuming {queue} (pid {pid})": "{key} está consumiendo {queue} (pid {pid})",
    "{key} is an event consumer: it has no web page. See its logs: pdms logs {key}":
        "{key} es un consumidor de eventos: no tiene página web. Mira sus logs: pdms logs {key}",
    "Docker is not available: {detail}": "Docker no está disponible: {detail}",
    "docker not found": "no encuentro docker",
    "Port {port} is in use by something else (maybe infra/local_sqs's docker compose). Stop it or change "
    "events_port in pdms config.":
        "El puerto {port} lo usa otra cosa (quizá el docker compose de infra/local_sqs). Páralo o cambia "
        "events_port en pdms config.",
    "Starting ElasticMQ...": "Arrancando ElasticMQ...",
    "Could not start ElasticMQ: {error}": "No pude arrancar ElasticMQ: {error}",
    "ElasticMQ did not answer on {url}; see: docker logs {name}": "ElasticMQ no responde en {url}; mira: docker logs {name}",
    "ElasticMQ started": "ElasticMQ arrancado",
    "ElasticMQ restarted with the updated queues": "ElasticMQ reiniciado con las colas actualizadas",
    "ElasticMQ was already running with these queues": "ElasticMQ ya estaba corriendo con estas colas",
    "{count} queues ({extra} only in infra/local_sqs/elasticmq.conf) · broker: {broker}":
        "{count} colas ({extra} solo en infra/local_sqs/elasticmq.conf) · broker: {broker}",
    "Stop the local ElasticMQ (its messages are lost).": "Para el ElasticMQ local (se pierden sus mensajes).",
    "ElasticMQ stopped.": "ElasticMQ parado.",
    "ElasticMQ is not running.": "ElasticMQ no está corriendo.",
    "Show whether ElasticMQ is running and the messages waiting in each queue.":
        "Muestra si ElasticMQ está corriendo y los mensajes pendientes en cada cola.",
    "Also list empty queues.": "Listar también las colas vacías.",
    "ElasticMQ is not running. Start it with [bold]pdms events up[/].":
        "ElasticMQ no está corriendo. Levántalo con [bold]pdms events up[/].",
    "Waiting": "Pendientes",
    "In flight": "En proceso",
    "ElasticMQ running at {url} · {count} queues": "ElasticMQ corriendo en {url} · {count} colas",
    "All queues are empty (--all to list them).": "Todas las colas están vacías (--all para listarlas).",
    "Install Docker and start it: https://docs.docker.com/get-docker/":
        "Instala Docker y arráncalo: https://docs.docker.com/get-docker/",
    # ------------------------------------------------------------------ events: publishing
    "Where the service publishes SQS events: auto, local (pdms events broker) or aws.":
        "Dónde publica el servicio los eventos SQS: auto, local (broker de pdms events) o aws.",
    "Unknown events mode '{mode}'. Available: {codes}": "Modo de eventos desconocido '{mode}'. Disponibles: {codes}",
    "AWS (the service's own configuration)": "AWS (la configuración del propio servicio)",
    "AWS (run pdms events up to publish locally)": "AWS (ejecuta pdms events up para publicar en local)",
    "The local ElasticMQ is not running. Start it now?": "El ElasticMQ local no está corriendo. ¿Lo levanto ahora?",
    "--events local needs the local ElasticMQ: pdms events up": "--events local necesita el ElasticMQ local: pdms events up",
    "⚠ No broker queue found in the repo's Terraform; SQS_EVENT_BROKER_URL is left as configured.":
        "⚠ No encontré la cola del broker en el Terraform del repo; SQS_EVENT_BROKER_URL queda como esté configurado.",
    "local broker · {url}": "broker local · {url}",
    "local ElasticMQ · {url}": "ElasticMQ local · {url}",
    "Events": "Eventos",
    "Publishing to the local broker: {names}": "Publicando en el broker local: {names}",
    "Where should services publish SQS events?": "¿Dónde deben publicar los servicios los eventos SQS?",
    "auto: local broker when pdms events up is running": "auto: broker local cuando pdms events up está corriendo",
    "local: always the local broker": "local: siempre el broker local",
    "aws: as configured by each service": "aws: como lo tenga configurado cada servicio",
    # ------------------------------------------------------------------ events: tools
    "Send an event (through the broker, or --direct to its queue) or a message to a queue.":
        "Envía un evento (a través del broker, o --direct a su cola) o un mensaje a una cola.",
    "Event type (e.g. email-notify) or queue name.": "Tipo de evento (p. ej. email-notify) o nombre de cola.",
    "Event fields / message body as JSON.": "Campos del evento / cuerpo del mensaje en JSON.",
    "Read the JSON from a file ('-' = stdin).": "Leer el JSON de un fichero ('-' = stdin).",
    "Skip the broker: send the event straight to its queue.": "Saltar el broker: enviar el evento directamente a su cola.",
    "Print the fields of the event type and exit.": "Muestra los campos del tipo de evento y sale.",
    "'{target}' is not an event type nor a queue.": "'{target}' no es un tipo de evento ni una cola.",
    "Did you mean: {names}": "¿Quizá: {names}?",
    "No event class found for '{target}' in backend/common/event.":
        "No encontré una clase de evento para '{target}' en backend/common/event.",
    "Invalid JSON: {error}": "JSON no válido: {error}",
    "Event fields must be a JSON object.": "Los campos del evento deben ser un objeto JSON.",
    "Sent {id} to {queue}": "Enviado {id} a {queue}",
    "The broker routes it to {queue} (consumer: {consumer}).": "El broker lo enruta a {queue} (consumidor: {consumer}).",
    "Nothing is consuming {queue} right now; it waits there (pdms events peek {queue}).":
        "Ahora mismo nadie consume {queue}; se queda esperando ahí (pdms events peek {queue}).",
    "Show the messages waiting in a queue, without consuming them.":
        "Muestra los mensajes pendientes de una cola, sin consumirlos.",
    "Queue name.": "Nombre de la cola.",
    "Maximum number of messages.": "Número máximo de mensajes.",
    "Print the whole body of each message.": "Mostrar el cuerpo completo de cada mensaje.",
    "Could not read {queue}: {error}": "No pude leer {queue}: {error}",
    "{queue} is empty.": "{queue} está vacía.",
    "received {n} times": "recibido {n} veces",
    "Delete every message of a queue (or of all of them).": "Borra todos los mensajes de una cola (o de todas).",
    "Purge every queue.": "Vaciar todas las colas.",
    "Do not ask for confirmation.": "No pedir confirmación.",
    "Unknown queue '{queue}'. See pdms events status --all.": "Cola desconocida '{queue}'. Mira pdms events status --all.",
    "Queue:": "Cola:",
    "Delete {count} messages from {queues}?": "¿Borrar {count} mensajes de {queues}?",
    "Purged {queues}.": "Vaciado: {queues}.",
    "Events (local SQS):": "Eventos (SQS local):",
    "Start ElasticMQ and the broker": "Levantar ElasticMQ y el broker",
    "Event map": "Mapa de eventos",
    "Peek a queue": "Ver los mensajes de una cola",
    "Purge a queue": "Vaciar una cola",
    "Stop everything": "Parar todo",
    "📨 Events (local SQS)": "📨 Eventos (SQS local)",
    # ------------------------------------------------------------------ migrations (Flyway)
    "Flyway (pdms-db-migrations): info and validate against any database; migrate only against a local one.":
        "Flyway (pdms-db-migrations): info y validate contra cualquier base de datos; migrate solo contra una local.",
    "info (default), validate or migrate.": "info (por defecto), validate o migrate.",
    "pdms-db-migrations folder (remembered for the repo).": "Carpeta de pdms-db-migrations (se recuerda para el repo).",
    "'{command}' is not allowed from pdms. Available: {codes}.": "'{command}' no está permitido desde pdms. Disponibles: {codes}.",
    "migrate only runs against a local database (localhost, not protected). '{name}' ({host}) is shared: "
    "it is migrated by the pdms-db-migrations pipeline.":
        "migrate solo se ejecuta contra una base de datos local (localhost, no protegida). '{name}' ({host}) es "
        "compartida: la migra el pipeline de pdms-db-migrations.",
    "Migrations": "Migraciones",
    "Downloading the Flyway image (about 360 MB, only the first time)...":
        "Descargando la imagen de Flyway (unos 360 MB, solo la primera vez)...",
    "Could not download {image}: {error}. Check the connection (public ECR also limits anonymous downloads; try "
    "again in a few minutes).":
        "No pude descargar {image}: {error}. Revisa la conexión (el ECR público también limita las descargas "
        "anónimas; prueba de nuevo en unos minutos).",
    "Migrations repo (Flyway)": "Repo de migraciones (Flyway)",
    "[dim]Migrations repo saved for '{alias}': {path}[/]": "[dim]Repo de migraciones guardado para '{alias}': {path}[/]",
    "{path} is not a Flyway migrations repo (flyway.toml + migrations/).":
        "{path} no es un repo de migraciones de Flyway (flyway.toml + migrations/).",
    "Several migrations repos found ({names}); choose one with --migrations PATH.":
        "Hay varios repos de migraciones ({names}); elige uno con --migrations RUTA.",
    "Which migrations repo goes with '{alias}'?": "¿Qué repo de migraciones va con '{alias}'?",
    "No Flyway migrations repo found. Clone pdms-db-migrations next to the PDMS repo, or use --migrations PATH.":
        "No encontré un repo de migraciones de Flyway. Clona pdms-db-migrations junto al repo de PDMS, o usa --migrations RUTA.",
    "Clone pdms-db-migrations next to the PDMS repo, or pdms migrate --migrations PATH":
        "Clona pdms-db-migrations junto al repo de PDMS, o pdms migrate --migrations RUTA",
    # ------------------------------------------------------------------ guided setup
    "Guided setup: import a shared configuration, then configure whatever is still missing.":
        "Configuración guiada: importa una configuración compartida y luego configura lo que falte.",
    "Guided setup": "Configuración guiada",
    "[bold]pdms setup[/]: first a configuration to import, if you have one, then whatever is missing.":
        "[bold]pdms setup[/]: primero una configuración para importar, si tienes una, y luego lo que falte.",
    "Welcome! There is no pdms configuration yet. Run the guided setup now?":
        "¡Bienvenido/a! Todavía no hay configuración de pdms. ¿Hacemos ahora la configuración guiada?",
    "Do you have a pdms configuration to import (e.g. exported by a teammate)?":
        "¿Tienes una configuración de pdms para importar (p. ej. exportada por alguien del equipo)?",
    "Another file...": "Otro fichero...",
    "Nothing imported; the next steps set it up by hand.":
        "No se ha importado nada; los siguientes pasos lo configuran a mano.",
    "Skipped; do it later with {command}.": "Omitido; hazlo más tarde con {command}.",
    "PDMS repo": "Repo de PDMS",
    "Use {path} as the PDMS repo?": "¿Usar {path} como repo de PDMS?",
    "Folder of your PDMS checkout (empty = skip):": "Carpeta de tu checkout de PDMS (vacío = omitir):",
    "Not a PDMS repo (no backend/snakesdk folder).": "No es un repo de PDMS (no hay carpeta backend/snakesdk).",
    "Folder of your pdms-db-migrations checkout (empty = skip):":
        "Carpeta de tu checkout de pdms-db-migrations (vacío = omitir):",
    "Not a Flyway migrations repo (flyway.toml + migrations/).":
        "No es un repo de migraciones de Flyway (flyway.toml + migrations/).",
    "Services need at least one database to run.": "Los servicios necesitan al menos una base de datos.",
    "Add a database now?": "¿Añadir una base de datos ahora?",
    "Add another database?": "¿Añadir otra base de datos?",
    "Password of '{name}' ({user}@{host}) (empty = later):": "Contraseña de '{name}' ({user}@{host}) (vacío = más tarde):",
    "Test the connection to {names}?": "¿Probar la conexión con {names}?",
    "Development users": "Usuarios de desarrollo",
    "Services run as a DEV_* user. How do you want to add them?":
        "Los servicios corren con un usuario DEV_*. ¿Cómo quieres añadirlos?",
    "Import them from the pdms_user table of a database": "Importarlos de la tabla pdms_user de una base de datos",
    "Add one by hand": "Añadir uno a mano",
    "Later": "Más tarde",
    "Add another user?": "¿Añadir otro usuario?",
    "Stacks": "Stacks",
    "Create a stack (services you usually start together) now?":
        "¿Crear ahora un stack (servicios que sueles levantar juntos)?",
    "Review the other defaults (port, log level, install, events...)?":
        "¿Revisar el resto de valores por defecto (puerto, nivel de log, install, eventos...)?",
    "Using the standard values (change them with pdms config).":
        "Se usan los valores estándar (cámbialos con pdms config).",
    "Check the environment now (pdms doctor)?": "¿Comprobar ahora el entorno (pdms doctor)?",
    "Setup finished. Run pdms to open the menu; pdms setup again completes what is left.":
        "Configuración terminada. Ejecuta pdms para abrir el menú; pdms setup de nuevo completa lo que falte.",
    "Still missing: {steps}": "Falta todavía: {steps}",
    "migrations repo": "repo de migraciones",
    "database passwords": "contraseñas de las bases de datos",
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
    "🌐 Proxy (one port for every service)": "🌐 Proxy (un solo puerto para todos los servicios)",
    "🗄  Databases": "🗄  Bases de datos",
    "👤 Users": "👤 Usuarios",
    "⚙  Settings": "⚙  Configuración",
    "✕  Exit": "✕  Salir",
}
