"""Interactive wizards built on questionary.

All prompts use ``unsafe_ask`` so Ctrl+C raises KeyboardInterrupt, handled once in the CLI entrypoint.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Iterable

import questionary
import typer

from .config import Database, Defaults, DevUser

LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


def require_tty() -> None:
    if not sys.stdin.isatty():
        raise typer.BadParameter("Se necesita una terminal interactiva (o pasa las opciones por flags).")


def _is_int(value: str) -> bool | str:
    return value.isdigit() or "Debe ser un número"


def _not_empty(value: str) -> bool | str:
    return bool(value.strip()) or "Campo obligatorio"


def ask_name(kind: str, taken: Iterable[str]) -> str:
    taken = set(taken)

    def validate(value: str) -> bool | str:
        value = value.strip()
        if not value:
            return "Campo obligatorio"
        if value in taken:
            return "Ya existe ese nombre"
        if not all(c.isalnum() or c in "-_" for c in value):
            return "Usa solo letras, números, '-' o '_'"
        return True

    return questionary.text(f"Alias ({kind}):", validate=validate).unsafe_ask().strip()


def select_name(message: str, names: list[str], default: str = "") -> str:
    return questionary.select(message, choices=names, default=default if default in names else None).unsafe_ask()


def ask_port(default: int, is_free: Callable[[int], bool]) -> int:
    def validate(value: str) -> bool | str:
        if not value.isdigit() or not 1 <= int(value) <= 65535:
            return "Debe ser un número entre 1 y 65535"
        return is_free(int(value)) or f"El puerto {value} está ocupado"

    return int(questionary.text("Puerto del servicio:", default=str(default), validate=validate).unsafe_ask())


def ask_background() -> bool:
    return questionary.select(
        "¿Cómo lo levanto?",
        choices=[
            questionary.Choice("Segundo plano (puedes levantar varios; logs con pdms logs)", True),
            questionary.Choice("Primer plano (en esta terminal)", False),
        ],
    ).unsafe_ask()


def ask_database(current: Database | None = None) -> Database:
    c = current or Database(host="localhost")
    host = questionary.text("Host:", default=c.host, validate=_not_empty).unsafe_ask()
    port = questionary.text("Puerto:", default=str(c.port), validate=_is_int).unsafe_ask()
    database = questionary.text("Base de datos:", default=c.database, validate=_not_empty).unsafe_ask()
    user = questionary.text("Usuario:", default=c.user, validate=_not_empty).unsafe_ask()
    hint = " (vacío = mantener la actual)" if current and current.password else ""
    password = questionary.password(f"Contraseña{hint}:").unsafe_ask()
    if not password and current:
        password = current.password
    protected = questionary.confirm(
        "¿Es una DB compartida/remota? (pedirá confirmación antes de levantar)",
        default=c.protected if current else host not in ("localhost", "127.0.0.1"),
    ).unsafe_ask()
    return Database(
        host=host.strip(),
        port=int(port),
        database=database.strip(),
        user=user.strip(),
        password=password,
        driver=c.driver,
        protected=protected,
    )


def ask_user(current: DevUser | None = None) -> DevUser:
    c = current or DevUser(user_id="", username="")
    return DevUser(
        user_id=questionary.text("DEV_USER_ID (uuid):", default=c.user_id, validate=_not_empty).unsafe_ask().strip(),
        username=questionary.text("DEV_USERNAME (email):", default=c.username, validate=_not_empty).unsafe_ask().strip(),
        first_name=questionary.text("DEV_FIRST_NAME:", default=c.first_name).unsafe_ask().strip(),
        last_name=questionary.text("DEV_LAST_NAME:", default=c.last_name).unsafe_ask().strip(),
        roles=questionary.text("DEV_ROLES (separados por coma):", default=c.roles).unsafe_ask().strip(),
    )


def ask_defaults(current: Defaults) -> Defaults:
    host = questionary.text("Host de uvicorn:", default=current.host).unsafe_ask()
    port = questionary.text("Puerto por defecto:", default=str(current.port), validate=_is_int).unsafe_ask()
    level = questionary.select("LOGGING_LEVEL:", choices=LOG_LEVELS, default=current.logging_level).unsafe_ask()
    reload = questionary.confirm("¿Usar --reload?", default=current.reload).unsafe_ask()
    install = questionary.confirm("¿Hacer poetry lock && poetry install antes de levantar?", default=current.install).unsafe_ask()
    db_timeout = questionary.text(
        "Timeout del test de conexión (segundos):", default=str(current.db_timeout), validate=_is_int
    ).unsafe_ask()
    backend_path = questionary.path(
        "Carpeta backend de PDMS (para listar servicios):",
        default=current.backend_path,
        only_directories=True,
        validate=lambda v: not v or Path(v).expanduser().is_dir() or "No existe esa carpeta",
    ).unsafe_ask()
    env = dict(current.env)
    while questionary.confirm(
        f"¿Añadir/editar variables de entorno extra? (actuales: {', '.join(env) or 'ninguna'})", default=False
    ).unsafe_ask():
        key = questionary.text("Nombre de la variable:", validate=_not_empty).unsafe_ask().strip()
        value = questionary.text(f"Valor de {key} (vacío = borrar):", default=env.get(key, "")).unsafe_ask()
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    return Defaults(
        host=host.strip(), port=int(port), logging_level=level, reload=reload, install=install,
        db_timeout=int(db_timeout), backend_path=backend_path.strip(), env=env,
    )
