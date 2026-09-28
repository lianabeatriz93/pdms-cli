"""``pdms`` command line entrypoint."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import questionary
import typer
from rich.console import Console
from rich.table import Table

from . import instances, prompts, runner, vscode
from .config import Config, Database, DevUser, Stack, config_path

console = Console()

app = typer.Typer(help="Levanta servicios de PDMS en local. Sin argumentos abre el menú interactivo.", add_completion=True)
db_app = typer.Typer(help="Gestionar bases de datos.", invoke_without_command=True)
user_app = typer.Typer(help="Gestionar usuarios de desarrollo (DEV_*).", invoke_without_command=True)
config_app = typer.Typer(help="Configuración general.", invoke_without_command=True)
stack_app = typer.Typer(help="Gestionar stacks (grupos de servicios que se levantan juntos).", invoke_without_command=True)
app.add_typer(db_app, name="db")
app.add_typer(user_app, name="user")
app.add_typer(config_app, name="config")
app.add_typer(stack_app, name="stack")


def fail(message: str) -> None:
    console.print(f"[red]✗[/] {message}")
    raise typer.Exit(1)


# --------------------------------------------------------------------------- tables


def print_dbs(cfg: Config) -> None:
    if not cfg.dbs:
        console.print("[yellow]No hay bases de datos configuradas.[/] Usa [bold]pdms db add[/].")
        return
    table = Table("Nombre", "Host", "Puerto", "DB", "Usuario", "Contraseña", "Protegida")
    for name, db in cfg.dbs.items():
        table.add_row(
            name, db.host, str(db.port), db.database, db.user,
            "****" if db.password else "[red]sin definir[/]", "sí" if db.protected else "",
        )
    console.print(table)


def print_users(cfg: Config) -> None:
    if not cfg.users:
        console.print("[yellow]No hay usuarios configurados.[/] Usa [bold]pdms user add[/].")
        return
    table = Table("Nombre", "DEV_USERNAME", "Nombre completo", "DEV_ROLES", "DEV_USER_ID")
    for name, u in cfg.users.items():
        table.add_row(name, u.username, f"{u.first_name} {u.last_name}".strip(), u.roles, u.user_id)
    console.print(table)


# --------------------------------------------------------------------------- CRUD helpers


def pick(cfg_items: dict, kind: str, name: Optional[str], default: str = "") -> str:
    if name:
        if name not in cfg_items:
            fail(f"'{name}' no existe ({kind}). Disponibles: {', '.join(cfg_items) or 'ninguno'}")
        return name
    if not cfg_items:
        fail(f"No hay nada configurado ({kind}).")
    if len(cfg_items) == 1:
        return next(iter(cfg_items))
    prompts.require_tty()
    return prompts.select_name(f"Elige {kind}:", list(cfg_items), default)


def add_db(cfg: Config) -> str:
    prompts.require_tty()
    name = prompts.ask_name("base de datos", cfg.dbs)
    cfg.dbs[name] = prompts.ask_database()
    cfg.save()
    console.print(f"[green]✓[/] Base de datos '{name}' guardada.")
    if questionary.confirm("¿Probar la conexión ahora?", default=True).unsafe_ask():
        check_db(name, cfg.dbs[name], cfg.defaults.db_timeout)
    return name


def add_user(cfg: Config) -> str:
    prompts.require_tty()
    name = prompts.ask_name("usuario", cfg.users)
    cfg.users[name] = prompts.ask_user()
    cfg.save()
    console.print(f"[green]✓[/] Usuario '{name}' guardado.")
    return name


def check_db(name: str, db: Database, timeout: int) -> bool:
    with console.status(f"Conectando a {name} ({db.host}:{db.port}, timeout {timeout}s)..."):
        try:
            version = runner.test_connection(db, timeout)
        except Exception as exc:  # noqa: BLE001 - show any driver error to the user
            console.print(f"[red]✗[/] {name}: {str(exc).strip()}")
            return False
    console.print(f"[green]✓[/] {name}: {version.split(',')[0]}")
    return True


# --------------------------------------------------------------------------- services


def services_root(cfg: Config) -> Optional[Path]:
    if cfg.defaults.backend_path:
        root = Path(cfg.defaults.backend_path).expanduser()
        if root.is_dir():
            return root.resolve()
        console.print(f"[yellow]⚠ La carpeta backend configurada no existe: {root}[/]")
    return None


def list_services(cfg: Config) -> tuple[Path, list[Path]]:
    root = services_root(cfg) or Path.cwd().resolve()
    candidates = runner.find_services_below(root)
    if not candidates:
        hint = "" if cfg.defaults.backend_path else " Configura la carpeta backend con [bold]pdms config[/]."
        fail(f"No encontré servicios (pyproject.toml + main.py) en {root}.{hint}")
    return root, candidates


def label(service: Path, root: Path) -> str:
    try:
        return str(service.relative_to(root))
    except ValueError:
        return str(service)


def running_by_service() -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for inst in instances.load().values():
        if inst.alive():
            result.setdefault(inst.service, []).append(inst.port)
    return result


def choose_service(candidates: list[Path], root: Path, message: str = "Servicio a levantar") -> Path:
    prompts.require_tty()
    running = running_by_service()
    labels = {label(c, root): c for c in candidates}
    meta = {
        text: "corriendo en :" + ", :".join(map(str, running[str(path)]))
        for text, path in labels.items() if str(path) in running
    }

    def matching(text: str) -> list[str]:
        text = text.strip()
        return [text] if text in labels else [t for t in labels if text and text in t]

    def validate(text: str) -> bool | str:
        found = matching(text)
        if len(found) == 1:
            return True
        return f"{len(found)} servicios coinciden, afina más" if found else "Ningún servicio coincide"

    choice = questionary.autocomplete(
        f"{message} (escribe para filtrar, Tab para ver la lista):",
        choices=list(labels),
        meta_information=meta,
        validate=validate,
        match_middle=True,
    ).unsafe_ask()
    return labels[matching(choice)[0]]


def resolve_service(cfg: Config, name: Optional[str], path: Optional[Path]) -> Path:
    if path:
        if service := runner.find_service_upwards(path.expanduser().resolve()):
            return service
        fail(f"{path} no es (ni está dentro de) un servicio.")
    if name:
        root, candidates = list_services(cfg)
        exact = [c for c in candidates if name in (c.name, label(c, root))]
        matches = exact or [c for c in candidates if name in label(c, root)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            fail(f"Ningún servicio coincide con '{name}' en {root}.")
        return choose_service(matches, root)
    if service := runner.find_service_upwards(Path.cwd().resolve()):
        return service
    root, candidates = list_services(cfg)
    return choose_service(candidates, root)


@app.command("services")
def services_cmd(filter: Optional[str] = typer.Argument(None, help="Filtrar por texto.")) -> None:
    """Lista los servicios disponibles en la carpeta backend."""
    cfg = Config.load()
    root, candidates = list_services(cfg)
    running = running_by_service()
    table = Table("Servicio", "Corriendo", title=str(root), title_justify="left")
    for c in candidates:
        text = label(c, root)
        if filter and filter not in text:
            continue
        ports = running.get(str(c), [])
        table.add_row(text, "[green]" + ", ".join(f":{p}" for p in ports) + "[/]" if ports else "")
    console.print(table)


# --------------------------------------------------------------------------- run


@dataclass
class Profile:
    service: Path
    user_name: str
    user: DevUser
    db_name: str
    db: Database
    host: str
    port: int


def choose_profile(
    cfg: Config,
    service_name: Optional[str],
    path: Optional[Path],
    user: Optional[str],
    db: Optional[str],
    port: Optional[int],
    host: Optional[str],
    yes: bool,
) -> Profile:
    """Resolve service, dev user, database and a free port, asking for whatever was not given."""
    service = resolve_service(cfg, service_name, path)
    interactive = sys.stdin.isatty()

    if not cfg.users:
        console.print("[yellow]No hay usuarios configurados, vamos a crear uno.[/]")
        add_user(cfg)
    if not cfg.dbs:
        console.print("[yellow]No hay bases de datos configuradas, vamos a crear una.[/]")
        add_db(cfg)

    user_name = pick(cfg.users, "usuario", user, cfg.last_user)
    db_name = pick(cfg.dbs, "base de datos", db, cfg.last_db)
    dev_user, database = cfg.users[user_name], cfg.dbs[db_name]
    if not database.password:
        console.print(f"[yellow]⚠ La base de datos '{db_name}' no tiene contraseña configurada.[/]")

    host = host or cfg.defaults.host
    # Ports of background instances count as taken even while they are still booting.
    taken = instances.running_ports()
    is_free = lambda p: p not in taken and runner.port_is_free(host, p)  # noqa: E731
    if port is None and interactive:
        port = prompts.ask_port(runner.next_free_port(host, cfg.defaults.port, taken), is_free)
    port = port or cfg.defaults.port
    if not is_free(port):
        free = runner.next_free_port(host, port + 1, taken)
        if yes or not interactive:
            fail(f"El puerto {port} está ocupado (el siguiente libre es {free}). Usa --port.")
        if not questionary.confirm(f"El puerto {port} está ocupado. ¿Usar el {free}?", default=True).unsafe_ask():
            raise typer.Exit(1)
        port = free
    return Profile(service, user_name, dev_user, db_name, database, host, port)


def print_summary(cfg: Config, prof: Profile, extra: dict[str, str], show_service: bool = True) -> None:
    summary = Table.grid(padding=(0, 2))
    if show_service:
        summary.add_row("[bold]Servicio[/]", str(prof.service))
    summary.add_row("[bold]Usuario[/]", f"{prof.user_name} → {prof.user.username} [dim]({prof.user.roles})[/]")
    summary.add_row("[bold]DB[/]", f"{prof.db_name} → {prof.db.url(mask=True)}")
    for key, value in extra.items():
        summary.add_row(f"[bold]{key}[/]", value)
    console.print(summary)


def confirm_protected(cfg: Config, prof: Profile, yes: bool) -> None:
    if prof.db.protected and not yes:
        prompts.require_tty()
        if not questionary.confirm(f"'{prof.db_name}' es una DB protegida. ¿Continuar?", default=False).unsafe_ask():
            raise typer.Exit(1)
    cfg.last_user, cfg.last_db = prof.user_name, prof.db_name
    cfg.save()


def poetry_install(service: Path) -> None:
    try:
        runner.ensure_poetry()
        console.rule("poetry lock && poetry install")
        runner.install(service)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        fail(str(exc))


def do_run(
    service_name: Optional[str] = None,
    user: Optional[str] = None,
    db: Optional[str] = None,
    port: Optional[int] = None,
    host: Optional[str] = None,
    install: Optional[bool] = None,
    reload: Optional[bool] = None,
    yes: bool = False,
    path: Optional[Path] = None,
    background: Optional[bool] = None,
) -> None:
    cfg = Config.load()
    prof = choose_profile(cfg, service_name, path, user, db, port, host, yes)
    if background is None:
        background = sys.stdin.isatty() and prompts.ask_background()
    install = cfg.defaults.install if install is None else install
    reload = cfg.defaults.reload if reload is None else reload

    print_summary(cfg, prof, {
        "Servidor": f"http://{prof.host}:{prof.port}  reload={'sí' if reload else 'no'}  log={cfg.defaults.logging_level}",
        "Modo": "segundo plano" if background else "primer plano",
        "Instalar": "poetry lock && poetry install" if install else "no",
    })
    confirm_protected(cfg, prof, yes)
    if install:
        poetry_install(prof.service)
    else:
        try:
            runner.ensure_poetry()
        except RuntimeError as exc:
            fail(str(exc))

    cmd = runner.uvicorn_command(prof.host, prof.port, reload)
    env = runner.build_env(cfg.defaults, prof.user, prof.db)
    if not background:
        console.rule(f"uvicorn :{prof.port}")
        runner.exec_server(prof.service, cmd, env)

    inst = instances.start(
        prof.service, cmd, env, host=prof.host, port=prof.port, user=prof.user_name, db=prof.db_name, reload=reload
    )
    wait_until_ready(inst)


def wait_until_ready(inst: instances.Instance, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    with console.status(f"Arrancando {inst.key}..."):
        while time.monotonic() < deadline:
            health = instances.health(inst)
            if health.state == "stopped":
                instances.forget(inst.key)
                console.print(instances.tail(inst.log, 30), markup=False, highlight=False)
                fail(f"{inst.key} terminó al arrancar. Log completo: {inst.log}")
            if health.state == "ok":
                console.print(f"[green]✓[/] {inst.key} responde en http://localhost:{inst.port} (pid {inst.pid})")
                break
            if health.state == "error":
                console.print(instances.tail(inst.log, 30), markup=False, highlight=False)
                console.print(f"[yellow]⚠ {inst.key} arrancó con errores: {health.detail}[/]", highlight=False)
                if inst.reload:
                    console.print("  Sigue vivo: al guardar el arreglo, --reload lo recargará solo.")
                break
            time.sleep(0.5)
        else:
            console.print(f"[yellow]⚠ {inst.key} no responde tras {timeout:.0f}s; mira los logs.[/]")
    console.print(f"  Logs: [bold]pdms logs {inst.key}[/]   Parar: [bold]pdms stop {inst.key}[/]")


@app.command()
def run(
    service: Optional[str] = typer.Argument(None, help="Servicio (nombre o ruta relativa a la carpeta backend)."),
    user: Optional[str] = typer.Option(None, "--user", "-u", help="Alias del usuario de desarrollo."),
    db: Optional[str] = typer.Option(None, "--db", "-d", help="Alias de la base de datos."),
    port: Optional[int] = typer.Option(None, "--port", "-p"),
    host: Optional[str] = typer.Option(None, "--host"),
    install: Optional[bool] = typer.Option(None, "--install/--no-install", "-i/-n", help="poetry lock && poetry install."),
    reload: Optional[bool] = typer.Option(None, "--reload/--no-reload"),
    background: Optional[bool] = typer.Option(None, "--background/--foreground", "-b/-f", help="Segundo o primer plano."),
    yes: bool = typer.Option(False, "--yes", "-y", help="No pedir confirmación para DBs protegidas."),
    path: Optional[Path] = typer.Option(None, "--path", "-C", help="Carpeta del servicio (por defecto la actual)."),
) -> None:
    """Instala dependencias y levanta el servicio con uvicorn."""
    do_run(service, user, db, port, host, install, reload, yes, path, background)


# --------------------------------------------------------------------------- debugging


@app.command()
def debug(
    service: Optional[str] = typer.Argument(None, help="Servicio (nombre o ruta relativa a la carpeta backend)."),
    user: Optional[str] = typer.Option(None, "--user", "-u", help="Alias del usuario de desarrollo."),
    db: Optional[str] = typer.Option(None, "--db", "-d", help="Alias de la base de datos."),
    port: Optional[int] = typer.Option(None, "--port", "-p"),
    host: Optional[str] = typer.Option(None, "--host"),
    install: Optional[bool] = typer.Option(None, "--install/--no-install", "-i/-n", help="poetry lock && poetry install."),
    yes: bool = typer.Option(False, "--yes", "-y", help="No pedir confirmación para DBs protegidas."),
    path: Optional[Path] = typer.Option(None, "--path", "-C", help="Carpeta del servicio (por defecto la actual)."),
) -> None:
    """Crea/actualiza la configuración de VS Code (launch.json) para depurar el servicio con breakpoints."""
    cfg = Config.load()
    prof = choose_profile(cfg, service, path, user, db, port, host, yes)
    install = cfg.defaults.install if install is None else install
    print_summary(cfg, prof, {
        "Depurar": f"http://{prof.host}:{prof.port} (sin --reload, para que los breakpoints funcionen)",
        "Instalar": "poetry lock && poetry install" if install else "no",
    })
    confirm_protected(cfg, prof, yes)
    if install:
        poetry_install(prof.service)
    python = runner.poetry_python(prof.service)
    if not python:
        console.print("[yellow]El servicio aún no tiene virtualenv; instalo dependencias.[/]")
        poetry_install(prof.service)
        python = runner.poetry_python(prof.service) or fail("No pude localizar el python del virtualenv (poetry env info -e).")

    env_file = vscode.write_env_file(prof.service, runner.service_env(cfg.defaults, prof.user, prof.db))
    launch, name, backup = vscode.upsert_configuration(
        prof.service, python=python, env_file=env_file, host=prof.host, port=prof.port,
        description=f"{prof.user_name} @ {prof.db_name} :{prof.port}",
    )
    console.print(f"[green]✓[/] Configuración [bold]{name}[/] guardada en {launch}")
    if backup:
        console.print(f"[yellow]⚠ launch.json tenía comentarios y se han perdido; copia del original en {backup}[/]")
    console.print(
        f"  Variables (con la contraseña) en {env_file} [dim](fuera del repo, permisos 600)[/]\n"
        "  En VS Code: Run and Debug (Ctrl+Shift+D) → elige la configuración → F5.\n"
        "  [dim]Ojo: las librerías de common/ se instalan como copia (develop = false); para parar en ellas pon el\n"
        "  breakpoint en la copia de .venv/lib/.../site-packages, o entra con F11 desde el servicio.[/]"
    )


@app.command()
def env(
    user: Optional[str] = typer.Option(None, "--user", "-u", help="Alias del usuario de desarrollo."),
    db: Optional[str] = typer.Option(None, "--db", "-d", help="Alias de la base de datos."),
    dotenv: bool = typer.Option(False, "--dotenv", help="Formato .env (KEY=\"valor\") en lugar de export."),
) -> None:
    """Imprime las variables de un perfil. Uso: eval "$(pdms env -u supervisor -d local)"."""
    cfg = Config.load()
    if not sys.stdout.isatty():
        # Inside $(...) prompts would end up in the captured output: fall back to the last choice.
        user, db = user or cfg.last_user or None, db or cfg.last_db or None
    user_name = pick(cfg.users, "usuario", user, cfg.last_user)
    db_name = pick(cfg.dbs, "base de datos", db, cfg.last_db)
    variables = runner.service_env(cfg.defaults, cfg.users[user_name], cfg.dbs[db_name])
    for key, value in variables.items():
        print(f'{key}="{value}"' if dotenv else f"export {key}={shlex.quote(value)}")


# --------------------------------------------------------------------------- background instances


STATUS_TEXT = {
    "ok": "[green]● ok[/]",
    "starting": "[cyan]… arrancando[/]",
    "error": "[yellow]⚠ error[/]",
    "stopped": "[red]✗ parado[/]",
}


def uptime(started_at: str) -> str:
    seconds = int((datetime.now() - datetime.fromisoformat(started_at)).total_seconds())
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m" if hours else f"{rest // 60}m{rest % 60:02d}s"


def pick_instance(key: Optional[str], only_alive: bool = False, message: str = "Elige instancia:") -> instances.Instance:
    items = [i for i in instances.load().values() if i.alive() or not only_alive]
    if not items:
        fail("No hay servicios en segundo plano. Levanta uno con [bold]pdms run -b[/].")
    if key:
        exact = [i for i in items if i.key == key]
        items = exact or [i for i in items if i.key.startswith(key) or key in i.service]
        if not items:
            fail(f"Ninguna instancia coincide con '{key}'. Mira [bold]pdms ps[/].")
    if len(items) == 1:
        return items[0]
    prompts.require_tty()
    choices = [questionary.Choice(f"{i.key}  ({'corriendo' if i.alive() else 'parado'})", i) for i in items]
    return questionary.select(message, choices=choices).unsafe_ask()


@app.command()
def ps(clean: bool = typer.Option(False, "--clean", help="Olvida las instancias paradas.")) -> None:
    """Lista los servicios en segundo plano."""
    items = instances.load()
    if clean:
        for inst in [i for i in items.values() if not i.alive()]:
            instances.forget(inst.key)
            items.pop(inst.key)
    if not items:
        console.print("No hay servicios en segundo plano.")
        return
    healths = instances.health_all(list(items.values()))
    table = Table("Instancia", "Estado", "URL", "PID", "Usuario", "DB", "Tiempo")
    table.columns[0].no_wrap = table.columns[1].no_wrap = table.columns[2].no_wrap = True
    for inst in items.values():
        state = healths[inst.key].state
        table.add_row(
            inst.key, STATUS_TEXT[state], f"http://localhost:{inst.port}", str(inst.pid),
            inst.user, inst.db, uptime(inst.started_at) if state != "stopped" else "",
        )
    console.print(table)
    for key, health in healths.items():
        if health.state == "error":
            console.print(f"[yellow]⚠ {key}:[/] {health.detail}  [dim](pdms logs {key})[/]", highlight=False)
    if any(h.state == "stopped" for h in healths.values()):
        console.print("[dim]Las paradas conservan su log (pdms logs <instancia>). Bórralas con pdms ps --clean.[/]")


@app.command()
def logs(
    key: Optional[str] = typer.Argument(None, help="Instancia (o parte del nombre del servicio)."),
    follow: bool = typer.Option(True, "--follow/--no-follow", "-F/-N", help="Seguir la salida en vivo."),
    lines: int = typer.Option(100, "--lines", "-l", help="Líneas previas a mostrar."),
) -> None:
    """Muestra la consola de un servicio en segundo plano (Ctrl+C para salir)."""
    inst = pick_instance(key, message="¿De qué instancia quieres ver los logs?")
    if not follow:
        console.print(instances.tail(inst.log, lines), markup=False, highlight=False, end="")
        return
    console.rule(f"{inst.key} · {inst.log} · Ctrl+C para salir")
    try:
        subprocess.run(["tail", "-n", str(lines), "-F", inst.log])
    except KeyboardInterrupt:
        console.print()


@app.command()
def stop(
    key: Optional[str] = typer.Argument(None, help="Instancia (o parte del nombre del servicio)."),
    all_: bool = typer.Option(False, "--all", "-a", help="Parar todas."),
) -> None:
    """Para servicios en segundo plano."""
    running = [i for i in instances.load().values() if i.alive()]
    if all_:
        targets = running
    elif key:
        targets = [pick_instance(key)]
    elif len(running) <= 1:
        targets = [pick_instance(None, only_alive=True)]
    else:
        prompts.require_tty()
        targets = questionary.checkbox(
            "¿Qué instancias quieres parar? (espacio para marcar)",
            choices=[questionary.Choice(i.key, i) for i in running],
        ).unsafe_ask()
    if not targets:
        console.print("No hay nada que parar.")
    for inst in targets:
        with console.status(f"Parando {inst.key}..."):
            instances.stop(inst)
        console.print(f"[green]✓[/] {inst.key} parado.")


@app.command()
def restart(
    key: Optional[str] = typer.Argument(None, help="Instancia (o parte del nombre del servicio)."),
    user: Optional[str] = typer.Option(None, "--user", "-u", help="Cambiar a este usuario."),
    db: Optional[str] = typer.Option(None, "--db", "-d", help="Cambiar a esta base de datos."),
    change: bool = typer.Option(False, "--change", "-c", help="Preguntar qué usuario y DB usar."),
    install: Optional[bool] = typer.Option(None, "--install/--no-install", "-i/-n", help="poetry lock && poetry install."),
) -> None:
    """Reinicia un servicio en segundo plano (mismo puerto; por defecto mismo usuario y DB)."""
    inst = pick_instance(key, message="¿Qué instancia quieres reiniciar?")
    cfg = Config.load()
    if change:
        prompts.require_tty()
        user = user or prompts.select_name("Usuario:", list(cfg.users), inst.user)
        db = db or prompts.select_name("Base de datos:", list(cfg.dbs), inst.db)
    user, db = user or inst.user, db or inst.db
    pick(cfg.users, "usuario", user)
    pick(cfg.dbs, "base de datos", db)
    # Confirm before stopping: a "no" must leave the instance running.
    database = cfg.dbs[db]
    if database.protected and db != inst.db:
        confirm_protected(cfg, Profile(Path(inst.service), user, cfg.users[user], db, database, inst.host, inst.port), False)
    with console.status(f"Parando {inst.key}..."):
        instances.stop(inst)
    do_run(
        user=user, db=db, port=inst.port, host=inst.host, install=install, reload=inst.reload,
        yes=True, path=Path(inst.service), background=True,
    )


# --------------------------------------------------------------------------- stacks


def print_stacks(cfg: Config) -> None:
    if not cfg.stacks:
        console.print("[yellow]No hay stacks.[/] Crea uno con [bold]pdms stack add[/].")
        return
    running = running_by_service()
    root = services_root(cfg)
    table = Table("Stack", "Servicios", "Usuario", "DB")
    for name, stack in cfg.stacks.items():
        lines = []
        for svc in stack.services:
            ports = running.get(str(root / svc), []) if root else []
            lines.append(f"{svc} [green]{' '.join(f':{p}' for p in ports)}[/]" if ports else svc)
        table.add_row(name, "\n".join(lines), stack.user or "[dim]preguntar[/]", stack.db or "[dim]preguntar[/]")
    console.print(table)


def ask_stack(cfg: Config, current: Optional[Stack] = None) -> Stack:
    prompts.require_tty()
    root, candidates = list_services(cfg)
    services = list(current.services) if current else []
    if services:
        services = questionary.checkbox(
            "Servicios del stack (desmarca para quitar):",
            choices=[questionary.Choice(s, s, checked=True) for s in services],
        ).unsafe_ask()
    while not services or questionary.confirm(
        f"¿Añadir otro servicio? (tiene {len(services)})", default=not services
    ).unsafe_ask():
        remaining = [c for c in candidates if label(c, root) not in services]
        services.append(label(choose_service(remaining, root, "Servicio a añadir"), root))
    ask = "(preguntar al levantar)"
    user = questionary.select(
        "Usuario del stack:", choices=[ask, *cfg.users], default=(current.user if current and current.user else ask)
    ).unsafe_ask()
    db = questionary.select(
        "Base de datos del stack:", choices=[ask, *cfg.dbs], default=(current.db if current and current.db else ask)
    ).unsafe_ask()
    return Stack(services=services, user="" if user == ask else user, db="" if db == ask else db)


@stack_app.callback()
def stack_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        stack_menu()


@stack_app.command("list")
def stack_list() -> None:
    """Lista los stacks."""
    print_stacks(Config.load())


@stack_app.command("add")
def stack_add() -> None:
    """Crea un stack (asistente)."""
    cfg = Config.load()
    prompts.require_tty()
    name = prompts.ask_name("stack", cfg.stacks)
    cfg.stacks[name] = ask_stack(cfg)
    cfg.save()
    console.print(f"[green]✓[/] Stack '{name}' guardado. Levántalo con [bold]pdms up {name}[/].")


@stack_app.command("edit")
def stack_edit(name: Optional[str] = typer.Argument(None)) -> None:
    """Edita un stack."""
    cfg = Config.load()
    name = pick(cfg.stacks, "stack", name)
    cfg.stacks[name] = ask_stack(cfg, cfg.stacks[name])
    cfg.save()
    console.print(f"[green]✓[/] Stack '{name}' actualizado.")


@stack_app.command("remove")
def stack_remove(name: Optional[str] = typer.Argument(None)) -> None:
    """Elimina un stack."""
    cfg = Config.load()
    name = pick(cfg.stacks, "stack", name)
    if questionary.confirm(f"¿Eliminar el stack '{name}'?", default=False).unsafe_ask():
        del cfg.stacks[name]
        cfg.save()
        console.print(f"[green]✓[/] '{name}' eliminado.")


def stack_paths(cfg: Config, stack: Stack) -> list[Path]:
    root = services_root(cfg) or fail("Configura la carpeta backend con [bold]pdms config[/] para usar stacks.")
    paths = []
    for svc in stack.services:
        path = root / svc
        if not runner.is_service(path):
            fail(f"'{svc}' ya no es un servicio en {root}. Edita el stack con [bold]pdms stack edit[/].")
        paths.append(path)
    return paths


@app.command()
def up(
    name: Optional[str] = typer.Argument(None, help="Stack a levantar."),
    user: Optional[str] = typer.Option(None, "--user", "-u", help="Usuario (por defecto el del stack)."),
    db: Optional[str] = typer.Option(None, "--db", "-d", help="Base de datos (por defecto la del stack)."),
    install: Optional[bool] = typer.Option(None, "--install/--no-install", "-i/-n", help="poetry lock && poetry install."),
    yes: bool = typer.Option(False, "--yes", "-y", help="No pedir confirmación para DBs protegidas."),
) -> None:
    """Levanta en segundo plano todos los servicios de un stack, cada uno en un puerto libre."""
    cfg = Config.load()
    name = pick(cfg.stacks, "stack", name)
    stack = cfg.stacks[name]
    paths = stack_paths(cfg, stack)
    user_name = pick(cfg.users, "usuario", user or stack.user or None, cfg.last_user)
    db_name = pick(cfg.dbs, "base de datos", db or stack.db or None, cfg.last_db)
    install = cfg.defaults.install if install is None else install
    host = cfg.defaults.host

    running = running_by_service()
    pending = [p for p in paths if str(p) not in running]
    for path in paths:
        if str(path) in running:
            console.print(f"[dim]· {path.name} ya corre en :{', :'.join(map(str, running[str(path)]))}, lo salto.[/]")
    if not pending:
        console.print(f"[green]✓[/] Todo el stack '{name}' está corriendo.")
        return

    taken = set(instances.running_ports())
    plan = []
    port = cfg.defaults.port
    for path in pending:
        port = runner.next_free_port(host, port, taken)
        taken.add(port)
        plan.append((path, port))

    first = Profile(plan[0][0], user_name, cfg.users[user_name], db_name, cfg.dbs[db_name], host, plan[0][1])
    print_summary(cfg, first, {
        "Servicios": "\n".join(f"{p.name} → :{port}" for p, port in plan),
        "Instalar": "poetry lock && poetry install (cada uno)" if install else "no",
    }, show_service=False)
    confirm_protected(cfg, first, yes)

    started = []
    for path, port in plan:
        if install:
            poetry_install(path)
        env = runner.build_env(cfg.defaults, cfg.users[user_name], cfg.dbs[db_name])
        cmd = runner.uvicorn_command(host, port, cfg.defaults.reload)
        started.append(instances.start(
            path, cmd, env, host=host, port=port, user=user_name, db=db_name, reload=cfg.defaults.reload
        ))
    failed = 0
    for inst in started:
        try:
            wait_until_ready(inst)
        except typer.Exit:
            failed += 1
    if failed:
        fail(f"{failed} de {len(started)} servicios del stack '{name}' no arrancaron.")


@app.command()
def down(name: Optional[str] = typer.Argument(None, help="Stack a parar.")) -> None:
    """Para todos los servicios de un stack."""
    cfg = Config.load()
    name = pick(cfg.stacks, "stack", name)
    paths = {str(p) for p in stack_paths(cfg, cfg.stacks[name])}
    targets = [i for i in instances.load().values() if i.service in paths and i.alive()]
    if not targets:
        console.print(f"Nada del stack '{name}' está corriendo.")
    for inst in targets:
        with console.status(f"Parando {inst.key}..."):
            instances.stop(inst)
        console.print(f"[green]✓[/] {inst.key} parado.")


# --------------------------------------------------------------------------- db commands


@db_app.callback()
def db_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        db_menu()


@db_app.command("list")
def db_list() -> None:
    """Lista las bases de datos."""
    print_dbs(Config.load())


@db_app.command("add")
def db_add() -> None:
    """Añade una base de datos (asistente)."""
    add_db(Config.load())


@db_app.command("edit")
def db_edit(name: Optional[str] = typer.Argument(None)) -> None:
    """Edita una base de datos."""
    cfg = Config.load()
    prompts.require_tty()
    name = pick(cfg.dbs, "base de datos", name)
    cfg.dbs[name] = prompts.ask_database(cfg.dbs[name])
    cfg.save()
    console.print(f"[green]✓[/] Base de datos '{name}' actualizada.")


@db_app.command("remove")
def db_remove(name: Optional[str] = typer.Argument(None)) -> None:
    """Elimina una base de datos."""
    cfg = Config.load()
    name = pick(cfg.dbs, "base de datos", name)
    if questionary.confirm(f"¿Eliminar '{name}'?", default=False).unsafe_ask():
        del cfg.dbs[name]
        cfg.save()
        console.print(f"[green]✓[/] '{name}' eliminada.")


@db_app.command("test")
def db_test(
    name: Optional[str] = typer.Argument(None, help="Vacío = probar todas."),
    timeout: Optional[int] = typer.Option(None, "--timeout", "-t", help="Segundos de espera (por defecto el de pdms config)."),
) -> None:
    """Prueba la conexión a una o todas las bases de datos."""
    cfg = Config.load()
    targets = [pick(cfg.dbs, "base de datos", name)] if name else list(cfg.dbs)
    if not targets:
        print_dbs(cfg)
    ok = all([check_db(n, cfg.dbs[n], timeout or cfg.defaults.db_timeout) for n in targets])
    raise typer.Exit(0 if ok else 1)


# --------------------------------------------------------------------------- user commands


@user_app.callback()
def user_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        user_menu()


@user_app.command("list")
def user_list() -> None:
    """Lista los usuarios."""
    print_users(Config.load())


@user_app.command("add")
def user_add() -> None:
    """Añade un usuario (asistente)."""
    add_user(Config.load())


@user_app.command("edit")
def user_edit(name: Optional[str] = typer.Argument(None)) -> None:
    """Edita un usuario."""
    cfg = Config.load()
    prompts.require_tty()
    name = pick(cfg.users, "usuario", name)
    cfg.users[name] = prompts.ask_user(cfg.users[name])
    cfg.save()
    console.print(f"[green]✓[/] Usuario '{name}' actualizado.")


@user_app.command("remove")
def user_remove(name: Optional[str] = typer.Argument(None)) -> None:
    """Elimina un usuario."""
    cfg = Config.load()
    name = pick(cfg.users, "usuario", name)
    if questionary.confirm(f"¿Eliminar '{name}'?", default=False).unsafe_ask():
        del cfg.users[name]
        cfg.save()
        console.print(f"[green]✓[/] '{name}' eliminado.")


# --------------------------------------------------------------------------- config commands


@config_app.callback()
def config_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        config_defaults()


@config_app.command("defaults")
def config_defaults() -> None:
    """Edita los valores por defecto (puerto, log, reload, env extra...)."""
    prompts.require_tty()
    cfg = Config.load()
    cfg.defaults = prompts.ask_defaults(cfg.defaults)
    cfg.save()
    console.print("[green]✓[/] Valores por defecto guardados.")


@config_app.command("path")
def config_show_path() -> None:
    """Muestra la ruta del fichero de configuración."""
    console.print(str(config_path()))


@config_app.command("edit")
def config_edit() -> None:
    """Abre el fichero de configuración en $EDITOR."""
    cfg = Config.load()
    if not config_path().exists():
        cfg.save()
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        subprocess.run([*shlex.split(editor), str(config_path())])
    else:
        typer.launch(str(config_path()))


# --------------------------------------------------------------------------- interactive menus


def _menu(title: str, options: dict[str, Callable[[], None]]) -> None:
    back = "← Volver"
    while True:
        choice = questionary.select(title, choices=[*options, back]).unsafe_ask()
        if choice == back:
            return
        try:
            options[choice]()
        except typer.Exit:
            pass
        console.print()


def db_menu() -> None:
    prompts.require_tty()
    _menu("Bases de datos:", {
        "Listar": db_list,
        "Añadir": db_add,
        "Editar": lambda: db_edit(None),
        "Probar conexión": lambda: db_test(None, None),
        "Eliminar": lambda: db_remove(None),
    })


def user_menu() -> None:
    prompts.require_tty()
    _menu("Usuarios:", {
        "Listar": user_list,
        "Añadir": user_add,
        "Editar": lambda: user_edit(None),
        "Eliminar": lambda: user_remove(None),
    })


def instances_menu() -> None:
    prompts.require_tty()
    _menu("Servicios en segundo plano:", {
        "Listar": lambda: ps(False),
        "Ver logs (consola)": lambda: logs(None, True, 100),
        "Parar": lambda: stop(None, False),
        "Reiniciar": lambda: restart(None, None, None, False, None),
        "Reiniciar cambiando usuario/DB": lambda: restart(None, None, None, True, None),
        "Parar todos": lambda: stop(None, True),
    })


def stack_menu() -> None:
    prompts.require_tty()
    _menu("Stacks:", {
        "Levantar stack": lambda: up(None, None, None, None, False),
        "Parar stack": lambda: down(None),
        "Listar": stack_list,
        "Crear": stack_add,
        "Editar": lambda: stack_edit(None),
        "Eliminar": lambda: stack_remove(None),
    })


def main_menu() -> None:
    prompts.require_tty()
    cfg = Config.load()
    if not config_path().exists():
        cfg.save()
        console.print(f"[dim]Configuración creada en {config_path()}[/]")
    if instances.running_ports():
        ps(False)
        console.print()
    while True:
        running = len(instances.running_ports())
        choice = questionary.select(
            "¿Qué quieres hacer?",
            choices=[
                questionary.Choice("▶  Levantar servicio", "run"),
                questionary.Choice(f"📋 Servicios en segundo plano ({running} corriendo)", "ps"),
                questionary.Choice("🧩 Stacks (grupos de servicios)", "stack"),
                questionary.Choice("🗄  Bases de datos", "db"),
                questionary.Choice("👤 Usuarios", "user"),
                questionary.Choice("⚙  Valores por defecto", "defaults"),
                questionary.Choice("✕  Salir", "exit"),
            ],
        ).unsafe_ask()
        if choice == "exit":
            return
        actions = {"run": do_run, "ps": instances_menu, "stack": stack_menu, "db": db_menu, "user": user_menu, "defaults": config_defaults}
        try:
            actions[choice]()  # a foreground do_run replaces the process
        except typer.Exit:
            pass


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        main_menu()


def _entrypoint() -> None:
    try:
        app()
    except KeyboardInterrupt:
        console.print("\n[dim]Cancelado.[/]")
        sys.exit(130)
