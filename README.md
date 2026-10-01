# pdms-cli

Interactive CLI to run PDMS services locally. Pick a development user and a database, and `pdms` runs
`poetry lock && poetry install` and starts `uvicorn main:app` with `DEV_*`, `DEVELOPMENT_MODE`, `LOGGING_LEVEL` and
`DB_PG_CONNECTION_STR` already set. It can run several services in the background, show their logs, detect broken
reloads, start groups of services together and generate VS Code debug configurations.

## Installation

macOS / Linux:

```sh
curl -LsSf https://github.com/lianabeatriz93/pdms-cli/releases/latest/download/install.sh | sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://github.com/lianabeatriz93/pdms-cli/releases/latest/download/install.ps1 | iex"
```

The installer sets up [uv](https://docs.astral.sh/uv/) if it is missing (uv also provides a suitable Python), installs
the latest release of `pdms`, adds it to the `PATH` and checks that it starts. Open a new terminal afterwards if it
says so. Both installers are also attached to every [release](https://github.com/lianabeatriz93/pdms-cli/releases),
and accept `PDMS_VERSION=0.2.0` to install a specific version.

Without the installer, with uv already installed:

```sh
uv tool install https://github.com/lianabeatriz93/pdms-cli/releases/download/v0.2.0/pdms_cli-0.2.0-py3-none-any.whl
```

| | |
| --- | --- |
| Check the version | `pdms --version` |
| Update | `pdms self-update` (`--check` only tells whether there is a new one; `--version X` for a specific one) |
| Pre-releases | Installers: `PDMS_PRERELEASE=1` (e.g. `curl ... \| PDMS_PRERELEASE=1 sh`, or `$env:PDMS_PRERELEASE = "1"` on Windows). `pdms self-update --pre`; once you run an alpha, `self-update` keeps following alphas |
| Uninstall | `uv tool uninstall pdms-cli` (the configuration in `~/.config/pdms` is kept) |

`pdms` tells you when a new version is published: it checks GitHub at most once a day, in the background while a
command runs, and shows `⬆ New pdms version available: … · update with: pdms self-update` when the command ends (or
when the menu opens), at most once a day. Stable installs are told about stable releases, alpha installs about alphas
too. It never checks without an interactive terminal, in CI, from an editable checkout, with
`PDMS_NO_UPDATE_CHECK=1`, or when disabled in `pdms config` (`update_check = false`).

### Platforms

Linux, macOS and Windows, with Python 3.10+ (tested in CI on the three systems with Python 3.10 and 3.12).
Running services also needs [Poetry](https://python-poetry.org/docs/#installation) in `PATH`, as PDMS uses it.

| | Linux / macOS | Windows |
| --- | --- | --- |
| Configuration | `~/.config/pdms/config.toml` | `%USERPROFILE%\.config\pdms\config.toml` |
| State (instances, logs, caches) | `~/.local/state/pdms/` | `%USERPROFILE%\.local\state\pdms\` |

`PDMS_CONFIG` and `XDG_STATE_HOME` override those locations. Background services are detached from the terminal and
stopped together with their child processes (the uvicorn reloader and its workers) on every system.

`pdms doctor` checks all of this at once: pdms and its Python, Poetry and a Python 3.10/3.11 for the services, the
configuration file and its permissions, every database connection (`--no-db` to skip them), the current repo and
what the proxy needs, the ports, background instances and tab completion. Each problem comes with a hint on how to
fix it; the exit code is 1 when something is broken.

First steps:

```bash
pdms repo add ~/Code/Alivi/pdms   # register your PDMS checkout (done automatically the first time you run pdms inside it)
pdms config          # settings: language, default port, install...
pdms db add          # wizard: alias, host, port, database, user, password
pdms user add        # wizard: DEV_USER_ID, DEV_USERNAME, DEV_ROLES...
pdms                 # interactive menu
```

### Tab completion

```bash
pdms --install-completion    # then open a new terminal
```

Completes service names, instances, users, databases, stacks and languages, e.g. `pdms run lead-tp-<Tab>`,
`pdms logs <Tab>`, `pdms up <Tab>`, `pdms run -u <Tab>`.

## Repos (several PDMS checkouts)

`pdms` can work with several checkouts of PDMS (e.g. `~/Code/Alivi/pdms` and `~/Code/Alivi/pdms_v2`). One of them is
the **current repo**: its backend is where services are listed, stacks are resolved and migrations run.

```bash
pdms repo list                       # ● marks the current one; also shows how many instances run from each
pdms repo add ~/Code/Alivi/pdms_v2   # alias defaults to the folder name
pdms repo use pdms_v2
pdms repo remove pdms                # only forgets it; nothing is deleted from disk
```

When you run `pdms` inside a registered or unregistered PDMS repo that is not the current one, it asks whether to
**switch** to it, use it **only for this command**, or **never ask again** in that repo. Without an interactive
terminal it uses the folder's repo for that command only. The first repo you use becomes the current one
automatically (an old `backend_path` setting is migrated to a repo).

Instances from different repos can run at the same time (`pdms ps` has a **Repo** column). When you switch repos and
instances of the previous one are running, `pdms` asks whether to keep them, stop them, or **restart them from the
new repo** with the same user, DB and port (services that do not exist in the new repo are left running).
Stacks store paths relative to the backend, so they work with any repo. Repos are not included in
`pdms config export`, since their paths are machine-specific.

## Language

English is the default. Spanish is also available:

```bash
pdms config language es    # or: pdms config language (asks)
pdms config language en
PDMS_LANG=es pdms ps       # one-off override
```

It is stored as `language` in the `[defaults]` section of the config.

### Importing users from a database

```bash
pdms user import -d local                 # search, then pick users with the space bar
pdms user import -d web -s ana -r TPR.Agent
pdms user import -d local --inactive -y   # every match, inactive ones included, without asking
```

Reads `public.pdms_user` (read-only) and creates a profile per user: `DEV_USER_ID` = `entity_id`, `DEV_USERNAME` =
`username`, names, and `DEV_ROLES` converted from the internal names stored in the table
(`TRANSPORTATION_PR_SUPERVISOR`) to the external ones the services expect (`TPR.Supervisor`). The mapping is read
from the current repo (`MAP_INTERNAL_ROLES` in `backend/common/core/core/settings.py` and the `UserRoleEnum` /
`UserRolePPEnum` enums, parsed without importing them), so new or renamed roles are picked up automatically; a
built-in copy is used only if the repo cannot be read. Aliases come from the email (`jane.doe@example.com` → `jane-doe`); users already imported
(same `DEV_USER_ID`) are updated in place instead of duplicated.

## Running a service

```bash
cd ~/Code/Alivi/pdms/backend/lead/lead-tp-create
pdms run                                  # asks for user, DB, port and foreground/background
pdms run -u supervisor -d local -p 8081 -n
pdms run lead-tp-create -b                # by name (or lead/lead-tp-create) from anywhere
pdms run -C backend/lead/lead-tp-create
```

`pdms run` finds the service by walking up from the current folder. Outside a service, it lists the services of the
current repo's backend folder (or below the current folder) with autocompletion; a unique partial name is enough.
It remembers the last user and DB, proposes the next free port, and asks for confirmation before using a database
marked as protected.

Options of `pdms run [SERVICE]`: `-u/--user`, `-d/--db`, `-p/--port`, `--host`, `-b/--background` / `-f/--foreground`,
`-i/--install` / `-n/--no-install` (force / skip the install), `--reload/--no-reload`, `-y/--yes` (no confirmation for protected DBs),
`-C/--path`.

### Smart install

Services depend on the `backend/common/*` libraries with `develop = false`, so a change in `common/` does not reach
the service until it is reinstalled. That is why `pdms` runs `poetry lock && poetry install` before starting, but only
when something that affects the install changed since the last successful one. The fingerprint covers:

- the service's `pyproject.toml` and `poetry.lock`;
- recursively, the whole source tree of its path dependencies installed as a copy (`develop = false`);
- only the `pyproject.toml` of editable path dependencies (`develop = true`), since their code is picked up live.

`-i` forces the install, `-n` skips it. `pdms config` can turn the smart check off (`smart_install = false`, always
install) or disable the install altogether (`install = false`). Fingerprints live in `~/.local/state/pdms/installs.json`.

## Background services

```bash
pdms services tp-                  # services of the current repo and the ports they are running on
pdms run lead-tp-create -b
pdms run lead-tp-details -b -p 8081
pdms ps                            # status, URL, user, DB and uptime of each instance
pdms logs lead-tp-create           # live console (Ctrl+C to exit); --no-follow to print and exit
pdms logs --all                    # every running instance merged, prefixed and colored per instance
pdms logs --stack tp               # the instances of a stack; or several: pdms logs lead-tp-list lead-tp-details
pdms logs lead-tp-create -p        # log of the previous run (kept as <log>.1 on every restart)
pdms urls                          # endpoints of every running instance: method, full URL and summary
pdms urls lead-tp-list -f health   # one instance, only paths containing 'health'
pdms open lead-tp-create           # Swagger (/docs) in the browser; --path /redoc for another page
pdms restart lead-tp-create        # same user, DB and port
pdms restart lead-tp-create -u agent   # switch user (-d for DB, -c to pick interactively)
pdms stop                          # pick which ones to stop; pdms stop --all (the proxy included)
```

When a background service starts, `pdms` also prints its endpoints (read live from its `/openapi.json`).

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

`pdms ps` also warns when the **installed code of a running instance is outdated**: `--reload` picks up changes of the
service and of editable (`develop = true`) libraries, but not of the `common/` libraries installed as a copy
(`develop = false`), nor a new `poetry.lock`. When any of them changed since the instance started, it says which ones
and suggests `pdms restart <instance>`, which reinstalls them automatically (smart install).

## Proxy: one port for every service

```bash
pdms up tp                 # start locally what you are working on
pdms proxy                 # http://localhost:8000 → local instances first, the remote API (dev) for the rest
pdms proxy --as agent      # act as another user on local services, without restarting them
pdms proxy -b              # in the background: pdms ps shows it, pdms logs proxy, pdms stop proxy
pdms proxy routes -f tp    # which service handles each route and where it would go now
```

- **Routes** come from the repo's Terraform (`infra/infra_auto/environments/dev/api_rsc_*.tf`, `--env` for others):
  every `METHOD /api/v1/...` is mapped to its service, preferring literal segments over `{params}` like API Gateway.
  They are cached until a `.tf` file changes.
- **Local first:** a request goes to the running instance of its service from the current repo (ports from
  `pdms ps`). Instances of the same service running from another repo are not used, to avoid mixing versions.
- **Remote fallback:** anything else is forwarded unchanged (headers, `Authorization`, body) to the repo's remote API,
  read once from `frontend/.env` (`VITE_APP_API_URL` + stage) and saved per repo; `--remote URL` sets another one and
  `--no-remote` answers `503` with the command to start the missing service. An API Gateway stage prefix
  (`/dev/api/v1/...`) is accepted too.
- **CORS** preflights are answered by the proxy (in AWS, API Gateway does it; the services have no CORS middleware).
- **`--as USER`** adds the `X-Dev-*` headers that services in `DEVELOPMENT_MODE` use instead of their `DEV_*`
  variables. They are only sent to local services, never to the remote API.
- **Frontend:** `pdms proxy` offers to write `VITE_APP_API_URL=http://localhost:8000` and
  `VITE_APP_API_URL_VERSION=api/v1` into `frontend/.env.local` (git-ignored, other lines are kept). Restart `yarn dev`.
  When the proxy stops, the file is put back as it was (if the proxy was killed, the next `pdms proxy` does it);
  values edited by hand in the meantime are left alone.
- **Docs:** `http://localhost:8000/docs` is a Swagger UI with a selector for every local service (live specs from
  their `/openapi.json`) plus an "All local services" view; "Try it out" goes through the proxy. Each service's own
  `http://localhost:<port>/docs` keeps working.
- Every request is logged with its status, target and time. Every response carries an `X-Pdms-Target` header.
- **Background:** `pdms proxy -b` keeps it running without a terminal (asked if you give neither `-b` nor `-f`).
  It shows up in `pdms ps` as `proxy@<port>`, its requests go to a log file (`pdms logs proxy`) and
  `pdms stop proxy` (or `pdms stop --all`) stops it and puts `frontend/.env.local` back.

## Web interface: `pdms ui` (preview)

```bash
pdms ui                    # opens http://127.0.0.1:8765 in the browser; Ctrl+C to stop it
pdms ui --no-browser -p 9000
```

A page with the background services, the proxy and the local events, updated live from the same files the CLI
uses, so both can be used at the same time. From the services screen you can follow a service's logs live (also the
previous run's and the install's), open its Swagger docs, stop it, forget the stopped ones, and restart it with the
same or another user and database: it asks before a protected database and installs only if something changed, like
`pdms restart`. The stacks screen starts (like `pdms up`) and stops (`pdms down`) a stack, shows which of its
services run, and creates, edits and deletes stacks with a searchable list of the repo's services. Starting single
new services and the proxy come in the next releases.

It only listens on `127.0.0.1`, and only the link `pdms ui` prints opens it: that link carries a random token for the
session, which the browser keeps as a cookie. Requests from other web pages are rejected, and database passwords are
never sent to the page.

## Events (local SQS)

PDMS services publish SQS events to the **broker** queue (`SQS_EVENT_BROKER_URL`); the `broker-sqs-event` Lambda
routes each one by its `type` to a destination queue, consumed by an event Lambda (`*-ev`). `pdms` reads that whole
map from the current repo — nothing has to be registered by hand:

| What | Where it comes from |
| --- | --- |
| Queues (name, FIFO, visibility timeout) | `aws_sqs_queue` in the repo's Terraform, resolving `var.*` defaults |
| Consumer of each queue and its handler | `aws_lambda_event_source_mapping` → the Lambda's `lambda_path` / `handler` |
| Queue of each event type | `EVENT_ROUTE_DEST` + `EventType` in `backend/common/event` and the broker Lambda's environment in Terraform |
| Queues of events without Terraform yet | extra queues in `infra/local_sqs/elasticmq.conf` |

```bash
pdms events map -f email   # event type → queue → consumer (and queues the broker does not route)
pdms events up             # local ElasticMQ (Docker) with every queue, at http://localhost:9324
pdms events status         # messages waiting / in flight per queue (--all for empty ones)
pdms events down
```

`pdms events up` generates the ElasticMQ configuration from that map and recreates the container only when the
queues change. The port is `events_port` in the configuration (9324 by default). Docker is required; `pdms doctor`
checks it.

### Publishing to the local broker

While `pdms events up` is running, every service started with `pdms run`, `pdms up`, `pdms restart` or `pdms debug`
publishes its events to the **local broker** — no `.env` to edit:

- `SQS_EVENT_BROKER_URL` points to the local broker queue, and the broker's destination variables
  (`SQS_EMAIL_NOTIFY`, …) to their local queues;
- a `sitecustomize.py` shipped with pdms is put on `PYTHONPATH`: Python loads it in every process of the service,
  including the ones uvicorn `--reload` spawns, and it sends the SQS clients created by botocore (1.29 in PDMS, which
  predates `AWS_ENDPOINT_URL_SQS`) to ElasticMQ with dummy credentials. Other AWS clients (S3, …) are not touched.

The `events` setting chooses the behaviour (`--events` overrides it per command): `auto` (default: local while the
local ElasticMQ runs, otherwise the service's own configuration), `local` (always; offers to start ElasticMQ) or
`aws`. The summary of every run shows where the service publishes, and `pdms events status` lists the instances
publishing locally. Queues are FIFO with content-based deduplication, like in AWS: the same message body sent twice
within 5 minutes is stored once.

### Broker and consumers

`pdms events up` also runs the **broker** (`broker-sqs-event`, with the last used user/DB; `--no-broker` to skip it),
so published events are routed to their queues exactly as in AWS. Event Lambdas run like any other service:

```bash
pdms run email-notify -b        # detected as the consumer of email-send-sqs-queue.fifo: no port, no uvicorn
pdms ps                         # email-notify@sqs · sqs ← email-send-sqs-queue.fifo
pdms logs --all                 # received / processed / failed messages of every consumer, next to the services
pdms events down                # stops the consumers, the broker and ElasticMQ
```

A service is a consumer when the repo's Terraform maps a queue to it. pdms then runs its own poller with the
service's Python: it long-polls the local queue and calls the Lambda `handler` from Terraform with
`{"Records": [...]}` and a Lambda-like context, deletes processed messages and honours `batchItemFailures`. A failed
message is retried after 5 seconds (not the queue's visibility timeout) and dropped with a warning after 3 attempts,
since there is no dead-letter queue locally. Consumers work with `ps`, `logs`, `stop`, `restart`, stacks and the
outdated-code warning; they always use the local ElasticMQ (and publish to the local broker), offering to start it.

### Sending and inspecting events

```bash
pdms events send email-notify --template             # the event's fields, from its class in backend/common/event
pdms events send email-notify -b '{"to_emails": ["a@x.com"]}'   # through the broker, like a publishing service
pdms events send email-notify --direct -f event.json # straight to its queue, skipping the broker
pdms events send demo-sqs-queue.fifo -b '{"raw": 1}' # a raw message to any queue
pdms events peek email-send-sqs-queue.fifo           # messages waiting, without consuming them
pdms events purge email-send-sqs-queue.fifo          # or --all
pdms debug email-notify                              # VS Code configuration running the consumer under the debugger
```

`send` with an event type builds the same message a service publishes (common/event's `Event` as JSON: `event_id`,
`type`, `app_context` and the given fields) and, by default, sends it to the broker queue. Every message gets its own
deduplication id, so sending the same test body twice is not swallowed by the FIFO deduplication. It warns when
nothing is consuming the target queue. `peek` counts as a receive in SQS, but the local poller counts its own
attempts, so peeking does not make a failing message be dropped sooner. The interactive menu has an **Events** entry
with all of this.

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

## Tests and migrations

```bash
pdms test lead-tp-create                 # poetry run pytest in the service (smart install first)
pdms test lead-tp-create -- -k create -x # extra arguments go to pytest
pdms test -u supervisor -d local         # also inject a profile's DEV_* / DB_PG_CONNECTION_STR
pdms migrate -d web-dev                  # flyway info: applied and pending migrations (read-only)
pdms migrate validate -d web-dev         # flyway validate, like the pipeline (pending ones are not errors)
pdms migrate migrate -d local            # apply them — only against a local database
```

`pdms migrate` runs [Flyway](https://flywaydb.org/) from the `pdms-db-migrations` repo (PDMP-467; the Alembic
migrations in `backend/common/sync-database` are frozen) with the same Docker image and arguments as the
CodePipeline (`flyway-postgres:11.20.2-1`, `-configFiles=flyway.toml,placeholder.toml -environment=local`), so no
Flyway install is needed — only Docker. `info` and `validate` work against any database; `migrate` only against a
local one (`localhost`, not protected), since shared databases are migrated by the pipeline; other Flyway commands
(`clean`, `repair`, …) are not allowed. The database password is passed to the container as an environment
variable, never on the command line.

The migrations checkout is found automatically and remembered per PDMS repo: the current folder if it is a Flyway
repo, the one saved for the repo, or a sibling of the PDMS checkout (`~/Code/Alivi/pdms-db-migrations`; with several,
the one whose suffix matches, e.g. `pdms_v2` ↔ `pdms-db-migrations-v2`). `--migrations PATH` sets another one.
`pdms doctor` and `pdms repo list` show it.

## Getting started: `pdms setup`

The first time you run `pdms` (with no configuration yet) it offers a guided setup. You can run it again at any time
with `pdms setup` (or **Settings → Guided setup**) to complete whatever is still missing.

1. **Import** — it first asks whether you have a configuration to import, such as the one a teammate exported with
   `pdms config export`. It suggests the `pdms-config*.toml` files it finds in the current folder, `~/Downloads` and
   your home, or you can pick another file. The import works as `pdms config import`.
2. **Then only what is missing**, whether the import happened or not:
   - the **language**, on a first setup when the import did not bring the defaults;
   - the **PDMS repo**: the checkout you are in, or a folder you enter;
   - the **migrations repo** (pdms-db-migrations), found next to the PDMS repo when possible;
   - **databases**: add one if there are none, and type the **passwords** that imported databases do not carry
     (exports leave them out by default), with an optional connection test;
   - **development users**: import them from the `pdms_user` table of a database, or add one by hand;
   - optionally a **stack**, and a review of the other defaults.
3. Finally it offers to run `pdms doctor`.

Steps that are already configured are just listed with a ✓, and every step can be skipped (it tells you the command to
do it later).

## Command reference

| Command | What it does |
| --- | --- |
| `pdms` | Interactive menu |
| `pdms setup` | Guided setup: import a shared configuration, then configure what is missing |
| `pdms run` / `pdms debug` / `pdms env` | Run a service / create a VS Code debug configuration / print a profile's variables |
| `pdms services` | List the services of the current repo |
| `pdms repo` | Repos menu (`list`, `add`, `use`, `remove`) |
| `pdms test` / `pdms migrate` | Run a service's tests / Flyway `info`, `validate` (and `migrate` locally) |
| `pdms ps` / `logs` / `urls` / `open` / `stop` / `restart` | Manage background instances |
| `pdms up` / `pdms down` | Start / stop a stack |
| `pdms proxy` | Local API gateway (`routes` to inspect the mapping) |
| `pdms ui` | Web interface with the services, the proxy and the events, live (preview) |
| `pdms events` | Local SQS: map, ElasticMQ and broker (`map`, `up`, `status`, `send`, `peek`, `purge`, `down`) |
| `pdms stack` | Stacks menu (`list`, `add`, `edit`, `remove`) |
| `pdms db` | Databases menu (`list`, `add`, `edit`, `remove`, `test`) |
| `pdms user` | Users menu (`list`, `add`, `edit`, `remove`, `import`) |
| `pdms config` | Settings (`defaults`, `language`, `export`, `import`, `path`, `edit`) |
| `pdms doctor` | Check the environment and the configuration |

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
smart_install = true                      # skip the install when nothing changed
update_check = true                       # tell when a new pdms version is out
banner = true                             # big PDMS banner when the menu opens (or PDMS_NO_BANNER=1)
events = "auto"                           # auto | local | aws: where services publish SQS events
events_port = 9324                        # local ElasticMQ (pdms events up)
db_timeout = 15                           # seconds for `pdms db test` (or `pdms db test -t 30`)

[defaults.env]
# extra variables injected on every run

[users.supervisor]
user_id = "00000000-0000-0000-0000-000000000001"
username = "supervisor@example.com"
first_name = "Jane"
last_name = "Doe"
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

### Export and import

Share the setup with the team or back it up:

```bash
pdms config export                         # asks which sections and whether to include passwords
pdms config export team.toml --no-secrets  # users, databases, stacks and defaults, without passwords
pdms config export backup.toml --secrets   # full backup, passwords included
pdms config export - --only users,stacks   # to stdout, only some sections
pdms config import team.toml               # shows new / changed / unchanged entries and asks what to overwrite
pdms config import team.toml --only dbs --overwrite -y
pdms config import backup.toml --replace   # the selected sections become exactly the file's content
```

- Sections: `defaults`, `users`, `dbs`, `stacks`. The runtime state (last user/DB) is never exported.
- Passwords are left out unless you choose to include them. Importing a database without password keeps the password
  you already have for that alias; new ones without password are listed so you can set them with `pdms db edit`.
- By default the import merges: new entries are added and, for entries that differ, you pick which ones to overwrite
  (without a terminal they are kept unless `--overwrite`). On a first setup, with no configuration yet, everything
  is imported.
- Before writing, the current configuration is copied to `config.toml.bak-<timestamp>`.
- Export files are written with permissions `0600`. Do not commit a file exported with `--secrets`.

The same options are available in the interactive menu under **Settings**.

## Development

```bash
git clone git@github.com:lianabeatriz93/pdms-cli.git ~/Code/Alivi/pdms-cli
cd ~/Code/Alivi/pdms-cli
uv sync                          # .venv with the dev dependencies
uv run pytest
uv tool install -e . --force     # use your checkout as the `pdms` command (re-run when dependencies change)
```

With the editable install, code changes apply immediately and `pdms self-update` tells you to use `git pull`.
If an update adds a dependency and the command fails, it says which one is missing and how to reinstall.

CI (`.github/workflows/ci.yml`) runs the tests on Linux, macOS and Windows (Python 3.10 and 3.12) and installs the
built package with `install.sh` / `install.ps1` on the three systems, on every push.

### Releasing

Versions follow [Conventional Commits](https://www.conventionalcommits.org/) with
[commitizen](https://commitizen-tools.github.io/commitizen/) (`feat` → minor, `fix` → patch while below 1.0) and
[PEP 440](https://peps.python.org/pep-0440/).

Version numbers advance slowly: every release is a **stable patch release** (`0.2.1`, `0.2.2`, `0.2.3`, ...), and
the minor version only goes up when something significant ships:

```
0.2.1  →  0.2.2  →  0.2.3  →  ...  →  (something significant)  →  0.3.0
```

```bash
git checkout main && git pull
uvx --from commitizen cz bump --increment PATCH --dry-run   # preview: e.g. 0.2.1 → 0.2.2
uvx --from commitizen cz bump --increment PATCH
git push --follow-tags
```

`--increment PATCH` keeps commitizen from bumping the minor version for `feat:` commits. For a significant release:

```bash
uvx --from commitizen cz bump --increment MINOR   # e.g. 0.2.7 → 0.3.0
git push --follow-tags
```

`cz bump` updates `pyproject.toml`, `uv.lock` and `CHANGELOG.md`, commits and creates the `vX.Y.Z` tag.
(Pre-release versions such as `0.3.0a1` are still supported, published as GitHub pre-releases and installed with
`PDMS_PRERELEASE=1` / `pdms self-update --pre`, but they are not part of the regular process.)

Pushing the tag runs `.github/workflows/release.yml`: it checks that the tag matches the package version, runs the
tests, builds the wheel and sdist, and publishes a GitHub release with them, both installers and the changelog
section as release notes.


User-facing texts are written in English inside `_()` (from `pdms_cli.i18n`) and translated in the `ES` catalog of
[`src/pdms_cli/i18n.py`](src/pdms_cli/i18n.py). `tests/test_i18n.py` fails if a string has no Spanish translation,
if placeholders differ between languages, or if Spanish text is left outside the catalog.
