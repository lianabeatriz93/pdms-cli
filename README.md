# pdms-cli

CLI interactivo para levantar servicios de PDMS en local: elige un usuario de desarrollo y una base de datos,
y el CLI hace `poetry lock && poetry install` y arranca `uvicorn main:app` con las variables `DEV_*`,
`DEVELOPMENT_MODE`, `LOGGING_LEVEL` y `DB_PG_CONNECTION_STR` ya puestas.

## Instalación

```bash
git clone git@github.com:lianabeatriz93/pdms-cli.git ~/Code/Alivi/pdms-cli
uv tool install -e ~/Code/Alivi/pdms-cli
```

Al ser una instalación editable (`-e`), los cambios del repo se aplican sin reinstalar.

## Uso

```bash
pdms                    # menú interactivo
cd backend/lead/lead-tp-create
pdms run                # pregunta usuario y DB (recuerda la última elección)
pdms run -u supervisor -d local -p 8081 --no-install
pdms run -C backend/lead/lead-tp-create
```

Desde la raíz del repo (o cualquier carpeta que no sea un servicio), `pdms run` busca los servicios que hay
debajo y deja elegir uno con autocompletado.

## Varios servicios en segundo plano

```bash
pdms config                        # configura la carpeta backend (p. ej. ~/Code/Alivi/pdms/backend)
pdms services tp-                  # lista los servicios de la carpeta backend (y en qué puerto corren)
pdms run lead-tp-create -b         # levanta en segundo plano (nombre o ruta relativa, p. ej. lead/lead-tp-create)
pdms run lead-tp-details -b -p 8081
pdms ps                            # instancias en segundo plano: estado, URL, usuario, DB, tiempo
pdms logs lead-tp-create           # la "consola" del servicio en vivo (Ctrl+C para salir); --no-follow para ver y salir
pdms restart lead-tp-create        # mismo usuario, DB y puerto
pdms stop                          # elige cuáles parar; pdms stop --all para todas
```

Sin `-b`/`-f`, `pdms run` pregunta si quieres primer o segundo plano. Cada instancia se identifica como
`servicio@puerto`, corre en su propio grupo de procesos (al pararla se para también el reloader de uvicorn) y
escribe su salida en `~/.local/state/pdms/logs/<servicio@puerto>.log`. El registro está en
`~/.local/state/pdms/instances.json`. Si un servicio muere al arrancar, `pdms run` muestra el final del log.
Las instancias paradas conservan su log hasta `pdms ps --clean`.

### Estado de los servicios

`pdms ps` hace una petición HTTP a cada instancia (con `--reload`, uvicorn sigue vivo aunque la app no cargue):

| Estado | Significado |
| --- | --- |
| `● ok` | La app responde |
| `… arrancando` | Proceso vivo, todavía sin responder |
| `⚠ error` | La última carga falló; se muestra la excepción (p. ej. `ModuleNotFoundError: ...`). Al guardar el arreglo, `--reload` la recarga |
| `✗ parado` | El proceso terminó; el log se conserva |

`pdms restart <instancia> -u agent` (o `-d`, o `-c` para elegir) reinicia en el mismo puerto con otro usuario/DB.

## Stacks: varios servicios a la vez

```bash
pdms stack add          # asistente: nombre, servicios, usuario/DB fijos o "preguntar al levantar"
pdms stack list
pdms up tp              # levanta en segundo plano los que no estén corriendo, cada uno en un puerto libre
pdms up tp -u agent -d web
pdms down tp
```

## Depurar en VS Code

```bash
pdms debug lead-tp-create -u supervisor -d local -p 8090
```

Añade (o actualiza) la configuración `pdms: lead-tp-create · supervisor @ local :8090` en el `.vscode/launch.json`
del repo, usando el python del virtualenv del servicio y sin `--reload` para que los breakpoints funcionen. Después:
Run and Debug (Ctrl+Shift+D) → elegir la configuración → F5.

Las variables (incluida la contraseña) **no** van en `launch.json` sino en un `envFile` en
`~/.local/state/pdms/env/<servicio>.env` (permisos `0600`), porque `.vscode` no está en el `.gitignore` del repo.
Si `launch.json` tenía comentarios se pierden al reescribirlo; queda una copia en `launch.json.bak`.

Las librerías de `backend/common/*` se instalan como copia (`develop = false`): para parar dentro de ellas pon el
breakpoint en la copia de `.venv/lib/python3.*/site-packages/...` o entra con F11 desde el servicio.

`pdms env -u supervisor -d local` imprime las variables (`eval "$(pdms env ...)"`, o `--dotenv` para formato `.env`).

| Comando | Qué hace |
| --- | --- |
| `pdms db` | Menú de bases de datos (`list`, `add`, `edit`, `remove`, `test`) |
| `pdms user` | Menú de usuarios (`list`, `add`, `edit`, `remove`) |
| `pdms config` | Valores por defecto: carpeta backend, puerto, host, log, reload, instalar, timeout del test de DB, variables extra |
| `pdms stack` | Menú de stacks (`list`, `add`, `edit`, `remove`); `pdms up` / `pdms down` para levantarlos |
| `pdms config path` / `pdms config edit` | Ruta del fichero / abrirlo en `$EDITOR` |

Opciones de `pdms run [SERVICIO]`: `-b/--background` / `-f/--foreground`, `-u/--user`, `-d/--db`, `-p/--port`, `--host`, `-i/--install` / `-n/--no-install`,
`--reload/--no-reload`, `-y/--yes` (no pedir confirmación en DBs protegidas), `-C/--path`.

## Configuración

Vive en `~/.config/pdms/config.toml` (o donde apunte `PDMS_CONFIG`) y se guarda con permisos `0600` porque
contiene las contraseñas de las DBs. No la subas a ningún repo.

```toml
[defaults]
host = "0.0.0.0"
port = 8080
logging_level = "DEBUG"
reload = true
install = true
backend_path = "~/Code/Alivi/pdms/backend"
db_timeout = 15   # segundos para `pdms db test` (o `pdms db test -t 30`)

[defaults.env]
# variables extra en cada ejecución

[users.supervisor]
user_id = "f3d55402-2dce-476f-aa93-890b2f4d61c4"
username = "supervisor.nemt1@gmail.com"
first_name = "Transportation"
last_name = "Supervisor"
roles = "TPR.Supervisor"

[dbs.local]
host = "localhost"
port = 5432
database = "pdm"
user = "postgres"
password = "..."
driver = "postgresql+psycopg2"
protected = false
```

Las DBs marcadas como `protected` (compartidas/remotas) piden confirmación antes de levantar el servidor.
Si el puerto está ocupado, el CLI propone el siguiente libre.

### ¿Por qué `poetry lock && poetry install` siempre?

Los servicios dependen de las librerías de `backend/common/*` con `develop = false`, así que un cambio en
`common/` no llega al servicio hasta reinstalar. Si sabes que no ha cambiado nada, usa `-n` para arrancar más rápido,
o pon `install = false` por defecto con `pdms config`.
