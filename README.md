# pdms-cli

Interactive CLI to run PDMS services locally. Pick a development user and a database, and `pdms` runs
`poetry lock && poetry install` and starts `uvicorn main:app` with `DEV_*`, `DEVELOPMENT_MODE`, `LOGGING_LEVEL` and
`DB_PG_CONNECTION_STR` already set. It can run several services in the background, show their logs, detect broken
reloads, start groups of services together and generate VS Code debug configurations.

## Installation

```bash
git clone git@github.com:lianabeatriz93/pdms-cli.git ~/Code/Alivi/pdms-cli
uv tool install -e ~/Code/Alivi/pdms-cli
```

It is an editable install (`-e`), so changes in the repo apply without reinstalling.

First steps:

```bash
pdms config          # settings: language, backend folder (e.g. ~/Code/Alivi/pdms/backend), default port...
pdms db add          # wizard: alias, host, port, database, user, password
pdms user add        # wizard: DEV_USER_ID, DEV_USERNAME, DEV_ROLES...
pdms                 # interactive menu
```

## Language

English is the default. Spanish is also available:

```bash
pdms config language es    # or: pdms config language (asks)
pdms config language en
PDMS_LANG=es pdms ps       # one-off override
```

It is stored as `language` in the `[defaults]` section of the config.

## Running a service

```bash
cd ~/Code/Alivi/pdms/backend/lead/lead-tp-create
pdms run                                  # asks for user, DB, port and foreground/background
pdms run -u supervisor -d local -p 8081 -n
pdms run lead-tp-create -b                # by name (or lead/lead-tp-create) from anywhere
pdms run -C backend/lead/lead-tp-create
```

`pdms run` finds the service by walking up from the current folder. Outside a service, it lists the services of the
configured backend folder (or below the current folder) with autocompletion; a unique partial name is enough.
It remembers the last user and DB, proposes the next free port, and asks for confirmation before using a database
marked as protected.

Options of `pdms run [SERVICE]`: `-u/--user`, `-d/--db`, `-p/--port`, `--host`, `-b/--background` / `-f/--foreground`,
`-i/--install` / `-n/--no-install`, `--reload/--no-reload`, `-y/--yes` (no confirmation for protected DBs),
`-C/--path`.

### Why always `poetry lock && poetry install`?

Services depend on the `backend/common/*` libraries with `develop = false`, so a change in `common/` does not reach
the service until it is reinstalled. If nothing changed, use `-n` to start faster, or set `install = false` in
`pdms config`.

## Background services

```bash
pdms services tp-                  # services in the backend folder and the ports they are running on
pdms run lead-tp-create -b
pdms run lead-tp-details -b -p 8081
pdms ps                            # status, URL, user, DB and uptime of each instance
pdms logs lead-tp-create           # live console (Ctrl+C to exit); --no-follow to print and exit
pdms restart lead-tp-create        # same user, DB and port
pdms restart lead-tp-create -u agent   # switch user (-d for DB, -c to pick interactively)
pdms stop                          # pick which ones to stop; pdms stop --all
```

Each instance is identified as `service@port`, runs in its own process group (stopping it also stops the uvicorn
reloader) and writes its output to `~/.local/state/pdms/logs/<service@port>.log`. The registry lives in
`~/.local/state/pdms/instances.json`. Stopped instances keep their log until `pdms ps --clean`. When there are
background services, the interactive menu shows them on entry.

`pdms ps` sends an HTTP request to each instance, because with `--reload` uvicorn stays alive even when the app
fails to load:

| Status | Meaning |
| --- | --- |
| `● ok` | The app responds |
| `… starting` | Process alive, not responding yet |
| `⚠ error` | The last load failed; the exception is shown (e.g. `ModuleNotFoundError: ...`). Saving the fix reloads it |
| `✗ stopped` | The process exited; its log is kept |

## Stacks: several services at once

```bash
pdms stack add          # wizard: name, services, fixed user/DB or "ask when starting"
pdms stack list
pdms up tp              # starts the services that are not running, each on a free port
pdms up tp -u agent -d web
pdms down tp
```

## Debugging in VS Code

```bash
pdms debug lead-tp-create -u supervisor -d local -p 8090
```

Adds (or updates) the configuration `pdms: lead-tp-create · supervisor @ local :8090` in the repo's
`.vscode/launch.json`, using the service's virtualenv python and no `--reload` so breakpoints work. Then:
Run and Debug (Ctrl+Shift+D) → pick the configuration → F5.

The variables (including the password) are **not** written to `launch.json` but to an `envFile` in
`~/.local/state/pdms/env/<service>.env` (permissions `0600`), because `.vscode` is not in the repo's `.gitignore`.
If `launch.json` had comments they are lost when it is rewritten; a copy is kept as `launch.json.bak`.

The `backend/common/*` libraries are installed as a copy (`develop = false`): to stop inside them, set the breakpoint
in the copy under `.venv/lib/python3.*/site-packages/...` or step in with F11 from the service.

`pdms env -u supervisor -d local` prints the variables (`eval "$(pdms env ...)"`, or `--dotenv` for `.env` format).

## Command reference

| Command | What it does |
| --- | --- |
| `pdms` | Interactive menu |
| `pdms run` / `pdms debug` / `pdms env` | Run a service / create a VS Code debug configuration / print a profile's variables |
| `pdms services` | List the services of the backend folder |
| `pdms ps` / `logs` / `stop` / `restart` | Manage background instances |
| `pdms up` / `pdms down` | Start / stop a stack |
| `pdms stack` | Stacks menu (`list`, `add`, `edit`, `remove`) |
| `pdms db` | Databases menu (`list`, `add`, `edit`, `remove`, `test`) |
| `pdms user` | Users menu (`list`, `add`, `edit`, `remove`) |
| `pdms config` | Settings (`defaults`, `language`, `path`, `edit`) |

`pdms <command> --help` shows every option.

## Configuration

Stored in `~/.config/pdms/config.toml` (or wherever `PDMS_CONFIG` points) with permissions `0600`, since it holds
the database passwords. Never commit it.

```toml
[defaults]
language = "en"                           # "en" or "es"
host = "0.0.0.0"
port = 8080
logging_level = "DEBUG"
reload = true
install = true
backend_path = "~/Code/Alivi/pdms/backend"
db_timeout = 15                           # seconds for `pdms db test` (or `pdms db test -t 30`)

[defaults.env]
# extra variables injected on every run

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
protected = false                         # true = ask for confirmation before using it

[stacks.tp]
services = ["lead/lead-tp-list", "lead/lead-tp-details"]
user = ""                                 # empty = ask when starting
db = ""
```

## Development

```bash
uv sync            # creates .venv with the dev dependencies
uv run pytest
```

User-facing texts are written in English inside `_()` (from `pdms_cli.i18n`) and translated in the `ES` catalog of
[`src/pdms_cli/i18n.py`](src/pdms_cli/i18n.py). `tests/test_i18n.py` fails if a string has no Spanish translation,
if placeholders differ between languages, or if Spanish text is left outside the catalog.
